# Human test cards

Loaded only when `board test-level $T` returns `human`.

`board test-level` already decided level, class and environment from phase × change class
(DESIGN.md §5) — do not second-guess it. Your job is the card.

It answers `human` even where the table says `auto` whenever the automation does not actually
exist: you lack `playwright`/`browser-test`, or the manifest declares no `environments.<env>.how`
for the surface. The `why` field says which. That is deliberate — `auto` with nothing to run means
nobody looks, and a UI nobody looked at is the one thing a test card is for. Do not work around
it by declaring the capability; add the automation, or file the card.

## The card is rejected unless it has all of this

`board test request $T --card-file card.json` validates the schema. Missing field → 400, and the
error comes to you, not to the human.

```json
{"project": "myproj", "repo": "web", "env": "dev",
 "url": "https://dev.example.com/queues/123/config",
 "login": "e2e-owner (pass claude/e2e-owner)",
 "steps":    ["Open the Products tab", "Toggle it on for Coffee, open /screen/<token>"],
 "expected": ["A new 'Show on board' button under each product", "Coffee appears within 5 s"],
 "risk": "normal",
 "rollback": "kubectl -n myproj rollout undo deploy/web"}
```

- `steps` and `expected` must be the same length. One expectation per step, or the tester cannot
  tell you what went wrong.
- `url` must be reachable in `env`. Take `env` from the manifest's `environments:`.
- `rollback` is a command the human can paste, not a description.
- Max 12 lines rendered. If it needs more, the task is too big — split it.

## When to file it

**After deploy, not before merge** — except in `launch`/`live`, where the gate itself demands a
human OK first and the task waits. In `build` the change is already live when the card lands, so
the human tests what actually runs instead of approving a diff. Nothing blocks on the answer.

## After you file the card

**You take the next task.** Do not wait.

- OK → nothing to do; the task is already done. (`launch`/`live`: returns to you as `claimed`
  with a fresh lease, unless you were reaped — then the OK is dropped with the owner.)
- FAIL on a task still `awaiting_human` → it returns to you as `claimed`; continue to the gate.
- FAIL on a task already `done` → the board opens a follow-up task at priority 90 with the
  human's words as the spec. The broken code is live, so fixing it is new work, not a reopened
  task.

A test card can never default to OK, whatever the deadline. That is deliberate: an unanswered
question can be guessed, an unverified UI cannot.
