#!/usr/bin/env bun
/**
 * Ariake tmux transport — channel/server.ts
 * -----------------------------------------
 * Bridges an external HTTP API into a tmux-resident Claude Code session and
 * relays CC's replies back out over SSE. Implements the lease protocol from
 * docs/lease-protocol.md and the fire-and-forget sdk -> tmux resume path from
 * docs/sdk-tmux-resume-proposal.md.
 *
 * Wire:
 *   HTTP POST /message        -> tmux send-keys injects <channel> tag into CC
 *   CC calls reply tool       -> broadcast to SSE GET /events
 *   POST /lease/{acquire,release} + GET /session/current — lease protocol
 *
 * Key constraints:
 *   - `send-keys -l` interprets \n as Enter; any payload reaching it MUST be
 *     single-line. We strip \r/\n/U+2028/U+2029 and C0 control chars before
 *     injection. Skipping this fragments the tag and CC sees malformed XML.
 *   - XML escape body + attributes to prevent </channel> injection from user
 *     content.
 *
 * See docs/architecture.md for the contract and docs/review-trail.md for the
 * five-round review history that produced the current shape.
 */
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { execFileSync } from 'child_process'
import {
  resolve as pathResolve,
  relative as pathRelative,
  isAbsolute as pathIsAbsolute,
  join as pathJoin,
} from 'node:path'
import { openSync, closeSync } from 'node:fs'
import { readdir, stat, writeFile, rm } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dlopen, FFIType } from 'bun:ffi'

const PORT = Number(process.env.ARIAKE_PORT ?? 8910)
const TMUX_TARGET = `${process.env.ARIAKE_TMUX ?? 'inkieran'}:0`
const UPLOADS_DIR = process.env.ARIAKE_UPLOADS ?? '/var/lib/ariake/uploads'
const SOURCE_NAME = process.env.ARIAKE_SOURCE ?? 'ariake'
// CC's cwd (the directory tmux's `claude` runs in). MUST match the SDK transport's
// ARIAKE_SDK_CWD so the two share the same jsonl pool and the same lock file.
const ARIAKE_CWD = process.env.ARIAKE_CWD ?? '/opt/ariake/cc-cwd'
const LEASE_LOCK_PATH = process.env.ARIAKE_LEASE_LOCK_PATH ?? pathJoin(ARIAKE_CWD, '.lease.lock')
const AUTOSTART = process.env.ARIAKE_AUTOSTART !== '0'
// Heuristic: assume a CC turn finishes within this many ms after the last inject.
// Used by /lease/release post-margin (tmux CC exposes no turn-end signal).
const DRAIN_MARGIN_MS = Number(process.env.ARIAKE_DRAIN_MARGIN_MS ?? 8000)
const DEFAULT_DRAIN_TIMEOUT_SEC = Number(process.env.ARIAKE_DRAIN_TIMEOUT ?? 30)
// File the cc startup script (templates/cc.sh.patch.sh) reads on boot to get
// the sid to `claude --resume <sid>` with.
const RESUME_SID_FILE = process.env.ARIAKE_RESUME_SID_FILE ?? '/tmp/ariake-cc-resume-sid'
// The CC service unit name systemctl restart targets. Match this to the unit
// the sudoers entry whitelists.
const CC_SERVICE_NAME = process.env.ARIAKE_CC_SERVICE ?? 'ariake-cc.service'

const enc = new TextEncoder()

// ── XML / line sanitization ──────────────────────────────────────────
const escXml = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const escAttr = (s: string) => escXml(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;')
// Strip every line-break form (\r/\n/U+2028/U+2029 -> space) + C0 controls
// (\x00-\x1F except \t, plus \x7F). `send-keys -l` treats \n as Enter and may
// also interpret control sequences as key chords, so any of these in the
// payload fragments the tag.
const toSafeLine = (s: string) =>
  s.replace(/[\r\n  ]+/g, ' ').replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, '')
const safeAttr = (s: string) => escAttr(toSafeLine(s).trim())

// Path sandbox: use path.resolve + path.relative to defeat trailing-`..`
// traversal. A naive `(p+'/').startsWith(root)` lets `/uploads/..` through.
function isWithinUploads(p: string): boolean {
  try {
    const norm = pathResolve(p)
    const rootNorm = pathResolve(UPLOADS_DIR)
    if (norm === rootNorm) return false
    const rel = pathRelative(rootNorm, norm)
    return rel !== '' && !rel.startsWith('..') && !pathIsAbsolute(rel)
  } catch {
    return false
  }
}

