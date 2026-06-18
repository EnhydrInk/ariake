# Optional: hardened sid file race window

## Status

**NOT IMPLEMENTED.** This document describes a path that was considered, fully spec'd, and consciously not taken — so a reader knows the road exists and does not have to rebuild the analysis from zero.

## The problem (current trade-off)

`channel/server.ts` writes the resume sid to `INKIERAN_RESUME_SID_FILE` (default `/tmp/ariake-cc-resume-sid`) before spawning `systemctl restart`. The cc startup script reads it on next boot. Current implementation:

- `writeFile` syscall (sync at the syscall layer for small payloads)
- the cc script reads the file, then `rm -f`s it
- sid format is validated (UUID-ish regex) on both write side and read side

**Race window**: a crash between the writeFile completing and the cc startup script reading + removing the file. On the next cc launch, the script reads a leftover sid and resumes the wrong session.

The window is small (~1ms — writeFile is sync, the spawn happens immediately after, the crash has to land in microseconds). It is non-zero.

## Full fix (spec)

The hardened layout the original review proposed but did not ship:

- **runtime dir mode 0700**: don't use `/tmp` (shared, multi-user); use a private dir like `/run/ariake` or `~/.local/state/ariake` with restrictive perms.
- **sid file mode 0600**: the file content is sensitive in the sense that anyone who can read it can hijack a session resume.
- **atomic rename**: write to `<file>.tmp`, then `rename(2)` to the final name. Defeats partial reads.
- **JSON content** `{"sid": "...", "created_at": "<ISO timestamp>"}` — the timestamp enables age-based filtering.
- **cc script validates**:
  - sid format (same UUID-ish regex)
  - age ≤ 60 seconds (defeats stale leftovers from a crash days ago)
  - reads and `rm -f`s immediately on successful parse

## Why we did not ship it

The current trade-off is acceptable for an MVP:

- The crash window is small enough that probability across a year of switches is negligible
- The recovery path on a wrong-sid resume is graceful (`claude --resume` fails on unknown sid → falls back to `--continue` → fresh session, user notices and re-tries the switch)
- The file-on-tmp approach makes debugging visible (you can `cat /tmp/ariake-cc-resume-sid` to see what's pending)
- The hardened version requires changing both `channel/server.ts` and `templates/cc.sh.patch.sh`, two files that are otherwise stable

## When to revisit

- If you deploy multi-user (multiple humans / tenants sharing a host where `/tmp` is shared)
- If the host's `/tmp` lifecycle is unusual (tmpfs that survives reboot, or shared across containers via bind mount)
- If you observe spurious "fresh session after switch" events in logs that would otherwise be unexplained

When implementing, run the new path through the same five-round review process — particularly, what crash points are introduced between the temp-file write and the atomic rename. That sequence has its own race windows.
