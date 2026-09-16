# Handoff block

Written once, when you call `board finished`. This is the only thing a human reads afterwards, so
it answers what the board cannot: what you would have done next, and what you are unsure about.

Print it as your last output. Keep it under 25 lines.

```
## Handoff — <project> — <ISO timestamp> — <agent id>

DONE THIS SESSION
- T-42 <title> → merged <sha>
- T-43 <title> → blocked, "cannot verify: no playwright on this host"

LEFT ON THE BOARD
- T-44 <title> → released, "<why>"      ← anyone can pick this up
- Q-11 unanswered, defaults to "<x>" at <time>

STOPPED BECAUSE
<quota 92% | queue empty | blocked: …>

WHAT I WOULD DO NEXT
1. <the single most useful next action, concrete>
2. <second>

UNSURE ABOUT
- <anything you assumed that a human should confirm; empty if none>
```

Rules:

- Every claim here must already exist on the board. If it is not in `board status`, it did not
  happen — write it to the board first, then repeat it here.
- Do not summarise the code. The PRs are the record.
- "WHAT I WOULD DO NEXT" is not a plan for a human to approve. It is what you would do if you
  woke up again — write it so the next agent can act on it without asking.
- Always conclude the handoff output by explicitly telling the user: all state is safely recorded on the board, and it is ready to run `/clear` and proceed with `/next`.
