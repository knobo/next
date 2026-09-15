#!/usr/bin/env bash
# Conformance suite for the board — the acceptance criterion for phase 0 (DESIGN.md §9).
# Runs every operation in §4 against a fresh board.py. Can be run against any backend that
# implements the same API:  ./conformance.sh https://board.example.com
set -uo pipefail
cd "$(dirname "$0")"

# The target is ALWAYS a fresh instance of its own — unless you say otherwise with an
# argument. BOARD_URL is exported in the shell of every agent that uses the board, so
# inheriting it from the environment would have meant that an innocent ./conformance.sh
# registers agents, creates tasks, pins roles and fires ntfy pushes in production. Intent
# is written, not inherited.
#   ./conformance.sh                             its own instance on a free port
#   ./conformance.sh https://board.example.com   against a real board — this is also the
#                                                hook an alternative backend is tested behind
TARGET="${1:-}"
[ "$TARGET" = "--against" ] && { TARGET="${2:-}"; [ -n "$TARGET" ] || { echo "--against without a url"; exit 2; }; }
case "$TARGET" in
  -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
  ""|http://*|https://*) ;;
  *) echo "conformance.sh [<board url> | --against <url>]  — with no argument: its own instance"; exit 2 ;;
esac

# A free port when none is given: several agents must be able to run the suite at once (T-117).
PORT="${BOARD_PORT:-$(python3 -c "import socket;s=socket.socket();s.bind((\"\",0));print(s.getsockname()[1]);s.close()")}"
# The tests that play the human (answering questions, pinning roles) must PROVE they are
# the human: `by: <human>` is no longer enough, that is the whole point of the human token.
HUMAN_TOKEN="${BOARD_HUMAN_TOKEN:-human-conformance}"
TMP=$(mktemp -d); trap 'kill %1 2>/dev/null; kill ${NTFY_PID:-0} 2>/dev/null; rm -rf "$TMP"' EXIT
OWN_SERVER=0
NTFY_PID=

if [ -n "$TARGET" ]; then
  # Against a real board the token has to be right. Falls back to `pass`, and says so
  # clearly rather than carrying on with the wrong token and reporting 38 "failures" that
  # are really 401s.
  BOARD_URL="$TARGET"
  TOKEN="${BOARD_TOKEN:-$(pass board/token 2>/dev/null | head -1)}"
  [ -n "$TOKEN" ] || { echo "$TARGET given, but no BOARD_TOKEN and `pass board/token` failed"; exit 2; }
  echo "== running against $BOARD_URL â the suite WRITES in the project «demo» there =="
else
  TOKEN=conformance         # our own instance: we pick the token ourselves
  cat > "$TMP/policy.json" <<'JSON'
{"grants": {"demo": {"claude-code@*": ["merge","deploy-dev"], "codex@*": ["deploy-dev"], "*": []}},
 "budget": {"demo": {"ceilings": {"5h": 85, "7d": 75}},
            "zerobudget": {"ceilings": {"7d": 0}},
            "fallbackproject": {"ceilings": {"5h": 85}, "fallback": {"max_tasks": 1}},
            "oldshape": {"rl7_ceiling": 60}},
 "roles": {"coordinator": {"singleton": true, "requires": ["merge"], "prefer_model": ["fable","opus","sonnet"]},
           "tester": {"requires": ["browser-test"]}, "implementer": {"requires": []}}}
JSON
  # A short reap interval here: the suite must POLL observed state (the T-184 flake came
  # from sleeping a fixed number of seconds and guessing), not wait for the real 60s cycle.
  # Its own ntfy receiver: NTFY_URL is read at import, so the port has to be chosen BEFORE
  # board.py. Without this the push was untested, and it was effectively dead for a day
  # (emoji in the Title header).
  NTFY_PORT=$(python3 -c "import socket;s=socket.socket();s.bind((\"\",0));print(s.getsockname()[1]);s.close()")
  NTFY_LOG="$TMP/ntfy" NTFY_PORT="$NTFY_PORT" \
  BOARD_DB="$TMP/board.db" BOARD_TOKEN="$TOKEN" BOARD_HUMAN_TOKEN="$HUMAN_TOKEN" BOARD_POLICY="$TMP/policy.json" \
    BOARD_PORT="$PORT" BOARD_BASE_URL="http://localhost:$PORT" BOARD_REAP_INTERVAL=1 \
    NTFY_URL="http://127.0.0.1:$NTFY_PORT/board" \
    python3 board.py > "$TMP/log" 2>&1 &
  OWN_SERVER=1
  BOARD_URL="http://localhost:$PORT"
  NTFY_LOG="$TMP/ntfy" python3 - "$NTFY_PORT" > "$TMP/ntfy-log" 2>&1 <<'PY' &
import os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        with open(os.environ["NTFY_LOG"], "ab") as f:
            f.write(("TITLE: %s\n" % self.headers.get("Title", "")).encode("utf-8", "replace"))
            f.write(self.rfile.read(n) + b"\n---\n")
        self.send_response(200); self.end_headers()
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
  NTFY_PID=$!
  for _ in $(seq 50); do curl -sf "$BOARD_URL/healthz" >/dev/null && break; sleep .1; done
fi

PASS=0; FAIL=0
api() { local m="$1" p="$2"; shift 2
  curl -sS -m 5 -X "$m" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    ${1:+-d "$1"} "$BOARD_URL/api/v1$p"; }
hum() { local m="$1" p="$2"; shift 2
  curl -sS -m 5 -X "$m" -H "Authorization: Bearer $HUMAN_TOKEN" -H 'Content-Type: application/json' \
    ${1:+-d "$1"} "$BOARD_URL/api/v1$p"; }
ok()  { PASS=$((PASS+1)); printf '  \033[32m✓\033[0m %s\n' "$1"; }
no()  { FAIL=$((FAIL+1)); printf '  \033[31m✗\033[0m %s\n     %s\n' "$1" "${2:-}"; }
check() { # check "name" <json> <jq-filter>
  if jq -e "$3" >/dev/null 2>&1 <<<"$2"; then ok "$1"; else no "$1" "$2"; fi; }
# Like api(), but the HTTP status is in the JSON. T-396 is 200 vs 400 vs 409, and `.error`
# alone cannot tell those apart.
apic() { local m="$1" p="$2"; shift 2
  local code
  code=$(curl -sS -m 5 -o "$TMP/apic.json" -w '%{http_code}' -X "$m" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    ${1:+-d "$1"} "$BOARD_URL/api/v1$p")
  jq -nc --argjson c "$code" --slurpfile b "$TMP/apic.json" '{code:$c, body:$b[0]}'
}

echo "== manifest.py: worktree_path (T-67) =="
WTCHK=$(python3 -c "
import importlib.util as u
s = u.spec_from_file_location('m', 'bin/manifest.py')
m = u.module_from_spec(s); s.loader.exec_module(m)
mm = {'root': '/tmp/proj', 'project': 'next', 'worktrees': '../{repo}-worktrees/{branch}'}
a = m.worktree_path(mm, '.', 'task/T-1')
b = m.worktree_path(mm, 'next', 'task/T-1')
assert a == '/tmp/next-worktrees/task/T-1', a
assert b == '/tmp/next-worktrees/task/T-1', b
# Multi-repo (DESIGN.md §3.2b): the project name is the root, a real repo is a subdirectory.
mr = {'root': '/tmp/demoproj', 'project': 'demoproj', 'worktrees': '../{repo}-worktrees/{branch}'}
c = m.worktree_path(mr, 'demoproj', 'task/T-2')
d = m.worktree_path(mr, 'api', 'task/T-2')
assert c == '/tmp/demoproj-worktrees/task/T-2', c
assert d == '/tmp/demoproj/api-worktrees/task/T-2', d
print('ok')
" 2>&1)
[ "$WTCHK" = ok ] && ok "worktree_path: project name and subdirectory repo, single and multi repo" || no "worktree_path" "$WTCHK"

echo "== bin/board: repo_dir mirrors worktree_path, unknown repo included (T-67) =="
# repo_dir() must NOT fall back silently to the first repo for an unknown repo field —
# that was the regression itself (checking out the wrong repo with no warning). Pulls the
# function (plus die(), which it uses) straight out of bin/board instead of duplicating
# the logic.
cat > "$TMP/repo_dir_check.sh" <<'SCRIPT'
set -uo pipefail
ROOT=/tmp/demoproj; PROJECT=demoproj; REPOS='api web'
[ "$(repo_dir demoproj)" = /tmp/demoproj ] || { echo "project name != root"; exit 1; }
[ "$(repo_dir .)" = /tmp/demoproj ] || { echo ". != root"; exit 1; }
[ "$(repo_dir api)" = /tmp/demoproj/api ] || { echo "known repo wrong"; exit 1; }
(repo_dir typo-in-the-field) >/dev/null 2>&1 && { echo "should have failed loudly on an unknown repo"; exit 1; }
echo ok
SCRIPT
{ sed -n '/^die() {/p' bin/board; sed -n '/^repo_dir() {/,/^}/p' bin/board; cat "$TMP/repo_dir_check.sh"; } > "$TMP/repo_dir_check_full.sh"
RDCHK=$(bash "$TMP/repo_dir_check_full.sh" 2>&1)
[ "$RDCHK" = ok ] && ok "repo_dir: project name/known repo mirror the root, an unknown repo fails loudly" || no "repo_dir multirepo" "$RDCHK"

echo "== manifest root: a worktree is not the project root (T-67) =="
# A worktree has its own project.yaml. If paths are computed from there the worktrees nest
# (`proj-worktrees/proj-worktrees/...`) and cleanup points at the wrong repo.
SRC=$PWD
WTROOT=$({
  p="$TMP/mfroot"; mkdir -p "$p"; cd "$p" || exit
  git init -q . && git config user.email a@b && git config user.name a
  printf 'project: mfroot\nphase: build\nrepos: [.]\n' > project.yaml
  git add -A && git commit -qm init
  git worktree add -q "$TMP/mfroot-worktrees/task/T-1" -b task/T-1 >/dev/null 2>&1
  cd "$TMP/mfroot-worktrees/task/T-1" || exit
  python3 -c "
import importlib.util as u
s = u.spec_from_file_location('m', '$SRC/bin/manifest.py'); m = u.module_from_spec(s); s.loader.exec_module(m)
p = m.find(); mm = m.load(p)
print(p, m.worktree_path(mm, 'mfroot', 'task/T-2'))"
} 2>&1 | tail -1)
check "find() from a worktree gives the primary checkout, and the path does not nest" \
  "$(jq -nc --arg o "$WTROOT" '{o:$o}')" \
  '.o=="'"$TMP/mfroot/project.yaml $TMP/mfroot-worktrees/task/T-2"'"'

# T-390: the human token gate. Two ways to be wrong, and the suite has to catch both:
# withhold it from the owner (the bug T-390 was filed for) or hand it to an agent (§3.7).
#
# These checks run the SHIPPED header of bin/board — everything up to and including the
# line that decides which token wins — with a stubbed `pass` on PATH. The first version of
# these checks re-typed the AGENT_SESSION expression inline instead, and review proved
# what that is worth: reverting the fix in bin/board left all 228 checks green. A check
# that re-implements the thing it tests only proves the tester can type it twice.
echo "== the human token gate, against the real bin/board (T-390) =="
HSDIR="$TMP/harness-home"; mkdir -p "$HSDIR/.codex" "$HSDIR/.grok" "$TMP/fakebin"
echo '{}' > "$HSDIR/.grok/active_sessions.json"
cat > "$TMP/fakebin/pass" <<'PASSSTUB'
#!/usr/bin/env bash
case "$1" in board/human-token) echo STUB-HUMAN ;; board/token) echo STUB-AGENT ;; *) exit 1 ;; esac
PASSSTUB
chmod +x "$TMP/fakebin/pass"
sed -n '1,/^\[ -n "\$BOARD_HUMAN_TOKEN" \] && BOARD_TOKEN="\$BOARD_HUMAN_TOKEN"$/p' "$SRC/bin/board" > "$TMP/hdr.sh"
grep -q 'BOARD_TOKEN="\$BOARD_HUMAN_TOKEN"' "$TMP/hdr.sh" \
  && ok "the real bin/board header was extracted for these checks" \
  || no "could not extract bin/board's header — the checks below would test nothing" ""
echo 'case "$BOARD_TOKEN" in STUB-HUMAN) echo human ;; STUB-AGENT) echo agent ;; *) echo other ;; esac' >> "$TMP/hdr.sh"
tok_as() {  # env assignments -> which token the SHIPPED header selects
  env -u CLAUDE_CODE_SESSION_ID -u ANTIGRAVITY_AGENT -u ANTIGRAVITY_CONVERSATION_ID \
      -u GROK_SESSION_ID -u GROK_CLI -u CODEX_SESSION_ID -u CODEX_HOME -u BOARD_HARNESS \
      -u BOARD_TOKEN -u BOARD_HUMAN_TOKEN -u BOARD_AS_HUMAN -u BOARD_URL \
      HOME="$HSDIR" PATH="$TMP/fakebin:$PATH" "$@" bash "$TMP/hdr.sh" 2>/dev/null | tail -1
}
# §3.7, the direction that must never break: an agent must not end up holding it.
[ "$(tok_as CLAUDE_CODE_SESSION_ID=x)" = agent ] \
  && ok "a live claude-code session gets the agent token" || no "claude-code session got the human token" "$(tok_as CLAUDE_CODE_SESSION_ID=x)"
[ "$(tok_as BOARD_HARNESS=grok)" = agent ] \
  && ok "an explicitly declared harness gets the agent token" || no "declared harness got the human token" "$(tok_as BOARD_HARNESS=grok)"
# A codex agent sets NEITHER CODEX_SESSION_ID (not a real variable) nor CODEX_HOME by
# default. Env evidence alone misses it entirely; the installed-tool probe is what catches
# it, which is why this fails closed on both kinds of evidence at once.
[ "$(tok_as)" = agent ] \
  && ok "a codex/grok agent with no session variable still gets the agent token" \
  || no "an agent with no session variable reached the human token (§3.7 broken)" "$(tok_as)"
# And the direction T-390 was filed for: the owner has to be able to get at it.
[ "$(tok_as BOARD_AS_HUMAN=1)" = human ] \
  && ok "BOARD_AS_HUMAN=1 gets the owner the human token despite installed harnesses" \
  || no "the owner cannot reach the human token even when saying so" "$(tok_as BOARD_AS_HUMAN=1)"
