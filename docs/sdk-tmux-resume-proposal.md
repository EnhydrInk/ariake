# Design proposal: sdk → tmux same-session resume

## Status

**IMPLEMENTED.** The implementation lives in:

- `channel/server.ts` `/lease/acquire`'s `restart_pending` branch (fire-and-forget `systemctl restart`)
- `backend/transport.py` `_poll_until_ready` and `_rollback_release_then_reacquire`
- `templates/cc.sh.patch.sh` (the cc startup script snippet)
- `templates/sudoers.example` (passwordless `systemctl restart` rule)

This document is the design proposal that produced the implementation. The five rounds of independent review that shaped it live in [review-trail.md](./review-trail.md).

## Background

Ariake has two channel transports — tmux (`channel/server.ts`) and SDK (`sdk-channel/server.py`) — that drive the same Claude Code session and can be switched between at runtime via `backend/transport.py`. The lease protocol ensures only one transport writes the jsonl at a time.

**tmux → sdk** was easy from day one: the SDK transport accepts a `resume=<sid>` parameter and calls `claude-agent-sdk`'s `connect(resume=sid)`. CC continues writing the same jsonl. Session continuity preserved.

**sdk → tmux** is harder. The tmux side's CC is an independent process started by an external startup script. There is no API to "switch which jsonl it writes to" — the jsonl is decided at `claude` boot from `--resume <sid>` or `--continue`. So the tmux side's CC must be **restarted** with the SDK's `session_id` for the conversation to survive the switch.

The naive plan was: in tmux's `/lease/acquire`, write the sid to a file, call `systemctl restart`, wait for ready, return success. The naive plan has six things wrong with it. See [review-trail.md](./review-trail.md) for the full path; the summary follows.

## The design

### Channel side — `/lease/acquire` with `resume` set

1. **Validate** the sid format (UUID-ish regex). Malformed sid → 400 with no side effects.
2. **Acquire the file lock** with `flock LOCK_EX | LOCK_NB`. Denied → 409, CC has not been touched, the coordinator rolls back cleanly. (This is the lock-first invariant from review round 1.)
3. **Save** `wasAccepting = acceptMessages`, set `acceptMessages = false`. New inbound messages get 503 instead of hitting a kill/loading CC.
4. **Write** the sid to `${INKIERAN_RESUME_SID_FILE}` (default `/tmp/ariake-cc-resume-sid`).
5. **Spawn** `sudo -n /bin/systemctl restart --no-block <cc service>`. `--no-block` tells systemctl not to wait for the restart job to complete; it returns ~500ms after dispatching the D-Bus request.
6. **Await** the spawn's exit with a 3-second timeout. Exit 0 → systemd has accepted the request, proceed. Non-zero or timeout → sudoers/systemctl misconfig. Clean up (`rm` sid file, release lock, restore `wasAccepting`), return 500. The coordinator rolls back.
7. **Immediately return** `200 {restart_pending: true, poll_url: "/session/current", poll_match: {session_id, active: true}}`. The HTTP response flushes to socket in the ~100-500ms window between `systemctl --no-block` returning and tmux killing the channel process. (This is the fire-and-forget pattern from review round 3 — see [review-trail.md](./review-trail.md) for why "await the actual restart" is impossible.)

### cc startup script side — `templates/cc.sh.patch.sh`

1. Reads `${INKIERAN_RESUME_SID_FILE}`. If present and the content is a valid sid: `RESUME_ARGS=("--resume" "$sid")`, `export INKIERAN_EXPECTED_SESSION_ID="$sid"`, `rm -f` the file.
2. If absent or invalid: falls back to `--continue` if the project's jsonl pool is non-empty, otherwise bare start.
3. `exec claude "${CLAUDE_BASE_ARGS[@]}" "${RESUME_ARGS[@]}"` — single line keeps claude as systemd's direct child so the next kill propagates cleanly.

The `INKIERAN_EXPECTED_SESSION_ID` env hint is what makes the coordinator's poll deterministic on the new channel process.

### Coordinator side — `_poll_until_ready` in `backend/transport.py`

After receiving `restart_pending: true` from the new channel's `/lease/acquire`:

1. **Poll** `GET new_url/session/current` every 1.5 seconds, up to 40 seconds total. (40s covers cold-cache `claude --resume` loads, which can take 25s+ in the wild.)
2. Connection refused → channel still restarting, keep polling.
3. Response received → check `session_id == sid && active == true`. Match → proceed to coordinator step 6 (commit). Mismatch or timeout → rollback.

### Rollback on poll timeout or any acquire failure

`_rollback_release_then_reacquire`:

1. **Best-effort** `POST new_url/lease/release`. The new transport may still be alive holding the lock; if it's already dead the OS released the fd. Do **not** skip this step. Reacquiring old without releasing new can deadlock on the cross-process lock (the new channel process can be alive-and-unreachable, lock held by an unkilled-yet zombie — see review round 4).
2. `POST old_url/lease/acquire` to put old back into active state.
3. Report `rollback_ok`, `reacquire_old_status`, `reacquire_old_error` in the response body so the caller (UI / operator) knows whether rollback itself succeeded.

## The four-file change

| file | what changes |
|------|--------------|
| `channel/server.ts` | `/lease/acquire` resume branch: validate + lock-first + write sid + spawn restart + return `restart_pending` |
| `backend/transport.py` | `_poll_until_ready` + `_rollback_release_then_reacquire` + the `restart_pending` and exception branches in `transport_switch` |
| cc startup script | resume sid file handling + `INKIERAN_EXPECTED_SESSION_ID` env export (template: `templates/cc.sh.patch.sh`) |
| sudoers | `<user> ALL=(root) NOPASSWD: /bin/systemctl restart <cc service>` (template: `templates/sudoers.example`) |

## Trade-offs (in the current shape)

- **8-15 second switch latency** when sdk → tmux is invoked. Most of this is `claude --resume` cold-cache load. Inherent to "must restart CC"; there is no fast path that preserves both speed and cross-session memory given how `claude` initializes.
- **Sudoers risk surface**: the channel process can restart the cc service. If the channel process is compromised, the attacker can restart cc. Mitigated by limiting `NOPASSWD` to one exact unit name; a typical attack path through the channel process is HTTP-level (a malformed request) which is hard to weaponize into shell.
- **`/tmp` sid file race window** (~1ms between writeFile and systemctl restart). See [optional-med-3-sid-race.md](./optional-med-3-sid-race.md) for a hardened path that was considered and not shipped.
- **Modal-eat on the post-restart window**: if a CC modal happens to be up when the restart finishes, the first inbound message after switch can be silently lost. [optional-med-2-poll-ready.md](./optional-med-2-poll-ready.md) discusses a stronger readiness signal.

## User-layer equivalent (alternative, not the default)

Instead of mechanism-layer continuity, the application running on top of Ariake can implement behavior-layer continuity: have the sdk-side CC periodically save important facts to an external memory store, and have the tmux-side CC read the same store on first turn.

Trade-offs vs the mechanism approach:

- **Pro**: zero switch latency. tmux comes up with `--continue` immediately, then queries memory as needed.
- **Pro**: more selective. Saved facts are summaries, not raw transcript noise.
- **Con**: relies on the application reliably saving things. If a key fact never gets saved (CC did not know it was important, or the moment passed), it is gone.
- **Con**: only useful when there is an external memory store the application controls.

The two can coexist — Ariake's mechanism layer handles general continuity; an application can layer high-signal long-term notes on top. Ariake itself stays at the mechanism layer; the user layer is an application concern.

## Verification

After implementation, end-to-end sanity:

1. **tmux → sdk** still works as before (no changes to that direction)
2. **sdk → tmux**: switch, then ask CC about something it said during the sdk window — it should remember
3. **Switch latency** in the 8-15 second range
4. **Rollback under fault**: kill the cc service mid-switch; the coordinator should report rollback detail in the response body, not silent state corruption

## Pointers

- [review-trail.md](./review-trail.md) — five rounds of independent review on this path
- [architecture.md](./architecture.md) — broader dual-transport context
- [lease-protocol.md](./lease-protocol.md) — the endpoint contract
