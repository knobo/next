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
# T-278: silence is not the same failure as an expired lease. `stalled` (below) only
# catches a task sitting untouched for LEASE_MIN (hours); an agent can go quiet for a
# quarter hour with the findings sitting only in its own chat, and nothing on the board
# says so until the lease itself runs out. This is visibility, not enforcement — the
# reaper (Q-107) never reads it, only `status`/`status --me` and the agent's own nudge do.
SILENT_MIN = max(1, min(1440, int(os.environ.get("BOARD_SILENT_MIN", "15"))))
PHASES = ("idea", "build", "launch", "live")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, project TEXT NOT NULL, stream TEXT NOT NULL,
  type TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_stream ON events(project, stream, id);
CREATE TABLE IF NOT EXISTS projects (
  name TEXT PRIMARY KEY, phase TEXT NOT NULL, goal TEXT, manifest TEXT,
  manifest_host TEXT, manifest_path TEXT, updated TEXT);
-- projects.ceilings/.wip are the OWNER'S OVERRIDE of board-policy.json, set from the
-- board itself (the gauges on /status) instead of by hand-editing the ConfigMap on the
-- host. The policy file stays the default; this wins when it is set. The reason it is a
-- table and not the file: the file is deliberately never deployed (k8s/board.yaml), so
-- the only way to raise a ceiling at 3am was to ssh in — and a project the file has no
-- entry for stops the whole fleet with a note telling the owner to go edit YAML. Written
-- ONLY behind the human token (§3.7): a ceiling an agent can raise is not a ceiling.
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
                          ("grants", "source", "TEXT DEFAULT 'policy'"),
                          # The estimate is a RELATIVE size, not hours, and the point of
                          # keeping who/what estimated is that the board can then answer
                          # "which model estimates well" — the model on the agent row is
                          # not enough, because agents are cleaned up and a session can
                          # change model. Snapshot both at the moment the number is given.
                          ("tasks", "estimate", "INTEGER"),
                          ("tasks", "estimate_by", "TEXT"),
                          ("tasks", "estimate_model", "TEXT"),
                          ("projects", "ceilings", "TEXT"),
                          ("projects", "wip", "INTEGER")):

    try:                                # database from before the column existed
        db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (_tbl, _col, _decl))
    except sqlite3.OperationalError:
        pass
# Reading the log BY TYPE: the cost of a task is folded out of its task.progress events,
# and both /status and /metrics do it — /metrics under the global lock, where every agent
# call waits behind it. Measured against the live board's own database: /status 726 ms →
# 214 ms, /metrics 95 ms → 24 ms.
#
# OUT of SCHEMA and into a try, deliberately. An index is an optimisation: the board ran
# without this one for its whole life and merely scanned more rows. Inside executescript()
# it was load-bearing instead — the first deploy of it met a `disk I/O error` on the live
# volume, which I have not been able to reproduce anywhere since (same code against a copy
# of that database, same securityContext with no writable temp directory, a concurrent
# writer: all fine), and because the statement sat in the fatal block the board did not
# start at all. Four restarts, CrashLoopBackOff, the board 503 until it was rolled back.
# Whatever that I/O error was, the fault worth fixing is that a slower query was allowed
# to become an outage.
try:
    db.execute("CREATE INDEX IF NOT EXISTS events_type ON events(type, project, id)")
except sqlite3.Error as _e:
    # The EXTENDED code, not just "disk I/O error". SQLITE_IOERR is a family — _WRITE,
    # _FSYNC, _SHMOPEN, _GETTEMPPATH and a dozen more — and they have entirely different
    # causes. The plain message is what this failure has offered so far, and it is not
    # enough to act on. sqlite_errorcode/sqlite_errorname are Python 3.11+, hence getattr.
    print("could not create the events(type) index — the board runs, reading the log by "
          "type is slower: %s (%s/%s)" % (
              _e, getattr(_e, "sqlite_errorname", "?"),
              getattr(_e, "sqlite_errorcode", "?")), file=sys.stderr, flush=True)

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
    # The owner's own setting, made on the board, beats the file — PER WINDOW, merged
    # over it, never instead of it. Each gauge on /status is its own form carrying one
    # `ceiling.<window>`, so dragging one handle posts one window: replacing the whole
    # dict meant that raising 5h from a phone deleted the 7d ceiling and stopped every
    # agent in the project, with a note pointing at a YAML file that already had the
    # value. That is the precise gesture this feature exists for.
    #
    # `source` travels with the numbers so the gauges can say which one they are drawing —
    # an override that looks identical to the policy is how you spend an hour editing the
    # ConfigMap wondering why nothing moves — and `overridden` names the windows the owner
    # has taken over, because "set here" for the project as a whole would be a lie about
    # the rest of them.
    own = ceilings_override(project)
    merged = dict(c or {}, **own)
    if not merged:
        return {"ceilings": {}, "fallback": p.get("fallback", {}), "ceilings_missing": True,
                "source": "none", "overridden": [],
                "note": "No ceiling is set for %s, so every agent here stops. Set one on "
                        "the board (the gauges under the fleet), or add "
                        "budget.%s.ceilings to board-policy.json." % (project, project)}
    return {"ceilings": merged, "fallback": p.get("fallback", {}),
            "source": "board" if own else "policy", "overridden": sorted(own)}


def ceilings_override(project):
    """The ceilings the owner set on the board itself, or {} when they set none."""
    r = db.execute("SELECT ceilings FROM projects WHERE name=?", (project,)).fetchone()
    raw = jl(r["ceilings"], {}) if r else {}
    if not isinstance(raw, dict):
        return {}
    return {win_name(k): v for k, v in raw.items() if num(v) is not None}


WIN_UNIT = {"m": 1, "h": 60, "d": 1440, "w": 10080}


def win_name(name):
    return str(name).strip().lower()


def window_minutes(name):
    """"5h" → 300. The window name IS the length of the window, and the length is the
    filter the reading is remembered within (T-192). An unknown shape is remembered for
    seven days: too long is safe, too short is a silent quota blackout."""
    return known_minutes(name) or 7 * 24 * 60


def known_minutes(name):
    """Window length when the name states it ("5h" → 300), else None."""
    m = re.fullmatch(r"(\d+)\s*([mhdw])", win_name(name))
    return int(m.group(1)) * WIN_UNIT[m.group(2)] if m else None


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


def resets_of(a):
    """{name: minutes until reset} for the agent's windows that report `resets_at` (ISO
    text or epoch seconds — the statusline passes on whatever the harness gives)."""
    raw = a.get("budget") if isinstance(a, dict) else a["budget"]
    out = {}
    for w in (raw if isinstance(raw, list) else jl(raw)):
        if not isinstance(w, dict) or w.get("window") is None or w.get("resets_at") in (None, ""):
            continue
        t = reset_time(w["resets_at"])
        left = (t - datetime.now(timezone.utc)).total_seconds() / 60 if t else None
        # A reset already past is a stale reading from the previous cycle: no ramp.
        if left is not None and left > 0:
            out[win_name(w["window"])] = left
    return out


def reset_time(r):
    """`resets_at` (ISO text or epoch seconds) as an aware UTC datetime, or None when it is
    not a usable time. Naive ISO is read as UTC. Never raises: one bad heartbeat must not
    break /status or the stop rules for the fleet."""
    try:
        t = (datetime.fromtimestamp(num(r), timezone.utc) if num(r) is not None
             else ts(str(r)))
        if t is None:
            return None
        return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def effective_ceilings(a, ceilings):
    """«Use up the quota before reset» (T-428, assumes Q-249 default): over the last 25 %
    of a window's length before `resets_at` the ceiling rises linearly to 100 — quota left
    unspent at reset is lost anyway. A window with no `resets_at` keeps its fixed ceiling."""
    left = resets_of(a)
    out = {}
    for name, ceil in ceilings.items():
        c = float(ceil)
        if name in left and known_minutes(name):     # unknown length → no ramp
            progress = min(1.0, max(0.0, 1 - left[name] / (0.25 * known_minutes(name))))
            c += (100 - c) * progress
        out[name] = round(c, 1)
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
    # The ramp uses this agent's own resets_at against the account-wide max: same account
    # means same reset time, and a missing/past resets_at falls back to the fixed ceiling.
    mine, eff = windows_of(a), effective_ceilings(a, ceilings)
    for name in sorted(mine):
        pct = max(mine[name], acct.get(name, 0.0))
        ceil = eff.get(name)
        if ceil is None:
            return ("window %s has no ceiling in the policy — set budget.%s.ceilings.%s"
                    % (name, project, name))
        if pct >= ceil:
            return "%s: %g%% used of the %g%% ceiling (per account, all sessions)" % (name, pct, ceil)
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


