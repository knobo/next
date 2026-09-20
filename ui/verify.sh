#!/usr/bin/env bash
# The acceptance test for the board's HTML pages. Starts its own instance on a free port
# and checks that each of the four pages answers 200 with the checked-in stylesheet linked,
# that the stylesheet answers 200 as text/css and is cached, and that the CSP has been
# widened by exactly one source ('self' for style-src) and nothing else.
#
# The last step is the one that cannot be reasoned out: a real Chromium loads every page in
# light and dark mode, on a wide screen and on a phone, and ANY CSP violation or other
# console error is a FAILURE. It was that check that revealed the CSP blocks data: URIs too
# — something a grep over the CSS would never have seen.
#   cd ui && npm install && ./build.sh && ./verify.sh
set -uo pipefail
cd "$(dirname "$0")"
W="$(cd .. && pwd)"
T=tst-$RANDOM; P=$((18000 + RANDOM % 900)); D=$(mktemp -d)
# board-policy.json is NOT in the repo (it is hand-edited on the host and never deployed,
# see budget()). The ceilings therefore have to be stubbed here, otherwise no meter has a
# ceiling to draw its line at.
cat > "$D/policy.json" <<'POL'
{"budget": {"*": {"ceilings": {"5h": 85, "7d": 60}}},
 "grants": {"demo": {"claude-code@*": ["merge"]}}}
POL
# A human token, because answering a form is a human action (T-390): without it the
# confirmation page is never reached and the check below would test the refusal instead.
HT=hum-$RANDOM
BOARD_TOKEN=$T BOARD_HUMAN_TOKEN=$HT BOARD_DB=$D/b.db BOARD_POLICY=$D/policy.json BOARD_PORT=$P \
  python3 "$W/board.py" >"$D/log" 2>&1 &
SRV=$!; trap 'kill $SRV 2>/dev/null; rm -rf "$D"' EXIT
for _ in $(seq 40); do curl -sf "http://127.0.0.1:$P/healthz" >/dev/null && break; sleep .1; done
A() { curl -s -H "Authorization: Bearer $T" -H 'Content-Type: application/json' "$@"; }
B="http://127.0.0.1:$P"
fail=0
ok() { printf '  ok   %s\n' "$1"; }
no() { printf '  FAIL %s\n' "$1"; fail=1; }

# a little data, so the pages render something other than empty states
A -X POST "$B/api/v1/projects" -d '{"project":"demo","phase":"build","goal":"a trial run"}' >/dev/null
AG=$(A -X POST "$B/api/v1/agents" -d '{"project":"demo","model":"claude-opus-5","harness":"claude-code","capabilities":["forgejo"],"host":"h","session":"s"}' | jq -r .id)
A -X POST "$B/api/v1/agents/$AG/heartbeat" -d '{"ctx_pct":42,"budget":[{"window":"5h","used_pct":58},{"window":"7d","used_pct":57}]}' >/dev/null
TID=$(A -X POST "$B/api/v1/tasks" -d '{"project":"demo","title":"a task to look at","repo":".","spec":"## What this is\n\nThe spec is the one long piece of text on a task.\n\n- it renders as markdown\n- with `code` and **bold**\n\n```\nboard task show T-1\n```","agent":"'"$AG"'"}' | jq -r .id)
A -X POST "$B/api/v1/tasks/$TID/claim" -d '{"agent":"'"$AG"'"}' >/dev/null
QID=$(A -X POST "$B/api/v1/questions" -d '{"project":"demo","task":"'"$TID"'","text":"Should we use daisyUI?","kind":"product","default_answer":"yes","deadline":"8h","agent":"'"$AG"'"}' | jq -r .id)
TQ=$(A -X POST "$B/api/v1/questions" -d '{"project":"demo","task":"'"$TID"'","text":"Does the page look right?","agent":"'"$AG"'"}' | jq -r .id)
# A second project, because the complaint that drove this redesign was "I cannot see what
# belongs to which project", and one project cannot exercise the answer.
A -X POST "$B/api/v1/projects" -d '{"project":"other","phase":"live","goal":"a second project"}' >/dev/null
AG2=$(A -X POST "$B/api/v1/agents" -d '{"project":"other","model":"grok-4","harness":"grok","host":"h2","session":"s2"}' | jq -r .id)
A -X POST "$B/api/v1/tasks" -d '{"project":"other","title":"work in the other project","agent":"'"$AG2"'"}' >/dev/null
# A task that has landed, so the check on hidden landed rows has something to measure —
# and so the `landed` group is exercised at all.
DONEID=$(A -X POST "$B/api/v1/tasks" -d '{"project":"demo","title":"a task that already landed","repo":".","agent":"'"$AG"'"}' | jq -r .id)
A -X POST "$B/api/v1/tasks/$DONEID/claim" -d '{"agent":"'"$AG"'"}' >/dev/null
A -X POST "$B/api/v1/tasks/$DONEID/done" -d '{"agent":"'"$AG"'","no_merge":true}' >/dev/null
# A second question on the SAME task: one question cannot show whether two of them stack
# or draw on top of each other.
A -X POST "$B/api/v1/questions" -d '{"project":"demo","task":"'"$TID"'","text":"And should the second question sit under the first? See [the RFC](https://example.com/rfc).","default_answer":"yes","deadline":"8h","agent":"'"$AG"'"}' >/dev/null

