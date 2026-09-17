---
name: next
description: Fully automatic development loop for any project. Use on `/next`, on `/next <role>` (coordinator|implementer|reviewer|tester), or on `/next "<free-text directive>"` which becomes the first task on the queue. Finds the project manifest, registers on the board, runs tasks end to end (worktree → subagent → review → gate → merge → verify → clean up) until the queue is empty or quota runs out. Never stops to ask — `board ask` and move on.
---

# /next

The argument is a role (`coordinator|implementer|reviewer|tester`, default coordinator) **or** free text. Free text is a directive, not a chat: finish Start below first (register mirrors the manifest, which `task create` validates `--repo` against), then `board task create --title "<the text>" --priority 99` verbatim, report the task id, and run the loop — it comes up first. A directive naming a skill (`/frontend-design`) stays in the spec; the implementation subagent invokes it.
Role ≠ coordinator → read `reference/roles.md`; the loop is the same, only which tasks you take and who types the code differ. As coordinator you do **not** type code: you dispatch it (step 6), then review, gate, merge, verify — context strategy, not rank, since implementation burns 50–150k tokens that die with the subagent while yours must survive the night. Everything you know goes on the board, never only in this chat. **The board is the memory.** `board help <cmd>` documents any command — do not guess.

## Start

1. One-step bootstrap: `eval "$(board start --sh [role])"` (or parse `board start [role]`) → runs project detection, capability probing, agent registration, role claim (coordinator with automatic fallback to implementer on 409), stop rule checks, and task discovery in ONE subsecond command.
   Alternatively step-by-step: `eval "$(board project detect)"` → ROOT PROJECT PHASE ENTRY REPOS FORGE_KIND ONBOARDING. No manifest or `$MANIFEST_WARN` → STOP and say `board project init` in the project root; do not create it yourself, `phase` decides the merge gate.
2. **Context budget rule: NEVER read `$ONBOARDING` directly into the coordinator/orchestrator context** (that permanently burns 10k–30k tokens!). If you must check tightened rules on start, dispatch a temporary research subagent (`invoke_subagent` / `Agent`) to summarize them in ≤5 lines. Otherwise pass `$ONBOARDING` to the implementer subagent in step 6 — implementation rules stay in the worker's context budget.
3. Role: already handled by `board start` (or `board role pin <role> --me` / `board role claim coordinator`).
4. Answers and messages: `board inbox` (handled in `board start`).
5. Queue hygiene & consolidation (coordinator): Run `board simplify [--apply]` regularly. It automatically:
   - Clusters related fragmented tasks (INNMELDING.md, counterless, web-stabilitet, infra, mobil, betaling) into consolidated packages, preserving all specs and shortening the dev cycle by 60–70%.
   - Closes and archives obsolete/retracted cards (`FAIL fra menneske-test`, `TRUKKET`) and completed tasks (`status: done` → `status: archived`), keeping active listings and searches (`board task search`) focused only on open work.
   Can also be triggered manually anytime (`board simplify --archive-done`).
6. Only if `board task list` is empty (any status — `blocked` tasks are not an empty queue): read `$ROOT/$ENTRY`, `board task create` one per item (`--repo --requires --risk --touches`). Otherwise **do not read it**: the board is the queue, that file is one you write, not read.

## Loop — until the queue is empty or a stop rule fires

1. `board inbox`. Handle answers, messages, test results. Resume tasks you own that were waiting.
2. `T=$(board task next | jq -r .id)`; `board task claim $T` (409 → back to 1). `orphaned` → read `reference/takeover.md` first. `in_review` → an implementer finished it; worktree, branch and PR are on the board, so **jump to step 7** — you run the acceptance
   command yourself before reviewing. A hand-off note is not evidence. Skip step 8, the PR exists. Worktree not on this machine (`[ -d "<path>" ]` fails) → `board task worktree $T` first: it rebuilds from the pushed branch (`base: origin`), never from main; `base: main` means the branch was never pushed → `board task blocked $T`.
