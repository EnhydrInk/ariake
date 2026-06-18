#!/usr/bin/env bash
# ============================================================================
# Ariake CC startup script — sdk -> tmux resume patch
# ============================================================================
# This is NOT a standalone script. It is a snippet to PASTE into your
# existing cc startup script (the one your systemd unit runs that launches
# `claude` inside tmux). The snippet handles the sdk -> tmux handoff: it
# reads a resume sid file written by channel/server.ts and launches claude
# with --resume <sid> when one is present.
#
# Related files:
#   - channel/server.ts (/lease/acquire writes the sid to RESUME_SID_FILE)
#   - templates/sudoers.example (allows passwordless systemctl restart)
#   - docs/sdk-tmux-resume-proposal.md (full design rationale)
#
# Usage:
#   1. Back up your existing cc startup script.
#   2. In the original, find the line that runs `exec claude ...`.
#   3. REPLACE that line with the snippet below; keep cd / env above unchanged.
#   4. Edit CLAUDE_BASE_ARGS to match your original `claude` arguments
#      (the snippet preserves them and appends --resume / --continue).
#   5. systemctl restart <your cc service> and verify it boots cleanly.
#
# Rollback: restore your backed-up script and restart the cc service.
# ============================================================================

# === BEGIN sdk -> tmux resume patch =========================================
# Decide claude's startup arguments from the resume sid file:
#   - file exists + content is a valid sid -> --resume $sid (continue sdk's session)
#   - file exists + content is invalid     -> warn + fallback --continue
#   - no file + jsonl pool non-empty       -> --continue (normal startup)
#   - no file + jsonl pool empty           -> bare start (first launch)
#
# RESUME_SID_FILE MUST match channel/server.ts's INKIERAN_RESUME_SID_FILE
# env, or the channel writes a file the script cannot find = silent forgetting.
RESUME_SID_FILE="${INKIERAN_RESUME_SID_FILE:-/tmp/ariake-cc-resume-sid}"

# Use a bash array, not a string. String concatenation gets word-split on
# expansion and any future multi-arg case breaks. Array elements stay atomic.
RESUME_ARGS=()

if [ -f "$RESUME_SID_FILE" ]; then
  sid=$(cat "$RESUME_SID_FILE" 2>/dev/null | tr -d '[:space:]')
  # rm immediately — even if validation below fails, do not leave the file
  # around to be picked up by a later launch with a stale sid.
  rm -f "$RESUME_SID_FILE"
  # Validate UUID-ish format. Same regex channel/server.ts uses on the write
  # side, so both sides agree on what counts as a valid sid. Defense: a
  # garbage / partial / shell-injection sid would otherwise be passed to
  # claude --resume verbatim, with undefined behavior.
  if echo "$sid" | grep -qE '^[0-9a-fA-F-]{8,64}$'; then
    RESUME_ARGS=("--resume" "$sid")
    # Export the sid as an env hint for channel/server.ts. Its /session/current
    # uses this in preference to a jsonl-mtime scan — defeats the race where
    # `claude --resume` has not yet written its first line but the channel
    # gets polled for readiness by the coordinator.
    export INKIERAN_EXPECTED_SESSION_ID="$sid"
    echo "ariake-cc: resume sid=${sid:0:8}… (from $RESUME_SID_FILE)" >&2
  else
    echo "ariake-cc: invalid sid in $RESUME_SID_FILE (length=${#sid}), falling back" >&2
  fi
fi

# Fallback chain: no valid resume sid -> try --continue to pick up the most
# recent jsonl. CC encodes the cwd as the project directory name (replacing
# `/` with `-`), so for cwd=/opt/ariake/cc-cwd the slug is `-opt-ariake-cc-cwd`.
# Adjust ARIAKE_CWD_SLUG to match your INKIERAN_CWD / INKIERAN_SDK_CWD.
ARIAKE_CWD_SLUG="${INKIERAN_CWD_SLUG:--opt-ariake-cc-cwd}"
if [ ${#RESUME_ARGS[@]} -eq 0 ]; then
  if ls "$HOME/.claude/projects/${ARIAKE_CWD_SLUG}"/*.jsonl >/dev/null 2>&1; then
    RESUME_ARGS=("--continue")
  fi
fi

# === REPLACE below with your ORIGINAL `exec claude` arguments ==============
# Paste your existing `claude` arguments into CLAUDE_BASE_ARGS.
# Examples:
#   original was `exec claude --model claude-opus-4-7`:
#     CLAUDE_BASE_ARGS=("--model" "claude-opus-4-7")
#   original was bare `exec claude` (most common):
#     CLAUDE_BASE_ARGS=()
#
# ⚠️ Required step. If you skip it, your cc service runs with different args
# than before and behavior diverges silently.
CLAUDE_BASE_ARGS=()  # ← fill from your original cc startup script

# Final exec. Single-line keeps claude as a direct child of systemd so the
# restart path's tmux kill propagates cleanly. "${ARR[@]}" expansion: empty
# arrays do not produce spurious empty arguments.
exec claude "${CLAUDE_BASE_ARGS[@]}" "${RESUME_ARGS[@]}"
# === END sdk -> tmux resume patch ===========================================
