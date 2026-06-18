#!/usr/bin/env bun
/**
 * Cross-process lease lock behavior test (bun:ffi flock side).
 *
 * Mirrors sdk-channel/tests/test_lease_lock.py — does NOT import
 * channel/server.ts (would spin up MCP/SSE). Uses the same libc.flock dlopen
 * path that channel/server.ts uses for tryAcquireLeaseLock/releaseLeaseLock.
 *
 * Run:
 *     bun run tests/test_lease_lock.ts
 *
 * Self-recursion (driver spawns holders by re-invoking with --hold):
 *     bun run tests/test_lease_lock.ts --hold <seconds>
 *
 * Step 4 (Python ↔ Bun interlock) requires sdk-channel/tests/test_lease_lock.py.
 * If that file is absent, Step 4 is skipped (within-Bun tests still run).
 */
import { spawnSync, spawn } from 'node:child_process'
import { openSync, closeSync, existsSync, rmSync } from 'node:fs'
import { dlopen, FFIType } from 'bun:ffi'
import { join as pathJoin } from 'node:path'

const TEST_LOCK = '/tmp/ariake-test-lease.lock'
const HOLDER_HOLD_TIME = 3.0
const SETTLE_MS = 500

const LOCK_EX = 2 as const
const LOCK_NB = 4 as const
const LOCK_UN = 8 as const

function loadFlock(): (fd: number, op: number) => number {
  // Keep candidate list in sync with channel/server.ts's _loadFlock —
  // otherwise tests pass while server fails (or vice versa) on edge platforms.
  const candidates =
    process.platform === 'darwin'
      ? ['libc.dylib']
      : ['libc.so.6', 'libc.musl-x86_64.so.1', 'libc.musl-aarch64.so.1', 'libc.so']
  let lastErr: unknown = null
  for (const lib of candidates) {
    try {
      const { symbols } = dlopen(lib, {
        flock: { args: [FFIType.i32, FFIType.i32], returns: FFIType.i32 },
      })
      return symbols.flock as unknown as (fd: number, op: number) => number
    } catch (e) {
      lastErr = e
    }
  }
  throw new Error(`cannot load libc.flock: ${String(lastErr)}`)
}

async function runHolder(holdSec: number): Promise<number> {
  const lockPath = process.env.INKIERAN_LEASE_LOCK_PATH ?? TEST_LOCK
  const flock = loadFlock()
  const fd = openSync(lockPath, 'a+')
  try {
    const rc = flock(fd, LOCK_EX | LOCK_NB)
    if (rc !== 0) {
      console.log('denied')
      return 0
    }
    console.log('acquired')
    await new Promise((r) => setTimeout(r, holdSec * 1000))
    flock(fd, LOCK_UN)
    console.log('released')
    return 0
  } finally {
    try { closeSync(fd) } catch {}
  }
}

function runHolderSpawn(holdSec: number): { stdout: string; status: number } {
  const r = spawnSync('bun', ['run', __filename, '--hold', String(holdSec)], {
    env: { ...process.env, INKIERAN_LEASE_LOCK_PATH: TEST_LOCK },
    encoding: 'utf8',
  })
  return { stdout: (r.stdout ?? '') + (r.stderr ?? ''), status: r.status ?? -1 }
}