# On a machine with no harness installed at all, no ceremony should be needed.
CLEANH="$TMP/clean-home"; mkdir -p "$CLEANH"
[ "$(env -u CLAUDE_CODE_SESSION_ID -u CODEX_HOME -u CODEX_SESSION_ID -u GROK_SESSION_ID \
      -u GROK_CLI -u BOARD_HARNESS -u BOARD_TOKEN -u BOARD_HUMAN_TOKEN -u BOARD_AS_HUMAN \
      HOME="$CLEANH" PATH="$TMP/fakebin:$PATH" bash "$TMP/hdr.sh" 2>/dev/null | tail -1)" = human ] \
  && ok "with no harness installed or running, the owner just gets the human token" \
  || no "the owner is locked out on a clean machine" ""
# An explicitly exported token always wins, which is how a deliberate caller passes one in.
[ "$(tok_as BOARD_HUMAN_TOKEN=STUB-HUMAN CLAUDE_CODE_SESSION_ID=x)" = human ] \
  && ok "an explicitly exported BOARD_HUMAN_TOKEN wins over the sniffing" || no "exported human token ignored" ""

# The repository slug a PR is opened against comes from the git remote, because the task's
# `repo` field is free text and is "." for a single-repo project — `$FORGE_ORG/.` is not a
# repository, and the forge refused it.
#
# Like the human-token checks above, this extracts the SHIPPED block from bin/board and
# runs it, with a stubbed `git` answering the one call it makes. The first version of this
# section defined its own slug_of()/shaped() copies; review broke the real regex in
# bin/board and the suite stayed green, which is the whole lesson of this task repeated one
# commit later.
echo "== the PR slug comes from the remote, and is shape-checked (T-390) =="
sed -n '/slug=\$(git -C "\$wt" remote get-url origin/,/|| slug="\${FORGE_ORG/p' \
  "$SRC/bin/board" > "$TMP/slugblock.sh"
grep -q 'FORGE_ORG' "$TMP/slugblock.sh" && grep -q 'remote get-url' "$TMP/slugblock.sh" \
  && ok "the real slug block was extracted from bin/board for these checks" \
  || no "could not extract the slug block — the checks below would test nothing" "$(cat "$TMP/slugblock.sh")"
cat > "$TMP/fakebin/git" <<'GITSTUB'
#!/usr/bin/env bash
printf '%s\n' "$FAKE_REMOTE"
GITSTUB
chmod +x "$TMP/fakebin/git"
slug_from() {  # a remote URL -> the slug the SHIPPED block derives
  FAKE_REMOTE="$1" PATH="$TMP/fakebin:$PATH" \
    bash -c 'wt=/x; FORGE_ORG=fallbackorg; repo=fallbackrepo
             . "'"$TMP"'/slugblock.sh"
             printf "%s\n" "$slug"' 2>/dev/null
}
[ "$(slug_from 'https://github.com/knobo/next.git')" = "knobo/next" ] \
  && ok "https remote -> org/repo" || no "https remote" "$(slug_from 'https://github.com/knobo/next.git')"
[ "$(slug_from 'git@github.com:knobo/next.git')" = "knobo/next" ] \
  && ok "ssh remote -> org/repo" || no "ssh remote" "$(slug_from 'git@github.com:knobo/next.git')"
# The point of the shape check: anything that is not org/repo must fall back to the
# manifest rather than be handed to the forge as --repo.
[ "$(slug_from '')" = "fallbackorg/fallbackrepo" ] \
  && ok "no remote falls back to the manifest instead of reaching the forge" \
  || no "empty remote" "$(slug_from '')"
[ "$(slug_from 'not-a-url-at-all')" = "fallbackorg/fallbackrepo" ] \
  && ok "a remote with no org/repo shape falls back instead of reaching the forge" \
  || no "shapeless remote" "$(slug_from 'not-a-url-at-all')"

echo "== board without a manifest dies with a message, not unbound variable (T-67) =="
NOMAN=$(cd "$TMP" && "$SRC/bin/board" test-level T-1 2>&1; echo "rc=$?")
check "test-level without a manifest: a message, no unbound variable" \
  "$(jq -nc --arg o "$NOMAN" '{o:$o}')" \
  '(.o|contains("No project.yaml")) and (.o|contains("unbound")|not) and (.o|contains("rc=1"))'

echo "== registration, capabilities, heartbeat =="
A=$(api POST /agents '{"project":"demo","harness":"claude-code","host":"host-a","model":"claude-fable-5-1","session":"s1","capabilities":["browser-test","playwright"]}')
check "register returns an id + grants from the policy" "$A" '.id and (.grants|index("merge"))'
AID=$(jq -r .id <<<"$A")
A2=$(api POST /agents '{"project":"demo","harness":"claude-code","host":"host-a","session":"s1"}')
[ "$(jq -r .id <<<"$A2")" = "$AID" ] && ok "register is idempotent on the session" || no "register idempotent" "$A2"
check "re-registration keeps capabilities and model" "$(api GET '/status?project=demo')" \
  '.projects[0].agents[] | select(.id=="'"$AID"'") | (.capabilities|contains("browser-test")) and (.model!=null)' 
B=$(api POST /agents '{"project":"demo","harness":"codex","host":"host-b","model":"gpt","session":"s2","capabilities":["browser-test"]}')
BID=$(jq -r .id <<<"$B")
check "codex does not get the merge grant" "$B" '(.grants|index("merge"))|not'
check "heartbeat with a window list" "$(api POST /agents/$AID/heartbeat '{"ctx_pct":41.2,"budget":[{"window":"5h","used_pct":63,"resets_at":"2026-09-07T09:00:00Z"},{"window":"7d","used_pct":22}]}')" '.ok'
# A string where the list should be would have been silently ignored — the worst failure a
# quota field can have: the agent thinks it is reporting, the board sees nothing, and the
# stop rule disappears.
check "a budget that is not a list is refused, not silently ignored" \
  "$(api POST /agents/$AID/heartbeat '{"budget":"5h=63"}')" '.error'
# Gammel, Claude-formet statuslinje-hook skal fortsatt telle — ellers gir en hook som ikke
# er oppdatert stille kvote-blackout i det board.py rulles ut.
api POST /agents/$BID/heartbeat '{"rl5_pct":10,"rl7_pct":55}' >/dev/null
check "an old rl5/rl7 report is translated into the 5h/7d windows" "$(api GET '/status?project=demo')" \
  '[.projects[0].agents[]|select(.id=="'"$BID"'")][0].budget | (map(.window)|sort)==["5h","7d"]'
check "caps --add" "$(api PUT /agents/$AID/capabilities '{"add":["kubectl-dev"]}')" '.capabilities|index("kubectl-dev")'
check "preference" "$(api PUT /agents/$BID/preference '{"preference":"implementer"}')" '.preference=="implementer"'
check "project phase" "$(api POST /projects '{"project":"demo","phase":"launch","goal":"users"}')" '.phase=="launch"'

# Quota: the ceilings are the human's, per WINDOW NAME; the number is per account — the
# highest per name.
S=$(api GET '/status?project=demo')
check "the ceilings come from the policy's budget.ceilings" "$S" \
  '.projects[0].budget.ceilings=={"5h":85,"7d":75}'
# "Per account" means per HARNESS account: codex and grok do not share Anthropic's windows
# with the Claude agents. /status shows the Claude fleet's numbers.
check "windows are the HIGHEST per name within the same harness (quota is per account)" "$S" \
  '.projects[0].budget.windows=={"5h":63,"7d":22}'

# T-164 review (score 92): a window name ONE agent in ONE project reports must not stop the
# agents in ANOTHER project that lacks that name in its ceilings. The suite reports such a
# name itself, so against a production board this used to signal stop to the whole fleet.
C=$(api POST /agents '{"project":"otherproject","harness":"grok","host":"host-b","model":"grok-4","session":"s3"}')
CID=$(jq -r .id <<<"$C")
api POST /agents/$CID/heartbeat '{"budget":[{"window":"9t","used_pct":99}]}' >/dev/null
check "an unknown window name in one project does not stop agents in another" \
  "$(api GET '/status?project=demo')" \
  '[.projects[0].agents[]|select(.id=="'"$AID"'")] | length==1 and .[0].stop==null'
check "but the agent that reports a ceiling-less window ITSELF is stopped" \
  "$(api GET '/status?project=otherproject')" \
  '[.projects[0].agents[]|select(.id=="'"$CID"'")][0].stop|test("9t")'

# Over the ceiling: stop, with a reason — and it applies per account, not per session.
api POST /agents/$BID/heartbeat '{"budget":[{"window":"7d","used_pct":80}]}' >/dev/null
SB=$(api GET '/status?project=demo')
check "a window over its ceiling stops the agent, with the window name in the reason" "$SB" \
  '[.projects[0].agents[]|select(.id=="'"$BID"'")][0].stop|test("7d")'
# Naboen stoppes bare innen samme harness-konto. BID er codex, AID er claude-code.
check "…but the codex reading does not stop a claude agent" "$SB" \
  '[.projects[0].agents[]|select(.id=="'"$AID"'")][0].stop == null'
CC2=$(api POST /agents '{"project":"demo","harness":"claude-code","host":"h2","session":"cc2"}' | jq -r .id)
api POST "/agents/$CC2/heartbeat" '{"budget":[{"window":"7d","used_pct":95}]}' >/dev/null
check "…but ANOTHER claude agent's reading does stop it (quota is per account)" \
  "$(api GET '/status?project=demo')" \
  '[.projects[0].agents[]|select(.id=="'"$AID"'")][0].stop|test("7d")'
api POST "/agents/$CC2/finished" '{"reason":"conformance"}' >/dev/null

# Review-funn 85/80: hooken sender {window:"5h", used_pct:null} hver gang rate_limits
# mangler i statuslinje-payloaden. Skrev tavla den, ville en agent som nettopp meldte 80%
# se ut som et harness uten kvoteintrospeksjon — stoppet med feil grunn, eller sluppet fri.
api POST /agents/$BID/heartbeat '{"budget":[{"window":"5h","used_pct":null},{"window":"7d","used_pct":null}]}' >/dev/null
check "a missing reading does not erase the previous one" "$(api GET '/status?project=demo')" \
  '[.projects[0].agents[]|select(.id=="'"$BID"'")][0].budget | map(select(.window=="7d"))[0].used_pct==80'
# Vindusnavn er ikke case-sensitivt: «5H» og «5h» er samme vindu, ellers ryker maksen per konto.
api POST /agents/$AID/heartbeat '{"budget":[{"window":"5H","used_pct":70}]}' >/dev/null
check "window names are case-insensitive — otherwise «5H» is a window with no ceiling" \
  "$(api GET '/status?project=demo')" '.projects[0].budget.windows["5h"]==70'

# A harness without quota introspection: an empty list is an honest value, but not free rein.
F=$(api POST /agents '{"project":"fallbackproject","harness":"codex","host":"host-b","model":"gpt","session":"s5"}')
FID=$(jq -r .id <<<"$F")
api POST /agents/$FID/heartbeat '{"budget":[]}' >/dev/null
check "an agent with no quota report may run to the fallback ceiling" \
  "$(api GET '/status?project=fallbackproject')" \
  '[.projects[0].agents[]|select(.id=="'"$FID"'")] | length==1 and .[0].stop==null'
FT=$(api POST /tasks "{\"agent\":\"$FID\",\"project\":\"fallbackproject\",\"title\":\"one\"}" | jq -r .id)
api POST /tasks/$FT/claim "{\"agent\":\"$FID\"}" >/dev/null
check "…and is stopped by the fallback ceiling once the tasks are used up" \
  "$(api GET '/status?project=fallbackproject')" \
  '[.projects[0].agents[]|select(.id=="'"$FID"'")][0].stop|test("fallback")'
# ...but a harness that SAYS it reports quota (reports-quota from `board probe`) and simply
# has not managed to send yet — pod just restarted, or in the middle of a long subagent with
# no statusline render — must not be stopped with "reports no quota".
Q=$(api POST '/agents' '{"project":"fallbackproject","harness":"claude-code","host":"host-a","session":"s7","capabilities":["reports-quota"]}')
QID=$(jq -r .id <<<"$Q")
check "reports-quota with no reading yet is «do not know yet», not a stop" \
  "$(api GET '/status?project=fallbackproject')" \
  '[.projects[0].agents[]|select(.id=="'"$QID"'")] | length==1 and .[0].stop==null'
api POST /agents/$QID/finished '{"reason":"conformance done"}' >/dev/null
api POST /tasks/$FT/release "{\"agent\":\"$FID\"}" >/dev/null
api POST /tasks/$FT/done "{\"agent\":\"$FID\",\"no_merge\":true}" >/dev/null
api POST /agents/$FID/finished '{"reason":"conformance done"}' >/dev/null

SA=$(api GET '/status?project=otherproject')
check "a project with no budget line has no ceiling — stop, not free rein" "$SA" \
  '.projects[0].budget.ceilings=={}'
# ...but the stop has to say WHY. Otherwise the fleet stops with no reason given on the day
# board.py is deployed before the line has been added to board-policy.json.
check "a missing ceiling is flagged, saying what the human has to do" "$SA" \
  '.projects[0].budget.ceilings_missing==true and (.projects[0].budget.note|test("board-policy.json"))'
api POST /agents '{"project":"zerobudget","harness":"claude-code","host":"host-b","session":"s4"}' >/dev/null
check "a ceiling deliberately set to 0 is NOT «missing»" "$(api GET '/status?project=zerobudget')" \
  '.projects[0].budget.ceilings=={"7d":0} and (.projects[0].budget|has("ceilings_missing")|not)'
# The live board-policy.json is hand-edited on the host and never deployed. If the old
# rl7_ceiling shape were lost here, the rollout would have made the ceiling unknown to the
# entire fleet.
api POST /agents '{"project":"oldshape","harness":"claude-code","host":"host-b","session":"s6"}' >/dev/null
check "an old rl7_ceiling in the policy is translated to ceilings.7d" \
  "$(api GET '/status?project=oldshape')" \
  '.projects[0].budget.ceilings["7d"]==60 and (.projects[0].budget|has("ceilings_missing")|not)'

echo "== tasks =="
T=$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"repo\":\"web\",\"title\":\"a button\",\"requires\":[\"browser-test\"],\"needs_grants\":[\"merge\"],\"touches\":[\"src/a\"],\"risk\":\"high\",\"priority\":90}")
TID=$(jq -r .id <<<"$T"); check "task create" "$T" '.id and .status=="open"'
check "task next matches capabilities" "$(api GET "/tasks/next?agent=$AID")" ".id==\"$TID\""
# The right assertion: codex does not get THIS task. That the queue is entirely empty is an
# assumption about an empty system, and the suite must be able to run against a board with
# other work on it.
check "task next hides what the agent lacks the grant for" "$(api GET "/tasks/next?agent=$BID")" \
  ".id != \"$TID\""
