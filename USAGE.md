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
                                  # budget.ceilings = the ceilings from the policy
                                  # fails (exit 2) if you are not registered; if the ceilings are
                                  # missing from the policy the block says so with
                                  # ceilings_missing + note
board answer Q-9 "unlimited"      # answer a question
board tail --since 2h             # what happened overnight
```

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
