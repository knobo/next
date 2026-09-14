# `/next` — a fully automatic agentic development loop + coordination service

**Status:** this began as a design document, written before the code. Most of it is now
implemented; where it still describes an intention rather than a fact, it says so. The section
numbers here (§3.7, §5b, §8.1) are referenced from comments throughout the code.

**Scope: user-global.** The skill lives in `~/.claude/skills/next/` and one board serves *all* of
an owner's projects across machines. That is the design constraint that shapes everything else: a
per-project tool could assume a single repo layout, a single quota, a single queue. This one
cannot.

The conventions across real projects genuinely diverge — one project is a workspace over ten
repositories with `docs/next-prompt.md`; another is a monorepo with the entry file at its root
and worktrees in a sibling directory; a third is a single repository. So the skill assumes
nothing about layout and reads it from a manifest instead (§3.2b).

---

## 1. Summary — the main recommendations

1. **One user-global board**, with project as a namespace; agents and capabilities are global,
   grants are per (agent, project). One Python file (stdlib HTTP + SQLite) behind an ingress,
   bearer token from a password store. Append-only `events` + projections. (§3.1–3.4)
2. **Project discovery via `project.yaml`**, found by searching upwards from cwd; covers a
   ten-repo workspace, a monorepo and a single repo without the skill assuming anything. (§3.2b)
3. **The storage layer is kept swappable.** The API is built storage-agnostic, and
   `conformance.sh <url>` is the hook any alternative backend is tested behind. (§3.3)
4. **Heartbeat and token reporting come free from the status line hook** (`rate_limits`,
   `context_window`); a dead process is a silent heartbeat. (§3.5)
5. **Questions never block**: `board ask` → next task; the board pushes a notification with an
   answer form; the answer is collected in `board inbox`. (§4)
6. **Phase per project** (`phase:` in the manifest) drives the test level, the merge gate, prod
   deploys and the model choice. (§3.6)
7. **Capabilities are self-declared, grants are assigned**; matched at claim time. Roles: a human
   pin wins, otherwise a deterministic ranking — no consensus protocol. (§3.7–3.8)
8. **The merge gate is enforced mechanically**: branch protection on the forge plus a
   `PreToolUse` hook that asks `board gate`. (§8)
9. **The test queue is the question table with `kind: test`**, in one place (`/tests`) across
   projects; OK/FAIL wakes the agent that asked. (§5b)
10. **Context is a hard budget**: SKILL.md ≤80 lines, prompt templates read by the subagents, the
    board is the memory; roughly 7k tokens per task. (§6.2–6.5)
11. **Phase 0 has value on its own**: board + CLI + heartbeat + push replaces a hand-edited
    markdown file listing who is working on what. (§9)

---

## 2. Background — the failure modes this is a response to

This design did not start from a blank page. It started from a working setup where coordination
was two markdown files (a list of active agents, and an entry file of what to do next) plus a
claim protocol described in prose in an onboarding document. That setup worked until it did not,
and the ways it failed are the specification for everything below.

The published version of this document deliberately does not carry the audit of one particular
installation that the original had — repository names, machine addresses, API responses. What
generalizes is the failure modes:

| Failure | What it means for the design |
|---|---|
| **Two agents in the same worktree.** Two sessions claimed work that touched the same files, because the claim was a line in a markdown file that neither had re-read. | A claim must be a compare-and-swap against shared state, and overlapping `touches` must make a task invisible to the second agent (§3.4, §4). |
| **Review forks that pushed without approval.** A review subagent forked from a session that held a merge mandate, and inherited it. | Review agents are fresh and READ-ONLY, never forks. The gate is mechanical, not a prompt instruction (§8.2). |
| **Agents waiting on a monitor and losing it.** A session blocked in the foreground on something that then went away, and the work was stranded. | Nothing waits in the foreground. Waiting is a state on the board, and another agent can take the task over (§7). |
| **Quota ran out mid-session.** The context and the work in it vanished with the process. | The board is the memory; progress notes are the hand-off; a lease expires and the reaper releases the task with its worktree, branch and last note intact (§3.4, §7). |
| **A classifier that refused to merge without a review that had never been requested.** Prompt-level rules that nothing enforced, and prompt-level rules that nothing could satisfy. | Every rule that matters is a check the board can answer yes or no to, and the reason is returned with the no (§4, §8). |
| **No branch protection on the repositories.** The agent's forge token was not an admin token, so it could not create it either. | Acknowledged as an open hole (§8.1), with the `PreToolUse` hook as the safeguard in the meantime, and it is called out as something a human must do. |
| **A merge to main deploys to production minutes later.** | Phase (§3.6) exists so that "users in production" is a declared property the gate can read, not something an agent infers. |

Two more facts about the environment shaped the design rather than the failures:

- **The Claude Code status line receives `rate_limits.{five_hour,seven_day}` and
  `context_window.used_percentage` on every render.** That is a free, high-frequency heartbeat and
  quota report — it costs nothing to send it to a board (§3.5). Other harnesses do not have it,
  which is why the quota model is a list of self-named windows rather than two fixed columns.
- **Hooks are per process and therefore work inside subagents.** That is what makes a mechanical
  merge gate possible at all (§8.2).

---

## 3. Architecture

### 3.1 Three layers

```
┌──────────────────────────────────────────────────────────────────────┐
│ Human (phone / laptop)   ←push / answer form→   board.example.com    │
└──────────────────────────────────────────────────────────────────────┘
                                   ▲  HTTPS JSON + bearer  (offline: local outbox)
        ┌──────────────────────────┼───────────────────────────┐
        │ machine A                │ machine B                 │ production
        │ Claude Code `/next`      │ Codex / Grok / Claude     │ (no agent — an ssh target)
        │  ├─ statusline hook →heartbeat + tokens              │
        │  ├─ PreToolUse hook  →gate before merge/deploy       │
        │  └─ subagents (sonnet/opus) in their own worktrees   │
        └──────────────────────────────────────────────────────┘
```

- **The board:** a passive service. It does not *decide* — it stores events, derives state,
  matches capabilities, and pushes to the human. All the intelligence is in the agents.
- **The coordinator role** is not a separate process: it is whichever `/next` session is running.
  Several `/next` sessions at once are fine — the board is the source of truth, not the session.
  A reaper loop marks dead agents and releases leases.
- **Worker agents** are subagents (Claude) or foreign harnesses (Codex/Grok) — both talk to the
  board through the same CLI.

### 3.2 (a) One user-global service, project as namespace — and what that implies

**Recommendation: one instance for everything.** The reasoning:

1. There is one developer and one question queue that has to reach them. Three instances = three
   notification streams, three places to answer, three "is it up?".
2. The agents run on the same machines across projects; one token, one CLI, one URL. A session can
   `cd` from one project to another and carry on — same agent id, same heartbeat.
3. The token budget (the 5-hour window) is **per account, not per project** — the stop rule
   (§6.3) has to be computed across all projects, and that requires one board.
4. The namespace cost is one field (`project`) on every event. A per-project instance gives zero
   isolation benefit (same user, same trust).

**What is per project vs. per agent — this table is the answer to the question:**

