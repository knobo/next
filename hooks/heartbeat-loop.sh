#!/usr/bin/env bash
# Heartbeat for harnesses WITHOUT a status line (grok, codex). Claude Code gets it for
# free from the statusline hook; the others have nothing, and then the reaper marks the
# agent dead in the middle of its work. Run it alongside the session; it dies with it.
#   BOARD_HARNESS=grok BOARD_SESSION=grok-1 hooks/heartbeat-loop.sh <pid> &
set -uo pipefail
PID="${1:?usage: heartbeat-loop.sh <pid of the agent>}"
while kill -0 "$PID" 2>/dev/null; do
  board heartbeat >/dev/null 2>&1
  sleep 60
done