CSSURL=$(python3 - <<PY
import hashlib
b=open("$W/board.css","rb").read()
print("/board.%s.css" % hashlib.sha256(b).hexdigest()[:12])
PY
)

for pth in "/status" "/q/$QID" "/t/$TID"; do
  H=$(curl -s -o "$D/body" -w '%{http_code}' -H "Authorization: Bearer $T" -D "$D/hdr" "$B$pth")
  [ "$H" = 200 ] || { no "$pth answered $H"; continue; }
  grep -q "<link rel=stylesheet href='$CSSURL'>" "$D/body" \
    && ok "$pth 200, links $CSSURL" || no "$pth is missing the link to $CSSURL"
  grep -qi "Content-Security-Policy: default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'sha256-.*'; form-action 'self'" "$D/hdr" \
    && ok "$pth CSP widened by 'self' and nothing else" || { no "$pth CSP:"; grep -i content-security "$D/hdr"; }
  grep -q "<style" "$D/body" && no "$pth still has an inline <style> block" || ok "$pth has no inline <style>"
done

# the question page must show the deadline note
curl -s -H "Authorization: Bearer $T" "$B/q/$QID" | grep -q "the agent carries on" \
  && ok "/q shows what the board answers if the deadline runs out" || no "/q is missing the deadline note"
# The confirmation page must lead somewhere. Landing on a dead end means finding your way
# back by typing a URL on a phone.
SAVED=$(curl -s -H "Authorization: Bearer $HT" -H 'Content-Type: application/x-www-form-urlencoded' \
        -X POST -d 'answer=ok' "$B/q/$TQ/answer")
grep -q "href='/status'" <<<"$SAVED" \
  && ok "the answer confirmation links back to the board" \
  || no "the confirmation page is a dead end" "$(head -c 160 <<<"$SAVED")"
# the meter's ceiling line must be there, with its number
curl -s -H "Authorization: Bearer $T" "$B/status" | grep -q "class=track" \
  && ok "/status renders the meters" || no "/status is missing the meters"
curl -s -H "Authorization: Bearer $T" "$B/status" | grep -qE "<u data-c='(85|60)'" \
  && ok "/status draws the ceiling line with its ceiling" || no "/status is missing the ceiling line"
curl -s -H "Authorization: Bearer $T" "$B/status" | grep -q "working." \
  && ok "/status opens with the watch line" || no "/status is missing the watch line"

# --- the operator's surface --------------------------------------------------
# Every control on the board is drawn only for the human token and enforced again on the
# server. Both halves are checked: markup an agent can see is markup an agent will press,
# and a control the human CANNOT see is a feature that does not exist.
curl -s -H "Authorization: Bearer $HT" "$B/status" > "$D/human.html"
curl -s -H "Authorization: Bearer $T"  "$B/status" > "$D/agent.html"
grep -q "class='rail'" "$D/human.html" \
  && ok "/status carries one filter rail for the whole board" || no "/status has no filter rail"
grep -q "name='ceiling\." "$D/human.html" \
  && ok "the ceiling is a handle the owner can drag" || no "/status has no ceiling handle"
# Not `data-ceiling`: the script that drives the handle is served on EVERY page and
# mentions it, so that grep passes on a page with no handle at all.
grep -q "name='ceiling\." "$D/agent.html" \
  && no "an agent is shown a ceiling handle it cannot use" \
  || ok "an agent sees the ceiling drawn, not a handle"