async function runDriver(): Promise<number> {
  if (existsSync(TEST_LOCK)) {
    try { rmSync(TEST_LOCK) } catch {}
  }
  const failures: string[] = []

  console.log('=== Step 1: A holds 3s; B starts 500ms later — B must be denied ===')
  const a = spawn('bun', ['run', __filename, '--hold', String(HOLDER_HOLD_TIME)], {
    env: { ...process.env, INKIERAN_LEASE_LOCK_PATH: TEST_LOCK },
  })
  let aOut = ''
  a.stdout.on('data', (d) => (aOut += d.toString()))
  a.stderr.on('data', (d) => (aOut += d.toString()))

  await new Promise((r) => setTimeout(r, SETTLE_MS))

  const b = runHolderSpawn(0.1)
  await new Promise<void>((resolve) => a.on('exit', () => resolve()))

  console.log(`  A -> ${JSON.stringify(aOut.trim())}`)
  console.log(`  B -> ${JSON.stringify(b.stdout.trim())}`)
  if (!aOut.includes('acquired') || !aOut.includes('released')) {
    failures.push('Step 1: A should be acquired + released')
  }
  if (!b.stdout.includes('denied')) {
    failures.push('Step 1: B should be denied (A still holding)')
  }

  console.log()
  console.log('=== Step 2: After A releases, C should be acquired ===')
  const c = runHolderSpawn(0.1)
  console.log(`  C -> ${JSON.stringify(c.stdout.trim())}`)
  if (!c.stdout.includes('acquired')) {
    failures.push('Step 2: C should be acquired after A released')
  }

  console.log()
  console.log('=== Step 3: D and E race — exactly one acquired, exactly one denied ===')
  const dProm = new Promise<string>((resolve) => {
    const p = spawn('bun', ['run', __filename, '--hold', '1.0'], {
      env: { ...process.env, INKIERAN_LEASE_LOCK_PATH: TEST_LOCK },
    })
    let buf = ''
    p.stdout.on('data', (d) => (buf += d.toString()))
    p.stderr.on('data', (d) => (buf += d.toString()))
    p.on('exit', () => resolve(buf))
  })
  const eProm = new Promise<string>((resolve) => {
    const p = spawn('bun', ['run', __filename, '--hold', '1.0'], {
      env: { ...process.env, INKIERAN_LEASE_LOCK_PATH: TEST_LOCK },
    })
    let buf = ''
    p.stdout.on('data', (d) => (buf += d.toString()))
    p.stderr.on('data', (d) => (buf += d.toString()))
    p.on('exit', () => resolve(buf))
  })
  const [dOut, eOut] = await Promise.all([dProm, eProm])
  console.log(`  D -> ${JSON.stringify(dOut.trim())}`)
  console.log(`  E -> ${JSON.stringify(eOut.trim())}`)
  const acqCount = (dOut.includes('acquired') ? 1 : 0) + (eOut.includes('acquired') ? 1 : 0)
  const denCount = (dOut.includes('denied') ? 1 : 0) + (eOut.includes('denied') ? 1 : 0)
  if (acqCount !== 1 || denCount !== 1) {
    failures.push(
      `Step 3: race should yield exactly 1 acquired + 1 denied; got ${acqCount} acquired / ${denCount} denied`,
    )
  }

  console.log()
  console.log('=== Step 4: Python <-> Bun cross-implementation interlock ===')
  // The real contract is Python fcntl.flock <-> Bun libc.flock interlock,
  // not just Bun <-> Bun. Reuses sdk-channel/tests/test_lease_lock.py's
  // --hold holder pattern. If that file is absent, Step 4 is skipped.
  const pyHolderScript = pathJoin(
    import.meta.dir,
    '..', 'sdk-channel', 'tests', 'test_lease_lock.py',
  )
  if (!existsSync(pyHolderScript)) {
    console.log(`  skip: ${pyHolderScript} not present (Python-side test not provided)`)
  } else {
    // 4a) Python holds 1.5s; Bun tries — Bun should be denied
    const pyA = spawn('python3', [pyHolderScript, '--hold', '1.5'], {
      env: { ...process.env, INKIERAN_LEASE_LOCK_PATH: TEST_LOCK },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let pyAOut = ''
    pyA.stdout?.on('data', (d) => (pyAOut += d.toString()))
    pyA.stderr?.on('data', (d) => (pyAOut += d.toString()))

    await new Promise((r) => setTimeout(r, SETTLE_MS))
    const bunB = runHolderSpawn(0.1)
    await new Promise<void>((resolve) => pyA.on('exit', () => resolve()))

    console.log(`  Python A -> ${JSON.stringify(pyAOut.trim())}`)
    console.log(`  Bun    B -> ${JSON.stringify(bunB.stdout.trim())}`)
    if (!pyAOut.includes('acquired')) failures.push('Step 4a: Python A should be acquired')
    if (!bunB.stdout.includes('denied'))
      failures.push('Step 4a: Bun B should be denied (Python holding)')

    // 4b) Bun holds 1.5s; Python tries — Python should be denied
    const bunC = spawn('bun', ['run', __filename, '--hold', '1.5'], {
      env: { ...process.env, INKIERAN_LEASE_LOCK_PATH: TEST_LOCK },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let bunCOut = ''
    bunC.stdout?.on('data', (d) => (bunCOut += d.toString()))
    bunC.stderr?.on('data', (d) => (bunCOut += d.toString()))

    await new Promise((r) => setTimeout(r, SETTLE_MS))
    const pyD = spawnSync('python3', [pyHolderScript, '--hold', '0.1'], {
      env: { ...process.env, INKIERAN_LEASE_LOCK_PATH: TEST_LOCK },
      encoding: 'utf8',
    })
    await new Promise<void>((resolve) => bunC.on('exit', () => resolve()))

    console.log(`  Bun    C -> ${JSON.stringify(bunCOut.trim())}`)
    console.log(`  Python D -> ${JSON.stringify((pyD.stdout ?? '').trim())}`)
    if (!bunCOut.includes('acquired')) failures.push('Step 4b: Bun C should be acquired')
    if (!(pyD.stdout ?? '').includes('denied'))
      failures.push('Step 4b: Python D should be denied (Bun holding)')
  }

  console.log()
  if (failures.length) {
    console.log('=== FAIL ===')
    for (const f of failures) console.log(`  - ${f}`)
    return 1
  }
  console.log('=== ALL PASS ===')
  return 0
}

async function main() {
  const args = process.argv.slice(2)
  const holdIdx = args.indexOf('--hold')
  if (holdIdx !== -1) {
    const sec = Number(args[holdIdx + 1])
    process.exit(await runHolder(isFinite(sec) ? sec : 1))
  }
  process.exit(await runDriver())
}

main()
