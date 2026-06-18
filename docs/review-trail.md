# Review Trail — five rounds on the sdk → tmux resume path

The fire-and-forget sdk → tmux resume path (the `restart_pending` branch in `channel/server.ts`'s `/lease/acquire` + the `_poll_until_ready` and `_rollback_release_then_reacquire` helpers in `backend/transport.py`) required five rounds of independent adversarial review to reach a working design.

This document is the trail of what was tried, what broke, and why the current shape is what it is. Each round was a code review by an independent reviewer who saw the prior round's design but not the discussion behind it.

The intent of preserving this trail is **not** to celebrate the design. It is to make a record of where the dangerous corners are. If you change this path, read this trail first; the easy-looking edits have already been tried.

## Background

The two transports can switch via `backend/transport.py`'s `POST /api/transport/switch`. From tmux → sdk this is straightforward: the sdk side accepts a `resume` sid, connects `claude-agent-sdk` with `resume=sid`, and continues writing the same jsonl.

From sdk → tmux is harder. The tmux side's CC is an independent process started by a separate startup script (`templates/cc.sh.patch.sh`). It cannot simply "switch which jsonl it writes to" — the jsonl is decided at `claude` boot from `--resume <sid>` or `--continue`. To pick up the sdk's `session_id`, the tmux side must **restart its CC** with the new sid.

The naive plan was: in `tmux/lease/acquire`, write the sid to a file, call `systemctl restart`, wait for the new process to come up, then return success. Five rounds of review later, that sentence had become a four-file change with non-obvious invariants spanning three different processes.

## Round 1 — order of operations

### HIGH-1: lock-first, then restart

**Initial proposal**:

1. write sid to file
2. `sudo systemctl restart <cc service>`
3. wait for CC to be ready
4. acquire the cross-process flock
5. set `acceptMessages = true`

**Objection**: if step 4 fails (lock held by another process), CC has already restarted with the new sid but the channel hasn't switched. The coordinator will roll back, the old transport will reacquire — but its CC process is now running the wrong sid. **Half switch**: backend state says "old transport active", actual CC state says "new transport's session".

**Fix**: invert the order. Try to acquire the lock FIRST. If denied, return 409 immediately — CC has not been touched, the coordinator can rollback cleanly. Only restart if the lock was acquired.

This is the layout in the current code. The lock-first comment in `channel/server.ts` near `/lease/acquire` traces back to here.

### HIGH-2: non-2xx from release must abort the switch

**Initial proposal**: in the coordinator, "if old transport's `/lease/release` returns non-2xx, swallow and proceed — the tmux side probably doesn't implement lease anyway".

**Objection**: the comment "tmux doesn't implement this, swallow" is stale — the tmux side implements lease now. A non-200 from release is a real failure. If we proceed to acquire on the new transport, **both transports end up thinking they own the writer**: old still holds the file lock, new acquires it via re-flock (which will actually fail too with EWOULDBLOCK, but the proceed-on-non-2xx logic has already moved past the error).

**Fix**: only 404 ("explicitly not implemented") proceeds. Any other non-200 aborts the switch with 502. Connection-level exceptions also abort (the OLD transport might be alive and still holding the lock — better to error out and let the operator look than to half-switch).

## Round 2 — budget derivation, cleanup paths, stderr

### HIGH-1: internal budget must be strictly less than coordinator HTTP timeout

Round 1's fix used the body's `timeout` value directly for each internal step (systemctl + sleep + poll).

**Objection**: if any internal sub-step runs to budget, the channel's total wall-clock exceeds the coordinator's HTTP timeout. The coordinator sees a `RemoteProtocolError` and executes its rollback path — but the channel has by then set `acceptMessages = true` and is actually running. **Half switch via a different door**: a timeout disagreement causes the coordinator and channel to disagree about who succeeded.

**Fix (round 2)**: derive an inner budget that's strictly less than the body timeout. `innerBudget = body.timeout - 5s safety margin`; systemctl ≤ 60% of inner; sleep ≤ remaining. The 5s margin survives typical timer drift.

(This was overtaken by round 3's fire-and-forget design, which removed internal awaits entirely. The principle — channel never spends longer than coordinator expects — still applies in modern form: see the 3-second `spawn.exited` timeout in `channel/server.ts`.)

### MED-1: already-held + resume needs wasAccepting cleanup

`tryAcquireLeaseLock` returns three states: `acquired`, `already_held`, `denied`. The resume path turns off `acceptMessages` before restart so messages don't hit a kill/loading CC. If any subsequent step (write sid file, spawn) fails, we need to restore the prior state precisely:

- `acquired`: release the lock we just took
- `already_held && wasAccepting`: lock was already ours AND we were active — restore `acceptMessages = true`
- `already_held && !wasAccepting`: lock was already ours but we were standby — leave alone

**Fix**: save `wasAccepting` at the start of the resume branch; `releaseOnFail` dispatches on `lockState + wasAccepting`. The current code in `channel/server.ts` `/lease/acquire` follows this pattern verbatim.

### MED-2: stderr stream can hold the state lock

The initial fix awaited `proc.stderr` text after killing the subprocess for error logging.

**Objection**: the stderr stream may wait for the inherited pipe to close, which doesn't happen until the kill propagates. Net effect: `withStateLock` holds for the duration of an irrelevant stream wait, blocking subsequent acquire/release operations.

**Fix**: fire-and-forget the spawn (no stderr stream wait). Errors route to the systemd journal via `stderr: 'inherit'`, viewable with `journalctl -u <service>`.

## Round 3 — the fundamental flaw

### HIGH-1: the channel cannot await its own restart

The round-2 design was: `await spawn(systemctl restart) → sleep → acceptMessages = true → return 200`. This **cannot work** in production. The reasoning chain:

1. The channel process is a stdio MCP server, child of the `claude` binary running inside tmux.
2. `systemctl restart inkieran-cc.service` causes tmux to kill the `claude` session.
3. `claude` dies → all its MCP child processes die, including the channel.
4. The `await spawn.exited` blocks until... the channel itself is killed.
5. Even if `await` returned a value, the HTTP response would never finish writing — the writer is dead.
6. The coordinator sees `RemoteProtocolError` → executes acquire-failed rollback → ends in half-switched state.

This was the fundamental flaw round 1 and 2 had been incrementally refining without realizing it couldn't reach a working state. The two earlier rounds had been adding safety to a design that did not have a self-consistent execution path.

**Fix**: **fire-and-forget mode**.

- spawn `systemctl restart --no-block` (exits in ~500ms once systemd accepts the D-Bus request)
- await its quick exit with a 3-second timeout (catches sudoers misconfig; doesn't wait for the actual restart)
- immediately return `200 {restart_pending: true}`
- the HTTP response flushes to socket in the ~100-500ms window between `systemctl --no-block` returning and `tmux kill` actually arriving at the `claude` process
- the coordinator polls `/session/current` on the new URL until ready

The coordinator handles this in two new paths: the success path with `restart_pending: true` (see `_poll_until_ready` in `backend/transport.py`), and the exception path (`RemoteProtocolError` on the `httpx.post` — may still indicate restart was dispatched, so poll before declaring failure).

### HIGH-3: jsonl mtime races against the poll

The first sketch of `/session/current` returned the most-recently-modified `.jsonl` file in the project directory.

**Objection**: `claude --resume <sid>` does not write to its jsonl until the first turn. There is a window between "channel returns 200 + restart_pending" and "claude writes its first line" during which the mtime-newest jsonl is **the PREVIOUS session**, not the resumed one. The coordinator polls and sees the old sid → times out → rolls back, even though everything succeeded.

**Fix**: the cc startup script exports `INKIERAN_EXPECTED_SESSION_ID="$sid"` to itself when launched with `--resume`. The channel's `/session/current` reads this env first; mtime scan is only a fallback. The env hint is deterministic and available immediately on process start, before any jsonl write.

This is the line in `templates/cc.sh.patch.sh` that exports the variable, and the corresponding env-first lookup in `getCurrentSessionId` in `channel/server.ts`.

### MED-2: restart_pending poll needs its own timeout

The coordinator was using `max(timeout, …)` to bound the poll, where `timeout` defaulted to 30s.

**Objection**: `claude --resume` can take 25 seconds or more on a cold cache (loading the jsonl + reinitializing context). 30 seconds is too tight; you'll see false-positive timeouts on cold starts and resort to clicking the switch again, which racing the still-finishing first switch.

**Fix**: `poll_timeout = max(40, timeout)` for the `restart_pending` branch. Current code: `backend/transport.py` `_poll_until_ready` callers.

## Round 4 — rollback order

### HIGH: release new before reacquire old

When the `restart_pending` poll times out, the coordinator's rollback was: reacquire old transport's lease.

**Objection**: `systemctl --no-block exit 0` does not guarantee the restart job completes — and meanwhile the channel process may still be alive holding the file lock. If we reacquire old immediately, old's `/lease/acquire` will try to flock the same file and get denied (409). Backend ends up in a **neither-side-has-the-writer** state with the lock held by an unreachable process.

**Fix**: rollback must do two steps in order:

1. best-effort `POST new/lease/release` (will gracefully fail if new is dead; OS releases the lock on process death anyway). This is the "release before reacquire" pattern.
2. then `POST old/lease/acquire`

The rollback result is reported in the response body via `rollback_ok`, `reacquire_old_status`, `reacquire_old_error` — a coordinator caller (often a UI button) needs to know whether the rollback itself succeeded or whether a human needs to look at it. We do not want to silently mask rollback failures.

See `_rollback_release_then_reacquire` in `backend/transport.py`.

## Round 5 — cleanup

### LOW: `typing.Any` import missing

Round 4's reacquire result accumulator used `Optional[int]` and `Any` in type annotations. The `Any` import had been dropped in an earlier refactor.

Static type-check caught it. Cosmetic round.

## Why five rounds?

Each round found something the previous round could not have, because each round took the previous round's diff as a given and looked for what the new shape made wrong.

- Round 3's "fundamental flaw" was only visible once round 2's budget derivation was in place — without budget derivation, the design had so many race windows that "the await blocks forever" looked like just another one.
- Round 4's release-before-reacquire only surfaced because round 3 introduced the restart-pending poll, which created the new "channel-alive-and-holding-lock-but-unreachable" state.
- Round 1's reorder enabled round 2's cleanup paths to be coherently statable — without lock-first, the cleanup paths multiplied combinatorially.

This is the case for adversarial review on tricky concurrent code: not that one reviewer would have caught everything in one pass, but that fresh eyes on each iteration catch what the iteration newly broke. If you change anything in this path, get a fresh review on the diff. The dangerous corners are the ones the diff itself just created.