grep -q "name='priority'" "$D/human.html" \
  && ok "the queue's priority can be set in place" || no "the queue has no priority control"
grep -q "name='estimate'" "$D/human.html" \
  && ok "the queue's size can be set in place" || no "the queue has no size control"
grep -q "name='priority'" "$D/agent.html" \
  && no "an agent is shown the owner's queue controls" || ok "an agent sees the queue read-only"
# Who coordinates is the owner's call (§3.8). `board role pin` was the only way to say
# so; the button has to reach the same handler and refuse the same way.
grep -q "action='/roles/coordinator/pin'" "$D/human.html" \
  && ok "the owner can pin a coordinator from the board" || no "no role control on /status"
grep -q "action='/roles/coordinator/" "$D/agent.html" \
  && no "an agent is shown the role control" || ok "an agent cannot pin a role from the board"
# A form that reaches a handler without its fields must answer 400, not raise into the
# 500 handler.
grep -q '"error"' <<<"$(curl -s -H "Authorization: Bearer $HT" -H 'Accept: application/json' \
  -H 'Content-Type: application/x-www-form-urlencoded' -X POST -d 'project=demo' \
  "$B/roles/coordinator/pin")" \
  && ok "pinning with no agent says which field is missing" \
  || no "a missing form field is not a 400"
# A question belongs with the task it is about, at that task's place in the order. It
# used to be a sibling row keyed by data-for; it now renders INSIDE the task's own
# article, which is why the old grep no longer describes the behaviour it was checking.
python3 - "$D/human.html" <<'QPY' && ok "a question renders inside the task it is about" \
  || no "the question is not attached to its task"
import re, sys
h = open(sys.argv[1]).read()
arts = re.findall(r"<article class='tk-row.*?</article>", h, re.S)
sys.exit(0 if any("qrow" in a for a in arts) else 1)
QPY
# Pressing an owner's control with an agent cookie must answer a PAGE, not a JSON blob on
# a phone.
REF=$(curl -s -H "Authorization: Bearer $T" -H 'Content-Type: application/x-www-form-urlencoded' \
      -X POST -d 'ceiling.5h=99&project=demo' "$B/projects/demo/ceilings")
grep -q "signed in as an" <<<"$REF" \
  && ok "an agent pressing an owner's control gets a page that says why" \
  || no "the refusal is not a page" "$(head -c 120 <<<"$REF")"