| Per project (namespace) | Per agent/session (global) | Per (agent, project) |
|---|---|---|
| phase, goal, manifest path, repos | id, harness, host, model, heartbeat, token quota | grants |
| tasks, questions, test cards | capabilities, preference | role (`coordinator` is a singleton *per project*) |
| the coordinator role | `current_project` (what the manifest at cwd resolved to) | |

**Isolation:** every read and write on tasks/questions/roles takes `project` from the agent's
`current_project` (set at `register`), and the board refuses calls where
`task.project ≠ agent.current_project` (403). An agent in one project therefore cannot claim
another project's task by accident. There is one deliberate exception: `board message` and
`board task create --project X` — an agent that finds a bug in another project while working here
should be able to file it in that project's queue. It is logged as
`task.created {actor: <id>, from_project: <here>}`.

**Status across projects:** `board status` without `--project` shows every project grouped (phase,
living agents with quota, roles, open questions), and the `/status` HTML page is the same view.
Push messages are always prefixed with the project name.

A repo is an *attribute* on a task (`repo: web`), not a level in the hierarchy. Phase lives on the
project (§3.6) because "heading for users" applies to every repository in the project at once.

### 3.2b Project discovery — the `project.yaml` manifest

The skill is global and has to work out *where it is*. The mechanism: **search upwards from cwd
for `project.yaml`** (exactly as git finds `.git`), stopping at `$HOME`. The manifest is the truth
about the project's *shape*; the board mirrors it (`project.phase_set`, `project.manifest_set`)
for cross-machine visibility, but it is never read *from* the board when the file exists (K6: it
works offline).

```yaml
# Fields. Everything except `project` and `phase` has a default.
project: myproj                  # name = namespace on the board
phase: launch                    # idea | build | launch | live
goal: "Get to production and find the first paying users."
entry: docs/next-prompt.md       # the entry file; default: next-prompt.md in the manifest directory
repos:                           # default: ['.'] if the manifest directory is a git repo
  - web
  - core
  - management
  - discovery
  - mobile
  - infra
worktrees: "../{repo}-worktrees/{branch}"   # default
docs: docs/                      # where status is written; default: the manifest directory
onboarding: docs/AGENT_ONBOARDING.md         # optional; read by the skill if present
forge: { kind: forgejo, url: https://git.example.com, org: myorg, login: claude }
environments:                    # used by the test queue (§5b) to derive a test URL from the phase
  # `requires`: which capabilities the test actually needs. Omitted = [browser-test], which is
  # right for an e2e test and wrong for a shell script — a command-line test sets
  # `requires: []` and can then be run by an agent with no browser (T-142).
  mock: { how: "E2E_MOCK=1 pnpm run test:e2e", requires: [] }
  dev:  { url: https://dev.example.com, login: "e2e-owner (pass claude/e2e-owner)" }
  prod: { url: https://example.com }
```

Three real project shapes, expressed in the manifest — the point being that one mechanism covers
all of them:

| Shape | `entry` | `repos` | Comment |
|---|---|---|---|
| A workspace over many repositories, itself not a git repo | `docs/next-prompt.md` | listed explicitly | the docs directory is synced separately |
| A monorepo | `next-prompt.md` (default) | `['.']` (default) | worktrees in a sibling directory (the default pattern) |
| A single repository on another forge | `next-prompt.md` (default) | `['.']` | `forge: {kind: github}`; idea phase |

