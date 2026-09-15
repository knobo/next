# next · phase build

An agentic development loop plus a coordination board for several AI agents across several machines and several projects.

## Agents

| agent | status | model | ctx | quota | task |
|---|---|---|---|---|---|
| cc-fixnum-ae1a | dead | claude-opus-5 | 86.0 | 5h 80% 7d 48% |  |
| gk-fixnum-574b | dead | claude-sonnet-5 | 24.0 | 5h 4.0% 7d 14.000000000000002% |  |
| gk-fixnum-cb2f | dead | claude-opus-5 | 7.0 | 5h 3.0% 7d 35.0% |  |
| cc-fixnum-76d4 | alive | claude-opus-5[1m] | 22.0 | 5h 17% 7d 42% |  |
| cc-fixnum-ba07 | dead | claude-sonnet-5 |  |  |  |
| cc-fixnum-cb5f | dead | claude-sonnet-5 |  |  |  |
| cc-fixnum-e267 | dead | claude-sonnet-5 |  |  |  |
| cc-fixnum-8086 | dead | claude-sonnet-5 |  |  |  |
| cc-fixnum-e8d2 | dead | claude-opus-5 | 38.0 | 5h 26% 7d 48% |  |
| cx-fixnum-d1bd | dead | gpt-5.6-sol | 10.0 |  |  |
| cx-fixnum-df85 | dead | gpt-5.6-sol |  |  |  |
| cc-fixnum-e27c | dead | claude-sonnet-5 |  |  |  |
| cc-fixnum-d2d3 | dead | claude-sonnet-5 |  |  |  |
| cc-fixnum-c2b2 | dead | claude-sonnet-5 |  |  |  |
| cc-fixnum-3bac | dead | claude-sonnet-5 |  |  |  |
| cc-fixnum-580a | dead | sonnet | 22.0 |  |  |
| cc-fixnum-37d7 | dead | sonnet | 12.0 |  |  |
| cc-fixnum-075a | dead | claude-opus-5 |  |  |  |
| ag-fixnum-0a42 | dead | gemini-3.8-flash |  |  |  |
| ag-fixnum-c93b | dead |  |  |  |  |
| cc-fixnum-9436 | dead | claude-sonnet-5 |  |  |  |
| cc-fixnum-f427 | dead | claude-sonnet-5 |  |  |  |
| ag-fixnum-a105 | stale | gemini-3.8-flash |  |  |  |
| gk-fixnum-0e54 | alive | grok |  |  | T-396 |
| ag-fixnum-7678 | alive | gemini-3.8-flash |  |  |  |

## Tasks

| id | status | repo | owner | title |
|---|---|---|---|---|
| T-349 | in_review | . |  | alle som bruker next skill må huske på å lage en plan før de implementerer. Hoved-agenten må lage en god plan, men så skal også subagentene lage sin egen plan før de implementerer sin bit. Det må være med i skillen. |
| T-351 | in_review | . |  | Alle som bruker tavla må teste alt selv. De kan bruke cli eller playwirght eller test-kode for å teste at ting virker. Jeg skal teste i dev eller prod etter at det er deployet. Ikke noe mellomstadie. |
| T-282 | in_review | . |  | gaten kan ikke passeres fra én økt: SKILL steg 8 lar koordinatoren sette review-resultatet, men guarden avviser eieren — og en subagent som registrerer seg får eierens egen id fordi BOARD_SESSION arves |
| T-259 | in_review | . |  | test-level lager menneske-kort der det ikke finnes menneskelig skjønn å gi |
| T-260 | blocked | . |  | spec-vedlegg T-259 |
| T-283 | in_review | . |  | board task deploy deployer hovedarbeidskopien, ikke det som ble merget: $MANIFEST løser worktreet til $ROOT, og produksjon fikk gamle filer med grønt rollout |
| T-278 | open | . |  | tavla merker ikke at en agent jobber i stillhet — regelen «board task progress etter hvert steg» har ingen forsterkning, og et kvarters funn fantes bare i chatten |
| T-284 | open | . |  | /status er 16776 px høy fordi 12 av 13 agentrader er døde: en død agents ctx-måler med takstrek tegner en presisjon som ikke er sann, og skyver oppgavelista langt ned |
| T-289 | open | . |  | board help skriver rå shell-kildekode i stedet for hjelp, og lister kommandonavn uten å si hva de gjør — 16 task-subkommandoer er usynlige |
| T-277 | open | . |  | en oppgave som venter på et menneskesvar har ingen tilstand å hvile i: release setter awaiting_human tilbake til open, og oppgaven blir blokkert på nytt |
| T-279 | open | . |  | SKILL.md steg 5 og 8 hardkoder ~/.claude/skills/next/prompts/ — en codex- eller grok-agent sendes til en sti som bare finnes fordi Claude også er installert på denne maskinen |
| T-287 | open | . |  | test-level ser bare på filendelse, så tavla rater sin EGEN UI-endring som auto: board.py rendrer HTML fra Python-strenger, og en lengre endelsesliste gjør det motsatte galt |
| T-281 | open | . |  | testkortets url og PR-lenka valideres ikke for skjema: en agent kan lage en klikkbar javascript:-lenke på flatene Knut åpner fra ntfy — inert i dag bare fordi script-src er pinnet til én hash |
| T-354 | open | . |  | en kommentar fra Knut på en blocked oppgave gjør ingenting — kortet står, og han er registrert som «ukjent» |
| T-350 | open | . |  | board task pr bygger ugyldig forge-slug når repo er '.' — ingen PR kan opprettes i ettrepo-prosjekter |
| T-390 | orphaned | . |  | board open --qr gir maskin-token i en agent-økt, og hverken CLI-en eller sidene sier hvilken identitet du har |
| T-352 | open | . |  | riv ut mellomstadiet: test-level, testkøen og menneske-OK i gaten skal bort fra koden |
| T-388 | open | . |  | ntfy-pusher går tapt i stillhet: ingen retry på transient feil, og ingen teller som viser at noe forsvant |
| T-396 | claimed | . | gk-fixnum-0e54 | board: task.progress refuses notes/dispatch tokens on done/archived tasks — history cannot be amended |
| T-190 | blocked | . |  | finished() ntfy-er på hver eneste øktavslutning — 'kø tom' er den vanlige stien, så den ene pushen som betyr noe drukner |
| T-70 | blocked | next |  | statuslinje-heartbeat hvert 1-3 sek gir 85% av hendelsesloggen og et gpg-kall per render — terskel på 30s |
| T-357 | open | . |  | conformance.sh flakker på to reaper-sjekker: 'utløpt lease er stalled tross levende heartbeat' og 'stalled agent får ikke ny oppgave' |
| T-389 | open | . |  | ntfy-varsler om én oppgave lenker til /status i stedet for til oppgaven, og «agent finished» lenker ingen steder |
| T-290 | orphaned | . |  | board task deploy må kjøre fra commitens worktree, ikke callerens utdaterte hovedarbeidskopi |
| T-191 | open | . |  | koordinatorens egen kostnad per oppgave (DEL 2 av T-188) — statuslinjefeltene finnes, men total_input_tokens er feil felt å differensiere |

## Open questions



## The board answered itself (can be overridden)

- Q-201 (question) ntfy-kanarifugl: kom dette varselet fram på telefonen din? → ja
