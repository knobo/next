# Append this block to the bottom of ~/.claude/statusline-command.sh (DESIGN.md §3.5).
# Heartbeat + token report for free: the status line already receives rate_limits and
# context_window.
# `budget` is a LIST of windows, not Anthropic's two fixed columns: a harness with other
# windows (or none) reports its own without the board having to change (T-164).
# `map(select(.used_pct != null))`: if rate_limits is missing from the payload (API key,
# Bedrock, a miss), we must not report a window with no reading — that would erase a good
# number.
# Never blocks the status line — 1 s timeout, backgrounded, errors ignored.
# The agent id is per SESSION (bin/board start/register write it to session-<id>, keyed
# under the board's own cache dir -- same urlsafe transform as bin/board's $CACHE, copied
# verbatim below). A session with no mapping there must NOT fall back to the flat `agent`
# file: that file is shared across every session on the machine, so the fallback kept
# exactly one ghost agent id alive forever and it won at the coordinator ranking (T-435).
# There is no flat compat copy to fall back to either -- a second, non-keyed write there
# would reopen the same collision one BOARD_URL up (same session registering against two
# boards, whichever wrote last wins the other board's heartbeat). Only a session-less
# caller (no `.session_id` in the payload at all) uses the flat file.
CACHE_ROOT="${BOARD_CACHE:-$HOME/.cache/board}"
BOARD_SESS=$(jq -r '.session_id // empty' <<<"$input" 2>/dev/null)
if [ -n "$BOARD_SESS" ]; then
  CACHE="$CACHE_ROOT/$(printf '%s' "${BOARD_URL#*://}" | tr -c 'A-Za-z0-9._-' '_')"
  AGENT_ID=""
  [ -s "$CACHE/session-$BOARD_SESS" ] && AGENT_ID=$(cat "$CACHE/session-$BOARD_SESS")
else
  AGENT_ID=$(cat "$CACHE_ROOT/agent" 2>/dev/null)
fi
[ -n "${BOARD_URL:-}" ] && [ -n "$AGENT_ID" ] && ( jq -c '{session:.session_id, model:.model.id,
   ctx_pct:.context_window.used_percentage, cwd:.workspace.current_dir,
   budget:[{window:"5h", used_pct:.rate_limits.five_hour.used_percentage,
            resets_at:.rate_limits.five_hour.resets_at},
           {window:"7d", used_pct:.rate_limits.seven_day.used_percentage,
            resets_at:.rate_limits.seven_day.resets_at}]
           | map(select(.used_pct != null))}' <<<"$input" \
 | curl -s -m 1 -H "Authorization: Bearer $BOARD_TOKEN" -H 'Content-Type: application/json' \
     -d @- "$BOARD_URL/api/v1/agents/$AGENT_ID/heartbeat" >/dev/null 2>&1 & )
