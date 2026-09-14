# next · phase build

Agentic development loop plus a coordination board.

This file is the project's **entry point** (`entry:` in `project.yaml`). It is written by the
loop, not read by it: every fifth task, and at the end of a run, the coordinator overwrites it
from `board status --md` and commits it. It exists so that a human — or an agent starting cold
with no board reachable — can see where the project stands from the repo alone.

The board is the queue. This file is a snapshot of it, not a second copy to keep in sync: an
agent that finds tasks on the board does **not** read this file (SKILL.md, Start step 6). Only an
empty queue sends it here, to seed the first tasks.

## Agents

| agent | status | model | ctx | quota | task |
|---|---|---|---|---|---|

## Tasks

| id | status | repo | owner | title |
|---|---|---|---|---|

## Open questions

## The board answered itself (can be overridden)

---

A fresh checkout starts here, with the queue empty. Seed it by describing the work you want:

```bash
/next "<what you want done first>"
```

or by writing tasks into the sections above and running `/next` — an empty board reads this file
and creates one task per item.
