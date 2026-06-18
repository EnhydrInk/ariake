# Architecture

## The problem

A Claude Code session = a single `~/.claude/projects/<slug>/<sid>.jsonl` file with exactly one process appending to it at a time. If you want **transport flexibility** (drive CC through tmux send-keys at some times, through the SDK at others) **and** session continuity (same `session_id`, same memory, same prompt history across switches), you need two things:

1. **A single-writer invariant** for the jsonl. Two transports writing the same file = interleaved lines, corrupt session.
2. **A coordinated handoff** that pauses inbound traffic, releases the old writer, hands the `session_id` to the new writer (so it can `--resume <sid>`), and unpauses.

Ariake gives you a lease protocol for #1 and a seven-step backend coordinator for #2.

## The signal envelope

Both transports speak the same wire format. Inbound user messages reach CC as a `<channel>` tag injected into the conversation stream:

```
<channel source="..." chat_id="..." message_id="..." user="..." ts="2026-06-18T13:27:31.435Z">message body here</channel>
```

CC sees the tag, recognises it as an inbound channel message (rather than transcript output), and replies through a registered MCP `reply` tool whose schema includes `chat_id` (for routing) and an optional `reply_to` (the inbound `message_id`, for threading).

The envelope is **byte-identical across transports**. tmux serialises the tag and sends it with `send-keys -l`; the SDK transport calls `client.query(envelope)` over stdin. Same string either way, so switching transports does not change what CC sees on the wire.

Hosts may add their own attributes (e.g. `inner="..."` for emotional state, custom `voice` markers on outbound events). The standard envelope is documented here; extensions are listed in [envelope-extensions.md](./envelope-extensions.md).

## Two transports

### tmux (`channel/server.ts`, Bun)

- **Inbound** — `HTTP POST /message` → `tmux send-keys -t <session>:0 -l <envelope>` + `Enter`. Blind injection. No turn-end signal; the only "did CC consume it" feedback is whether the next `reply` shows up on the SSE channel.
- **Outbound** — CC calls the `reply` MCP tool (the channel process is a stdio MCP server registered with CC). `reply` calls broadcast to a SSE ring buffer; clients subscribe via `GET /events` with `Last-Event-ID` replay support.
- **Why this design** — `--dangerously-load-development-channels` (the experimental path that lets a plugin push channel notifications into CC directly) has had stability regressions across CC versions. `send-keys` works on any CC build that recognises the `<channel>` tag (the official Discord/Telegram plugins depend on the same tag format), at the cost of being blind to modal UI state.

### SDK (`sdk-channel/server.py`, FastAPI + `claude-agent-sdk`)

- **Inbound** — `HTTP POST /message` → build envelope → `client.query(envelope)` over the SDK's stdin/stdout JSON-RPC. No `send-keys`, no modal hazards.
- **Outbound** — an **in-process MCP server** (built with `create_sdk_mcp_server`) registers the same `reply` tool. CC calls it, the tool function runs in the same Python process, the payload is broadcast to a SSE ring buffer.
- **Sidecar** — the SDK transport can scrape `ThinkingBlock` / `TextBlock` content out of `client.receive_response()` and POST it to the host as a "sidecar" (CoT digest, asides). The reference implementation does this on each turn end; you can disable it by removing the `_post_sidecar` call.
- **Why this design** — `claude-agent-sdk` exposes a structured JSON contract that bypasses the terminal entirely. Trade-off: smaller feature surface (no plugins by default — they are gated by `plugins=[]` + `setting_sources=[]` + `strict_mcp_config=True`) and subject to provider SDK pricing windows.

### Contract (both transports must implement)

| method  | path                  | role |
|---------|-----------------------|------|
| `POST`  | `/message`            | inbound user message |
| `POST`  | `/tick`               | inbound non-message envelope (timers, heartbeats, etc.) |
| `GET`   | `/events`             | outbound SSE stream of `reply` events |
| `POST`  | `/lease/acquire`      | claim writer lease (see [lease-protocol.md](./lease-protocol.md)) |
| `POST`  | `/lease/release`      | release writer lease |
| `GET`   | `/session/current`    | report current `session_id` + `active` flag |
| `GET`   | `/`                   | health check |

## The lease

A single cross-process file (`${CWD}/.lease.lock` by default) is `flock`-locked with `LOCK_EX | LOCK_NB` by whichever transport currently owns the jsonl writer role. Both implementations use the same syscall family:

- tmux side: `bun:ffi` loads `libc.so.6` (or platform equivalent) and calls `flock(2)` directly.
- SDK side: Python `fcntl.flock`.

Because both go through the kernel's advisory lock table on the same inode, they interlock cleanly — the test in `tests/test_lease_lock.ts` exercises this.

**Standby vs active.** A transport that did not acquire the lock at startup (because some other transport already held it) goes into standby: `acceptMessages = false`, inbound returns 503, the SSE stream still runs, and `GET /session/current` reports `active: false`. It will become active only when the backend calls `POST /lease/acquire` explicitly.

## The switch — seven steps

`backend/transport.py` exposes `POST /api/transport/switch {target, timeout}`. The implementation must execute **strictly** in this order. Reordering steps 3-5 has been shown to cause "half-switch" states (lock held by no one, lock held by both, jsonl corruption).