def silent_min(a):
    """Minutes since the last task.claimed/task.progress event on the task this agent
    currently owns. None when it owns no task, or the task is not `claimed` — silence is
    only measurable, and only a concern, while the agent is actually meant to be working
    it. `blocked`/`awaiting_human` are documented waits (task_blocked/human.test_requested
    never clear current_task, so without this check a blocked agent reads as gone quiet);
    `in_review`/`merging`/`done`/`archived` have moved past the agent's own coding.
    Q-107: purely informational, the reaper never reads this."""
    tid = a["current_task"]
    if not tid:
        return None
    t = db.execute("SELECT project, status FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t or t["status"] != "claimed":
        return None
    # project=? lets this hit the events_stream(project, stream, id) index instead of a
    # full table scan per agent per /status render.
    r = db.execute(
        "SELECT MAX(ts) m FROM events WHERE project=? AND stream=? "
        "AND type IN ('task.claimed','task.progress')",
        (t["project"], "task/" + tid)).fetchone()
    return int(mins_since(r["m"])) if r and r["m"] else None


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
        for w in ws:                    # garbage resets_at is dropped here, not stored
            if w.get("resets_at") not in (None, "") and reset_time(w["resets_at"]) is None:
                del w["resets_at"]
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
    # An agent that stands down must not do it silently — UNLESS the reason is one of the
    # routine, expected exits: no reason at all (skill/SKILL.md:71 calls `board finished`
    # bare on queue-empty — the bug this replaces compared against the literal string
    # "queue empty", which no real caller ever sent), "queue empty" said explicitly
    # (skill/SKILL.md:71 now says so), or "session end" (hooks/session-end.sh's
    # SessionEnd hook, which fires on every clean exit on top of whatever explicit call
    # already happened, and carries no diagnostic content of its own). A fleet running
    # overnight ends every session one of these ways, and a push per drain teaches the
    # human to mute the channel — which buries the one push that matters. Any other
    # reason — including the quota-stop text from `.stop` (skill/SKILL.md:67), which
    # always names a window and a ceiling — still pushes, named in the title.
    reason = (b.get("reason") or "").strip()
    if reason.lower() not in ("", "queue empty", "session end"):
        note = budget(a["current_project"]).get("note")
        click = ("%s/t/%s" % (BASE_URL, a["current_task"]) if a["current_task"]
                 else "%s/status" % BASE_URL)
        ntfy("agent finished (%s): %s" % (reason, aid), "%s%s" % (
            a["current_project"] or "?", "\n" + note if note else ""), click)
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
    # Validated BEFORE the insert: a task that exists with a silently dropped estimate is
    # worse than one that was refused, because the number never comes back.
    est = estimate_fields(b.get("estimate"), actor)
    db.execute("""INSERT INTO tasks (id,project,repo,title,spec,status,requires,needs_grants,touches,
                  risk,review_open,created,updated,priority,routine,estimate,estimate_by,estimate_model)
                  VALUES (?,?,?,?,?,'open',?,?,?,?,NULL,?,?,?,?,?,?,?)""",
               (tid, project, b.get("repo"), b["title"], b.get("spec"),
                json.dumps(b.get("requires", [])), json.dumps(b.get("needs_grants", [])),
                json.dumps(b.get("touches", [])), b.get("risk", "normal"), now(), now(),
                int(b.get("priority", 50)), b.get("routine"),
                est["estimate"], est["estimate_by"], est["estimate_model"]))
    ev(project, "task/" + tid, "task.created", actor, title=b["title"], repo=b.get("repo"),
       risk=b.get("risk", "normal"), from_project=b.get("from_project"),
       estimate=est["estimate"])
    return {"id": tid, "status": "open"}


def dispatches_of(rows):
    """Who dispatched what on one task, oldest first. `rows` are that task's
    `task.progress` events in id order.

    SKILL.md asks the coordinator to log the same dispatch TWICE: once before the
    subagent starts, and once when it returns with --tokens. One event = one dispatch
    therefore counted every dispatch twice, and `complete` could never become true — the
    "before" row stands forever without a token count. The closing entry is folded into
    the open row for the same role:model instead. A NEW round with the same role:model
    begins with a new "before" row, and therefore counts as its own dispatch — that is
    where `ts` tells them apart.

    Lifted out of task_show because /metrics needs precisely the same fold, and a second
    implementation of it would be a second set of numbers that disagree with the page."""
    out = []
    for r in rows:
        body = jl(r["body"], {})
        x = body.get("dispatch")
        if not x:
            continue
        res = body.get("result")
        open_row = next((p for p in reversed(out)
                         if p["role"] == x["role"] and p["model"] == x["model"]
                         and p["tokens"] is None), None)
        if open_row is not None and (x.get("tokens") is not None or res is not None):
            open_row["tokens"] = x.get("tokens")
            open_row["result"] = res if res is not None else open_row["result"]
            continue
        out.append({"ts": r["ts"], "actor": r["actor"], "role": x["role"],
                    "model": x["model"], "tokens": x.get("tokens"), "result": res})
    return out


def cost_of(dispatches):
    """What a task cost in subagents. Tokens is the number every harness has, unlike the
    vendor's percentage windows (T-164). `complete` is false as soon as one dispatch is
    missing the number — a sum that pretends to be whole is worse than one that says it
    is not, and the estimate-vs-actual panels on Grafana are only honest if they can
    leave the incomplete ones out."""
    toks = [x.get("tokens") for x in dispatches]
    by_model = {}
    for x in dispatches:
        if x.get("tokens"):
            by_model[x["model"]] = by_model.get(x["model"], 0) + x["tokens"]
    return {"dispatches": len(toks), "tokens": sum(t for t in toks if t),
            "by_model": by_model, "complete": all(t is not None for t in toks)}


def task_costs(project=None):
    """{task id: cost} for every task that has dispatches, in one pass.

    One query, not one per task: the queue page shows the cost on every row, and forty
    tasks meant forty scans of the event log while the global lock was held."""
    where, args = "type='task.progress'", []
    if project:                          # hits the events(project, stream, id) index
        where += " AND project=?"
        args.append(project)
    per = {}
    for r in db.execute("SELECT stream, ts, actor, body FROM events WHERE %s "
                        "AND json_extract(body, '$.dispatch') IS NOT NULL "
                        "ORDER BY id" % where, args):
        per.setdefault(r["stream"].split("/", 1)[-1], []).append(r)
    return {tid: cost_of(dispatches_of(rows)) for tid, rows in per.items()}


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
    d["dispatches"] = dispatches_of(db.execute(
        "SELECT ts, actor, body FROM events WHERE stream=? AND type='task.progress' "
        "ORDER BY id", ("task/" + tid,)))
    d["cost"] = cost_of(d["dispatches"])
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
    ceilings = {win_name(k): v for k, v in budget(t["project"])["ceilings"].items()}
    return st, {"last_seen": a["last_seen"], "ctx_pct": a["ctx_pct"],
                "budget": windows_of(a), "model": a["model"], "status": st,
                "effective_ceilings": effective_ceilings(a, ceilings)}


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
    r = db.execute("SELECT wip FROM projects WHERE name=?", (project,)).fetchone()
    if r and r["wip"] is not None:        # the owner's own number, set on the board
        return int(r["wip"])
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
    # 'blocked' is here only for owner IS NULL: a task blocked by a live owner is never
    # claimable this way — the reaper orphans it once the owner dies (reap() orphans on
    # owner death regardless of status; it only exempts awaiting_human/blocked from lease
    # expiry, not from a dead owner), and from there it is taken over as 'orphaned', not
    # reassigned in place. owner IS NULL + blocked cannot happen going forward (task_blocked
    # now claims the task when it blocks one nobody owns) but existing rows in that state —
    # the dead end this task fixes — recover through the ordinary claim path instead of a
    # bespoke repair command.
    cur = db.execute("UPDATE tasks SET status=CASE status WHEN 'in_review' THEN 'in_review'"
                     " ELSE 'claimed' END, owner=?, lease_until=?, updated=? "
                     "WHERE id=? AND owner IS NULL AND status IN ('open','orphaned','in_review','blocked')",
                     (aid, plus(LEASE_MIN), now(), tid)).rowcount
    if not cur:
        raise Err(409, "the task is taken", owner=t["owner"], status=t["status"])
    db.execute("UPDATE agents SET current_task=? WHERE id=?", (tid, aid))
    # A new owner means new code under review: results given for the previous owner's work
    # must not pass the gate for this one. Same owner re-claiming keeps its round.
    prev = db.execute("SELECT actor FROM events WHERE stream=? AND type='task.claimed' "
                      "ORDER BY id DESC LIMIT 1", ("task/" + tid,)).fetchone()
    if prev and prev["actor"] != aid:
        ev(t["project"], "task/" + tid, "task.review_round", aid, reason="new owner")
        db.execute("UPDATE tasks SET review_open=NULL, review_fixed=NULL WHERE id=?", (tid,))
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


PATCHABLE = ("repo", "touches", "risk", "priority", "estimate")
PATCH_LOCKED = ("status", "owner", "merge_sha")
# Two kinds of field, and they do not have the same owner.
#   repo/touches/risk describe THE WORK: where it lands and how dangerous it is. `risk`
#   is what triggers the human gate before a merge (§3.6), so it stays the owner's — that
#   guard is the reason task_patch checks ownership at all.
#   priority/estimate describe THE QUEUE: what should be picked up next, and how big we
#   think it is. Both are set while grooming, BEFORE anyone has claimed anything, so
#   demanding ownership for them made an estimate impossible to record in the one moment
#   it is actually made ("the task is no longer yours (open)" on a task nobody held).
QUEUE_FIELDS = ("priority", "estimate")
# Relative sizes, not hours. A closed set, because the whole point of the number is that
# it is comparable ACROSS agents and models — "how well does this model size its own
# work" is unanswerable if every agent invents its own scale. Fibonacci-ish: the gaps
# widen as the uncertainty does.
ESTIMATES = (1, 2, 3, 5, 8, 13)


def estimate_fields(value, aid):
    """The estimate, plus WHO gave it and on which model.

    The model is copied here rather than read back off the agent row later, for two
    reasons that both actually happen: `board agent cleanup` deletes nothing but the
    agent rows go stale and get reused, and a session can come back on a different model
    after a restart. Reading it later answers "what does that agent run now", which is
    not the question. The question is what the estimate was worth when it was made."""
    if value in (None, ""):
        return {"estimate": None, "estimate_by": None, "estimate_model": None}
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise Err(400, "an estimate is one of %s" % ", ".join(str(x) for x in ESTIMATES))
    if n not in ESTIMATES:
        raise Err(400, "an estimate is one of %s — relative size, not hours"
                  % ", ".join(str(x) for x in ESTIMATES), estimates=list(ESTIMATES))
    a = db.execute("SELECT model FROM agents WHERE id=?", (aid,)).fetchone()
    return {"estimate": n, "estimate_by": aid,
            "estimate_model": (a["model"] if a else None) or ("human" if aid == HUMAN else None)}


def task_patch(tid, aid, b):
    """Correct metadata that describes the task. State (status/owner/merge_sha) is owned
    by the transitions."""
    t = task(tid)
    if as_human(b) and not (b or {}).get("agent"):
        # The owner reordering the queue from the board has no agent id, and is not
        # bound by ownership — it is their board. Everything below still applies.
        aid = HUMAN
    else:
        a = agent(aid)
        same_project(a, t["project"])
        # Ownership, for the fields that describe the work. Without this any agent in the
        # project could lower `risk` from high to low on SOMEONE ELSE'S task — and risk is
        # exactly what triggers the human gate before merge (§3.6). That is, a detour
        # around the one mechanical safeguard the design rests on. The queue fields are
        # not part of that: see QUEUE_FIELDS.
        if any(k in b for k in PATCHABLE if k not in QUEUE_FIELDS):
            owns(t, aid)
    locked = [k for k in PATCH_LOCKED if k in b]
    if locked:
        raise Err(400, "cannot be set with PATCH: %s — they are owned by the transitions"
                  % ", ".join(locked))
    fields = {k: b[k] for k in PATCHABLE if k in b}
    if not fields:
        raise Err(400, "nothing to change (repo, touches, risk, priority, estimate)")
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
    if "estimate" in fields:
        fields.update(estimate_fields(fields.pop("estimate"), aid))
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
    # Ownership, like every other state change (task_patch, task_progress, release,
    # gate_merge) — a task owned by someone else is refused via owns() below. But a task
    # nobody owns yet (open/orphaned/in_review with owner IS NULL — e.g. an orphaned task
    # found via takeover.md before it was ever claimed) is the one case owns() cannot be
    # used for unchanged: it reads NULL owner as "no longer yours" and refuses outright,
    # leaving no way to block work nobody owns. So blocking it claims it in the same step —
    # a blocked task always has an owner afterwards, and the old dead end (blocked with no
    # owner, unclaimable and unannotatable) cannot arise going forward. Legacy rows already
    # stuck in it recover through task_claim instead (see task_claim's comment), not here.
    if t["owner"] is None and t["status"] in ("open", "orphaned", "in_review"):
        # Same validation task_claim does: an unknown or dead agent id must not become
        # the owner (a phantom owner makes the task unclaimable by anyone real), and a
        # project mismatch must not let an agent block work outside its own project.
        a = agent(aid, alive_only=True)
        same_project(a, t["project"])
        cur = db.execute("UPDATE tasks SET status='blocked', owner=?, updated=? "
                         "WHERE id=? AND owner IS NULL",
                         (aid, now(), tid)).rowcount
        if not cur:
            raise Err(409, "the task is taken", owner=t["owner"], status=t["status"])
        db.execute("UPDATE agents SET current_task=? WHERE id=?", (tid, aid))
    else:
        owns(t, aid)
        db.execute("UPDATE tasks SET status='blocked', updated=? WHERE id=?", (now(), tid))
    ev(t["project"], "task/" + tid, "task.blocked", aid, note=b.get("note"))
    ntfy("⛔ %s %s blocked" % (t["project"], tid), b.get("note") or t["title"],
         "%s/t/%s" % (BASE_URL, tid))
    return {"ok": True}


def review_round(tid):
    """Latest result per reviewer in the CURRENT round: {actor: {"open","fixed"}}.
    One shared column was last-write-wins: reviewer A reports 1 blocker, B and C report 0
    afterwards, and the gate opened on a known blocker. A round starts when the owner
    records a fix with --round; that drops the old results, which is safe because the owner
    cannot add a non-owner result."""
    res = {}
    for e in db.execute("SELECT type, actor, body FROM events WHERE stream=? AND type IN "
                        "('task.review_round','task.review_result') ORDER BY id", ("task/" + tid,)):
        if e["type"] == "task.review_round":
            res = {}
        else:
            res[e["actor"]] = json.loads(e["body"] or "{}")
    return res


def task_review(tid, aid, b):
    t = task(tid)
    if b.get("round"):
        if aid != t["owner"]:
            raise Err(403, "only the owner starts a new review round (--round), after a fix")
        ev(t["project"], "task/" + tid, "task.review_round", aid)
    ev(t["project"], "task/" + tid, "task.review_result", aid,
       open=int(b.get("open", 0)), fixed=int(b.get("fixed", 0)), sha=b.get("sha"))
    # The columns are derived (UI, status, WIP counter): the worst NON-OWNER result in this
    # round, same as the gate. Only the owner's word → NULL, i.e. still unreviewed.
    rs = [r for a, r in review_round(tid).items() if a != t["owner"]]
    db.execute("UPDATE tasks SET review_open=?, review_fixed=?, status='in_review', updated=? WHERE id=?",
               (max((r.get("open", 0) for r in rs), default=None),
                max((r.get("fixed", 0) for r in rs), default=None), now(), tid))
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
    rs = review_round(tid)
    others = [r for a, r in rs.items() if a != t["owner"]]
    if not rs:
        reasons.append("no review result reported in this round (board task review)")
    elif not others:
        # DESIGN.md §8 admits the hole: "an agent that writes board task review --open 0
        # without having run a review". Phase 3 was to require that the review event comes
        # from a DIFFERENT agent id than the owner's. That rule was missing, and a
        # coordinator reviewed its own task — the gate let it through on its own word.
        # Not watertight: an agent can still log a review it never ran. But it can no
        # longer do so on WORK IT OWNS ITSELF, and that is the common failure.
        # The reason must say what to DO, not just what is wrong. "a fresh agent must
        # review" is true, but a Claude subagent inherits the coordinator's session,
        # and register() above dedupes on exactly that — so the obvious fix (let the
        # subagent report) silently comes back as the owner's own id, and the reader
        # concludes the gate itself is broken. That happened and stalled the queue.
        reasons.append("the review result was set by the owner itself (%s) — a fresh "
                       "agent must review, otherwise the gate is only an echo of the "
                       "owner's word. A subagent inherits your session: set BOTH "
                       "BOARD_SESSION and BOARD_CACHE before it registers, and let it "
                       "run `board task review` itself" % t["owner"])
    else:
        # Every reviewer's latest word counts, not just the last one to write.
        n = max(r.get("open", 0) for r in others)
        if n > 0 and phase != "idea":
            reasons.append("%d open review findings" % n)
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
    if t["owner"] and t["owner"] != aid and not as_human(b or {}):
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
    # The owner of the BOARD can always take a task back. owns() protects agents from
    # each other; it was never meant to leave a person watching a task sit with a live
    # owner that has stopped making progress and no way to free it but the database.
    if not as_human(b or {}):
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
                # Both spellings. The column, the status payload and `board status --json`
                # all say `default_answer`; only the request field said `default`. Anyone
                # who read a question and wrote one back used the name they had just seen
                # and lost their default in silence — and a question with no default never
                # self-answers, so the agent blocks on it until a human appears.
                b.get("text"), json.dumps(b.get("options", [])),
                b.get("default") or b.get("default_answer"),
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

# What the night band draws. Four marks, four meanings, and they reuse the colours the
# rest of the board already assigns: landed is green, stopped is red, waiting on you is
# amber. Nothing else earns a mark — a band with fifteen kinds of dot is a log, and the
# log is already one click away on the task.
RIBBON_KINDS = {"task.done": "landed", "task.blocked": "stopped",
                "agent.presumed_dead": "stopped", "question.asked": "asked"}
RIBBON_HOURS = 12


def ribbon_events(project, hours=RIBBON_HOURS):
    """What happened in this project over the last `hours`, for the band at the top.

    The board is read in the morning by someone whose fleet worked all night, and until
    now the page carried no time at all: it could say what IS, never what HAPPENED. The
    event log has always held the answer and was never drawn.

    Indexed by events(type, project, id) — four seeks, not a scan of the log."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    out = []
    # DESC, then put back in order. `ORDER BY ts LIMIT 300` truncates from the far end:
    # a project with more than 300 of these in twelve hours would have rendered its
    # OLDEST ones and left the last few hours — the part a morning reader opens the board
    # for — silently empty.
    for r in db.execute(
            "SELECT ts, type, stream FROM events WHERE project=? AND ts>=? AND type IN (%s) "
            "ORDER BY ts DESC LIMIT 300" % ",".join("?" * len(RIBBON_KINDS)),
            [project, since] + list(RIBBON_KINDS)):
        out.append({"ts": r["ts"], "kind": RIBBON_KINDS[r["type"]], "type": r["type"],
                    "ref": r["stream"].split("/", 1)[-1] if "/" in r["stream"] else r["stream"]})
    return out[::-1]



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
            d["effective_ceilings"] = effective_ceilings(
                a, {win_name(k): v for k, v in budget(name)["ceilings"].items()})
            # T-278: same idea for silence — surfaced here so the human sees it on the
            # board and the agent sees it in `status --me`, instead of it only ever
            # having existed in the chat.
            sm = d["silent_min"] = silent_min(a)
            d["nudge"] = ("you have been silent %d min on %s — post `board task "
                          "progress` with what you found before you go quiet again"
                          % (sm, a["current_task"])) if sm is not None and sm >= SILENT_MIN else None
            agents.append(d)
        costs = task_costs(name)
        out["projects"].append({
            # The fold over this project's dispatch events, done ONCE. The HTML reads it
            # again for the done rows it draws; a second call here is a second scan of
            # the event log on every page render, under the global lock.
            "costs": costs,
            "name": name, "phase": p["phase"], "goal": p["goal"], "paused": p["paused"],
            "wip": wip_limit(name), "unreviewed": unreviewed(name),
            "ribbon": ribbon_events(name),
            "agents": agents,
            # /status is an overview for the human: show the Claude fleet's numbers, which
            # are what drive the stop rules for most of the agents.
            "budget": dict(budget(name), windows=quota_max()),
            # The cost travels with the task, not in a separate section: "what has this
            # one already spent" is read next to "how big did we think it was", or it is
            # not read at all.
            "tasks": [dict(brief(t), cost=costs.get(t["id"])) for t in db.execute(
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


# ---------- markdown ------------------------------------------------------
#
# The board renders text somebody else wrote: a task spec, a plan, a question, a comment.
# Until now all of it was escaped and printed flat, so a spec with headings and a list
# arrived as one grey wall and the structure its author gave it was thrown away.
#
# Stdlib only, like the rest of the board (DESIGN.md §10) — and deliberately a SUBSET.
# The rule that makes it safe is the order: the text is escaped FIRST and the markup is
# built out of the escaped text afterwards, so nothing an agent writes can become an
# element. `<script>` in a spec is `&lt;script&gt;` before this code ever looks at it.
# That is also why every pattern below matches on escaped text (`&lt;`, `&amp;`) and why
# the only href that survives is one this file wrote.
MD_LINK = re.compile(r"\[([^\]\n]{1,200})\]\(([^)\s]{1,500})\)")
MD_CODE = re.compile(r"(`[^`\n]+`)")
MD_BOLD = re.compile(r"\*\*([^*\n]{1,300})\*\*")
MD_ITAL = re.compile(r"(?<![*\w])\*([^*\n]{1,300})\*(?!\w)")
MD_ITAL_ = re.compile(r"(?<![_\w])_([^_\n]{1,300})_(?!\w)")
MD_HEAD = re.compile(r"^(#{1,4})\s+(.*)$")
MD_ULI = re.compile(r"^(\s*)[-*+]\s+(.*)$")
MD_OLI = re.compile(r"^(\s*)\d{1,3}[.)]\s+(.*)$")
MD_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
MD_FENCE = re.compile(r"^\s*```+\s*([A-Za-z0-9_+-]*)\s*$")


def md_href(url):
    """The only links that survive are ones that go somewhere over http.

    `[click](javascript:…)` is the reason: a scheme check here is what keeps a task spec
    from shipping a script as a link. Relative paths are allowed because the board links
    to its own pages; anything else is left as plain text by the caller."""
    u = url.strip()
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if u.startswith("/") and not u.startswith("//"):
        return u
    return None


def md_inline(esc):
    """Inline marks, on text that is ALREADY escaped.

    Code spans are lifted out first and put back untouched: a backticked `**x**` is a
    literal in every markdown there is, and treating it as bold is how a spec that
    documents markdown ends up rendering its own examples."""
    out = []
    for i, part in enumerate(MD_CODE.split(esc)):
        if i % 2:
            out.append("<code>%s</code>" % part[1:-1])
            continue

        def link(m):
            href = md_href(m.group(2))
            if not href:
                return m.group(0)          # not a link: leave the source text standing
            return "<a class='%s' href='%s' rel='noreferrer'>%s</a>" % (LINK, href, m.group(1))
        part = MD_LINK.sub(link, part)
        part = MD_BOLD.sub(r"<b>\1</b>", part)
        part = MD_ITAL.sub(r"<i>\1</i>", part)
        part = MD_ITAL_.sub(r"<i>\1</i>", part)
        out.append(part)
    return "".join(out)


def md_list(lines, start):
    """One list, from `start`, returning the markup and the line it stopped at. Handles
    one level of nesting — deeper than that is an outline, and an outline in a task spec
    is a sign the task wants splitting, not that the renderer wants features."""
    ordered = bool(MD_OLI.match(lines[start]))
    items, i, sub = [], start, []
    while i < len(lines):
        m = MD_OLI.match(lines[i]) if ordered else MD_ULI.match(lines[i])
        other = MD_ULI.match(lines[i]) if ordered else MD_OLI.match(lines[i])
        if m and len(m.group(1)) < 2:
            if sub:
                items[-1] += "<ul>%s</ul>" % "".join("<li>%s</li>" % s for s in sub)
                sub = []
            items.append(md_inline(m.group(2)))
        elif (m or other) and len((m or other).group(1)) >= 2 and items:
            sub.append(md_inline((m or other).group(2)))
        elif other and len(other.group(1)) < 2:
            break            # the other kind of list at this level starts a list of its own
        elif lines[i].strip() and items and not MD_HEAD.match(lines[i]) \
                and not MD_FENCE.match(lines[i]) and not MD_HR.match(lines[i]) \
                and not lines[i].lstrip().startswith("&gt; "):
            # A lazy continuation — markdown lets a wrapped line keep belonging to the
            # item above it. Everything that starts a block of its own is excluded, or a
            # rule or a quote written straight after a list is swallowed into the list.
            items[-1] += " " + md_inline(lines[i].strip())
        else:
            break
        i += 1
    if sub:
        items[-1] += "<ul>%s</ul>" % "".join("<li>%s</li>" % s for s in sub)
    tag = "ol" if ordered else "ul"
    return "<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % x for x in items), tag), i


def md(text, inline=False):
    """Markdown, for text the board did not write.

    `inline=True` gives the marks only — for a title or a goal, which live inside a line
    the page has already laid out and must not suddenly contain a heading."""
    if not text:
        return ""
    esc = escape(str(text)).replace("\r\n", "\n").replace("\r", "\n")
    if inline:
        return md_inline(esc.replace("\n", " "))
    lines, out, i = esc.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        fence = MD_FENCE.match(line)
        if fence:
            body, i = [], i + 1
            while i < len(lines) and not MD_FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1                                   # the closing fence, if there is one
            out.append("<pre><code>%s</code></pre>" % "\n".join(body))
            continue
        if not line.strip():
            i += 1
            continue
        if MD_HR.match(line):
            out.append("<hr>")
            i += 1
            continue
        h = MD_HEAD.match(line)
        if h:
            lvl = min(len(h.group(1)) + 2, 6)        # a spec's "#" is not the page's h1
            out.append("<h%d>%s</h%d>" % (lvl, md_inline(h.group(2).strip()), lvl))
            i += 1
            continue
        if MD_ULI.match(line) or MD_OLI.match(line):
            block, i = md_list(lines, i)
            out.append(block)
            continue
        if line.lstrip().startswith("&gt; "):
            quote = []
            while i < len(lines) and lines[i].lstrip().startswith("&gt;"):
                quote.append(lines[i].lstrip()[4:].lstrip())
                i += 1
            out.append("<blockquote>%s</blockquote>" % md_inline(" ".join(quote)))
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not MD_HEAD.match(lines[i]) \
                and not MD_ULI.match(lines[i]) and not MD_OLI.match(lines[i]) \
                and not MD_FENCE.match(lines[i]) and not MD_HR.match(lines[i]) \
                and not lines[i].lstrip().startswith("&gt; "):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>%s</p>" % md_inline("<br>".join(para)))
    return "".join(out)


def prose(text, cls=""):
    """A block of somebody else's text, in a container the stylesheet can reach."""
    body = md(text)
    if not body:
        return ""
    return "<div class='%s'>%s</div>" % (("md " + cls).strip(), body)


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


# Applied BEFORE the first paint, which is the whole reason this is its own script and
# lives in <head>. VERIFIED in board.css: daisyUI guards the dark rules with
# `@media (prefers-color-scheme:dark){:root:not([data-theme])}` and emits
# `[data-theme=board-dark]` unguarded — so an explicit choice on <html> overrides the
# operating system in BOTH directions and needs no stylesheet change at all. Set from the
# bottom of the page instead, and the browser paints the wrong theme and then swaps it:
# a flash of exactly the thing the setting exists to prevent.
#
# It also handles its own clicks, by delegation on `document`, so the toggle works on
# every page — the question and task pages carry the header too, and only /status and /t
# get the filter script.
THEME_JS = """
(function(){
var K='board:theme';
function get(){try{return localStorage.getItem(K)||'system';}catch(e){return 'system';}}
function apply(t){
  var r=document.documentElement;
  if(t==='light'||t==='dark')r.setAttribute('data-theme','board-'+t);
  else r.removeAttribute('data-theme');
  var b=document.querySelectorAll('[data-theme-set]');
  for(var i=0;i<b.length;i++){
    b[i].setAttribute('aria-pressed', b[i].getAttribute('data-theme-set')===t?'true':'false');
  }
}
apply(get());
document.addEventListener('click',function(e){
  var el=e.target&&e.target.closest?e.target.closest('[data-theme-set]'):null;
  if(!el)return;
  e.preventDefault();
  var t=el.getAttribute('data-theme-set');
  try{localStorage.setItem(K,t);}catch(err){}
  apply(t);
});
document.addEventListener('DOMContentLoaded',function(){apply(get());});
})();
"""
THEME_SHA = "'sha256-%s'" % base64.b64encode(
    hashlib.sha256(THEME_JS.encode()).digest()).decode()
THEME = "<script>%s</script>" % THEME_JS


def theme_toggle():
    """Follow the system, or say otherwise. Three states and not a switch, because
    "follow the system" is a real answer and a two-state switch cannot express it — once
    you flip a switch you have opted out of the operating system forever without being
    told that is what you did."""
    return ("<div class='themer' role='group' aria-label='colour theme'>%s</div>"
            % "".join("<button type='button' class='th' data-theme-set='%s' "
                      "aria-pressed='false'>%s</button>" % (k, lbl)
                      for k, lbl in (("system", "auto"), ("light", "light"), ("dark", "dark"))))


def page(title, body):
    return ("<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s</title><link rel=stylesheet href='%s'>%s"
            "<body class='bg-base-100 text-base-content font-sans antialiased'>"
            "<div class='%s'>%s</div>" % (escape(title), CSS_URL, THEME, WRAP, body))


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


def head(title, right="", nav=(("/status", "board"),), human=False):
    return ("<header class='flex flex-wrap items-baseline gap-x-3 gap-y-1 "
            "border-b-2 border-base-300 pb-3'>"
            "<h1 class='text-xl font-semibold tracking-tight sm:text-2xl'>%s</h1>%s"
            "<nav class='ml-auto flex items-center gap-4 text-sm'>%s%s%s</nav></header>" % (
                escape(title), right,
                "".join("<a class='%s' href='%s'>%s</a>" % (LINK, u, escape(t))
                        for u, t in nav),
                theme_toggle(),
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


def toks(n):
    """12400 → 12.4k. The queue shows one of these per row; the exact figure belongs on
    the task's own page, and six digits per row in a phone-width column is noise."""
    n = int(n or 0)
    if n >= 1000000:
        return "%.1fM" % (n / 1000000.0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)


def cost_cell(t):
    """What the task has spent so far. `~` means at least one subagent never reported its
    number, so the sum is a floor and not a total — the difference matters as soon as
    anyone divides it by the estimate."""
    c = t.get("cost") or {}
    if not c.get("dispatches"):
        return ""
    mark = "" if c.get("complete") else "~"
    return ("<span class='%s' title='%d dispatches%s'>%s%s</span>"
            % (MONO, c["dispatches"], "" if c.get("complete") else ", one is missing its token count",
               mark, toks(c.get("tokens"))))


def gauge(window, pct, ceiling, project, human, overridden=False):
    """The quota gauge: what the fleet has used, and the line it must not cross — with
    the line as a handle you can drag.

    Until now the only way to move a ceiling was to hand-edit a ConfigMap on the host,
    which is why a project missing an entry in it simply stopped, with a note telling the
    owner to go and write YAML. The handle sits ON the track, not under it: dragging it
    is reading it, and a second bar underneath would have been the same numbers twice.

    A range input rather than a drawn handle and mouse maths — it drags with a finger,
    it moves with the arrow keys, it announces itself to a screen reader, and with the
    script blocked it still moves and the Set button still saves it."""
    v = float(pct or 0)
    c = float(ceiling or 0)
    fill = "over" if c > 0 and v >= c else ("warn" if c > 0 and v >= c - 5 else "")
    tick = ("<u data-c='%s' style='left:%s%%'></u>" % (round(c), round(min(c, 100)))) if c > 0 else ""
    if not human:
        return meter(window, pct, ceiling)
    return ("<form class='meter gauge' method='POST' action='/projects/%s/ceilings'>"
            "<input type='hidden' name='project' value='%s'>"
            "<span class=k>%s</span>"
            "<span class=track><i class='%s' style='width:%s%%'></i>%s"
            "<input class='handle' type='range' name='ceiling.%s' min='0' max='100' "
            "step='1' value='%d' data-ceiling aria-label='ceiling for %s, now %d percent'>"
            "</span>"
            "<output class='%s' data-ceiling-out title='%s'>%d</output>"
            "<button type='submit' class='sr-only-btn'>Set</button>"
            "</form>" % (
                escape(project), escape(project), escape(window),
                fill, round(min(v, 100)), tick,
                escape(window), int(round(c)), escape(window), int(round(c)),
                MONO, "set here" if overridden else "from board-policy.json",
                int(round(c))))


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
    the page shows: it is the first one the fleet hits.

    Ramped like agent_stop (T-428 review 55): each project's raw ceiling is replaced by
    the LOOSEST (max) effective ceiling among that project's own agents for the window —
    a project with no agent near reset keeps its fixed ceiling, one agent near reset
    stops the runway from showing "over" while it is still correctly working. Still the
    tightest (min) across projects, same as before."""
    ceil = {}
    for p in s["projects"]:
        for w, c in ((p.get("budget") or {}).get("ceilings") or {}).items():
            w, c = win_name(w), float(c or 0)
            eff = max([a.get("effective_ceilings", {}).get(w, c) for a in p["agents"]] or [c])
            ceil[w] = min(ceil.get(w, eff), eff)
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
    parts = [("", len(p["agents"]), "agents" if len(p["agents"]) != 1 else "agent", ""),
             ("", len(p["tasks"]), "queued", "")]
    for st, lbl, cls in (("in_review", "to review", ""),
                         ("blocked", "blocked", "text-error"),
                         ("orphaned", "unowned", "text-error")):
        if n.get(st):
            parts.append((cls, n[st], lbl, ""))
    # T-278: a task inching along is not "stuck" (that is `blocked`/`orphaned` above) but
    # nobody would otherwise notice an agent gone quiet until the lease itself expires.
    # Review finding 85: the UI stays English ("silent"), but the acceptance spec greps
    # the Norwegian word `stille` — put it in the title so the grep holds honestly rather
    # than switching the visible label.
    silent = sum(1 for a in p["agents"] if a.get("status") == "alive"
                 and a.get("silent_min") is not None and a["silent_min"] >= SILENT_MIN)
    if silent:
        parts.append(("text-warning", silent, "silent", "stille (silent)"))
    if p["questions"]:
        parts.append(("text-warning", len(p["questions"]),
                      "question" if len(p["questions"]) == 1 else "questions", ""))
    return ("<span class='flex flex-wrap justify-end gap-x-3 gap-y-1 text-xs %s'>%s</span>" % (
        DIM, "".join("<span class='%s'%s><b class='%s font-semibold'>%s</b> %s</span>" % (
            c, (" title='%s'" % escape(title)) if title else "", MONO, v, escape(l))
            for c, v, l, title in parts)))


def last_activity(p):
    """Last movement, not last manifest change. p["tasks"]/p["agents"] are already
    filtered for display (done/finished are removed there), so a project whose last event
    was precisely "task went done" would otherwise sort as dead. Ask the database
    directly instead, regardless of status."""
    name = p["name"]
    t = db.execute("SELECT MAX(updated) m FROM tasks WHERE project=?", (name,)).fetchone()["m"]
    a = db.execute("SELECT MAX(last_seen) m FROM agents WHERE current_project=?", (name,)).fetchone()["m"]
    return max(t or "", a or "", "")


# Opens the project you last looked at, drives the filter rail, and saves a control the
# moment you let go of it. The ONLY script the board serves: the CSP admits it by hash,
# so anything added here changes FOCUS_SHA and nothing else can ever be added by accident.
FOCUS_JS = """
(function(){
var LS={g:function(k,d){try{return localStorage.getItem(k)||d;}catch(e){return d;}},
        s:function(k,v){try{localStorage.setItem(k,v);}catch(e){}}};
var AGE={'1h':60,'24h':1440,'7d':10080};
var STOP={'blocked':1,'orphaned':1},FLY={'claimed':1,'merging':1};

/* Projects are bands, not <details>: nothing to open, nothing to remember. The rail's
   project filter is what narrows the board now, and it remembers its own value. */
var pjs=document.querySelectorAll('.pj');

/* links from a notification carry the filter with them */
var qs=new URLSearchParams(window.location.search);
if(qs.get('all')==='1'||qs.get('all')==='true')LS.s('board:all','1');
else if(qs.has('all'))LS.s('board:all','0');
if(qs.get('interval'))LS.s('board:age',qs.get('interval'));
if(qs.get('status'))LS.s('board:status',qs.get('status'));

function val(name,dflt){return LS.g('board:'+name,dflt);}

function apply(){
  var all=val('all','0')==='1';
  var fp=val('project','all'),fs=val('status','active'),fa=val('age','all');
  /* The project filter is remembered across pages, but /status?project=X renders ONE
     project. A filter naming a different one then hid the only row on the page: a blank
     board, a blank select and "0 shown", with nothing saying why. A stored value that
     matches nothing here is not a filter, it is a leftover. */
  if(fp!=='all'){
    var known=false;
    for(var q=0;q<pjs.length;q++){if(pjs[q].dataset.p===fp){known=true;break;}}
    if(!known){fp='all';LS.s('board:project','all');}
  }
  var sel=document.querySelectorAll('[data-f]');
  for(var s=0;s<sel.length;s++){
    var f=sel[s].getAttribute('data-f');
    sel[s].value=f==='project'?fp:(f==='status'?fs:fa);
  }
  var b=document.getElementById('btn-show-all');
  if(b){
    b.textContent=all?'Filters off':'Show everything';
    b.classList.toggle('badge-primary',all);
    b.classList.toggle('badge-ghost',!all);
  }
  var hid=0,shown=0;
  for(var p=0;p<pjs.length;p++){
    var pj=pjs[p];
    var offProject=!all&&fp!=='all'&&pj.dataset.p!==fp;
    pj.style.display=offProject?'none':'';
    var ci=pj.querySelector('[data-agent-cleanup]');
    if(ci&&AGE[fa])ci.value=fa;

    var ac=pj.querySelectorAll('[data-agent]'),ah=0;
    for(var c=0;c<ac.length;c++){
      var ag=ac[c],st=ag.getAttribute('data-status');
      var mn=parseInt(ag.getAttribute('data-mins')||'0',10);
      /* A dead agent is history, not fleet: it is hidden once it is older than the
         window you are looking at, and a day is the window when you have not said.
         Without that default one project here shows eighty-nine of them and the three
         that are working scroll off the screen. A LIVE agent is never hidden by age. */
      var off=false;
      if(!all&&st==='dead')off=(mn>(AGE[fa]||1440));
      ag.style.display=off?'none':'';
      if(off)ah++;
    }
    var ab=pj.querySelector('[data-agent-badge]');
    if(ab)ab.textContent=ah?('('+ah+' hidden)'):'';

    var rows=pj.querySelectorAll('[data-task]'),th=0;
    for(var r=0;r<rows.length;r++){
      var rw=rows[r],ts=rw.getAttribute('data-status');
      var tm=parseInt(rw.getAttribute('data-mins')||'0',10),off2=false;
      if(!all){
        if(fs==='active')off2=(ts==='done'||ts==='archived');
        else if(fs==='in_flight')off2=!FLY[ts];
        else if(fs==='in_review')off2=(ts!=='in_review');
        else if(fs==='stopped')off2=!STOP[ts];
        else if(fs==='done')off2=(ts!=='done');
        if(!off2&&AGE[fa])off2=(tm>AGE[fa]);
      }
      rw.style.display=off2?'none':'';
      if(off2)th++;else if(!offProject)shown++;
    }
    /* A group heading with every row under it hidden is a label for nothing. The
       headings are siblings of the rows, so walk forward from each one until the next
       heading and hide it when it turns out to own no visible row. */
    var grps=pj.querySelectorAll('.grp');
    for(var g=0;g<grps.length;g++){
      var any=false,sib=grps[g].nextElementSibling;
      while(sib&&!sib.classList.contains('grp')){
        if(sib.hasAttribute('data-task')&&sib.style.display!=='none'){any=true;break;}
        sib=sib.nextElementSibling;
      }
      grps[g].style.display=any?'':'none';
    }
    if(!offProject)hid+=th;
  }
  var out=document.querySelector('[data-rail-count]');
  if(out)out.textContent=shown+' shown'+(hid?', '+hid+' hidden':'');
}

var fsel=document.querySelectorAll('[data-f]');
for(var i2=0;i2<fsel.length;i2++){
  fsel[i2].addEventListener('change',function(){
    LS.s('board:'+this.getAttribute('data-f'),this.value);
    LS.s('board:all','0');
    apply();
  });
}
var sab=document.getElementById('btn-show-all');
if(sab)sab.addEventListener('click',function(e){
  e.preventDefault();
  LS.s('board:all',val('all','0')==='1'?'0':'1');
  apply();
});

/* a control you changed is a control you meant to change: save it on release, so the
   Set button beside it is the fallback and not the ritual */
var auto=document.querySelectorAll('[data-autosave]');
for(var a2=0;a2<auto.length;a2++){
  auto[a2].addEventListener('change',function(){
    if(this.form)this.form.submit();
  });
}
/* the ceiling: the number follows the handle while you drag, the save happens when you
   let go (change, not input — otherwise every pixel is a POST) */
var rng=document.querySelectorAll('[data-ceiling]');
for(var r2=0;r2<rng.length;r2++){
  rng[r2].addEventListener('input',function(){
    var o=this.parentNode.querySelector('[data-ceiling-out]');
    if(o)o.textContent=this.value;
    var tick=this.closest('.gauge').querySelector('.track > u');
    if(tick){tick.style.left=Math.min(this.value,100)+'%';tick.setAttribute('data-c',this.value);}
  });
  rng[r2].addEventListener('change',function(){this.form.submit();});
}

var cf=document.querySelectorAll('form[data-confirm]');
for(var k2=0;k2<cf.length;k2++){
  cf[k2].addEventListener('submit',function(e){
    if(!confirm(this.getAttribute('data-confirm')))e.preventDefault();
  });
}
apply();
})();
"""
# The CSP lets no script-src in; the hash keeps it exactly as tight as before.
FOCUS_SHA = "'sha256-%s'" % base64.b64encode(
    hashlib.sha256(FOCUS_JS.encode()).digest()).decode()
FOCUS = "<script>%s</script>" % FOCUS_JS


def q_inline(q, answer=None):
    """A question where it belongs: under the task it is about, in the queue, at that
    task's place in the priority order.

    It used to sit in its own block at the bottom of the project, which is the one place
    it cannot be read in context — you saw that something was waiting on you, and then
    had to go and find out which of forty tasks it was waiting on. The card at the bottom
    survives for questions about no task at all; everything else is a child of its row."""
    answered = ("<span class='badge badge-sm badge-warning'>answered by the board: %s</span>"
                % escape(answer)) if answer is not None else ""
    return ("<a class='qrow' href='/q/%s'>"
            "<span class='%s text-xs'>%s</span>"
            "<span class='max-w-[68ch]'>%s</span>%s</a>" % (
                escape(q["id"]), MONO, escape(q["id"]), md(q["text"], inline=True), answered))


def prio_cell(t, human, back):
    """Priority, and for the owner of the board a field to change it in.

    A number rather than up/down arrows: the queue is sorted by it and forty tasks all
    sitting on the default 50 need one of them set to 80, not nudged thirty times."""
    v = int(t.get("priority") or 0)
    if not human:
        return "<span class='%s'>%d</span>" % (MONO, v)
    return ("<form method='POST' action='/t/%s/patch' class='m-0'>"
            "<input type='hidden' name='back' value='%s'>"
            "<input class='cell-num %s' type='number' name='priority' value='%d' min='0' "
            "max='100' step='5' data-autosave aria-label='priority for %s'>"
            "<button type='submit' class='sr-only-btn'>Set</button></form>" % (
                escape(t["id"]), escape(back), MONO, v, escape(t["id"])))


def size_cell(t, human, back):
    """Estimate and actual, side by side, because neither is worth much alone. The arrow
    is the whole point of the column: it is read as "we thought 5, it has cost 12.4k"."""
    c = cost_cell(t)
    if not human:
        size = ("<span class='%s' title='relative size'>%s</span>" % (MONO, t["estimate"])
                if t.get("estimate") else "")
        return "%s%s" % (size, (" " + c) if c else "")
    opts = "".join("<option value='%d'%s>%d</option>"
                   % (e, " selected" if t.get("estimate") == e else "", e)
                   for e in ESTIMATES)
    return ("<form method='POST' action='/t/%s/patch' class='m-0 flex items-baseline gap-1'>"
            "<input type='hidden' name='back' value='%s'>"
            "<select class='cell-num %s' name='estimate' data-autosave aria-label='size of %s'>"
            "<option value=''>–</option>%s</select>"
            "<button type='submit' class='sr-only-btn'>Set</button>"
            "<span class='%s'>%s</span></form>" % (
                escape(t["id"]), escape(back), MONO, escape(t["id"]), opts, DIM, c))


def q_card(q, answer=None):
    """The question card is the ONLY surface on the board that is a human's job, and that
    is why it is the only one that gets the accent edge. The edge carries the meaning; it
    is not decoration."""
    return ("<div class='rounded-box border border-base-300 border-l-4 border-l-warning "
            "bg-base-200 p-4'>"
            "<h3 class='mb-2 text-base font-semibold'><a class='%s %s' href='/q/%s'>%s</a>%s</h3>"
            "<dl class='mb-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-sm'>"
            "<dt class='%s'>kind<dd>%s<dt class='%s'>task<dd class='%s'>%s%s</dl>"
            "<div class='md'>%s</div></div>" % (
                LINK, MONO, escape(q["id"]), escape(q["id"]),
                " <span class='badge badge-sm badge-warning'>board answered</span>"
                if answer is not None else "",
                DIM, escape(q["kind"]), DIM, MONO, escape(q["task"] or "—"),
                ("<dt class='%s'>board's answer<dd>%s" % (DIM, escape(answer)))
                if answer is not None else "",
                md(q["text"])))


def rail(s):
    """One filter bar for the whole board, not one per project.

    Every project used to carry its own pair of selects, so "show me everything blocked"
    meant setting the same filter four times and the answer was still four separate
    lists. The rail is sticky because the board is scrolled, and the thing you are
    filtering is below the fold by the second project."""
    projects = "".join("<option value='%s'>%s</option>" % (escape(p["name"]), escape(p["name"]))
                       for p in s["projects"])
    return ("<div class='rail' role='group' aria-label='filters'>"
            "<label class='f'><span class='%s'>Project</span>"
            "<select data-f='project'><option value='all'>All</option>%s</select></label>"
            "<label class='f'><span class='%s'>Status</span>"
            "<select data-f='status'>"
            "<option value='active'>Active and open</option>"
            "<option value='in_flight'>In flight</option>"
            "<option value='in_review'>Waiting for review</option>"
            "<option value='stopped'>Blocked or unowned</option>"
            "<option value='done'>Landed</option>"
            "<option value='all'>Everything</option></select></label>"
            "<label class='f'><span class='%s'>Changed</span>"
            "<select data-f='age'><option value='all'>Any time</option>"
            "<option value='1h'>Last hour</option>"
            "<option value='24h'>Last 24 hours</option>"
            "<option value='7d'>Last 7 days</option></select></label>"
            "<button type='button' id='btn-show-all' class='badge badge-sm badge-ghost "
            "cursor-pointer font-sans select-none' "
            "title='Ignore the filters and show every row'>Show everything</button>"
            "<span data-rail-count class='%s ml-auto text-xs'></span></div>" % (
                DIM, projects, DIM, DIM, DIM))


def phase_control(p, human):
    if not human:
        return "<span class='badge badge-sm badge-ghost'>%s</span>" % escape(p["phase"])
    opts = "".join("<option value='%s'%s>%s</option>"
                   % (ph, " selected" if ph == p["phase"] else "", ph) for ph in PHASES)
    return ("<form method='POST' action='/projects/%s/phase' class='m-0 inline-flex'>"
            "<input type='hidden' name='project' value='%s'>"
            "<select name='phase' class='pill' data-autosave aria-label='phase'>%s</select>"
            "<button type='submit' class='sr-only-btn'>Set</button></form>" % (
                escape(p["name"]), escape(p["name"]), opts))


def night_band(events, hours=RIBBON_HOURS):
    """What happened while you were away, drawn on a line.

    The board had no time on it at all. It is read in the morning by someone whose fleet
    worked all night, and it could say what IS but never what HAPPENED — while the event
    log held the whole answer and was never drawn. Four kinds of mark, reusing the
    colours the rest of the board already assigns; a band with fifteen kinds of dot would
    be a log, and the log is one click away on the task itself."""
    now_ = datetime.now(timezone.utc)
    start = now_ - timedelta(hours=hours)
    span = hours * 3600.0
    marks, n = [], {"landed": 0, "stopped": 0, "asked": 0}
    for e in events:
        t = ts(e["ts"])
        if not t:
            continue
        at = (t - start).total_seconds() / span * 100.0
        if at < 0 or at > 100:
            continue
        n[e["kind"]] = n.get(e["kind"], 0) + 1
        marks.append("<i class='mk mk-%s' style='left:%.2f%%' title='%s %s at %s'></i>" % (
            e["kind"], at, escape(e["ref"]), escape(e["kind"]), escape(e["ts"][11:16])))
    # Hour ticks every three hours, labelled in the board's own UTC.
    ticks = []
    for h in range(0, hours + 1, 3):
        ticks.append("<b class='tk' style='left:%.2f%%'>%s</b>" % (
            h / float(hours) * 100.0, (start + timedelta(hours=h)).strftime("%H")))
    counts = [(n["landed"], "landed", ""), (n["stopped"], "stopped", "text-error"),
              (n["asked"], "asked", "text-warning")]
    legend = "".join("<span class='%s'><b class='%s font-semibold'>%d</b> %s</span>"
                     % (cls, MONO, v, lbl) for v, lbl, cls in counts if v)
    if not marks:
        legend = "<span class='%s'>nothing has happened in %d hours</span>" % (DIM, hours)
    return ("<div class='night'><span class='%s night-k'>last %dh</span>"
            "<span class='night-track'>%s%s</span>"
            "<span class='night-legend'>%s</span></div>" % (
                DIM, hours, "".join(ticks), "".join(marks), legend))


GRANT_VOCAB = ("merge", "deploy-dev", "deploy-prod")


def grant_chips(a, project, known, human):
    """Permissions as chips you switch on and off.

    What stood here was a text field with a datalist and a Grant button — a form for
    writing a row into a table, not a control. It could not show what permissions EXIST,
    only which had already been typed in, so the one question you actually have looking
    at an agent ("may it merge?") took reading a list and knowing what was missing from
    it. Held is filled, available is outlined, one press either way."""
    held = set(a.get("grants") or [])
    if not human:
        return "".join("<span class='chip chip-on'>%s</span>" % escape(g)
                       for g in sorted(held))
    out = []
    for g in known:
        on = g in held
        out.append(
            "<form method='POST' action='/agents/%s/grants%s' class='inline m-0'>"
            "<input type='hidden' name='project' value='%s'>"
            "<input type='hidden' name='grant' value='%s'>"
            "<button type='submit' class='chip %s' aria-pressed='%s' title='%s %s'>%s</button>"
            "</form>" % (
                escape(a["id"]), "/revoke" if on else "", escape(project or ""), escape(g),
                "chip-on" if on else "chip-off", "true" if on else "false",
                "take back" if on else "give", escape(g), escape(g)))
    return "".join(out)


def tightest(a, ceilings):
    """The one window this agent is closest to being stopped by.

    Three meters per agent, times a dozen agents, is not density — it is the same
    drawing repeated until it stops being read. Only the binding constraint earns the
    bar; the rest are on the agent's own task page."""
    eff = a.get("effective_ceilings") or ceilings or {}
    best = None
    for w, pct in windows_of(a).items():
        c = float(eff.get(w) or ceilings.get(w) or 0)
        share = (pct / c) if c > 0 else 0
        if best is None or share > best[0]:
            best = (share, w, pct, c)
    return best


def agent_line(a, ceilings, human, project, known):
    """One agent, one line: what it runs, what it holds, what it is on, and the one
    number that can stop it."""
    st = a.get("status") or "unknown"
    mins = int(mins_since(a.get("last_seen"))) if a.get("last_seen") else 999999
    live = st in ("alive", "stalled")
    t = tightest(a, ceilings) if live else None
    if t:
        _, w, pct, c = t
        bar = meter(w, pct, c)
    elif live:
        # Reporting no quota window is a valid, honest answer (T-164) and a different
        # thing from having stopped reporting at all. The first cut printed "last seen"
        # for both, which told a working agent's row that it was dead.
        bar = "<span class='%s text-xs'>reports no quota</span>" % DIM
    else:
        bar = "<span class='%s text-xs'>last seen %s</span>" % (
            DIM, escape((a.get("last_seen") or "—")[:16].replace("T", " ")))
    ctx = a.get("ctx_pct")
    ctx_h = ""
    if live and ctx is not None:
        cls = "text-error" if ctx >= 80 else ("text-warning" if ctx >= 65 else DIM)
        ctx_h = "<span class='%s %s text-xs' title='context window'>ctx %d%%</span>" % (
            cls, MONO, round(ctx))
    task_h = ("<a class='%s %s' href='/t/%s'>%s</a>" % (
        LINK, MONO, escape(a["current_task"]), escape(a["current_task"]))
        if a.get("current_task") else "<span class='%s'>—</span>" % DIM)
    roles = " ".join(a.get("roles") or [])
    role_h = ("<span class='badge badge-sm badge-outline'>%s</span>" % escape(roles)) if roles else ""
    if human and project:
        if "coordinator" in (a.get("roles") or []):
            role_h += ("<form method='POST' action='/roles/coordinator/unpin' class='inline m-0'>"
                       "<input type='hidden' name='project' value='%s'>"
                       "<button type='submit' class='chip chip-act' "
                       "title='Let the election decide again'>unpin</button></form>"
                       % escape(project))
        elif st in ("alive", "stalled"):
            role_h += ("<form method='POST' action='/roles/coordinator/pin' class='inline m-0'>"
                       "<input type='hidden' name='project' value='%s'>"
                       "<input type='hidden' name='agent' value='%s'>"
                       "<button type='submit' class='chip chip-act' "
                       "title='Pin this agent as coordinator'>make coordinator</button></form>"
                       % (escape(project), escape(a["id"])))
    stop = a.get("stop")
    silent = (a.get("silent_min") is not None and a["silent_min"] >= SILENT_MIN
              and st == "alive")
    note = ""
    if stop:
        note = "<p class='ag-note text-error'>%s</p>" % escape(stop)
    elif silent:
        note = "<p class='ag-note text-warning'>silent %d min</p>" % a["silent_min"]
    return ("<div class='ag' data-agent data-status='%s' data-mins='%d'>"
            "<span class='ag-model'>%s</span>"
            "<span class='ag-task'>%s</span>"
            "<span class='ag-meter'>%s</span>"
            "<span class='ag-ctx'>%s</span>"
            "<span class='ag-grants'>%s%s</span>"
            "<span class='%s ag-id' title='%s'>%s</span>"
            "%s</div>" % (
                escape(st), mins,
                escape(a.get("model") or a.get("harness") or "?"),
                task_h, bar, ctx_h,
                grant_chips(a, project, known, human), role_h,
                # The id, small and dim, with the host as its tooltip — not the other way
                # round, which is how the first cut had it. What was wrong with the old
                # block was that the id SHOUTED, not that it was there: it is the handle
                # you paste into `board grant <id>`, and a row you cannot name is a row
                # you cannot act on from a shell.
                DIM + " " + MONO, escape(a.get("host") or ""), escape(a["id"]),
                note))


QUEUE_GROUPS = (
    ("stopped", "stopped", ("blocked", "orphaned")),
    ("flight", "in flight", ("claimed", "merging", "in_review")),
    ("waiting", "waiting", ("open",)),
)


def queue_card(t, human, back, questions, big, hidden=False):
    """A task. `big` ones carry their title at reading size; the rest are one line,
    because forty rows that all look important look like a spreadsheet and get read like
    one.

    No owner column: it said `6320` — the tail of an agent id, which is not information —
    and the fleet directly below already says which agent is on which task, by model. One
    cross-reference beats a column of hashes.

    priority and size are rendered ONCE each, by the same helpers the task page uses:
    they come back as a number for a reader and as a control for the owner of the board.
    The first cut drew both, so every row showed its priority twice and its size twice."""
    st = t.get("status") or "unknown"
    mins = int(mins_since(t.get("updated") or t.get("created"))) if (t.get("updated") or t.get("created")) else 0
    pn = pr_num(t.get("pr"))
    pr_h = (("<a class='%s %s' href='%s'>#%s</a>" % (LINK, MONO, escape(t["pr"]), escape(pn))
             if str(t.get("pr", "")).startswith("http") else
             "<span class='%s'>#%s</span>" % (MONO, escape(pn))) if pn else "")
    # One container owning the grid area, not N items placed into it: named-area
    # placement does not auto-flow, so two questions on the same task landed in the
    # identical cell and drew on top of each other.
    qs = ("<div class='tk-qs'>%s</div>" % "".join(
        q_inline(q, q["answer"] if q["status"] == "defaulted" else None)
        for q in questions)) if questions else ""
    return ("<article class='tk-row %s%s' data-task data-status='%s' data-mins='%d' id='%s'%s>"
            "<span class='tk-pri'>%s</span>"
            "<a class='tk-id %s %s' href='/t/%s'>%s</a>"
            "<span class='badge badge-sm %s tk-st'>%s</span>"
            "<h4 class='tk-title'>%s</h4>"
            "<span class='tk-meta'>%s%s</span>"
            "%s</article>" % (
                "tk-big" if big else "", " " + SPINE.get(st, "") if SPINE.get(st) else "",
                escape(st), mins, escape(t["id"]),
                # Hidden in the markup and not only by the script at the end of <body>:
                # thirty landed rows would otherwise paint and then disappear, and stay
                # for good if the script never runs.
                " style='display:none;'" if hidden else "",
                prio_cell(t, human, back),
                LINK, MONO, escape(t["id"]), escape(t["id"]),
                BADGE.get(st, "badge-ghost"), escape(st),
                md(t.get("title"), inline=True),
                size_cell(t, human, back), pr_h,
                qs))


def queue(p, human, back):
    """The queue, grouped by what is actually happening to the work: stopped first
    because it wants you, then what is being worked, then what is waiting.

    A single table sorted by priority could not answer "what is going on" without
    reading every row's status column. The groups answer it before you read anything,
    and priority still orders within each one."""
    by_task = {}
    for q in p["questions"] + p["questions_defaulted"]:
        if q.get("task"):
            by_task.setdefault(q["task"], []).append(q)
    shown, html = set(), []
    left = list(p["tasks"])
    # Said BEFORE the landed rows are added, and about the live queue only. Testing
    # `html` at the end instead meant a project whose only rows were `done` never got
    # this line — and since landed is hidden by the default filter, its queue read as a
    # blank space with nothing to say why.
    if not left:
        html.append("<p class='%s mt-2 text-sm'>The queue is empty. Add the next piece of "
                    "work with <code class='rounded bg-base-200 px-1 font-mono'>board task "
                    "create</code>.</p>" % DIM)
    for key, label, statuses in QUEUE_GROUPS:
        rows = [t for t in left if (t.get("status") or "") in statuses]
        if not rows:
            continue
        html.append("<h3 class='grp grp-%s'>%s<span class='%s grp-n'>%d</span></h3>"
                    % (key, label, MONO, len(rows)))
        for i, t in enumerate(rows):
            shown.add(t["id"])
            # The first row of stopped and of in-flight is the one you are most likely to
            # act on; waiting is a queue and its front is its first row.
            html.append(queue_card(t, human, back, by_task.get(t["id"], ()),
                                   big=(i == 0 and key != "waiting")))
    other = [t for t in left if t["id"] not in shown]
    if other:
        html.append("<h3 class='grp'>other<span class='%s grp-n'>%d</span></h3>" % (MONO, len(other)))
        for t in other:
            shown.add(t["id"])
            html.append(queue_card(t, human, back, by_task.get(t["id"], ()), big=False))
    costs = p.get("costs") or {}
    done = [dict(brief(t), cost=costs.get(t["id"])) for t in db.execute(
        "SELECT * FROM tasks WHERE project=? AND status='done' ORDER BY updated DESC LIMIT 30",
        (p["name"],))]
    if done:
        # No data-task on the heading: the row loop counts every [data-task] into the
        # rail's "N shown, M hidden" readout, so the heading reported itself as a task.
        # Empty groups are hidden by the group pass, which needs no marker here.
        html.append("<h3 class='grp grp-landed'>landed<span class='%s grp-n'>%d</span></h3>"
                    % (MONO, len(done)))
        for t in done:
            shown.add(t["id"])
            # Landed rows carry their own questions like any other: a task can be done
            # with a question still open on it, and `shown` then denies it the fallback
            # card too — so it vanished from the board entirely while the API still
            # reported it open. `board task cleanup-done` makes that routine.
            html.append(queue_card(t, human, back, by_task.get(t["id"], ()),
                                   big=False, hidden=True))
    return "".join(html), shown


def limits_row(p, human):
    """The two numbers the owner sets, on one line under the fleet they govern."""
    b = p.get("budget") or {}
    ceilings = {win_name(k): float(v or 0) for k, v in (b.get("ceilings") or {}).items()}
    used = {win_name(k): float(v or 0) for k, v in (b.get("windows") or {}).items()}
    windows = sorted(set(list(ceilings) + list(used)) or {"5h", "7d"})
    over = set(b.get("overridden") or [])
    rows = "".join(gauge(w, used.get(w), ceilings.get(w), p["name"], human, w in over)
                   for w in windows)
    if not over:
        src_txt = "from board-policy.json"
    elif over >= set(windows):
        src_txt = "set here"
    else:
        src_txt = "%s set here" % ", ".join(sorted(over))
    wip, unrev = int(p.get("wip") or 0), int(p.get("unreviewed") or 0)
    if human:
        wip_ctl = ("<form method='POST' action='/projects/%s/ceilings' class='m-0 inline-flex items-baseline gap-1'>"
                   "<input type='hidden' name='project' value='%s'>"
                   "<input class='cell-num %s' type='number' name='wip' min='1' max='99' "
                   "value='%d' data-autosave aria-label='review limit'>"
                   "<button type='submit' class='sr-only-btn'>Set</button></form>"
                   % (escape(p["name"]), escape(p["name"]), MONO, wip))
    else:
        wip_ctl = "<span class='%s'>%d</span>" % (MONO, wip)
    return ("<div class='lim'><div class='lim-head'><span>ceilings</span>"
            "<span class='%s text-xs'>%s</span></div>%s"
            # A DIV and not a P: <form> is not allowed inside <p>, so the browser closed
            # the paragraph at the form's start tag and re-parented it as a sibling —
            # which is why the number sat on a line of its own with no rule able to pull
            # it back. Nothing in the markup showed it; only the live DOM did.
            "<div class='lim-wip'><span class='%s'>waiting for review</span>"
            "<span class='of'><span class='%s'>%d of</span>%s</span></div></div>" % (
                DIM, escape(src_txt), rows, DIM, MONO, unrev, wip_ctl))


def band_head(p, human):
    """The project's name, stuck to the top of the viewport for as long as you are
    inside that project.

    This is the fix for the complaint that started the redesign: with four projects
    stacked as identical accordions, nothing on screen told you which one you were
    reading once its heading had scrolled away. The name now stays, and a rule runs the
    full height of the band beside everything that belongs to it."""
    pause = ("<span class='badge badge-sm badge-error'>paused: %s</span>"
             % escape(p["paused"]) if p.get("paused") else "")
    btn = ""
    if human:
        act, label, cls, confirm = (
            ("resume", "Resume", "badge-success", "")
            if p.get("paused") else
            ("pause", "Pause", "badge-ghost",
             " data-confirm='Pause the project? Agents finish what they hold and take nothing new.'"))
        btn = ("<form method='POST' action='/projects/%s/%s' class='inline m-0'%s>"
               "<input type='hidden' name='project' value='%s'>"
               "<button type='submit' class='badge badge-sm %s cursor-pointer'>%s</button>"
               "</form>" % (escape(p["name"]), act, confirm, escape(p["name"]), cls, label))
    return ("<div class='band-head'><h2 class='band-name'>%s</h2>%s%s%s%s</div>" % (
        escape(p["name"]), phase_control(p, human), tell(p),
        (" " + pause) if pause else "", (" " + btn) if btn else ""))


def html_status(project, token="", human=False):
    s = status(project)
    h = [head("board", "<span class='%s text-xs'>%s</span>" % (
        MONO + " " + DIM, escape(s["generated"][11:16] + " UTC")), (), human)]
    if s.get("ntfy_failures_since_success", 0) > 0:
        fails = s["ntfy_failures_since_success"]
        h.append("<div class='mt-4 rounded-box border border-l-4 border-base-300 "
                 "border-l-error bg-base-200 p-3 text-sm'>"
                 "<b class='font-semibold text-error'>Push notifications are failing:</b> "
                 "%d %s have not gone out since the last one that did. Check NTFY_URL and "
                 "the topic's credentials.</div>" % (
                     fails, "push" if fails == 1 else "pushes"))
    if not s["projects"]:
        h.append("<div class='mt-8 rounded-box border border-dashed border-base-300 "
                 "px-4 py-10 text-center %s'>No projects yet.<br>"
                 "Run <code class='rounded bg-base-200 px-1 font-mono'>board project init</code> "
                 "in a project to put it here.</div>" % DIM)
        return page("board", "".join(h))
    h.append(watchline(s))
    # The night and the quota on one row: what happened, and what is left to spend.
    h.append("<div class='topband'>%s%s</div>" % (
        night_band([e for p in s["projects"] for e in (p.get("ribbon") or [])]),
        fleet_runway(s)))
    h.append(rail(s))
    back = "/status?project=" + urllib.parse.quote(project) if project else "/status"
    for p in sorted(s["projects"], key=last_activity, reverse=True):
        known = sorted(set(GRANT_VOCAB) | {g for a in p["agents"] for g in (a.get("grants") or [])})
        ceilings = {win_name(k): float(v or 0)
                    for k, v in ((p.get("budget") or {}).get("ceilings") or {}).items()}
        h.append("<section class='band pj' data-p='%s'>" % escape(p["name"]))
        h.append(band_head(p, human))
        h.append("<div class='band-body'>")
        if p.get("goal"):
            h.append("<p class='band-goal %s'>%s</p>" % (DIM, md(p["goal"], inline=True)))
        if (p.get("budget") or {}).get("ceilings_missing"):
            h.append("<p class='mt-2 rounded-box border border-l-4 border-base-300 "
                     "border-l-error bg-base-200 p-3 text-sm'>%s</p>"
                     % escape(p["budget"]["note"]))
        rows_html, shown = queue(p, human, back)
        h.append(rows_html)
        h.append("<div class='band-foot'><div class='fleet'>"
                 "<div class='fleet-head'><span>fleet</span>"
                 "<span data-agent-badge class='%s text-xs'></span>"
                 "<form method='POST' action='/agents/cleanup' class='m-0 ml-auto' "
                 "data-confirm='Mark every agent that has been dead for a day as finished?'>"
                 "<input type='hidden' name='project' value='%s'>"
                 "<input type='hidden' name='older_than' value='24h' data-agent-cleanup>"
                 "<button type='submit' class='badge badge-sm badge-ghost cursor-pointer'>"
                 "Clear out dead</button></form></div>" % (DIM, escape(p["name"])))
        if not p["agents"]:
            h.append("<p class='%s text-sm'>No agent has registered here yet. Run "
                     "<code class='rounded bg-base-200 px-1 font-mono'>/next</code> in the "
                     "project to start one.</p>" % DIM)
        for a in p["agents"]:
            h.append(agent_line(a, ceilings, human, p["name"], known))
        h.append("</div>")
        h.append(limits_row(p, human))
        h.append("</div>")
        adrift = lambda q: not q.get("task") or q["task"] not in shown
        loose = [q for q in p["questions"] if adrift(q)]
        loose_d = [q for q in p["questions_defaulted"] if adrift(q)]
        if loose:
            h.append("<p class='%s'>waiting for an answer from you</p>" % LBL)
            h.append("<div class=qgrid>%s</div>" % "".join(q_card(q) for q in loose))
        if loose_d:
            h.append("<p class='%s'>the board answered itself — you can still override it</p>" % LBL)
            h.append("<div class=qgrid>%s</div>"
                     % "".join(q_card(q, q["answer"] or "") for q in loose_d))
        h.append("</div></section>")
    h.append(FOCUS)
    return page("board", "".join(h))


def cost_block(d):
    """What the task has cost so far, and who spent it.

    The estimate is shown against it on purpose: side by side they are a measurement of
    the estimator, which is the only reason a relative size is worth writing down. A
    dispatch still missing its token count is named rather than quietly dropped — the
    sum would otherwise read as a total when it is a floor."""
    ds = d.get("dispatches") or []
    c = d.get("cost") or {}
    if not ds:
        return ("<p class='%s text-sm'>No subagent has reported a cost on this task yet. "
                "The coordinator records one with "
                "<code class='rounded bg-base-200 px-1 font-mono'>board task progress "
                "--dispatch role:model --tokens N</code>.</p>" % DIM)
    rows = "".join(
        "<tr><td class='%s py-1 pr-3 align-top whitespace-nowrap text-xs'>%s"
        "<td class='py-1 pr-3 align-top'>%s<td class='%s py-1 pr-3 align-top'>%s"
        "<td class='%s py-1 pr-3 align-top text-right whitespace-nowrap'>%s"
        "<td class='%s py-1 align-top'>%s" % (
            MONO + " " + DIM, escape((x.get("ts") or "")[:16].replace("T", " ")),
            escape(x.get("role") or ""), MONO, escape(x.get("model") or ""),
            MONO, toks(x["tokens"]) if x.get("tokens") is not None else "—",
            DIM, md(x.get("result"), inline=True))
        for x in ds)
    est = d.get("estimate")
    per = ""
    if est and c.get("tokens") and c.get("complete"):
        per = " <span class='%s'>%s per point</span>" % (DIM, toks(c["tokens"] / float(est)))
    total = ("<p class='mt-2 text-sm'><b class='%s font-semibold'>%s</b> tokens over %d "
             "%s%s%s</p>" % (
                 MONO, toks(c.get("tokens")), c.get("dispatches", 0),
                 "dispatch" if c.get("dispatches") == 1 else "dispatches",
                 "" if c.get("complete") else
                 " <span class='text-warning'>— at least one has not reported its tokens, "
                 "so this is a floor</span>", per))
    return ("<div class='min-w-0 overflow-x-auto'><table class='w-full text-sm'>"
            "<thead><tr class='%s text-xs'><th class='py-1 pr-3 text-left font-semibold'>when"
            "<th class='py-1 pr-3 text-left font-semibold'>role"
            "<th class='py-1 pr-3 text-left font-semibold'>model"
            "<th class='py-1 pr-3 text-right font-semibold'>tokens"
            "<th class='py-1 text-left font-semibold'>result</thead>"
            "<tbody>%s</tbody></table></div>%s" % (DIM, rows, total))


def task_controls(d, human):
    """The two things you do to a task from outside it: take it back, or put it away.

    Both refuse for an agent that does not own the task, and both go through for the
    owner of the board — a task held by an agent that has stopped moving is exactly the
    case a person is looking at this page to fix."""
    if not human:
        return ""
    tid = escape(d["id"])
    rel = ("<form method='POST' action='/t/%s/release' class='m-0' "
           "data-confirm='Put this task back in the queue? Whoever holds it loses it.'>"
           "<input type='hidden' name='back' value='/t/%s'>"
           "<button type='submit' class='btn btn-outline min-h-10'>Put back in the queue</button>"
           "</form>" % (tid, tid)) if d.get("status") not in ("done", "archived") else ""
    arc = ("<form method='POST' action='/t/%s/archive' class='m-0' "
           "data-confirm='Archive this task? It leaves the board and the queue.'>"
           "<input type='hidden' name='back' value='/status'>"
           "<button type='submit' class='btn btn-outline min-h-10'>Archive</button></form>"
           % tid) if d.get("status") != "archived" else ""
    if not (rel or arc):
        return ""
    return "<div class='mt-4 flex flex-wrap gap-3'>%s%s</div>" % (rel, arc)


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
    est = d.get("estimate")
    est_h = "%s%s" % (
        ("<span class='%s'>%s</span>" % (MONO, est)) if est else "—",
        (" <span class='%s'>estimated by %s</span>" % (DIM, escape(d.get("estimate_model") or
                                                                   d.get("estimate_by") or "")))
        if est and (d.get("estimate_model") or d.get("estimate_by")) else "")
    back = "/t/" + urllib.parse.quote(d["id"])
    facts = (("phase", escape(d.get("phase") or "—")),
             ("risk", escape(d.get("risk") or "—")),
             ("priority", prio_cell(d, human, back)),
             ("size", size_cell(d, human, back) if human else est_h),
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
                prose(e["note"], "md-note") if e.get("note") else ""))
    tl = ("<ul class='trail mt-1 list-none border-b border-base-300 p-0 text-sm'>%s</ul>"
          % "".join(evs) if evs else
          "<p class='%s text-sm'>Nothing has happened here yet.</p>" % DIM)
    oa = d.get("owner_agent")
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
                  # the red line is the project's EFFECTIVE ceiling for exactly that
                  # window (T-428 review 55) — a fixed 85 here was the hardcoded
                  # threshold T-164 removes everywhere else
                  "".join(meter(w, pct, (oa.get("effective_ceilings") or {}).get(win_name(w)))
                          for w, pct in sorted((oa.get("budget") or {}).items())),
                  MONO, DIM, escape(oa.get("last_seen") or "—")))
    else:
        ab = "<p class='%s text-sm'>Nobody holds this task.</p>" % DIM
    return page(d["id"], """%s
        <h2 class='mt-5 max-w-[68ch] text-lg font-semibold leading-snug sm:text-2xl'>%s</h2>
        %s
        <dl class='mt-4 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 border-t
          border-base-300 pt-3 text-sm sm:grid-cols-[auto_minmax(0,1fr)_auto_minmax(0,1fr)]'>%s</dl>
        %s
        <p class='%s'>what it has cost</p>%s
        <p class='%s'>timeline</p>%s
        <p class='%s'>the owner's state</p>%s
        <form class='mt-6 max-w-[42rem]' method=post action='/t/%s/comment'>
          <label class='%s' for=comment>Add a comment</label>
          <input id=comment class='input w-full' type=text name=text
            placeholder='what you saw, or what the agent should do next'>
          <button class='btn btn-primary mt-3 min-h-12'>Add comment</button>
        </form>%s""" % (
        head(d["id"], "<span class='badge badge-sm %s'>%s</span>" % (
            BADGE.get(st, "badge-ghost"), escape(st)), human=human),
        md(d.get("title"), inline=True),
        prose(d.get("spec"), "md-spec"),
        "".join("<dt class='%s'>%s<dd class='m-0 [overflow-wrap:anywhere]'>%s" % (DIM, k, v)
                for k, v in facts),
        task_controls(d, human),
        LBL, cost_block(d), LBL, tl, LBL, ab, escape(d["id"]), LBL, FOCUS))


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
          <div class='md md-lead'>%s</div>
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
        md(q["text"]), qid, opts, LBL, foot))


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
    ("PATCH",  r"/tasks/([^/]+)$",              lambda h, m, b, q: task_patch(m[0], actor_h(q, b), b)),
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
    ("POST",   r"/tasks/([^/]+)/archive$",      lambda h, m, b, q: task_archive(m[0], actor_h(q, b), b)),
    ("POST",   r"/tasks/cleanup$",              lambda h, m, b, q: tasks_cleanup(
        (b or {}).get("project") or q.get("project", [None])[0],
        (b or {}).get("older_than") or q.get("older_than", ["0"])[0], b)),
    ("POST",   r"/tasks/([^/]+)/release$",      lambda h, m, b, q: release(m[0], actor_h(q, b), b)),
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
    ("POST",   r"/projects/([^/]+)/ceilings$", lambda h, m, b, q: set_ceilings(m[0], b)),
    ("POST",   r"/projects/([^/]+)/phase$",    lambda h, m, b, q: set_phase(m[0], b)),
    ("GET",    r"/status$",                     lambda h, m, b, q: status(q.get("project", [None])[0])),
    ("GET",    r"/events$",                     lambda h, m, b, q: events(q)),
]
ROUTES = [(mth, re.compile("^" + API + pat), fn) for mth, pat, fn in ROUTES]


