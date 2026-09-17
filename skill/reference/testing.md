# Verification

Loaded at step 10, when you have to prove the change works.

Nobody verifies your work for you. The human tests what is **deployed**, in dev or prod, after
the fact — never a branch, never your worktree, never a screenshot of your localhost.

## What counts

Whatever actually executes the change. Pick by what you touched:

- **CLI.** Run the command. `board task pr $T` either printed a URL or it printed an error —
  that is the whole test, and it takes four seconds.
- **Playwright / browser-test.** Anything with a visual surface: open the page, drive the
  interaction, assert what the user sees. A page that renders is not a page that works.
- **Test code.** A test that failed before your change and passes after. Written first, run both
  times — a test that was green from the start proved nothing.

Against something real: the live board, a dev deploy, a local process you started. A mock you
wrote in the same commit tests your mock.

## What does not count

Reading the diff. "The types line up." A test you wrote but never saw red. A screenshot of
localhost. "The human checks it after deploy" — they will look, and it is still not your
verification.

## No surface you can reach

You lack `playwright`, the manifest declares no environment, there is nothing to run it
against — then say so. `board ask --default "<what you would have checked>"`, or
`board task blocked $T --note "cannot verify: <what is missing>"`, and take the next task.
An unverified change that is honest about it beats work that sits waiting for a human to look.

## Evidence

The command and the last lines of its output go on the board, in the same note as the claim:

    board task progress $T "acceptance: ./conformance.sh → 219 PASS / 0 FAIL"

`board task show $T` is what a takeover reads, and what the reviewer re-runs. A claim with no
command in it is not evidence — the reviewer scores that at 80.

## After deploy

One line, so the human knows where to look:

    board task progress $T "dev: https://board.example.com/status — the agent rows now collapse"

That is their test. It happens on what runs, it blocks nothing, and a FAIL from them is new work
at priority 90 — not a reopened task.