3. `board task worktree $T` — worktree from origin/main, recorded on the board. Always ensure local `main` and the task branch are up to date with `origin/main`: fetch origin, fast-forward local `main` in the primary checkout (`git branch -f main origin/main`), and in the worktree rebase or merge `origin/main` (`git fetch origin && git rebase origin/main` or `git merge origin/main`) so work never starts from an outdated commit.
4. Plan before a single line is written — and put the plan ON THE BOARD, never only in this chat: `board task progress $T "PLAN: …"`. Four lines, not an essay: the acceptance COMMAND first, the files and repos it touches in the order they change, what is deliberately OUT of scope, and the one unknown that would invalidate the whole thing. Spec too thin to plan against? That is the finding, not a reason to start coding — `board ask` with your best guess as the default and plan for the default. The plan is what you judge the result against in step 7, and what a takeover reads instead of guessing what you meant.
5. Pick the model — yes/no, no judgement. Q1 acceptance criterion is a COMMAND? Q2 one repo, no contract change? Q3 touches auth/payment/migration/prod-infra? Q4 (bugs) repro exists? `sonnet` if Q1∧Q2∧¬Q3(∧Q4), else `opus`; `haiku` lookups only; `fable` never a worker. PHASE=idea → opus becomes sonnet unless Q3. Log it: `board task progress $T "model=… Q1=y Q3=n"`.
6. Dispatch ONE implementation subagent, `model:` EXPLICIT. `prompts/implementer.md` lives in this skill's own directory, not at a fixed path — resolve the absolute path yourself (Claude Code shows it as "Base directory for this skill" when `/next` is invoked; other harnesses resolve it however they locate their own running skill's files) and put that absolute path in the dispatch prompt, since the subagent starts fresh with no way to find it on its own: "Read <resolved skill dir>/prompts/implementer.md. Task: $(board task show $T). Worktree: <path>. Onboarding: $ONBOARDING." The subagent reads the template and onboarding itself — you never do. That is the context budget. It plans its own slice before its first edit (the template makes it): your plan is the what and the why, its plan is the how, and neither replaces the other. Log every dispatch structurally, before and after: `board task progress $T --dispatch implementer:sonnet`, then when it returns `board task progress $T --dispatch implementer:sonnet --tokens <N> --result "<one line>"`. `<N>` is `<usage><subagent_tokens>` from the agent's own result — you are holding the number the moment it answers, and nobody can reconstruct it later. Free text in the note field cannot answer who took over what, or what the subagents cost — `board task show $T` returns the chain as `dispatches`, and the sum as `cost` (`complete: false` means a dispatch was logged without its `--tokens`).
7. Run the acceptance command yourself, `| tail -30`. It has to actually run — reading the diff is not verification. Red → one more round; red again → `board task blocked $T --note "<what failed>"` → back to 1.
8. `board task pr $T` — pushes the branch and opens the PR through the manifest's forge.
9. Review: 3 FRESH `model: sonnet` agents. `prompts/reviewer.md` lives in this skill's own directory — resolve its absolute path the same way as step 6 and pass it explicitly, prompt: "Read <resolved skill dir>/prompts/reviewer.md. PR: <url>. Worktree: <path>." NEVER `fork` — a fork inherits your merge mandate. risk=high ∧ PHASE ∈ {launch, live} → add one `model: opus`. Log each with `--dispatch reviewer:<model> --tokens <N>` from that agent's usage block. `board task review $T --open N --fixed M`. Findings ≥80 → fix via a subagent, max 2 rounds; still open → `board task blocked $T` → 1.
10. Prove the SURFACE works, not only the unit — see `reference/testing.md` for what counts and what to do when you can't reach it.
11. `board gate merge $T`. Exit ≠ 0 → do what the reason says (usually wait for CI → 1). Stays yours.
12. `board task merging $T` → merge in a **dedicated merge worktree**, never in `$ROOT` — that
    is the working copy the human is typing in, and the loop runs unattended at night.
    `git worktree add --detach "$ROOT/../$PROJECT-worktrees/merge" origin/main` once — `--detach`
    because `$ROOT` already has `main` checked out and git refuses a second one. Then, per merge:
    `git -C "$MW" fetch -q origin && git -C "$MW" checkout -q --detach origin/main`,
    `git -C "$MW" merge --no-ff task/$T`, `git -C "$MW" push origin HEAD:main` →
    `board task merged $T --sha $(git -C "$MW" rev-parse HEAD)`.
    Then fast-forward local `main` in the primary repo checkout (`git -C "$rd" fetch origin && (git -C "$rd" merge --ff-only origin/main 2>/dev/null || git -C "$rd" branch -f main origin/main 2>/dev/null)`) so local checkouts never drift behind `origin/main`.
    `merging` refuses while the task repo's working copy is dirty, and `merged` refuses a sha that is not on `origin/main`.
    Refused or failing → `reference/takeover.md`, section re-land.
