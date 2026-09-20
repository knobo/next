# The board — a guide for the human

Everything you have to do yourself. The agents use the `board` CLI; you use a browser and five
commands. **You never need to set environment variables** — `board` fetches the token from
`pass board/token` on its own.

## Log in to the browser (once per device)

```bash
board open            # opens /status in a browser here
board open --qr       # a QR code in the terminal — scan it with your phone
```

The token is 43 characters and impossible to type on a phone, so `--qr` is the way there: scan
the code, and the phone holds the cookie for 90 days. (`board open` without `--qr` also falls
back to a QR code when it cannot find a browser, e.g. over ssh.)

The board swaps the token for an `HttpOnly; Secure` cookie that lasts 90 days, and redirects to a
clean URL. The token never ends up in the address bar, the history or the server log. After that
the plain addresses are enough:

| Address | What you see |
|---|---|
| `/status` | phase per project, living agents with context and quota usage, tasks, open questions |
| `/t/<id>` | one task: PR number, review findings, timeline, and whether its owner is alive |
| `/q/<id>` | one question, with answer buttons |

**On the phone:** `board open --qr` and scan. After that every push notification is
clickable straight through.

## Testing

There is no test queue. The agents test their own work with the CLI, playwright or test code
before merge; you test in dev or prod after deploy. Found something? `board task create`, or
comment on the task at `/t/<id>`.

## The commands you actually use

```bash
board status                      # everything, all projects
board status --project myproj     # one project
board status --me                 # your own row + .stop: the board's finished stop answer
                                  # (null = keep going)
                                  # budget.windows = the highest per window name on the account,
                                  # budget.ceilings = the ceilings in force, budget.source says
                                  # whether they came from the policy or from the board
                                  # fails (exit 2) if you are not registered; if no ceiling is set
                                  # anywhere the block says so with ceilings_missing + note
board answer Q-9 "unlimited"      # answer a question
board tail --since 2h             # what happened overnight
```

Two numbers are yours alone to set, and the board refuses them from an agent. Both are also the
handles on `/status`: drag the ceiling line, type in the review limit.

```bash
board ceiling                     # what is set, and whether it came from the policy or from you
board ceiling 5h 85               # move the quota ceiling for one window
board ceiling 7d ''               # hand that window back to board-policy.json
board ceiling --wip 4             # how much finished work may wait for review at once
```

Anything an agent writes for you to read — a task's `spec`, a plan posted as a progress
note, a question, a comment — is rendered as **markdown** on the board: headings, lists,
`code`, fenced blocks, quotes and links. Write it that way and it arrives readable.

```bash
board task create --title "..." --spec-file spec.md    # the spec now has a page to live on
board ask --default "yes" --deadline 8h "$(cat question.md)"
```

The text is escaped before any of it is read as markup, so nothing written into a spec
can become an element on the page. A link survives only if it points at `http(s)://` or
at a path on the board itself — and a path is checked for more than a leading slash,
since a browser normalises `\` to `/` and `/\elsewhere.example` is not local at all.

Sizing work, so the board can tell you later how well each model guesses:

```bash
board task create --title "..." --estimate 5     # relative size: 1 2 3 5 8 13, nothing else
board task estimate T-42 8                       # or afterwards, from anyone in the project
board task patch T-42 --priority 80              # what should be picked up next
```

The board records which model gave each estimate and what the task then cost in tokens, and
publishes both on `/metrics` — see `docs/grafana-ai-agents-dashboard.json` for the panels that
put one against the other.

Role control, for when you want to decide who coordinates:

```bash
board role pin coordinator --project myproj --agent cc-laptop-7f3a
board role unpin coordinator --project myproj
board role recommend --project myproj     # who the ranking points at, and why
```

Your pin always beats the agents' own election — right up until the agent is dead (60 minutes
without a heartbeat). Then they elect a new one themselves and you get a notification about it,
rather than the project standing without a coordinator until you wake up.

Pinning a role and answering as yourself both require the **human token**
(`pass board/human-token`). Without it, `by: <you>` would be a claim any agent could write, and an
agent could pin itself as coordinator or answer its own high-risk question.

## When something is wrong

| Symptom | What it means | What you do |
|---|---|---|
| An agent shows as `stale` | >5 min without a heartbeat | often just a slow task; `dead` after 60 min |
| An agent shows as `dead` | the process is gone | the reaper releases its task as `orphaned`; the next agent takes it with worktree, branch and last note |
| An agent shows as `stalled` | alive, but no progress for longer than the lease | something other than quota is wrong; the reaper hands the task on at the next tick |
| `{"queued":true}` from an agent | the board was down, the call is in the outbox | nothing — it flushes on the next call |
| `{"stale":true}` on a read | board down, this is cache | nothing; the agent carries on |
| A question went `defaulted` | the deadline passed | the PR is marked "assumes X, Q-n unanswered" |
| The board does not answer | the pod is down | `kubectl -n board rollout restart deploy/board` |

## Phase drives the strictness

The phase is in the project's `project.yaml` and is mirrored to the board. It decides the merge gate, prod deploys and the model choice — see `DESIGN.md` §3.6.

| Phase | A merge requires |
|---|---|
| `idea` | nothing beyond the mechanical checks |
| `build` | + review findings closed |
| `launch` | + review findings closed |
| `live` | + review findings closed |

Change phase: `board project set --project myproj --phase live`.

## Bringing a new project into next

The agent can do nearly all of it. Stand in the project's root directory and have it run:

```bash
board project init          # reads the shape off disk and writes project.yaml
```

It finds the repos (one git repo → `repos: ['.']`, otherwise every git subdirectory), the entry
file (`next-prompt.md` or `docs/next-prompt.md`), the onboarding file (`AGENT_ONBOARDING.md` or
`CLAUDE.md`), and the forge type from `git remote get-url origin`.

**Two fields you must set yourself**, and that is deliberate: `phase:` and `goal:`. The phase
drives the merge gate and prod deploys — it cannot be read out of files, and an
agent must not be able to give itself slacker rules by guessing it.

```bash
$EDITOR project.yaml        # set phase: idea|build|launch|live and goal:
board project sync          # mirror the manifest to the board (visible from other machines)
```

After that `/next` works in that project. If the phase is `launch` or `live` without
`onboarding:` pointing at a file, the skill refuses to start — a project with users must not be
run on the general rules alone (§10).

## What you have to do yourself (agents cannot)

- **Branch protection** on the forge. The agent's token is not an admin token. Without branch
  protection, prompt text is the only thing stopping "push straight to main" — see `DESIGN.md`
  §8.1.
- **Testing after deploy** in dev or prod — the merge does not wait for it.

## Useful, but not the board

```bash
python3 graph.py index        # build the knowledge graph over memories/skills/docs
python3 graph.py near <slug>  # what is connected to this
python3 graph.py dangling     # links to something we promised to write
python3 graph.py orphans      # memories nobody points at
```

`graph.py` indexes `~/.claude` by default; set `GRAPH_ROOTS` (colon-separated, like `$PATH`) to
point it somewhere else.
