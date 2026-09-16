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
{"budget": {"*": {"ceilings": {"5h": 85, "7d": 60}}}}
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
TID=$(A -X POST "$B/api/v1/tasks" -d '{"project":"demo","title":"a task to look at","repo":".","agent":"'"$AG"'"}' | jq -r .id)
A -X POST "$B/api/v1/tasks/$TID/claim" -d '{"agent":"'"$AG"'"}' >/dev/null
QID=$(A -X POST "$B/api/v1/questions" -d '{"project":"demo","task":"'"$TID"'","text":"Should we use daisyUI?","kind":"product","default_answer":"yes","deadline":"8h","agent":"'"$AG"'"}' | jq -r .id)
TQ=$(A -X POST "$B/api/v1/questions" -d '{"project":"demo","task":"'"$TID"'","text":"Does the page look right?","agent":"'"$AG"'"}' | jq -r .id)

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
for pth in "/status" "/q/$QID" "/t/$TID"; do
  curl -s -H "Authorization: Bearer $T" "$B$pth"
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
