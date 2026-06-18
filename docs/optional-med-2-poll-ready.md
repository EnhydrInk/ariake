# Optional: stronger `/session/current` ready signal

## Status

**NOT IMPLEMENTED.** Same disposition as `optional-med-3-sid-race.md`.

## The problem (current trade-off)

After the fire-and-forget restart, the coordinator polls `/session/current` until `session_id == resume_sid && active == true`. The channel-side answer comes from two sources, in order:

1. `INKIERAN_EXPECTED_SESSION_ID` env (set by the cc startup script when launched with `--resume <sid>`)
2. fallback: scan `~/.claude/projects/*/*.jsonl`, filter by `cwd` field, return the most-recently-modified

Source 1 is deterministic and fast — set in the script's exec line, well before the new channel process starts. Source 2 is only used when source 1 is absent, which shouldn't happen in production but covers the case of running `channel/server.ts` manually without going through the script.

**What "ready" actually means in current implementation**: the env hint is set AND the channel process is up AND `acceptMessages` has flipped to true. This is correct in the happy path but doesn't actually verify that CC is responsive — only that the lock has been acquired by something with the right env.

## Stronger signal (spec)

A more reliable signal:

1. `systemctl is-active <cc service>` returns `active`
2. `tmux has-session -t <session>` succeeds
3. `tmux capture-pane -t <session>:0 -p` shows stable content (two consecutive captures match byte-for-byte over a 500ms window)
4. The capture does not contain known loading / auth / error strings (`Loading project`, `Please log in`, `ERROR`, `Initializing`, ...)

This would guarantee CC is actually ready to receive keystrokes — not just that some process has acquired the file lock.

## Why we did not ship it

- **Complexity**: implementing the capture-pane parser is a project on its own. CC's startup output varies across versions; "stable for 500ms" requires picking the timing constants empirically.
- **Cross-platform fragility**: tmux behavior on non-POSIX systems (some BSDs, WSL2 corner cases) varies enough that the same capture-pane invocation can produce different output. The env-hint approach is portable.
- **Match string drift**: the strings to watch for ("Loading project" etc.) drift with CC versions. A list maintained in source code goes stale silently.
- **Not blocking**: the env hint already covers the production path. The reasons to want this are corner cases that haven't materialized.

## When to revisit

- If you find scenarios where `INKIERAN_EXPECTED_SESSION_ID` is reliably set but the channel reports ready while CC is still in a modal / loading state
- If you operate CC across many host environments with non-trivial startup paths (some with `--resume` actually failing partway, leaving CC in a recoverable but not-yet-ready state)
- If you observe "first message after switch silently lost to modal-eat" reports correlated with the post-switch window

If you do implement this, the natural place is a new endpoint `/session/ready` returning a stronger signal than `/session/current`'s `active` flag. Keep `/session/current` simple so old coordinators don't break.