# A path on this board and nothing else: leading "/", not "//" (that is a URL with a
# host), no backslash (browsers read it as "/"), and no control character at all.
# http.server's send_header does NOT sanitize — VERIFIED: it wrote
# "Location: /status\r\nX-Injected: yes" out verbatim — so a CR or LF here is a header
# the caller got to write. SameSite=Lax and form-action 'self' make that hard to reach
# from outside, which is a reason it stayed unnoticed, not a reason to allow it.
BACK_OK = re.compile(r"^/(?!/)[^\\\x00-\x1f\x7f]*$")


def need(b, key):
    """A form field the handler cannot do without. `b[key]` raises KeyError, which the
    request handler turns into a 400 with no idea what was missing; this says which."""
    v = (b or {}).get(key)
    if not v:
        raise Err(400, "%s is required" % key)
    return v


def back_to(b, dflt):
    """Where a form on the board returns to.

    Every control exists on more than one page now — a priority can be changed from the
    queue and from the task itself — and coming back to the wrong one costs a scroll and
    a lost place in a list of forty tasks. The page says where it was; anything that is
    not a path on this board is dropped for the default rather than followed."""
    v = (b or {}).get("back") or ""
    return v if BACK_OK.match(v) else dflt


def proj_back(b, res):
    p = (b or {}).get("project") or (res or {}).get("project")
    return back_to(b, "/" if not p else "/status?project=" + urllib.parse.quote(p))


