# next · phase build

An agentic development loop plus a coordination board for several AI agents across several machines and several projects.

## Agents

| agent | status | model | ctx | quota | task |
|---|---|---|---|---|---|
| cc-fixnum-ae1a | dead | claude-opus-5 | 86.0 | 5h 80% 7d 48% |  |
| gk-fixnum-574b | dead | claude-sonnet-5 | 24.0 | 5h 4.0% 7d 14.000000000000002% |  |
| gk-fixnum-cb2f | dead | claude-opus-5 | 7.0 | 5h 3.0% 7d 35.0% |  |
| cc-fixnum-76d4 | alive | claude-opus-5[1m] | 53.0 | 5h 65% 7d 87% |  |
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
| ag-fixnum-0a42 | dead | gemini-3.8-flash |  |  |  |
| ag-fixnum-c93b | dead |  |  |  |  |
| cc-fixnum-9436 | dead | claude-sonnet-5 |  |  |  |
| cc-fixnum-f427 | dead | claude-sonnet-5 |  |  |  |
| ag-fixnum-a105 | dead | gemini-3.8-flash |  |  |  |
| gk-fixnum-0e54 | dead | grok |  |  |  |
| ag-fixnum-7678 | dead | gemini-3.8-flash |  |  |  |
| gk-fixnum-ccfd | dead |  |  |  |  |
| cc-fixnum-e862 | dead | claude-sonnet-5 |  |  |  |
| gk-fixnum-d1e4 | dead | grok |  |  |  |
| ag-fixnum-9b00 | dead | gemini-3.8-flash |  |  |  |
| ag-fixnum-a755 | dead |  |  |  |  |
| cc-fixnum-1a7d | alive | claude-opus-5 |  |  | T-191 |
| ag-fixnum-39bf | dead |  |  |  |  |
| ag-fixnum-e596 | dead |  |  |  |  |
| ag-fixnum-acf1 | dead |  |  |  |  |
| ag-fixnum-9fe2 | dead |  |  |  |  |

## Tasks

| id | status | repo | owner | title |
|---|---|---|---|---|
| T-70 | blocked | next |  | statuslinje-heartbeat hvert 1-3 sek gir 85% av hendelsesloggen og et gpg-kall per render — terskel på 30s |
| T-191 | blocked | . | cc-fixnum-1a7d | koordinatorens egen kostnad per oppgave (DEL 2 av T-188) — statuslinjefeltene finnes, men total_input_tokens er feil felt å differensiere |

## Open questions

- Q-256 (question) show

## The board answered itself (can be overridden)

- Q-201 (question) ntfy-kanarifugl: kom dette varselet fram på telefonen din? → ja
- Q-249 (question) T-428: hvordan skal taket stige mot 100 % når vinduet nærmer seg reset? → Lineær rampe: effektivt tak = tak + (100 - tak) * andel av vinduet som er brukt opp i tid, men bare de siste 25 % av vinduets varighet (før det gjelder taket uendret). Gjelder alle vinduer med resets_at; vinduer uten resets_at beholder fast tak.
