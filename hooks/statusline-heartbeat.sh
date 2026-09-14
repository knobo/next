# Append this block to the bottom of ~/.claude/statusline-command.sh (DESIGN.md §3.5).
# Heartbeat + token report for free: the status line already receives rate_limits and
# context_window.
# `budget` is a LIST of windows, not Anthropic's two fixed columns: a harness with other
# windows (or none) reports its own without the board having to change (T-164).
# `map(select(.used_pct != null))`: if rate_limits is missing from the payload (API key,
# Bedrock, a miss), we must not report a window with no reading — that would erase a good
# number.
# Never blocks the status line — 1 s timeout, backgrounded, errors ignored.
AGENT_ID=$(cat "$HOME/.cache/board/agent" 2>/dev/null)
[ -n "${BOARD_URL:-}" ] && [ -n "$AGENT_ID" ] && ( jq -c '{session:.session_id, model:.model.id,
   ctx_pct:.context_window.used_percentage, cwd:.workspace.current_dir,
   budget:[{window:"5h", used_pct:.rate_limits.five_hour.used_percentage,
            resets_at:.rate_limits.five_hour.resets_at},
           {window:"7d", used_pct:.rate_limits.seven_day.used_percentage}]
           | map(select(.used_pct != null))}' <<<"$input" \
 | curl -s -m 1 -H "Authorization: Bearer $BOARD_TOKEN" -H 'Content-Type: application/json' \
     -d @- "$BOARD_URL/api/v1/agents/$AGENT_ID/heartbeat" >/dev/null 2>&1 & )
