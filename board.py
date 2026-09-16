#!/usr/bin/env python3
"""board — coordination board for AI agents. Phase 0 of DESIGN.md (§3.4 schema, §4 API).

Stdlib only: http.server + sqlite3. One writer, one pod (do not scale to 2 replicas).
Run:  BOARD_TOKEN=... BOARD_DB=/data/board.db python3 board.py
"""
import base64, hashlib, hmac, json, os, re, secrets, sqlite3, sys, threading, time
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH   = os.environ.get("BOARD_DB", "board.db")
TOKEN     = os.environ.get("BOARD_TOKEN", "")
POLICY    = os.environ.get("BOARD_POLICY", "board-policy.json")
NTFY_URL  = os.environ.get("NTFY_URL", "")
NTFY_AUTH = os.environ.get("NTFY_AUTH", "")
# A token only the human holds. Without it `by: "<human>"` is a claim any agent can
# write, and then it can pin itself as coordinator or answer its own question in the
# human's name (T-71, T-72, T-143, Q-156).
HUMAN_TOKEN = os.environ.get("BOARD_HUMAN_TOKEN", "")          # "Bearer tk_..." or "Basic ..."
# The human's name on the board: the actor string that shows up in events,
# `answered_by`, `pinned_by` and on the HTML pages. Configurable because the owner of
# a board is a person with a name, not a hardcoded string — but it MUST stay stable
# for a given board: change it and the history rows stop matching the new identity,
# and the human gate (as_human) no longer recognizes old answers as human answers.
HUMAN     = os.environ.get("BOARD_HUMAN", "human")
BASE_URL  = os.environ.get("BOARD_BASE_URL", "http://localhost:8080")
PORT      = int(os.environ.get("BOARD_PORT", "8080"))
# Simplification: configurable so conformance.sh can set it low and poll instead of
# sleeping 60s against a fresh instance — the reap logic itself is unchanged.
REAP_INTERVAL = int(os.environ.get("BOARD_REAP_INTERVAL", "60"))

NTFY_LOCK = threading.Lock()
ntfy_failures_since_success = 0

STALE_MIN, DEAD_MIN = 5, 60
# The lease is also the threshold for `stalled`. It is renewed by claim and
# task.progress, never by heartbeat (Q-107): a process that has sat for more than one
# 5-hour window without progress is not out of quota — something else is wrong, and
# another agent must be able to take over. Clamped: 0 would orphan everything at once
# on the next tick.
LEASE_MIN = max(1, min(1440, int(os.environ.get("BOARD_LEASE_MIN", "300"))))
PHASES = ("idea", "build", "launch", "live")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, project TEXT NOT NULL, stream TEXT NOT NULL,
  type TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_stream ON events(project, stream, id);
CREATE TABLE IF NOT EXISTS projects (
  name TEXT PRIMARY KEY, phase TEXT NOT NULL, goal TEXT, manifest TEXT,
  manifest_host TEXT, manifest_path TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, harness TEXT, host TEXT, model TEXT, session TEXT,
  current_project TEXT, capabilities TEXT, preference TEXT,
  last_seen TEXT, status TEXT, registered TEXT,
  ctx_pct REAL, budget TEXT, current_task TEXT);
CREATE TABLE IF NOT EXISTS grants (
  agent TEXT, project TEXT, grant_name TEXT, source TEXT DEFAULT 'policy', PRIMARY KEY (agent, project, grant_name));
-- tasks.human_test and questions.kind='test'/card are legacy from the removed test stage
-- (T-352). Kept so old databases load; nothing reads or writes them any more.
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, project TEXT, repo TEXT, title TEXT, spec TEXT, status TEXT,
  requires TEXT, needs_grants TEXT, touches TEXT, risk TEXT, owner TEXT, lease_until TEXT,
  worktree TEXT, branch TEXT, pr TEXT, merge_sha TEXT, review_open INTEGER, review_fixed INTEGER,
  human_test TEXT, created TEXT, updated TEXT, priority INTEGER);
CREATE TABLE IF NOT EXISTS questions (
  id TEXT PRIMARY KEY, project TEXT, task TEXT, asked_by TEXT, kind TEXT NOT NULL, card TEXT,
  text TEXT, options TEXT, default_answer TEXT, deadline TEXT, status TEXT,
  answer TEXT, answered_by TEXT, answered TEXT, read INTEGER DEFAULT 0, created TEXT);
CREATE TABLE IF NOT EXISTS roles (
  project TEXT, role TEXT, agent TEXT, source TEXT, pinned_by TEXT, since TEXT, lease_until TEXT,
  PRIMARY KEY (project, role, agent));
CREATE UNIQUE INDEX IF NOT EXISTS roles_singleton ON roles(project, role) WHERE role = 'coordinator';
CREATE TABLE IF NOT EXISTS routines (
  project TEXT, name TEXT, title TEXT, spec TEXT, interval TEXT, deadline TEXT,
  repo TEXT, priority INTEGER, role TEXT, last_run TEXT, next_due TEXT, status TEXT,
  PRIMARY KEY (project, name));
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, project TEXT, to_agent TEXT, from_agent TEXT,
  text TEXT, task TEXT, read INTEGER DEFAULT 0, ts TEXT);