check "claim" "$(api POST /tasks/$TID/claim "{\"agent\":\"$AID\"}")" ".owner==\"$AID\" and .lease_until"
check "a double claim gives a 409" "$(api POST /tasks/$TID/claim "{\"agent\":\"$BID\"}")" '.error'
check "progress" "$(api POST /tasks/$TID/progress "{\"agent\":\"$AID\",\"worktree\":\"/wt/x\",\"branch\":\"t/$TID\",\"pr\":\"web#201\",\"note\":\"draft\"}")" '.ok'
# Dispatch-loggen: rolle:modell, validert. Halvstrukturert logg er verre enn ingen.
api POST /tasks/$TID/progress "{\"agent\":\"$AID\",\"dispatch\":\"implementer:sonnet\"}" >/dev/null
api POST /tasks/$TID/progress "{\"agent\":\"$AID\",\"dispatch\":\"reviewer:opus\",\"result\":\"3 findings\"}" >/dev/null
check "a dispatch without tokens yields an incomplete sum" "$(api GET /tasks/$TID)" \
  '.cost.complete==false and .cost.dispatches==2 and .cost.tokens==0'
check "the dispatch chain is on the task, in order" "$(api GET /tasks/$TID)" \
  '(.dispatches|length)==2 and .dispatches[0].role=="implementer" and .dispatches[0].model=="sonnet"
   and .dispatches[1].model=="opus" and .dispatches[1].result=="3 findings"'
api POST /tasks/$TID/progress "{\"agent\":\"$AID\",\"dispatch\":\"implementer:sonnet\",\"tokens\":100}" >/dev/null
api POST /tasks/$TID/progress "{\"agent\":\"$AID\",\"dispatch\":\"reviewer:opus\",\"tokens\":25}" >/dev/null
check "cost sums tokens per model" "$(api GET /tasks/$TID)" \
  '.cost.tokens==125 and .cost.by_model.sonnet==100 and .cost.by_model.opus==25'
# SKILL.md asks for a log both before and after the same dispatch. That is still TWO
# dispatches, not four — and with the token count in place the sum is complete.
check "logging before and after counts one dispatch, not two" "$(api GET /tasks/$TID)" \
  '.cost.dispatches==2 and .cost.complete==true and (.dispatches|length)==2
   and .dispatches[0].tokens==100 and .dispatches[1].result=="3 findings"'
# ...men en NY runde med samme rolle:modell er en ny dispatch, ikke et sent tokentall.
api POST /tasks/$TID/progress "{\"agent\":\"$AID\",\"dispatch\":\"implementer:sonnet\"}" >/dev/null
check "a new round with the same role:model is a new dispatch" "$(api GET /tasks/$TID)" \
  '.cost.dispatches==3 and .cost.complete==false and .cost.tokens==125'
api POST /tasks/$TID/progress "{\"agent\":\"$AID\",\"dispatch\":\"implementer:sonnet\",\"tokens\":7}" >/dev/null
check "and round two is closed by its own token count" "$(api GET /tasks/$TID)" \
  '.cost.dispatches==3 and .cost.complete==true and .cost.by_model.sonnet==107'
check "tokens without a dispatch are refused" \
  "$(api POST /tasks/$TID/progress "{\"agent\":\"$AID\",\"tokens\":5}")" '.error'
check "tokens that are not a number are refused" \
  "$(api POST /tasks/$TID/progress "{\"agent\":\"$AID\",\"dispatch\":\"lookup:haiku\",\"tokens\":\"mye\"}")" '.error'
check "a malformed dispatch is refused" \
  "$(api POST /tasks/$TID/progress "{\"agent\":\"$AID\",\"dispatch\":\"sonnet\"}")" '.error'
check "an unknown dispatch role is refused" \
  "$(api POST /tasks/$TID/progress "{\"agent\":\"$AID\",\"dispatch\":\"koordinator:opus\"}")" '.error'
# Reviewen kommer fra BID, ikke eieren AID: en fersk agent er hele poenget med steg 8.
check "review result" "$(api POST /tasks/$TID/review "{\"agent\":\"$BID\",\"open\":0,\"fixed\":2}")" '.ok'
check "PATCH repo and priority" "$(api PATCH /tasks/$TID "{\"agent\":\"$AID\",\"repo\":\"web2\",\"priority\":10}")" \
  '.repo=="web2" and .priority==10'
check "PATCH touches and risk" "$(api PATCH /tasks/$TID "{\"agent\":\"$AID\",\"touches\":[\"src/b\"],\"risk\":\"low\"}")" \
  '.risk=="low" and (.touches|fromjson)==["src/b"]'
# Restore what the PATCH tests changed: later checks verify the merge in the repo, and
# `web2` does not exist on disk.
api PATCH /tasks/$TID "{\"agent\":\"$AID\",\"risk\":\"high\",\"repo\":\"web\"}" >/dev/null
check "PATCH refuses status" "$(api PATCH /tasks/$TID "{\"agent\":\"$AID\",\"status\":\"done\"}")" '.error'
check "PATCH refuses owner" "$(api PATCH /tasks/$TID "{\"agent\":\"$AID\",\"owner\":\"x\"}")" '.error'
PAT=$(api POST /agents '{"project":"demo","harness":"codex","host":"h","session":"patchtest","capabilities":[]}' | jq -r .id)
check "PATCH is refused from an agent that does not own the task" \
  "$(api PATCH /tasks/$TID "{\"agent\":\"$PAT\",\"risk\":\"low\"}")" '.error'
check "and the risk is unchanged after the attempt" "$(api GET "/tasks/$TID")" '.risk=="high"'
api POST "/agents/$PAT/finished" '{"reason":"conformance"}' >/dev/null
check "PATCH refuses merge_sha" "$(api PATCH /tasks/$TID "{\"agent\":\"$AID\",\"merge_sha\":\"abc\"}")" '.error'
check "task show has a timeline and owner state" "$(api GET /tasks/$TID)" \
  '(.events|length>0) and ((.events|map(.type)|index("task.progress")) != null) and .owner_status and .phase'

if [ "$OWN_SERVER" = 1 ]; then
echo "== gate + human test (phase launch, risk=high) =="
G=$(api GET "/tasks/$TID/gate/merge?agent=$AID")
# DESIGN.md §8: the gate was "only an echo of the owner's word". A coordinator once set a
# review result on its OWN task and the gate let it through.
SELFR=$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"repo\":\"web\",\"title\":\"self review\"}" | jq -r .id)
api POST "/tasks/$SELFR/claim" "{\"agent\":\"$AID\"}" >/dev/null
api POST "/tasks/$SELFR/review" "{\"agent\":\"$AID\",\"open\":0}" >/dev/null
check "the owner cannot review its own task past the gate" \
  "$(api GET "/tasks/$SELFR/gate/merge?agent=$AID")" \
  '.ok==false and (.reasons|join(" ")|test("set by the owner itself"))'
api POST "/tasks/$SELFR/review" "{\"agent\":\"$BID\",\"open\":0}" >/dev/null
check "another agent's review opens it" \
  "$(api GET "/tasks/$SELFR/gate/merge?agent=$AID")" \
  '(.reasons//[]|join(" ")|test("set by the owner itself"))|not'
api POST "/tasks/$SELFR/done" "{\"agent\":\"$AID\",\"no_merge\":true}" >/dev/null

check "the gate is closed without a human-OK" "$G" '.ok==false and (.reasons|length>0)'
cat > "$TMP/card.json" <<JSON
{"project":"demo","repo":"web","env":"dev","url":"https://dev.example.com/x","login":"e2e-owner",
 "steps":["Open the tab"],"expected":["The new button appears"],"risk":"normal","rollback":"kubectl rollout undo deploy/web"}
JSON
check "an invalid test card is refused" "$(api POST /questions "{\"agent\":\"$AID\",\"project\":\"demo\",\"kind\":\"test\",\"task\":\"$TID\",\"card\":{\"repo\":\"web\"}}")" '.error'
QT=$(api POST /questions "$(jq -c --arg a "$AID" --arg t "$TID" --slurpfile c "$TMP/card.json" \
      '{agent:$a,project:"demo",kind:"test",task:$t,card:$c[0]}' <<<'{}')")
QTID=$(jq -r .id <<<"$QT"); check "testkort godtatt" "$QT" '.id'
check "the test queue spans projects" "$(api GET '/questions?kind=test&status=open')" ".questions[0].id==\"$QTID\""
check "the human answers OK" "$(hum POST /questions/$QTID/answer '{"answer":"ok","by":"human"}')" '.answer=="ok"'
check "the gate opens after a human-OK" "$(api GET "/tasks/$TID/gate/merge?agent=$AID")" '.ok==true'
# T-143: an agent answer under its OWN id must not open the gate. human_only only closed
# "claiming to be the human"; the gate read the column without caring who set it.
# Its own task: a new test card clears human_test on purpose, so it cannot share a task with
# the check above.
SFT=$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"repo\":\"web\",\"title\":\"self answer\",\"risk\":\"high\"}" | jq -r .id)
api POST "/tasks/$SFT/claim" "{\"agent\":\"$AID\"}" >/dev/null
api POST "/tasks/$SFT/review" "{\"agent\":\"$BID\",\"open\":0}" >/dev/null
QSELF=$(api POST /questions "$(jq -c --arg a "$AID" --arg t "$SFT" --slurpfile c "$TMP/card.json" \
        '{agent:$a,project:"demo",kind:"test",task:$t,card:$c[0]}' <<<'{}')" | jq -r .id)
api POST "/questions/$QSELF/answer" "{\"answer\":\"ok\",\"by\":\"$AID\"}" >/dev/null
check "the agent's own test answer does not count as a human-OK" "$(api GET "/tasks/$SFT")" \
  '.human_test != "ok"'
check "and the gate is still closed after the self-answer" \
  "$(api GET "/tasks/$SFT/gate/merge?agent=$AID")" \
  '.ok==false and (.reasons|join(" ")|test("human-OK"))'
QSELF2=$(api POST /questions "$(jq -c --arg a "$AID" --arg t "$SFT" --slurpfile c "$TMP/card.json" \
         '{agent:$a,project:"demo",kind:"test",task:$t,card:$c[0]}' <<<'{}')" | jq -r .id)
check "a human answer on a new card opens it" \
  "$(hum POST "/questions/$QSELF2/answer" '{"answer":"ok","by":"human"}' >/dev/null; api GET "/tasks/$SFT")" \
  '.human_test=="ok"'
api POST "/tasks/$SFT/release" "{\"agent\":\"$AID\"}" >/dev/null
api POST "/tasks/$SFT/done" "{\"agent\":\"$AID\",\"no_merge\":true}" >/dev/null

check "the gate refuses an agent without the merge grant" "$(api GET "/tasks/$TID/gate/merge?agent=$BID")" '.ok==false'

echo "== merge, questions, messages =="
check "merge_requested" "$(api POST /tasks/$TID/merge_requested "{\"agent\":\"$AID\"}")" '.ok'
# merge mutex: while TID sits in `merging`, no other task may pass the gate
LK=$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"title\":\"lock test\"}" | jq -r .id)
api POST "/tasks/$LK/claim" "{\"agent\":\"$AID\"}" >/dev/null
api POST "/tasks/$LK/review" "{\"agent\":\"$BID\",\"open\":0}" >/dev/null
# The lock is released once the merge HAS landed: what remains (deploy, test card, done)
# does not touch anyone else's branches, and done can wait on a human for hours.
api POST "/tasks/$TID/merge_verified" "{\"agent\":\"$AID\",\"sha\":\"deadbee\"}" >/dev/null
check "a merged task does not hold the lock while it waits for a test card" \
  "$(api GET "/tasks/$LK/gate/merge?agent=$AID")" \
  '(.reasons//[]|join(" ")|test("is merging right now"))|not'
python3 - "$TMP/board.db" "$TID" <<'RESETPY'
import sqlite3, sys
d = sqlite3.connect(sys.argv[1], timeout=5)
d.execute("UPDATE tasks SET merge_sha=NULL, status='merging' WHERE id=?", (sys.argv[2],))
d.commit()
RESETPY
check "the gate closes for another merge while one is in progress" \
  "$(api GET "/tasks/$LK/gate/merge?agent=$AID")" '.ok==false and (.reasons|join(" ")|test("is merging right now"))'
api POST "/tasks/$LK/done" "{\"agent\":\"$AID\",\"no_merge\":true}" >/dev/null   # do not release it: an unowned in_review would sort ahead of the next test
check "merge_verified" "$(api POST /tasks/$TID/merge_verified "{\"agent\":\"$AID\",\"sha\":\"3f2a1b9c4d5e6f708192a3b4c5d6e7f809a1b2c3\"}")" '.ok'
# An aborted merge could be reported as complete. The board cannot know whether the sha is
# on main (it has no working copy — bin/board checks that against origin/main), but it must
# refuse to store something that is NOT a sha (T-195).
check "merge_verified refuses something that is not a sha" \
  "$(api POST /tasks/$TID/merge_verified "{\"agent\":\"$AID\",\"sha\":\"merged ok\"}")" '.error'
check "merge_verified refuses an empty sha" \
  "$(api POST /tasks/$TID/merge_verified "{\"agent\":\"$AID\"}")" '.error'
check "done" "$(api POST /tasks/$TID/done "{\"agent\":\"$AID\"}")" '.ok'
check "archive" "$(api POST /tasks/$TID/archive "{\"agent\":\"$AID\",\"note\":\"arkivert\"}")" '.ok'
check "an archived task is hidden from an ordinary task list" "$(api GET '/tasks?project=demo')" "all(.tasks[]; .id != \"$TID\")"
check "an archived task is found with status=archived" "$(api GET '/tasks?project=demo&status=archived')" "any(.tasks[]; .id == \"$TID\")"
check "an archived task is found with all=1" "$(api GET '/tasks?project=demo&all=1')" "any(.tasks[]; .id == \"$TID\")"
Q=$(api POST /questions "{\"agent\":\"$AID\",\"project\":\"demo\",\"text\":\"A eller B?\",\"options\":[\"A\",\"B\"],\"default\":\"B\",\"deadline\":\"2026-09-07T08:00:00Z\"}")
QID=$(jq -r .id <<<"$Q"); check "ask returnerer umiddelbart" "$Q" '.id and .status=="open"'
check "answer" "$(hum POST /questions/$QID/answer '{"answer":"A","by":"human"}')" '.answer=="A"'
IN=$(api GET "/agents/$AID/inbox")
check "the inbox delivers the answer" "$IN" 'any(.questions[]; .answer=="A")'
check "the inbox empties after reading" "$(api GET "/agents/$AID/inbox")" '.questions|length==0'
check "message agent→agent" "$(api POST /messages "{\"agent\":\"$AID\",\"to\":\"$BID\",\"project\":\"demo\",\"text\":\"regenerer typer\"}")" '.ok'
check "the message is in the recipient's inbox" "$(api GET "/agents/$BID/inbox")" '.messages[0].text=="regenerer typer"'
fi

