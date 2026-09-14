#!/usr/bin/env python3
"""graph — a knowledge graph over memories, skills and project documents.

The graph is *derived*, not written: `graph.py index` builds it from the corpus on disk.
That leaves no sync problem, no migrations, and the index can be deleted at any time.
The sources own the truth; this is a projection — the same principle as the board
(DESIGN.md §3.4).

  graph.py index [--root DIR ...]     rebuild the index
  graph.py near <slug> [--hops 2]     what is connected to this
  graph.py dangling                   links to something that has not been written yet
  graph.py orphans                    nodes nobody points at
  graph.py find <word>                free text over title/description/body
  graph.py stats | selftest

GRAPH_ROOTS (colon-separated) chooses what is indexed; the default is ~/.claude.
"""
import json, os, re, sqlite3, sys

DB = os.environ.get("GRAPH_DB", os.path.expanduser("~/.cache/graph.db"))
# What to index. Paths, colon-separated, like $PATH — this is per-machine configuration,
# so it must not be a list of one person's directories baked into the source.
ROOTS = [os.path.expanduser(p) for p in
         os.environ.get("GRAPH_ROOTS", "~/.claude").split(":") if p]
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
LINK = re.compile(r"\[\[([^\]|]+?)\]\]")
FENCE = re.compile(r"^\s*(```|~~~)")

SCHEMA = """
CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, title TEXT, path TEXT, project TEXT,
                    type TEXT, description TEXT, body TEXT, dupes TEXT);
CREATE TABLE edges (src TEXT, dst TEXT, kind TEXT, PRIMARY KEY (src, dst, kind));
CREATE INDEX edges_dst ON edges(dst);
"""


def strip_code(text):
    """Links inside code blocks are not links — bash's [[ -f x ]] is not a node."""
    out, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out.append(re.sub(r"`[^`]*`", "", line))
    return "\n".join(out)


