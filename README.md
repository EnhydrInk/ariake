# Ariake

> **有明** — the moment at daybreak when the remnant moon and the rising sun share the sky.
> Two channel transports running side-by-side; one Claude Code session leased between them.

Ariake is a dual-transport channel layer for [Claude Code](https://docs.claude.com/en/docs/claude-code). It lets a single Claude Code session be driven by either a **tmux** plugin (terminal `send-keys` injection) or a **SDK** runner (`claude-agent-sdk`), and switched between them at runtime — without splitting the conversation into two timelines.

The two transports share one cross-process file lock; only the holder writes to the underlying session jsonl. A coordinator pauses inbound traffic, releases the old lease, acquires the new lease with `resume=<sid>`, then unpauses. The `session_id` survives the switch.

## Why two transports

| transport | strengths | weaknesses |
|---|---|---|
| **tmux** | full CC feature surface (plugins, hooks, TTY UI) | `send-keys` is blind — modal screens silently eat input; `--dangerously-load-development-channels` has been crash-prone across CC versions |
| **SDK** | clean stdin/stdout JSON contract — no modal hazards, structured streaming | smaller feature surface (no plugins by default); subject to provider SDK pricing/policy windows |

Running both lets you flip when either side has trouble: if the provider's SDK billing model shifts, flip to tmux; if a CC version regresses on the development-channels path, flip to SDK.

## What's in the box

- **`backend/transport.py`** — switch coordinator. Seven-step atomic switch with `restart_pending` poll, release-before-reacquire rollback, exception-path poll guard.
- **`channel/server.ts`** — tmux transport. Bun + `@modelcontextprotocol/sdk`. `tmux send-keys` injection + `bun:ffi` `flock` + fire-and-forget `systemctl restart` for sdk → tmux session resume.
- **`sdk-channel/server.py`** — SDK transport. FastAPI + `claude-agent-sdk`. `AdmissionTicket` pattern + three-lock turn serialization + `fcntl.flock` cross-process lease.
- **`docs/`** — [architecture](./docs/architecture.md) / [lease-protocol](./docs/lease-protocol.md) / [sdk-tmux-resume-proposal](./docs/sdk-tmux-resume-proposal.md) / [review-trail](./docs/review-trail.md) (five rounds of independent review) / [envelope-extensions](./docs/envelope-extensions.md) / two unverified optional optimizations.
- **`templates/`** — `sudoers` / `systemd` units / `docker-compose.yml` / `cc.sh.patch.sh`.
- **`tests/test_lease_lock.ts`** — Python ↔ Bun cross-process `flock` interlock test.
- **`example/echo_app.py`** — minimal FastAPI host wiring the coordinator to an echo bot so `docker compose up` is green out of the box.

## What's NOT in the box

Ariake is the **transport layer**. The application using it — UI, message store, business logic, the actual CC system prompt and personality — is yours. Ariake gives you:

- a coordinator that safely hands the session off
- two channel implementations that respect the lease
- a contract spec so a third transport can be added without surprise

## Quick start

The repo runs locally without Docker. You need: Python 3.10+, Bun (for the tmux transport), and one of `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` in your env so `claude-agent-sdk` can authenticate.

```bash
# 1. SDK transport (terminal A)
cd sdk-channel
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ARIAKE_SDK_CWD=/tmp/ariake-cc-cwd  # any dir is fine for the demo
export ARIAKE_SDK_SYSTEM="You are an echo bot. Reply to every channel message by calling the reply tool with the same text the user sent."
python server.py

# 2. Echo host (terminal B)
cd example
pip install fastapi 'uvicorn[standard]' httpx
ARIAKE_INITIAL=sdk python echo_app.py

# 3. Try it (terminal C)
curl -X POST http://127.0.0.1:18080/api/message -H 'content-type: application/json' -d '{"text":"hello"}'
# Then watch the reply land on SSE:
curl -N http://127.0.0.1:18080/events
```

The tmux transport (`channel/server.ts`) needs a running tmux session with `claude` inside it plus host-level `systemctl` access for the fire-and-forget restart path, so it doesn't fit a one-line quick start. See [channel/README.md](./channel/README.md) for setup.

[templates/docker-compose.yml](./templates/docker-compose.yml) is a starting point for a production-style compose deploy of just the SDK transport + host — adapt as you need.

## Provenance

This is an extraction from a private project (InKieran) at HEAD `inkieran/9da8a6a`. The transport layer has been running in production with daily transport switches; the extraction strips application-specific glue (UI, business prompt, voice synthesis adapter, etc.) and keeps the protocol + reference implementations.

## Status

The two unverified optional optimizations in `docs/optional-*.md` are spec-level only — they are paths considered but not implemented. They are documented so a reader knows the road exists and does not have to start from zero if they want to take it.

The fire-and-forget sdk → tmux resume path required five rounds of independent review to reach a working design; see [docs/review-trail.md](./docs/review-trail.md) for the full trail of what was tried, what broke, and why the current shape is what it is.

## License

MIT. See [LICENSE](./LICENSE).