echo "== append-only progress on done/archived (T-396) =="
# A subagent archived; the coordinator's --tokens then 409'd because owner is NULL.
# History cannot be amended, cost.complete stays false. The append is event-only.
AT=$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"title\":\"late tokens after archive\"}" | jq -r .id)
api POST /tasks/$AT/claim "{\"agent\":\"$AID\"}" >/dev/null
api POST /tasks/$AT/archive "{\"agent\":\"$AID\",\"note\":\"subagent archived\"}" >/dev/null
ARCH=$(api GET /tasks/$AT)
ARCH_UPD=$(jq -r .updated <<<"$ARCH")
sleep 1
# A different agent in the same project — the coordinator, not the last owner.
APP=$(apic POST /tasks/$AT/progress "{\"agent\":\"$BID\",\"dispatch\":\"tester:sonnet\",\"tokens\":98512,\"result\":\"x\"}")
check "append-only progress on an archived task is 200" "$APP" '.code==200 and .body.ok==true'
SHOW=$(api GET /tasks/$AT)
check "board task show returns the dispatch and cost includes the tokens" "$SHOW" \
  '.status=="archived" and (.dispatches|length)==1
   and .dispatches[0].role=="tester" and .dispatches[0].model=="sonnet"
   and .dispatches[0].tokens==98512 and .dispatches[0].result=="x"
   and .dispatches[0].actor=="'"$BID"'"
   and .cost.tokens==98512 and .cost.complete==true'
[ "$(jq -r .updated <<<"$SHOW")" = "$ARCH_UPD" ] && [ "$(jq -r .status <<<"$SHOW")" = archived ] \
  && ok "archived row status/updated are unchanged by the append" \
  || no "archived row status/updated are unchanged by the append" "$SHOW"
WT=$(apic POST /tasks/$AT/progress "{\"agent\":\"$BID\",\"worktree\":\"/wt/no\"}")
check "progress --worktree on an archived task is 400" "$WT" '.code==400 and .body.error'
SHOW2=$(api GET /tasks/$AT)
check "the archived row is untouched after the refused worktree" "$SHOW2" \
  '.status=="archived" and .worktree==null and .updated=="'"$ARCH_UPD"'"'

DT396=$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"title\":\"late tokens after done\"}" | jq -r .id)
api POST /tasks/$DT396/claim "{\"agent\":\"$AID\"}" >/dev/null
api POST /tasks/$DT396/done "{\"agent\":\"$AID\",\"no_merge\":true}" >/dev/null
DONE_UPD=$(jq -r .updated <<<"$(api GET /tasks/$DT396)")
sleep 1
DAPP=$(apic POST /tasks/$DT396/progress "{\"agent\":\"$BID\",\"dispatch\":\"tester:sonnet\",\"tokens\":7,\"result\":\"x\"}")
check "append-only progress on a done task is 200" "$DAPP" '.code==200 and .body.ok==true'
DSHOW=$(api GET /tasks/$DT396)
check "done: dispatch lands, status/updated unchanged" "$DSHOW" \
  '.status=="done" and .updated=="'"$DONE_UPD"'" and .cost.tokens==7
   and .dispatches[0].role=="tester" and .dispatches[0].tokens==7'

if [ "$OWN_SERVER" = 1 ]; then
echo "== a defaulted question can still be answered by a human (T-197) =="
# A deadline in the past: the reaper must sweep this to 'defaulted' before any human can answer.
QD=$(api POST /questions "{\"agent\":\"$AID\",\"project\":\"demo\",\"text\":\"will we make the deadline?\",\"default\":\"no\",\"deadline\":\"2020-01-01T00:00:00Z\"}")
QDID=$(jq -r .id <<<"$QD")
# T-198: TWO more questions with the same missed deadline — one on a task that manages to
# finish on the default, one on a task with a living owner. They default in the same reap cycle.
DT=$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"repo\":\"web\",\"title\":\"built on the guess\"}" | jq -r .id)
api POST /tasks/$DT/claim "{\"agent\":\"$AID\"}" >/dev/null
QT=$(api POST /questions "{\"agent\":\"$AID\",\"project\":\"demo\",\"task\":\"$DT\",\"text\":\"unlimited?\",\"default\":\"no\",\"deadline\":\"2020-01-01T00:00:00Z\"}" | jq -r .id)
api POST /tasks/$DT/done "{\"agent\":\"$AID\",\"no_merge\":true}" >/dev/null
LT=$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"repo\":\"web\",\"title\":\"still alive\"}" | jq -r .id)
api POST /tasks/$LT/claim "{\"agent\":\"$AID\"}" >/dev/null
QL=$(api POST /questions "{\"agent\":\"$AID\",\"project\":\"demo\",\"task\":\"$LT\",\"text\":\"and this one?\",\"default\":\"no\",\"deadline\":\"2020-01-01T00:00:00Z\"}" | jq -r .id)
# Poll observed state instead of sleeping a fixed number of seconds (the T-184 flake).
# BOARD_REAP_INTERVAL=1 on our own instance keeps this quick; against a real board (60s
# cycle) the 75 attempts still fit inside the time budget.
DEFAULTED=0
for _ in $(seq 75); do
  jq -e '.events[]|select(.type=="question.defaulted")' >/dev/null 2>&1 \
    <<<"$(api GET "/events?stream=question/$QDID")" && { DEFAULTED=1; break; }
  sleep 1
done
[ "$DEFAULTED" = 1 ] && ok "the reaper swept the overdue question to defaulted" \
  || no "the reaper swept the overdue question to defaulted" "never defaulted after 75s"
check "a defaulted question is visible in /status, not gone" \
  "$(api GET '/status?project=demo')" \
  "[.projects[0].questions_defaulted[]|select(.id==\"$QDID\")]|length==1"
check "a defaulted question is kept apart from open ones in /status" \
  "$(api GET '/status?project=demo')" \
  "[.projects[0].questions[]|select(.id==\"$QDID\")]|length==0"
check "a human can answer a defaulted question (not 409)" \
  "$(hum POST /questions/$QDID/answer '{"answer":"yes","by":"human"}')" '.answer=="yes" and (.error|not)'
check "the answer overrides the default visibly in the event log" "$(api GET "/events?stream=question/$QDID")" \
  '[.events[]|select(.type=="question.answered")][0].body.overrode_default=="no"'
# SQLite's LOWER() is ASCII-only: LOWER('ÅPEN') gives 'Åpen', not 'åpen'. A non-ASCII
# default answer is then read as a confirmation in question_answer (Python .lower()) and as
# an OVERRIDE in the inbox query — the coordinator is spammed with an answer that merely
# confirmed the default. `ulower` is Python's lower() registered as a SQL function: one
# normalization.
UL=$(python3 - "$TMP/board.db" <<'ULPY'
import sqlite3, sys
d = sqlite3.connect(sys.argv[1])
d.create_function("ulower", 1, lambda v: v.lower() if isinstance(v, str) else v)
print(d.execute("SELECT ulower('ÅPEN')='åpen', LOWER('ÅPEN')='åpen'").fetchone())
ULPY
)
check "ulower normalizes non-ASCII where SQLite's LOWER does not" "$(jq -nc --arg u "$UL" '{u:$u}')" \
  '.u|test("\\(1, 0\\)")'

check "a question already answered by a human is still 409 on a new answer" \
  "$(hum POST /questions/$QDID/answer '{"answer":"no really","by":"human"}')" '.error and (.error|test("already"))'

# T-198: the audit trail was right, but the loop did not close. A human who changes their
# mind must reach the WORK that was done on the guess — otherwise the board "knows" yes
# while the merged code does no, and that is only visible by reading raw events.
for Q in "$QT" "$QL"; do
  for _ in $(seq 75); do
    jq -e '.events[]|select(.type=="question.defaulted")' >/dev/null 2>&1 \
      <<<"$(api GET "/events?stream=question/$Q")" && break
    sleep 1
  done
done
hum POST /questions/$QT/answer '{"answer":"yes","by":"human"}' >/dev/null
check "an overridden default on a DONE task becomes a new task in the queue" \
  "$(api GET '/tasks?project=demo')" \
  "[.tasks[]|select(.title|test(\"overridden default on $DT\"))]|length==1"
check "…and the task's event log shows the override where the work lives" \
  "$(api GET "/events?stream=task/$DT")" \
  '[.events[]|select(.type=="task.default_overridden")]|length==1'
# The task has a LIVING owner — the coordinator fallback was scoped to unowned and orphaned
# ones, so precisely this case fell outside it. $BID is not the one that asked.
hum POST /questions/$QL/answer '{"answer":"yes","by":"human"}' >/dev/null
check "an overridden default reaches the coordinator even when the task has a living owner" \
  "$(api GET "/agents/$BID/inbox?as=coordinator")" \
  "[.questions[]|select(.id==\"$QL\")]|length==1"
# Delivery to the coordinator must happen ONCE. Without the read gate the same row came
# back in every single poll until the task went done.
check "…but only once — not in every single coordinator poll" \
  "$(api GET "/agents/$BID/inbox?as=coordinator")" \
  "[.questions[]|select(.id==\"$QL\")]|length==0"
# A default that was NOT overridden must not make noise — it was followed, after all.
check "an answer that confirms the default creates no follow-up task" \
  "$(api GET '/tasks?project=demo')" \
  '[.tasks[]|select(.title|test("overridden default"))]|length==1'
# "Yes" with a capital letter in the free-text field is CONFIRMING the default "yes", not
# overriding it.
CT2=$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"repo\":\"web\",\"title\":\"confirmed default\"}" | jq -r .id)
api POST /tasks/$CT2/claim "{\"agent\":\"$AID\"}" >/dev/null
QC=$(api POST /questions "{\"agent\":\"$AID\",\"project\":\"demo\",\"task\":\"$CT2\",\"text\":\"same?\",\"default\":\"yes\",\"deadline\":\"2020-01-01T00:00:00Z\"}" | jq -r .id)
api POST /tasks/$CT2/done "{\"agent\":\"$AID\",\"no_merge\":true}" >/dev/null
for _ in $(seq 75); do
  jq -e '.events[]|select(.type=="question.defaulted")' >/dev/null 2>&1 \
    <<<"$(api GET "/events?stream=question/$QC")" && break
  sleep 1
done
hum POST /questions/$QC/answer '{"answer":"Yes ","by":"human"}' >/dev/null
check "«Yes » confirms the default «yes» — not an override" \
  "$(api GET '/tasks?project=demo')" \
  "[.tasks[]|select(.title|test(\"overridden default on $CT2\"))]|length==0"
fi

echo "== roles =="
check "the recommendation is deterministic" "$(api GET '/roles/recommendation?project=demo')" ".recommended==\"$AID\" and .reason"
check "an agent without the capabilities is refused the role" "$(api POST /roles/coordinator/claim "{\"project\":\"demo\",\"agent\":\"$BID\"}")" '.error'
if [ "$OWN_SERVER" = 1 ]; then
check "claim" "$(api POST /roles/coordinator/claim "{\"project\":\"demo\",\"agent\":\"$AID\"}")" '.agent=="'"$AID"'"'
fi
check "a pin wins" "$(hum PUT /roles/coordinator "{\"project\":\"demo\",\"agent\":\"$AID\"}")" '.source=="pinned"'
check "unpin" "$(hum DELETE '/roles/coordinator?project=demo')" '.ok'

echo "== status, events, release =="
check "status" "$(api GET /status)" '[.projects[]|select(.name=="demo")]|length==1 and (.[0].agents|length>=2)'
check "status per project" "$(api GET '/status?project=demo')" '(.projects|length)==1 and .projects[0].name=="demo"'
check "event log" "$(api GET '/events?since=0')" '(.events|length>10) and (.events|map(.type)|index("task.claimed"))'
T2=$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"title\":\"for release\"}"); T2ID=$(jq -r .id <<<"$T2")
api POST /tasks/$T2ID/claim "{\"agent\":\"$AID\"}" >/dev/null
check "release keeps the context" "$(api POST /tasks/$T2ID/release "{\"agent\":\"$AID\",\"note\":\"out of tokens\"}")" '.ok'
# The implementer→coordinator hand-off: in_review + release, and ANOTHER agent must get it
# from task next and be able to claim it without losing the in_review status.
T3ID=$(jq -r .id <<<"$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"title\":\"for review\",\"priority\":1}")")
api POST /tasks/$T3ID/claim "{\"agent\":\"$AID\"}" >/dev/null
api POST /tasks/$T3ID/progress "{\"agent\":\"$AID\",\"status\":\"in_review\",\"pr\":\"http://pr/1\"}" >/dev/null
check "releasing an in_review keeps the status" \
  "$(api POST /tasks/$T3ID/release "{\"agent\":\"$AID\",\"note\":\"ready for review\"}"; api GET /tasks/$T3ID)" \
  '.status=="in_review" and .owner==null'
check "task next offers handed-off work" "$(api GET "/tasks/next?agent=$BID")" ".id==\"$T3ID\""
check "another agent can claim it, and it does not become 'claimed'" \
  "$(api POST /tasks/$T3ID/claim "{\"agent\":\"$BID\"}")" '.owner=="'"$BID"'" and .pr=="http://pr/1"'
check "the status is still in_review after the claim" "$(api GET /tasks/$T3ID)" '.status=="in_review"'
api POST /tasks/$T3ID/release "{\"agent\":\"$BID\"}" >/dev/null

# `awaiting_human` waits on a human, not on an agent. If it loses that status, finished
# work comes back out of `task next` as new work, and the next agent re-implements it.
T4ID=$(jq -r .id <<<"$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"repo\":\"web\",\"title\":\"waiting on the human\",\"risk\":\"high\"}")")
api POST /tasks/$T4ID/claim "{\"agent\":\"$AID\"}" >/dev/null
api POST /questions "$(jq -c --arg a "$AID" --arg t "$T4ID" --slurpfile c "$TMP/card.json" \
  '{agent:$a,project:"demo",kind:"test",task:$t,card:$c[0]}' <<<'{}')" >/dev/null
