## 4. API / protocol

Everything is `Authorization: Bearer <token>`, JSON. Base `$BOARD_URL/api/v1`. The `board`
CLI (bash + curl, on `$PATH` on every machine) is a 1:1 wrapper and what the agents actually
use. **CLI rule K6:** when the server does not answer within 2 s, the CLI appends the call to
`~/.cache/board/<board>/outbox.jsonl`, returns exit 0 with `{"queued":true}` and flushes the
outbox on the next successful call. The whole cache — agent id, GET answers and outbox — lives
under a directory keyed on `BOARD_URL`, so a local test board cannot hijack the identity you
hold against the production board and cannot receive its queued writes. Reads return the last
known answer from the cache with `"stale":true`. **The agent always carries on.**

| Operation | CLI | HTTP | Semantics |
|---|---|---|---|
| Register | `board register --project myproj --harness claude-code --host laptop --model fable --cap browser-test,kubectl-dev` | `POST /agents` → `{id: "cc-laptop-7f3a"}` | The board sets `grants` from the policy. Idempotent on `session`. |
| Heartbeat | `board heartbeat [--ctx-pct 41 --budget '[{"window":"5h","used_pct":63}]']` | `POST /agents/{id}/heartbeat` | Renews `last_seen`, but **not** the lease on `current_task` — only claim and `task.progress` do that (Q-107). A process that has sat without progress longer than the lease is `stalled`, and the reaper hands its task on. |
| Token report | (part of the heartbeat) | same | `ctx_pct` and `budget`: a LIST `[{window, used_pct, resets_at?}]`. An empty list is a valid, honest value ("do not know"); anything that is not a list is refused with 400. Old `rl5_pct`/`rl5_reset`/`rl7_pct` are translated to `5h`/`7d` at the edge (T-164). |
| Set capabilities | `board caps --add playwright` | `PUT /agents/{id}/capabilities` | Emits `agent.capabilities_set`. |
| Finished | `board finished [--reason "queue empty" \| "quota 95%"]` | `POST /agents/{id}/finished` | Releases leases. The coordinator may then end the process. |
| Create task | `board task create --repo web --title ... --spec-file t.md --requires browser-test --risk high --touches 'src/routes/**'` | `POST /tasks` | Any agent may do this (a finding → a new task for somebody else). |
| Next task | `board task next [--repo X]` | `GET /tasks/next?agent=…` | The highest priority among `open` that the agent qualifies for and whose `touches` do not overlap a `claimed` task in the same repo. |
| Claim | `board task claim T-42` | `POST /tasks/T-42/claim` | CAS; 409 if taken. Sets `lease_until`. |
| Progress | `board task progress T-42 --worktree … --branch … --pr web#201 "review run, 2 findings"` | `POST /tasks/T-42/progress` | Free text plus structured fields. **Everything another agent needs to take over.** Renews the lease. If the task was reaped from you, you get a 409 — claim it again. |
| Review result | `board task review T-42 --open 0 --fixed 2` | `POST /tasks/T-42/review` | Gate input. The gate refuses a review set by the task's own owner, and needs `--open 0` from every non-owner reviewer in the round. The owner's `--round` (after a fix) starts a new round; so does a claim by a new owner. |
| Gate | `board gate merge T-42` → exit 0/1 + a JSON reason | `GET /tasks/T-42/gate/merge` | Checks: review `open==0` (skipped in `idea` phase), at least one fresh reviewer (not the task's own owner) in the current round, the merge mutex, and the grant. No human sign-off gates the merge — the agent verifies itself, the human tests what is deployed. |
| Merge markers | `board task merging T-42` / `board task merged T-42 --sha abc` | `POST …/merge_requested`, `…/merge_verified` | Two events around the merge itself (§7c). `merging` refuses while the task's working copy is dirty; `merged` refuses a sha that is not on `origin/main`. |
| Task done | `board task done T-42` | `POST /tasks/T-42/done` | Requires `merge_verified` (or `--no-merge` for docs-only), and a deploy event when the manifest declares one. |
| Release | `board task release T-42 --note "out of tokens"` | `POST /tasks/T-42/release` | Back to `open` with all context preserved. An `in_review` task keeps its status. |
| Ask | `board ask --task T-42 --default "B" --deadline 8h "Should X be A or B?"` → `{id: Q-9}` | `POST /questions` | **Returns immediately.** The board pushes a notification with a link to `/q/Q-9`. |
| Inbox | `board inbox` | `GET /agents/{id}/inbox` | Answered questions plus messages. The agent checks before each new task and after each subagent. |
| Answer | `board answer Q-9 "A"` (the human) | `POST /questions/Q-9/answer` | At the deadline with no answer: `question.defaulted` with `default_answer` — the agent carries on with the default and *marks the PR* "assumes A, Q-9 unanswered". |
| Message | `board message cc-other-1 --task T-42 "I changed the OpenAPI contract, regenerate types"` | `POST /messages` | Agent→agent. Shows up in the recipient's `inbox`. |
| Role: pin (the human) | `board role pin coordinator --me` / `--agent <id>`; `board role unpin coordinator` | `PUT /roles/{role}` `{agent, source:"pinned"}` / `DELETE` | Always beats an election while the holder is alive. Released on `dead`. Requires the human token. |
| Role: claim (agent) | `board role claim coordinator` | `POST /roles/{role}/claim` | CAS + ranking check (§3.8). 409 `{recommended: <id>}` on a loss. **Never queued in the outbox.** |
| Role: recommendation | `board role recommend [coordinator]` | `GET /roles/recommendation` | A deterministic ranking with its reason; the same answer for everyone. |
| Preference | `board caps --prefer implementer` | `PUT /agents/{id}/preference` | The agent's vote in the ranking. |
| Status | `board status [--project myproj]` | `GET /status` | Phase, agents, tasks, questions and roles per project. |
| Dispatch | `board task progress T-42 --dispatch reviewer:sonnet [--tokens 84213] [--result "3 findings"]` | `POST /tasks/{id}/progress` | `role:model`, validated — a malformed value is refused (400). `board task show` returns the chain as `dispatches` and the sum as `cost`. |
| Status for me | `board status --me` | `GET /status` | Your own agent row + `budget`: `ceilings` from the policy and `windows` = the HIGHEST `used_pct` per window name across all agents (quota is per account). **`.stop`** is the board's finished answer — non-null means stop, and the text is the reason. `budget.effective_ceilings` is the ceiling `.stop` actually compares against: over the last 25 % of a window before its `resets_at` it rises linearly toward 100 (unspent quota is lost at reset); windows without `resets_at` keep the policy ceiling. If the ceilings are missing from the policy the block carries `ceilings_missing: true` + `note`. If the agent cannot find itself in the answer the command fails (exit 2) rather than printing nothing. |
| Stream | `board tail --since 2h` | `GET /events?since=…&stream=…` | Reading the log; what the coordinator uses when resuming. Max 500, taken from the **end**; `truncated:true` + `from_id` says so when something is missing in the middle. |

**Example — a heartbeat body:**
```json
{"session":"9fe776e4-…","model":"claude-fable-5-1","ctx_pct":41.2,
 "budget":[{"window":"5h","used_pct":63,"resets_at":"2026-09-07T09:00:00Z"},
           {"window":"7d","used_pct":22}],"cwd":"/home/me/prog/myproj"}
```

**Example — a question and its answer:**
```json
POST /questions
{"project":"myproj","task":"T-42","kind":"question","agent":"cc-laptop-7f3a",
 "text":"Should the admin quota be unlimited or capped?",
 "default":"capped at 1000","options":["unlimited","capped"],"deadline":"8h"}
→ {"id":"Q-9","status":"open"}

GET /agents/cc-laptop-7f3a/inbox
→ {"questions":[{"id":"Q-9","status":"answered","answer":"unlimited",
                 "answered_by":"human"}],"messages":[]}
```