# The pages POST here. Same handlers as the API, but the reply is a redirect to the page
# the press came from, because the caller is a form in a browser and not a client that
# reads JSON. A table, because this was eight copies of the same six lines and every new
# control meant a ninth — which is how the grant form ended up being the only control on
# the board for a year.
UI_POST = [
    (r"/agents/cleanup$",
     lambda m, b, q: agents_cleanup(b.get("project"), b.get("older_than", "24h"), b), proj_back),
    (r"/tasks/cleanup$",
     lambda m, b, q: tasks_cleanup(b.get("project"), b.get("older_than", "0"), b), proj_back),
    (r"/agents/([^/]+)/grants$",
     lambda m, b, q: agent_grant_add(m.group(1), b), proj_back),
    (r"/agents/([^/]+)/grants/revoke$",
     lambda m, b, q: agent_grant_del(m.group(1), (b or {}).get("grant"), b, q), proj_back),
    (r"/projects/([^/]+)/pause$",
     lambda m, b, q: set_paused(m.group(1), b, True), proj_back),
    (r"/projects/([^/]+)/resume$",
     lambda m, b, q: set_paused(m.group(1), b, False), proj_back),
    (r"/projects/([^/]+)/ceilings$",
     lambda m, b, q: set_ceilings(m.group(1), b), proj_back),
    (r"/projects/([^/]+)/phase$",
     lambda m, b, q: set_phase(m.group(1), b), proj_back),
    # .get(), like every other entry here: a form that posts without them must answer
    # 400, not raise a KeyError into the 500 handler.
    (r"/roles/([^/]+)/pin$",
     lambda m, b, q: role_pin(need(b, "project"), m.group(1), need(b, "agent"), HUMAN, b),
     proj_back),
    (r"/roles/([^/]+)/unpin$",
     lambda m, b, q: role_unpin(need(b, "project"), m.group(1), b), proj_back),
    (r"/t/([^/]+)/patch$",
     lambda m, b, q: task_patch(m.group(1), actor_h(q, b), b),
     lambda b, res: back_to(b, "/t/" + urllib.parse.quote(res.get("id", "")))),
    (r"/t/([^/]+)/release$",
     lambda m, b, q: dict(release(m.group(1), actor_h(q, b), b), id=m.group(1)),
     lambda b, res: back_to(b, "/t/" + urllib.parse.quote(res.get("id", "")))),
    (r"/t/([^/]+)/archive$",
     lambda m, b, q: dict(task_archive(m.group(1), actor_h(q, b), b), id=m.group(1)),
     lambda b, res: back_to(b, "/t/" + urllib.parse.quote(res.get("id", "")))),
]
UI_POST = [(re.compile("^" + pat), fn, loc) for pat, fn, loc in UI_POST]