def frontmatter(text):
    """Minimal YAML: only `key: value` and one level under `metadata:`. The files are
    machine-written."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fm, rest = {}, text[end + 4:]
    for line in text[3:end].splitlines():
        m = re.match(r"^(\s*)([a-z_]+):\s*(.*)$", line)
        if m:
            fm[m.group(2)] = m.group(3).strip().strip('"')
    return fm, rest


def links_in(text):
    return {t.strip() for t in LINK.findall(strip_code(text)) if SLUG.match(t.strip())}


def classify(path):
    if "/memory/" in path:
        return "memory"
    if "/skills/" in path:
        return "skill"
    return "doc"


def project_of(path):
    m = re.search(r"/projects/([^/]+)/", path)
    if m:
        # Claude Code encodes a project directory as its path with "/" → "-":
        # "-home-alice-prog-foo" → "prog/foo". Strip the user's own $HOME rather than a
        # hardcoded one, so the label is the path relative to home on any machine.
        enc = m.group(1).lstrip("-")
        home = os.path.expanduser("~").strip("/").replace("/", "-") + "-"
        return enc[len(home):].replace("-", "/") if enc.startswith(home) \
            else enc.replace("-", "/")
    return os.path.basename(os.path.dirname(path))


def walk(roots):
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
            for f in files:
                if f.endswith(".md"):
                    yield os.path.join(dirpath, f)


def index(roots, board=True):
    if os.path.exists(DB):
        os.remove(DB)
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    seen = {}
    for path in walk(roots):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        fm, body = frontmatter(text)
        slug = fm.get("name") or os.path.splitext(os.path.basename(path))[0]
        if not SLUG.match(slug):
            slug = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")
        kind = classify(path)
        if slug in seen:                     # same name in two namespaces: keep both paths
            db.execute("UPDATE nodes SET dupes = COALESCE(dupes,'') || ? WHERE id=?",
                       ("\n" + path, slug))
        else:
            seen[slug] = path
            db.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,NULL)",
                       (slug, kind, (re.search(r"^#\s+(.+)", body, re.M) or [None, slug])[1]
                        if re.search(r"^#\s+(.+)", body, re.M) else slug,
                        path, project_of(path), fm.get("type"), fm.get("description"), body[:4000]))
        for dst in links_in(body):
            db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,'link')", (slug, dst))
    db.commit()
    got = ingest_board(db) if board else 0
    if got:
        print("  board: %d events" % got)
    db.commit()
    n = db.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]
    e = db.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
    print("%d nodes, %d edges → %s" % (n, e, DB))
    return db


def ingest_board(db):
    """The board's event log IS an edge list: agent → task → PR → question. We read it
    into the same graph as documents and memories, so one query can go from a task to the
    file it changed to the decision that justified it."""
    import subprocess
    # /events has LIMIT 500 and returns the OLDEST first, so a single round with since=0
    # never sees what just happened. We page on the last id until the log is empty.
    events, since = [], "0"
    try:
        while True:
            raw = subprocess.run(["board", "tail", "--since", since], capture_output=True,
                                 text=True, timeout=20).stdout
            batch = json.loads(raw).get("events", [])
            if not batch:
                break
            events += batch
            since = str(batch[-1]["id"])
            if len(batch) < 500:
                break
    except Exception as e:
        print("  (skipped the board: %s)" % e)
        return 0
    n = 0
    for e in events:
        stream, actor, body = e["stream"], e["actor"], e.get("body", {})
        for ident, kind, desc in ((stream, "stream", e["type"]), ("agent:" + actor, "agent", None)):
            db.execute("INSERT OR IGNORE INTO nodes VALUES (?,?,?,?,?,?,?,?,NULL)",
                       (ident, kind, ident, "", e["project"], e["type"], desc, ""))
        db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?)",
                   ("agent:" + actor, stream, e["type"]))
        # an edge to the file/PR the event mentions, so the graph crosses into the code
        for field in ("pr", "sha", "worktree", "branch"):
            if body.get(field):
                tgt = "%s:%s" % (field, body[field])
                db.execute("INSERT OR IGNORE INTO nodes VALUES (?,?,?,?,?,?,?,?,NULL)",
                           (tgt, field, tgt, "", e["project"], field, None, ""))
                db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?)", (stream, tgt, field))
        n += 1
    return n


def conn():
    if not os.path.exists(DB):
        sys.exit("no index — run: graph.py index")
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db


def near(slug, hops=2):
    """Traversal in both directions. This is the query a graph actually exists for."""
    db = conn()
    rows = db.execute("""
      WITH RECURSIVE reach(id, dist, via) AS (
        SELECT ?, 0, ''
        UNION
        SELECT CASE WHEN e.src = r.id THEN e.dst ELSE e.src END, r.dist + 1,
               CASE WHEN e.src = r.id THEN '→' ELSE '←' END
        FROM reach r JOIN edges e ON (e.src = r.id OR e.dst = r.id)
        WHERE r.dist < ?)
      SELECT reach.id, MIN(dist) d, via, nodes.kind, nodes.project, nodes.description
      FROM reach LEFT JOIN nodes ON nodes.id = reach.id
      WHERE dist > 0 AND reach.id <> ?1 GROUP BY reach.id ORDER BY d, reach.id""",
                      (slug, hops))
    hit = db.execute("SELECT * FROM nodes WHERE id=?", (slug,)).fetchone()
    print("%s  %s" % (slug, ("[%s, %s]" % (hit["kind"], hit["project"])) if hit else "(not written yet)"))
    if hit and hit["description"]:
        print("  %s" % hit["description"])
    for r in rows:
        mark = "" if r["kind"] else "  ⚠ not written"
        print("  %s%s %-38s %s%s" % ("  " * (r["d"] - 1), r["via"], r["id"],
                                     r["description"] or r["project"] or "", mark))


def dangling():
    for r in conn().execute("""SELECT e.dst, COUNT(*) n, GROUP_CONCAT(e.src, ', ') srcs
                               FROM edges e LEFT JOIN nodes ON nodes.id = e.dst
                               WHERE nodes.id IS NULL GROUP BY e.dst ORDER BY n DESC"""):
        print("%-38s ←%d  %s" % (r["dst"], r["n"], r["srcs"]))


def orphans():
    for r in conn().execute("""SELECT n.id, n.kind, n.project, n.description FROM nodes n
                               LEFT JOIN edges e ON e.dst = n.id WHERE e.dst IS NULL
                               AND n.kind='memory' ORDER BY n.project, n.id"""):
        print("%-38s %-22s %s" % (r["id"], r["project"], r["description"] or ""))


def find(term):
    for r in conn().execute("""SELECT id, kind, project, description FROM nodes
                               WHERE id LIKE ?1 OR description LIKE ?1 OR body LIKE ?1
                               ORDER BY kind, id""", ("%" + term + "%",)):
        print("%-38s %-8s %-20s %s" % (r["id"], r["kind"], r["project"], r["description"] or ""))


def stats():
    db = conn()
    for q, label in [("SELECT kind k, COUNT(*) c FROM nodes GROUP BY kind", "nodes"),
                     ("SELECT 'links' k, COUNT(*) c FROM edges", "edges")]:
        for r in db.execute(q):
            print("%-10s %s" % (r["k"], r["c"]))
    d = db.execute("SELECT COUNT(DISTINCT dst) c FROM edges e LEFT JOIN nodes n ON n.id=e.dst "
                   "WHERE n.id IS NULL").fetchone()["c"]
    o = db.execute("SELECT COUNT(*) c FROM nodes n LEFT JOIN edges e ON e.dst=n.id "
                   "WHERE e.dst IS NULL AND n.kind='memory'").fetchone()["c"]
    print("unwritten %s\norphans %s" % (d, o))


def selftest():
    import tempfile
    global DB
    d = tempfile.mkdtemp()
    DB = os.path.join(d, "t.db")
    os.makedirs(d + "/memory", exist_ok=True)
    open(d + "/memory/a.md", "w").write(
        "---\nname: a\ndescription: the first\nmetadata:\n  type: project\n---\n\nSee [[b]].\n"
        "```bash\nif [[ -f x ]]; then :; fi\n```\nAnd `[[inline-code]]` does not count.\n")
    open(d + "/memory/b.md", "w").write("---\nname: b\n---\nPoints on to [[c]].\n")
    index([d], board=False)          # the selftest must be hermetic
    db = conn()
    ids = {r["id"] for r in db.execute("SELECT id FROM nodes")}
    assert ids == {"a", "b"}, ids
    edges = {(r["src"], r["dst"]) for r in db.execute("SELECT src, dst FROM edges")}
    assert edges == {("a", "b"), ("b", "c")}, edges          # code block and inline code filtered out
    assert db.execute("SELECT description FROM nodes WHERE id='a'").fetchone()[0] == "the first"
    assert db.execute("SELECT type FROM nodes WHERE id='a'").fetchone()[0] == "project"
    reach = db.execute("""WITH RECURSIVE reach(id,dist) AS (SELECT 'a',0 UNION
        SELECT CASE WHEN e.src=r.id THEN e.dst ELSE e.src END, r.dist+1
        FROM reach r JOIN edges e ON (e.src=r.id OR e.dst=r.id) WHERE r.dist<2)
        SELECT id FROM reach WHERE dist>0 AND id<>'a'""").fetchall()
    assert {r["id"] for r in reach} == {"b", "c"}, [r["id"] for r in reach]  # two hops, both directions
    print("selftest OK")


if __name__ == "__main__":
    args = sys.argv[1:] or ["stats"]
    cmd, rest = args[0], args[1:]
    roots = [rest[i + 1] for i, a in enumerate(rest) if a == "--root"] or ROOTS
    hops = int(next((rest[i + 1] for i, a in enumerate(rest) if a == "--hops"), 2))
    pos = [a for i, a in enumerate(rest)
           if not a.startswith("--") and (i == 0 or rest[i - 1] not in ("--root", "--hops"))]
    {"index": lambda: index(roots), "near": lambda: near(pos[0], hops), "dangling": dangling,
     "orphans": orphans, "find": lambda: find(pos[0]), "stats": stats,
     "selftest": selftest}.get(cmd, lambda: sys.exit(__doc__))()