If there is no `project.yaml` anywhere above: the skill stops with one line ("No project.yaml
found above <cwd>. Create one with `board project init`") — it does not guess the project.
`board project init` writes a minimal manifest (name = directory name) that a human then edits.

The manifest is deliberately YAML rather than a registration on the board because: it is versioned
with the project, it works without a network, and a human changes the phase by editing one line.
The board keeps a *copy* (`projects.manifest` as JSON) so that an agent on another machine that
does not have the project checked out can still see the phase and the repos.

### 3.3 (b) Technology choice — an honest assessment

The requirements, weighted: **(K1)** append-only events with streams, **(K2)** frequent small
heartbeats, **(K3)** questions and answers with notification, **(K4)** reachable from 4–5 machines
over a VPN or HTTPS, **(K5)** as little code as possible to write *and maintain*, **(K6)** agents
must not be blocked when the service is down, **(K7)** ad-hoc querying ("what happened last
night?").

| Option | K1 streams | K2 heartbeat | K3 Q&A | K4 multi-machine | K5 code we own | K6 down | K7 querying | Verdict |
|---|---|---|---|---|---|---|---|---|
| **SQLite + Python stdlib HTTP** | an `events` table | trivial | trivial + push | one URL | **~400 lines of Python + ~100 of CLI** | a CLI outbox | `sqlite3` | ✅ **v1** |
| Postgres | same | same | same, + LISTEN/NOTIFY | the same HTTP layer is needed anyway | same + credentials and migrations | same | `psql` | ⚖️ equivalent; pick it if the SQLite file ever becomes a problem (it will not at 5 agents) |
| Redis Streams | native (XADD, consumer groups) | native TTL — best on paper | ok | an HTTP shim is still needed for foreign harnesses | Redis has to be operated (the AOF footgun) | same | poor (no SQL) | ❌ |
| An event-sourcing database | **perfect fit** | ok | ok | HTTP/gRPC | heavy, a client library, and a facade is still needed | same | projections must be written | ❌ overkill for 5 agents; the "if we outgrow SQLite" path |
| A graph database | no — events are not a graph | no | no | Bolt/HTTP | a JVM, a query language, operations | same | good at *relationships* | ❌ **the wrong problem**: we do not traverse |
| **A home-grown in-process event log** | **yes — named streams are exactly this** | ok | ok | **no — no network API; replication is log shipping, not write access** | an HTTP layer + auth + a JSON API + a CLI + a container + operations | same | a REPL | ❌ for v1 / ✅ as a later backend behind the same API |

**On building it on your own event log, directly:** the coordination problem is almost pure event
sourcing, and `append`/`read`/`subscribe` over named streams is *exactly* the primitive the board
needs. The data model is not the problem. What is missing is everything outside the data model:

- **No HTTP server.** You would write routes, JSON mapping, bearer auth, TLS termination, an image,
  a health probe. That is a week, not a day — and the result can only be debugged by one person at
  3 a.m., because AI agents are markedly weaker in a niche language than in Python and SQL.
- **The event log lacks retention and consumer groups.** Heartbeats are 60 %+ of the volume;
  without retention the log grows until the snapshot takes real time.
- **Corners that execute code from a file on disk at load time** are harmless in a single-user
  setup, but they are exactly the kind of corner that makes the whole system worse.
- **Replication does not solve the multi-machine requirement.** Replicas are read copies; every
  write has to reach the primary over the network anyway — i.e. the HTTP layer that does not exist.

**What would make it a *good* later candidate:** the board is small (thousands of events, one
writer, no concurrency requirement), has a natural stream model, and needs snapshot + log. Once
the HTTP API is defined (§4) and has a conformance suite (curl-based, runnable against any
backend), another implementation can be held to the same contract and run in shadow mode. That is
how you find out whether it holds up on a real workload without the agents ever depending on it.
**The condition for switching:** the conformance suite green, retention in the log, the unsafe
corner closed, and a container with a health probe.

This is also why `conformance.sh` takes a URL argument, and why it refuses to inherit `BOARD_URL`
from the environment. The suite is the contract, and the contract is the thing that makes the
storage choice reversible.

### 3.4 Data model

One append-only table plus projections updated in the same SQLite transaction (no separate
projection process; the simplification is that the projections are "materialized views by hand").

```sql
CREATE TABLE events (
  id      INTEGER PRIMARY KEY,          -- global, monotonic
  ts      TEXT NOT NULL,                -- ISO-8601 UTC
  project TEXT NOT NULL,                -- project name, or '_global'
  stream  TEXT NOT NULL,                -- 'agent/<id>' | 'task/<id>' | 'question/<id>' | 'project'
  type    TEXT NOT NULL,                -- 'agent.heartbeat', 'task.claimed', ...
  actor   TEXT NOT NULL,                -- an agent id, the human's name, or 'board'
  body    TEXT NOT NULL                 -- JSON
);
CREATE INDEX events_stream ON events(project, stream, id);

CREATE TABLE projects (
  name TEXT PRIMARY KEY, phase TEXT NOT NULL,          -- idea|build|launch|live
  goal TEXT, manifest TEXT,                             -- a JSON copy of project.yaml (§3.2b)
  manifest_host TEXT, manifest_path TEXT, updated TEXT  -- where the copy was last read from
);
CREATE TABLE agents (                                   -- GLOBAL: one row per session, any project
  id TEXT PRIMARY KEY, harness TEXT,                    -- 'claude-code'|'codex'|'grok'|…
  host TEXT, model TEXT, session TEXT,
  current_project TEXT,                                 -- what the manifest at cwd resolved to
  capabilities TEXT,                                    -- JSON list, self-declared, global
  preference TEXT,                                      -- role preference (§3.8)
  last_seen TEXT, status TEXT,                          -- alive|stale|stalled|dead|finished
  ctx_pct REAL, budget TEXT,      -- [{"window":"5h","used_pct":63,"resets_at":"…"}], T-164
  current_task TEXT
);
CREATE TABLE grants (                    -- per (agent, project); only the policy writes these
  agent TEXT, project TEXT, grant_name TEXT, PRIMARY KEY (agent, project, grant_name)
);
CREATE TABLE tasks (
  id TEXT PRIMARY KEY, project TEXT, repo TEXT, title TEXT, spec TEXT,
  status TEXT,          -- open|claimed|in_review|awaiting_human|merging|done|blocked|orphaned|archived
  requires TEXT,        -- JSON: capabilities needed ['browser-test','kubectl-dev']
  needs_grants TEXT,    -- JSON: grants needed ['merge','deploy-prod']
  touches TEXT,         -- JSON: file surfaces (a collision hint)
  risk TEXT,            -- low|normal|high  (auth/payment/migration/prod-infra = high)
  owner TEXT, lease_until TEXT,
  worktree TEXT, branch TEXT, pr TEXT, merge_sha TEXT,
  created TEXT, updated TEXT, priority INTEGER
);
CREATE TABLE questions (      -- questions AND test cards: same lifecycle, different `kind` (§5b)
  id TEXT PRIMARY KEY, project TEXT, task TEXT, asked_by TEXT,
  kind TEXT NOT NULL,             -- question | test
  card TEXT,                      -- JSON for kind=test: {repo, env, url, login, steps[], expected[], risk, rollback}
  text TEXT, options TEXT, default_answer TEXT, deadline TEXT,
  status TEXT,          -- open|answered|defaulted
  answer TEXT, answered_by TEXT, answered TEXT
);
CREATE TABLE roles (   -- one row per (project, role, agent); coordinator is a singleton per project
  project TEXT, role TEXT, agent TEXT,
  source TEXT,          -- pinned (by the human) | elected (by the ranking)
  pinned_by TEXT, since TEXT, lease_until TEXT,
  PRIMARY KEY (project, role, agent)
);
CREATE UNIQUE INDEX roles_singleton ON roles(project, role) WHERE role = 'coordinator';
CREATE TABLE messages (   -- an agent→agent inbox
  id INTEGER PRIMARY KEY, project TEXT, to_agent TEXT, from_agent TEXT,
  text TEXT, task TEXT, read INTEGER DEFAULT 0, ts TEXT
);
```

**Event types (the complete list for v1):**
`project.phase_set`, `project.paused`, `project.resumed`, `agent.registered`, `agent.heartbeat`,
`agent.capabilities_set`, `agent.grants_set` (only `board` or the human as actor),
`agent.finished`, `agent.presumed_dead`,
`task.created`, `task.claimed`, `task.progress`, `task.patched`, `task.blocked`,
`task.review_result`, `task.merge_requested`, `task.merge_verified`, `task.released`,
`task.orphaned`, `task.deployed`, `task.done`, `task.archived`, `task.comment`,
`task.default_overridden`,
`question.asked`, `question.answered`, `question.defaulted`, `message.sent`,
`human.test_requested`, `human.test_result`,
`role.pinned`, `role.unpinned`, `role.claimed`, `role.released`, `role.recommended`.

Heartbeats are written to `events` like everything else. A nightly job can compact
`agent.heartbeat` older than 7 days to one per hour (K1 without the file growing).

### 3.5 Heartbeat and token reporting — the mechanism

**Claude Code:** the status line script already receives everything needed. Append to the bottom of
`~/.claude/statusline-command.sh` (the file is in `hooks/statusline-heartbeat.sh`):

```bash
# board heartbeat: never block the status line (1 s timeout, backgrounded, errors ignored)
[ -n "$BOARD_URL" ] && ( jq -c '{session:.session_id, model:.model.id,
   ctx_pct:.context_window.used_percentage,
   budget:[{window:"5h", used_pct:.rate_limits.five_hour.used_percentage,
            resets_at:.rate_limits.five_hour.resets_at},
           {window:"7d", used_pct:.rate_limits.seven_day.used_percentage}]
           | map(select(.used_pct != null)),      # no reading ≠ a window at 0
   cwd:.workspace.current_dir}' <<<"$input" \
 | curl -s -m 1 -H "Authorization: Bearer $BOARD_TOKEN" -H 'Content-Type: application/json' \
     -d @- "$BOARD_URL/api/v1/agents/$AGENT_ID/heartbeat" >/dev/null 2>&1 & )
```

`AGENT_ID` is set by `/next` at registration (written to `~/.cache/board/<board>/session-<id>`;
the script reads it back from `session_id`). The status line updates on every turn and tool call →
a heartbeat every few tens of seconds while the agent works, and it **stops when the process
dies**. That is the entire death detection. A `SessionEnd` hook posts `agent.finished` on a clean
exit.

**Harnesses without a status line (Codex, Grok):** no equivalent hook. They call `board heartbeat`
explicitly in their loop, or run `hooks/heartbeat-loop.sh` alongside the session. Their token
report is best effort — most harnesses do not expose quota at all, and an empty window list is a
valid, honest answer that the fallback ceiling then covers (§3.7, `budget.fallback`).

**Thresholds the board derives:** `alive` = seen in the last 5 min; `stale` = 5–60 min; `dead` =
more than 60 min; `stalled` = seen in the last 5 min, but the lease on the task has expired.

**The heartbeat does NOT renew the lease — only `claim` and `task.progress` do (Q-107).** This is
the distinction the mechanism actually needs, and it took a revision to get right. The first
version renewed the lease on every heartbeat, on the theory that a healthy agent is *constantly*
inside one long step (an implementation subagent, an acceptance run, three reviewers) and cannot
report `task.progress` from inside a tool call, so it should keep its task. But that makes a
process that is alive and stuck indistinguishable from one that is alive and working: it holds the
task forever. Tying the lease to *progress* instead means a process that has sat for longer than
one 5-hour window without progress is `stalled` — not out of quota, something else is wrong — and
another agent can take over. `stalled` never counts as `alive` for `task next` or the role
ranking, but it shows in `board status` and it does not strip a role a human pinned.

The reaper also never orphans `awaiting_human` or `blocked`: those are documented waits on a
human, not stalls. If the *owner* dies, they can still be taken over.

### 3.6 Project phase — the declared input

**Where:** `phase:` in the project manifest `project.yaml` (§3.2b) — at project level, i.e. above
all the repositories the project consists of, versioned with the project. Not in the entry file
(that is overwritten every session and is *status*), and not primarily on the board (it has to be
readable when the board is down). `/next` mirrors the file to the board at startup
(`project.phase_set`); the board holds it for cross-machine visibility and for gate checks.

**The phase drives four things:**

| | `idea` | `build` | `launch` | `live` |
|---|---|---|---|---|
| **Test level (§5)** | direct production testing is fine, breaking changes are fine | dev; non-breaking only | dev always; a human OK for `risk=high` | mandatory automated levels; human OK for anything visual; migrations need a rollback plan |
| **Merge gate** | the mechanical checks | + review findings closed | + `risk=high` → human OK | + every risk level → human OK |
| **Prod deploy** | free | free for non-breaking | a merge that auto-deploys is **treated as a deploy decision** | requires an explicit `deploy-prod` grant per task |
| **Model (default)** | one notch cheaper: sonnet for design too | the table in §6.3 | the table in §6.3 | + an Opus review in addition to the Sonnet finders on `risk=high` |

The model choice should follow the phase only in *idea* (cheaper, faster iteration). In
launch/live it is not the model that should be tightened but the *review* — a mistake there costs
users, and an extra Opus review on a high-risk diff is cheaper than a rollback.

### 3.7 The capability register — capabilities vs. grants

**Decision: capabilities are self-declared, grants are assigned.** The reasoning: an agent *knows*
whether it has Playwright or kubectl (it can test it), but it must not be able to *tell itself* it
may merge — that is exactly the failure in §2 where a review fork inherited a merge mandate and
acted on it. The assignment lives in a policy file the board reads, edited by the human:

```yaml
# board-policy.json (on the board, not in any repo)
grants:
  myproj:
    claude-code@laptop:   [merge, deploy-dev]
    claude-code@desktop:  [merge, deploy-dev]
    codex@*:              [deploy-dev]      # codex does not get merge until we trust it
    "*":                  []
  sandbox:
    "*":                  [merge, deploy-dev, deploy-prod]   # idea phase, no users
```

The phase can only ever *tighten* what a grant allows, never widen it (§3.6).

**The capability vocabulary (v1, freely extensible):** `browser-test`, `playwright`,
`kubectl-dev`, `ssh-prod`, `gradle`, `flutter-build`, `pass-secrets`, `forgejo`, `github`,
`reports-quota`, `interactive`. The agent registers with what it has *verified* at startup (e.g.
`kubectl get ns` succeeding → `kubectl-dev`), not what it believes. That is what `board probe`
does.

**Expiry:** capabilities live exactly as long as the agent is `alive`. `stale`/`dead` agents never
match. There is no separate TTL on capabilities — the heartbeat *is* the TTL.

**Matching at claim time:** `board task claim <task>` succeeds only if
`agent.status == alive ∧ task.requires ⊆ agent.capabilities ∧ task.needs_grants ⊆ agent.grants`
— checked on the board, in one SQLite transaction
(`UPDATE tasks SET owner=? WHERE id=? AND owner IS NULL` → 0 rows = lost the race).

**The question-queue loop:** the board itself holds the notification credentials — it does not
depend on an agent being alive to reach the human. That is deliberate: at 4 a.m. every agent may
be dead of quota, and the question should still be on the phone when the human wakes up. Agents
with their own notification capability are an *additional* channel the board could route through
later, not the precondition. The answer comes back through the board's own answer form
(`/q/<id>`, one HTML page served by the same Python file) or `board answer <id> "..."` from any
machine.

### 3.8 Roles — manual appointment and the agents' own "consensus"

**A role is not a capability.** A capability is what the agent *can* do (§3.7); a role is the *job*
it has now. The roles: `coordinator` (a singleton per project), `implementer`, `reviewer`,
`tester`. Only the coordinator needs uniqueness; the others are labels `board task next` uses as
filters (a `reviewer` gets `in_review` tasks first, a `tester` gets tasks with
`requires: browser-test`). Each role declares which capabilities it requires, in the policy file —
a role can never be held by an agent that lacks them:

```yaml
roles:
  coordinator: { singleton: true, requires: [merge], prefer_model: [fable, opus, sonnet] }
  reviewer:    { requires: [] }
  tester:      { requires: [browser-test] }
  implementer: { requires: [] }
```

**1. Manual appointment — the human's word wins.** From the session itself: `/next coordinator`
(or `/next reviewer`, `/next tester`) → the skill runs `board role pin <role> --me`. From any
terminal: `board role pin coordinator --agent cc-desktop-1` / `board role unpin coordinator`.
A pinned row has `source=pinned, pinned_by=<human>` and:

- is **never** overwritten by an election while the agent is `alive` or `stale` — not even if the
  ranking (below) says otherwise;
- is **released automatically when the agent goes `dead`** (>60 min without a heartbeat, §3.5). An
  election then happens, and the board pushes: "pinned coordinator cc-laptop-7f3a dead 61 min —
  cc-desktop-1 took over". The alternative — standing without a coordinator until the human wakes
  up — is precisely what we are trying to avoid. If the human starts a new session with
  `/next coordinator`, that one is pinned and the elected one steps aside (`role.released`).

Pinning requires the human token (§3.7). Otherwise an agent could write `pinned_by: <human>` about
itself and become untouchable — the pin mechanism would protect the wrong thing.

**2. The agents' consensus — without Raft.** There is one store, so this is not a distributed
problem; "consensus" is a **deterministic ranking over data everyone can see**. Every agent (and
the board, via `GET /roles/recommendation`) computes the same answer from the same state:

1. `alive` and satisfies `roles.coordinator.requires`
2. has not set `prefer: implementer` (the agent's own vote — "I am mid-way through a contract
   change across three repos, do not make me coordinator"; set with `board caps --prefer
   implementer`)
3. model, by `prefer_model` (fable > opus > sonnet)
4. lowest reported quota window (most budget left — the coordinator should outlive everyone)
5. earliest registered (stability; avoids flapping)
6. lexically lowest `agent.id` (a tie is impossible)

Point 2 is the entire "agents propose among themselves" mechanism: an agent that *knows* something
the ranking does not expresses it as a preference in data, not as a round of messages. That is
enough. A propose/ack protocol between agents was considered and rejected: it is chatty, agents
are often busy (silence ≠ consent), and everything it could express fits in the preference field.

**Enforcement:** `board role claim coordinator` is a compare-and-swap — it refuses while a pinned
row exists whose holder is not dead, and refuses while a non-pinned row exists whose holder is
alive. Two agents claiming at once → one gets the row, the other gets a 409 and carries on as
`implementer`. A claim that does not match the top of the ranking is refused (409 with
`recommended: <id>`) — so an agent cannot sneak into the role even when the row is free.
**A role claim is the one operation that is never queued in the outbox** (§4): if the board is
down, everyone keeps the role they have.

**Binding, or a proposal?** **Automatic enforcement when the role is not pinned; logged as
`role.claimed` plus a push; never automatic while a living pinned holder exists.** The reasoning:
a proposal sitting on the question queue at 4 a.m. is identical to "no coordinator until
tomorrow". The human sees the event in `board tail` or the push and can override with one command.
The ranking is deterministic and logged with its reason
(`role.recommended {reason: "fable, 5h=12%, registered 02:10"}`), so it stays auditable.

**Takeover — the role state lives on the board, not in anyone's head.** The coordinator *owns*
nothing special: tasks have their own owners, questions are bound to *tasks* (`questions.task`),
messages sit in the recipient's inbox, and the entry file is a file. When a new coordinator claims:

- `board inbox --as coordinator` additionally returns answered questions and human-test results on
  **unowned or orphaned** tasks in the project — an answer is never lost because the asker died.
- Tasks the dead coordinator had claimed follow the orphan procedure (§7) and come first from
  `board task next`.
- Agents that were "waiting for word" were waiting on their inbox, not on a particular
  coordinator — they do not notice the change.
- The new coordinator runs the startup step in the skill (inbox + `board status`) and continues the
  loop. The only state that *can* be lost is what the dead coordinator held in context without
  having written `board task progress` — which is why writing progress is mandatory after every
  step, not optional.

---

## 4. API / protocol

See [skill/reference/protocol.md](skill/reference/protocol.md) for the full operation table — it
is the same document the agents load, and keeping one copy is the point.

The shape of it: everything is `Authorization: Bearer <token>`, JSON, under `/api/v1`. The `board`
CLI is a 1:1 wrapper and what the agents actually use.

**CLI rule K6:** when the server does not answer within 2 s, the CLI appends the call to
`~/.cache/board/<board>/outbox.jsonl`, returns exit 0 with `{"queued":true}`, and flushes the
outbox on the next successful call. Reads return the last known answer from the cache with
`"stale":true`. **The agent always carries on.**

The whole cache — agent id, GET answers and outbox — lives under a directory keyed on
`BOARD_URL`. Without that, registering against a local test instance overwrites the identity you
hold against the production board, and — worse — queued writes could be flushed to the wrong
board.

Two deliberate exceptions to "never block":

- **A role claim is never queued.** If the board is down, everyone keeps the role they have; a
  queued claim would be enacted minutes later against a state that has moved on (§3.8).
- **Commands that act on the board's answer** — `task pr`, `task worktree`, `task cleanup` —
  refuse a cached answer. They push branches and delete worktrees; acting on a stale answer means
  pushing the wrong branch or deleting the wrong directory. Reads are still never blocking; this
  applies only to the three that write to disk or to the forge.

And one that fails closed rather than open: **`board gate merge` refuses a cached answer.**
Everything else in the CLI fails open, because the cost of a false stop is a stalled agent. Here
the cost of a false pass is a merge nobody approved.

---

## 5. Browser testing — the level derived from phase + change class

Three levels:

| Level | What | Who | Cost |
|---|---|---|---|
| **L0 mock** | Playwright against a mock backend, or a local dev server plus screenshots | the agent itself | minutes |
| **L1 dev** | The dev environment from the manifest — Playwright against a live stack, or a human | the agent (with `playwright`) or the human | minutes–hours |
| **L2 prod** | Production | the human, exceptionally an agent | real risk |

**The change class** is derived mechanically from the diff: `ui` (only markup/css/i18n), `logic`
(code without a migration), `contract` (OpenAPI/DTO/route change), `data` (SQL/migrations),
`infra` (helm/k8s/CI), `money-auth` (paths under payments or auth).

**The decision table (the agent looks it up; it does not use judgement):**

| Phase \ class | ui | logic | contract | data | infra | money-auth |
|---|---|---|---|---|---|---|
| idea | L0 | L0 | L0 | L2 ok | L2 ok | L1 |
| build | L0+L1(auto) | L0+L1(auto) | L1(auto) | L1 + human | L1 | L1 + human |
| launch | L0+L1(auto) | L1(auto) | L1 + human | L1 + human + rollback plan | human | L1 + human |
| live | L0+L1(auto) | L1 + human | L1 + human | L1 + human + rollback plan + backup check | human | L1 + human + Opus review |

"(auto)" = an agent with the `playwright` capability runs it itself; "human" = a test card goes to
the human, the task becomes `awaiting_human`, and the agent takes the next task.

**Two corrections the table alone does not capture**, both learned the hard way and both now in
`board test-level`:

- **`auto` with nothing to run means nobody looks.** If the agent lacks `browser-test`, or the
  manifest declares no `environments.<env>.how` for that surface, the answer is `human` whatever
  the table says — and `why` says which. The temptation is to work around it by declaring the
  capability; the fix is to add the automation, or file the card.
- **A visual change is always `human`.** `how` can prove the API works. It can never prove the
  page looks right. Any diff touching markup, CSS or a template goes to a human regardless of
  class.

**The test card** (what the agent writes for the human) — a fixed template, at most 12 lines,
pushed as a notification and shown at `/tests`:

```
T-42 · web · L1 dev · ~3 min · risk: normal
URL: https://dev.example.com/queues/123/config   Log in as: e2e-owner (pass claude/e2e-owner)
1. Open the "Products" tab → expect: a new "Show on board" button under each product
2. Turn it on for "Coffee", open /screen/<token> in a new tab → expect: "Coffee" within 5 s
3. Turn it off → expect: gone within 5 s, no console errors
If it fails: answer "fail: <what you saw>" — the agent picks it up in its inbox.
Rollback: `kubectl -n myproj rollout undo deploy/web`
Answer: [OK] [FAIL]
```

The card must contain *an expectation per step* and *a rollback command* — without them it is not
valid, and `board test request` refuses it (a schema check, not an AI check).

### 5b. The test queue — one definite place for "what do I test today"

**Decision: the test queue IS the question table with `kind: test`** — same table, same inbox,
same notification path, same answer form, same "the agent moves on and is resumed by the answer".
A test card is a question with a structured body ("does this work?") and a structured answer
(`ok` | `fail: …`). Giving it its own lifecycle would have produced two inboxes, two reaper rules
and two places the human has to look — without anything getting better. What *is* specific to it
is the card's schema and its rendering:

- **One place:** `board tests` (CLI) and `/tests` (HTML) — **across all projects**, sorted by phase
  (live > launch > build > idea), then risk, then age. Each entry shows everything needed without a
  lookup: project/repo/PR, **environment and URL derived from the phase × class table and the
  manifest's `environments`**, login, numbered steps with expectations, the risk if it is broken,
  the rollback command, and two buttons: **OK** / **FAIL** (+ free text).
- **A schema check at submission** (`board test request`): every field in `card` is mandatory;
  `steps`/`expected` must be the same length; `env` must exist in the manifest. An invalid card →
  400, the card never reaches the queue, and the agent gets the error — not the human.
- **The way back:** OK → `human.test_result {ok}` → the task returns from `awaiting_human` to
  `claimed` with its owner and a fresh lease; the owner sees it in `board inbox` and carries on to
  the gate. FAIL → `human.test_result {fail, note}` → the owner dispatches a fix round with the
  note as its spec and files a new card. The answer unblocks the agent exactly as a question
  answer does — it is the same code.
- **No defaults on tests.** A `deadline` is allowed (the card is marked as old), but a test card
  can never `default` to OK. An unanswered question can be guessed; an unverified UI cannot.
- **A project's own test-plan documents become an *export*.** `board tests --project X` generates
  them. The board is the source.

The human's morning routine, concretely: a push says "3 tests waiting (myproj 2, otherproj 1)" →
open `/tests` on the phone → work down from the top → each answer wakes the right agent. No
reading of PRs, logs or the entry file.

---

## 6. The `/next` skill in practice

### 6.1 Placement and form

`~/.claude/skills/next/SKILL.md` — user level, applying to every project. The skill contains **no**
project names, paths or conventions; all of that comes from the manifest (§3.2b) and from the
project's own onboarding file (`onboarding:` in the manifest), which the skill reads *after* its
own rules.

### 6.2 A hard requirement: minimal context consumption

An agent that is going to work all night has a context budget, and the logs that motivated this
design show four agents killed by token limits mid-task. Hence three principles, in priority
order:

1. **The service is the memory, not the prompt.** Tasks, roles, capabilities, questions and the
   test queue are *fetched* from the board with short calls when needed; none of it is baked into
   SKILL.md or into the entry file. The entry file is read **only at bootstrap** — when the board
   has no tasks for the project yet. After that the board is the queue, and the file is a human
   summary that is *written*, not read.
2. **Subagents are a context strategy, not just parallelism.** The implementation (reading 50
   files, compiling, running tests — 50–150k tokens) happens in a subagent whose context dies when
   it finishes; the coordinator sees only the report (≤1k). The same holds for review. The skill's
   prompt templates are read by the *subagent* from a file ("read `prompts/implementer.md` first"),
   never by the coordinator.
3. **Progressive disclosure.** SKILL.md says what the loop is and where the rest lives. Everything
   else is a file loaded only when that situation arises (§6.4). The `board` CLI is itself terse
   (one line per item, `--json` only on request), and `board help <command>` replaces protocol
   documentation in the prompt.

**Ceiling: SKILL.md ≤ 80 lines (≈1.5k tokens).**

### 6.3 SKILL.md

The loop itself is [skill/SKILL.md](skill/SKILL.md) — the real file, not a sketch. The parts worth
calling out as *design* rather than instruction:

**The model choice is a lookup, not a judgement.** Four yes/no questions — is the acceptance
criterion a command? one repo, no contract change? does it touch auth/payment/migration/prod
infra? (for bugs) does a reproduction exist? — and the answer falls out. An agent asked to "use
judgement" about which model to use will reliably pick the expensive one, and the decision is then
unauditable. Logging the answers (`model=… Q1=y Q3=n`) makes it reviewable afterwards.

**The coordinator runs the acceptance command itself.** Not because the subagent lies, but because
a hand-off note is not evidence. This is the one step that cannot be delegated without losing the
property the whole loop rests on.

**The merge happens in a dedicated merge worktree.** Never in the primary checkout — that is the
tree a human may be typing in, and the loop runs unattended at night. `board task merging` refuses
while that tree is dirty, which is the mechanical version of the same rule.

**Stopping is read from the board, not re-derived.** `board status --me` returns `.stop`: non-null
means stop, and the text is the reason. Thresholds belong on the board, not spread across every
SKILL that reads it — a harness may report two windows, five, or none (§3.5, T-164). A
`board status --me` that *fails* means the agent is not registered in the project the answer
covered; an empty answer is never permission to keep claiming.

### 6.4 Progressive disclosure — the files under the skill directory

| File | Contents | Loaded when | By whom |
|---|---|---|---|
| `SKILL.md` | the loop, the stop rules, the hard rules (≤80 lines) | `/next` | the coordinator, once |
| `prompts/implementer.md` | the implementation subagent's mandate: commit in the worktree, never push/PR/merge, the TDD rule, the report format | at dispatch | **the subagent**, never the coordinator |
| `prompts/reviewer.md` | the READ-ONLY mandate, three angles, 0–100 scoring, the JSON format | at dispatch | **the reviewer subagent** |
| `reference/testing.md` | the phase × class table (§5), the test card template, the `environments` lookup | step 9 returns `human` | the coordinator, only then |
| `reference/takeover.md` | orphan takeover, `merge_requested` without `merged`, re-landing, board down / outbox | `task next` returns orphaned; a merge wrapper fails; the CLI answers `queued` | the coordinator, only then |
| `reference/roles.md` | pin/claim/ranking (§3.8), what a new coordinator does | `/next <role>`; `role recommend` points at you; a 409 on claim | the coordinator, only then |
| `reference/handoff.md` | the closing block when the queue is empty or quota runs out | at the end of a run | the coordinator |
| `reference/protocol.md` | the full API table (§4) | when a command is unclear | rarely — `board help` first |
| `reference/deploy.md` | deploy verification per the manifest's rules | step 12 | the coordinator |

A project's own `onboarding:` file is usually the single largest contribution to context and sits
outside the skill's control. Mitigation: the skill reads only the sections the manifest points at
(`onboarding: {file: docs/AGENT_ONBOARDING.md, sections: ["2. Workflow", "3. Known hazards"]}`);
the rest is for humans. And critically — the coordinator never reads it at all. It is passed to
the implementation subagent, where the cost dies with that context.

### 6.5 A rough context estimate per `/next` round (coordinator, 200k window)

| Item | Tokens | Note |
|---|---|---|
| Startup: SKILL.md + manifest + `status` + `inbox` | ~2.5k | once |
| Bootstrap read of ENTRY (≤150 lines) | ~2k | **only** when the board is empty for the project |
| Onboarding sections | ~6k | once; 0 for projects without one |
| Per task: `next/claim/worktree/show` | ~0.6k | the CLI is terse |
| Per task: implementation dispatch + report | ~1.2k | the implementation context (50–150k) dies in the subagent |
| Per task: the verification command (`tail -30`) | ~0.8k | |
| Per task: 3 review reports + 1 fix round | ~2.5k | |
| Per task: PR/gate/merge/verify/cleanup | ~1.2k | |
| Per task: a test card (when human) | ~0.6k | |
| **Sum per task** | **~7k** | against 50–150k if the coordinator implemented it itself |
| Rewriting ENTRY every fifth task | ~2.5k | |

≈ 10k fixed + 7.5k/task → **about 20 tasks before ctx hits 80 %**. The 5-hour quota runs out long
before that; context stops being what kills the session. The precondition is strict: the
coordinator never `cat`s source files, never reads full CI logs, and lets the subagents read the
prompt templates themselves.

### 6.6 Why the model criteria are what they are

Sonnet fails predictably on two things: *working out* what should be done (an unclear spec), and
holding a contract consistent across repositories. Both are Q1/Q2. Opus rarely fails there, but
costs 5×. Q3 is not a competence question — it is that the *cost of being wrong* is users or
money, at which point the model price is irrelevant. The criteria are yes/no so that two
coordinators choose the same way, and so that the choice can be logged.

### 6.7 Packing — where the line is

The suggestion that "the skill does not need to be human-readable, so it can be packed" is half
right. Measured with a tokenizer on one and the same rule in four forms (an OpenAI tokenizer used
as a *proxy* — Claude's is different, but the pattern holds):

| Form | Chars | Tokens | Readable? |
|---|---|---|---|
| Prose — a full paragraph explaining the gate, what a non-zero exit means, and what to do | 498 | **138** | yes |
| **Compact, readable** — "Merge only after `board gate merge $T` = 0. Not 0 → do what the reason says (CI red: wait; findings open: fix; phase needs a human: test card), go to step 1. The task stays yours." | 175 | **60** | yes |
| Abbreviated — "Mrg only if `board gate merge $T`=0. ≠0→do rsn (CI red→wait; fndgs opn→fix; ph req hmn→tstcard), goto st1. Tsk stays urs." | 121 | **53** | barely |
| Cryptic — "mrg⇐gate($T)==0; !0→act(reason)∧goto 1; own(T)↑" | 47 | **26** | no |

And at word level: `reason` = 1 token, `rsn` = 2; `findings` = 2, `fndgs` = 3; `→` = 1, `⇐` = 2–3;
`∧` = 2.

**The conclusion, with numbers:**

1. **Prose → compact-readable saves 57 %.** That is nearly the whole gain, and it costs nothing in
   readability. That is where the effort belongs.
2. **Compact → abbreviated saves a further 12 % of tokens for 31 % of the characters.**
   Abbreviations tokenize *worse* per character (2.3 vs 2.9 chars/token). What you win is not
   worth "≠0 → do rsn" being misread at 4 a.m.
3. **Cryptic saves another 57 %, but is not auditable.** A rule the owner cannot read is a rule
   they cannot correct, and therefore cannot let run while they sleep. Rejected.
4. **Structural savings beat all of it:** a `reference/takeover.md` that is *not loaded* saves
   100 % of itself. The whole of SKILL.md measures ~1.5k tokens — packing it to 1.0k saves 0.25 %
   of a 200k window. Not worth one misread rule.
5. **Language is a bigger lever than abbreviation, and it is free:** the same compact rule measured
   **51 tokens in English against 60 in the author's native language (−15 %)** with *more*
   characters — words in a smaller language split more often. That is why the skill files were
   written in English from the start, and it is one of the reasons the rest of this repository
   followed.

**The rule for the skill files, briefly:** tables and imperatives, not prose; one fact in one
place (SKILL.md points, the reference files own); whole words, standard arrows (`→`), no
home-made abbreviations or symbol languages; "why" only where an agent would otherwise break the
rule (e.g. "NEVER fork for review — the fork inherits your mandate"). The test for whether a line
is too packed: can a sleepy human read it once and say what the agent was supposed to do? No →
write it out again.

---

## 7. Failure modes

| Situation | What happens | Why it is safe |
|---|---|---|
| **The board is down** | The CLI queues writes in the outbox and reads from cache with `stale:true`. The agent carries on with the task it has. A `board task next` served from cache can hand out a task somebody else took meanwhile → the CAS fails at flush; the agent notices at its next `board status` and releases it. | The board runs next to the forge and the cluster. If it is down, merge and CI are usually down too — nothing is lost by the board being away. It is *designed* so that the board's uptime need not exceed the forge's. |
| **An agent dies mid-task with an open worktree** | The heartbeat stops → `stale` after 5 min, `dead` after 60 → the lease expires → the reaper sets `task.orphaned`. If the process is alive but not progressing, the lease is not renewed (only `claim` and `task.progress` renew it), the agent shows as `stalled`, and the task is orphaned the same way. `awaiting_human` and `blocked` are never orphaned by lease expiry. The next `board task next` returns the orphaned task **first**, with `worktree`, `branch`, `pr` and the last `progress` note. The new agent runs `git -C <worktree> status` and continues. | Everything needed for a takeover is structured on the board, because writing progress after every step is mandatory. The worktree directory is on the machine that died — if the new agent is elsewhere, it rebuilds from the pushed branch instead (`board task worktree` does this, and reports `base: origin`). |
| **Two agents take the same task** | `UPDATE … WHERE owner IS NULL` → one gets 1 row, the other 0 → 409. | SQLite serializes writes. With the board down (outbox) both can *believe* they have it; at flush the first wins and the second gets a 409 and must release — worst case duplicated work on one task, never corrupt state, because they have *separate worktrees*. |
| **Out of tokens mid-merge** | `task.merge_requested` is written before the merge. A taking-over agent sees `merge_requested` without `merge_verified` → runs only the verification step (ancestry, the PR's state on the forge) — it does **not** re-merge. | Merge is one idempotent operation on the forge; the state is read from the forge, not from the board. The stop rule "do not start a merge near the ceiling" makes this rare. |
| **A question unanswered for hours** | The deadline passes → `question.defaulted`. The agent has already implemented the default; the PR is marked. `risk=high` is never merged on a default. | Better a PR that assumes B and says so than no PR. And if the human later answers differently, the board turns the override into a new task — the loop closes even when the work is already merged (T-198). |
| **A foreign harness without a heartbeat** | It is orphaned sooner. | It loses at most a few minutes of work to a false orphan, and orphaning is *not* destructive — the worktree and branch stand. |
| **The coordinator dies** | The lease expires → the top of the ranking among the living claims it → `role.claimed` plus a push. The new coordinator runs `inbox --as coordinator` and receives answers to the dead one's open questions. | Role state, questions and task ownership live on the board (§3.8); the dead coordinator held nothing exclusively in memory that was not also written as `task.progress`. |
| **Two agents believe they are coordinator** | Impossible on the board: a unique index plus a CAS. It can only happen as a *belief* while the board is down — and a role claim is not allowed then (it is not queued in the outbox), so both keep their previous role. When contact returns, the row decides. | One truth, no distributed state. |
| **A pinned coordinator dies while the human sleeps** | The pin is released on `dead` (60 min) → an ordinary election → a push. If the human comes back with `/next coordinator`, the elected one steps aside. | Without release, the project stands without a coordinator until morning; with it, the human can always override. |
| **Production crash-loops after a merge that auto-deploys** | The deploy verification step notices (readiness, restarts) and creates a P0 "rollback" task that is taken first; `kubectl rollout undo` is in the test card's rollback line. | A known trap, and the reason the deploy step exists between merge and done rather than after it. |

---

## 8. Security / robustness — what mechanically prevents a half-finished merge and uncovered production

The original protection was *prompt text* ("run a review before merging"). The incidents in §2
show that this does not hold. Three mechanical layers, in order of cost:

1. **Branch protection on `main` in every repository.** Require the status checks green, block
   direct pushes. Zero code. This alone stops "push to main" and "merge on red CI". It is a *user
   step*: it needs admin rights, and an agent's forge token is not an admin token.
2. **A `PreToolUse` hook** (`hooks/gate-merge.sh`): matches `Bash` against `tea pr merge*`,
   `gh pr merge*` and pushes to main → runs `board gate merge <T>` (the task id from
   `~/.cache/board/<board>/current-task-<session>`) → exit ≠ 0 ⇒ the hook returns `deny` with the
   reason. This works in *subagents too*, because hooks are per process. It fails **open**: if the
   board is down or the task is unknown, it does not interfere — a gate that blocks work when the
   board hiccups would be worked around within a day, and then it protects nothing.
3. **The gate on the board is data-driven, not trust-based.** It requires
   `task.review_result.open == 0`, requires that the review event came from an agent id other than
   the task's owner, requires `human.test_result == ok` when the phase table says so, checks that
   the agent holds the `merge` grant, and holds a mutex so only one merge is in flight per project.
   An agent cannot set grants (a policy file; only `board` or the human as actor).

In addition, cheap protections already learned: review agents are never forks; `model:` is always
explicit; one PR at a time with an ancestry check; `board task merging` refuses while the task's
working copy is dirty.

**What this does NOT guard against:** an agent that writes `board task review --open 0` without
having run a review. That is lying in its own log, and there is no mechanical gate against it
short of running the review *inside* the board. The owner check narrows it — the agent has to lie
about someone else's work rather than its own, which is the case that actually occurred — but it
does not close it.

---

## 9. The phased implementation plan

| Phase | Contents | Value on its own | Estimate |
|---|---|---|---|
| **0 — The board** | `board.py` (stdlib, SQLite, HTTP, the `/q/<id>` and `/status` pages), the `board` CLI with its outbox, the k8s manifest and ingress, the statusline heartbeat, the `SessionEnd` hook, push on `question.asked`/`task.blocked`, the reaper. A conformance suite: one bash script with curl that runs every operation in §4. | **Yes:** real-time "who is alive, how much quota do they have, what do they own" plus questions that reach the phone. Replaces a hand-edited file listing active agents. | 1 day |
| **1 — `/next` v1** | SKILL.md (§6), manifest discovery (§3.2b) + `board project init`, a `project.yaml` per project, the PreToolUse gate hook, `board task create` from the entry points. Run first *with a human awake* — and on an idea-phase project with no users before anything else. | The loop runs end to end on one repository with Sonnet workers, in three projects of different shapes. | 1–2 days |
| **2 — Robustness** | Leases and orphan takeover tested by killing a session on purpose; branch protection (a user step); an onboarding prompt for foreign harnesses using the same CLI; installation on a second machine. | Several agents, harnesses and machines without collisions. | 1 day |
| **3 — The human loop** | The test card schema and the `/t/<id>` page, `human.test_result`, the phase table in the gate, an Opus review on `risk=high`. | Night work in `launch` phase becomes defensible. | 1 day |
| **4 — Optional** | An alternative backend behind the same API in shadow mode, held to the conformance suite; additional notification channels; heartbeat compaction. | Dogfooding another store without risk. | open |

Phase 0 can be built by a Sonnet agent with the spec = the §4 table + the §3.4 schema + the
conformance script as its acceptance criterion (Q1=yes, Q2=yes, Q3=no). It was.

---

## 10. Risks, and what I do NOT recommend

**Risks**

- **Lying in your own log** (§8) — the agent reports review-clean without a review. Narrowed by
  requiring a fresh reviewer; not eliminated.
- **Permission classifiers in automatic mode** refuse some merges and deletions. `/next` must not
  "try variants"; on a refusal → `board task progress "READY FOR MERGE, blocked by the permission
  layer"` plus a notification. Unsolvable without the user's allow-list.
- **Auto-deploy on merge makes "merge" and "deploy" the same decision.** The phase table treats it
  that way, but it is structurally fragile — a tag-gated production deploy would remove an entire
  class of risk. Out of scope here, but noted, because the board cannot repair it.
- **The statusline hook only runs in interactive sessions.** Subagents do not inherit their own
  heartbeat — they count under the coordinator's session. Fine for an interactive harness; for
  batch runs the heartbeat has to be explicit (`hooks/heartbeat-loop.sh`).
- **SQLite on a PVC: one writer, one pod.** Do not scale to 2 replicas. Backup = copy the file;
  `k8s/board-backup.yaml` is that copy, using SQLite's `.backup` rather than `cp`, because a file
  copied mid-write is corrupt.
- **Quota numbers are per account.** Two `/next` sessions on different machines, or in different
  projects, share both the 5-hour and the 7-day window. The board therefore computes the maximum
  itself and serves it in the `budget` block — an agent must never read its own window as its stop
  criterion. The windows are a LIST (`agents.budget = [{window, used_pct, resets_at?}]`), not two
  fixed columns: other harnesses have different names, different counts, or no introspection at
  all, and must be able to register without a code change. The maximum is taken per WINDOW NAME
  and per HARNESS account, and `agent.stop` compares only the windows THE AGENT ITSELF reports —
  an unknown window name from one agent must not stop the fleet in projects that do not have that
  name (T-164). If an agent reports no windows at all,
  `budget.<project>.fallback.max_tasks` applies — coarse, but more honest than free rein.

  The asymmetry between windows matters: a 5-hour window heals overnight, a week-long one does
  not, and it is the same budget the human works from. The ceiling
  (`budget.<project>.ceilings`) is therefore the human's decision in the policy, like `phase`, and
  a missing one means a ceiling of 0. Because 0 ≥ 0 is always true, a missing line stops the
  ENTIRE fleet — so the board distinguishes "set to 0" from "missing": when it is missing, the
  `budget` block carries `ceilings_missing` + `note`, the status page shows it, and `finished`
  pushes the reason. **Deploying board.py requires adding `budget.<project>.ceilings` to the live
  policy in the same motion; otherwise every agent in every project stands down at its next status
  check.**
- **Convention drift between projects.** The manifest covers *shape* (paths, repos, worktrees), but
  not project-specific *rules* ("merge means production here", migration numbering, explicit
  `model:`). Those belong in the project's `onboarding:` file, and the skill has to read it. A
  project without one gets only the skill's general rules — right for an idea-phase project, wrong
  for a live one. The gate: `phase ∈ {launch, live}` without `onboarding:` → the skill refuses to
  start.

**Do NOT recommend**

- Building on a home-grown event store as the primary in v1 (§3.3) — the data model fits, the
  network layer does not exist, and the maintenance is one person. Build the API storage-agnostic;
  test the alternative behind it later.
- A graph database — we do not traverse. If the interest in graphs is about *knowledge*
  (docs/memory/skill relationships), that is a different project from coordination — see
  `graph.py`, which is exactly that and deliberately separate.
- Redis or an event-sourcing database — the right shape, the wrong weight for 5 agents and one
  user.
- A dedicated "coordinator daemon" that dispatches agents automatically (spawning batch sessions).
  Tempting, but it moves decisions into a process nobody watches, and the token cost becomes
  invisible. `/next` started by a human, with the board as the truth, is enough.
- `fork` as a subagent type for anything other than a pure continuation of your own task.
- Putting the phase in the entry file — that file is overwritten every session; policy must
  outlive status.
- Letting agents assign themselves grants, or letting the phase *widen* grants (it can only
  tighten).
- Polling CI in the foreground ("wait for the monitor") — a known loss; always
  `board task progress` plus the next task.
