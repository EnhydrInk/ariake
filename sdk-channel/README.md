# sdk-channel — SDK transport

FastAPI + `claude-agent-sdk`. Drives `claude` via stdin/stdout JSON-RPC; no tmux, no send-keys.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# One of these is required for claude-agent-sdk to authenticate:
#   ANTHROPIC_API_KEY=...
#   CLAUDE_CODE_OAUTH_TOKEN=...
# See docs/architecture.md for auth precedence.

ARIAKE_SDK_CWD=/opt/ariake/cc-cwd \
ARIAKE_SDK_SYSTEM="You are an echo bot. Reply to every channel message by calling the reply tool with the same text." \
  python server.py
```

## Environment

| var                                | default                            | what |
|------------------------------------|------------------------------------|------|
| `ARIAKE_SDK_PORT`                | `8911`                             | HTTP port |
| `ARIAKE_SDK_CWD`                 | `/opt/ariake/cc-cwd`               | `claude` cwd — MUST match channel's `ARIAKE_CWD` |
| `ARIAKE_BACKEND`                 | empty                              | sidecar target; empty -> skip |
| `ARIAKE_SOURCE`                  | `ariake`                           | value of `<channel source="...">` |
| `ARIAKE_UPLOADS`                 | `/var/lib/ariake/uploads`          | uploads sandbox |
| `ARIAKE_LEASE_LOCK_PATH`         | `${CWD}/.lease.lock`               | cross-process lock — MUST match channel |
| `ARIAKE_SDK_AUTOSTART`           | `1`                                | `0` = standby |
| `ARIAKE_SDK_DRAIN_TIMEOUT`       | `30`                               | seconds; release/acquire drain timeout |
| `ARIAKE_SDK_PERMISSION_MODE`     | `dontAsk`                          | claude-agent-sdk permission_mode; `bypassPermissions` opens Bash/Read/Write/Edit + external MCP |
| `ARIAKE_SDK_SYSTEM`              | empty                              | inline system prompt; empty = CC has NO instructions (see note below) |

## Empty system prompt = CC runs naked

The transport sets `setting_sources=[]` to keep CC from auto-loading settings (which would invoke hooks the sidecar duplicates). That also blocks the user-level `CLAUDE.md` loader. Net effect: `ARIAKE_SDK_SYSTEM` is the **only** way to give CC a personality on this transport. Leave it empty and CC runs with just the `reply` MCP tool, no instructions.

## permission_mode

| value               | semantics |
|---------------------|-----------|
| `dontAsk` (default) | tools not in `allowed_tools` are auto-denied; never prompts |
| `bypassPermissions` | all tools open; accept the prompt-injection risk from user input / external MCP returns |
| `default`           | prompts user — **don't use without a UI to answer**; will hang the `client_lock` |
| `acceptEdits`       | accept Edit / Write without prompt; other tools still need permission |
| `plan`              | plan mode (no execution) |
| `auto`              | provider-decided |

## External MCP

`sdk-channel` reads `mcpServers` from `~/.claude.json` (user-level) and `${CWD}/.mcp.json` (project-level) and merges them, project-level winning name conflicts. The `ariake` entry is reserved for the in-process MCP server providing the `reply` tool.

To use external MCP tools, you also need to widen `permission_mode` (above) — `dontAsk` denies everything except `mcp__ariake__reply`.

## Sidecar (optional)

Set `ARIAKE_BACKEND=http://your-host:port` and every completed turn POSTs to `${BACKEND}/api/sidecar`:

```json
{
  "chat_id": "...",
  "thinking": "concatenated ThinkingBlock content from this turn",
  "asides": ["each TextBlock from this turn"],
  "session_id": "..."
}
```

Empty `ARIAKE_BACKEND` -> no sidecar calls. The host can run without one.

## Endpoints

See [docs/lease-protocol.md](../docs/lease-protocol.md). Same contract as the tmux transport — that's the point.
