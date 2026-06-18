# Lease Protocol

A transport implementation that wants to participate in switching must expose four HTTP endpoints and respect the cross-process file lock. This document is the wire-level spec — implementations in this repo (`channel/server.ts`, `sdk-channel/server.py`) are reference; a third transport can be added by re-implementing the same contract.

## State

Each transport tracks two pieces of state:

- `acceptMessages: bool` — gates `/message` and `/tick` inbound. When `false`, both return `503 {"error": "lease released; transport idle"}`.
- `_lease_lock_fd: int | null` — the open file descriptor holding `flock(LOCK_EX | LOCK_NB)` on `${CWD}/.lease.lock` (path configurable via env). `null` means standby.

The lock path defaults to `${CWD}/.lease.lock`. **Both transports must point at the same path** — that is how they interlock. The conventional layout is one shared cwd (e.g. `/opt/ariake/cc-cwd`) for both processes, so they also share the same jsonl pool.

## Endpoints

### `POST /lease/acquire`

Claim the writer lease. Optionally `resume` an existing session.

**Request body** (JSON, all optional):
```json
{
  "resume": "<session_id>",
  "timeout": 30
}
```

- `resume` — if set, the transport must drive CC to continue this `session_id`. Format must match `/^[0-9a-f-]{8,64}$/i` (reject 400 otherwise — protects against shell injection on transports that feed the value into a shell-launched binary).
- `timeout` — seconds. Used to bound `drain_admitted` (when this transport was already active and needs to drain in-flight turns before swapping client), and (resume path only) as a budget check.

**Behaviour**:

1. If already active (re-acquire), drain in-flight admitted requests under the timeout. Drain timeout → rollback `is_active = True`, return `408`.
2. Acquire the file lock (`flock LOCK_EX | LOCK_NB`).
   - Already held by this process: idempotent no-op (`already_held`).
   - Held by another process: return `409 {"error": "lease lock held by another process", "lock_path": "..."}`.
3. If `resume` is set, validate format. Then drive whatever mechanism this transport uses to make CC resume that session id.
4. Connect or activate the CC client.
5. Set `acceptMessages = true`, return `200`.

**Responses**:

| status | body | meaning |
|---|---|---|
| `200` | `{"ok": true, "active": true, "lock": "acquired"\|"already_held", "resumed": "<sid>"\|null}` | success |
| `200` | `{"ok": true, "active": true, "lock": "acquired", "resumed": "<sid>", "restart_pending": true, "poll_url": "/session/current", "poll_match": {"session_id": "<sid>", "active": true}}` | success in fire-and-forget mode — see below |
| `400` | `{"ok": false, "error": "invalid resume sid format" \| "body.timeout=Xs too small for resume path; min 20s required"}` | client error |
| `408` | `{"ok": false, "error": "drain timeout during acquire", "in_flight": N}` | drain exceeded `timeout` |
| `409` | `{"ok": false, "error": "lease lock held by another process", "lock_path": "..."}` | another transport holds the lock |
| `500` | `{"ok": false, "error": "connect failed: ..."}` | downstream (CC binary / SDK client) failed |

**Fire-and-forget mode** (used by the tmux transport when it must restart its CC process to pick up `--resume <sid>`): the endpoint writes the sid to a known location, dispatches `systemctl restart --no-block`, awaits the spawn's quick exit (3-second timeout to detect sudoers misconfiguration), and returns `200` with `restart_pending: true` **before** the channel process is killed by systemd. The HTTP response flushes to socket in the ~100-500ms window between spawn-exit and tmux kill. The coordinator then polls `GET /session/current` on the same URL until ready (typically 8-15 s for `claude --resume <sid>`).

### `POST /lease/release`

Release the writer lease. Drains in-flight before releasing the file lock.

**Request body**:
```json
{ "timeout": 30 }
```

**Behaviour**:

1. Set `acceptMessages = false` (immediately rejects new `/message` and `/tick` with 503).
2. Drain in-flight admitted requests under `timeout`. If drain times out → rollback `acceptMessages = was_active`, return `408`.
3. Disconnect / quiesce the CC client.
4. Release the file lock (`flock LOCK_UN` + close fd). **Only after** drain + disconnect succeed — releasing while still writing would let another transport acquire and trigger jsonl corruption.

**Responses**:

| status | body | meaning |
|---|---|---|
| `200` | `{"ok": true, "active": false, "drained_for_ms": N}` (tmux) or `{"ok": true, "active": false, "in_flight": 0}` (SDK) | success |
| `408` | `{"ok": false, "error": "drain timeout", "in_flight": N, "timeout": Xs}` | drain exceeded; lock still held |

### `GET /session/current`

Report the current `session_id` (the jsonl basename, no `.jsonl` suffix) and the active state.

**Response**:
```json
{ "session_id": "abc12345-...", "active": true }
```

