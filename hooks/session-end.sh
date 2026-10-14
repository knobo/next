#!/usr/bin/env bash
# SessionEnd hook: report that the agent has finished, so leases are released at once
# instead of waiting for the reaper. ~/.claude/settings.json:
#   "SessionEnd": [{"hooks":[{"type":"command","command":"<repo>/hooks/session-end.sh"}]}]
#
# Identity comes from the hook's own stdin (.session_id), never from the environment: a
# nested `claude -p` inherits CLAUDE_CODE_SESSION_ID from the parent's shell, so `board
# finished` would resolve the PARENT's agent and end it (T-863). Same keyed cache lookup as
# statusline-heartbeat.sh; no mapping for this session = it never registered = do nothing.
SESS=$(jq -r '.session_id // empty' 2>/dev/null)
[ -n "$SESS" ] || exit 0
CACHE="${BOARD_CACHE:-$HOME/.cache/board}/$(printf '%s' "${BOARD_URL:-http://localhost:8080}" | sed 's|^[^:]*://||' | tr -c 'A-Za-z0-9._-' '_')"
[ -s "$CACHE/session-$SESS" ] || exit 0
BOARD_SESSION="$SESS" BOARD_AGENT_ID=$(cat "$CACHE/session-$SESS") \
  exec board finished --reason "session end" >/dev/null 2>&1