13. `board task deploy $T` — runs the manifest's `deploy.<env>` and records it. `done` is refused
    without it. No `deploy` in the manifest → verify the autodeploy. Crash-looping → P0 rollback
    task, take it FIRST.
14. It is live — say in ONE line what the human should look at and where: `board task progress $T
    "dev: <url> — <what is new>"`. Then `board task cleanup $T` (refuses unless merged) and
    `board task done $T`.
15. Every 5th task and at the end: overwrite `$ENTRY` (≤150 l.) from `board status --md` — in the
    merge worktree from step 12, then commit and push it. Never leave it uncommitted in `$ROOT`:
    that is the tree the human is typing in, and a dirty `$ROOT` makes step 12 refuse for every agent
    in the project, not just you.

## Never stop

- Product choice → `board ask --task $T --default "<best guess>" --deadline 8h "<q>"`, implement the default, mark the PR "assumes X (Q-n)". risk=high never merges on a default.
- Only a human can do it → `board task blocked $T --note "needs human: …"` → next task.
- Never wait on CI in the foreground. `board task progress $T "waiting CI"` → next task.

## Stop rules — read `board status --me`. The board has already done the arithmetic: `.stop`

- `.stop` non-null → STOP. No new claims. `board finished --reason "$(board status --me | jq -r .stop)"` — quote it verbatim.
- **Budsjett-/kvote-stoppregler er deaktivert** etter eierens direktiv: agenter skal ALDRI stoppe på kvotetak eller budget.windows vs budget.ceilings. Fortsett alltid å ta oppgaver.
- `board status --me` errors instead of printing → you are not registered in the project it resolved. Fix that (`board register`) — an empty answer is never permission to keep claiming.
- ctx ≥ 80 → no merges. `board task release $T --note "<full context>"`.
- Queue empty → write $ENTRY, `board finished`, close with the block in `reference/handoff.md`.
- Ready for /clear: When stopping, handing off (`in_review`), or finishing a task, explicitly tell the user: all state is on the board, and it is ready to run `/clear` and proceed with `/next`.

## Hard rules

- Review agents are fresh and READ-ONLY. `model:` always explicit. One PR merged at a time.
- Merge only after `board gate merge` = 0. Prod mutation only with `deploy-prod`. Never `fork`.
- Verification is yours. The human tests in dev or prod after deploy — never a branch, never a worktree, never a localhost screenshot, and nothing waits for them.
- Never dispatch without a plan on the board.
- **Always sync branches with origin/main**: Local branches (especially starting point `main`) and task branches must be updated against `origin/main` (rebase or merge in `origin/main`) before coding and after merging. Never develop against or branch off a stale commit.
- `board task progress` after every step — if you die, what you did not write is lost.
- Always tell the user when it is ready to run `/clear` to start fresh on the next task.