check "a test card sets the task to awaiting_human" "$(api GET /tasks/$T4ID)" '.status=="awaiting_human"'
check "releasing an awaiting_human keeps the status" \
  "$(api POST /tasks/$T4ID/release "{\"agent\":\"$AID\",\"note\":\"out of context\"}"; api GET /tasks/$T4ID)" \
  '.status=="awaiting_human" and .owner==null'
check "task next does not offer work that is waiting on a human" "$(api GET "/tasks/next?agent=$BID")" \
  '.id != "'"$T4ID"'"'
# The answer on the card must deliver the task back to the queue. `claimed` without an owner
# is invisible to `task next` and untouchable until the lease expires — finished work would
# have been lost there.
QT4=$(jq -r '.questions[]|select(.task=="'"$T4ID"'")|.id' <<<"$(api GET '/questions?kind=test&status=open')")
hum POST /questions/$QT4/answer '{"answer":"ok","by":"human"}' >/dev/null
check "answering a test card returns an unowned task to the queue" "$(api GET /tasks/$T4ID)" \
  '.status=="open" and .human_test=="ok"'

if [ "$OWN_SERVER" = 1 ]; then
check "blocked notifies" "$(api POST /tasks/$T2ID/blocked "{\"agent\":\"$AID\",\"note\":\"classifier refused\"}")" '.ok'
fi
check "project isolation (403)" "$(api POST /agents '{"project":"otherproject","harness":"grok","host":"mac","session":"s3"}' >/dev/null; api POST /tasks/$TID/claim "{\"agent\":\"$(api POST /agents '{"project":"otherproject","harness":"grok","host":"mac","session":"s3"}' | jq -r .id)\"}")" '.error'
check "finished releases everything" "$(api POST /agents/$AID/finished '{"reason":"queue empty"}')" '.ok'
api POST "/agents/$BID/finished" '{"reason":"conformance"}' >/dev/null

echo "== lease: heartbeat is life, task.progress is progress (Q-107) =="
C=$(api POST /agents '{"project":"demo","harness":"claude-code","host":"host-a","session":"s4"}')
CID=$(jq -r .id <<<"$C")
E=$(api POST /agents '{"project":"demo","harness":"claude-code","host":"host-a","session":"s5"}')
EID=$(jq -r .id <<<"$E")
T3ID=$(api POST /tasks "{\"agent\":\"$CID\",\"project\":\"demo\",\"title\":\"lease\"}" | jq -r .id)
L0=$(api POST /tasks/$T3ID/claim "{\"agent\":\"$CID\"}" | jq -r .lease_until)
sleep 1; api POST /agents/$CID/heartbeat '{"ctx_pct":5}' >/dev/null
L1=$(api GET /tasks/$T3ID | jq -r .lease_until)
[ "$L1" = "$L0" ] && ok "the heartbeat does not renew the lease" \
  || no "the heartbeat does not renew the lease" "$L0 → $L1"
sleep 1; api POST /tasks/$T3ID/progress "{\"agent\":\"$CID\",\"note\":\"jobber\"}" >/dev/null
L3=$(api GET /tasks/$T3ID | jq -r .lease_until)
[ "$L3" \> "$L1" ] && ok "task.progress renews the lease" || no "task.progress renews the lease" "$L1 → $L3"

# The rest needs expired leases and dead agents. The suite cannot wait half an hour, and the
# board has no time machine in the API, so we set the timestamps in the database directly.
# Against a foreign board (BOARD_URL set) we skip it.
if [ "$OWN_SERVER" = 1 ]; then
  sql() { python3 -c 'import sqlite3,sys
d=sqlite3.connect(sys.argv[1],timeout=5); d.execute(sys.argv[2]); d.commit()' "$TMP/board.db" "$1"; }
  PAST=2020-01-01T00:00:00Z
  sql "UPDATE tasks SET lease_until='$PAST' WHERE id='$T3ID'"

  check "an expired lease is stalled despite a living heartbeat" "$(api GET '/status?project=demo')" \
    '[.projects[0].agents[]|select(.id=="'"$CID"'")][0].status=="stalled"'
  check "a stalled agent gets no new task" "$(api GET "/tasks/next?agent=$CID")" '.error'

  # A pinned role is the human's choice. `stalled` is a new status value, and any
  # enumeration of statuses that does not know it steals the role from a living holder.
  hum PUT /roles/coordinator "{\"project\":\"demo\",\"agent\":\"$CID\"}" >/dev/null
  check "a pinned role is not stolen from a stalled holder" \
    "$(api POST /roles/coordinator/claim "{\"project\":\"demo\",\"agent\":\"$EID\"}")" '.error'
  hum DELETE '/roles/coordinator?project=demo' >/dev/null

  api POST /reap '{}' >/dev/null
  check "the reaper orphans despite a living heartbeat" "$(api GET /tasks/$T3ID)" \
    '.status=="orphaned" and .owner==null'
  # Without this, progress still answers ok and writes the worktree back onto the orphaned
  # row — and the next claimant is handed the same worktree.
  check "a reaped owner gets an error on task.progress" \
    "$(api POST /tasks/$T3ID/progress "{\"agent\":\"$CID\",\"worktree\":\"/wt/ghost\"}")" '.error'
  # T-396: done/archived accept a late --tokens; orphaned must not — that row is waiting
  # to be claimed, not amended.
  check "dispatch+tokens on an orphaned task is still 409" \
    "$(apic POST /tasks/$T3ID/progress "{\"agent\":\"$CID\",\"dispatch\":\"tester:sonnet\",\"tokens\":1,\"result\":\"x\"}")" \
    '.code==409 and (.body.error|test("no longer yours")) and .body.status=="orphaned"'

  echo "== the reaper tells waiting apart from stopping =="
  T5ID=$(api POST /tasks "{\"agent\":\"$EID\",\"project\":\"demo\",\"repo\":\"web\",\"title\":\"waiting on a human\",\"risk\":\"high\"}" | jq -r .id)
  api POST /tasks/$T5ID/claim "{\"agent\":\"$EID\"}" >/dev/null
  Q5=$(api POST /questions "$(jq -c --arg a "$EID" --arg t "$T5ID" --slurpfile c "$TMP/card.json" \
        '{agent:$a,project:"demo",kind:"test",task:$t,card:$c[0]}' <<<'{}')" | jq -r .id)
  T6ID=$(api POST /tasks "{\"agent\":\"$EID\",\"project\":\"demo\",\"title\":\"blocked\"}" | jq -r .id)
  api POST /tasks/$T6ID/claim "{\"agent\":\"$EID\"}" >/dev/null
  api POST /tasks/$T6ID/blocked "{\"agent\":\"$EID\",\"note\":\"waiting for an answer\"}" >/dev/null
  sql "UPDATE tasks SET lease_until='$PAST' WHERE id IN ('$T5ID','$T6ID')"
  api POST /reap '{}' >/dev/null
  check "the reaper does not orphan awaiting_human" "$(api GET /tasks/$T5ID)" \
    '.status=="awaiting_human" and .owner=="'"$EID"'"'
  # A row can end up unowned in an owner state (the agent released ownership, but not the
  # status). Claim then refuses on status and everything else on ownership: wedged forever.
  STK=$(api POST /tasks "{\"agent\":\"$EID\",\"project\":\"demo\",\"title\":\"wedged\"}" | jq -r .id)
  sql "UPDATE tasks SET status='claimed', owner=NULL, lease_until=NULL WHERE id='$STK'"
  api POST /reap '{}' >/dev/null
  check "the reaper frees an unowned claimed — otherwise the row is wedged forever" \
    "$(api GET "/tasks/$STK")" '.status=="orphaned"'
  # Tidy up: an orphaned row sorts first in task next and would have failed later checks.
  sql "UPDATE tasks SET status='done' WHERE id='$STK'"
  check "the reaper does not orphan blocked" "$(api GET /tasks/$T6ID)" '.status=="blocked"'

  # skill/reference/testing.md promises: "OK → the task comes back to you as claimed".
  # Without an owner and a lease the row is invisible to both task_next and task_claim.
  NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  hum POST /questions/$Q5/answer '{"answer":"ok","by":"human"}' >/dev/null
  check "a test answer returns the task to its owner with a fresh lease" "$(api GET /tasks/$T5ID)" \
    '.status=="claimed" and .owner=="'"$EID"'" and .lease_until > "'"$NOW"'"'

  # The same answer when the owner has been reaped away: the task must NOT become claimed
  # without an owner, and the human-OK must not carry over to a new agent that never filed
  # the card.
  T7ID=$(api POST /tasks "{\"agent\":\"$CID\",\"project\":\"demo\",\"repo\":\"web\",\"title\":\"reaped during test\",\"risk\":\"high\"}" | jq -r .id)
  api POST /tasks/$T7ID/claim "{\"agent\":\"$CID\"}" >/dev/null
  Q7=$(api POST /questions "$(jq -c --arg a "$CID" --arg t "$T7ID" --slurpfile c "$TMP/card.json" \
        '{agent:$a,project:"demo",kind:"test",task:$t,card:$c[0]}' <<<'{}')" | jq -r .id)
  sql "UPDATE agents SET last_seen='$PAST' WHERE id='$CID'"
  api POST /reap '{}' >/dev/null
  hum POST /questions/$Q7/answer '{"answer":"ok","by":"human"}' >/dev/null
  check "a test answer on a reaped task does not produce an unowned claimed" "$(api GET /tasks/$T7ID)" \
    '.status=="orphaned" and .owner==null and .human_test==null'

  # And the human-OK dies with the owner: otherwise the next claimant walks straight through
  # gate_merge's human check on a branch nobody has tested.
  sql "UPDATE agents SET last_seen='$PAST' WHERE id='$EID'"
  api POST /reap '{}' >/dev/null
  check "orphaning clears the human-OK" "$(api GET /tasks/$T5ID)" \
    '.status=="orphaned" and .human_test==null'

  # A FAIL on a task that is already merged and done must become a NEW task. The block reads
  # repo, title, pr and merge_sha from the same row the status came from — a narrower SELECT
  # compiles fine and crashes only here, in front of a human (T-199).
  T8ID=$(api POST /tasks "{\"agent\":\"$EID\",\"project\":\"demo\",\"repo\":\"web\",\"title\":\"out in production\",\"risk\":\"high\"}" | jq -r .id)
  api POST /tasks/$T8ID/claim "{\"agent\":\"$EID\"}" >/dev/null
  Q8=$(api POST /questions "$(jq -c --arg a "$EID" --arg t "$T8ID" --slurpfile c "$TMP/card.json" \
        '{agent:$a,project:"demo",kind:"test",task:$t,card:$c[0]}' <<<'{}')" | jq -r .id)
  sql "UPDATE tasks SET status='done', pr='http://pr/8', merge_sha='deadbee' WHERE id='$T8ID'"
  hum POST /questions/$Q8/answer '{"answer":"fail","by":"human","note":"the button is gone"}' >/dev/null
  check "a FAIL on a done task creates a follow-up task" \
    "$(api GET '/tasks?project=demo')" \
    '[.tasks[]|select(.title|startswith("FAIL from human test of '"$T8ID"'"))]|length==1'
  check "and the done task does not rise again" "$(api GET /tasks/$T8ID)" '.status=="done"'

  # BOARD_LEASE_MIN=0 would have orphaned everything at once on the next tick. The clamp
  # must prevent that.
  # A free port, not PORT+1: several agents run the suite at the same time, and a fixed
  # offset collided with the neighbour's main instance. That produced red runs that were not
  # regressions.
  P2=$(python3 -c "import socket;s=socket.socket();s.bind((\"\",0));print(s.getsockname()[1]);s.close()")
  U2="http://localhost:$P2"
  BOARD_DB="$TMP/b2.db" BOARD_TOKEN="$TOKEN" BOARD_POLICY="$TMP/policy.json" BOARD_LEASE_MIN=0 \
    BOARD_PORT="$P2" BOARD_BASE_URL="$U2" python3 board.py > "$TMP/log2" 2>&1 &
  P2PID=$!
  for _ in $(seq 50); do curl -sf "$U2/healthz" >/dev/null && break; sleep .1; done
  api2() { local m="$1" p="$2"; shift 2
    curl -sS -m 5 -X "$m" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      ${1:+-d "$1"} "$U2/api/v1$p"; }
  DID=$(api2 POST /agents '{"project":"demo","harness":"claude-code","host":"host-a","session":"z1"}' | jq -r .id)
  T4ID=$(api2 POST /tasks "{\"agent\":\"$DID\",\"project\":\"demo\",\"title\":\"clamp\"}" | jq -r .id)
  api2 POST /tasks/$T4ID/claim "{\"agent\":\"$DID\"}" >/dev/null
  api2 POST /reap '{}' >/dev/null
  check "BOARD_LEASE_MIN=0 is clamped — the reaper does not take everything" "$(api2 GET /tasks/$T4ID)" \
    '.status=="claimed"'
  kill $P2PID 2>/dev/null
  # Tidy the lease fixture so it does not sort ahead of the WIP drain (orphaned > in_review).
  sql "UPDATE tasks SET status='done', owner=NULL WHERE id IN ('$T3ID','$T5ID','$T6ID','$T7ID','$T8ID')"
fi

echo "== WIP limit on unreviewed work =="
WAG=$(api POST /agents '{"project":"demo","harness":"claude-code","host":"host-a","session":"wip","capabilities":["merge"]}' | jq -r .id)
WIDS=""
for i in 1 2 3 4 5; do
  W=$(api POST /tasks "{\"agent\":\"$WAG\",\"project\":\"demo\",\"title\":\"wip$i\"}" | jq -r .id)
  api POST "/tasks/$W/claim" "{\"agent\":\"$WAG\"}" >/dev/null
  api POST "/tasks/$W/progress" "{\"agent\":\"$WAG\",\"status\":\"in_review\"}" >/dev/null
  api POST "/tasks/$W/release" "{\"agent\":\"$WAG\"}" >/dev/null
  WIDS="$WIDS $W"
done
WNEW=$(api POST /tasks "{\"agent\":\"$WAG\",\"project\":\"demo\",\"title\":\"wip-new\",\"priority\":99}" | jq -r .id)
NX=$(api GET "/tasks/next?agent=$WAG")
check "the limit hides open tasks" "$NX" '.id != "'"$WNEW"'"'
check "but still offers the in_review that drains it" "$NX" '.status=="in_review" or .wip_full==true'
W1=$(cut -d" " -f2 <<<"$WIDS")
api POST "/tasks/$W1/claim" "{\"agent\":\"$WAG\"}" >/dev/null
check "a hand-off is NEVER blocked by the limit" \
  "$(api POST "/tasks/$W1/release" "{\"agent\":\"$WAG\",\"note\":\"back\"}")" '.ok'
api POST "/tasks/$W1/claim" "{\"agent\":\"$WAG\"}" >/dev/null
api POST "/tasks/$W1/review" "{\"agent\":\"$WAG\",\"open\":0}" >/dev/null
api POST "/tasks/$W1/release" "{\"agent\":\"$WAG\"}" >/dev/null
check "a review lowers the counter" "$(api GET '/tasks?project=demo')" \
  '[.tasks[]|select(.status=="in_review" and .review_open==null and (.title|startswith("wip")))]|length == 4'
for W in $WIDS $WNEW; do
  api POST "/tasks/$W/release" "{\"agent\":\"$WAG\"}" >/dev/null
  api POST "/tasks/$W/done" "{\"agent\":\"$WAG\",\"no_merge\":true}" >/dev/null
done
api POST "/agents/$WAG/finished" '{"reason":"conformance"}' >/dev/null

echo "== a deploy is required before done when the manifest declares one =="
DAG=$(api POST /agents '{"project":"demo","harness":"claude-code","host":"host-a","session":"depl","capabilities":["merge"]}' | jq -r .id)
api POST /projects '{"project":"demo","manifest":{"project":"demo","deploy":{"dev":"true"}}}' >/dev/null
DT=$(api POST /tasks "{\"agent\":\"$DAG\",\"project\":\"demo\",\"title\":\"deploytest\"}" | jq -r .id)
api POST "/tasks/$DT/claim" "{\"agent\":\"$DAG\"}" >/dev/null
api POST "/tasks/$DT/merge_verified" "{\"agent\":\"$DAG\",\"sha\":\"deadbeef\"}" >/dev/null
check "done is refused without a deploy" "$(api POST "/tasks/$DT/done" "{\"agent\":\"$DAG\"}")" '.needs_deploy==true'
check "deployed is reported" "$(api POST "/tasks/$DT/deployed" "{\"agent\":\"$DAG\",\"env\":\"dev\"}")" '.ok'
check "and then done goes through" "$(api POST "/tasks/$DT/done" "{\"agent\":\"$DAG\"}")" '.ok'
api POST /projects '{"project":"demo","manifest":{"project":"demo"}}' >/dev/null
api POST "/agents/$DAG/finished" '{"reason":"conformance"}' >/dev/null

if [ "$OWN_SERVER" = 1 ]; then
echo "== the human token: the human identity must be proven, not claimed =="
HT="human-$$"
# Its own instance with BOARD_HUMAN_TOKEN set, so we can test both sides.
HDIR=$(mktemp -d); cp "$TMP/policy.json" "$HDIR/p.json" 2>/dev/null || echo '{}' > "$HDIR/p.json"
HPORT=$(python3 -c "import socket;s=socket.socket();s.bind(('',0));print(s.getsockname()[1]);s.close()")
BOARD_DB="$HDIR/h.db" BOARD_TOKEN="$TOKEN" BOARD_HUMAN_TOKEN="$HT" BOARD_POLICY="$HDIR/p.json" \
  BOARD_PORT="$HPORT" python3 board.py > "$HDIR/log" 2>&1 &
HPID=$!
for _ in $(seq 50); do curl -sf "http://localhost:$HPORT/healthz" >/dev/null && break; sleep .1; done
hapi() { local m="$1" p="$2" t="$3"; shift 3
  curl -sS -m 5 -X "$m" -H "Authorization: Bearer $t" -H 'Content-Type: application/json' \
    ${1:+-d "$1"} "http://localhost:$HPORT/api/v1$p"; }
HA=$(hapi POST /agents "$TOKEN" '{"project":"p","harness":"claude-code","host":"h","session":"s","capabilities":[]}' | jq -r .id)
check "an agent token can NOT pin a role" \
  "$(hapi PUT /roles/coordinator "$TOKEN" "{\"project\":\"p\",\"agent\":\"$HA\"}")" '.needs_human_token==true'
check "the human token can pin" \
  "$(hapi PUT /roles/coordinator "$HT" "{\"project\":\"p\",\"agent\":\"$HA\"}")" '.source=="pinned"'
HQ=$(hapi POST /questions "$TOKEN" "{\"agent\":\"$HA\",\"project\":\"p\",\"text\":\"ok?\"}" | jq -r .id)
check "an agent cannot answer AS the human" \
  "$(hapi POST "/questions/$HQ/answer" "$TOKEN" '{"answer":"yes","by":"human"}')" '.needs_human_token==true'
check "an agent can answer under its own id" \
  "$(hapi POST "/questions/$HQ/answer" "$TOKEN" "{\"answer\":\"yes\",\"by\":\"$HA\"}")" '.answer=="yes"'
HQ2=$(hapi POST /questions "$TOKEN" "{\"agent\":\"$HA\",\"project\":\"p\",\"text\":\"and?\"}" | jq -r .id)
check "the human token can answer as the human" \
  "$(hapi POST "/questions/$HQ2/answer" "$HT" '{"answer":"yes","by":"human"}')" '.answer=="yes"'
HQ3=$(hapi POST /questions "$TOKEN" "{\"agent\":\"$HA\",\"project\":\"p\",\"text\":\"browser?\"}" | jq -r .id)
HC=$(curl -si -H "Cookie: board_token=$TOKEN" "http://localhost:$HPORT/status?t=$HT" \
     | sed -n 's/^Set-Cookie: \([^;]*\).*/\1/p' | tr -d '\r')
check "browser login swaps an old agent cookie for the human token" \
  "$(curl -sS -X POST -H "Cookie: $HC" -H 'Content-Type: application/x-www-form-urlencoded' \
      -d answer=yes "http://localhost:$HPORT/q/$HQ3/answer" >/dev/null; hapi GET "/questions" "$HT")" \
  '.questions | all(.id != "'"$HQ3"'")'
BAD=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
      -H "Cookie: board_token=$HT" "http://localhost:$HPORT/status?t=invalid")
