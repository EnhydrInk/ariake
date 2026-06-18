# channel — tmux transport

Bun + `@modelcontextprotocol/sdk`. Bridges HTTP into a tmux-resident Claude Code session via `send-keys`, and CC's replies back out over SSE.

## Run

```bash
bun install
# Make sure a tmux session named "inkieran" (or whatever ARIAKE_TMUX is set to)
# is running with `claude` inside it.
ARIAKE_TMUX=inkieran \
ARIAKE_CWD=/opt/ariake/cc-cwd \
ARIAKE_CC_SERVICE=ariake-cc.service \
  bun server.ts
```

The process is itself a stdio MCP server (registered with CC via `claude --mcp-config` or a plugin manifest), plus an HTTP server on `ARIAKE_PORT` (default 8910).

## Environment

| var                              | default                              | what |
|----------------------------------|--------------------------------------|------|
| `ARIAKE_PORT`                  | `8910`                               | HTTP port |
| `ARIAKE_TMUX`                  | `inkieran`                           | tmux session name (target = `<name>:0`) |
| `ARIAKE_CWD`                   | `/opt/ariake/cc-cwd`                 | `claude` cwd — MUST match sdk-channel's `ARIAKE_SDK_CWD` |
| `ARIAKE_UPLOADS`               | `/var/lib/ariake/uploads`            | uploads sandbox for attachments |
| `ARIAKE_SOURCE`                | `ariake`                             | value of the `<channel source="...">` attribute |
| `ARIAKE_LEASE_LOCK_PATH`       | `${CWD}/.lease.lock`                 | cross-process lock file — MUST match sdk-channel |
| `ARIAKE_AUTOSTART`             | `1`                                  | `0` = start in standby (wait for `/lease/acquire`) |
| `ARIAKE_DRAIN_TIMEOUT`         | `30`                                 | seconds; default release/acquire drain timeout |
| `ARIAKE_DRAIN_MARGIN_MS`       | `8000`                               | ms; post-margin heuristic in /lease/release |
| `ARIAKE_RESUME_SID_FILE`       | `/tmp/ariake-cc-resume-sid`          | where this process writes the sid for the cc startup script |
| `ARIAKE_CC_SERVICE`            | `ariake-cc.service`                | systemd unit `sudo systemctl restart` targets |
| `ARIAKE_EXPECTED_SESSION_ID`   | (set by cc startup script)           | overrides jsonl-mtime scan in `/session/current` |

## Endpoints

See [docs/lease-protocol.md](../docs/lease-protocol.md) for the contract. Briefly:

- `POST /message` — inbound user message
- `POST /tick` — verbatim envelope injection (host pre-builds tag)
- `GET /events` — SSE outbound, with `Last-Event-ID` replay
- `POST /lease/acquire` — claim writer lease (with optional `resume` sid for sdk→tmux restart)
- `POST /lease/release` — drain + release writer lease
- `GET /session/current` — current jsonl basename (or `ARIAKE_EXPECTED_SESSION_ID` hint)
- `GET /` — health

## fire-and-forget restart path

When `POST /lease/acquire` is called with a `resume` sid, this process:

1. acquires the file lock (denied -> 409, CC untouched)
2. writes the sid to `ARIAKE_RESUME_SID_FILE`
3. spawns `sudo -n /bin/systemctl restart --no-block $ARIAKE_CC_SERVICE`
4. awaits the spawn's exit (3s timeout; non-zero -> 500, sudoers misconfig)
5. immediately returns `200 {restart_pending: true}`; systemd kills this process shortly after
6. the cc startup script (see [templates/cc.sh.patch.sh](../templates/cc.sh.patch.sh)) reads the sid file and `claude --resume <sid>`
7. the new channel process starts in active mode, with `ARIAKE_EXPECTED_SESSION_ID` exported so `/session/current` answers deterministically

The coordinator (`backend/transport.py`) polls `/session/current` until `session_id == sid && active == true`. See [docs/sdk-tmux-resume-proposal.md](../docs/sdk-tmux-resume-proposal.md) and [docs/review-trail.md](../docs/review-trail.md).

## Sudoers

The fire-and-forget restart needs passwordless `systemctl restart`. See [templates/sudoers.example](../templates/sudoers.example).
