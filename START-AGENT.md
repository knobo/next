# Starting an agent

```
/next
```

That is all. The skill finds the project from `project.yaml` (searching upwards from where you
stand), registers the agent on the board with verified capabilities, claims a role, and runs the
loop until the queue is empty or the quota runs out.

## Roles

```
/next                  coordinator (default) — dispatches, reviews, merges, verifies
/next implementer      takes tasks and writes the code itself; never merges
/next reviewer         takes `in_review` first; READ-ONLY
/next tester           takes tasks that require browser-test, and open test cards
```

The loop is the same for everyone. What differs is which tasks you take and who writes the code —
the details live in `skill/reference/roles.md`, which is loaded only when the role is not
coordinator.

A second `/next implementer` session on another machine is a normal setup: it drains the queue in
parallel while the coordinator handles PRs, review and merges. Two coordinators is impossible —
the board has a unique index, and `board role claim` says so.

## What the agent does without asking

| Situation | What it does |
|---|---|
| Unsure about a product decision | `board ask` with its best guess as the default, implements the default, marks the PR, moves on |
| Needs you to test in a browser | files a test card to `/tests`, takes the next task |
| Only you can solve it | `board task blocked`, moves on |
| CI is slow | writes progress, moves on — never waits in the foreground |
| Quota is running out | stops claiming, finishes to a safe point, releases the task with full context |

So it never stops and waits for you. That is the entire point: it should work while you sleep.

## What mechanically stops it doing damage

- `board gate merge` must return 0 before a merge — it checks review findings, grants, the merge
  mutex and the phase requirements.
- The `PreToolUse` hook aborts `tea pr merge` / `gh pr merge` / a push to main when the gate is
  closed, inside subagents too. If the board is down it fails *open* and stays out of the way.
- Grants come from the policy on the board. An agent cannot give itself `merge` or `deploy-prod`.
- Review agents are fresh and READ-ONLY, never a `fork` — a fork would inherit the merge mandate.
- The gate refuses a review result set by the task's own owner. A coordinator cannot wave its own
  work through.

What is **not** covered: an agent that writes `board task review --open 0` without having run a
review at all. That is lying in its own log. The owner check above makes it lie about someone
else's work rather than its own, which is the common failure, but it is not watertight.

## Check that it arrived

```bash
board status              # the agent should show as alive with model, ctx and quota windows
board open                # the same picture in a browser
```