"""

# Simplification: one global lock around every handler. SQLite serializes writes
# anyway and the load is 5 agents; per-table locking if it ever becomes a bottleneck.
LOCK = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
# SQLite's own LOWER() is ASCII-only — LOWER('ÅPEN') yields 'Åpen'. A non-ASCII
# default answer is then read as confirmation one way and as an override the other.
# One normalization, Python's, on both sides.
db.create_function("ulower", 1, lambda v: v.lower() if isinstance(v, str) else v)
db.executescript(SCHEMA)
for _tbl, _col, _decl in (("projects", "paused", "TEXT"),
                          ("agents", "budget", "TEXT"),
                          ("tasks", "routine", "TEXT"),
                          ("grants", "source", "TEXT DEFAULT 'policy'")):

    try:                                # database from before the column existed
        db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (_tbl, _col, _decl))
    except sqlite3.OperationalError:
        pass
_cols = {r[1] for r in db.execute("PRAGMA table_info(agents)")}
if {"rl5_pct", "rl7_pct"} <= _cols:      # T-164: existing database, old columns still there
    for _r in db.execute("SELECT id,rl5_pct,rl5_reset,rl7_pct FROM agents WHERE budget IS NULL "
                         "AND (rl5_pct IS NOT NULL OR rl7_pct IS NOT NULL)").fetchall():
        _ws = ([{"window": "5h", "used_pct": _r["rl5_pct"], "resets_at": _r["rl5_reset"]}]
               if _r["rl5_pct"] is not None else [])
        if _r["rl7_pct"] is not None:
            _ws.append({"window": "7d", "used_pct": _r["rl7_pct"]})
        db.execute("UPDATE agents SET budget=? WHERE id=?", (json.dumps(_ws), _r["id"]))
# T-352: the human test stage is gone, and nothing moves a task out of `awaiting_human`
# any more. These rows are not fresh work: a PR means the task is `in_review`, a
# worktree/branch with no PR means it was `orphaned` (matches how the reaper and
# task_claim already treat existing work — see agents_cleanup and task_next). Only a
# row with neither goes back to `open`. Idempotent: a second boot finds no rows.
_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
for _r in db.execute("SELECT id, project, pr, branch, worktree FROM tasks "
                     "WHERE status='awaiting_human'").fetchall():
    if _r["pr"]:
        _status = "in_review"
    elif _r["branch"] or _r["worktree"]:
        _status = "orphaned"
    else:
        _status = "open"
    db.execute("UPDATE tasks SET status=?, owner=NULL, lease_until=NULL, updated=%s "
               "WHERE id=?" % _NOW, (_status, _r["id"]))
    db.execute("UPDATE agents SET current_task=NULL WHERE current_task=?", (_r["id"],))
    db.execute("INSERT INTO events (ts,project,stream,type,actor,body) VALUES (%s,?,?,?,?,?)" % _NOW,
               (_r["project"] or "_global", "task/" + _r["id"],
                "task.orphaned" if _status == "orphaned" else "task.released", "board",
                json.dumps({"note": "human test stage removed (T-352)", "status": _status})))
db.execute("UPDATE questions SET status='answered', answered_by='board', read=1, answered=%s, "
           "answer='withdrawn: human test stage removed (T-352)' "
           "WHERE kind='test' AND status='open'" % _NOW)
db.commit()


class Err(Exception):
    def __init__(self, code, msg, **extra):
        self.code, self.body = code, dict(error=msg, **extra)


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ts(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))



def parse_interval(s):
    if not s: return None
    s = s.strip().lower()
    try:
        if s.endswith("w"): return timedelta(days=float(s[:-1])*7)
        elif s.endswith("d"): return timedelta(days=float(s[:-1]))
        elif s.endswith("h"): return timedelta(hours=float(s[:-1]))
        elif s.endswith("m"): return timedelta(minutes=float(s[:-1]))
    except (ValueError, TypeError):
        pass
    return None


def mins_since(s):
    return 1e9 if not s else (datetime.now(timezone.utc) - ts(s)).total_seconds() / 60


def plus(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


def num(v):
    """v as float, or None. One place to answer "is this a reading at all"."""
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def jl(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else [])
    except (TypeError, ValueError):
        return default if default is not None else []


def pr_num(pr):
    """The PR number alone, from a URL, 'web#201' or a bare number. Never a full URL."""
    if not pr:
        return None
    s = str(pr).rstrip("/")
    if s.startswith("http") or "/" in s:
        s = s.rsplit("/", 1)[-1]
    elif "#" in s:
        s = s.rsplit("#", 1)[-1]
    return s or None


def pr_html(pr):
    n = pr_num(pr)
    if not n:
        return "—"
    label = "#" + n
    if str(pr).startswith("http"):
        return "<a href='%s'>%s</a>" % (escape(pr), escape(label))
    return escape(label)


def ev(project_, stream, type_, actor, **body):
    db.execute("INSERT INTO events (ts,project,stream,type,actor,body) VALUES (?,?,?,?,?,?)",
               (now(), project_ or "_global", stream, type_, actor, json.dumps(body)))


def policy():
    try:
        with open(POLICY) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def budget(project):
    """How much of the quota the agents may spend. The human's decision, like `phase`:
    without `budget` in the policy every ceiling is 0 — no default that is more
    permissive than stopping.

    `ceilings` is per WINDOW NAME, not per Anthropic window: {"5h": 85, "7d": 60}. A
    harness with different windows (or none) is configured without a code change.
    `fallback` applies to those that do not report quota at all (T-164).

    But "the ceiling is set to 0" and "the line is missing" must be told apart: 0 stops
    every single agent in every single project, and if that happens because the line was
    never added to board-policy.json, it must say so in plain words — not look like an
    ordinary quota stop.

    The live board-policy.json is hand-edited on the host and is NEVER deployed (see
    k8s/board.yaml). That is why the old `rl7_ceiling` shape is translated here, at the
    edge: otherwise this change would have made the ceiling unknown to the whole fleet
    the moment it rolled out.
    """
    p = policy().get("budget", {})
    p = p.get(project, p.get("*", {}))
    c = p.get("ceilings")
    if c is None and p.get("rl7_ceiling") is not None:
        c = {"7d": p["rl7_ceiling"], "5h": p.get("rl5_ceiling", 85)}
    if c is None:
        return {"ceilings": {}, "fallback": p.get("fallback", {}), "ceilings_missing": True,
                "note": "budget.%s.ceilings is not set for %s — %s must add it to "
                        "board-policy.json (budget.%s.ceilings, e.g. "
                        '{"5h": 85, "7d": 60})' % (project, project, HUMAN, project)}
    return {"ceilings": c, "fallback": p.get("fallback", {})}


WIN_UNIT = {"m": 1, "h": 60, "d": 1440, "w": 10080}


def win_name(name):
    return str(name).strip().lower()


def window_minutes(name):
    """"5h" → 300. The window name IS the length of the window, and the length is the
    filter the reading is remembered within (T-192). An unknown shape is remembered for
    seven days: too long is safe, too short is a silent quota blackout."""
    m = re.fullmatch(r"(\d+)\s*([mhdw])", win_name(name))
    return int(m.group(1)) * WIN_UNIT[m.group(2)] if m else 7 * 24 * 60


def windows_of(a):
    """The agent's declared windows as {name: used_percent}. Empty = "do not know", and
    that is a valid, honest value. Accepts both a database row (JSON text) and a parsed
    dict."""
    raw = a.get("budget") if isinstance(a, dict) else a["budget"]
    out = {}
    for w in (raw if isinstance(raw, list) else jl(raw)):
        pct = num(w.get("used_pct")) if isinstance(w, dict) else None
        if pct is not None and w.get("window") is not None:
            # the name is the key the max is taken per, and "5H" and "5h" are one window
            out[win_name(w["window"])] = max(out.get(win_name(w["window"]), 0.0), pct)
    return out


def quota_max(harness="claude-code"):
    """Quota is per account, not per session: two `/next` sessions on different machines
    or in different projects share the same windows. The stop rules must therefore read
    the HIGHEST number per WINDOW NAME across all agents in all projects (DESIGN.md §11).

    And the reading must be REMEMBERED after the agent is gone. A "currently alive"
    filter produces a loop in exactly the case the mechanism exists for: every agent hits
    the ceiling, every agent calls `board finished`, the MAX goes empty — and the next
    start believes the quota is free and burns it again. The window's own length is the
    filter, and status does not count: a finished agent's last reading is still true
    until the window has rolled (T-192).

    But "per account" means per HARNESS account. A grok or codex agent does not share
    Anthropic's windows with the Claude agents, and taking the MAX across them let a
    foreign (or a test registration's) reading stop the entire Claude fleet for up to
    seven days, with no self-correction. Window names can collide too: "7d" does not mean
    the same thing at two vendors."""
    out = {}
    for a in db.execute("SELECT budget, last_seen, harness FROM agents "
                        "WHERE budget IS NOT NULL AND harness=?", (harness,)):
        for name, pct in windows_of(a).items():
            if mins_since(a["last_seen"]) <= window_minutes(name):
                out[name] = max(out.get(name, 0.0), pct)
    return out


def agent_stop(a, project):
    """Should THIS agent stop? A reason, or None. Works for 0, 1 or N windows.

    Only the windows the agent itself declares count. The value measured is the highest
    reading of the same window name across all agents — quota is per account — but a
    window name the agent does not have is not the agent's problem. Otherwise one unknown
    window name from one agent in one project (the conformance suite sends one itself)
    would stop every single agent in all OTHER projects, which lack that name in their
    ceilings (T-164).

    If the agent reports nothing, the answer is still not "run freely": then the fallback
    ceiling applies, counted in something the agent actually can count. Coarse, but
    honest."""
    # Same harness as the agent itself: quota is per vendor account, not per machine.
    b, acct = budget(project), quota_max(a["harness"] or "claude-code")
    ceilings = {win_name(k): v for k, v in b["ceilings"].items()}
    mine = windows_of(a)
    for name in sorted(mine):
        pct = max(mine[name], acct.get(name, 0.0))
        ceil = ceilings.get(name)
        if ceil is None:
            return ("window %s has no ceiling in the policy — set budget.%s.ceilings.%s"
                    % (name, project, name))
        if pct >= float(ceil):
            return "%s: %g%% used of the %s%% ceiling (per account, all sessions)" % (name, pct, ceil)
    if mine:
        return None
    # `reports-quota` is self-declared (§3.7) and is set by `board probe` only when the
    # statusline hook actually exists. If the agent says it DOES report, but the reading
    # has not landed yet (pod just restarted, or the agent is inside a long subagent with
    # no statusline render), the answer is "do not know yet" — not "cannot count".
    # Stopping there would have ended the night run with a reason that is untrue.
    if "reports-quota" in jl(a["capabilities"]):
        return None
    mx = b["fallback"].get("max_tasks")
    if mx is None:
        return ("the agent reports no quota and the project has no fallback ceiling — "
                "set budget.%s.fallback.max_tasks" % project)
    n = db.execute("SELECT COUNT(*) c FROM events WHERE type='task.claimed' AND actor=? "
                   "AND ts>=?", (a["id"], a["registered"] or "")).fetchone()["c"]
    return ("fallback: %d of %s tasks used in this session (the agent reports no "
            "quota)" % (n, mx)) if n >= int(mx) else None


def grants_for(harness, host, project):
    p = policy().get("grants", {}).get(project, {})
    out = set()
    for key in ("*", "%s@*" % harness, "%s@%s" % (harness, host)):
        out |= set(p.get(key, []))
    return sorted(out)


def next_id(prefix, table):
    row = db.execute("SELECT COALESCE(MAX(CAST(SUBSTR(id,%d) AS INTEGER)),0)+1 n FROM %s"
                     % (len(prefix) + 1, table)).fetchone()
    return "%s%d" % (prefix, row["n"])


def ntfy(title, message, click=""):
    """Push to the human. Called under the lock, so the network call itself has to go
    into a thread — otherwise a slow ntfy holds the whole board for 3 s per question.

    http.client encodes headers as latin-1, and every single title here starts with an
    emoji. The Title header therefore raised UnicodeEncodeError inside the thread, and
    the push never left the process — for a full day, invisibly, because the call
    swallowed everything. The header is trimmed to ascii; the full title with the emoji
    goes in the body, which is UTF-8 and works against any HTTP receiver. Still never
    fails the board, but says so on stderr.

    Retries transient errors (DNS/URLError, timeout, 5xx HTTPError) up to 3 attempts with
    short backoff (0.1s, 0.2s). Non-transient 4xx errors are not retried. Tracks failures
    since last success in ntfy_failures_since_success."""
    if not NTFY_URL:
        return None

    def send():
        global ntfy_failures_since_success
        head = title.encode("ascii", "ignore").decode().strip() or "board"
        body = message if head == title else "%s\n%s" % (title, message)
        backoffs = [0.1, 0.2]
        max_attempts = 3
        last_exc = None
        for attempt in range(max_attempts):
            req = urllib.request.Request(NTFY_URL, data=body.encode(),
                                         headers={"Title": head, "Click": click} if click
                                         else {"Title": head})
            if NTFY_AUTH:
                req.add_header("Authorization", NTFY_AUTH)
            try:
                urllib.request.urlopen(req, timeout=3).close()
                with NTFY_LOCK:
                    ntfy_failures_since_success = 0
                return
            except urllib.error.HTTPError as e:
                last_exc = e
                # Do NOT retry on 4xx client errors (400 <= code < 500)
                if 400 <= e.code < 500:
                    break
                if attempt < len(backoffs):
                    time.sleep(backoffs[attempt])
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_exc = e
                if attempt < len(backoffs):
                    time.sleep(backoffs[attempt])
            except Exception as e:
                last_exc = e
                break

        with NTFY_LOCK:
            ntfy_failures_since_success += 1
        print("ntfy failed (%s: %s) — title %r" % (type(last_exc).__name__, last_exc, title),
              file=sys.stderr, flush=True)

    t = threading.Thread(target=send, daemon=True)
    t.start()
    return t


# ---------- agents --------------------------------------------------------

def agent(aid, alive_only=False):
    a = db.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone()
    if not a:
        raise Err(404, "unknown agent %s" % aid)
    if alive_only and status_of(a) != "alive":
        raise Err(409, "agent %s is %s" % (aid, status_of(a)))
    return a


def status_of(a):
    """`stalled` = the process is alive, but the lease on the task has expired. The
    heartbeat does not renew the lease (Q-107), so this catches the one that has sat
    without progress for longer than LEASE_MIN (default 5 h). Visible in status, never
    `alive` for task.next or the role ranking. The reaper orphans the task on the next
    tick."""
    if a["status"] == "finished":
        return "finished"
    m = mins_since(a["last_seen"])
    if m >= STALE_MIN:
        return "stale" if m < DEAD_MIN else "dead"
    if a["current_task"]:
        t = db.execute("SELECT lease_until FROM tasks WHERE id=? AND owner=?",
                       (a["current_task"], a["id"])).fetchone()
        if t and t["lease_until"] and mins_since(t["lease_until"]) > 0:
            return "stalled"
    return "alive"


def caps(a):
    return set(jl(a["capabilities"]))


def agent_grants(aid, project):
    return {r["grant_name"] for r in db.execute(
        "SELECT grant_name FROM grants WHERE agent=? AND project=?", (aid, project))}


def agent_grants_get(aid, q):
    a = agent(aid)
    project = q.get("project", [None])[0] or a["current_project"]
    return {"id": aid, "project": project, "grants": sorted(agent_grants(aid, project))}


def agent_grant_add(aid, b):
    b = b or {}
    human_only(b, "granting permissions")
    a = agent(aid)
    project = b.get("project") or a["current_project"]
    ensure_project(project)
    raw = b.get("grant") or b.get("grants")
    if not raw:
        raise Err(400, "grant is required")
    if isinstance(raw, str):
        to_add = [x.strip() for x in raw.split(",") if x.strip()]
    elif isinstance(raw, list):
        to_add = [str(x).strip() for x in raw if str(x).strip()]
    else:
        to_add = [str(raw).strip()]
    if not to_add:
        raise Err(400, "grant is required")
    for g in to_add:
        db.execute(
            "INSERT INTO grants (agent, project, grant_name, source) VALUES (?, ?, ?, 'human') "
            "ON CONFLICT(agent, project, grant_name) DO UPDATE SET source='human'",
            (aid, project, g)
        )
        ev(project, "agent/" + aid, "agent.granted", HUMAN, grant=g, project=project)
    return {"id": aid, "project": project, "grants": sorted(agent_grants(aid, project))}


def agent_grant_del(aid, grant, b, q):
    human_only(b or {}, "revoking permissions")
    if not grant:
        raise Err(400, "grant is required")
    a = agent(aid)
    project = (b or {}).get("project") or q.get("project", [None])[0] or a["current_project"]
    ensure_project(project)
    db.execute("DELETE FROM grants WHERE agent=? AND project=? AND grant_name=?", (aid, project, grant))
    ev(project, "agent/" + aid, "agent.revoked", HUMAN, grant=grant, project=project)
    return {"id": aid, "project": project, "grants": sorted(agent_grants(aid, project))}


def same_project(a, project):
    """§3.2: an agent cannot touch another project's tasks by accident."""
    if project and a["current_project"] and project != a["current_project"]:
        raise Err(403, "the agent is in %s, not %s" % (a["current_project"], project))


def register(b):
    harness, host = b.get("harness", "unknown"), b.get("host", "unknown")
    project, session = b.get("project"), b.get("session")
    if not project:
        raise Err(400, "project is missing")
    old = db.execute("SELECT * FROM agents WHERE session=?", (session,)).fetchone() if session else None
    aid = old["id"] if old else "%s-%s-%s" % (
        {"claude-code": "cc", "codex": "cx", "grok": "gk", "antigravity": "ag"}.get(harness, harness[:2]),
        host, secrets.token_hex(2))
    db.execute("""INSERT INTO agents (id,harness,host,model,session,current_project,capabilities,
                    preference,last_seen,status,registered) VALUES (?,?,?,?,?,?,?,?,?,'alive',?)
                  ON CONFLICT(id) DO UPDATE SET harness=excluded.harness, host=excluded.host,
                    model=COALESCE(excluded.model, agents.model),
                    current_project=excluded.current_project,
                    capabilities=CASE WHEN excluded.capabilities='[]' THEN agents.capabilities
                                 ELSE excluded.capabilities END,
                    last_seen=excluded.last_seen, status='alive'""",
               (aid, harness, host, b.get("model"), session, project,
                json.dumps(b.get("capabilities", [])), b.get("preference"), now(), now()))
    ensure_project(project)
    db.execute("DELETE FROM grants WHERE agent=? AND project=? AND (source='policy' OR source IS NULL)", (aid, project))
    g = grants_for(harness, host, project)
    db.executemany("INSERT OR IGNORE INTO grants (agent, project, grant_name, source) VALUES (?,?,?,'policy')", [(aid, project, x) for x in g])
    all_g = sorted(agent_grants(aid, project))
    ev(project, "agent/" + aid, "agent.registered", aid, harness=harness, host=host,
       model=b.get("model"), capabilities=b.get("capabilities", []), grants=all_g)
    return {"id": aid, "grants": all_g, "project": project}


def ensure_project(name, phase=None, goal=None, manifest=None, host=None, path=None):
    if phase and phase not in PHASES:
        raise Err(400, "unknown phase %r (%s)" % (phase, "|".join(PHASES)))
    row = db.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
    if not row:
        db.execute("INSERT INTO projects (name,phase,updated) VALUES (?,?,?)",
                   (name, phase or "build", now()))
        ev(name, "project", "project.phase_set", "board", phase=phase or "build")
        row = db.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
    if phase and phase != row["phase"]:
        db.execute("UPDATE projects SET phase=?, updated=? WHERE name=?", (phase, now(), name))
    if manifest:
        m_dict = json.loads(manifest) if isinstance(manifest, str) else manifest
        routines = m_dict.get("routines", {})
        existing = db.execute("SELECT name FROM routines WHERE project=?", (name,)).fetchall()
        for row in existing:
            if row["name"] not in routines:
                db.execute("DELETE FROM routines WHERE project=? AND name=?", (name, row["name"]))
        for r_id, r_def in routines.items():
            title = r_def.get("title", "")
            spec = r_def.get("spec", r_def.get("description", ""))
            interval = r_def.get("interval", "")
            deadline = r_def.get("deadline", "")
            repo = r_def.get("repo", "")
            priority = int(r_def.get("priority", 50))
            role = r_def.get("role", "")
            
            exists = db.execute("SELECT name, last_run, next_due FROM routines WHERE project=? AND name=?", (name, r_id)).fetchone()
            if exists:
                db.execute("UPDATE routines SET title=?, spec=?, interval=?, deadline=?, repo=?, priority=?, role=? WHERE project=? AND name=?",
                           (title, spec, interval, deadline, repo, priority, role, name, r_id))
            else:
                db.execute("INSERT INTO routines (project, name, title, spec, interval, deadline, repo, priority, role, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (name, r_id, title, spec, interval, deadline, repo, priority, role, "open"))
                   

        ev(name, "project", "project.phase_set", HUMAN, phase=phase)
    if goal or manifest:
        db.execute("UPDATE projects SET goal=COALESCE(?,goal), manifest=COALESCE(?,manifest),"
                   " manifest_host=COALESCE(?,manifest_host), manifest_path=COALESCE(?,manifest_path),"
                   " updated=? WHERE name=?",
                   (goal, json.dumps(manifest) if manifest else None, host, path, now(), name))
                   

    return db.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()


def check_repo(project, repo):
    """A `--repo` that does not exist in the manifest is a silent error at creation time
    that only blows up several steps later, in a different agent: `board task worktree`
    and `cleanup` compute `$ROOT/$repo`, and a task with repo="next" in a project with
    `repos: ['.']` produced `<root>/next/next` — no such directory (T-201).

    Valid is what the manifest says: "", ".", the project name (the board stores project
    names, not manifest repos — same rule as worktree_path/repo_dir), or a name in
    `repos`. If the project has not mirrored any manifest there is nothing to validate
    against, and then we let it through: refusing would lock out a project before its
    first `board register`. That hole is real — the SKILL's free-text mode runs
    `task create` BEFORE `register` — but it only restores today's behaviour (a clear
    `die` in `board task worktree`), and the alternative is to refuse the very first
    task in a new project."""
    if repo in (None, "", ".", project):
        return
    row = db.execute("SELECT manifest FROM projects WHERE name=?", (project,)).fetchone()
    repos = (jl(row["manifest"], {}) if row and row["manifest"] else {}).get("repos")
    if not isinstance(repos, list) or not repos:
        return
    if repo not in repos:
        raise Err(400, "unknown repo %r in %s — the manifest knows: %s" % (
            repo, project, ", ".join(str(r) for r in repos)))


def phase_of(project):
    row = db.execute("SELECT phase FROM projects WHERE name=?", (project,)).fetchone()
    return row["phase"] if row else "build"


def budget_in(b):
    """The quota report from a heartbeat, normalized to a window list. No report = None
    (keep the previous one); empty list = "I do not know", a valid and honest value.

    rl5_*/rl7_* is the old, Claude-shaped report. It is translated here, at the edge, so
    that a statusline hook that has not been updated yet does not cause a silent quota
    blackout."""
    if b.get("budget") is not None and not isinstance(b["budget"], list):
        raise Err(400, "budget must be a list of windows, not %s" % type(b["budget"]).__name__)
    if isinstance(b.get("budget"), list):
        ws = [w for w in b["budget"] if isinstance(w, dict) and num(w.get("used_pct")) is not None]
        # A list that SHRINKS to empty is not the same as an empty list: the statusline
        # hook sends {window:"5h", used_pct:null} every time `rate_limits` is missing from
        # the payload (API key, Bedrock, or a miss). If we wrote that, an agent that just
        # reported 91% would look like a harness without quota introspection — and be
        # stopped for the wrong reason, or let loose. No reading = keep the previous one.
        return json.dumps(ws) if ws or not b["budget"] else None
    ws = []
    if b.get("rl5_pct") is not None:
        ws.append({"window": "5h", "used_pct": b["rl5_pct"], "resets_at": b.get("rl5_reset")})
    if b.get("rl7_pct") is not None:
        ws.append({"window": "7d", "used_pct": b["rl7_pct"]})
    return json.dumps(ws) if ws else None


def heartbeat(aid, b):
    a = agent(aid)
    db.execute("""UPDATE agents SET last_seen=?, status=CASE WHEN status='finished' THEN 'finished'
                  ELSE 'alive' END, ctx_pct=COALESCE(?,ctx_pct), budget=COALESCE(?,budget),
                  model=COALESCE(?,model) WHERE id=?""",
               (now(), b.get("ctx_pct"), budget_in(b), b.get("model"), aid))
    # The heartbeat proves the process is alive, not that the work is progressing (Q-107).
    bi = budget_in(b)
    ev(a["current_project"], "agent/" + aid, "agent.heartbeat", aid,
       **({"ctx_pct": b["ctx_pct"]} if b.get("ctx_pct") is not None else {}),
       **({"budget": jl(bi)} if bi is not None else {}))
    return {"ok": True, "task": a["current_task"]}


def finished(aid, b):
    a = agent(aid)
    for t in db.execute("SELECT id FROM tasks WHERE owner=? AND status IN ('claimed','in_review')", (aid,)):
        release(t["id"], aid, {"note": "agent finished: " + (b.get("reason") or "")})
    db.execute("UPDATE agents SET status='finished', current_task=NULL WHERE id=?", (aid,))
    db.execute("DELETE FROM roles WHERE agent=?", (aid,))
    ev(a["current_project"], "agent/" + aid, "agent.finished", aid, reason=b.get("reason"))
    # An agent that stands down must not do it silently: without a push the human only
    # sees the fleet stop working. If the quota ceiling is missing from the policy, the
    # reason goes in the same notification.
    note = budget(a["current_project"]).get("note")
    ntfy("agent finished: %s" % aid, "%s — %s%s" % (
        a["current_project"] or "?", b.get("reason") or "no reason given",
        "\n" + note if note else ""))
    return {"ok": True}


def agents_cleanup(project=None, older_than="24h", b=None):
    if (b or {}).get("all") or project == "all":
        project = None
    min_mins = 0
    if older_than and str(older_than).lower() not in ("0", "all", "none", "active"):
        td = parse_interval(str(older_than))
        if td:
            min_mins = td.total_seconds() / 60
        else:
            try:
                min_mins = float(older_than)
            except (ValueError, TypeError):
                raise Err(400, "invalid interval: %s" % older_than)

    q = "SELECT * FROM agents WHERE status<>'finished'"
    params = []
    if project:
        q += " AND current_project=?"
        params.append(project)

    cleaned = []
    for a in db.execute(q, params).fetchall():
        st = status_of(a)
        if st == "dead":
            m = mins_since(a["last_seen"])
            if m >= min_mins:
                for t in db.execute("SELECT id, status FROM tasks WHERE owner=?", (a["id"],)):
                    keep = t["status"] if t["status"] == "in_review" else "orphaned"
                    db.execute("UPDATE tasks SET status=?, owner=NULL, lease_until=NULL, updated=? WHERE id=?",
                               (keep, now(), t["id"]))
                    db.execute("UPDATE agents SET current_task=NULL WHERE current_task=?", (t["id"],))
                    ev(a["current_project"], "task/" + t["id"], "task." + ("released" if keep != "orphaned" else "orphaned"),
                       "board", reason="agent cleaned up: dead for %d min" % int(m))
                db.execute("UPDATE agents SET status='finished', current_task=NULL WHERE id=?", (a["id"],))
                db.execute("DELETE FROM roles WHERE agent=?", (a["id"],))
                ev(a["current_project"], "agent/" + a["id"], "agent.finished", "board",
                   reason="cleaned up: dead for %d min" % int(m))
                cleaned.append(a["id"])
    return {"ok": True, "cleaned": len(cleaned), "agents": cleaned}


def tasks_cleanup(project=None, older_than="0", b=None):
    if (b or {}).get("all") or project == "all":
        project = None
    min_mins = 0
    if older_than and str(older_than).lower() not in ("0", "all", "none", "active"):
        td = parse_interval(str(older_than))
        if td:
            min_mins = td.total_seconds() / 60
        else:
            try:
                min_mins = float(older_than)
            except (ValueError, TypeError):
                raise Err(400, "invalid interval: %s" % older_than)

    q = "SELECT * FROM tasks WHERE status='done'"
    params = []
    if project:
        q += " AND project=?"
        params.append(project)

    archived = []
    for t in db.execute(q, params).fetchall():
        m = mins_since(t["updated"] or t["created"])
        if m >= min_mins:
            db.execute("UPDATE tasks SET status='archived', owner=NULL, lease_until=NULL, updated=? WHERE id=?",
                       (now(), t["id"]))
            db.execute("UPDATE agents SET current_task=NULL WHERE current_task=?", (t["id"],))
            ev(t["project"], "task/" + t["id"], "task.archived", "board", note="cleaned up: completed task")
            archived.append(t["id"])
    return {"ok": True, "archived": len(archived), "tasks": archived}


# ---------- tasks ---------------------------------------------------------

def task(tid):
    t = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        raise Err(404, "unknown task %s" % tid)
    return t


def task_create(b, actor):
    project = b.get("project")
    if not project or not b.get("title"):
        raise Err(400, "project and title are mandatory")
    ensure_project(project)
    # Only on what comes from outside. The board itself creates follow-up tasks that
    # INHERIT the repo from a task already on the board (an overridden default on a done
    # task). If the manifest's `repos` shrinks afterwards, the human's answer must
    # not blow up with a 400 and roll back the whole write — the repo is not their input.
    if actor != "board":
        check_repo(project, b.get("repo"))
    tid = next_id("T-", "tasks")
    db.execute("""INSERT INTO tasks (id,project,repo,title,spec,status,requires,needs_grants,touches,
                  risk,review_open,created,updated,priority,routine) VALUES (?,?,?,?,?,'open',?,?,?,?,NULL,?,?,?,?)""",
               (tid, project, b.get("repo"), b["title"], b.get("spec"),
                json.dumps(b.get("requires", [])), json.dumps(b.get("needs_grants", [])),
                json.dumps(b.get("touches", [])), b.get("risk", "normal"), now(), now(),
                int(b.get("priority", 50)), b.get("routine")))
    ev(project, "task/" + tid, "task.created", actor, title=b["title"], repo=b.get("repo"),
       risk=b.get("risk", "normal"), from_project=b.get("from_project"))
    return {"id": tid, "status": "open"}


def task_show(tid):
    """Everything another agent needs to take over, including the last progress note —
    reference/takeover.md asks for it, and before this it only lived in the event log."""
    t = task(tid)
    d = dict(t)
    notes = [dict(r, body=jl(r["body"], {})) for r in db.execute(
        "SELECT * FROM events WHERE stream=? "
        "AND type IN ('task.progress','task.blocked','task.comment') "
        "ORDER BY id DESC LIMIT 3", ("task/" + tid,))]
    # `text` for comments, `note` for progress — both are "what happened", and a taker-
    # over must see what the HUMAN said, not only what the agent did.
    d["progress"] = [{"ts": n["ts"], "actor": n["actor"],
                      "note": n["body"].get("note") or n["body"].get("text")}
                     for n in notes if n["body"].get("note") or n["body"].get("text")]
    # Who dispatched what, all the way back — this is what makes a takeover and a cost
    # possible to unravel after the fact.
    # SKILL.md asks the coordinator to log the same dispatch TWICE: once before the
    # subagent starts, and once when it returns with --tokens. One event = one dispatch
    # therefore counted every dispatch twice, and `complete` could never become true — the
    # "before" row stands forever without a token count. The closing entry is folded into
    # the open row for the same role:model instead. A NEW round with the same role:model
    # begins with a new "before" row, and therefore counts as its own dispatch — that is
    # where `ts` tells them apart.
    d["dispatches"] = []
    for r in db.execute("SELECT * FROM events WHERE stream=? AND type='task.progress' "
                        "ORDER BY id", ("task/" + tid,)):
        body = jl(r["body"], {})
        x = body.get("dispatch")
        if not x:
            continue
        res = body.get("result")
        open_row = next((p for p in reversed(d["dispatches"])
                         if p["role"] == x["role"] and p["model"] == x["model"]
                         and p["tokens"] is None), None)
        if open_row is not None and (x.get("tokens") is not None or res is not None):
            open_row["tokens"] = x.get("tokens")
            open_row["result"] = res if res is not None else open_row["result"]
            continue
        d["dispatches"].append({"ts": r["ts"], "actor": r["actor"], "role": x["role"],
                                "model": x["model"], "tokens": x.get("tokens"),
                                "result": res})
    # What the task cost in subagents. Tokens is the number every harness has, unlike
    # the vendor's percentage windows (T-164). `complete` is false as soon as one
    # dispatch is missing the number — a sum that pretends to be whole is worse than one
    # that says it is not.
    toks = [x.get("tokens") for x in d["dispatches"]]
    by_model = {}
    for x in d["dispatches"]:
        if x.get("tokens"):
            by_model[x["model"]] = by_model.get(x["model"], 0) + x["tokens"]
    d["cost"] = {"dispatches": len(toks), "tokens": sum(t for t in toks if t),
                 "by_model": by_model, "complete": all(t is not None for t in toks)}
    d["phase"] = phase_of(d["project"])
    d["events"] = [
        {"ts": r["ts"], "type": r["type"], "actor": r["actor"],
         "note": jl(r["body"], {}).get("note")}
        for r in db.execute(
            # LIMIT: the whole request holds the global lock, so a task with
            # thousands of events would stall every other agent's call while the
            # page renders. The last 200 are what anyone actually reads.
            "SELECT ts, type, actor, body FROM events WHERE stream=? AND type<>'agent.heartbeat' "
            "ORDER BY id DESC LIMIT 200", ("task/" + tid,))][::-1]
    ost, oa = owner_view(t)
    d["owner_status"] = ost
    d["owner_agent"] = oa
    return d


def owner_view(t):
    """The owner's signs of life — heartbeat, ctx, budget. None when the task is
    unowned."""
    if not t["owner"]:
        return None, None
    a = db.execute("SELECT * FROM agents WHERE id=?", (t["owner"],)).fetchone()
    if not a:
        return "unknown", None
    st = status_of(a)
    return st, {"last_seen": a["last_seen"], "ctx_pct": a["ctx_pct"],
                "budget": windows_of(a), "model": a["model"], "status": st}


def brief(t):
    """Lists drop `spec`. 20 tasks at 1k tokens of spec would blow the coordinator's
    context budget every time it ran `board status` (DESIGN.md §6.5)."""
    d = dict(t)
    if d.get("spec"):
        d["spec"] = "(%d chars — board task show %s)" % (len(d["spec"]), d["id"])
    return d



def routine_list(q):
    project = q.get("project", [None])[0]
    st = q.get("status", [None])[0]
    
    query = "SELECT * FROM routines WHERE 1=1"
    args = []
    if project:
        query += " AND project=?"
        args.append(project)
    if st:
        query += " AND status=?"
        args.append(st)
        
    rows = [dict(r) for r in db.execute(query, args).fetchall()]
    return {"routines": rows}

def task_list(q):
    where, args = ["1=1"], []
    for field in ("project", "repo", "status", "owner"):
        if q.get(field):
            where.append("%s=?" % field)
            args.append(q[field][0])
    if q.get("exclude_status"):
        where.append("status != ?")
        args.append(q["exclude_status"][0])
    elif not q.get("status") and not (q.get("all") and q["all"][0] in ("1", "true")):
        where.append("status != 'archived'")
    if q.get("q"):
        term = "%" + q["q"][0] + "%"
        where.append("(id LIKE ? OR title LIKE ? OR spec LIKE ?)")
        args.extend([term, term, term])
    return {"tasks": [brief(t) for t in db.execute(
        "SELECT * FROM tasks WHERE %s ORDER BY priority DESC, created" % " AND ".join(where), args)]}


DEFAULT_WIP = 5


def wip_limit(project):
    """Max number of finished-but-unreviewed tasks waiting for review. The human's
    number, in the policy. The default is a LIMIT, not a free pass: without a limit the
    queue grows until production has outrun the review, which is exactly what happened
    once."""
    p = policy().get("limits", {})
    return int(p.get(project, p.get("*", {})).get("unreviewed", DEFAULT_WIP))


def unreviewed(project):
    return db.execute("SELECT COUNT(*) c FROM tasks WHERE project=? AND status='in_review' "
                      "AND review_open IS NULL", (project,)).fetchone()["c"]


def paused(project):
    r = db.execute("SELECT paused FROM projects WHERE name=?", (project,)).fetchone()
    return r["paused"] if r else None


def task_next(aid, q):
    a = agent(aid)
    if status_of(a) == "stalled":
        raise Err(409, "agent %s is stalled on %s — report task.progress first"
                  % (aid, a["current_task"]))
    project = a["current_project"]
    # Drain, not stop: whoever holds a task finishes and merges it. Nobody may start
    # anything new, so the loop lands on "queue empty" and ends itself.
    # Pause means "no NEW tasks", not "no work". Finished work must still be reviewable
    # and mergeable — otherwise pause is a full stop, and the queue never drains.
    # Same shape as the WIP limit: `open` disappears, draining continues.
    # WIP limit on UNREVIEWED work. Once reached, `open` disappears from the queue and
    # only work that DRAINS it is offered. No deadlock: review requires no grant (only
    # merge does), so any living agent can empty the queue.
    # The limit never blocks a hand-off — releasing an in_review always goes through.
    full = paused(project) or unreviewed(project) >= wip_limit(project)
    mine = caps(a)
    grants = agent_grants(aid, project)
    taken = set()
    # An unowned `in_review` is not busy — it was just handed off for review, and must
    # not lock itself out on its own `touches`.
    for r in db.execute("SELECT repo, touches FROM tasks WHERE project=? AND owner IS NOT NULL "
                        "AND status IN ('claimed','in_review','merging')", (project,)):
        taken |= {(r["repo"], x) for x in jl(r["touches"])}
    # Finished work waiting for review is worth more than new work: orphaned, then
    # in_review (the implementer's hand-off), then open.
    rows = db.execute("""SELECT * FROM tasks WHERE project=? AND owner IS NULL
                         AND status IN ('open','orphaned','in_review')
                         AND (? = 1 OR status <> 'open')
                         ORDER BY status='orphaned' DESC, status='in_review' DESC,
                                  priority DESC, created""", (project, 0 if full else 1))
    want_repo = q.get("repo", [None])[0]
    for t in rows:
        if want_repo and t["repo"] != want_repo:
            continue
        if not set(jl(t["requires"])) <= mine:
            continue
        if not set(jl(t["needs_grants"])) <= grants:
            continue
        if {(t["repo"], x) for x in jl(t["touches"])} & taken:
            continue
        return dict(t)
    if full:
        return {"wip_full": True, "unreviewed": unreviewed(project),
                "limit": wip_limit(project), "paused": paused(project),
                "note": "%d finished tasks are waiting for review, the limit is %d. "
                        "No new tasks are handed out until some are reviewed. Review "
                        "requires no grant: take an in_review, run step 8, set board task "
                        "review. If all are taken, write ENTRY and board finished."
                        % (unreviewed(project), wip_limit(project))}
    return {}


def task_claim(tid, aid):
    a = agent(aid, alive_only=True)
    t = task(tid)
    same_project(a, t["project"])
    # While paused: only NEW tasks are refused. Taking an in_review or orphaned is
    # draining work that already exists, and that is exactly what the pause must let
    # through.
    if paused(t["project"]) and t["owner"] != aid and t["status"] == "open":
        raise Err(409, "the project is paused (%s): no NEW tasks. Take an in_review or "
                       "an orphaned one and get it across the line." % paused(t["project"]),
                  paused=True)
    if not set(jl(t["requires"])) <= caps(a):
        raise Err(409, "missing capabilities", requires=jl(t["requires"]))
    if not set(jl(t["needs_grants"])) <= agent_grants(aid, t["project"]):
        raise Err(409, "missing grants", needs_grants=jl(t["needs_grants"]))
    # An `in_review` keeps its status through a claim: the new owner is to review what is
    # there, not start the task over. Worktree, branch and PR are on the board.
    cur = db.execute("UPDATE tasks SET status=CASE status WHEN 'in_review' THEN 'in_review'"
                     " ELSE 'claimed' END, owner=?, lease_until=?, updated=? "
                     "WHERE id=? AND owner IS NULL AND status IN ('open','orphaned','in_review')",
                     (aid, plus(LEASE_MIN), now(), tid)).rowcount
    if not cur:
        raise Err(409, "the task is taken", owner=t["owner"], status=t["status"])
    db.execute("UPDATE agents SET current_task=? WHERE id=?", (tid, aid))
    ev(t["project"], "task/" + tid, "task.claimed", aid, lease_until=plus(LEASE_MIN))
    return {"id": tid, "owner": aid, "lease_until": plus(LEASE_MIN),
            "worktree": t["worktree"], "branch": t["branch"], "pr": t["pr"]}


def owns(t, aid):
    """Only the owner touches task state — but only for as long as the owner is ALIVE.
    Without that last part a `blocked` task owned by a dead agent becomes impossible to
    take over forever: the reaper only orphans claimed/in_review/merging, and everyone
    else is locked out."""
    if not aid or t["owner"] == aid:
        return
    if not t["owner"]:
        # The reaper has taken the task. Without this task.progress still answers ok and
        # writes worktree/branch into the orphaned row — and the next claimant is handed
        # exactly the same worktree.
        raise Err(409, "the task is no longer yours (%s) — claim it again" % t["status"],
                  status=t["status"])
    o = db.execute("SELECT * FROM agents WHERE id=?", (t["owner"],)).fetchone()
    if o is None or status_of(o) in ("dead", "finished"):
        return
    raise Err(403, "the task is owned by %s (%s)" % (t["owner"], status_of(o)), owner=t["owner"])


DISPATCH_ROLES = ("implementer", "reviewer", "tester", "lookup")


def dispatch_of(b):
    """`--dispatch reviewer:sonnet` — role:model, validated. Free text in the note field
    was useless for what this exists for: who took over what, and what the subagents
    cost. A half-structured log is worse than none, so a malformed value is refused
    instead."""
    d = b.get("dispatch")
    tok = b.get("tokens")
    if d is None:
        # A token count without a dispatch has nothing to belong to, and would have
        # vanished silently into the event. A 400 beats a sum that is missing an entry.
        if tok is not None:
            raise Err(400, "--tokens belongs to a --dispatch")
        return None
    role, _, model = str(d).partition(":")
    if role not in DISPATCH_ROLES or not model.strip():
        raise Err(400, "dispatch must be role:model", roles=list(DISPATCH_ROLES))
    out = {"role": role, "model": model.strip()}
    if tok is not None:
        try:
            out["tokens"] = int(tok)
        except (TypeError, ValueError):
            raise Err(400, "tokens must be a whole number")
        if out["tokens"] < 0:
            raise Err(400, "tokens must be a whole number")
    return out


def task_progress(tid, aid, b):
    t = task(tid)
    # done/archived clear the owner, so owns() would 409 every late append — a
    # subagent archives, the coordinator's --tokens never lands, cost.complete
    # stays false forever (T-396). Append-only (note/dispatch/tokens/result)
    # writes the event and leaves the row alone. worktree/branch/pr/status
    # still mutate the row, so they stay refused. orphaned keeps the 409:
    # that guard exists so progress does not write a worktree into a row the
    # reaper took.
    if t["status"] in ("done", "archived"):
        same_project(agent(aid), t["project"])
        locked = [k for k in ("worktree", "branch", "pr", "status") if k in b]
        if locked:
            raise Err(400, "cannot set %s on a %s task — append-only (note, dispatch, tokens, result)"
                      % (", ".join(locked), t["status"]))
        dispatch = dispatch_of(b)
        ev(t["project"], "task/" + tid, "task.progress", aid, note=b.get("note"),
           dispatch=dispatch, result=b.get("result"))
        return {"ok": True}
    owns(t, aid)
    dispatch = dispatch_of(b)
    # `status` is set by the dedicated transitions, not by a free-text field in progress.
    st = b.get("status") if b.get("status") in ("in_review", "blocked") else None
    db.execute("""UPDATE tasks SET worktree=COALESCE(?,worktree), branch=COALESCE(?,branch),
                  pr=COALESCE(?,pr), status=COALESCE(?,status), lease_until=?, updated=? WHERE id=?""",
               (b.get("worktree"), b.get("branch"), b.get("pr"), st,
                plus(LEASE_MIN), now(), tid))
    ev(t["project"], "task/" + tid, "task.progress", aid, note=b.get("note"),
       worktree=b.get("worktree"), branch=b.get("branch"), pr=b.get("pr"),
       dispatch=dispatch, result=b.get("result"))
    return {"ok": True}


PATCHABLE = ("repo", "touches", "risk", "priority")
PATCH_LOCKED = ("status", "owner", "merge_sha")


def task_patch(tid, aid, b):
    """Correct metadata that describes the task. State (status/owner/merge_sha) is owned
    by the transitions."""
    t = task(tid)
    a = agent(aid)
    same_project(a, t["project"])
    # Ownership, like every other state change. Without this any agent in the project
    # could lower `risk` from high to low on SOMEONE ELSE'S task — and risk is exactly
    # what triggers the human gate before merge (§3.6). That is, a detour around the one
    # mechanical safeguard the design rests on.
    owns(t, aid)
    locked = [k for k in PATCH_LOCKED if k in b]
    if locked:
        raise Err(400, "cannot be set with PATCH: %s — they are owned by the transitions"
                  % ", ".join(locked))
    fields = {k: b[k] for k in PATCHABLE if k in b}
    if not fields:
        raise Err(400, "nothing to change (repo, touches, risk, priority)")
    if "touches" in fields:
        v = fields["touches"]
        if isinstance(v, str):
            v = [x for x in v.split(",") if x]
        if not isinstance(v, list):
            raise Err(400, "touches must be a list")
        fields["touches"] = json.dumps(v)
    if "priority" in fields:
        try:
            fields["priority"] = int(fields["priority"])
        except (TypeError, ValueError):
            raise Err(400, "priority must be an integer")
    if "risk" in fields and fields["risk"] not in (None, "low", "normal", "high"):
        raise Err(400, "unknown risk %r" % fields["risk"])
    if "repo" in fields:
        check_repo(t["project"], fields["repo"])
    db.execute("UPDATE tasks SET %s, updated=? WHERE id=?" % (", ".join("%s=?" % k for k in fields)),
               list(fields.values()) + [now(), tid])
    ev(t["project"], "task/" + tid, "task.patched", aid,
       **{k: b[k] for k in PATCHABLE if k in b})
    return task_show(tid)


def task_blocked(tid, aid, b):
    t = task(tid)
    db.execute("UPDATE tasks SET status='blocked', updated=? WHERE id=?", (now(), tid))
    ev(t["project"], "task/" + tid, "task.blocked", aid, note=b.get("note"))
    ntfy("⛔ %s %s blocked" % (t["project"], tid), b.get("note") or t["title"],
         "%s/status" % BASE_URL)
    return {"ok": True}


def task_review(tid, aid, b):
    t = task(tid)
    db.execute("UPDATE tasks SET review_open=?, review_fixed=?, status='in_review', updated=? WHERE id=?",
               (int(b.get("open", 0)), int(b.get("fixed", 0)), now(), tid))
    ev(t["project"], "task/" + tid, "task.review_result", aid,
       open=int(b.get("open", 0)), fixed=int(b.get("fixed", 0)), sha=b.get("sha"))
    return {"ok": True}


def gate_merge(tid, aid):
    t, reasons = task(tid), []
    phase = phase_of(t["project"])
    if t["status"] not in ("claimed", "in_review", "merging"):
        reasons.append("the task is %s, not ready for merge" % t["status"])
    try:
        owns(t, aid)
    except Err as e:
        reasons.append(e.body["error"])
    # "One PR merged at a time" is a prompt rule PER agent. With several agents in the
    # same project a mechanical mutex is needed: two simultaneous merges invalidate each
    # other's worktrees and leave a rebase tangle nobody asked for. The lock is released
    # by `done`, and by the reaper if whoever held it died.
    # The lock covers the merge window itself, not the rest of the task. If merge_sha is
    # set the merge HAS landed — what remains (deploy, done) does not touch anyone
    # else's branches, and holding the lock through it locked the queue for everyone.
    other = db.execute("SELECT id, owner FROM tasks WHERE project=? AND status='merging' "
                       "AND merge_sha IS NULL AND id<>?", (t["project"], tid)).fetchone()
    if other:
        # The reason must say what the agent should DO. "wait" reads as foreground
        # waiting, and three agents waiting at once is exactly what DESIGN.md §7 forbids.
        reasons.append("%s is merging right now (%s) — write progress and take ANOTHER "
                       "task; come back to this one later. Do not wait in the foreground."
                       % (other["id"], other["owner"] or "unowned"))
    if t["review_open"] is None:
        reasons.append("no review result reported (board task review)")
    else:
        # DESIGN.md §8 admits the hole: "an agent that writes board task review --open 0
        # without having run a review". Phase 3 was to require that the review event comes
        # from a DIFFERENT agent id than the owner's. That rule was missing, and a
        # coordinator reviewed its own task — the gate let it through on its own word.
        # Not watertight: an agent can still log a review it never ran. But it can no
        # longer do so on WORK IT OWNS ITSELF, and that is the common failure.
        rev = db.execute("SELECT actor FROM events WHERE stream=? AND type='task.review_result' "
                         "ORDER BY id DESC LIMIT 1", ("task/" + tid,)).fetchone()
        if rev and t["owner"] and rev["actor"] == t["owner"]:
            reasons.append("the review result was set by the owner itself (%s) — a fresh "
                           "agent must review, otherwise the gate is only an echo of the "
                           "owner's word" % rev["actor"])
        if t["review_open"] > 0 and phase != "idea":
            reasons.append("%d open review findings" % t["review_open"])
    if aid:
        if "merge" not in agent_grants(aid, t["project"]):
            reasons.append("the agent lacks the merge grant in %s" % t["project"])
        if not set(jl(t["needs_grants"])) <= agent_grants(aid, t["project"]):
            reasons.append("missing grants: %s" % ",".join(jl(t["needs_grants"])))
    # Simplification: CI status is not yet queried from Forgejo (needs forge credentials
    # on the board). Branch protection is the mechanical safeguard meanwhile — §8.1.
    return {"ok": not reasons, "reasons": reasons, "phase": phase, "risk": t["risk"]}


def task_merge_requested(tid, aid, b):
    t = task(tid)
    owns(t, aid)
    g = gate_merge(tid, aid)
    if not g["ok"]:
        raise Err(409, "gate closed", **g)
    db.execute("UPDATE tasks SET status='merging', updated=? WHERE id=?", (now(), tid))
    ev(t["project"], "task/" + tid, "task.merge_requested", aid, pr=t["pr"])
    return g


def task_merge_verified(tid, aid, b):
    t = task(tid)
    owns(t, aid)
    # The board has no working copy and cannot know whether the sha is on main — that
    # check is done by bin/board against origin/main, where git actually exists. Here we
    # only stop something that is NOT a sha from being stored as one: an empty or
    # invented string made `done` go green, and the task stood as merged with nothing
    # landed.
    sha = (b.get("sha") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise Err(400, "sha is missing or is not a git sha")
    db.execute("UPDATE tasks SET merge_sha=?, updated=? WHERE id=?", (sha, now(), tid))
    ev(t["project"], "task/" + tid, "task.merge_verified", aid, sha=sha)
    return {"ok": True}


def deploys(project):
    """Does the manifest declare a deploy for this project?"""
    row = db.execute("SELECT manifest FROM projects WHERE name=?", (project,)).fetchone()
    return bool((jl(row["manifest"], {}) if row and row["manifest"] else {}).get("deploy"))


def task_comment(tid, b):
    """Free text on a task, from human or agent. Stored as an event, not as a column:
    the timeline reads events anyway, and a comment is by definition something that
    happened at a point in time. NEVER shown in `status` or `task list` — those are hot
    paths every agent calls several times per task, and the context budget is hard
    (§6.5)."""
    t = task(tid)
    text = (b.get("text") or "").strip()
    if not text:
        raise Err(400, "empty comment")
    if len(text) > 4000:
        raise Err(400, "the comment is over 4000 characters — put it in the spec instead")
    # T-354: a comment nobody can be held to is refused, not filed as "unknown".
    if as_human(b):
        who = HUMAN
    elif b.get("agent"):
        who = agent(b["agent"])["id"]
    else:
        raise Err(400, "a comment needs an author: send agent, or the human token")
    ev(t["project"], "task/" + tid, "task.comment", who, text=text)
    # A blocked task waits on the human. His comment IS the answer, so it goes back in the
    # queue, unowned, and the next agent reads the comment. Only the human token does
    # this — an agent commenting its own block away would skip §3.7.
    if who == HUMAN and t["status"] == "blocked":
        db.execute("UPDATE tasks SET status='open', owner=NULL, lease_until=NULL, updated=? "
                   "WHERE id=?", (now(), tid))
        db.execute("UPDATE agents SET current_task=NULL WHERE current_task=?", (tid,))
        ev(t["project"], "task/" + tid, "task.unblocked", who, owner=t["owner"],
           note="the human commented on a blocked task")
        return {"ok": True, "by": who, "status": "open"}
    return {"ok": True, "by": who}


def task_deployed(tid, aid, b):
    t = task(tid)
    owns(t, aid)
    ev(t["project"], "task/" + tid, "task.deployed", aid, env=b.get("env", "dev"),
       note=b.get("note"), sha=t["merge_sha"])
    return {"ok": True, "env": b.get("env", "dev")}


def task_done(tid, aid, b):
    t = task(tid)
    owns(t, aid)
    if not t["merge_sha"] and not b.get("no_merge"):
        raise Err(409, "missing merge_verified (use no_merge for docs-only)")
    # Merged code that is not rolled out is not done: it is invisible to the human who is
    # to test it. Requires a deploy event AFTER the merge sha.
    if t["merge_sha"] and deploys(t["project"]):
        seen = db.execute(
            """SELECT 1 FROM events WHERE stream=? AND type='task.deployed'
               AND id > COALESCE((SELECT MAX(id) FROM events WHERE stream=?
                                  AND type='task.merge_verified'), 0)""",
            ("task/" + tid, "task/" + tid)).fetchone()
        if not seen:
            raise Err(409, "the project declares a deploy: run `board task deploy %s` "
                           "(it runs the manifest's command AND reports it) before done. "
                           "Use `board task deployed %s` only when you have already rolled "
                           "out by hand." % (tid, tid), needs_deploy=True)
    db.execute("UPDATE tasks SET status='done', owner=NULL, lease_until=NULL, updated=? WHERE id=?",
               (now(), tid))
    
    db.execute("UPDATE agents SET current_task=NULL WHERE current_task=?", (tid,))
    
    if dict(t).get("routine"):
        r = db.execute("SELECT * FROM routines WHERE project=? AND name=?", (t["project"], t["routine"])).fetchone()
        if r:
            last_run = now()
            interval = parse_interval(r["interval"])
            if interval is None:
                interval = timedelta(days=1)
            next_due = (datetime.fromisoformat(last_run.replace("Z", "+00:00")) + interval).isoformat().replace("+00:00", "Z")
            db.execute("UPDATE routines SET last_run=?, next_due=? WHERE project=? AND name=?",
                       (last_run, next_due, t["project"], t["routine"]))


    ev(t["project"], "task/" + tid, "task.done", aid, sha=t["merge_sha"])
    return {"ok": True}


def task_archive(tid, aid, b):
    """Archive a task (status='archived'). Hidden from /status and ordinary searches."""
    t = task(tid)
    if t["owner"] and t["owner"] != aid:
        raise Err(409, "the task is owned by %s — cannot be archived while it is being "
                       "worked on" % t["owner"])
    db.execute("UPDATE tasks SET status='archived', owner=NULL, lease_until=NULL, updated=? WHERE id=?",
               (now(), tid))
    
    db.execute("UPDATE agents SET current_task=NULL WHERE current_task=?", (tid,))
    
    if dict(t).get("routine"):
        r = db.execute("SELECT * FROM routines WHERE project=? AND name=?", (t["project"], t["routine"])).fetchone()
        if r:
            last_run = now()
            interval = parse_interval(r["interval"])
            if interval is None:
                interval = timedelta(days=1)
            next_due = (datetime.fromisoformat(last_run.replace("Z", "+00:00")) + interval).isoformat().replace("+00:00", "Z")
            db.execute("UPDATE routines SET last_run=?, next_due=? WHERE project=? AND name=?",
                       (last_run, next_due, t["project"], t["routine"]))

    

    ev(t["project"], "task/" + tid, "task.archived", aid, note=b.get("note"))
    return {"ok": True}


def release(tid, aid, b):
    """Release IS the implementer→coordinator hand-off: a task that stands `in_review`
    keeps its status and merely becomes unowned. Without that, finished work with an open
    PR became invisible to `task next` and locked by `owns()` — deadlock on exactly what
    was worth the most."""
    t = task(tid)
    owns(t, aid)
    keep = t["status"] if t["status"] == "in_review" else "open"
    db.execute("UPDATE tasks SET status=?, owner=NULL, lease_until=NULL, updated=? WHERE id=?",
               (keep, now(), tid))
    
    db.execute("UPDATE agents SET current_task=NULL WHERE current_task=?", (tid,))
    

    ev(t["project"], "task/" + tid, "task.released", aid, note=b.get("note"))
    return {"ok": True}


# ---------- questions, messages -------------------------------

DUR = {"m": 1, "h": 60, "d": 1440}


def deadline_of(v):
    """`8h`/`45m`/`2d` → ISO. The reaper must never meet something it cannot parse."""
    if not v:
        return None
    v = str(v).strip()
    if v[-1:] in DUR and v[:-1].isdigit():
        return plus(int(v[:-1]) * DUR[v[-1]])
    try:
        ts(v)
    except (ValueError, TypeError):
        raise Err(400, "invalid deadline %r — use ISO-8601 or 8h/45m/2d" % v)
    return v


def question_create(b, actor):
    project = b.get("project")
    kind = b.get("kind", "question")
    if not project:
        raise Err(400, "project is missing")
    if kind == "test":
        # The human test stage is gone (T-352): whoever builds a change tests it with the
        # CLI, playwright or test code, and the human tests after deploy.
        raise Err(400, "test cards were removed — test it yourself (CLI, playwright, test "
                       "code); the human tests in dev/prod after deploy")
    if not b.get("text"):
        raise Err(400, "text is missing")
    qid = next_id("Q-", "questions")
    db.execute("""INSERT INTO questions (id,project,task,asked_by,kind,text,options,
                  default_answer,deadline,status,created) VALUES (?,?,?,?,?,?,?,?,?,'open',?)""",
               (qid, project, b.get("task"), actor, kind,
                b.get("text"), json.dumps(b.get("options", [])), b.get("default"),
                deadline_of(b.get("deadline")), now()))
    ev(project, "question/" + qid, "question.asked", actor, kind=kind, task=b.get("task"),
       text=b.get("text"))
    ntfy("❓ %s %s" % (project, b.get("task") or ""), b.get("text", ""),
         "%s/q/%s" % (BASE_URL, qid))
    return {"id": qid, "status": "open"}


def question_answer(qid, answer, who, note="", b=None):
    # An answer signed as the human is taken as the human's decision. The identity must
    # therefore be PROVEN: without this an agent could ask its own question and answer
    # it in the human's name, with nobody looking (T-143).
    if who == HUMAN and not as_human(b or {}):
        raise Err(403, "only the human token can answer as %s; answer under your own "
                       "agent id if you have an opinion about the question" % HUMAN,
                  needs_human_token=True)
    q = db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if not q:
        raise Err(404, "unknown question %s" % qid)
    # 'defaulted' is the board's own answer, not a human's — an explicit answer must
    # always be allowed to override it. 'answered' on the other hand is a human who has
    # already answered, and THAT must still be a 409 (T-197).
    if q["status"] not in ("open", "defaulted"):
        raise Err(409, "already %s" % q["status"], answer=q["answer"])
    was_defaulted = q["status"] == "defaulted"
    db.execute("UPDATE questions SET status='answered', answer=?, answered_by=?, answered=?, read=0 "
               "WHERE id=?", (answer, who, now(), qid))
    ev(q["project"], "question/" + qid, "question.answered", who, answer=answer,
       **({"overrode_default": q["answer"]} if was_defaulted else {}))
    # T-198: a human who overrides the board's own default must reach the WORK that was
    # done on the guess. Without this the loop does not close: the only delivery path was
    # inbox(), which hits EXACTLY the agent id that asked — and only if it is alive and
    # polls again. A task that finished perfectly normally is not caught by the
    # coordinator fallback (it is scoped to unowned and orphaned ones), so the board
    # "knew" yes while the merged code did no.
    # Same normalization as the SQL in inbox(): a human who confirms the default with
    # "Yes" or "yes " in the free-text field has overridden nothing, and must not trigger
    # a follow-up task at priority 90.
    if (was_defaulted and q["task"]
            and str(answer).strip().lower() != str(q["answer"] or "").strip().lower()):
        t = db.execute("SELECT * FROM tasks WHERE id=?", (q["task"],)).fetchone()
        ev(q["project"], "task/" + q["task"], "task.default_overridden", who,
           question=qid, was=q["answer"], now=answer)
        # Done and merged? Then the answer is new work, not a reopening.
        if t and t["status"] == "done":
            nid = task_create({
                "project": q["project"], "repo": t["repo"], "risk": t["risk"], "priority": 90,
                "title": "overridden default on %s: %s" % (q["task"], str(answer)[:60]),
                "spec": "%s was built and merged on the board's own default %r.\n\n%s "
                        "later answered %r (%s).\n\nOriginal task: %s\nPR: %s\nMerge: "
                        "%s\n\nGo over what was delivered and fix what rests on the "
                        "default."
                        % (q["task"], q["answer"], HUMAN, answer, qid, t["title"], t["pr"],
                           t["merge_sha"])}, "board")
            ev(q["project"], "task/" + q["task"], "task.created", "board", followup=nid["id"])
    return {"id": qid, "answer": answer}


def inbox(aid, q):
    a = agent(aid)
    rows = list(db.execute("SELECT * FROM questions WHERE asked_by=? AND status IN "
                           "('answered','defaulted') AND read=0", (aid,)))
    if q.get("as") == ["coordinator"]:
        # Including already-read answers (read=1): the asker may have died between
        # `inbox` and the next step, and then the answer is otherwise lost forever.
        rows += list(db.execute(
            """SELECT q.* FROM questions q LEFT JOIN tasks t ON t.id=q.task
               WHERE q.project=? AND q.status IN ('answered','defaulted')
               AND q.asked_by<>?
               AND (t.owner IS NULL OR t.status='orphaned'
                    -- T-198: an overridden default must reach the coordinator regardless
                    -- of owner status. The task may have finished perfectly normally, and
                    -- then there is no unowned or orphaned row to hang the answer on.
                    -- Done tasks are already delivered as a follow-up task in the queue,
                    -- so they are not repeated here.
                    -- read=0: here there IS a live recipient, unlike the unowned branch
                    -- above. Without the gate the answer came back in EVERY coordinator
                    -- poll until the task went done.
                    OR (q.read=0 AND q.default_answer IS NOT NULL AND q.answer IS NOT NULL
                        AND TRIM(ulower(q.answer))<>TRIM(ulower(q.default_answer))
                        AND (t.status IS NULL OR t.status NOT IN ('done', 'archived'))))""",
            (a["current_project"], aid)))
    msgs = list(db.execute("SELECT * FROM messages WHERE to_agent=? AND read=0", (aid,)))
    for r in rows:
        db.execute("UPDATE questions SET read=1 WHERE id=?", (r["id"],))
    for m in msgs:
        db.execute("UPDATE messages SET read=1 WHERE id=?", (m["id"],))
    return {"questions": [dict(r) for r in rows], "messages": [dict(m) for m in msgs]}


def message_send(b, actor):
    to = b.get("to")
    if not to:
        raise Err(400, "to is missing")
    agent(to)
    db.execute("INSERT INTO messages (project,to_agent,from_agent,text,task,ts) VALUES (?,?,?,?,?,?)",
               (b.get("project"), to, actor, b.get("text"), b.get("task"), now()))
    ev(b.get("project"), "agent/" + to, "message.sent", actor, task=b.get("task"), text=b.get("text"))
    return {"ok": True}


# ---------- roles ---------------------------------------------------------

def role_spec(role):
    return policy().get("roles", {}).get(role, {})


def qualifies(a, role, project):
    have = caps(a) | agent_grants(a["id"], project)
    for req in role_spec(role).get("requires", []):
        if not any(alt in have for alt in req.split("|")):
            return False
    return True


def ranking(project, role="coordinator"):
    prefer_model = role_spec(role).get("prefer_model", ["fable", "opus", "sonnet"])

    def model_rank(m):
        m = m or ""
        for i, name in enumerate(prefer_model):
            if name in m:
                return i
        return len(prefer_model)

    cands = []
    for a in db.execute("SELECT * FROM agents WHERE current_project=?", (project,)):
        if status_of(a) != "alive" or not qualifies(a, role, project):
            continue
        if a["preference"] and a["preference"] != role:
            continue
        cands.append((model_rank(a["model"]), max(windows_of(a).values() or [100]),
                      a["registered"] or "", a["id"], dict(a)))
    cands.sort(key=lambda c: c[:4])
    return [c[4] for c in cands]


def role_recommendation(project, role="coordinator"):
    r = ranking(project, role)
    if not r:
        return {"role": role, "recommended": None, "reason": "no living qualified agent"}
    top = r[0]
    reason = "%s, quota=%s, registered %s" % (top["model"], windows_of(top) or "unknown",
                                              top["registered"])
    ev(project, "role/" + role, "role.recommended", "board", agent=top["id"], reason=reason)
    return {"role": role, "recommended": top["id"], "reason": reason,
            "order": [a["id"] for a in r]}


def role_holder(project, role):
    r = db.execute("SELECT * FROM roles WHERE project=? AND role=?", (project, role)).fetchone()
    return r


def role_pin(project, role, aid, who=HUMAN, b=None):
    # §3.8: "the human's word wins". That means it must be PROVEN that the human is
    # speaking — otherwise an agent pins itself as pinned_by=<human> and becomes
    # untouchable.
    human_only(b or {}, "pinning a role")
    agent(aid)
    db.execute("DELETE FROM roles WHERE project=? AND role=?", (project, role))
    db.execute("INSERT INTO roles (project,role,agent,source,pinned_by,since,lease_until) "
               "VALUES (?,?,?,'pinned',?,?,NULL)", (project, role, aid, who, now()))
    ev(project, "role/" + role, "role.pinned", who, agent=aid)
    return {"role": role, "agent": aid, "source": "pinned"}


def role_unpin(project, role, b=None, who=HUMAN):
    human_only(b or {}, "removing a pin")
    db.execute("DELETE FROM roles WHERE project=? AND role=? AND source='pinned'", (project, role))
    ev(project, "role/" + role, "role.unpinned", who)
    return {"ok": True}


def role_claim(project, role, aid):
    a = agent(aid, alive_only=True)
    same_project(a, project)
    if not qualifies(a, role, project):
        raise Err(409, "missing capabilities for %s" % role,
                  requires=role_spec(role).get("requires", []))
    if not role_spec(role).get("singleton"):
        db.execute("INSERT OR REPLACE INTO roles (project,role,agent,source,since,lease_until) "
                   "VALUES (?,?,?,'elected',?,NULL)", (project, role, aid, now()))
        ev(project, "role/" + role, "role.claimed", aid)
        return {"role": role, "agent": aid}
    held = role_holder(project, role)
    if held and held["agent"] != aid:
        h = db.execute("SELECT * FROM agents WHERE id=?", (held["agent"],)).fetchone()
        hstatus = status_of(h) if h else "dead"
        if held["source"] == "pinned" and hstatus in ("alive", "stale", "stalled"):
            raise Err(409, "the role is pinned to %s" % held["agent"], holder=held["agent"])
        if held["source"] != "pinned" and hstatus == "alive":
            raise Err(409, "the role has a living holder", holder=held["agent"])
    rec = role_recommendation(project, role)
    if rec["recommended"] and rec["recommended"] != aid:
        raise Err(409, "the ranking points at someone else", recommended=rec["recommended"],
                  reason=rec["reason"])
    db.execute("DELETE FROM roles WHERE project=? AND role=?", (project, role))
    db.execute("INSERT INTO roles (project,role,agent,source,since,lease_until) "
               "VALUES (?,?,?,'elected',?,NULL)", (project, role, aid, now()))
    ev(project, "role/" + role, "role.claimed", aid, reason=rec["reason"])
    ntfy("👑 %s: %s is coordinator" % (project, aid), rec["reason"], "%s/status" % BASE_URL)
    return {"role": role, "agent": aid, "source": "elected"}


# ---------- reaper --------------------------------------------------------

def reap():
    """Dead agents, expired leases, defaulted questions. Runs every minute."""
    for a in db.execute("SELECT * FROM agents WHERE status<>'finished'").fetchall():
        st = status_of(a)
        if st != a["status"]:
            db.execute("UPDATE agents SET status=? WHERE id=?", (st, a["id"]))
            if st == "dead":
                ev(a["current_project"], "agent/" + a["id"], "agent.presumed_dead", "board",
                   last_seen=a["last_seen"])
                for r in db.execute("SELECT * FROM roles WHERE agent=?", (a["id"],)).fetchall():
                    db.execute("DELETE FROM roles WHERE project=? AND role=? AND agent=?",
                               (r["project"], r["role"], a["id"]))
                    ev(r["project"], "role/" + r["role"], "role.released", "board",
                       agent=a["id"], reason="dead")
                    if r["role"] == "coordinator":
                        ntfy("💀 %s: coordinator %s dead" % (r["project"], a["id"]),
                             "the role is free", "%s/status" % BASE_URL)
    dead = {r["id"] for r in db.execute("SELECT id FROM agents WHERE status IN ('dead','finished')")}
    for t in db.execute("SELECT * FROM tasks WHERE status NOT IN ('done','open','orphaned','archived')").fetchall():
        # `blocked` is a documented wait, not a stop: it waits on a human for hours and
        # must not be orphaned by the lease running out. If the owner dies (dead/finished)
        # it must still be takeable.
        expired = (t["lease_until"] and mins_since(t["lease_until"]) > 0
                   and t["status"] != "blocked")
        # An unowned row in an owner state is impossible: nobody can claim it (wrong
        # status), and everything else requires ownership. Then it is wedged forever.
        # Release it.
        stuck = t["owner"] is None and t["status"] in ("claimed", "merging")
        if expired or stuck or (t["owner"] in dead):
            db.execute("UPDATE tasks SET status='orphaned', owner=NULL, "
                       "updated=? WHERE id=?", (now(), t["id"]))
            db.execute("UPDATE agents SET current_task=NULL WHERE current_task=?", (t["id"],))
            ev(t["project"], "task/" + t["id"], "task.orphaned", "board", was_owner=t["owner"])
    for q in db.execute("SELECT * FROM questions WHERE status='open' AND kind='question' "
                        "AND deadline IS NOT NULL").fetchall():
        try:
            overdue = mins_since(q["deadline"]) > 0
        except (ValueError, TypeError):        # old row from before the validation
            db.execute("UPDATE questions SET deadline=NULL WHERE id=?", (q["id"],))
            continue
        if overdue and q["default_answer"]:
            db.execute("UPDATE questions SET status='defaulted', answer=?, answered_by='board', "
                       "answered=?, read=0 WHERE id=?", (q["default_answer"], now(), q["id"]))
            ev(q["project"], "question/" + q["id"], "question.defaulted", "board",
               answer=q["default_answer"])
            ntfy("⏱ %s: the board answered %r on %s" % (q["project"], q["default_answer"], q["id"]),
                 "the deadline passed — you can still override the answer",
                 "%s/q/%s" % (BASE_URL, q["id"]))
    
    for r in db.execute("SELECT * FROM routines WHERE status='open'").fetchall():
        is_due = False
        if r["next_due"] and mins_since(r["next_due"]) >= 0:
            is_due = True
        elif not r["next_due"] and not r["last_run"]:
            is_due = True
        
        if is_due:
            open_task = db.execute("SELECT id FROM tasks WHERE project=? AND routine=? AND status NOT IN ('done','archived','orphaned')", (r["project"], r["name"])).fetchone()
            if not open_task:
                b = {
                    "project": r["project"],
                    "title": r["title"] or r["name"],
                    "spec": r["spec"] or "Automated routine task",
                    "repo": r["repo"],
                    "priority": r["priority"] if r["priority"] is not None else 50,
                    "routine": r["name"]
                }
                tid = task_create(b, "board")["id"]
                ntfy("⏰ %s: routine %s is due" % (r["project"], r["title"]), "Spawned task %s" % tid, "%s/t/%s" % (BASE_URL, tid))

    db.commit()
    return {"ok": True}


def reaper_loop():
    while True:
        time.sleep(REAP_INTERVAL)
        try:
            with LOCK:
                reap()
        except Exception as e:                       # the board must never die of the reaper
            print("reaper:", e, flush=True)


# ---------- status --------------------------------------------------------

def status(project=None):
    with NTFY_LOCK:
        fails = ntfy_failures_since_success
    out = {"generated": now(), "projects": [], "ntfy_failures_since_success": fails}
    names = [project] if project else [r["name"] for r in db.execute(
        "SELECT name FROM projects ORDER BY name")]
    for name in names:
        p = db.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        if not p:
            continue
        agents = []
        for a in db.execute("SELECT * FROM agents WHERE current_project=? AND status<>'finished'", (name,)):
            d = dict(a)
            d["status"] = status_of(a)
            d["roles"] = [r["role"] for r in db.execute(
                "SELECT role FROM roles WHERE project=? AND agent=?", (name, a["id"]))]
            d["grants"] = sorted(agent_grants(a["id"], name))
            d["budget"] = jl(a["budget"])
            # the board answers in full: if `stop` is set, the agent must stop. Thresholds
            # belong here, not spread across every SKILL that reads the board (T-164).
            d["stop"] = agent_stop(a, name)
            agents.append(d)
        out["projects"].append({
            "name": name, "phase": p["phase"], "goal": p["goal"], "paused": p["paused"],
            "agents": agents,
            # /status is an overview for the human: show the Claude fleet's numbers, which
            # are what drive the stop rules for most of the agents.
            "budget": dict(budget(name), windows=quota_max()),
            "tasks": [brief(t) for t in db.execute(
                "SELECT * FROM tasks WHERE project=? AND status NOT IN ('done', 'archived') "
                "ORDER BY priority DESC, created", (name,))],
            "questions": [dict(q) for q in db.execute(
                "SELECT * FROM questions WHERE project=? AND status='open' ORDER BY created", (name,))],
            # the board answered itself (the deadline ran out) — still overridable by a
            # human, so it must be visible without being mixed into "open" and confusing
            # existing readers
            "questions_defaulted": [dict(q) for q in db.execute(
                "SELECT * FROM questions WHERE project=? AND status='defaulted' ORDER BY created", (name,))],
            "roles": [dict(r) for r in db.execute("SELECT * FROM roles WHERE project=?", (name,))],
        })
    return out


# ---------- HTML ----------------------------------------------------------

# The stylesheet is built by ui/build.sh and is CHECKED IN as board.css: the board is
# stdlib-only Python in production, and the CSP (default-src 'none') lets no CDN in. The
# filename carries the content hash, so the pages can be cached forever on the phone and
# still come back new after a deploy — they are opened from ntfy notifications.
CSS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "board.css")
try:
    CSS_BYTES = open(CSS_PATH, "rb").read()
except OSError:
    CSS_BYTES = b""
    sys.stderr.write("board.css is missing at %s — the pages will be unstyled. "
                     "Run ui/build.sh.\n" % CSS_PATH)
CSS_URL = "/board.%s.css" % hashlib.sha256(CSS_BYTES).hexdigest()[:12]

WRAP = "mx-auto w-full max-w-6xl px-4 pb-24 pt-4 sm:px-6"
LINK = "underline decoration-base-300 decoration-2 underline-offset-4 hover:decoration-warning"
MONO = "font-mono tabular-nums"
DIM = "text-base-content/60"
LBL = "mt-6 mb-1 text-sm text-base-content/60"


def page(title, body):
    return ("<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s</title><link rel=stylesheet href='%s'>"
            "<body class='bg-base-100 text-base-content font-sans antialiased'>"
            "<div class='%s'>%s</div>" % (escape(title), CSS_URL, WRAP, body))


def whoami(human):
    """Which identity the cookie in this browser carries.

    A page rendered with an agent token looks exactly like one rendered with the human
    token — the same tasks, the same answer buttons — and then
    refuses every answer. The login appears to have worked right up until the moment you
    try to use it, which is the worst possible place to find out. So the page says it,
    always, on the surface where the confusion happens: the phone."""
    if human:
        return ("<span class='badge badge-sm badge-ghost'>%s</span>" % escape(HUMAN))
    return ("<span class='badge badge-sm badge-warning' "
            "title='signed in as an agent: answers will be refused'>agent</span>")


def head(title, right="", nav=(("/status", "board"),), human=False, show_all_btn=False):
    btn = "<button id='btn-show-all' class='badge badge-sm badge-ghost cursor-pointer font-sans select-none' title='Vis alt / Skjul inaktive'>vis alt</button>" if show_all_btn else ""
    return ("<header class='flex flex-wrap items-baseline gap-x-3 gap-y-1 "
            "border-b-2 border-base-300 pb-3'>"
            "<h1 class='text-xl font-semibold tracking-tight sm:text-2xl'>%s</h1>%s"
            "<nav class='ml-auto flex items-baseline gap-4 text-sm'>%s%s%s</nav></header>" % (
                escape(title), right,
                "".join("<a class='%s' href='%s'>%s</a>" % (LINK, u, escape(t))
                        for u, t in nav),
                btn,
                whoami(human)))


def answer_saved_page(qid, human):
    """What you see the instant after answering, on a phone, one-handed.

    It used to be three lines of markup using a `.top` class that does not exist in the
    stylesheet — so the one page reached by pressing the one button rendered unstyled.
    ui/verify.sh never caught it because verify never POSTs an answer, so the class
    checker never saw this markup. Rendered pages are now part of that check.

    The links matter more than the styling. After answering you are either done (back to
    the board) or you have more cards waiting (back to the queue), and if the question
    belonged to a task you usually want to see what it was about. Landing on a dead end
    means finding your way back by typing a URL on a phone."""
    q = db.execute("SELECT project, task, kind FROM questions WHERE id=?", (qid,)).fetchone()
    task_link = ""
    if q and q["task"]:
        task_link = ("<a class='btn btn-outline min-h-12 flex-1' href='/t/%s'>Task %s</a>"
                     % (escape(q["task"]), escape(q["task"])))
    return page("answer saved", head("answer saved", human=human) + (
        "<div class='mt-5 max-w-[42rem] rounded-box border border-base-300 border-l-4 "
        "border-l-success bg-base-200 p-4'>"
        "<p class='text-lg leading-snug'>The answer is saved.</p>"
        "<p class='%s mt-1 text-sm'>The agent picks it up in its inbox and carries on. "
        "Nothing is waiting on you for this one.</p>"
        "<div class='mt-4 flex flex-wrap gap-3'>"
        "<a class='btn btn-primary min-h-12 flex-1' href='/status'>The board</a>"
        "%s</div></div>" % (DIM, task_link)))


def not_human_page(what):
    """A refused human action, rendered as a page rather than as raw JSON.

    The API answers `needs_human_token` and that is right for an agent. But /q/<id>/answer
    and /t/<id>/comment are HTML forms pressed by a person on a phone, and a JSON blob
    tells them nothing they can act on. Say what is wrong and the one command that fixes
    it."""
    return page("signed in as an agent", head("signed in as an agent") + (
        "<div class='mt-5 max-w-[42rem] rounded-box border border-base-300 border-l-4 "
        "border-l-warning bg-base-200 p-4'>"
        "<p class='max-w-[68ch] text-lg leading-snug'>This browser is signed in as an "
        "<b class='font-semibold'>agent</b>, so %s was refused.</p>"
        "<p class='%s mt-3 text-sm'>Everything renders, but only %s can answer. Run this "
        "in your own shell — not inside an agent session — and scan the code again:</p>"
        "<p class='mt-2'><code class='rounded bg-base-100 px-1 font-mono text-sm'>"
        "board open --qr</code></p></div>" % (escape(what), DIM, escape(HUMAN))))


def meter(label, pct, ceiling=None):
    """The meter. Quota is the only thing that stops the fleet, so the ceiling is drawn
    as a hard line with its own number beside it — hence not daisyUI's `progress`, which
    cannot draw a ceiling line, and the ceiling line is the entire point.

    Yellow five points BEFORE the ceiling is not decoration: the stop rule says that
    within five points you must not start a task you cannot finish. The rule belongs in
    the meter's colour, not only in the prose."""
    v = float(pct or 0)
    c = float(ceiling or 0)
    cls = ""
    if c > 0:
        cls = "over" if v >= c else ("warn" if v >= c - 5 else "")
    tick = ("<u data-c='%s' style='left:%s%%'></u>" % (round(c), round(min(c, 100)))) if c > 0 else ""
    return ("<div class=meter><span class=k>%s</span>"
            "<span class=track><i class='%s' style='width:%s%%'></i>%s</span>"
            "<span class=v>%s%%</span></div>" % (
                escape(label), cls, round(min(v, 100)), tick, round(v)))


SPINE = {"blocked": "spine-stop", "orphaned": "spine-stop",
         "merged": "spine-land", "done": "spine-land", "archived": ""}

BADGE = {"blocked": "badge-error", "orphaned": "badge-error",
         "merged": "badge-success", "done": "badge-success", "archived": "badge-ghost"}


def waiting_on_you(p):
    """What requires a human in this project."""
    return len(p["questions"])


def stuck(p):
    return len([t for t in p["tasks"] if t["status"] in ("blocked", "orphaned")])


def watchline(s):
    """The top of /status answers the two questions you have at three in the morning, in
    the board's own language: is the fleet running, and is anything waiting on me? Not
    counter tiles — one sentence, with the numbers inside it and linked where you need to
    go."""
    live = sum(1 for p in s["projects"] for a in p["agents"] if a["status"] == "alive")
    wait = sum(waiting_on_you(p) for p in s["projects"])
    stop = sum(stuck(p) for p in s["projects"])
    # The number is a link, and it must hit SOMETHING — the first thing it counts, not
    # an anchor that does not exist on the page.
    first = lambda gen, dflt: next(gen, dflt)
    wait_url = first((("/q/%s" % q["id"]) for p in s["projects"] for q in p["questions"]),
                     "/status")
    stop_url = first((("/t/%s" % t["id"]) for p in s["projects"] for t in p["tasks"]
                      if t["status"] in ("blocked", "orphaned")), "/status")
    n = lambda v, cls, href, txt: (
        "<a class='%s underline decoration-2 underline-offset-4' href='%s'>"
        "<b class='%s font-semibold'>%s</b> %s</a>" % (cls, href, MONO, v, escape(txt)))
    parts = []
    if live:
        parts.append("<span><b class='%s font-semibold'>%s</b> %s working.</span>" % (
            MONO, live, "agent" if live == 1 else "agents"))
    else:
        parts.append("<span class='%s'>No agents are working right now.</span>" % DIM)
    if wait:
        parts.append(n(wait, "text-warning decoration-warning", wait_url,
                       "waiting on you." if wait == 1 else "waiting on you."))
    if stop:
        parts.append(n(stop, "text-error decoration-error", stop_url,
                       "is blocked." if stop == 1 else "are blocked."))
    if not wait and not stop:
        parts.append("<span class='%s'>Nothing is waiting on you.</span>" % DIM)
    return ("<p class='mt-5 flex flex-wrap gap-x-3 gap-y-1 text-lg leading-snug "
            "sm:text-2xl'>%s</p>" % "".join(parts))


def fleet_runway(s):
    """Quota is per HARNESS account, not per agent and not per project (§11) — so the
    runway is per harness. The ceiling drawn is the TIGHTEST ceiling among the projects
    the page shows: it is the first one the fleet hits."""
    ceil = {}
    for p in s["projects"]:
        for w, c in ((p.get("budget") or {}).get("ceilings") or {}).items():
            w, c = win_name(w), float(c or 0)
            ceil[w] = min(ceil.get(w, c), c)
    harnesses = sorted({(a.get("harness") or "claude-code")
                        for p in s["projects"] for a in p["agents"]})
    rows = []
    for h in harnesses:
        ws = quota_max(h)
        if not ws:
            continue
        rows.append("<div class='min-w-0'><p class='%s mb-1 text-xs'>%s</p>%s</div>" % (
            DIM, escape(h),
            "".join(meter(w, pct, ceil.get(w)) for w, pct in sorted(ws.items()))))
    if not rows:
        return ""
    return ("<div class='mt-5 grid gap-x-8 gap-y-4 border-t border-base-300 pt-4 "
            "sm:grid-cols-2'>%s</div>" % "".join(rows))


def tell(p):
    """The counter strip in the heading: a collapsed project must still answer "is
    anything stuck, and is anything waiting on me?"."""
    n = {}
    for t in p["tasks"]:
        n[t["status"]] = n.get(t["status"], 0) + 1
    parts = [("", len(p["agents"]), "agents" if len(p["agents"]) != 1 else "agent"),
             ("", len(p["tasks"]), "queued")]
    for st, lbl, cls in (("in_review", "to review", ""),
                         ("blocked", "blocked", "text-error"),
                         ("orphaned", "unowned", "text-error")):
        if n.get(st):
            parts.append((cls, n[st], lbl))
    if p["questions"]:
        parts.append(("text-warning", len(p["questions"]),
                      "question" if len(p["questions"]) == 1 else "questions"))
    return ("<span class='flex flex-wrap justify-end gap-x-3 gap-y-1 text-xs %s'>%s</span>" % (
        DIM, "".join("<span class='%s'><b class='%s font-semibold'>%s</b> %s</span>" % (
            c, MONO, v, escape(l)) for c, v, l in parts)))


def last_activity(p):
    """Last movement, not last manifest change. p["tasks"]/p["agents"] are already
    filtered for display (done/finished are removed there), so a project whose last event
    was precisely "task went done" would otherwise sort as dead. Ask the database
    directly instead, regardless of status."""
    name = p["name"]
    t = db.execute("SELECT MAX(updated) m FROM tasks WHERE project=?", (name,)).fetchone()["m"]
    a = db.execute("SELECT MAX(last_seen) m FROM agents WHERE current_project=?", (name,)).fetchone()["m"]
    return max(t or "", a or "", "")


# Opens the project you last looked at, and applies agent/task visibility filters.
FOCUS_JS = """
(function(){
var d=document.querySelectorAll('.pj'),k='board:focus',w=localStorage.getItem(k),o;
for(var i=0;i<d.length;i++){if(d[i].dataset.p===w)o=d[i];
d[i].addEventListener('toggle',function(){if(this.open)localStorage.setItem(k,this.dataset.p);
else if(localStorage.getItem(k)===this.dataset.p)localStorage.removeItem(k);});}
(o||d[0]||{}).open=true;

var iv={'1h':60,'24h':1440,'7d':10080};
var pms=new URLSearchParams(window.location.search);
if(pms.get('all')==='1'||pms.get('all')==='true'){localStorage.setItem('board:show_all','1');}
else if(pms.has('all')){localStorage.setItem('board:show_all','0');}
if(pms.get('interval')){
  localStorage.setItem('board:agent_filter',pms.get('interval'));
  localStorage.setItem('board:task_filter',pms.get('interval'));
}

function upd(){
  var sa=localStorage.getItem('board:show_all')==='1';
  var b=document.getElementById('btn-show-all');
  if(b){
    if(sa){b.textContent='viser alt';b.classList.add('badge-primary');b.classList.remove('badge-ghost');}
    else{b.textContent='vis alt';b.classList.remove('badge-primary');b.classList.add('badge-ghost');}
  }
  var pjs=document.querySelectorAll('.pj');
  for(var p=0;p<pjs.length;p++){
    var pj=pjs[p];
    var as=pj.querySelector('[data-agent-filter]');
    var av=sa?'all':(localStorage.getItem('board:agent_filter')||(as?as.value:'24h'));
    if(as&&!sa)as.value=av;
    var ci=pj.querySelector('[data-agent-cleanup]');
    if(ci)ci.value=av;

    var ac=pj.querySelectorAll('[data-agent]');
    var ah=0;
    for(var c=0;c<ac.length;c++){
      var cd=ac[c];
      var st=cd.getAttribute('data-status');
      var mn=parseInt(cd.getAttribute('data-mins')||'0',10);
      var hd=false;
      if(!sa&&av!=='all'){
        if(av==='active'){hd=(st==='dead');}
        else if(iv[av]){hd=(st==='dead'&&mn>iv[av]);}
      }
      cd.style.display=hd?'none':'';
      if(hd)ah++;
    }
    var ab=pj.querySelector('[data-agent-badge]');
    if(ab){ab.textContent=ah>0?('('+ah+' døde skjult)'):'';}

    var ts=pj.querySelector('[data-task-filter]');
    var tv=sa?'all':(localStorage.getItem('board:task_filter')||(ts?ts.value:'active'));
    if(ts&&!sa)ts.value=tv;

    var tr=pj.querySelectorAll('[data-task]');
    var th=0;
    for(var r=0;r<tr.length;r++){
      var rw=tr[r];
      var tst=rw.getAttribute('data-status');
      var tmn=parseInt(rw.getAttribute('data-mins')||'0',10);
      var thd=false;
      if(!sa&&tv!=='all'){
        if(tv==='active'){thd=(tst==='done');}
        else if(tv==='in_flight'){thd=(tst!=='claimed'&&tst!=='in_review'&&tst!=='merging');}
        else if(iv[tv]){thd=(tmn>iv[tv]);}
      }
      rw.style.display=thd?'none':'';
      if(thd)th++;
    }
    var tb=pj.querySelector('[data-task-badge]');
    if(tb){tb.textContent=th>0?('('+th+' skjult)'):'';}
  }
}

var afs=document.querySelectorAll('[data-agent-filter]');
for(var i=0;i<afs.length;i++){
  afs[i].addEventListener('change',function(){
    localStorage.setItem('board:agent_filter',this.value);
    localStorage.setItem('board:show_all','0');
    upd();
  });
}
var tfs=document.querySelectorAll('[data-task-filter]');
for(var j=0;j<tfs.length;j++){
  tfs[j].addEventListener('change',function(){
    localStorage.setItem('board:task_filter',this.value);
    localStorage.setItem('board:show_all','0');
    upd();
  });
}
var sab=document.getElementById('btn-show-all');
if(sab){
  sab.addEventListener('click',function(e){
    e.preventDefault();
    var cur=localStorage.getItem('board:show_all')==='1';
    localStorage.setItem('board:show_all',cur?'0':'1');
    upd();
  });
}
var cforms=document.querySelectorAll('form[data-confirm]');
for(var k=0;k<cforms.length;k++){
  cforms[k].addEventListener('submit',function(e){
    if(!confirm(this.getAttribute('data-confirm'))){e.preventDefault();}
  });
}
upd();
})();
"""
# The CSP lets no script-src in; the hash keeps it exactly as tight as before.
FOCUS_SHA = "'sha256-%s'" % base64.b64encode(
    hashlib.sha256(FOCUS_JS.encode()).digest()).decode()
FOCUS = "<script>%s</script>" % FOCUS_JS


def agent_block(a, ceilings, human=False, project=None):
    roles = " ".join(a["roles"]) if a.get("roles") else ""
    ws = sorted(windows_of(a).items())
    st = a.get("status") or "unknown"
    mins = int(mins_since(a.get("last_seen"))) if a.get("last_seen") else 999999
    grants_badges = []
    for g in a.get("grants", []):
        rev = ""
        if human:
            rev = ("<form method='POST' action='/agents/%s/grants/revoke' class='inline m-0'>"
                   "<input type='hidden' name='grant' value='%s'>"
                   "<input type='hidden' name='project' value='%s'>"
                   "<button type='submit' class='cursor-pointer ml-1 text-xs opacity-60 hover:opacity-100 hover:text-error' title='Trekk tilbake grant'>✕</button>"
                   "</form>" % (escape(a["id"]), escape(g), escape(project or "")))
        grants_badges.append("<span class='badge badge-sm badge-outline font-mono'>%s%s</span>" % (escape(g), rev))
    grants_html = "".join(grants_badges)
    add_form = ""
    if human:
        add_form = ("<form method='POST' action='/agents/%s/grants' class='m-0 flex items-center gap-1 mt-1'>"
                    "<input type='hidden' name='project' value='%s'>"
                    "<input list='grants-list-%s' name='grant' placeholder='+ grant' class='rounded border border-base-300 bg-base-100 px-1.5 py-0.5 text-xs font-mono w-24'>"
                    "<datalist id='grants-list-%s'><option value='merge'><option value='deploy-dev'><option value='deploy-prod'></datalist>"
                    "<button type='submit' class='badge badge-sm badge-primary cursor-pointer'>gi</button></form>"
                    % (escape(a["id"]), escape(project or ""), escape(a["id"]), escape(a["id"])))
    return ("<div data-agent class='border-t border-base-300 py-2.5' data-status='%s' data-mins='%d'>"
            "<b class='%s block font-semibold'>%s</b>"
            "<div class='mt-0.5 flex flex-wrap items-baseline gap-x-2 gap-y-1'>"
            "<span class='%s text-xs'>%s</span>"
            "<span class='badge badge-sm %s'>%s</span>%s%s%s</div>"
            "%s"
            "<div class='mt-1.5 grid gap-1'>%s%s</div>%s</div>" % (
                escape(st), mins,
                MONO, escape(a["id"]), DIM, escape(a["model"] or ""),
                "badge-error" if a["status"] == "dead" else
                ("badge-warning" if a["status"] != "alive" else "badge-ghost"),
                escape(a["status"] or ""),
                ("<span class='badge badge-sm badge-outline'>%s</span>" % escape(roles))
                if roles else "",
                ("<a class='%s %s text-sm' href='/t/%s'>%s</a>" % (
                    LINK, MONO, escape(a["current_task"]), escape(a["current_task"])))
                if a["current_task"] else "",
                grants_html,
                add_form,
                meter("ctx", a["ctx_pct"], 80),
                "".join(meter(w, pct, ceilings.get(win_name(w))) for w, pct in ws)
                or "<p class='%s text-xs'>reports no quota</p>" % DIM,
                ("<p class='mt-1.5 text-sm text-error'>stop: %s</p>" % escape(a["stop"]))
                if a.get("stop") else ""))


def dash(v):
    """"—" is not information. On a phone every empty cell still becomes a row in the
    card, and five tasks with an empty pr, repo and owner became a screenful of dashes.
    `data-empty` lets the CSS hide exactly those rows in card mode — on a wide screen the
    column stands there as before."""
    return (escape(v), "") if v not in (None, "", "—") else ("—", " data-empty")


def task_rows(p):
    h = ["<div class='min-w-0 overflow-x-auto'><table class='tasks w-full text-sm'>"
         "<thead><tr class='%s text-xs'><th class='py-1 pl-3 pr-2 text-left font-semibold'>id"
         "<th class='px-2 py-1 text-left font-semibold'>status"
         "<th class='px-2 py-1 text-left font-semibold'>pr"
         "<th class='px-2 py-1 text-left font-semibold'>repo"
         "<th class='px-2 py-1 text-left font-semibold'>owner"
         "<th class='px-2 py-1 text-left font-semibold'>title</thead><tbody>" % DIM]
    for t in p["tasks"]:
        pn = pr_num(t.get("pr"))
        pr_v, pr_e = dash("#" + pn if pn else None)
        rp_v, rp_e = dash(t["repo"])
        ow_v, ow_e = dash(t["owner"])
        st = t.get("status") or "unknown"
        mins = int(mins_since(t.get("updated") or t.get("created"))) if (t.get("updated") or t.get("created")) else 0
        h.append("<tr id='%s' data-task class='spine %s' data-status='%s' data-mins='%d'>"
                 "<td data-l=id class='py-1.5 pl-3 pr-2 align-top whitespace-nowrap'>"
                 "<a class='%s %s' href='/t/%s'>%s</a>"
                 "<td data-l=status class='%s px-2 py-1.5 align-top whitespace-nowrap'>"
                 "<span class='badge badge-sm %s'>%s</span>"
                 "<td data-l=pr%s class='%s %s px-2 py-1.5 align-top whitespace-nowrap'>%s"
                 "<td data-l=repo%s class='%s px-2 py-1.5 align-top whitespace-nowrap'>%s"
                 "<td data-l=owner%s class='%s %s px-2 py-1.5 align-top whitespace-nowrap'>%s"
                 "<td data-l=title class='px-2 py-1.5 align-top font-medium "
                 "[overflow-wrap:anywhere]'>%s" % (
                     escape(t["id"]), SPINE.get(t["status"], ""), escape(st), mins,
                     LINK, MONO,
                     escape(t["id"]), escape(t["id"]),
                     DIM, BADGE.get(t["status"], "badge-ghost"), escape(t["status"]),
                     pr_e, DIM, MONO, pr_v,
                     rp_e, DIM, rp_v,
                     ow_e, DIM, MONO, ow_v,
                     escape(t["title"] or "")))
    recent_done = [brief(t) for t in db.execute(
        "SELECT * FROM tasks WHERE project=? AND status='done' ORDER BY updated DESC LIMIT 30", (p["name"],))]
    for t in recent_done:
        pn = pr_num(t.get("pr"))
        pr_v, pr_e = dash("#" + pn if pn else None)
        rp_v, rp_e = dash(t["repo"])
        ow_v, ow_e = dash(t["owner"])
        mins = int(mins_since(t.get("updated") or t.get("created"))) if (t.get("updated") or t.get("created")) else 0
        h.append("<tr id='%s' data-task class='spine spine-land' data-status='done' data-mins='%d' style='display:none;'>"
                 "<td data-l=id class='py-1.5 pl-3 pr-2 align-top whitespace-nowrap'>"
                 "<a class='%s %s' href='/t/%s'>%s</a>"
                 "<td data-l=status class='%s px-2 py-1.5 align-top whitespace-nowrap'>"
                 "<span class='badge badge-sm %s'>%s</span>"
                 "<td data-l=pr%s class='%s %s px-2 py-1.5 align-top whitespace-nowrap'>%s"
                 "<td data-l=repo%s class='%s px-2 py-1.5 align-top whitespace-nowrap'>%s"
                 "<td data-l=owner%s class='%s %s px-2 py-1.5 align-top whitespace-nowrap'>%s"
                 "<td data-l=title class='px-2 py-1.5 align-top font-medium "
                 "[overflow-wrap:anywhere]'>%s" % (
                     escape(t["id"]), mins,
                     LINK, MONO,
                     escape(t["id"]), escape(t["id"]),
                     DIM, BADGE.get("done", "badge-ghost"), "done",
                     pr_e, DIM, MONO, pr_v,
                     rp_e, DIM, rp_v,
                     ow_e, DIM, MONO, ow_v,
                     escape(t["title"] or "")))
    h.append("</tbody></table></div>")
    return "".join(h)


def q_card(q, answer=None):
    """The question card is the ONLY surface on the board that is a human's job, and that
    is why it is the only one that gets the accent edge. The edge carries the meaning; it
    is not decoration."""
    return ("<div class='rounded-box border border-base-300 border-l-4 border-l-warning "
            "bg-base-200 p-4'>"
            "<h3 class='mb-2 text-base font-semibold'><a class='%s %s' href='/q/%s'>%s</a>%s</h3>"
            "<dl class='mb-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-sm'>"
            "<dt class='%s'>kind<dd>%s<dt class='%s'>task<dd class='%s'>%s%s</dl>"
            "<p class='max-w-[68ch]'>%s</p></div>" % (
                LINK, MONO, escape(q["id"]), escape(q["id"]),
                " <span class='badge badge-sm badge-warning'>board answered</span>"
                if answer is not None else "",
                DIM, escape(q["kind"]), DIM, MONO, escape(q["task"] or "—"),
                ("<dt class='%s'>board's answer<dd>%s" % (DIM, escape(answer)))
                if answer is not None else "",
                escape(q["text"] or "")))


def html_status(project, token="", human=False):
    s = status(project)
    h = [head("board", "<span class='%s text-xs'>%s</span>" % (
        MONO + " " + DIM, escape(s["generated"][11:16] + " UTC")),
        (), human, show_all_btn=True)]
    if s.get("ntfy_failures_since_success", 0) > 0:
        fails = s["ntfy_failures_since_success"]
        h.append("<div class='mt-4 rounded-box border border-l-4 border-base-300 "
                 "border-l-error bg-base-200 p-3 text-sm'>"
                 "<b class='font-semibold text-error'>ntfy push failed:</b> %d %s failed since last success. "
                 "Check notification server / ntfy settings.</div>" % (
                     fails, "push" if fails == 1 else "pushes"))
    if not s["projects"]:
        h.append("<div class='mt-8 rounded-box border border-dashed border-base-300 "
                 "px-4 py-10 text-center %s'>No projects yet.<br>"
                 "Run <code class='rounded bg-base-200 px-1 font-mono'>board project init</code> "
                 "in a project to put it here.</div>" % DIM)
        return page("board", "".join(h))
    h.append(watchline(s))
    h.append(fleet_runway(s))
    h.append("<div class='mt-8'>")
    for p in sorted(s["projects"], key=last_activity, reverse=True):
        pause = ("<span class='badge badge-sm badge-error'>paused: %s</span>"
                 % escape(p["paused"])) if p.get("paused") else ""
        pause_btn = ""
        if human:
            if p.get("paused"):
                pause_btn = ("<form method='POST' action='/projects/%s/resume' class='inline m-0'>"
                             "<input type='hidden' name='project' value='%s'>"
                             "<button type='submit' class='badge badge-sm badge-success cursor-pointer'>▶ gjenoppta</button></form>"
                             % (escape(p["name"]), escape(p["name"])))
            else:
                pause_btn = ("<form method='POST' action='/projects/%s/pause' class='inline m-0' data-confirm='Pause prosjektet (draining)?'>"
                             "<input type='hidden' name='project' value='%s'>"
                             "<button type='submit' class='badge badge-sm badge-ghost cursor-pointer' title='Pause prosjekt'>⏸ pause</button></form>"
                             % (escape(p["name"]), escape(p["name"])))
        h.append("<details class='pj border-t-2 border-base-300 last-of-type:border-b-2' "
                 "data-p='%s'><summary class='grid cursor-pointer list-none "
                 "grid-cols-[1.2ch_auto_auto_minmax(0,1fr)] items-baseline gap-x-2 gap-y-1 "
                 "rounded px-1 py-3 max-sm:grid-cols-[1.2ch_minmax(0,1fr)_auto] "
                 "[&::-webkit-details-marker]:hidden'>"
                 "<span class='mark %s select-none' aria-hidden=true></span>"
                 "<h2 class='text-base font-semibold'>%s</h2>"
                 "<span class='badge badge-sm badge-ghost'>%s</span>"
                 "<span class='max-sm:col-start-2 max-sm:col-end-[-1] "
                 "max-sm:justify-start'>%s%s%s</span></summary>" % (
                     escape(p["name"]), DIM, escape(p["name"]), escape(p["phase"]),
                     tell(p), (" " + pause) if pause else "", (" " + pause_btn) if pause_btn else ""))
        h.append("<div class='pb-4'>")
        if p.get("goal"):
            h.append("<p class='max-w-[68ch] %s'>%s</p>" % (DIM, escape(p["goal"])))
        if (p.get("budget") or {}).get("ceilings_missing"):
            h.append("<p class='mt-2 rounded-box border border-l-4 border-base-300 "
                     "border-l-error bg-base-200 p-3 text-sm'>%s</p>"
                     % escape(p["budget"]["note"]))
        ceilings = {win_name(k): float(v or 0)
                    for k, v in ((p.get("budget") or {}).get("ceilings") or {}).items()}
        h.append("<div class='pane mt-2'><section class='min-w-0'>")
        h.append("<div class='mt-6 mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs'>"
                 "<div class='flex items-center gap-1'>"
                 "<p class='text-sm text-base-content/60 m-0'>agents</p>"
                 "<span data-agent-badge class='text-xs text-base-content/60'></span></div>"
                 "<div class='ml-auto flex items-center gap-1'>"
                 "<select data-agent-filter class='rounded border border-base-300 bg-base-100 px-2 py-1 text-xs font-sans cursor-pointer' data-project='%s'>"
                 "<option value='active'>Kun aktive</option>"
                 "<option value='1h'>&lt; 1 time</option>"
                 "<option value='24h' selected>&lt; 24 timer</option>"
                 "<option value='7d'>&lt; 7 dager</option>"
                 "<option value='all'>Vis alle</option></select>"
                 "<form method='POST' action='/agents/cleanup' class='m-0 flex items-center' data-confirm='Rydd opp døde agenter?'>"
                 "<input type='hidden' name='project' value='%s'>"
                 "<input type='hidden' name='older_than' value='24h' data-agent-cleanup>"
                 "<button type='submit' class='badge badge-sm badge-error cursor-pointer' title='Merk døde agenter som ferdige'>rydd opp</button>"
                 "</form></div></div>" % (escape(p["name"]), escape(p["name"])))
        if not p["agents"]:
            h.append("<p class='%s text-sm'>No agents are registered here yet.</p>" % DIM)
        for a in p["agents"]:
            h.append(agent_block(a, ceilings, human=human, project=p["name"]))
        h.append("</section><section class='min-w-0'>")
        h.append("<div class='mt-6 mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs'>"
                 "<div class='flex items-center gap-1'>"
                 "<p class='text-sm text-base-content/60 m-0'>tasks</p>"
                 "<span data-task-badge class='text-xs text-base-content/60'></span></div>"
                 "<div class='ml-auto flex items-center gap-1'>"
                 "<select data-task-filter class='rounded border border-base-300 bg-base-100 px-2 py-1 text-xs font-sans cursor-pointer' data-project='%s'>"
                 "<option value='active' selected>Aktive &amp; åpne</option>"
                 "<option value='in_flight'>I arbeid</option>"
                 "<option value='24h'>Endret &lt; 24t</option>"
                 "<option value='7d'>Endret &lt; 7d</option>"
                 "<option value='all'>Vis alle</option></select>"
                 "<form method='POST' action='/tasks/cleanup' class='m-0 flex items-center' data-confirm='Arkiver fullførte oppgaver?'>"
                 "<input type='hidden' name='project' value='%s'>"
                 "<button type='submit' class='badge badge-sm badge-ghost cursor-pointer' title='Arkiver fullførte oppgaver'>arkiver ferdige</button>"
                 "</form></div></div>" % (escape(p["name"]), escape(p["name"])))
        if not p["tasks"]:
            h.append("<p class='%s text-sm'>The queue is empty.</p>" % DIM)
        else:
            h.append(task_rows(p))
        h.append("</section></div>")
        # The questions sit OUTSIDE the panel, at full width: this is the human's own
        # surface, and it must not be squeezed into the right column with empty space
        # beside it.
        if p["questions"]:
            h.append("<p class='%s'>waiting for an answer from you</p>" % LBL)
            h.append("<div class=qgrid>%s</div>"
                     % "".join(q_card(q) for q in p["questions"]))
        if p["questions_defaulted"]:
            h.append("<p class='%s'>the board answered itself — can be overridden</p>" % LBL)
            h.append("<div class=qgrid>%s</div>"
                     % "".join(q_card(q, q["answer"] or "") for q in p["questions_defaulted"]))
        h.append("</div></details>")
    h.append("</div>")
    h.append(FOCUS)
    return page("board", "".join(h))


def html_task(tid, token="", human=False):
    d = task_show(tid)
    st = d.get("status") or ""
    owner = d.get("owner") or "—"
    ost = d.get("owner_status")
    owner_h = "<span class='%s'>%s</span>%s" % (
        MONO, escape(owner),
        " <span class='%s'>(%s)</span>" % (DIM, escape(ost)) if ost else "")
    rev = "—"
    if d.get("review_open") is not None or d.get("review_fixed") is not None:
        rev = "%s open / %s fixed" % (d.get("review_open") or 0, d.get("review_fixed") or 0)
    # status already stands as a badge in the heading, and must not be repeated here.
    facts = (("phase", escape(d.get("phase") or "—")),
             ("risk", escape(d.get("risk") or "—")),
             ("owner", owner_h),
             ("repo", escape(d.get("repo") or "—")),
             ("branch", "<span class='%s'>%s</span>" % (MONO, escape(d.get("branch") or "—"))),
             ("PR", pr_html(d.get("pr"))),
             ("review", escape(rev)),
             ("merge", "<span class='%s'>%s</span>" % (
                 MONO, escape(d["merge_sha"]) if d.get("merge_sha") else "—")),
             ("worktree", "<span class='%s [overflow-wrap:anywhere]'>%s</span>" % (
                 MONO, escape(d.get("worktree") or "—"))))
    evs, day = [], None
    for e in d.get("events") or []:
        ts = e["ts"] or ""
        if ts[:10] != day:
            day = ts[:10]
            evs.append("<li class='day'><span class='%s %s text-xs'>%s</span></li>" % (
                MONO, DIM, escape(day)))
        evs.append(
            "<li><span class='%s %s text-xs'>%s</span>"
            "<span class='max-w-[68ch]'><code class='rounded bg-base-200 px-1 font-mono "
            "text-xs'>%s</code> <span class='%s'>%s</span>%s</span></li>" % (
                MONO, DIM, escape(ts[11:19]), escape(e["type"]), MONO,
                escape(e["actor"] or ""),
                (" — %s" % escape(e["note"])) if e.get("note") else ""))
    tl = ("<ul class='trail mt-1 list-none border-b border-base-300 p-0 text-sm'>%s</ul>"
          % "".join(evs) if evs else
          "<p class='%s text-sm'>No events yet.</p>" % DIM)
    oa = d.get("owner_agent")
    _ceil = {win_name(k): float(v or 0)
             for k, v in budget(d["project"]).get("ceilings", {}).items()}
    if oa:
        ab = ("<div class='border-t border-base-300 py-2.5'>"
              "<div class='flex flex-wrap items-baseline gap-x-2'>"
              "<b class='%s font-semibold'>%s</b><span class='%s text-xs'>%s</span>"
              "<span class='badge badge-sm %s'>%s</span></div>"
              "<div class='mt-1.5 grid gap-1'>%s%s</div>"
              "<p class='%s mt-1.5 text-xs %s'>last heartbeat %s</p></div>" % (
                  MONO, escape(d.get("owner") or ""), DIM, escape(oa.get("model") or ""),
                  "badge-error" if oa.get("status") == "dead" else "badge-ghost",
                  escape(oa.get("status") or ""),
                  meter("ctx", oa.get("ctx_pct"), 80),
                  # the red line is the project's ceiling for EXACTLY that window — a
                  # fixed 85 here was the hardcoded threshold T-164 removes everywhere else
                  "".join(meter(w, pct, _ceil.get(win_name(w)))
                          for w, pct in sorted((oa.get("budget") or {}).items())),
                  MONO, DIM, escape(oa.get("last_seen") or "—")))
    else:
        ab = "<p class='%s text-sm'>No owner.</p>" % DIM
    return page(d["id"], """%s
        <h2 class='mt-5 max-w-[68ch] text-lg font-semibold leading-snug sm:text-2xl'>%s</h2>
        <dl class='mt-4 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 border-t
          border-base-300 pt-3 text-sm sm:grid-cols-[auto_minmax(0,1fr)_auto_minmax(0,1fr)]'>%s</dl>
        <p class='%s'>timeline</p>%s
        <p class='%s'>the owner's state</p>%s
        <form class='mt-6 max-w-[42rem]' method=post action='/t/%s/comment'>
          <label class='%s' for=comment>Add a comment</label>
          <input id=comment class='input w-full' type=text name=text
            placeholder='what you saw, or what the agent should do next'>
          <button class='btn btn-primary mt-3 min-h-12'>Comment</button>
        </form>""" % (
        head(d["id"], "<span class='badge badge-sm %s'>%s</span>" % (
            BADGE.get(st, "badge-ghost"), escape(st)), human=human),
        escape(d.get("title") or ""),
        "".join("<dt class='%s'>%s<dd class='m-0 [overflow-wrap:anywhere]'>%s" % (DIM, k, v)
                for k, v in facts),
        LBL, tl, LBL, ab, escape(d["id"]), LBL))


def html_question(qid, token="", human=False):
    q = db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if not q:
        raise Err(404, "unknown question")
    if q["status"] not in ("open", "defaulted"):
        return page(qid, head(qid, human=human) + (
            "<div class='mt-5 max-w-[42rem] rounded-box border border-base-300 bg-base-200 p-4'>"
            "<p class='text-lg'>Answered: <b class='font-semibold'>%s</b></p>"
            "<p class='%s mt-1 text-sm'>%s</p></div>" % (
                escape(q["answer"] or ""), DIM, escape(q["status"]))))
    opts = "".join("<button class='btn btn-primary min-h-12 flex-1' name=answer value='%s'>%s"
                   "</button>" % (escape(o), escape(o)) for o in jl(q["options"]))
    # defaulted = the board answered itself when the deadline ran out — but a human can
    # still answer and override it, so the form must be shown here too (T-197).
    defaulted = q["status"] == "defaulted"
    # The deadline note is written in the future tense ("if you do not answer in time")
    # and is plainly wrong on a defaulted question: the deadline HAS passed, and the note
    # above already says what the board answered. The page must say one thing, not two.
    foot = ("<p class='mt-3 text-sm text-warning'>The deadline ran out — the board "
            "answered \u201c%s\u201d. If you answer now, that overrides it.</p>"
            % escape(q["answer"] or "")) if defaulted else (
        "<p class='%s mt-3 text-sm'>If you do not answer before the deadline, the agent "
        "carries on with: %s</p>" % (DIM, escape(q["default_answer"] or "—")))
    return page(qid, """%s
        <div class='mt-5 max-w-[42rem] rounded-box border border-base-300 border-l-4
          border-l-warning bg-base-200 p-4'>
          <p class='max-w-[68ch] text-lg leading-snug'>%s</p>
          <form class='mt-4' method=post action='/q/%s/answer'>
            <div class='flex flex-wrap gap-3'>%s</div>
            <label class='%s' for=freetext>Or answer in your own words</label>
            <input id=freetext class='input w-full' type=text name=answer
              placeholder='your answer'>
            <button class='btn btn-outline mt-3 min-h-12'>Send answer</button>
          </form>%s</div>""" % (
        # head() escapes on its own. This was the only call site that pre-escaped, and a
        # project name with & or < was then double-escaped and shown as "A&amp;amp;B" in
        # the browser. Project names are not validated, so it really can happen.
        head(q["project"], "<span class='%s %s text-xs'>%s</span>" % (
            MONO, DIM, escape(q["task"] or "")), human=human),
        escape(q["text"] or ""), qid, opts, LBL, foot))


# ---------- routes --------------------------------------------------------

API = "/api/v1"
ROUTES = [
    ("POST",   r"/agents$",                     lambda h, m, b, q: register(b)),
    ("POST",   r"/agents/([^/]+)/heartbeat$",   lambda h, m, b, q: heartbeat(m[0], b)),
    ("PUT",    r"/agents/([^/]+)/capabilities$",lambda h, m, b, q: set_caps(m[0], b)),
    ("PUT",    r"/agents/([^/]+)/preference$",  lambda h, m, b, q: set_pref(m[0], b)),
    ("GET",    r"/agents/([^/]+)/grants$",      lambda h, m, b, q: agent_grants_get(m[0], q)),
    ("POST",   r"/agents/([^/]+)/grants$",      lambda h, m, b, q: agent_grant_add(m[0], b)),
    ("POST",   r"/agents/([^/]+)/grants/revoke$", lambda h, m, b, q: agent_grant_del(m[0], (b or {}).get("grant"), b, q)),
    ("DELETE", r"/agents/([^/]+)/grants/([^/]+)$", lambda h, m, b, q: agent_grant_del(m[0], m[1], b, q)),
    ("POST",   r"/agents/cleanup$",             lambda h, m, b, q: agents_cleanup(
        (b or {}).get("project") or q.get("project", [None])[0],
        (b or {}).get("older_than") or q.get("older_than", ["24h"])[0], b)),
    ("POST",   r"/agents/([^/]+)/finished$",    lambda h, m, b, q: finished(m[0], b)),
    ("GET",    r"/agents/([^/]+)/inbox$",       lambda h, m, b, q: inbox(m[0], q)),
    ("POST",   r"/projects$",                   lambda h, m, b, q: dict(ensure_project(
        b["project"], b.get("phase"), b.get("goal"), b.get("manifest"), b.get("host"), b.get("path")))),
    ("POST",   r"/tasks$",                      lambda h, m, b, q: task_create(b, actor(q, b))),
    ("GET",    r"/tasks/next$",                 lambda h, m, b, q: task_next(q["agent"][0], q)),
        ("GET",    r"/routines$",                   lambda h, m, b, q: routine_list(q)),
    ("GET",    r"/tasks$",                      lambda h, m, b, q: task_list(q)),
    ("GET",    r"/tasks/([^/]+)$",              lambda h, m, b, q: task_show(m[0])),
    ("PATCH",  r"/tasks/([^/]+)$",              lambda h, m, b, q: task_patch(m[0], actor(q, b), b)),
    ("POST",   r"/tasks/([^/]+)/claim$",        lambda h, m, b, q: task_claim(m[0], actor(q, b))),
    ("POST",   r"/tasks/([^/]+)/progress$",     lambda h, m, b, q: task_progress(m[0], actor(q, b), b)),
    ("POST",   r"/tasks/([^/]+)/blocked$",      lambda h, m, b, q: task_blocked(m[0], actor(q, b), b)),
    ("POST",   r"/tasks/([^/]+)/review$",       lambda h, m, b, q: task_review(m[0], actor(q, b), b)),
    ("GET",    r"/tasks/([^/]+)/gate/merge$",   lambda h, m, b, q: gate_merge(m[0], q.get("agent", [None])[0])),
    ("POST",   r"/tasks/([^/]+)/merge_requested$", lambda h, m, b, q: task_merge_requested(m[0], actor(q, b), b)),
    ("POST",   r"/tasks/([^/]+)/merge_verified$",  lambda h, m, b, q: task_merge_verified(m[0], actor(q, b), b)),
    ("POST",   r"/tasks/([^/]+)/comment$",     lambda h, m, b, q: task_comment(m[0], b)),
    ("POST",   r"/tasks/([^/]+)/deployed$",   lambda h, m, b, q: task_deployed(m[0], actor(q, b), b)),
    ("POST",   r"/tasks/([^/]+)/done$",         lambda h, m, b, q: task_done(m[0], actor(q, b), b)),
    ("POST",   r"/tasks/([^/]+)/archive$",      lambda h, m, b, q: task_archive(m[0], actor(q, b), b)),
    ("POST",   r"/tasks/cleanup$",              lambda h, m, b, q: tasks_cleanup(
        (b or {}).get("project") or q.get("project", [None])[0],
        (b or {}).get("older_than") or q.get("older_than", ["0"])[0], b)),
    ("POST",   r"/tasks/([^/]+)/release$",      lambda h, m, b, q: release(m[0], actor(q, b), b)),
    ("POST",   r"/questions$",                  lambda h, m, b, q: question_create(b, actor(q, b))),
    ("GET",    r"/questions$",                  lambda h, m, b, q: {"questions": [
        # 'defaulted' is still overridable by a human — it must not disappear from this
        # list. .status tells the two apart.
        dict(r) for r in db.execute("SELECT * FROM questions WHERE status IN ('open','defaulted')")]}),
    ("POST",   r"/questions/([^/]+)/answer$",   lambda h, m, b, q: question_answer(
        m[0], b.get("answer", ""), b.get("by", HUMAN), b.get("note", ""), b)),
    ("POST",   r"/messages$",                   lambda h, m, b, q: message_send(b, actor(q, b))),
    ("GET",    r"/roles/recommendation$",       lambda h, m, b, q: role_recommendation(
        q["project"][0], q.get("role", ["coordinator"])[0])),
    ("PUT",    r"/roles/([^/]+)$",              lambda h, m, b, q: role_pin(
        b["project"], m[0], b["agent"], HUMAN, b)),
    ("DELETE", r"/roles/([^/]+)$",              lambda h, m, b, q: role_unpin(
        q["project"][0], m[0], b)),
    ("POST",   r"/roles/([^/]+)/claim$",        lambda h, m, b, q: role_claim(
        b["project"], m[0], actor(q, b))),
    # Same job as the minute tick, just on demand — otherwise the reaper is untestable.
    ("POST",   r"/reap$",                       lambda h, m, b, q: reap()),
    ("POST",   r"/projects/([^/]+)/pause$",    lambda h, m, b, q: set_paused(m[0], b, True)),
    ("POST",   r"/projects/([^/]+)/resume$",   lambda h, m, b, q: set_paused(m[0], b, False)),
    ("GET",    r"/status$",                     lambda h, m, b, q: status(q.get("project", [None])[0])),
    ("GET",    r"/events$",                     lambda h, m, b, q: events(q)),
]
ROUTES = [(mth, re.compile("^" + API + pat), fn) for mth, pat, fn in ROUTES]


def actor(q, b):
    a = (b or {}).get("agent") or q.get("agent", [None])[0]
    if not a:
        raise Err(400, "agent is missing (set BOARD_AGENT_ID)")
    return a


def as_human(b):
    """Is this call proven to come from the human? Only if it carried the human token."""
    return bool(b.get("_human"))


def human_only(b, what):
    if not as_human(b):
        raise Err(403, "%s requires the human token (BOARD_HUMAN_TOKEN). An agent cannot "
                       "act as %s — that is the entire point of §3.7." % (what, HUMAN),
                  needs_human_token=True)


def set_paused(project, b, on):
    ensure_project(project)
    who = b.get("by") or b.get("agent") or HUMAN
    val = ("%s: %s" % (who, b.get("note") or "paused")) if on else None
    db.execute("UPDATE projects SET paused=?, updated=? WHERE name=?", (val, now(), project))
    ev(project, "project", "project.paused" if on else "project.resumed", who, note=b.get("note"))
    if on:
        ntfy("⏸ %s paused" % project, b.get("note") or "the agents are draining",
             "%s/status" % BASE_URL)
    return {"project": project, "paused": val}


def set_caps(aid, b):
    a = agent(aid)
    have = set(jl(a["capabilities"])) | set(b.get("add", []))
    have -= set(b.get("remove", []))
    if b.get("set") is not None:
        have = set(b["set"])
    db.execute("UPDATE agents SET capabilities=? WHERE id=?", (json.dumps(sorted(have)), aid))
    ev(a["current_project"], "agent/" + aid, "agent.capabilities_set", aid, capabilities=sorted(have))
    return {"capabilities": sorted(have)}


def set_pref(aid, b):
    agent(aid)
    db.execute("UPDATE agents SET preference=? WHERE id=?", (b.get("preference"), aid))
    return {"preference": b.get("preference")}


def events(q):
    since = q.get("since", ["0"])[0]
    if since.isdigit():
        where, args = "id > ?", [int(since)]
    elif since[-1:] in DUR and since[:-1].isdigit():   # 2h / 30m / 1d — what USAGE.md promises
        where, args = "ts >= ?", [plus(-int(since[:-1]) * DUR[since[-1]])]
    else:
        where, args = "ts >= ?", [since]
    if q.get("project"):
        where, args = where + " AND project=?", args + [q["project"][0]]
    if q.get("stream"):
        where, args = where + " AND stream=?", args + [q["stream"][0]]
    # The cap must be taken from the END. `ORDER BY id LIMIT 500` gave the 500 OLDEST
    # hits, so `board tail --since 2h` on a busy board showed a two-hour-old log and never
    # what just happened — worthless for the one thing the command exists for.
    rows = list(db.execute("SELECT * FROM (SELECT * FROM events WHERE %s ORDER BY id DESC "
                           "LIMIT 501) ORDER BY id" % where, args))
    out = {"truncated": len(rows) > 500, "events":
           [dict(r, body=jl(r["body"], {})) for r in rows[-500:]]}
    # If the answer is truncated, something is missing in the MIDDLE for whoever follows
    # the log from an id. Say so, and say where the rest has to be fetched from.
    if out["truncated"]:
        out["from_id"] = out["events"][0]["id"]
    return out


def prom_esc(s):
    if s is None:
        return ""
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def prometheus_metrics():
    out = []

    def add_metric(name, mtype, samples):
        if samples:
            out.append("# TYPE %s %s" % (name, mtype))
            out.extend(samples)

    # 1. board_up 1
    add_metric("board_up", "gauge", ["board_up 1"])

    # 2. board_tasks_total{project="...",repo="...",status="..."} (gauge for nåværende tasks)
    tasks_rows = db.execute(
        "SELECT COALESCE(project, '') AS project, COALESCE(repo, '') AS repo, "
        "COALESCE(status, '') AS status, COUNT(*) AS cnt "
        "FROM tasks GROUP BY project, repo, status"
    ).fetchall()
    tasks_samples = [
        'board_tasks_total{project="%s",repo="%s",status="%s"} %d' % (
            prom_esc(r["project"]), prom_esc(r["repo"]), prom_esc(r["status"]), r["cnt"])
        for r in tasks_rows
    ]
    add_metric("board_tasks_total", "gauge", tasks_samples)
    rt_total = {}
    rt_overdue = []
    rt_last_run = []
    rt_deadline = []
    
    for r in db.execute("SELECT * FROM routines").fetchall():
        p = r["project"].replace('"', '\\"')
        st = r["status"].replace('"', '\\"')
        rt = r["name"].replace('"', '\\"')
        
        rt_total[(p, st)] = rt_total.get((p, st), 0) + 1
        
        open_task = db.execute("SELECT id FROM tasks WHERE project=? AND routine=? AND status NOT IN ('done','archived','orphaned')", (r["project"], r["name"])).fetchone()
        is_overdue = 0
        if r["next_due"] and mins_since(r["next_due"]) > 0 and not open_task:
            is_overdue = 1
        
        # deadline check
        if r["deadline"]:
            d_interval = parse_interval(r["deadline"])
            if d_interval and r["next_due"]:
                deadline_ts = (datetime.fromisoformat(r["next_due"].replace("Z", "+00:00")) + d_interval)
                deadline_ts_iso = deadline_ts.isoformat().replace("+00:00", "Z")
                if mins_since(deadline_ts_iso) > 0 and not open_task:
                    is_overdue = 1
                rt_deadline.append('board_routine_deadline_timestamp_seconds{project="%s",routine="%s"} %f' % (p, rt, deadline_ts.timestamp()))
            elif d_interval and r["last_run"]:
                # fallback if next_due is none? just use next_due logic
                pass
        
        rt_overdue.append('board_routine_overdue{project="%s",routine="%s"} %d' % (p, rt, is_overdue))
        
        if r["last_run"]:
            ts_sec = ts(r["last_run"]).timestamp()
            rt_last_run.append('board_routine_last_run_timestamp_seconds{project="%s",routine="%s"} %f' % (p, rt, ts_sec))
            
        if r["next_due"]:
            ts_sec = ts(r["next_due"]).timestamp()
            rt_deadline.append('board_routine_deadline_timestamp_seconds{project="%s",routine="%s"} %f' % (p, rt, ts_sec))
            
    add_metric("board_routines_total", "gauge", [
        'board_routines_total{project="%s",status="%s"} %d' % (p, st, cnt)
        for (p, st), cnt in rt_total.items()
    ])
    add_metric("board_routine_overdue", "gauge", rt_overdue)
    add_metric("board_routine_last_run_timestamp_seconds", "gauge", rt_last_run)
    add_metric("board_routine_deadline_timestamp_seconds", "gauge", rt_deadline)



    # Event counters for task lifecycle:
    # 3. board_tasks_created_total
    # 4. board_tasks_completed_total
    # 5. board_tasks_blocked_total
    # 6. board_task_review_rounds_total
    # 7. board_task_merges_total
    task_counters = [
        ("task.created", "board_tasks_created_total"),
        ("task.done", "board_tasks_completed_total"),
        ("task.blocked", "board_tasks_blocked_total"),
        ("task.review_result", "board_task_review_rounds_total"),
        ("task.merge_verified", "board_task_merges_total"),
    ]
    for ev_type, metric_name in task_counters:
        rows = db.execute(
            "SELECT e.project, COALESCE(t.repo, json_extract(e.body, '$.repo'), '') AS repo_val, COUNT(*) AS cnt "
            "FROM events e LEFT JOIN tasks t ON e.stream = ('task/' || t.id) "
            "WHERE e.type = ? "
            "GROUP BY e.project, repo_val", (ev_type,)
        ).fetchall()
        samples = [
            '%s{project="%s",repo="%s"} %d' % (
                metric_name, prom_esc(r["project"]), prom_esc(r["repo_val"]), r["cnt"])
            for r in rows
        ]
        add_metric(metric_name, "counter", samples)

    # 8. board_task_dispatches_total{project="...",role="...",model="..."}
    # 9. board_task_dispatch_tokens_total{project="...",role="...",model="..."}
    dispatches = {}
    dispatch_tokens = {}
    for r in db.execute("SELECT project, body FROM events WHERE json_extract(body, '$.dispatch') IS NOT NULL").fetchall():
        b = jl(r["body"], {})
        d = b.get("dispatch")
        if not d:
            continue
        if isinstance(d, dict):
            role = d.get("role") or ""
            model = d.get("model") or ""
            tokens = d.get("tokens")
        elif isinstance(d, str) and ":" in d:
            role, _, model = d.partition(":")
            tokens = None
        else:
            continue
        key = (r["project"] or "", str(role), str(model))
        dispatches[key] = dispatches.get(key, 0) + 1
        if tokens is not None:
            try:
                dispatch_tokens[key] = dispatch_tokens.get(key, 0) + int(tokens)
            except (ValueError, TypeError):
                pass

    disp_samples = [
        'board_task_dispatches_total{project="%s",role="%s",model="%s"} %d' % (
            prom_esc(proj), prom_esc(role), prom_esc(model), cnt)
        for (proj, role, model), cnt in sorted(dispatches.items())
    ]
    add_metric("board_task_dispatches_total", "counter", disp_samples)

    token_samples = [
        'board_task_dispatch_tokens_total{project="%s",role="%s",model="%s"} %d' % (
            prom_esc(proj), prom_esc(role), prom_esc(model), tok_cnt)
        for (proj, role, model), tok_cnt in sorted(dispatch_tokens.items())
    ]
    add_metric("board_task_dispatch_tokens_total", "counter", token_samples)

    # 10. board_agents_total{project="...",status="..."} (gauge for agenter)
    agent_rows = db.execute(
        "SELECT COALESCE(current_project, '') AS project, COALESCE(status, '') AS status, COUNT(*) AS cnt "
        "FROM agents GROUP BY current_project, status"
    ).fetchall()
    agent_samples = [
        'board_agents_total{project="%s",status="%s"} %d' % (
            prom_esc(r["project"]), prom_esc(r["status"]), r["cnt"])
        for r in agent_rows
    ]
    add_metric("board_agents_total", "gauge", agent_samples)

    # 11. board_agent_ctx_percent{agent="...",harness="...",model="..."} (gauge fra agents.ctx_pct der status='alive')
    ctx_rows = db.execute(
        "SELECT id, COALESCE(harness, '') AS harness, COALESCE(model, '') AS model, ctx_pct "
        "FROM agents WHERE status = 'alive' AND ctx_pct IS NOT NULL"
    ).fetchall()
    ctx_samples = [
        'board_agent_ctx_percent{agent="%s",harness="%s",model="%s"} %s' % (
            prom_esc(r["id"]), prom_esc(r["harness"]), prom_esc(r["model"]), r["ctx_pct"])
        for r in ctx_rows
    ]
    add_metric("board_agent_ctx_percent", "gauge", ctx_samples)

    # 12. board_agent_budget_used_percent{agent="...",harness="...",window="..."} (gauge fra agents.budget json der status='alive')
    budget_rows = db.execute(
        "SELECT id, COALESCE(harness, '') AS harness, budget "
        "FROM agents WHERE status = 'alive' AND budget IS NOT NULL"
    ).fetchall()
    budget_samples = []
    for r in budget_rows:
        for w in jl(r["budget"], []):
            if isinstance(w, dict) and w.get("window") and w.get("used_pct") is not None:
                budget_samples.append(
                    'board_agent_budget_used_percent{agent="%s",harness="%s",window="%s"} %s' % (
                        prom_esc(r["id"]), prom_esc(r["harness"]), prom_esc(w["window"]), w["used_pct"]))
    add_metric("board_agent_budget_used_percent", "gauge", budget_samples)

    # 13. board_questions_total{project="...",kind="...",status="..."} (gauge)
    q_rows = db.execute(
        "SELECT COALESCE(project, '') AS project, COALESCE(kind, '') AS kind, "
        "COALESCE(status, '') AS status, COUNT(*) AS cnt "
        "FROM questions GROUP BY project, kind, status"
    ).fetchall()
    q_samples = [
        'board_questions_total{project="%s",kind="%s",status="%s"} %d' % (
            prom_esc(r["project"]), prom_esc(r["kind"]), prom_esc(r["status"]), r["cnt"])
        for r in q_rows
    ]
    add_metric("board_questions_total", "gauge", q_samples)

    # 14. board_events_total{project="...",type="..."} (counter fra events grupperer per type)
    ev_rows = db.execute(
        "SELECT COALESCE(project, '') AS project, COALESCE(type, '') AS type, COUNT(*) AS cnt "
        "FROM events GROUP BY project, type"
    ).fetchall()
    ev_samples = [
        'board_events_total{project="%s",type="%s"} %d' % (
            prom_esc(r["project"]), prom_esc(r["type"]), r["cnt"])
        for r in ev_rows
    ]
    add_metric("board_events_total", "counter", ev_samples)

    # 15. board_task_review_findings_total{project="...",repo="...",status="open|fixed"}
    findings_rows = db.execute(
        "SELECT COALESCE(project, '') AS project, COALESCE(repo, '') AS repo, "
        "SUM(COALESCE(review_open, 0)) AS s_open, SUM(COALESCE(review_fixed, 0)) AS s_fixed "
        "FROM tasks GROUP BY project, repo"
    ).fetchall()
    findings_samples = []
    for r in findings_rows:
        findings_samples.append('board_task_review_findings_total{project="%s",repo="%s",status="open"} %d' % (
            prom_esc(r["project"]), prom_esc(r["repo"]), r["s_open"]))
        findings_samples.append('board_task_review_findings_total{project="%s",repo="%s",status="fixed"} %d' % (
            prom_esc(r["project"]), prom_esc(r["repo"]), r["s_fixed"]))
    add_metric("board_task_review_findings_total", "counter", findings_samples)

    # 16. board_roles_active{project="...",role="...",agent="..."} (gauge)
    role_rows = db.execute(
        "SELECT COALESCE(project, '') AS project, COALESCE(role, '') AS role, COALESCE(agent, '') AS agent "
        "FROM roles"
    ).fetchall()
    role_samples = [
        'board_roles_active{project="%s",role="%s",agent="%s"} 1' % (
            prom_esc(r["project"]), prom_esc(r["role"]), prom_esc(r["agent"]))
        for r in role_rows
    ]
    add_metric("board_roles_active", "gauge", role_samples)

    # 17. board_tasks_by_risk_total{project="...",risk="..."} (gauge)
    risk_rows = db.execute(
        "SELECT COALESCE(project, '') AS project, COALESCE(risk, 'normal') AS risk, COUNT(*) AS cnt "
        "FROM tasks GROUP BY project, risk"
    ).fetchall()
    risk_samples = [
        'board_tasks_by_risk_total{project="%s",risk="%s"} %d' % (
            prom_esc(r["project"]), prom_esc(r["risk"]), r["cnt"])
        for r in risk_rows
    ]
    add_metric("board_tasks_by_risk_total", "gauge", risk_samples)

    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "board/0"

    def log_message(self, fmt, *a):
        # Simplification: the query string is stripped from the log — it can contain
        # ?t=<token>.
        print("%s %s" % (self.address_string(), (fmt % a).split("?")[0]), flush=True)

    def token_given(self, q):
        """Header, cookie and query only — never the body. The body is read only after
        auth."""
        if q.get("t"):
            return q["t"][0]
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        cookie = re.search(r"board_token=([^;]+)", self.headers.get("Cookie", "") or "")
        if cookie:
            return urllib.parse.unquote(cookie.group(1))
        return ""

    def authed(self, q):
        given = self.token_given(q)
        # The human token is a SUPERSET: it authenticates like every other token, and in
        # addition sets the flag that lets the call act as the human.
        self.is_human = bool(HUMAN_TOKEN) and hmac.compare_digest(given, HUMAN_TOKEN)
        if self.is_human:
            return True
        if not TOKEN:
            return True
        ok = hmac.compare_digest(given, TOKEN)
        if not ok:
            time.sleep(0.5)          # slows guessing; the board has no other users to starve
        return ok

    def send(self, code, obj, ctype="application/json", extra=()):
        data = (obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)).encode()
        self.send_response(code)
        for k, v in extra:
            self.send_header(k, v)
        if "charset=" not in ctype:
            ctype = ctype + "; charset=utf-8"
        self.send_header("Content-Type", ctype)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("X-Content-Type-Options", "nosniff")
        # style-src: 'self' for board.css. 'unsafe-inline' has to stay — the meter's
        # width and the ceiling line's position are DATA, and do not exist as a class you
        # can build ahead of time. No font-src, img-src or connect-src: default-src 'none'
        # covers them, and ui/build.sh fails if the stylesheet ever fetches anything over
        # the network.
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'self' 'unsafe-inline'; "
                         "script-src %s; form-action 'self'" % FOCUS_SHA)
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    MAX_BODY = 1 << 20                     # 1 MiB. Without a limit a 500 MB Content-Length is an OOM.

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > self.MAX_BODY:
            raise Err(413, "body over %d bytes" % self.MAX_BODY)
        raw = self.rfile.read(n) if n else b""
        if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
        return json.loads(raw) if raw else {}

    def handle_one(self, method):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/healthz":            # kubelet probe: no token, no data
            return self.send(200, {"ok": True})
        if url.path == "/metrics" and method == "GET":
            with LOCK:
                text = prometheus_metrics()
            return self.send(200, text, "text/plain; version=0.0.4; charset=utf-8")
        if not self.authed(q):                # BEFORE the body is read — an
            return self.send(401, {"error": "invalid token"})   # unauthenticated client
        try:                                                    # must not make us allocate
            body = self.body()
            if isinstance(body, dict):
                body["_human"] = getattr(self, "is_human", False)
        except Err as e:
            return self.send(e.code, e.body)
        except ValueError:
            return self.send(400, {"error": "invalid JSON"})
        # ?t=<token> in a link from ntfy: move it into an HttpOnly cookie and redirect to
        # a clean URL, so the token is not left in history or logs.
        if q.get("t") and method == "GET" and not url.path.startswith(API):
            clean = urllib.parse.urlencode({k: v[0] for k, v in q.items() if k != "t"})
            return self.send(302, "", "text/html", extra=[
                ("Location", url.path + ("?" + clean if clean else "")),
                # Lax, not Strict: a link opened from a QR scanner, ntfy or a messenger
                # has no initiator page, so Strict holds the cookie back on the redirect
                # and the phone gets a 401. Lax is sent on top-level GET navigation, but
                # still NEVER on cross-site POST — so the CSRF protection on the answer
                # form stands.
                ("Set-Cookie", "board_token=%s; HttpOnly; SameSite=Lax; Secure; Path=/; Max-Age=7776000"
                 % urllib.parse.quote(q["t"][0], safe=""))])
        # The rollback MUST happen under the same lock as the write. Outside it, it rolls
        # back another thread's in-flight transaction on the shared connection — and the
        # agent believes it owns a task the board considers free.
        try:
            with LOCK:
                try:
                    r = self.route(method, url.path, body, q, "")
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
        except Err as e:
            return self.send(e.code, e.body)
        except (KeyError, IndexError) as e:
            return self.send(400, {"error": "missing parameter %s" % e})
        except Exception as e:
            self.log_message("500 %r", e)
            return self.send(500, {"error": repr(e)})
        if r is None:
            return
        if isinstance(r, str):
            return self.send(200, r, "text/html")
        return self.send(200, r)

    def route(self, method, path, body, q, token):
        if path == "/healthz":
            return {"ok": True}
        if path == "/metrics" and method == "GET":
            return self.send(200, prometheus_metrics(), "text/plain; version=0.0.4; charset=utf-8")
        if path == CSS_URL and method == "GET":
            # The content hash is in the filename, so the response can be cached
            # forever: a deploy yields a new URL. That saves the phone 25 kB per page
            # view.
            return self.send(200, CSS_BYTES.decode("utf-8"), "text/css",
                             extra=[("Cache-Control", "public, max-age=31536000, immutable")])
        # Whether the cookie in this browser is the human's. Every HTML page says so, on
        # the surface where it matters — an agent-token session renders identically and
        # then refuses every answer (T-390).
        human = getattr(self, "is_human", False)
        if path in ("/", "/status"):
            return html_status(q.get("project", [None])[0], token, human)
        m = re.match(r"^/q/([^/]+)$", path)
        if m and method == "GET":
            return html_question(m.group(1), token, human)
        m = re.match(r"^/t/([^/]+)$", path)
        if m and method == "GET":
            return html_task(m.group(1), token, human)
        m = re.match(r"^/t/([^/]+)/comment$", path)
        if m and method == "POST":
            if not human:
                return self.send(403, not_human_page("commenting on %s" % m.group(1)),
                                 "text/html")
            task_comment(m.group(1), body)
            return self.send(302, "", "text/html", extra=[("Location", "/t/" + m.group(1))])
        if path == "/agents/cleanup" and method == "POST":
            proj = (body or {}).get("project")
            older = (body or {}).get("older_than", "24h")
            res = agents_cleanup(proj, older, body)
            if self.headers.get("accept") == "application/json":
                return self.send(200, res)
            loc = "/" if not proj else ("/status?project=" + urllib.parse.quote(proj))
            return self.send(302, "", "text/html", extra=[("Location", loc)])
        if path == "/tasks/cleanup" and method == "POST":
            proj = (body or {}).get("project")
            older = (body or {}).get("older_than", "0")
            res = tasks_cleanup(proj, older, body)
            if self.headers.get("accept") == "application/json":
                return self.send(200, res)
            loc = "/" if not proj else ("/status?project=" + urllib.parse.quote(proj))
            return self.send(302, "", "text/html", extra=[("Location", loc)])
        m = re.match(r"^/agents/([^/]+)/grants$", path)
        if m and method == "POST":
            proj = (body or {}).get("project")
            res = agent_grant_add(m.group(1), body)
            proj = proj or res.get("project")
            if self.headers.get("accept") == "application/json":
                return self.send(200, res)
            loc = "/" if not proj else ("/status?project=" + urllib.parse.quote(proj))
            return self.send(302, "", "text/html", extra=[("Location", loc)])
        m = re.match(r"^/agents/([^/]+)/grants/revoke$", path)
        if m and method == "POST":
            proj = (body or {}).get("project")
            grant = (body or {}).get("grant")
            res = agent_grant_del(m.group(1), grant, body, q)
            proj = proj or res.get("project")
            if self.headers.get("accept") == "application/json":
                return self.send(200, res)
            loc = "/" if not proj else ("/status?project=" + urllib.parse.quote(proj))
            return self.send(302, "", "text/html", extra=[("Location", loc)])
        m = re.match(r"^/projects/([^/]+)/pause$", path)
        if m and method == "POST":
            proj = m.group(1)
            res = set_paused(proj, body, True)
            if self.headers.get("accept") == "application/json":
                return self.send(200, res)
            loc = "/" if not proj else ("/status?project=" + urllib.parse.quote(proj))
            return self.send(302, "", "text/html", extra=[("Location", loc)])
        m = re.match(r"^/projects/([^/]+)/resume$", path)
        if m and method == "POST":
            proj = m.group(1)
            res = set_paused(proj, body, False)
            if self.headers.get("accept") == "application/json":
                return self.send(200, res)
            loc = "/" if not proj else ("/status?project=" + urllib.parse.quote(proj))
            return self.send(302, "", "text/html", extra=[("Location", loc)])
        m = re.match(r"^/q/([^/]+)/answer$", path)
        if m and method == "POST":
            ans = body.get("answer", "")
            if ans == "fail" and body.get("note"):
                ans = "fail: " + body["note"]
            # A form pressed by a person, so a refusal is a page, not a JSON blob on a
            # phone. Only this one shape — anything else is a real error and belongs in
            # the ordinary handler.
            if not human:
                return self.send(403, not_human_page("answering %s" % m.group(1)),
                                 "text/html")
            question_answer(m.group(1), ans, HUMAN, body.get("note", ""), body)
            return answer_saved_page(m.group(1), human)
        for mth, pat, fn in ROUTES:
            mm = pat.match(path)
            if mm and mth == method:
                return fn(self, mm.groups(), body, q)
        raise Err(404, "unknown route %s %s" % (method, path))

    def do_GET(self):
        self.handle_one("GET")

    def do_POST(self):
        self.handle_one("POST")

    def do_PUT(self):
        self.handle_one("PUT")

    def do_PATCH(self):
        self.handle_one("PATCH")

    def do_DELETE(self):
        self.handle_one("DELETE")


if __name__ == "__main__":
    threading.Thread(target=reaper_loop, daemon=True).start()
    print("board on :%d (db=%s, policy=%s)" % (PORT, DB_PATH, POLICY), flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
