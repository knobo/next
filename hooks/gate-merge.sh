#!/usr/bin/env bash
# PreToolUse hook: the mechanical merge gate (DESIGN.md §8.2).
#
# Prompt text ("run a review before merging") has already failed in practice — so we ask
# the board instead, and refuse if it says no. Works in subagents too, since hooks are
# per process. Fails OPEN: if the board is down or the task is unknown, we stay out of it.
set -uo pipefail
IN=$(cat)
CMD=$(jq -r '.tool_input.command // ""' <<<"$IN")

case "$CMD" in
  *"tea pr merge"*|*"gh pr merge"*|*"git push"*origin*main*|*"git push"*main*) ;;
  *) exit 0 ;;
esac

# The same session keying as bin/board, otherwise the hook reads another session's task.
SESS="${BOARD_SESSION:-${CLAUDE_CODE_SESSION_ID:-}}"
# Keyed on the board URL, exactly as bin/board's $CACHE. This read the cache ROOT, where the
# CLI stopped writing when the cache was keyed on the board: no task was ever found, and the
# gate let every merge through (it fails open on "no task").
ROOT="${BOARD_CACHE:-$HOME/.cache/board}"
CACHE="$ROOT/$(printf '%s' "${BOARD_URL:-http://localhost:8080}" | sed 's|^[^:]*://||' | tr -c 'A-Za-z0-9._-' '_')"
T=$(cat "$CACHE/current-task${SESS:+-$SESS}" 2>/dev/null)
[ -n "$T" ] || exit 0                       # no claimed task: none of our business

# If the task is finished, the file is stale. Without this an old task id blocks work in
# an entirely different repo, forever.
ST=$(board task show "$T" --json 2>/dev/null | jq -r '.status // empty' 2>/dev/null)
case "$ST" in claimed|in_review|merging) ;; *) exit 0 ;; esac

OUT=$(board gate merge "$T" 2>/dev/null) || {
  # exit≠0 from the CLI can also mean "the board is down". Tell them apart by whether we
  # got an answer at all.
  REASONS=$(jq -r '.reasons // [] | join("; ")' 2>/dev/null <<<"$OUT")
  [ -n "$REASONS" ] || exit 0
  jq -nc --arg r "board gate merge $T closed: $REASONS" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
}
exit 0