// ── SSE outbound ──────────────────────────────────────────────────────
type OutboundAttachment = { path: string; filename?: string; type?: 'image' | 'file' }
type OutboundEvent = {
  type: 'reply'
  chat_id: string
  message_id: string
  reply_to?: string
  text: string
  ts: string
  attachments?: OutboundAttachment[]
}
const listeners = new Set<(chunk: string) => void>()
const EVENT_RING_MAX = 100
const eventRing: { id: number; frame: string }[] = []
let seq = 0
// id basis: Date.now() means ids stay monotonic across process restarts so
// subscribers holding an old Last-Event-ID from a previous process won't filter
// out new events from the current one.
let eventSeq = Date.now()
const nextId = (prefix: string) => `${prefix}${Date.now()}-${++seq}`

function broadcast(ev: OutboundEvent) {
  const id = ++eventSeq
  const frame = `id: ${id}\ndata: ${JSON.stringify(ev)}\n\n`
  eventRing.push({ id, frame })
  if (eventRing.length > EVENT_RING_MAX) eventRing.shift()
  for (const emit of listeners) emit(frame)
}

function replaySince(lastEventId: string | null, emit: (chunk: string) => void) {
  if (!lastEventId) return
  const last = Number(lastEventId)
  if (!Number.isFinite(last)) return
  // If last > our max id, this subscriber is holding an id from a previous
  // process generation — replay the whole buffer rather than filter to nothing.
  const maxId = eventRing.length > 0 ? eventRing[eventRing.length - 1].id : 0
  if (last > maxId) {
    for (const ev of eventRing) emit(ev.frame)
    return
  }
  for (const ev of eventRing) {
    if (ev.id > last) emit(ev.frame)
  }
}

// ── MCP server: reply tool (outbound) ────────────────────────────────
const mcp = new Server(
  { name: 'ariake-channel', version: '0.1.0' },
  {
    capabilities: { tools: {} },
    instructions:
      `Inbound messages arrive as <channel source="${SOURCE_NAME}" chat_id="..." ` +
      'message_id="..." user="..." ts="...">…</channel>. Inbound attachments appear ' +
      'inline as [attachment N -> /abs/path] — Read that path to access the file. ' +
      'Your transcript output never reaches the user — reply ONLY through the reply tool. ' +
      'Pass chat_id from the inbound tag to route the reply; set reply_to to the inbound ' +
      'message_id to thread it.',
  },
)

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'reply',
      description:
        'Send a message back to the inbound user. Pass chat_id (from the inbound ' +
        '<channel> tag) to route, optional reply_to to thread, optional attachments. ' +
        `To send files, first save them to ${UPLOADS_DIR}, then list their absolute paths.`,
      inputSchema: {
        type: 'object',
        properties: {
          chat_id: { type: 'string', description: 'Conversation to reply in (from the inbound tag).' },
          text: { type: 'string', description: 'Message text (may be empty when only sending attachments).' },
          reply_to: { type: 'string', description: 'Inbound message_id this answers (optional).' },
          attachments: {
            type: 'array',
            description: `Files to send back. Save into ${UPLOADS_DIR} first, then list absolute paths.`,
            items: {
              type: 'object',
              properties: {
                path: { type: 'string', description: `Absolute path; must live inside ${UPLOADS_DIR}.` },
                filename: { type: 'string', description: 'Display name (optional; defaults to basename).' },
                type: { type: 'string', enum: ['image', 'file'], description: 'Optional; inferred from extension.' },
              },
              required: ['path'],
            },
          },
        },
        required: ['chat_id'],
      },
    },
  ],
}))