def actor(q, b):
    a = (b or {}).get("agent") or q.get("agent", [None])[0]
    if not a:
        raise Err(400, "agent is missing (set BOARD_AGENT_ID)")
    return a


def actor_h(q, b):
    """Like actor(), for the calls a person can make as themselves.

    The human token is an identity, not a permission bit: the owner pressing a button on
    the board has no BOARD_AGENT_ID and never will. Falling back to HUMAN here is what
    lets one handler serve both the agent's API call and the form on the phone, with the
    identity written into the event log either way."""
    a = (b or {}).get("agent") or q.get("agent", [None])[0]
    if a:
        return a
    if as_human(b or {}):
        return HUMAN
    raise Err(400, "agent is missing (set BOARD_AGENT_ID)")


def as_human(b):
    """Is this call proven to come from the human? Only if it carried the human token."""
    return bool(b.get("_human"))


def human_only(b, what):
    if not as_human(b):
        raise Err(403, "%s requires the human token (BOARD_HUMAN_TOKEN). An agent cannot "
                       "act as %s — that is the entire point of §3.7." % (what, HUMAN),
                  # `what` on its own, so the refusal PAGE can name the action in a
                  # sentence of its own instead of quoting a paragraph about tokens.
                  needs_human_token=True, action=what)


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