# `back` is a path this board serves, or it is dropped. http.server's send_header does
# not sanitize, so a CR or LF that got this far would be a response header the caller
# wrote. The check reads the RAW headers: the redirect must go to the default and no
# X-Injected must exist anywhere in them.
INJ=$(curl -s -o /dev/null -D - -H "Authorization: Bearer $HT" \
      -H 'Content-Type: application/x-www-form-urlencoded' -X POST \
      --data-urlencode 'back=/status
X-Injected: yes' --data 'priority=70' "$B/t/$TID/patch")
grep -qi 'x-injected' <<<"$INJ" \
  && no "a newline in back wrote a response header" \
  || ok "a newline in back is dropped, not written into the headers"
grep -qi "^Location: /t/$TID" <<<"$INJ" \
  && ok "a back that is not a path on this board falls back to the task" \
  || no "the refused back did not fall back" "$(grep -i location <<<"$INJ")"
OPEN=$(curl -s -o /dev/null -D - -H "Authorization: Bearer $HT" \
       -H 'Content-Type: application/x-www-form-urlencoded' -X POST \
       -d 'back=//example.com&priority=71' "$B/t/$TID/patch")
grep -qi "^Location: //" <<<"$OPEN" \
  && no "back accepted a protocol-relative URL — that is an open redirect" \
  || ok "back refuses a URL with a host"
# --- the control room -------------------------------------------------------
# What happened overnight is drawn, not just what is true now. The board carried no time
# at all before this; the event log held the answer and was never shown.
grep -q "class='night-track'" "$D/human.html" \
  && ok "the night band draws the last hours" || no "/status has no night band"
# One project, one band, with its name in a heading that sticks. This is the fix for
# "I cannot see what belongs to which project" — the complaint that drove the redesign.
test "$(grep -c "class='band-head'" "$D/human.html")" = "$(grep -c "class='band pj'" "$D/human.html")" \
  && ok "every project band carries its own heading" || no "a band is missing its heading"
# The queue is grouped by what is happening to the work, not flattened into one table.
grep -q "class='grp grp-flight'\|class='grp grp-stopped'\|class='grp grp-waiting'" "$D/human.html" \
  && ok "the queue is grouped by what is happening" || no "the queue is not grouped"
# Permissions are chips: held is on, available is off, and both are present so you can
# see what EXISTS. The datalist-and-button form could only show what had been typed in.
grep -q "class='chip chip-on'" "$D/human.html" && grep -q "class='chip chip-off'" "$D/human.html" \
  && ok "permissions show what is held and what is available" \
  || no "the permission chips do not show both states"
grep -q "chip chip-off" "$D/agent.html" \
  && no "an agent is shown permissions it can switch on" \
  || ok "an agent sees only the permissions that are held"
# The theme switch is on EVERY page, because the header is, and its script is admitted by
# its own hash — a second inline script means a second hash, and forgetting one is a
# silent CSP block, not an error anyone sees.
THEMEOK=1
for pth in "/status" "/t/$TID" "/q/$QID"; do
  curl -s -H "Authorization: Bearer $HT" "$B$pth" | grep -q "data-theme-set='dark'" \
    || { no "$pth has no theme switch"; THEMEOK=0; }
done
# The ok belongs inside the result, not after the loop: it used to print unconditionally
# and claim success on the very page it had just reported as missing the switch.
[ "$THEMEOK" = 1 ] && ok "the theme switch is on every page"
HASHES=$(curl -s -D - -o /dev/null -H "Authorization: Bearer $HT" "$B/status" \
         | grep -i '^content-security-policy' | grep -o "sha256-" | wc -l)
[ "$HASHES" = 2 ] && ok "both inline scripts are admitted by hash" \
  || no "the CSP names $HASHES script hashes, expected 2 (theme + filters)"
# Landed rows must be hidden in the MARKUP, not only by the script at the end of <body>:
# up to thirty of them per project would otherwise paint and then vanish, and stay for
# good wherever the script does not run.
python3 - "$D/human.html" <<'LPY' && ok "landed rows are hidden in the markup, not only by script" \
  || no "a landed row would paint before the script hides it"
import re, sys
h = open(sys.argv[1]).read()
rows = re.findall(r"<article class='tk-row[^>]*data-status='done'[^>]*>", h)
sys.exit(0 if rows and all("display:none" in r for r in rows) else 1)
LPY
# <form> is not allowed inside <p>. The browser closes the paragraph at the form's start
# tag and re-parents the form as a sibling — so a control laid out inside a phrase ends up
# on a line of its own, and NOTHING in the served markup shows it. It cost a real
# debugging cycle on the review limit; this catches the whole class from the source.
python3 - "$D/human.html" "$D/agent.html" <<'PPY' && ok "no form is nested inside a paragraph" \
  || no "a <form> sits inside a <p> — the browser will re-parent it"
import re, sys
bad = []
for f in sys.argv[1:]:
    h = open(f).read()
    for m in re.finditer(r"<p\b[^>]*>(.*?)</p>", h, re.S):
        if "<form" in m.group(1):
            bad.append(f + ": " + m.group(0)[:80])
if bad:
    print("\n".join(bad))
sys.exit(1 if bad else 0)
PPY
# A comment is the only thing on a task page that a PERSON wrote. It is stored under a
# different field from every other event's words, and the timeline used to read only the
# other one — so it rendered as a bare "task.comment <who>".
curl -s -H "Authorization: Bearer $HT" -H 'Content-Type: application/x-www-form-urlencoded' \
  -X POST --data-urlencode 'text=a comment with **marks** in it' "$B/t/$TID/comment" >/dev/null
CMTH=$(curl -s -H "Authorization: Bearer $HT" "$B/t/$TID")
grep -q "a comment with <b>marks</b> in it" <<<"$CMTH" \
  && ok "a comment reaches the timeline, rendered" \
  || no "the comment text is missing from the timeline"
# The compact question under a task row IS a link. An <a> from the question's own text
# closes it at the start tag, so the rest of the text and the badge fall out of the row's
# click target — and the target becomes wherever the agent pointed.
python3 - "$D/human.html" <<'APY' && ok "no link is nested inside the question row's own link" \
  || no "an <a> inside the .qrow link will break the row"
import re, sys
h = open(sys.argv[1]).read()
bad = [m.group(0)[:100] for m in re.finditer(r"<a class='qrow'.*?</a>", h, re.S)
       if "<a " in m.group(0)[len("<a class='qrow'"):]]
if bad:
    print("\n".join(bad))
sys.exit(1 if bad else 0)
APY
# The spec is the one long piece of text on a task and the board never showed it at all.
# Rendered, it is the page's answer to "what is this work"; flat, it was a grey wall.
SPECH=$(curl -s -H "Authorization: Bearer $HT" "$B/t/$TID")
grep -q "class='md md-spec'" <<<"$SPECH" && grep -q "<h4>What this is</h4>" <<<"$SPECH" \
  && grep -q "<pre><code>" <<<"$SPECH" \
  && ok "the spec renders as prose, with its headings and code" \
  || no "the spec is missing or flat"
# Preflight strips list markers and heading sizes to nothing, so prose inside .md needs
# every rule declared. A bullet with no marker is the failure this catches.
python3 - "$D/human.html" ../board.css <<'MPY' && ok "the prose styles survive the CSS reset" \
  || no "a prose element has no rule — preflight will have flattened it"
import re, sys
css = open(sys.argv[2]).read()
need = [".md ul", ".md ol", ".md li", ".md h3", ".md h4", ".md code", ".md pre",
        ".md blockquote", ".md p"]
missing = [n for n in need if n.replace(" ", " ") not in css and n.replace(" ", "") not in css]
if missing:
    print("no rule for: " + ", ".join(missing))
sys.exit(1 if missing else 0)
MPY
# The task page has to say what the task cost, or the estimate beside it means nothing.
curl -s -H "Authorization: Bearer $HT" "$B/t/$TID" | grep -q "what it has cost" \
  && ok "/t shows what the task has cost" || no "/t does not show the cost"

# the stylesheet itself
HC=$(curl -s -o "$D/css" -w '%{http_code}' -D "$D/chdr" -H "Authorization: Bearer $T" "$B$CSSURL")
[ "$HC" = 200 ] && ok "$CSSURL answers 200" || no "$CSSURL answered $HC"
grep -qi 'Content-Type: text/css' "$D/chdr" && ok "the stylesheet is text/css" || no "the stylesheet has the wrong Content-Type"
grep -qi 'Cache-Control: public, max-age=31536000, immutable' "$D/chdr" \
  && ok "the stylesheet is cached forever (the hash is in the filename)" || no "the stylesheet is missing the cache header"
cmp -s "$D/css" "$W/board.css" && ok "the stylesheet is byte for byte the checked-in one" || no "the stylesheet differs from board.css"
grep -qiE '@import|url\(\s*"?(https?:)?//' "$D/css" \
  && { no "the stylesheet fetches something over the network — the CSP blocks it silently"; } \
  || ok "the stylesheet fetches nothing over the network"
grep -qE 'prefers-color-scheme: ?dark' "$D/css" && ok "dark mode follows prefers-color-scheme" || no "no dark mode"

# --- no silently dead classes ------------------------------------------------
# A Tailwind class split across two Python string literals is never seen by the text
# extractor: the markup looks right, and the rule does not exist. The failure is mute.
# It caught `.pane` once and made the meters page-wide without anything complaining.
# Both identities: half the markup on this board (the gauges, the in-place fields, the
# phase pill) is drawn ONLY for the human token, and a class used only there would
# otherwise never reach this check — which is the exact failure it exists to catch.
for pth in "/status" "/q/$QID" "/t/$TID"; do
  curl -s -H "Authorization: Bearer $T" "$B$pth"
  curl -s -H "Authorization: Bearer $HT" "$B$pth"
done > "$D/all.html"
# Pages you only reach by POSTing a form were invisible to this check, and the
# confirmation page had been rendering unstyled because of it: it used a `.top` class
# that does not exist in the stylesheet, on the one page you land on after pressing the
# one button. Render them too, or the class checker keeps having a blind spot exactly
# where a human is looking.
printf '%s' "$SAVED" >> "$D/all.html"
U=$(python3 unknown-classes.py "$D/all.html" ../board.css)
if [ "$U" = "(none)" ]; then
  ok "every class in the markup exists in the stylesheet"
else
  no "classes used in the markup that were NOT built (split across string literals?):"
  printf '       %s\n' $U
fi

# --- real browser: no CSP violations, no console errors, no horizontal scrolling ---
if [ -d node_modules/playwright ]; then
  BR=$(NODE_PATH=node_modules node - "$B" "$T" "$TID" "$QID" <<'JS' 2>&1
const [B, T, TID, QID] = process.argv.slice(2);
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch(); let bad = 0;
  for (const scheme of ['light', 'dark']) {
    for (const [w, h, name] of [[1280, 900, 'wide'], [390, 844, 'phone']]) {
      const c = await b.newContext({ colorScheme: scheme, viewport: { width: w, height: h },
        extraHTTPHeaders: { Authorization: 'Bearer ' + T } });
      const p = await c.newPage();
      const errs = [];
      // favicon.ico is the browser's own spontaneous request, and the board has none. It
      // is not a fault in the pages.
      p.on('console', m => { if (m.type() === 'error' && !/favicon/.test(m.text())) errs.push(m.text()); });
      for (const path of ['/status', '/q/' + QID, '/t/' + TID]) {
        await p.goto(B + path, { waitUntil: 'networkidle' });
        if (await p.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1)) {
          console.log('  FAIL ' + path + ' ' + scheme + '/' + name + ' scrolls horizontally'); bad++;
        }
      }
      // Two questions on one task used to be placed into the SAME named grid area and
      // drew one over the other. Only layout can show that; the markup looks fine.
      if (scheme === 'light' && name === 'wide') {
        await p.goto(B + '/status', { waitUntil: 'networkidle' });
        const overlap = await p.evaluate(() => {
          for (const row of document.querySelectorAll('.tk-row')) {
            const qs = [...row.querySelectorAll('.qrow')];
            for (let i = 1; i < qs.length; i++) {
              const a = qs[i-1].getBoundingClientRect(), b = qs[i].getBoundingClientRect();
              if (b.top < a.bottom - 1) return 'rows ' + (i-1) + '/' + i + ' overlap';
            }
          }
          return null;
        });
        if (overlap) { console.log('  FAIL questions on one task overlap: ' + overlap); bad++; }
        else console.log('  ok   two questions on one task stack instead of overlapping');
      }
      // The rail filters the whole board, and the theme switch is a real setting. Both
      // are script, so only a browser can say whether they work.
      if (scheme === 'light' && name === 'wide') {
        await p.goto(B + '/status', { waitUntil: 'networkidle' });
        const vis = () => p.evaluate(() => [...document.querySelectorAll('.pj')]
          .filter(e => e.style.display !== 'none').map(e => e.dataset.p));
        if ((await vis()).length < 2) { console.log('  FAIL the fixture lost a project'); bad++; }
        await p.selectOption('[data-f=project]', 'other');
        const only = await vis();
        if (only.length === 1 && only[0] === 'other')
          console.log('  ok   choosing a project hides the others');
        else { console.log('  FAIL the project filter showed ' + JSON.stringify(only)); bad++; }
        await p.selectOption('[data-f=project]', 'all');
        await p.click('[data-theme-set=dark]');
        const forced = await p.evaluate(() => document.documentElement.getAttribute('data-theme'));
        await p.click('[data-theme-set=system]');
        const back = await p.evaluate(() => document.documentElement.getAttribute('data-theme'));
        if (forced === 'board-dark' && back === null)
          console.log('  ok   the theme switch forces a theme and hands it back to the system');
        else { console.log('  FAIL theme switch: forced=' + forced + ' back=' + back); bad++; }
      }
      if (errs.length) { console.log('  FAIL ' + scheme + '/' + name + ': ' + errs[0].slice(0, 160)); bad += errs.length; }
      else console.log('  ok   ' + scheme + '/' + name + ': no CSP violations or console errors, no horizontal scrolling');
      await c.close();
    }
  }
  await b.close();
  process.exit(bad ? 1 : 0);
})();
JS
) || fail=1
  echo "$BR"
else
  echo "  skip  playwright is not installed — run npm install for the browser check"
fi

echo; [ $fail = 0 ] && echo "board pages: all green" || echo "board pages: SOMETHING FAILED"
exit $fail