// Runtime validation of outbound attachments. inputSchema enforces shape; this
// enforces content (absolute, sandboxed, no control chars).
function validateOutboundAttachments(raw: unknown): OutboundAttachment[] {
  if (!Array.isArray(raw)) return []
  const out: OutboundAttachment[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const a = item as Record<string, unknown>
    const path = typeof a.path === 'string' ? a.path : ''
    if (!path) continue
    if (/[\r\n\x00-\x1F\x7F]/.test(path)) continue
    if (!path.startsWith('/')) continue
    if (!isWithinUploads(path)) continue
    const cleaned: OutboundAttachment = { path }
    if (typeof a.filename === 'string') cleaned.filename = a.filename.replace(/[\r\n\x00-\x1F\x7F]/g, '')
    if (a.type === 'image' || a.type === 'file') cleaned.type = a.type
    out.push(cleaned)
  }
  return out
}

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  const name = req.params.name
  if (name !== 'reply') {
    return { content: [{ type: 'text', text: `unknown tool: ${name}` }], isError: true }
  }
  const args = (req.params.arguments ?? {}) as Record<string, unknown>
  const text = String(args.text ?? '')
  const chat_id_raw = args.chat_id
  if (typeof chat_id_raw !== 'string' || !chat_id_raw.trim()) {
    return {
      content: [{ type: 'text', text: 'reply: chat_id required (copy from inbound <channel> tag)' }],
      isError: true,
    }
  }
  const chat_id = chat_id_raw.trim()
  const reply_to = args.reply_to ? String(args.reply_to) : undefined
  const message_id = nextId('a')
  const attachments = validateOutboundAttachments(args.attachments)
  if (!text.trim() && attachments.length === 0) {
    return {
      content: [
        {
          type: 'text',
          text:
            'reply: text and attachments cannot both be empty (or attachments were all ' +
            `rejected — check that each path is absolute and inside ${UPLOADS_DIR})`,
        },
      ],
      isError: true,
    }
  }
  broadcast({ type: 'reply', chat_id, message_id, reply_to, text, ts: new Date().toISOString(), attachments })
  return { content: [{ type: 'text', text: `sent (${message_id}) -> ${listeners.size} listener(s)` }] }
})

await mcp.connect(new StdioServerTransport())

// ── Cross-process lease (flock via bun:ffi) ──────────────────────────
// The SDK transport (Python) uses fcntl.flock on the SAME lock file. Both go
// through the kernel advisory-lock table on the same inode -> interlock.
// tests/test_lease_lock.ts exercises the cross-impl interlock.

const LOCK_EX = 2 as const
const LOCK_NB = 4 as const
const LOCK_UN = 8 as const

let _flock: ((fd: number, op: number) => number) | null = null

function _loadFlock(): (fd: number, op: number) => number {
  if (_flock) return _flock
  // libc soname varies by platform / libc impl. Try the common ones; the first
  // one that loads wins. Alpine/musl extras are best-effort — if none load,
  // we throw and the process fails to start (better than a silent no-lock run).
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
      _flock = symbols.flock as unknown as (fd: number, op: number) => number
      return _flock
    } catch (e) {
      lastErr = e
    }
  }
  throw new Error(`cannot load libc.flock: ${lastErr instanceof Error ? lastErr.message : String(lastErr)}`)
}

let _leaseLockFd: number | null = null
let acceptMessages = true
let lastInjectAt = 0

function tryAcquireLeaseLock(): 'acquired' | 'already_held' | 'denied' {
  if (_leaseLockFd !== null) return 'already_held'
  const flock = _loadFlock()
  // 'a+' = O_RDWR|O_APPEND|O_CREAT; we only use the fd for flock(2), not for
  // I/O. The file's content is irrelevant — the lock lives on the inode.
  const fd = openSync(LEASE_LOCK_PATH, 'a+')
  const rc = flock(fd, LOCK_EX | LOCK_NB)
  if (rc !== 0) {
    // Non-zero (libc returns -1 on failure). 99% of the time this is
    // EWOULDBLOCK (held by another process); ENOLCK / EINVAL on weird FSes
    // are also possible but rare. We can't easily read errno from bun:ffi
    // without dlopen'ing __errno_location; treat all non-zero as denied.
    try { closeSync(fd) } catch {}
    return 'denied'
  }
  _leaseLockFd = fd
  return 'acquired'
}

function releaseLeaseLock(): void {
  if (_leaseLockFd === null) return
  try { _loadFlock()(_leaseLockFd, LOCK_UN) } catch {}
  try { closeSync(_leaseLockFd) } catch {}
  _leaseLockFd = null
}