1. **Acquire transport_lock** — only one switch in flight at a time.
2. **`transport_paused = True`** — host must check this on every inbound path and return 503 (`/api/message`, group chat dispatch, any timer that injects into CC).
3. **`GET old/session/current`** — read the current `session_id` so the new transport can resume the same jsonl. If `old` does not implement this endpoint, `sid = None` and resume is skipped.
4. **`POST old/lease/release {timeout}`** — drain the old transport. Non-2xx other than 404 (= transport does not implement lease, passive ready) aborts the switch.
5. **`POST new/lease/acquire {resume: sid, timeout}`** — acquire the new transport.
   - **200 + `restart_pending: true`** path: the new transport spawned a process restart and is about to die before HTTP response writes; coordinator must poll `GET new/session/current` until `session_id == sid && active == true` or `poll_timeout` (typically 40s — `claude` binary boot can take ~25s on cold cache).
   - **404** path: the new transport does not implement lease (passive ready); treat as success.
   - **Any other non-2xx** path: rollback. Rollback **must** `release new` first, then `acquire old` (see [review-trail.md](./review-trail.md) round 4, HIGH-1).
   - **Exception** path: HTTP-level exception (e.g. `RemoteProtocolError` from a write race when the channel self-killed mid-response) may still mean the restart was dispatched. If `sid` is known and `target == tmux`, fall through into the poll guard before declaring failure.
6. **Commit**: set `CHANNEL_BASE = new_url` atomically, reconfigure dependent state (heartbeat producer, SSE subscriber task — cancel and restart so it reconnects to the new URL).
7. **`transport_paused = False`** — release inbound.

The coordinator does not own application state; it takes callbacks (`reconfigure_callback`, `sse_restart_callback`) so the host can wire its own subsystems into step 6.

## sdk → tmux resume (fire-and-forget restart)

The tmux transport's CC process is independent — it cannot just "switch which jsonl it appends to". To pick up the SDK's `session_id`, the tmux side must restart its CC with `claude --resume <sid>`. The implementation:

1. **`POST tmux/lease/acquire {resume: sid}`** — validates `sid` format, acquires the local file lock first (so even if `restart` fails the backend can rollback cleanly), writes `sid` to `${RESUME_SID_FILE}`.
2. **Spawn** `sudo -n /bin/systemctl restart --no-block inkieran-cc.service` and `await proc.exited` with a 3-second timeout. Exit 0 means systemd accepted the D-Bus request.
3. **Immediately return** `200 {ok: true, restart_pending: true, poll_url: "/session/current", poll_match: {session_id, active: true}}`. The HTTP response flushes to socket before systemd kills the channel process (~100-500ms window).
4. **The CC startup script** (`templates/cc.sh.patch.sh`) reads `${RESUME_SID_FILE}` on boot. If present + valid: `claude ... --resume "$sid"` + `rm -f $RESUME_SID_FILE`. Otherwise fallback to `--continue`.
5. **The new channel process** comes up with `AUTOSTART=1`, acquires the file lock, sets `acceptMessages = true`, exports `INKIERAN_EXPECTED_SESSION_ID="$sid"` to itself as an env hint so `GET /session/current` can return it deterministically (without scanning jsonl `mtime`, which races against `claude --resume`'s lazy first write).
6. **The backend** polls `new/session/current` until `session_id == sid && active == true` or timeout.

A non-trivial design — see [sdk-tmux-resume-proposal.md](./sdk-tmux-resume-proposal.md) for why each piece is shaped this way and [review-trail.md](./review-trail.md) for the four-round path to the current implementation.

## Why not simpler

| alternative | what breaks |
|---|---|
| "Just have the backend always proxy through one server" | The two transports use different driver APIs (tmux send-keys vs SDK stdio JSON-RPC) — a single proxy still needs both implementations underneath. |
| "Synchronise via an external lock service (Redis, etcd, ZK)" | Adds an operational dependency for a problem the OS already solves with `flock(2)`. |
| "Skip the lease, just kill the old transport before starting the new one" | Inbound messages between kill and acquire are dropped or hit a dead socket; no way to drain in-flight turns cleanly. |
| "Use one CC instance, switch only the *input pipe*" | CC writes to jsonl based on its own `--session-id` at process boot. Switching input pipes does not change which jsonl is being written. You need a real session resume. |

## File layout (reference)

```
ariake/
├── backend/transport.py        # the seven-step coordinator
├── channel/server.ts           # tmux transport (Bun)
├── sdk-channel/server.py       # SDK transport (Python + FastAPI + claude-agent-sdk)
├── docs/
│   ├── architecture.md         # this file
│   ├── lease-protocol.md       # endpoint contract spec
│   ├── envelope-extensions.md  # how to add inner / voice / custom attributes
│   ├── sdk-tmux-resume-proposal.md
│   ├── review-trail.md         # five rounds of review on the resume design
│   ├── optional-med-3-sid-race.md
│   └── optional-med-2-poll-ready.md
├── templates/
│   ├── cc.sh.patch.sh
│   ├── sudoers.example
│   ├── cc.service.example
│   ├── sdk-channel.service.example
│   └── docker-compose.yml
├── tests/test_lease_lock.ts
└── example/echo_app.py
```