check "the query token must itself be valid before it can overwrite the cookie" \
  "{\"code\":$BAD}" '.code==401'
kill $HPID 2>/dev/null; rm -rf "$HDIR"

echo "== pause: draining, not stopping =="
PAG=$(api POST /agents '{"project":"demo","harness":"claude-code","host":"host-a","session":"pause","capabilities":[]}' | jq -r .id)
PA=$(api POST /projects/demo/pause '{"by":"human","note":"conformance"}')
check "pause is set" "$PA" '.paused|test("human")'
PT=$(api POST /tasks "{\"agent\":\"$PAG\",\"project\":\"demo\",\"title\":\"pausetest\"}" | jq -r .id)
check "pause hides NEW tasks from task next" \
  "$(api GET "/tasks/next?agent=$PAG")" '.id != "'"$PT"'"'
check "claiming a NEW task is refused" \
  "$(api POST "/tasks/$PT/claim" "{\"agent\":\"$PAG\"}")" '.paused==true'
PD=$(api POST /tasks "{\"agent\":\"$PAG\",\"project\":\"demo\",\"title\":\"pause-drenering\"}" | jq -r .id)
api POST /projects/demo/resume '{"by":"human"}' >/dev/null
api POST "/tasks/$PD/claim" "{\"agent\":\"$PAG\"}" >/dev/null
api POST "/tasks/$PD/progress" "{\"agent\":\"$PAG\",\"status\":\"in_review\"}" >/dev/null
api POST "/tasks/$PD/release" "{\"agent\":\"$PAG\"}" >/dev/null
api POST /projects/demo/pause '{"by":"human","note":"conformance"}' >/dev/null
check "an in_review can be claimed while paused: draining does not stop" \
  "$(api POST "/tasks/$PD/claim" "{\"agent\":\"$PAG\"}")" '.owner=="'"$PAG"'"'
api POST "/tasks/$PD/done" "{\"agent\":\"$PAG\",\"no_merge\":true}" >/dev/null
check "resume opens it again" "$(api POST /projects/demo/resume '{"by":"human"}')" '.paused==null'
check "and then the task comes back" "$(api GET "/tasks/next?agent=$PAG")" '.id != null'
api POST "/tasks/$PT/done" "{\"agent\":\"$PAG\",\"no_merge\":true}" >/dev/null
api POST "/agents/$PAG/finished" '{"reason":"conformance"}' >/dev/null
fi

if [ "$OWN_SERVER" = 1 ]; then
  echo "== ntfy push =="
  # The proof has to be that the push ARRIVES at an HTTP receiver. Everything else (that the
  # call does not throw, that the thread starts) was true for the entire day the push was dead.
  api POST /questions "{\"agent\":\"$AID\",\"project\":\"demo\",\"text\":\"ntfy canary\",\"default\":\"no\",\"deadline\":\"2026-09-08T08:00:00Z\"}" >/dev/null
  for _ in $(seq 50); do [ -s "$TMP/ntfy" ] && break; sleep .1; done
  N=$(cat "$TMP/ntfy" 2>/dev/null || true)
  grep -q "ntfy canary" <<<"$N" && ok "the push arrives at the receiver" \
    || no "the push arrives at the receiver" "nothing received: $(tail -3 "$TMP/log")"
  grep -q "TITLE: .*demo" <<<"$N" && ok "the Title header is latin-1 safe and set" \
    || no "the Title header" "$N"
  grep -q "❓" <<<"$N" && ok "the emoji title survives in the body (UTF-8)" \
    || no "emoji in the body" "$N"
  grep -q "ntfy failed" "$TMP/log" && no "ntfy error in the pod log" "$(grep -m1 'ntfy failed' "$TMP/log")" \
    || ok "no ntfy errors in the pod log"
fi

# An absolute symlink points out of any worktree and into the primary checkout: the agent
# believes it is changing its own branch, but dirties the main tree and loses the change. It
# hit three tasks in one evening before anyone noticed.
if [ -z "$(find . -type l -lname '/*' -printf '%p ')" ]; then ok "no absolute symlinks in the repo"
else no "no absolute symlinks in the repo" "$(find . -type l -lname '/*' -printf '%p -> %l\n')"; fi

echo "== comments on a task =="
CMT=$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"repo\":\"web\",\"title\":\"commented\"}" | jq -r .id)
check "an agent can comment" \
  "$(api POST "/tasks/$CMT/comment" "{\"agent\":\"$AID\",\"text\":\"finding: this touches the payment flow\"}")" \
  '.ok and .by=="'"$AID"'"'
check "the human token makes the human the sender" \
  "$(hum POST "/tasks/$CMT/comment" '{"text":"ok, take it after the release"}')" '.by=="human"'
check "an empty comment is refused" "$(api POST "/tasks/$CMT/comment" "{\"agent\":\"$AID\",\"text\":\"  \"}")" '.error'
check "the comment is in the timeline and in progress" "$(api GET "/tasks/$CMT")" \
  '[.progress[]|select(.note|test("after the release"))]|length==1'
# The context budget is hard (§6.5): comments must NEVER bleed into the hot paths.
LIST_B=$(api GET '/tasks?project=demo' | wc -c)
for i in 1 2 3 4 5; do
  api POST "/tasks/$CMT/comment" "{\"agent\":\"$AID\",\"text\":\"$(head -c 600 /dev/zero | tr '\0' 'x')\"}" >/dev/null
done
LIST_A=$(api GET '/tasks?project=demo' | wc -c)
check "a 3000-character comment does not grow task list" \
  "$(jq -nc --argjson a "$LIST_A" --argjson b "$LIST_B" '{d:($a-$b)}')" '.d < 50'
api POST "/tasks/$CMT/done" "{\"agent\":\"$AID\",\"no_merge\":true}" >/dev/null

echo "== the HTML pages =="
J="$TMP/cookies"
for p in "/status?t=$TOKEN" "/tests" ${QID:+"/q/$QID"} ${TID:+"/t/$TID"}; do
  code=$(curl -sLo /dev/null -w '%{http_code}' -c "$J" -b "$J" "$BOARD_URL$p")
  [ "$code" = 200 ] && ok "GET $p" || no "GET $p" "HTTP $code"
done
code=$(curl -so /dev/null -w '%{http_code}' "$BOARD_URL/status")
[ "$code" = 401 ] && ok "without a token: 401" || no "without a token it must be 401" "HTTP $code"

# T-390: a page rendered with an agent token looks identical to one rendered with the
# human token, and then refuses every answer. The identity has to be ON the page, and a
# refused form has to be a page rather than a JSON blob on a phone.
AGP=$(curl -sL -H "Authorization: Bearer $TOKEN" "$BOARD_URL/status")
grep -q '>agent<' <<<"$AGP" \
  && ok "an agent-token page says it is signed in as an agent" \
  || no "the agent-token page does not say which identity it carries" "$(grep -o 'badge-[a-z]*' <<<"$AGP" | sort -u | tr '\n' ' ')"
if [ "$OWN_SERVER" = 1 ]; then
  AQ=$(api POST /questions "{\"agent\":\"$AID\",\"project\":\"demo\",\"text\":\"identitet?\"}" | jq -r .id)
  FORM=$(curl -s -o "$TMP/refused" -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
         -H 'Content-Type: application/x-www-form-urlencoded' \
         -X POST -d 'answer=yes' "$BOARD_URL/q/$AQ/answer")
  [ "$FORM" = 403 ] && grep -q 'board open' "$TMP/refused" \
    && ok "a refused form answer is an HTML page that says how to fix it, not JSON" \
    || no "refused form answer" "HTTP $FORM: $(head -c 120 "$TMP/refused")"
  grep -q '{"error"' "$TMP/refused" \
    && no "the refused form still returns raw JSON" "$(head -c 80 "$TMP/refused")" \
    || ok "the refused form returns no raw JSON"
  hum POST "/questions/$AQ/answer" '{"answer":"yes","by":"human"}' >/dev/null
  HP=$(curl -sL -H "Authorization: Bearer $HUMAN_TOKEN" "$BOARD_URL/status")
  grep -q ">$(printf %s "${BOARD_HUMAN:-human}")<" <<<"$HP" \
    && ok "a human-token page says it is signed in as the human" \
    || no "the human-token page does not name the human" "$(grep -o 'badge-sm badge-[a-z]*>[a-z]*' <<<"$HP" | head -3)"
fi
TH=$(curl -sL -c "$J" -b "$J" "$BOARD_URL/t/$TID")
if grep -q "$TID" <<<"$TH" && grep -q "timeline" <<<"$TH" && grep -q "the owner.s state" <<<"$TH" && grep -q "#201" <<<"$TH"; then
  ok "GET /t/<id> is a task page with a PR number and a timeline"
else
  no "GET /t/<id> task page" "$TH"