// state lock serializes acquire/release. Without it, release's internal await
// (drain, setTimeout) can be raced by a concurrent acquire flipping the same
// state and ending up with acceptMessages=true + lock=false.
let _stateLockChain: Promise<void> = Promise.resolve()
async function withStateLock<T>(fn: () => Promise<T>): Promise<T> {
  const prev = _stateLockChain
  let releaseNext: () => void
  _stateLockChain = new Promise<void>((r) => { releaseNext = r })
  await prev
  try {
    return await fn()
  } finally {
    releaseNext!()
  }
}

// AdmissionTicket — a sync ticket issued at endpoint entry closes the TOCTOU
// window between "check acceptMessages" and the eventual deliver/inject call.
// release's drain waits on `_inFlight == 0`, so once a ticket exists, release
// must wait for its release before unlocking.
let _inFlight = 0
const _drainResolvers: Array<() => void> = []

class AdmissionTicket {
  private released = false
  release(): void {
    if (this.released) return
    this.released = true
    _inFlight--
    if (_inFlight <= 0) {
      _inFlight = 0
      const rs = _drainResolvers.splice(0)
      for (const r of rs) r()
    }
  }
}

function admitSync(): AdmissionTicket | null {
  if (!acceptMessages) return null
  _inFlight++
  return new AdmissionTicket()
}

function drainInFlight(timeoutMs: number): Promise<boolean> {
  if (_inFlight === 0) return Promise.resolve(true)
  return new Promise<boolean>((resolve) => {
    let settled = false
    const onDrained = () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(true)
    }
    _drainResolvers.push(onDrained)
    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      const idx = _drainResolvers.indexOf(onDrained)
      if (idx >= 0) _drainResolvers.splice(idx, 1)
      resolve(false)
    }, timeoutMs)
  })
}

// ── session_id lookup ────────────────────────────────────────────────
// Read up to 1MB from a jsonl file's head and parse the first complete JSON
// line that carries a `cwd` field. The first line of a CC jsonl includes the
// cwd among its top-level fields, so we don't need to scan further.
const SESSION_HEAD_MAX_BYTES = 1024 * 1024

async function _readSessionCwd(fp: string): Promise<string | null> {
  const file = Bun.file(fp)
  const truncated = file.size > SESSION_HEAD_MAX_BYTES
  const head = await file.slice(0, SESSION_HEAD_MAX_BYTES).text()
  let safeText: string
  if (truncated) {
    // File larger than our read budget: the tail of `head` may be a partial
    // line. Drop everything past the last \n so JSON.parse doesn't choke.
    const lastNL = head.lastIndexOf('\n')
    if (lastNL < 0) return null
    safeText = head.slice(0, lastNL)
  } else {
    safeText = head
  }
  for (const line of safeText.split('\n')) {
    if (!line) continue
    let obj: any
    try { obj = JSON.parse(line) } catch { continue }
    if (obj && typeof obj.cwd === 'string') return obj.cwd
  }
  return null
}

async function getCurrentSessionId(): Promise<string | null> {
  // Prefer the env hint over jsonl mtime: `claude --resume <sid>` doesn't
  // write to its jsonl until the first turn, so mtime races against the
  // coordinator's poll. The cc startup script sets ARIAKE_EXPECTED_SESSION_ID
  // when it launches `claude` with --resume.
  const expected = process.env.ARIAKE_EXPECTED_SESSION_ID
  if (expected && /^[0-9a-f-]{8,64}$/i.test(expected)) {
    return expected
  }
  // Fallback: scan ~/.claude/projects/*/*.jsonl for files whose `cwd` matches
  // ours, then return the most recently modified.
  const root = pathJoin(homedir(), '.claude', 'projects')
  let projectDirs: string[]
  try { projectDirs = await readdir(root) } catch { return null }
  const candidates: Array<{ sid: string; mtime: number }> = []
  for (const projDir of projectDirs) {
    const dir = pathJoin(root, projDir)
    let files: string[]
    try { files = (await readdir(dir)).filter((f) => f.endsWith('.jsonl')) } catch { continue }
    for (const f of files) {
      const fp = pathJoin(dir, f)
      try {
        const st = await stat(fp)
        const cwd = await _readSessionCwd(fp)
        if (cwd === ARIAKE_CWD) {
          candidates.push({ sid: f.replace(/\.jsonl$/, ''), mtime: st.mtimeMs })
        }
      } catch { continue }
    }
  }
  if (!candidates.length) return null
  candidates.sort((a, b) => b.mtime - a.mtime)
  return candidates[0].sid
}

