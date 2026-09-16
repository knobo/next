# Roles

Loaded on `/next <role>`, on a 409 from `board role claim`, or when `board role recommend` points
at you.

A capability is what you *can* do. A role is the job you have now. Only `coordinator` is unique
(one per project); the rest are filters `board task next` uses.

## Who becomes coordinator

The human's pin always wins while the holder is `alive` or `stale`. Otherwise the ranking decides, and
every agent computes the same answer from the same board data — no election protocol, no messages:

1. `alive` and holds the capabilities the role requires
2. has not set a different preference (`board caps --prefer implementer`)
3. model, by the policy's `prefer_model` (fable > opus > sonnet)
4. lowest reported quota window — the coordinator should outlive everyone
5. earliest registered (stability; stops flapping)
6. lexically lowest agent id (ties are impossible)

`board role claim coordinator` is a compare-and-swap plus a ranking check. 409 with
`recommended: <id>` means someone else is the correct answer — you are implementer, and that is
not a demotion. Do not retry in a loop.

Point 2 is the whole "agents propose among themselves" mechanism. If you know something the
ranking does not — you are mid-way through a contract change across three repos and must not be
interrupted — say it as data: `board caps --prefer implementer`.

## You just became coordinator

1. `board inbox --as coordinator` — this also returns answers to questions asked by the agent that
   died, on tasks nobody owns. Answers are never lost because the asker is gone.
2. `board status` — who is alive, what they hold, how much quota is left.
3. Queue hygiene: `board simplify` (or `board simplify --apply`) to cluster scattered verification follow-ups and close retracted/obsolete cards so the queue stays clean.
4. Tasks the dead coordinator held are `orphaned` and come first from `board task next`
   (`reference/takeover.md`).
5. Continue the loop. You own nothing special: tasks have owners, questions belong to tasks, and
   messages sit in the recipient's inbox.

## The human takes over

`/next coordinator` in their own session pins them, and the elected coordinator steps aside
(`role.released`). Expect it; it is not an error.

## What changes per role

The loop in SKILL.md is the same for everyone. Only these lines differ:

| Role | Which tasks | Who types the code | Merges? |
|---|---|---|---|
| `coordinator` | `board task next` — anything you qualify for, orphans first | a dispatched subagent (step 6) | yes, after the gate |
| `implementer` | same queue, but you take the task and **write the code yourself** — skip step 6, do the work in your own worktree | you | no — hand it over (below) |
| `reviewer` | `board task list --status in_review` first | nobody; you are READ-ONLY (`prompts/reviewer.md` applies to you) | no |
| `tester` | tasks with `requires: browser-test` — the browser verification another agent had no capability to run | nobody; you run it, and the output is the evidence | no |

Whoever writes the code plans first (loop step 4), and the plan goes on the board before the
first edit. An implementer has no coordinator holding a plan for them — so theirs is the only
one there is, and a takeover has nothing else to read.

**The implementer's hand-off is two commands, and the second one is not optional:**
`board task progress $T --status in_review --note "<what you did, what you verified>"`, then
`board task release $T --note "ready for review"`. Release is the hand-off — it keeps the task
`in_review` and only drops your ownership. Stop after the first command and the task is finished
work with an open PR that `board task next` will not offer anyone and `owns()` will not let anyone
touch: a deadlock on exactly what was most valuable. A coordinator picking it up gets it back
ahead of new work, still `in_review`, with worktree, branch and PR already on the board.
Immediately after releasing, tell the user: all state is safely on the board, and it is ready to run `/clear` and continue with `/next`.

A second `/next implementer` session on another machine is a normal setup: it drains the queue in
parallel while the coordinator handles PRs, review and merges. Two coordinators is not — the
board's unique index makes it impossible, and `board role claim` will tell you so.

Whatever your role, the stop rules and the hard rules in SKILL.md still apply. They are about
quota and safety, not about rank.