fi
# A task of its own for this check: the earlier ones were closed by the tests above, and
# /status does not show `done`. The test must measure the rendering, not the ordering.
SLID=$(jq -r .id <<<"$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"title\":\"status link\"}")")
api POST /tasks/$SLID/claim "{\"agent\":\"$AID\"}" >/dev/null
api POST /tasks/$SLID/progress "{\"agent\":\"$AID\",\"pr\":\"http://pr/1\"}" >/dev/null
STHTML=$(curl -sL -c "$J" -b "$J" "$BOARD_URL/status")
# `>pr<` and not `<th>pr`: the check must see that the PR has its OWN COLUMN, not that the
# header cell has no attributes. The previous form made every class on a <th> a conformance
# violation.
if grep -q "href='/t/$SLID'" <<<"$STHTML" && grep -q ">pr<" <<<"$STHTML" && grep -q "#1" <<<"$STHTML"; then
  ok "status: the task id is a link, the PR number is in its own column"
else
  no "status links and the PR column" "$STHTML"
fi

echo "== prometheus metrics =="
M_CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BOARD_URL/metrics")
[ "$M_CODE" = 200 ] && ok "GET /metrics returns 200 without token" || no "GET /metrics returns 200 without token" "HTTP $M_CODE"
M_CT=$(curl -s -o /dev/null -w '%{content_type}' "$BOARD_URL/metrics")
grep -qi "text/plain" <<<"$M_CT" && ok "GET /metrics has text/plain content-type" || no "GET /metrics content-type" "$M_CT"
M_BODY=$(curl -s "$BOARD_URL/metrics")
grep -q "^board_up 1" <<<"$M_BODY" && ok "/metrics contains board_up 1" || no "/metrics contains board_up 1" "$M_BODY"
grep -q "board_tasks_total" <<<"$M_BODY" && ok "/metrics contains board_tasks_total" || no "/metrics contains board_tasks_total" "$M_BODY"

# Ordering by last activity: a project whose last event was "task went done" must still sort
# above an older project that merely has an open task lying around.
# (last_activity() has to read unfiltered from the database, not the already-filtered
# task/agent lists status() shows — those have removed done/finished.)
ZO=$(api POST /agents '{"project":"zzold","harness":"claude-code","host":"host-a","session":"zzold-s"}')
ZOID=$(jq -r .id <<<"$ZO")
api POST /tasks "{\"agent\":\"$ZOID\",\"project\":\"zzold\",\"title\":\"old, untouched task\"}" >/dev/null
ZN=$(api POST /agents '{"project":"zznew","harness":"claude-code","host":"host-a","session":"zznew-s"}')
ZNID=$(jq -r .id <<<"$ZN")
NT=$(api POST /tasks "{\"agent\":\"$ZNID\",\"project\":\"zznew\",\"title\":\"just finished\"}")
NTID=$(jq -r .id <<<"$NT")
api POST /tasks/$NTID/claim "{\"agent\":\"$ZNID\"}" >/dev/null
api POST /tasks/$NTID/done "{\"agent\":\"$ZNID\",\"no_merge\":true}" >/dev/null
api POST "/agents/$ZNID/finished" '{"reason":"conformance ordering"}' >/dev/null
ORDER=$(curl -sSL -m 5 -c "$J" -b "$J" "$BOARD_URL/status?t=$TOKEN" | grep -o "data-p='[^']*'")
IDX_NEW=$(grep -n "'zznew'" <<<"$ORDER" | cut -d: -f1)
IDX_OLD=$(grep -n "'zzold'" <<<"$ORDER" | cut -d: -f1)
[ -n "$IDX_NEW" ] && [ -n "$IDX_OLD" ] && [ "$IDX_NEW" -lt "$IDX_OLD" ] \
  && ok "a project with a recently done task sorts above an older, untouched project" \
  || no "ordering by last activity" "new=$IDX_NEW old=$IDX_OLD order=$ORDER"

# ---------------------------------------------------------------------------
# The CLI layer. Up to here the suite only tested HTTP with hand-written JSON bodies — the
# path SKILL.md actually takes (bin/board from a directory with project.yaml) was untested,
# and that is where the most serious bugs sat.
echo "== the CLI, as the skill calls it =="
# A local test board must not hijack the identity you hold against the production board:
# the cache (agent id, GET answers, outbox) is keyed on BOARD_URL. Otherwise you register
# against localhost and send the next `board task claim` to production as the wrong agent.
CH="$TMP/cachetest"
for U in "$BOARD_URL" "http://localhost:1"; do
  BOARD_CACHE="$CH" BOARD_URL="$U" BOARD_TOKEN="$TOKEN" BOARD_SESSION=hijack \
    ./bin/board register --project demo --model m --host hijacker >/dev/null 2>&1
done
if [ "$(find "$CH" -name 'session-hijack' | wc -l)" -eq 2 ]; then
  ok "the session cache is keyed on BOARD_URL"
else no "the session cache is keyed on BOARD_URL" "$(find "$CH" -name 'session-hijack' | tr '\n' ' ')"; fi

CLIDIR="$TMP/proj"; mkdir -p "$CLIDIR/web"
git -C "$CLIDIR/web" init -q . 2>/dev/null
git -C "$CLIDIR/web" config user.email c@c; git -C "$CLIDIR/web" config user.name c
echo x > "$CLIDIR/web/r.md"; git -C "$CLIDIR/web" add -A
git -C "$CLIDIR/web" commit -qm init; git -C "$CLIDIR/web" branch -M main 2>/dev/null
touch "$CLIDIR/CLAUDE.md"
cat > "$CLIDIR/project.yaml" <<'YML'
project: demo
phase: launch
goal: conformance
onboarding: CLAUDE.md
repos: [web, missing]
YML
export BOARD_URL BOARD_TOKEN="$TOKEN" BOARD_CACHE="$TMP/cli-cache" BOARD_SESSION="conf-$$" BOARD_HARNESS=claude-code
export PATH="$PWD/bin:$PATH"
cli() { (cd "$CLIDIR" && board "$@" 2>/dev/null); }

R=$(cli register --cap merge,browser-test --model claude-sonnet-5)
check "register without --project takes it from the manifest" "$R" '.project=="demo" and .id'
CLIID=$(jq -r .id <<<"$R")
check "register mirrors the phase from the manifest" "$(cli status --json)" \
  '[.projects[]|select(.name=="demo")][0].phase=="launch"'

# `status --me` is the command the stop rules are read from. It must give the agent's own
# row AND the budget block — and it must blow up, not stay quiet, when the agent is not in
# the answer: empty output with exit 0 reads as "no data, carry on".
check "status --me gives your own row + the budget block" "$(cli status --me)" \
  '.agent.id=="'"$CLIID"'" and .project=="demo" and .budget.ceilings["7d"]==75
    and .budget.windows and has("stop")'
ME_ERR=$(cd "$TMP" && BOARD_AGENT_ID=A-doesnotexist board status --me 2>&1 >"$TMP/me.out"); ME_RC=$?
if [ "$ME_RC" -ne 0 ] && [ ! -s "$TMP/me.out" ] && [ -n "$ME_ERR" ]; then
  ok "status --me with no agent in the answer fails loudly (exit≠0 + message), not empty"
else no "status --me with no agent" "rc=$ME_RC out='$(cat "$TMP/me.out")' err='$ME_ERR'"; fi
check "task create without --project" "$(cli task create --title "cli task" --repo web)" '.id'

# T-201: a `--repo` the manifest does not know is a silent error at creation time that only
# blows up in the NEXT agent, in `task worktree`/`cleanup` ($ROOT/$repo does not exist). The
# board has mirrored the manifest at register and can refuse immediately — and say what is
# valid.
BAD=$(api POST /tasks "{\"agent\":\"$CLIID\",\"project\":\"demo\",\"repo\":\"does-not-exist\",\"title\":\"wrong repo\"}")
check "task create refuses a repo the manifest does not know, and says what is valid" "$BAD" \
  '(.error|test("does-not-exist")) and (.error|test("web"))'
check "…but a repo from the manifest still goes through" \
  "$(api POST /tasks "{\"agent\":\"$CLIID\",\"project\":\"demo\",\"repo\":\"web\",\"title\":\"right repo\"}")" '.id'
# repos: [.] is stored as the project name on the board (T-67) — that must still be valid.
check "…and the project name itself is valid" \
  "$(api POST /tasks "{\"agent\":\"$CLIID\",\"project\":\"demo\",\"repo\":\"demo\",\"title\":\"root repo\"}")" '.id'
RPT=$(api POST /tasks "{\"agent\":\"$CLIID\",\"project\":\"demo\",\"title\":\"for patch\"}" | jq -r .id)
api POST /tasks/$RPT/claim "{\"agent\":\"$CLIID\"}" >/dev/null   # patch requires ownership
check "task patch refuses the same repo — otherwise the refusal at create is just a detour" \
  "$(api PATCH /tasks/$RPT "{\"agent\":\"$CLIID\",\"repo\":\"does-not-exist\"}")" '.error|test("does-not-exist")'
check "task patch lets a known repo through" \
  "$(api PATCH /tasks/$RPT "{\"agent\":\"$CLIID\",\"repo\":\"web\"}")" '.repo=="web"' 
if [ "$OWN_SERVER" = 1 ]; then
check "ask without --project" "$(cli ask --default B --deadline 8h "A or B?")" '.id'

# An empty or unreadable --spec-file used to produce a task with an empty spec and exit 0.
BEFORE=$(api GET '/tasks?project=demo' | jq '.tasks|length')
: > "$TMP/empty-spec"
(cd "$CLIDIR" && board task create --title "empty spec" --spec-file "$TMP/empty-spec") >/dev/null 2>&1
st=$?
AFTER=$(api GET '/tasks?project=demo' | jq '.tasks|length')
[ "$st" -ne 0 ] && [ "$BEFORE" = "$AFTER" ] \
  && ok "an empty --spec-file creates no task" \
  || no "empty --spec-file" "exit=$st, tasks $BEFORE -> $AFTER"
(cd "$CLIDIR" && board task create --title "gone" --spec-file "$TMP/does-not-exist") >/dev/null 2>&1 \
  && no "unreadable --spec-file" "exit 0" || ok "an unreadable --spec-file dies"
check "--spec-file - reads stdin" \
  "$(cd "$CLIDIR" && printf 'fra stdin' | board task create --title "stdin-spec" --spec-file -)" '.id'
check "deadline 8h becomes a real deadline" "$(api GET '/questions?status=open')" \
  '[.questions[]|select(.default_answer=="B")][0].deadline | test("^20")'
check "role claim without --project" "$(cli role claim coordinator)" ".agent==\"$CLIID\""
fi
DET=$(cli project detect)
if grep -q "^PROJECT='demo'" <<<"$DET" && grep -q "^PHASE='launch'" <<<"$DET"; then
  ok "project detect returns shell variables"; else no "project detect" "$DET"; fi
[ -n "$(cli probe)" ] && ok "probe answers with verified capabilities" || no "probe" "empty"

# Step 11 used to merge in $ROOT — the working copy the human is sitting in. The guard must
# refuse when the tree is not clean, instead of switching branch under somebody who is
# working (T-194).
# "demo" has repos: [web, missing] — $CLIDIR is NOT itself a git repo. That is the multi-repo
# case: a guard that asks $ROOT gets "not a git repository", swallows the error and never
# fires. It has to look at the working copy of the task's repo ($ROOT/web). $TID has repo=web.
echo "in the middle of something" > "$CLIDIR/web/uncommitted.txt"
# $TID is not ours, so the command fails regardless — checking the exit code is therefore not
# enough: it has to fail ON THE GUARD, with the guard's own reason.
D=$( (cd "$CLIDIR" && board task merging "$TID") 2>&1 )
grep -qi "uncommitted changes" <<<"$D" \
  && ok "merging refuses when the task's repo is dirty (multi-repo)" \
  || no "merging refuses in a dirty repo (multi-repo)" "$D"
rm -f "$CLIDIR/web/uncommitted.txt"
C=$( (cd "$CLIDIR" && board task merging "$TID") 2>&1 )
grep -qi "uncommitted changes" <<<"$C" \
  && no "merging lets a clean repo through" "$C" \
  || ok "merging lets a clean repo through"
# If the working copy does not exist the guard is blind — it must say so, not swallow it.
NGT=$(api POST /tasks "{\"agent\":\"$AID\",\"project\":\"demo\",\"repo\":\"missing\",\"title\":\"without a working copy\"}" | jq -r .id)
N=$( (cd "$CLIDIR" && board task merging "$NGT") 2>&1 )
grep -qi "not a git repo" <<<"$N" \
  && ok "merging says so when the task's repo is not a git repo" \
  || no "merging swallows a missing git repo" "$N"
# Give web a real origin/main — without it the ancestry check is only "ref missing".
ORIG="$TMP/origin-web.git"
git clone --bare -q "$CLIDIR/web" "$ORIG"
git -C "$CLIDIR/web" remote add origin "$ORIG" 2>/dev/null || true
git -C "$CLIDIR/web" push -q -u origin main >/dev/null
# `merged` skal nekte en sha som ikke er ancestor av origin/main.
M=$( (cd "$CLIDIR" && board task merged "$TID" --sha 3f2a1b9c4d5e6f708192a3b4c5d6e7f809a1b2c3) 2>&1 )
grep -qi "is not on origin/main" <<<"$M" \
  && ok "merged refuses a sha that is not on main" || no "merged sha guard" "$M"
# An aborted merge: MW sits on origin/main, so HEAD (a real main sha) is an ancestor — but
# the task branch never landed. merged must refuse anyway.
BR=$(api GET /tasks/$TID | jq -r '.branch // empty'); [ -z "$BR" ] && BR=task/$TID
git -C "$CLIDIR/web" checkout -qb "$BR" >/dev/null
echo abort > "$CLIDIR/web/abort.md"
git -C "$CLIDIR/web" add -A && git -C "$CLIDIR/web" commit -qm "never merged"
git -C "$CLIDIR/web" checkout -q main
MAINSHA=$(git -C "$CLIDIR/web" rev-parse origin/main)
MB=$( (cd "$CLIDIR" && board task merged "$TID" --sha "$MAINSHA") 2>&1 )
grep -qiE "$BR|branch|did not land|aborted" <<<"$MB" \
  && ok "merged refuses a main sha when the task branch is not on main" \
  || no "merged branch guard" "$MB"
# Fail closed when \$ROOT/\$repo is not a git repo (repos:[web], no .git there).
mv "$CLIDIR/web/.git" "$CLIDIR/web.git.bak"
MG=$( (cd "$CLIDIR" && board task merged "$TID" --sha "$MAINSHA") 2>&1 )
mv "$CLIDIR/web.git.bak" "$CLIDIR/web/.git"
grep -qiE "not a git repo|cannot verify" <<<"$MG" \
  && ok "merged refuses when the git dir is missing (fail closed)" \
  || no "merged fail closed" "$MG"