// ── shutdown ─────────────────────────────────────────────────────────
let _shuttingDown = false
function shutdown() {
  if (_shuttingDown) return
  _shuttingDown = true
  // OS will release advisory lock on fd close anyway, but explicit release is
  // clearer in logs and tests.
  releaseLeaseLock()
  process.exit(0)
}
process.on('SIGTERM', shutdown)
process.on('SIGINT', shutdown)

// AUTOSTART: try to acquire the lock; if held by another process, fall back to
// standby. Standby returns 503 on /message + /tick; backend can promote us by
// calling /lease/acquire after releasing the holder.
if (AUTOSTART) {
  const state = tryAcquireLeaseLock()
  if (state === 'denied') {
    acceptMessages = false
    process.stderr.write(
      `ariake-channel: lease lock held by another process at ${LEASE_LOCK_PATH}; ` +
        `falling back to standby (acceptMessages=false)\n`,
    )
  } else {
    acceptMessages = true
    process.stderr.write(`ariake-channel: lease lock acquired (${LEASE_LOCK_PATH})\n`)
  }
} else {
  acceptMessages = false
  process.stderr.write(`ariake-channel: standby mode (ARIAKE_AUTOSTART=0); waiting for /lease/acquire\n`)
}

// ── inbound: send-keys ────────────────────────────────────────────────
type InboundAttachment = { filename?: string; path: string; is_image?: boolean }

// Injection primitive. Both /message (user input) and /tick (timer envelopes)
// route through here so the timing guards live in one place.
//
// KNOWN LIMITATION: send-keys is blind. If CC's pane is showing a modal
// (permission prompt, plugin menu, loading screen), the keystrokes are eaten
// by the modal and the message is silently lost. We acknowledge over HTTP
// regardless. Detecting modal state via capture-pane has been tried and is
// too flaky; a real fix needs CC to expose an "idle/ready" signal.
function injectLine(line: string) {
  // Pre-clear the input buffer with C-u. A leftover draft (e.g. from a CC
  // `--continue` startup that re-populated input) would concatenate with our
  // injection and produce a malformed tag.
  execFileSync('tmux', ['send-keys', '-t', TMUX_TARGET, 'C-u'], { timeout: 3000 })
  Bun.sleepSync(50)
  // Body: -l = literal (treat input as literal text, not key bindings).
  execFileSync('tmux', ['send-keys', '-t', TMUX_TARGET, '-l', line], { timeout: 3000 })
  // Enter must NOT be flush against the body. CC's input layer batches
  // fast-arriving keystrokes as "paste"; an Enter inside that paste window is
  // treated as a newline insertion, not a submit. Floor ~80ms; we use 120ms
  // for margin under load. (sleepSync blocks the event loop, which is fine
  // here — low-frequency single-user inject.)
  Bun.sleepSync(120)
  execFileSync('tmux', ['send-keys', '-t', TMUX_TARGET, 'Enter'], { timeout: 3000 })
  lastInjectAt = Date.now()
}

function validateInboundAttachments(raw: unknown): InboundAttachment[] {
  if (!Array.isArray(raw)) return []
  const out: InboundAttachment[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const a = item as Record<string, unknown>
    const path = typeof a.path === 'string' ? a.path : ''
    if (!path) continue
    if (/[\r\n\x00-\x1F\x7F]/.test(path)) continue
    if (!path.startsWith('/')) continue
    if (!isWithinUploads(path)) continue
    const cleaned: InboundAttachment = { path }
    if (typeof a.filename === 'string') cleaned.filename = a.filename
    if (typeof a.is_image === 'boolean') cleaned.is_image = a.is_image
    out.push(cleaned)
  }
  return out
}

function deliver(
  chat_id: string,
  message_id: string,
  text: string,
  attachments: InboundAttachment[] = [],
  user = 'user',
) {
  // Inject "[attachment N -> /abs/path]" prefixes (not original filenames):
  // file names from user input are a prompt-injection surface and may also
  // contain characters that break the tag.
  const attachPrefix = attachments.map((a, i) => `[attachment ${i + 1} -> ${a.path}]`).join(' ')
  const merged = [attachPrefix, text].filter(Boolean).join(' ')
  const safe = escXml(toSafeLine(merged))
  const ts = new Date().toISOString()
  const tag =
    `<channel source="${SOURCE_NAME}" chat_id="${safeAttr(chat_id)}" ` +
    `message_id="${escAttr(message_id)}" user="${safeAttr(user)}" ts="${escAttr(ts)}">${safe}</channel>`
  injectLine(tag)
}