def set_ceilings(project, b):
    """Move a quota ceiling, or the review limit, from the board itself.

    Human-only, and that is the whole design: DESIGN.md §3.7 and CONTRIBUTING.md both say
    an agent must never be able to decide its own quota. The gauge is draggable for the
    owner and read-only for everyone else, enforced HERE — not in the markup, which an
    agent with a token could simply POST past.

    A window is cleared by sending it empty, which puts board-policy.json back in charge
    of that window. Clearing them all removes the override entirely."""
    human_only(b or {}, "setting a ceiling")
    ensure_project(project)
    b = b or {}
    cur = ceilings_override(project)
    changed = {}
    for k, v in b.items():
        if not k.startswith("ceiling."):
            continue
        w = win_name(k[len("ceiling."):])
        if not w:
            continue
        if v in (None, ""):
            cur.pop(w, None)
            changed[w] = None
            continue
        pct = num(v)
        if pct is None or pct < 0 or pct > 100:
            raise Err(400, "a ceiling is a percentage between 0 and 100, not %r" % v)
        cur[w] = round(float(pct), 1)
        changed[w] = cur[w]
    if "wip" in b:
        if b["wip"] in (None, ""):
            db.execute("UPDATE projects SET wip=NULL, updated=? WHERE name=?", (now(), project))
            changed["wip"] = None
        else:
            try:
                w = int(b["wip"])
            except (TypeError, ValueError):
                raise Err(400, "the review limit is a whole number of tasks")
            if w < 1:
                raise Err(400, "a review limit under 1 stops every task before review")
            db.execute("UPDATE projects SET wip=?, updated=? WHERE name=?", (w, now(), project))
            changed["wip"] = w
    if not changed:
        raise Err(400, "nothing to set (ceiling.<window>, wip)")
    if any(k != "wip" for k in changed):
        db.execute("UPDATE projects SET ceilings=?, updated=? WHERE name=?",
                   (json.dumps(cur) if cur else None, now(), project))
    ev(project, "project", "project.ceilings_set", HUMAN, **changed)
    return {"project": project, "ceilings": ceilings_override(project),
            "wip": wip_limit(project), "budget": budget(project)}