CT=$(cli task list --status open --json | jq -r '.tasks[0].id')
cli task claim "$CT" >/dev/null
SHOW=$(cli task show "$CT")
if grep -q "$CT" <<<"$SHOW" && grep -q "timeline" <<<"$SHOW" && grep -q "status=" <<<"$SHOW"; then
  ok "task show is readable without jq"
else
  no "task show readable" "$SHOW"
fi
check "task show --json is JSON" "$(cli task show "$CT" --json)" ".id==\"$CT\""
check "task patch sets repo" "$(cli task patch "$CT" --repo web --priority 7)" '.repo=="web" and .priority==7'
check "cli task search finds a task by title" "$(cli task search "cli task" --json)" 'any(.tasks[]; .title=="cli task")'
check "cli task search with no hit gives an empty result" "$(cli task search guaranteed-not-to-exist --json)" "(.tasks|length)==0"
check "the gate is closed for risk=normal in launch without a review" "$(cli gate merge $CT)" '.ok==false'
check "tail --since 2h returns events" "$(cli tail --since 2h)" '(.events|length) > 0'
# The cap of 500 has to be taken from the end: whoever asks for "the last 2 hours" wants to
# see what JUST happened, not the 500 oldest hits.
LAST=$(api GET '/events?since=0' | jq -r '.events[-1].id')
for _ in $(seq 3); do api POST /agents/$CLIID/heartbeat '{"ctx_pct":1}' >/dev/null; done
check "tail returns the NEWEST events, not the oldest" "$(cli tail --since 2h)" \
  "(.events[-1].id > $LAST) and (.events|length) <= 500"
check "a truncated answer says so" "$(api GET '/events?since=0')" \
  '.truncated == ((.events|length) == 500) and (.truncated|not or (.from_id != null))'
# A guessed harness = the wrong grants. Without BOARD_HARNESS and with no trace of a known
# harness in the environment, register must REFUSE, not register as claude-code and inherit
# merge + deploy-dev from the policy. HOME points at an empty directory so the machine's own
# ~/.codex and ~/.grok do not answer for the test.
NOHARN=$(cd "$CLIDIR" && env -u CLAUDE_CODE_SESSION_ID -u GROK_SESSION_ID -u GROK_CLI \
  -u CODEX_SESSION_ID -u CODEX_HOME -u ANTIGRAVITY_AGENT -u ANTIGRAVITY_CONVERSATION_ID -u ANTIGRAVITY_SOURCE_METADATA \
  -u BOARD_HARNESS -u BOARD_SESSION \
  HOME="$TMP/empty-home" BOARD_URL="$BOARD_URL" BOARD_TOKEN="$TOKEN" \
  BOARD_CACHE="$TMP/cli-cache-2" board register --model grok-4 2>&1; echo "exit=$?")
case "$NOHARN" in
  *"exit=0"*) no "register refuses when the harness cannot be derived" "$NOHARN" ;;
  *BOARD_HARNESS*) ok "register refuses when the harness cannot be derived" ;;
  *) no "register refuses when the harness cannot be derived" "$NOHARN" ;;
esac
check "no claude-code agent sneaked in with a grok model" "$(api GET '/status?project=demo')" \
  '[.projects[0].agents[]?|select(.model=="grok-4")]|length==0'
GROKID=$(jq -r .id <<<"$(cd "$CLIDIR" && env -u CLAUDE_CODE_SESSION_ID BOARD_HARNESS=grok \
  BOARD_SESSION="conf-grok-$$" BOARD_URL="$BOARD_URL" BOARD_TOKEN="$TOKEN" \
  BOARD_CACHE="$TMP/cli-cache-3" board register --model grok-4 2>/dev/null)")
check "grok gets the gk prefix and no merge grant" "$(api GET '/status?project=demo')" \
  '[.projects[0].agents[]?|select(.id=="'"$GROKID"'")][0] | (.id|startswith("gk-")) and (.grants|index("merge")|not)'

# An in_review takeover on ANOTHER machine: the directory on the board does not exist here,
# but the branch is pushed. `worktree add -b <br> origin/main` then gave an EMPTY branch and
# silently threw away all the work. The worktree must be built from the branch.
git -C "$CLIDIR/web" remote add origin "$TMP/origin.git" 2>/dev/null
git init -q --bare "$TMP/origin.git"; git -C "$CLIDIR/web" push -q origin main
WT_T=$(cli task create --title "takeover" --repo web | jq -r .id)
cli task claim "$WT_T" >/dev/null
git -C "$CLIDIR/web" checkout -q -b "task/$WT_T"
echo "the implementer's work" > "$CLIDIR/web/done.md"
git -C "$CLIDIR/web" add -A && git -C "$CLIDIR/web" commit -qm work
git -C "$CLIDIR/web" push -q -u origin "task/$WT_T"
git -C "$CLIDIR/web" checkout -q main && git -C "$CLIDIR/web" branch -qD "task/$WT_T"
W=$(cli task worktree "$WT_T")
check "the worktree is built from the pushed branch, not from main" "$W" '.base=="origin"'
if [ -f "$(jq -r .worktree <<<"$W")/done.md" ]; then ok "the work is there after a takeover"
else no "the work is there after a takeover" "done.md is missing from $(jq -r .worktree <<<"$W")"; fi

# T-142: what the automatic test requires is a property of the test, not of the CLI. An agent
# without a browser must be able to run a `how` that is not a browser test.
NOCAP() { (cd "$CLIDIR" && BOARD_SESSION="conf-nocap-$$" BOARD_CACHE="$TMP/nocap-cache" board "$@" 2>/dev/null); }
NOCAPID=$(NOCAP register --cap merge --model claude-sonnet-5 | jq -r .id)
cat >> "$CLIDIR/project.yaml" <<'YML'
environments:
  dev: {how: "true", requires: []}
YML
check "test-level: requires:[] gives auto without a browser capability" "$(NOCAP test-level $CT)" \
  '.level=="auto" and .how=="true"'
sed -i 's/requires: \[\]/requires: [browser-test]/' "$CLIDIR/project.yaml"
check "test-level: requires:[browser-test] gives human without a browser capability" "$(NOCAP test-level $CT)" \
  '.level=="human" and (.why|test("browser-test"))'
api POST "/agents/$NOCAPID/finished" '{"reason":"conformance done"}' >/dev/null

# T-189: a board that answers 503 (Traefik during a rollout) is down, not a valid answer. The
# cache is keyed on BOARD_URL (T-118), so a cache warmed against the real board does not apply
# to the 503 URL. What matters here is that the agent does not stop: a 503 must read as
# "down", yield stale:true and exit 0 — with or without a cache hit.
P503=$(python3 -c "import socket;s=socket.socket();s.bind((\"\",0));print(s.getsockname()[1]);s.close()")
cat > "$TMP/down.py" <<'DOWNPY'
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(503); self.end_headers(); self.wfile.write(b"no available server")
    do_POST = do_GET
    def log_message(self, *a): pass
http.server.HTTPServer(("", int(sys.argv[1])), H).serve_forever()
DOWNPY
python3 "$TMP/down.py" "$P503" & DOWNPID=$!
for _ in $(seq 30); do curl -s -o /dev/null "http://localhost:$P503/" && break; sleep .1; done
DOWN=$(cd "$CLIDIR" && BOARD_URL="http://localhost:$P503" board task show "$CT" --json 2>/dev/null); DRC=$?
check "a 503 from the board reads as down: stale:true, exit 0 (the agent does not stop)" \
  "$DOWN" ".stale==true and $DRC==0"
DOWN2=$(cd "$CLIDIR" && BOARD_URL="http://localhost:$P503" board task pr "$CT" 2>&1); DRC2=$?
if [ "$DRC2" -ne 0 ] && grep -q 'the board is down' <<<"$DOWN2"; then
  ok "a command that touches git refuses to act on a cached answer"
else no "task pr against a board that is down" "rc=$DRC2 $DOWN2"; fi
CACHED=$(cd "$CLIDIR" && board task show "$CT" --json 2>/dev/null)
check "the error page never landed in the cache" "$CACHED" '.stale != true and (.error|not)'
kill $DOWNPID 2>/dev/null

# Clean up after ourselves. A suite that leaves living agents on a shared board makes the
# NEXT run lose the role ranking against its own ghost.
for A in "$AID" "$BID" "$CLIID" $(api GET '/status?project=demo' \
         | jq -r '.projects[0].agents[]?|select(.status!="finished")|.id'); do
  [ -n "$A" ] && api POST "/agents/$A/finished" '{"reason":"conformance done"}' >/dev/null
done
# The suite owns `demo` entirely and must hand it back EMPTY. If we left remnants behind they
# sorted first in `task next` on the next run and failed the suite's own assertions — it
# tripped over its own litter.
for X in $(api GET '/tasks?project=demo' | jq -r '.tasks[]?|select(.status!="done")|.id'); do
  api POST "/tasks/$X/release" "{\"agent\":\"$CLIID\"}" >/dev/null
  api POST "/tasks/$X/done" "{\"agent\":\"$CLIID\",\"no_merge\":true}" >/dev/null
done
# Unowned in_review/orphaned rows survive release+done (403). The suite owns demo.
if [ "$OWN_SERVER" = 1 ]; then
  python3 -c 'import sqlite3,sys
d=sqlite3.connect(sys.argv[1],timeout=5)
d.execute("UPDATE tasks SET status=\"done\", owner=NULL WHERE project=\"demo\" AND status!=\"done\"")
d.commit()' "$TMP/board.db"
fi
# Questions have to be tidied too. Without this the suite's "A or B?" piles up in the real
# question queue and drowns the genuine ones — 35 of them before this was noticed.
for Q in $(api GET '/questions' | jq -r '.questions[]?|select(.status=="open" and .project=="demo")|.id'); do
  api POST "/questions/$Q/answer" '{"answer":"tidied by conformance","by":"board"}' >/dev/null
done
check "demo is empty after the suite" "$(api GET '/tasks?project=demo')" \
  '[.tasks[]?|select(.status!="done")]|length == 0'
check "no open conformance questions left" "$(api GET '/questions')" \
  '[.questions[]?|select(.status=="open" and .project=="demo")]|length == 0' 
check "no living agents left after the suite" "$(api GET '/status?project=demo')" \
  '[.projects[0].agents[]?] | length == 0'

# The reaper runs on a 60-second loop and is not visible over HTTP, so it is tested
# in-process against an empty database of its own. Only against our own board.py — a foreign
# backend has its own reaper and must not be measured against ours.
if [ "$OWN_SERVER" = 1 ]; then
  echo "== reaper =="
  R=$(BOARD_DB="$TMP/reap.db" python3 - <<'REAPPY'
import board
for tid, st in (("R-1", "awaiting_human"), ("R-2", "claimed")):
    board.db.execute("INSERT INTO tasks (id,project,status,owner,lease_until,updated) "
                     "VALUES (?,'demo',?,'a1',?,?)", (tid, st, board.plus(-1), board.now()))
board.reap()
print(*[board.db.execute("SELECT status FROM tasks WHERE id=?", (t,)).fetchone()[0]
        for t in ("R-1", "R-2")])
REAPPY
)
  [ "$R" = "awaiting_human orphaned" ] \
    && ok "the reaper leaves awaiting_human alone, but orphans an expired claimed" \
    || no "the reaper leaves awaiting_human alone, but orphans an expired claimed" "$R"
fi


if [ "$OWN_SERVER" = 1 ]; then
  # Register a fresh agent for routine tests
  RT_AID=$(api POST /agents '{"project":"demo","harness":"claude-code","host":"host-r","session":"rt-session"}' | jq -r .id)


  # Register a fresh agent for routine tests
  RT_AID=$(api POST /agents '{"project":"demo","harness":"claude-code","host":"host-r","session":"rt-session"}' | jq -r .id)
  
  api POST /projects/demo/resume '{"by":"human"}' >/dev/null
  echo "== routines =="
  api POST /projects '{"project": "demo", "manifest": {"routines": {"clean": {"title": "Clean logs", "interval": "1d", "priority": 10}}}}' >/dev/null
  check "routine created" "$(api GET "/routines?project=demo")" '.routines[0].name == "clean"'
  
  TID=$(api POST /tasks "{\"project\": \"demo\", \"title\": \"Run clean\", \"routine\": \"clean\", \"agent\": \"$RT_AID\"}" | jq -r .id)
  api POST "/tasks/$TID/claim" "{\"agent\": \"$RT_AID\"}" >/dev/null
  api POST "/tasks/$TID/done" "{\"agent\": \"$RT_AID\", \"no_merge\": true}" >/dev/null
  
  check "routine last_run updated" "$(api GET "/routines?project=demo")" '.routines[0].last_run != null'


  R=$(curl -s "$BOARD_URL/metrics")
  echo "$R" | grep -q 'board_routines_total{project="demo",status="open"}' && ok "metrics contains routines" || no "metrics contains routines" "missing"
  echo "$R" | grep -q 'board_routine_last_run_timestamp_seconds{project="demo",routine="clean"}' && ok "metrics contains last_run" || no "metrics contains last_run" "missing"
  
  echo "== reaper routines =="
  # test reaper spawns a routine task when due
  api POST /projects '{"project": "demo-2", "manifest": {"routines": {"overdue": {"title": "Overdue routine", "interval": "1d", "priority": 10}}}}' >/dev/null
  # manipulate next_due via python
  R2=$(BOARD_DB="$TMP/reap.db" python3 - <<'REAPPY'
import board
board.db.execute("INSERT INTO projects (name, phase) VALUES ('demo-2', 'idea')")
board.db.execute("INSERT INTO routines (project, name, title, spec, interval, status, next_due) VALUES ('demo-2', 'overdue', 'T', 'S', '1d', 'open', ?)", (board.plus(-10),))
board.db.commit()
board.reap()
res = board.db.execute("SELECT id FROM tasks WHERE project='demo-2' AND routine='overdue'").fetchone()
print(res[0] if res else "")
REAPPY
)
  [ -n "$R2" ] && ok "the reaper spawns a task for an overdue routine" || no "the reaper spawns a task for an overdue routine" "task not spawned"
fi

echo
printf 'PASS %d  FAIL %d\n' "$PASS" "$FAIL"
[ "$OWN_SERVER" = 1 ] && [ "$FAIL" -gt 0 ] && { echo "--- server log ---"; tail -20 "$TMP/log"; }
exit $((FAIL > 0))