// ── HTTP server ───────────────────────────────────────────────────────
Bun.serve({
  port: PORT,
  hostname: '127.0.0.1',
  idleTimeout: 0, // don't close idle SSE long-polls
  async fetch(req) {
    const url = new URL(req.url)

    // GET /events — SSE outbound stream
    if (req.method === 'GET' && url.pathname === '/events') {
      const stream = new ReadableStream({
        start(ctrl) {
          ctrl.enqueue(enc.encode(': connected\n\n'))
          const emit = (chunk: string) => {
            try {
              ctrl.enqueue(enc.encode(chunk))
            } catch {
              // Stream closed (client disconnected). Drop the listener
              // immediately or every subsequent broadcast leaks memory.
              listeners.delete(emit)
            }
          }
          listeners.add(emit)
          replaySince(req.headers.get('last-event-id'), emit)
          req.signal.addEventListener('abort', () => listeners.delete(emit))
        },
      })
      return new Response(stream, {
        headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' },
      })
    }

    // POST /message — inbound user message
    if (req.method === 'POST' && url.pathname === '/message') {
      // Sync admission ticket BEFORE awaiting body parse. The naive form (await
      // body, then check acceptMessages) has a TOCTOU window during which
      // release can flip acceptMessages and drain to zero — our turn would
      // then race the file-lock release.
      const ticket = admitSync()
      if (!ticket) return Response.json({ error: 'lease released; transport idle' }, { status: 503 })
      try {
        let text = ''
        let chat_id = 'default'
        let user = 'user'
        let attachments: InboundAttachment[] = []
        const ct = req.headers.get('content-type') ?? ''
        if (ct.includes('application/json')) {
          const body = (await req.json().catch(() => ({}))) as Record<string, unknown>
          text = String(body.text ?? '')
          if (body.chat_id) chat_id = String(body.chat_id)
          if (body.user) user = String(body.user)
          if (Array.isArray(body.attachments)) attachments = validateInboundAttachments(body.attachments)
        } else {
          text = await req.text()
        }
        if (!text.trim() && attachments.length === 0)
          return Response.json({ error: 'empty message' }, { status: 400 })
        const message_id = nextId('u')
        try {
          deliver(chat_id, message_id, text.trim(), attachments, user)
        } catch (err) {
          return Response.json(
            { error: `tmux inject failed (session ${TMUX_TARGET}?): ${err instanceof Error ? err.message : err}` },
            { status: 502 },
          )
        }
        return Response.json({ ok: true, chat_id, message_id }, { status: 202 })
      } finally {
        ticket.release()
      }
    }

    // POST /tick — verbatim envelope injection (timers, heartbeats, etc.)
    // The host has pre-built the full <channel source="..."> tag; we don't
    // wrap or escape, only sanitize control chars.
    if (req.method === 'POST' && url.pathname === '/tick') {
      const ticket = admitSync()
      if (!ticket) return Response.json({ error: 'lease released; transport idle' }, { status: 503 })
      try {
        const body = (await req.json().catch(() => ({}))) as Record<string, unknown>
        const payload = toSafeLine(String(body.payload ?? '')).trim()
        if (!payload) return Response.json({ error: 'empty payload' }, { status: 400 })
        try {
          injectLine(payload)
        } catch (err) {
          return Response.json(
            { error: `tmux inject failed (session ${TMUX_TARGET}?): ${err instanceof Error ? err.message : err}` },
            { status: 502 },
          )
        }
        return Response.json({ ok: true }, { status: 202 })
      } finally {
        ticket.release()
      }
    }

    // POST /lease/acquire {resume?, timeout?}
    //
    // Three modes:
    //   1. resume given + valid sid:
    //      a) lock-first: try to acquire flock; denied -> 409, CC untouched
    //      b) write sid to RESUME_SID_FILE for the cc startup script
    //      c) spawn `sudo -n systemctl restart --no-block <CC_SERVICE_NAME>`,
    //         await quick exit with 3s timeout
    //      d) immediately return 200 {restart_pending: true}; channel process
    //         will be killed by systemd shortly (after HTTP response flushes)
    //   2. no resume + standby -> active: just acquire flock + acceptMessages=true
    //   3. no resume + already active: idempotent no-op
    //
    // Reorder hazards (NEVER reorder these):
    //   - "restart then acquire" leaves CC running new sid even if lock fails
    //     -> half switch where two transports both think they own the writer
    //   - "skip the lock check" lets two transports race onto the same jsonl
    //     -> corrupt session
    //
    // See docs/sdk-tmux-resume-proposal.md and docs/review-trail.md for the
    // five rounds of review that produced this layout.
    if (req.method === 'POST' && url.pathname === '/lease/acquire') {
      const body = (await req.json().catch(() => ({}))) as Record<string, unknown>
      const resume = typeof body.resume === 'string' ? body.resume : null
      const bodyTimeoutSec =
        typeof body.timeout === 'number' && isFinite(body.timeout) ? body.timeout : DEFAULT_DRAIN_TIMEOUT_SEC

      const result = await withStateLock(async () => {
        if (resume) {
          // sid format check FIRST: no side effects, just reject malformed
          // input before we touch the lock or CC. Also protects against shell
          // injection if the sid eventually feeds a shell-launched binary.
          if (!/^[0-9a-f-]{8,64}$/i.test(resume)) {
            return {
              status: 400 as const,
              body: { ok: false, error: 'invalid resume sid format', sid_preview: resume.slice(0, 16) },
            }
          }
          // 20s floor: systemctl(~12s budget) + sleep(~3s) + 5s safety. The
          // coordinator clamps payload.timeout to >=30s, so this is a sanity
          // check that catches misconfigured direct callers.
          if (bodyTimeoutSec < 20) {
            return {
              status: 400 as const,
              body: {
                ok: false,
                error: `body.timeout=${bodyTimeoutSec}s too small for resume path; min 20s required`,
                hint: 'coordinator should clamp payload.timeout to >=30s',
              },
            }
          }
        }
        const lockState = tryAcquireLeaseLock()
        if (lockState === 'denied') {
          return {
            status: 409 as const,
            body: {
              ok: false,
              error: 'lease lock held by another process',
              lock_path: LEASE_LOCK_PATH,
              resumed: null,
            },
          }
        }
        if (resume) {
          // Save acceptMessages so any cleanup path can restore it.
          const wasAccepting = acceptMessages
          acceptMessages = false
          const releaseOnFail = () => {
            if (lockState === 'acquired') releaseLeaseLock()
            else if (lockState === 'already_held' && wasAccepting) acceptMessages = true
          }
          try {
            await writeFile(RESUME_SID_FILE, resume, { mode: 0o644 })
          } catch (e) {
            releaseOnFail()
            return {
              status: 500 as const,
              body: {
                ok: false,
                error: `write ${RESUME_SID_FILE} failed: ${e instanceof Error ? e.message : String(e)}`,
              },
            }
          }
          process.stderr.write(
            `ariake-channel: /lease/acquire resume=${resume.slice(0, 8)}… -> ` +
              `spawn restart --no-block, await quick exit\n`,
          )
          // stdout:'ignore' prevents systemctl output from contaminating this
          // process's stdout (the MCP JSON-RPC channel). stderr:'inherit' lets
          // systemd journal capture errors via journalctl -u <service>.
          let restartProc: ReturnType<typeof Bun.spawn>
          try {
            restartProc = Bun.spawn(
              ['sudo', '-n', '/bin/systemctl', 'restart', '--no-block', CC_SERVICE_NAME],
              { stdout: 'ignore', stderr: 'inherit' },
            )
          } catch (e) {
            try { await rm(RESUME_SID_FILE, { force: true }) } catch {}
            releaseOnFail()
            return {
              status: 500 as const,
              body: {
                ok: false,
                error: `spawn systemctl restart failed: ${e instanceof Error ? e.message : String(e)}`,
              },
            }
          }
          // Await quick exit with 3s timeout. `systemctl --no-block` typically
          // returns in <500ms; a hang means sudoers misconfig. Non-zero exit
          // = systemctl error; we clean up and 500 so the coordinator rolls
          // back instead of polling forever for a restart that never happened.
          let restartExit: number | null = null
          let restartTimedOut = false
          const restartTimeoutHandle = setTimeout(() => {
            restartTimedOut = true
            try { restartProc.kill() } catch {}
          }, 3000)
          try {
            restartExit = await restartProc.exited
          } catch {}
          clearTimeout(restartTimeoutHandle)
          if (restartTimedOut || restartExit !== 0) {
            try { await rm(RESUME_SID_FILE, { force: true }) } catch {}
            releaseOnFail()
            return {
              status: 500 as const,
              body: {
                ok: false,
                error: restartTimedOut
                  ? `systemctl --no-block hung (>3s, sudoers misconfig?)`
                  : `systemctl --no-block exit=${restartExit} (sudoers misconfig or systemctl error?)`,
              },
            }
          }
          // systemd has accepted the D-Bus request and will async-process the
          // stop/start. This process will be killed shortly (claude die -> bun
          // MCP child die). Return 200 now; the response flushes to socket in
          // the ~100-500ms window before kill.
          return {
            status: 200 as const,
            body: {
              ok: true,
              active: true,
              lock: lockState,
              resumed: resume,
              restart_pending: true,
              poll_url: '/session/current',
              poll_match: { session_id: resume, active: true },
            },
          }
        }
        // No resume — straight standby -> active (or already_held no-op).
        acceptMessages = true
        return {
          status: 200 as const,
          body: { ok: true, active: true, lock: lockState, resumed: resume ?? null },
        }
      })
      return Response.json(result.body, { status: result.status })
    }

    // POST /lease/release {timeout?}
    //
    // Two-phase drain:
    //   phase 1: ticket drain (precise) — wait for all admitted in-flight to
    //     finish; if it doesn't drain in `timeout`, rollback acceptMessages
    //     and return 408. The lock is NOT released.
    //   phase 2: post-margin (heuristic) — give the tmux CC turn time. tmux
    //     has no turn-end signal we can listen on; we proxy with
    //     lastInjectAt + DRAIN_MARGIN_MS.
    //
    // KNOWN RISK (phase 2): turns longer than the margin race the unlock.
    // The other transport could acquire the lock and start writing while CC
    // is still appending to the jsonl. Mitigations are limited because the
    // signal we want doesn't exist. In practice this is rare: switches are
    // human-triggered, infrequent, and CC turns typically complete in <margin.
    if (req.method === 'POST' && url.pathname === '/lease/release') {
      const body = (await req.json().catch(() => ({}))) as Record<string, unknown>
      const timeoutSec =
        typeof body.timeout === 'number' && isFinite(body.timeout) ? body.timeout : DEFAULT_DRAIN_TIMEOUT_SEC
      const timeoutMs = Math.max(0, timeoutSec * 1000)
      const result = await withStateLock(async () => {
        const wasActive = acceptMessages
        acceptMessages = false
        const drainStart = Date.now()
        const drained = await drainInFlight(timeoutMs)
        if (!drained) {
          if (wasActive) acceptMessages = true
          return {
            status: 408 as const,
            body: { ok: false, error: 'drain timeout', in_flight: _inFlight, timeout: timeoutSec },
          }
        }
        const elapsed = Date.now() - drainStart
        const remainingMs = Math.max(0, timeoutMs - elapsed)
        const postWait = Math.max(0, Math.min(remainingMs, lastInjectAt + DRAIN_MARGIN_MS - Date.now()))
        if (postWait > 0) await new Promise((r) => setTimeout(r, postWait))
        releaseLeaseLock()
        return {
          status: 200 as const,
          body: { ok: true, active: false, drained_for_ms: postWait },
        }
      })
      return Response.json(result.body, { status: result.status })
    }

    // GET /session/current
    if (req.method === 'GET' && url.pathname === '/session/current') {
      const sid = await getCurrentSessionId()
      return Response.json({ session_id: sid, active: acceptMessages })
    }

    // GET / — health
    if (req.method === 'GET' && url.pathname === '/') {
      return Response.json({
        service: 'ariake-channel',
        port: PORT,
        tmux: TMUX_TARGET,
        listeners: listeners.size,
        active: acceptMessages,
        in_flight: _inFlight,
        lease_lock_path: LEASE_LOCK_PATH,
        lease_lock_held: _leaseLockFd !== null,
      })
    }

    return new Response('not found', { status: 404 })
  },
})

process.stderr.write(
  `ariake-channel: up on http://127.0.0.1:${PORT}  (POST /message -> tmux ${TMUX_TARGET} · GET /events)\n`,
)
