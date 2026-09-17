# Taking over work someone else started

Loaded when `board task next` hands you an `orphaned` task, when a merge wrapper fails, or when
the CLI answers `{"queued":true}` / `{"stale":true}`.

## Orphaned task

The reaper released someone's task. Two paths lead here, and neither destroys anything — the
worktree and branch still exist:

- The agent **died**: 60 min without a heartbeat.
- The agent is **stalled**: the process is still alive and still heartbeating, but the lease on
  the task expired. The lease is renewed by `claim` and `task.progress` only, never by the
  heartbeat (Q-107) — so this is an agent that has gone longer than the lease without making any
  progress it wrote down. That is not quota death; something else is wrong, and the task is
  handed on.

This is why writing `board task progress` after every step is mandatory rather than polite: it is
what tells the board you are still working, and it is also the note the next agent reads.

A task in `blocked` is never orphaned by lease expiry alone; that is a documented wait. But the
reaper still orphans it once its owner is confirmed dead (60 min without a heartbeat) — a blocked
task needs a live owner to eventually unblock it. And an orphaned
task's verification does not carry: the previous agent ran it against their branch, so the note
is a claim about code you are about to change. Re-run it yourself before you believe it.

1. `board task show $T` → `worktree`, `branch`, `pr`, and the last `progress` note.
2. Worktree exists on **this** machine → `git -C <worktree> status`. Uncommitted work is real
   work; read it before you decide anything.
3. Worktree is on another machine → `board task worktree $T` rebuilds it here from the pushed
   branch and answers `base: origin`. Check that field: `base: main` means the branch was never
   pushed, so there is nothing to take over — do not build on the empty branch it just made,
   `board task blocked $T` instead. The branch was pushed at PR time; anything after that is
   lost, and that is acceptable.
4. `board task claim $T`, then continue the loop from the step the progress note implies.

Do not restart from scratch because it is easier to reason about. The progress trail exists so
the work survives the agent.

## `merge_requested` with no `merge_verified`

The previous agent died mid-merge. **Do not merge again.** Read the truth from the forge, not
from the board:

1. Is the branch already an ancestor of main? `git merge-base --is-ancestor <branch> origin/main`
   → yes: the merge happened. `board task merged $T --sha $(git rev-parse origin/main)` and go on.
2. No → the merge did not happen. Check CI on the current head, then merge normally.

Merge in the forge is idempotent per PR; re-running it after a real merge is a no-op error, not
a double merge. The danger is not re-merging — it is assuming state instead of reading it.

## Re-landing a PR that was closed or lost

`git fetch origin refs/pull/<N>/head:recover/<N>` gives you the commits even when the branch is
gone. Make a new branch from it, open a new PR, and reference the old number.

## The board is down

The CLI answered `{"queued":true}` (write, now in `~/.cache/board/outbox.jsonl`) or
`{"stale":true}` (read from cache). Both are normal. Keep working on the task you already hold.

- Do not claim new tasks from stale data if you can avoid it — the CAS fails at flush time and
  you have done duplicate work. Finish what you hold instead.
- **Role claims are never queued.** If you were not coordinator before the outage, you are not
  coordinator during it.
- Never merge on stale gate data. `board gate merge` answering from cache is not an answer.