`session_id` may be `null` if the transport has just started and not yet observed CC writing a session. Returning `null` is valid; the coordinator skips the resume hand-off if no sid is available from the old side.

**Source of truth.** The reference SDK implementation reads `current_session_id` from a module-level variable updated by `SystemMessage(subtype="init")` / `ResultMessage.session_id` events from `client.receive_response()`. The reference tmux implementation looks at `ARIAKE_EXPECTED_SESSION_ID` (an env hint set by the cc startup script when launched with `--resume <sid>`) first, then falls back to scanning `~/.claude/projects/*/*.jsonl` by `cwd` field + most recent `mtime`. The env hint exists because `claude --resume <sid>` does not write to its jsonl until the first turn — a fresh mtime can race against the poll.

### `GET /`

Health check. Returning shape is implementation-defined but should at minimum include:

```json
{
  "service": "...",
  "port": N,
  "active": true,
  "in_flight": N,
  "lease_lock_path": "...",
  "lease_lock_held": true
}
```

## Cross-process file lock

`flock(2)` is **advisory** — only cooperating processes are constrained. Both reference transports honour the same lock file via:

- Python: `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` (raises `BlockingIOError` if held)
- Bun (TypeScript): `dlopen('libc.so.6', { flock: ... }).flock(fd, LOCK_EX | LOCK_NB)` (returns -1 if held)

Both go through the kernel advisory-lock table on the same inode. Cross-implementation interlock is verified by `tests/test_lease_lock.ts`.

**Lock file lifecycle**:
- The file is created on first acquire (`O_RDWR | O_CREAT`, mode `0o644`). It is **not truncated** — the file content is not used for anything, the lock is on the inode.
- The fd must stay open for the duration of the lease. Closing the fd releases the lock automatically (OS guarantee), so process death is safe — there is no stale lock to clean up.
- `LOCK_UN` is called explicitly on `release` for symmetry but is not strictly required.

## Admission tickets (turn safety)

Both reference transports use an **AdmissionTicket** pattern to make `release` drain correct under concurrent `/message` requests:

```
endpoint /message:
    ticket = admit_sync()             # sync: acceptMessages check + in_flight++ atomically
    if ticket is None: return 503
    spawn_with_admission(run_turn, ticket)
    return 202

run_turn (background):
    try: ... process turn ...
    finally: ticket.release_sync()    # idempotent
```

The naïve form — `if not acceptMessages: return 503; await req.json(); spawn(run_turn)` — has a TOCTOU window between the check and the spawn during which `release` can flip `acceptMessages`, see `in_flight == 0`, and release the file lock while a turn is still being scheduled. The sync admission closes this window: `acceptMessages` check + `in_flight++` happen atomically (JS single-threaded; Python under `admission_lock`), so by the time `acceptMessages` flips, the ticket already exists and `release`'s drain must wait for it.

The ticket is wired with `add_done_callback` (Python) / try/finally (Bun) so even cancel-before-body cancellation still releases. The release function is idempotent (a `released: bool` guard) so dual release paths don't double-decrement.

## State machine

```
                   acquire (lock denied → 409, stay)
                      │
        ┌─────────────┴─────────────┐
        │                           │
        ▼                           │
    [standby]      acquire OK       │
   acceptMessages=false  ─────────► [active]
   in_flight=0                       acceptMessages=true
        ▲                            in_flight≥0
        │                            │
        │  release (drain OK,        │
        │   disconnect, unlock)      │
        └────────────────────────────┘

    release timeout → rollback to [active], return 408
    acquire timeout (re-acquire path) → rollback to [active], return 408
```

## Error code summary

| code | name                       | semantic | caller action |
|------|----------------------------|----------|---------------|
| 200  | OK                         | normal success                                       | proceed |
| 200  | restart_pending            | acquire dispatched a process restart                 | poll `/session/current` until match or timeout |
| 202  | accepted                   | inbound message admitted                             | client waits for SSE `reply` event |
| 400  | invalid                    | malformed resume sid / timeout too small             | fix request |
| 404  | not implemented            | this transport does not implement the lease         | treat as success (passive transport) |
| 408  | drain timeout              | could not drain in-flight within `timeout` seconds   | coordinator retries with larger timeout, or rolls back the switch |
| 409  | lock held                  | another process holds the file lock                  | coordinator releases the holder first, or aborts |
| 500  | downstream connect failed  | CC binary / SDK client could not be brought up       | check logs (`journalctl -u <unit>`) |
| 502  | upstream error             | (coordinator-level) acquire/release returned non-2xx | rollback per the coordinator algorithm |
| 503  | idle                       | `acceptMessages=false` — transport is in standby     | caller waits or switches transports |
| 504  | switch timeout             | (coordinator-level) `restart_pending` poll timed out | inspect `rollback_ok` field; manual recovery may be needed |
