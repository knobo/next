# Contributing

This is a research experiment. Contributions are welcome, and so is forking it and going a
different direction — that is probably the more interesting use of it.

## The one hard rule

`./conformance.sh` must be green, and **it is the specification**. It runs every operation in
DESIGN.md §4 against a fresh instance of `board.py` on a free port — no network, no cluster, no
secrets, about a minute.

```bash
./conformance.sh
```

If you change behaviour, change the check that asserts it in the same commit. A behaviour change
with no check change is either a regression or an untested feature; both are worth catching in
review.

If you change the HTML pages, `ui/verify.sh` is their acceptance test, and it needs Node:

```bash
cd ui && npm install && ./build.sh && ./verify.sh
```

`board.css` is generated and checked in. Edit `ui/input.css` and rebuild — never edit
`board.css` directly.

## House style

The comments in this codebase are unusually long, and that is deliberate. A comment here explains
**why** a line exists, and almost always the answer is that its absence broke something specific:
a silently dead push for a day, an agent that reviewed its own work, a lease that orphaned the
whole queue at once. If you remove a guard, you are removing the answer to a question somebody
already paid for — so say in the commit message what makes it safe now.

Concretely:

- Prefer a comment that names the failure over one that restates the code.
- A constant with a surprising value gets the reason next to it, not in a wiki.
- Task ids (`T-164`, `Q-107`) in comments point at the board entry that produced the change. They
  will not resolve for you; treat them as provenance markers, not links.
- English throughout — code, comments, commit messages, docs.

## What belongs in code, and what belongs in config

The dividing line is: **anything true about one installation is configuration.**

- Hostnames, the owner's name, the kubectl context, the notification topic → `board.env`
- Grants, quota ceilings, role requirements, WIP limits → `board-policy.json` on the board
- A project's shape, phase, repos, forge and deploy command → that project's `project.yaml`
- Which task titles cluster together for `board simplify` → that project's `simplify-rules.yaml`

If you find yourself adding a repo name, a machine name, a person's name or a domain to a `.py`
or `.sh` file, it belongs in one of those four instead. That rule is why `simplify.py` is
rule-driven rather than a list of one project's clusters, and it is the main thing to preserve.

## Scope

Things that would fit well:

- Another backend behind the same API. `conformance.sh <url>` exists precisely so a different
  implementation can be held to the same contract.
- Harness support beyond Claude Code. The board is already neutral about quota window names; what
  is missing is testing and heartbeat paths for harnesses without a status line.
- Closing the gaps in [Status and honesty](README.md#status-and-honesty) — CI status in the gate
  is the biggest one.

Things that would need a conversation first:

- Anything that makes the board scale horizontally. One writer and one SQLite file is a deliberate
  choice (DESIGN.md §10), and the append-only event log is what makes the projections safe.
- Anything that lets an agent decide its own permissions, phase or quota ceiling. Those are the
  load-bearing constraints; if they can be relaxed from inside, the rest of the design is
  decoration.

## Reporting something

Open an issue with what you ran, what you expected and what happened. If it is about the loop
rather than the board, the output of `board task show <id>` and `board tail --since 2h` usually
contains the whole story — the board is designed so that it does.

Check both for tokens before pasting: `board tail` can contain question text, and a URL with
`?t=…` is a live credential.
