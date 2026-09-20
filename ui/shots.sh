#!/usr/bin/env bash
# Screenshots of all four pages, in light and dark mode, on a wide screen and on a phone.
# Fills its own instance with realistic data first — empty pages say nothing about how the
# density actually behaves.
#   cd ui && npm install && ./build.sh && ./shots.sh [outdir]
# Reports horizontal scrolling and console errors along the way; ui/verify.sh is the one
# that FAILS on them. This one is for looking.
set -uo pipefail
cd "$(dirname "$0")"
W="$(cd .. && pwd)"
OUT="${1:-$(mktemp -d)/board-shots}"; mkdir -p "$OUT"; rm -f "$OUT"/*.png
T=shot-$RANDOM; P=$((19000 + RANDOM % 900)); D=$(mktemp -d)
# The screenshots are taken AS THE OWNER. Half the surface this exists to look at — the
# ceiling handles, the priority and size fields, pause, phase — only renders for the human
# token, so a shot taken with the agent token shows a board with its controls missing and
# nothing to say that is why.
HT=shothum-$RANDOM
cat > "$D/policy.json" <<'POL'
{"budget": {"*": {"ceilings": {"5h": 85, "7d": 60}, "fallback": {"max_tasks": 12}}},
 "grants": {"board": {"*": ["merge", "deploy-dev"]},
            "shopfront": {"*": ["deploy-dev"]}}}
POL
BOARD_TOKEN=$T BOARD_HUMAN_TOKEN=$HT BOARD_DB=$D/b.db BOARD_POLICY=$D/policy.json BOARD_PORT=$P \
  python3 "$W/board.py" >"$D/log" 2>&1 &
SRV=$!; trap 'kill $SRV 2>/dev/null' EXIT
for _ in $(seq 40); do curl -sf "http://127.0.0.1:$P/healthz" >/dev/null && break; sleep .1; done
B="http://127.0.0.1:$P"; A(){ curl -s -H "Authorization: Bearer $T" -H 'Content-Type: application/json' "$@"; }

# Two invented projects. Nothing here is anyone's real deployment: the point is density,
# not data.
A -X POST "$B/api/v1/projects" -d '{"project":"board","phase":"build","goal":"a user-global agentic development loop and coordination service for several AI agents on several machines"}' >/dev/null
A -X POST "$B/api/v1/projects" -d '{"project":"shopfront","phase":"live","goal":"customer portal"}' >/dev/null
mk(){ A -X POST "$B/api/v1/agents" -d "{\"project\":\"$1\",\"model\":\"$2\",\"harness\":\"$3\",\"host\":\"demo-host\",\"session\":\"s$RANDOM\",\"capabilities\":[\"forgejo\",\"kubectl-dev\"]}" | jq -r .id; }
A1=$(mk board claude-opus-5 claude-code); A2=$(mk board claude-sonnet-5 claude-code)
A3=$(mk shopfront grok-4 grok)
A -X POST "$B/api/v1/agents/$A1/heartbeat" -d '{"ctx_pct":63,"budget":[{"window":"5h","used_pct":81},{"window":"7d","used_pct":57}]}' >/dev/null
A -X POST "$B/api/v1/agents/$A2/heartbeat" -d '{"ctx_pct":12,"budget":[{"window":"5h","used_pct":34},{"window":"7d","used_pct":57}]}' >/dev/null
A -X POST "$B/api/v1/agents/$A3/heartbeat" -d '{"ctx_pct":88,"budget":[{"window":"5h","used_pct":92},{"window":"7d","used_pct":61}]}' >/dev/null
A -X PUT "$B/api/v1/agents/$A1/preference" -d '{"role":"coordinator"}' >/dev/null 2>&1
curl -s -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -X POST "$B/api/v1/roles" -d "{\"project\":\"board\",\"role\":\"coordinator\",\"agent\":\"$A1\"}" >/dev/null 2>&1

t(){ A -X POST "$B/api/v1/tasks" -d "{\"project\":\"$1\",\"title\":\"$2\",\"repo\":\"$3\",\"risk\":\"$4\",\"priority\":$5,\"estimate\":$6,\"agent\":\"$A1\"}" | jq -r .id; }
T1=$(t board "the board gets a UI framework: Tailwind + daisyUI, built in and checked in" . normal 99 13)
T2=$(t board "the merge gate no longer waits on a human test" . normal 96 3)
T3=$(t board "the coordinator's own cost per task" . low 45 5)
T4=$(t board "a statusline heartbeat every 1-3 s captures 85% of the events" . low 30 2)
T5=$(t shopfront "per-item try/catch in the scheduled sweep" api high 80 1)
T6=$(t board "the merge mutex is released once the merge has landed" . normal 60 8)
A -X POST "$B/api/v1/tasks/$T1/claim" -d "{\"agent\":\"$A1\"}" >/dev/null
A -X POST "$B/api/v1/tasks/$T1/progress" -d "{\"agent\":\"$A1\",\"note\":\"model=opus Q1=y Q3=n — the acceptance criterion is a command, but the CSP makes the choice of framework more than a one-repo change\",\"dispatch\":\"implementer:sonnet\"}" >/dev/null
A -X POST "$B/api/v1/tasks/$T1/progress" -d "{\"agent\":\"$A1\",\"note\":\"stylesheet built and checked in, 47 kB, no network references\",\"dispatch\":\"implementer:sonnet\",\"tokens\":84213,\"result\":\"four pages rebuilt, conformance green\"}" >/dev/null
A -X POST "$B/api/v1/tasks/$T1/progress" -d "{\"agent\":\"$A1\",\"pr\":\"http://forge.example.com/demo/board/pulls/42\",\"status\":\"in_review\"}" >/dev/null
A -X POST "$B/api/v1/tasks/$T1/review" -d "{\"agent\":\"$A1\",\"open\":1,\"fixed\":3}" >/dev/null
A -X POST "$B/api/v1/tasks/$T1/progress" -d "{\"agent\":\"$A1\",\"dispatch\":\"reviewer:opus\",\"tokens\":21870,\"result\":\"3 findings, 1 blocking\"}" >/dev/null
A -X POST "$B/api/v1/tasks/$T6/claim" -d "{\"agent\":\"$A2\"}" >/dev/null
A -X POST "$B/api/v1/tasks/$T6/progress" -d "{\"agent\":\"$A2\",\"dispatch\":\"implementer:sonnet\",\"tokens\":9100,\"result\":\"green\"}" >/dev/null
A -X POST "$B/api/v1/tasks/$T3/blocked" -d "{\"agent\":\"$A1\",\"note\":\"waiting on a human design decision: total_input_tokens is the context window size and GOES DOWN on compaction, so the difference claimed→done is meaningless. The only cumulative field is cost.total_cost_usd, which breaks the vendor neutrality T-164 introduced.\"}" >/dev/null
A -X POST "$B/api/v1/tasks/$T5/claim" -d "{\"agent\":\"$A3\"}" >/dev/null
QP=$(A -X POST "$B/api/v1/questions" -d "{\"project\":\"board\",\"task\":\"$T3\",\"kind\":\"product\",\"text\":\"Should the board measure the coordinator's own share in USD (cumulative, but only one harness reports it), or should the task be closed as unbuildable?\",\"default\":\"a unit-tagged optional field\",\"options\":[\"usd\",\"close\"],\"deadline\":\"8h\",\"agent\":\"$A1\"}" | jq -r .id)
QD=$(A -X POST "$B/api/v1/questions" -d "{\"project\":\"board\",\"task\":\"$T4\",\"kind\":\"question\",\"text\":\"Should the heartbeat fire on every statusline render, or be throttled to every 20 seconds?\",\"default\":\"throttle to 20s\",\"deadline\":\"1s\",\"agent\":\"$A1\"}" | jq -r .id)
echo "QP=$QP QD=$QD"; sleep 2; curl -s "$B/status" -H "Authorization: Bearer $HT" >/dev/null

cat > "$D/shot.js" <<JS
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const pages = [['status','/status'],['task','/t/$T1'],['q','/q/$QP'],['blocked','/t/$T3']];
  for (const scheme of ['light','dark']) {
    for (const vp of [[1280,900,'wide'],[390,844,'phone']]) {
      const c = await b.newContext({colorScheme: scheme, viewport: {width: vp[0], height: vp[1]},
        deviceScaleFactor: 2, extraHTTPHeaders: {Authorization: 'Bearer $HT'}});
      const p = await c.newPage();
      const errs = [];
      p.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
      for (const [name, path] of pages) {
        await p.goto('$B' + path, {waitUntil: 'networkidle'});
        if (path === '/status') for (const d of await p.\$\$('details')) await d.evaluate(e => e.open = true);
        await p.screenshot({path: '$OUT/' + name + '-' + scheme + '-' + vp[2] + '.png', fullPage: true});
        const ow = await p.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
        if (ow) console.log('HORIZONTAL SCROLLING: ' + path + ' ' + scheme + ' ' + vp[2]);
      }
      if (errs.length) console.log('CONSOLE ERROR ' + scheme + ' ' + vp[2] + ': ' + errs.join(' | '));
      await c.close();
    }
  }
  await b.close();
  console.log('screenshots done');
})();
JS
NODE_PATH=node_modules node "$D/shot.js"
ls "$OUT"