def set_phase(project, b):
    """The phase drives the merge gate and whether a prod deploy is allowed, so it is the
    owner's to move (DESIGN.md §10) — `board project init` may declare one for a NEW
    project, but changing it afterwards is a human action."""
    human_only(b or {}, "changing the phase")
    phase = (b or {}).get("phase")
    if phase not in PHASES:
        raise Err(400, "unknown phase %r (%s)" % (phase, "|".join(PHASES)))
    ensure_project(project)
    db.execute("UPDATE projects SET phase=?, updated=? WHERE name=?", (phase, now(), project))
    ev(project, "project", "project.phase_set", HUMAN, phase=phase)
    return {"project": project, "phase": phase}


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
        # `t.id = substr(e.stream, 6)` and not `e.stream = 'task/' || t.id`: the second
        # form builds the key on the TASKS side, so no index on tasks.id can be used and
        # every matching event re-scans the whole table. Measured on a 42k-event log, per
        # event type and with this endpoint holding the global lock: 243 ms → 10 ms. The
        # results are identical — a stream that is not a task ("agent/…", "project")
        # matches nothing either way and the LEFT JOIN yields NULL, which is what
        # COALESCE is already there for.
        rows = db.execute(
            "SELECT e.project, COALESCE(t.repo, json_extract(e.body, '$.repo'), '') AS repo_val, COUNT(*) AS cnt "
            "FROM events e LEFT JOIN tasks t ON t.id = substr(e.stream, 6) "
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
    #
    # ONE pass over the dispatch events, feeding both the per-role counters below and the
    # per-task fold that 9b needs. It used to be two full scans of the event log, and
    # this endpoint holds the GLOBAL LOCK while it runs — measured at 42k events (the
    # live board's own size) the second scan took the scrape from 545 ms to 1035 ms, and
    # every agent call waits behind it for as long as it lasts.
    #
    # The two readings are deliberately different and both are wanted: the counters below
    # count dispatch EVENTS (SKILL.md logs one before and one after, so a completed
    # dispatch is two), while cost_of folds those two back into one costed dispatch.
    # Changing the counters to the folded number would silently redefine a metric Grafana
    # already draws, so they stay as they are.
    dispatches = {}
    dispatch_tokens = {}
    per_task = {}
    for r in db.execute("SELECT project, stream, ts, actor, body FROM events "
                        "WHERE type='task.progress' "
                        "AND json_extract(body, '$.dispatch') IS NOT NULL ORDER BY id"):
        b = jl(r["body"], {})
        d = b.get("dispatch")
        if not d:
            continue
        per_task.setdefault(r["stream"].split("/", 1)[-1], []).append(r)
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
    costs = {tid: cost_of(dispatches_of(rows)) for tid, rows in per_task.items()}

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

    # 9b. Estimate against actual — the pair of series that answers "which model sizes
    # its own work well". Labelled by the model that GAVE the estimate, never by the one
    # that did the work: they are usually different agents, and conflating them makes
    # both numbers unreadable. Cardinality is bounded on purpose — six estimate values,
    # a handful of models — so this stays a label and never becomes a per-task series.
    #
    # Only tasks whose cost is COMPLETE contribute tokens (cost_of: every dispatch has
    # its number). A task still missing one would drag the average down for a model that
    # did nothing wrong but forget a --tokens flag.
    est_n, est_tok, est_costed, est_points = {}, {}, {}, {}
    for r in db.execute("SELECT id, project, status, estimate, estimate_model FROM tasks "
                        "WHERE estimate IS NOT NULL"):
        key = (r["project"] or "", r["estimate_model"] or "unknown", str(r["estimate"]))
        est_n[key] = est_n.get(key, 0) + 1
        est_points[(r["project"] or "", r["status"] or "")] = (
            est_points.get((r["project"] or "", r["status"] or ""), 0) + int(r["estimate"]))
        c = costs.get(r["id"])
        if c and c["complete"] and c["dispatches"]:
            est_tok[key] = est_tok.get(key, 0) + c["tokens"]
            est_costed[key] = est_costed.get(key, 0) + 1
    add_metric("board_tasks_estimated_total", "gauge", [
        'board_tasks_estimated_total{project="%s",estimate_model="%s",estimate="%s"} %d' % (
            prom_esc(p), prom_esc(m), prom_esc(e), n)
        for (p, m, e), n in sorted(est_n.items())])
    # gauge, not counter: these are recomputed absolutes carrying an `estimate` label.
    # Re-sizing a task moves its whole token sum from estimate="5" to estimate="8", and a
    # new dispatch logged without --tokens drops the task out of the series altogether
    # (conformance asserts exactly that). rate() over either would read those as counter
    # resets — the dashboard only divides raw values, so the TYPE line was a trap rather
    # than a visible fault.
    add_metric("board_task_estimate_tokens_total", "gauge", [
        'board_task_estimate_tokens_total{project="%s",estimate_model="%s",estimate="%s"} %d' % (
            prom_esc(p), prom_esc(m), prom_esc(e), n)
        for (p, m, e), n in sorted(est_tok.items())])
    add_metric("board_tasks_estimate_costed_total", "gauge", [
        'board_tasks_estimate_costed_total{project="%s",estimate_model="%s",estimate="%s"} %d' % (
            prom_esc(p), prom_esc(m), prom_esc(e), n)
        for (p, m, e), n in sorted(est_costed.items())])
    add_metric("board_task_estimate_points", "gauge", [
        'board_task_estimate_points{project="%s",status="%s"} %d' % (
            prom_esc(p), prom_esc(st), n)
        for (p, st), n in sorted(est_points.items())])

    # 9c. What a task actually cost, by the model that did the work and by the task's
    # own size. This is the other half of the question: not "did we guess right" but
    # "what does this model cost per unit of work".
    model_tok = {}
    if costs:
        marks = {r["id"]: r for r in db.execute(
            "SELECT id, project, estimate FROM tasks WHERE id IN (%s)"
            % ",".join("?" * len(costs)), list(costs))}
        for tid, c in costs.items():
            r = marks.get(tid)
            if not r:
                continue
            for model, tok in c["by_model"].items():
                k = (r["project"] or "", model, str(r["estimate"]) if r["estimate"] else "")
                model_tok[k] = model_tok.get(k, 0) + tok
    add_metric("board_task_tokens_total", "gauge", [
        'board_task_tokens_total{project="%s",model="%s",estimate="%s"} %d' % (
            prom_esc(p), prom_esc(m), prom_esc(e), n)
        for (p, m, e), n in sorted(model_tok.items())])

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
                         "script-src %s %s; form-action 'self'" % (THEME_SHA, FOCUS_SHA))
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
        for pat, fn, loc in UI_POST:
            mm = pat.match(path)
            if not (mm and method == "POST"):
                continue
            # A refused human action is a PAGE here, not a JSON blob. These are buttons a
            # person presses on a phone, and `{"needs_human_token": true}` on a white
            # screen tells them nothing they can act on (T-390).
            try:
                res = fn(mm, body or {}, q)
            except Err as e:
                if e.body.get("needs_human_token") and self.headers.get("accept") != "application/json":
                    return self.send(403, not_human_page(e.body.get("action", "that")),
                                     "text/html")
                raise
            if self.headers.get("accept") == "application/json":
                return self.send(200, res)
            return self.send(302, "", "text/html",
                             extra=[("Location", loc(body or {}, res if isinstance(res, dict) else {}))])
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
