#!/usr/bin/env bash
# SessionEnd hook: report that the agent has finished, so leases are released at once
# instead of waiting for the reaper. ~/.claude/settings.json:
#   "SessionEnd": [{"hooks":[{"type":"command","command":"<repo>/hooks/session-end.sh"}]}]
exec board finished --reason "session end" >/dev/null 2>&1
