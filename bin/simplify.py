#!/usr/bin/env python3
"""simplify.py — consolidate and tidy tasks on the coordination board.

A long-running loop fragments its own queue: one finding becomes one task, and twenty
small tasks in the same file cost twenty review-and-merge cycles instead of one. This
command finds groups that belong together and turns each group into a single task whose
spec is the concatenation of the originals.

Two rules are built in and need no configuration:

  retracted     tasks a human withdrew          -> closed and archived
  verification  follow-ups the board itself made (an overridden default, or the legacy
                "FAIL from human test" of the removed test stage) -> one task

Everything else is DOMAIN knowledge and therefore configuration, not code: which repos a
project has, which words mark a cluster, what the consolidated task should be called.
Those rules live in a rules file, so this script stays the same across projects.

  simplify-rules.yaml   next to project.yaml, or $BOARD_SIMPLIFY_RULES

See simplify-rules.example.yaml for the schema.

Usage:
  board simplify [--project <p>] [--apply] [--json]
  board simplify --archive-done
"""
import argparse, json, os, re, subprocess, sys

try:
    import yaml
except ImportError:                                   # rules are optional; the built-ins are not
    yaml = None

# Markers the board itself writes (board.py: question_answer). They are matched here so
# that the built-in verification rule keeps working without any configuration.
BOARD_FAIL_MARKER = "FAIL from human test"
BOARD_DEFAULT_MARKER = "overridden default on"


def run_board(args, stdin=None):
    cmd = ["board"] + args
    res = subprocess.run(cmd, input=stdin, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError("board %s failed (exit %d): %s" % (
            " ".join(args), res.returncode, res.stderr.strip() or res.stdout.strip()))
    return res.stdout


def get_tasks(project=None):
    args = ["task", "list", "--json"]
    if project:
        args.extend(["--project", project])
    data = json.loads(run_board(args))
    return data.get("tasks", []) if isinstance(data, dict) else data


def manifest():
    try:
        return json.loads(run_board(["project", "show"]))
    except Exception:
        return {}


# ---------- rules ---------------------------------------------------------

DEFAULT_RULES = {
    # A withdrawn task is one a human took back. The words a human uses are the human's
    # own, in the human's own language, so the markers are configuration with a neutral
    # default rather than a fixed list.
    "retracted": {"title_any": ["RETRACTED", "WITHDRAWN"]},
    "verification": {"title_any": [BOARD_FAIL_MARKER, BOARD_DEFAULT_MARKER],
                     "title": "consolidated verification of {n} follow-ups ({ids})",
                     "prefix": "consolidated verification",
                     "priority": 60},
    "clusters": [],
}


def rules_path(root):
    env = os.environ.get("BOARD_SIMPLIFY_RULES")
    if env:
        return env if os.path.exists(env) else None
    for name in ("simplify-rules.yaml", "simplify-rules.yml"):
        p = os.path.join(root or ".", name)
        if os.path.exists(p):
            return p
    return None


def load_rules(root):
    """Built-in defaults, with the project's own rules file merged over them. A missing
    file is the normal case: the built-in rules are useful on their own, and a
    project only writes a file when it has clusters of its own to declare."""
    out = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in DEFAULT_RULES.items()}
    p = rules_path(root)
    if not p:
        return out, None
    if yaml is None:
        sys.exit("%s exists but PyYAML is not installed" % p)
    user = yaml.safe_load(open(p, encoding="utf-8")) or {}
    for key in ("retracted", "verification"):
        if isinstance(user.get(key), dict):
            out[key].update(user[key])
    if isinstance(user.get("clusters"), list):
        out["clusters"] = user["clusters"]
    return out, p


def matches(task, match):
    """One cluster's match block. Every key given must hold; keys not given do not
    constrain. `title_any` and `text_any` are case-insensitive substrings — a cluster is
    a rule of thumb a human wrote down, not a parser."""
    if not match:
        return False
    if "repo" in match and (task.get("repo") or "") != match["repo"]:
        return False
    if "ids" in match and task["id"] in [str(i) for i in match["ids"]]:
        return True
    title = (task.get("title") or "").lower()
    text = (title + " " + (task.get("spec") or "")).lower()
    if "title_any" in match:
        if any(w.lower() in title for w in match["title_any"]):
            return True
    if "text_any" in match:
        if any(w.lower() in text for w in match["text_any"]):
            return True
    # A match block with only `repo` means "every open task in this repo".
    return "title_any" not in match and "text_any" not in match and "ids" not in match


def analyze(tasks, rules):
    # Planned tasks stay as they are. Merging one that others wait `after` would archive
    # it, and archived counts as landed — the dependants would start before the work they
    # wait for. And the owner's own tasks are not the fleet's to consolidate.
    waited = {x for t in tasks for x in json.loads(t.get("after") or "[]")}
    claimable = [t for t in tasks if t.get("status") in ("open", "orphaned")
                 and not t.get("human") and not t.get("after") and t["id"] not in waited]
    used, groups = set(), []

    def take(name, spec, predicate):
        hits = [t for t in claimable if t["id"] not in used and predicate(t)]
        used.update(t["id"] for t in hits)
        groups.append(dict(spec, name=name, tasks=hits))

    markers = [m.lower() for m in rules["retracted"].get("title_any", [])]
    take("retracted", {"action": "close"},
         lambda t: any(m in (t.get("title") or "").lower() for m in markers))

    vmarkers = [m.lower() for m in rules["verification"].get("title_any", [])]
    take("verification", rules["verification"],
         lambda t: any(m in (t.get("title") or "").lower() for m in vmarkers))

    for c in rules["clusters"]:
        take(c.get("name", "cluster"), c, lambda t, c=c: matches(t, c.get("match")))

    return {"open_count": len(claimable), "groups": groups}


# ---------- reporting -----------------------------------------------------

def fmt(template, tasks):
    ids = [t["id"] for t in tasks]
    shown = ", ".join(ids[:6]) + ("…" if len(ids) > 6 else "")
    return template.format(n=len(tasks), ids=shown)


def format_report(analysis, project, rules, rules_file):
    out = ["# Task analysis for project: %s" % project,
           "Open or unowned tasks: %d" % analysis["open_count"],
           "Rules: %s" % (rules_file or "built-in only"), ""]
    n = 0
    for g in analysis["groups"]:
        if not g["tasks"] or (g["name"] != "retracted" and len(g["tasks"]) < 2):
            continue
        n += 1
        if g["name"] == "retracted":
            out.append("## %d. Retracted tasks to close (%d)" % (n, len(g["tasks"])))
        else:
            out.append("## %d. Cluster: %s (%d)" % (n, g["name"], len(g["tasks"])))
        for t in g["tasks"]:
            out.append("- **%s** (prio %s): %s" % (t["id"], t.get("priority"), t.get("title")))
        if g["name"] != "retracted":
            out += ["", "### Proposal:",
                    "- **Title:** " + fmt(g.get("title", g["name"] + " ({ids})"), g["tasks"]),
                    "- **Repo:** %s" % (g.get("repo") or "—"),
                    "- **Priority:** %s" % g.get("priority", 50)]
            if g.get("benefit"):
                out.append("- **Why:** %s" % g["benefit"])
        out.append("")

    if n == 0:
        out.append("No obvious clusters or redundant tasks found.")
    else:
        out += ["---", "Run `board simplify --apply` to carry this out on the board."]
    return "\n".join(out)


# ---------- applying ------------------------------------------------------

def find_existing(project, prefix):
    """An earlier run already made this consolidation. Reuse it rather than creating a
    second one — `simplify` is meant to be run repeatedly, and two consolidation tasks for
    the same cluster is worse than the fragmentation it set out to fix."""
    for t in get_tasks(project):
        if t.get("status") == "open" and prefix and prefix in (t.get("title") or ""):
            return t.get("id")
    return None


def close_task(tid, note):
    run_board(["heartbeat"])
    if '"error"' in run_board(["task", "claim", tid]):
        raise RuntimeError("could not claim %s" % tid)
    run_board(["task", "progress", tid, note])
    run_board(["task", "done", tid, "--no-merge"])
    run_board(["task", "archive", tid, note])


def consolidate(group, project):
    tasks = group["tasks"]
    if len(tasks) <= 1:
        return []
    actions = []
    title = fmt(group.get("title", group["name"] + " ({ids})"), tasks)
    prefix = group.get("prefix") or group["name"]

    new_id = find_existing(project, prefix)
    if new_id:
        actions.append("Reusing existing consolidation task %s" % new_id)
    else:
        # The sub-task specs are carried over verbatim. A consolidation that drops them
        # would lose exactly what makes each sub-task doable.
        spec = ["# %s\n" % title,
                "This task consolidates and replaces the following sub-tasks:\n"]
        for t in tasks:
            spec.append("### %s: %s" % (t["id"], t.get("title")))
            try:
                info = json.loads(run_board(["task", "show", t["id"], "--json"]))
                s = info.get("spec") or ""
                if s and not s.startswith("("):
                    spec.append(s + "\n")
            except Exception:
                pass
        cmd = ["task", "create", "--title", title,
               "--priority", str(group.get("priority", 50)), "--spec-file", "-"]
        if group.get("repo"):
            cmd += ["--repo", group["repo"]]
        try:
            new_id = json.loads(run_board(cmd, stdin="\n".join(spec))).get("id")
            actions.append("Created consolidation task %s: '%s'" % (new_id, title))
        except Exception as e:
            return actions + ["Could not create consolidation task '%s': %s" % (title, e)]

    for t in tasks:
        if t["id"] == new_id:
            continue
        note = "Merged into consolidation task %s" % new_id
        try:
            close_task(t["id"], note)
            actions.append("  - merged and archived %s -> %s" % (t["id"], new_id))
        except Exception as e:
            actions.append("  - could not close %s: %s" % (t["id"], e))
    return actions


def archive_done_tasks(project):
    """Archive every task with status 'done', so it drops out of ordinary lists and
    searches while staying in the event log."""
    actions = []
    for t in [t for t in get_tasks(project) if t.get("status") == "done"]:
        try:
            run_board(["task", "archive", t["id"], "Archived: completed task"])
            actions.append("Archived completed task %s" % t["id"])
        except Exception as e:
            actions.append("Could not archive %s: %s" % (t["id"], e))
    return actions


def apply_all(analysis, project, rules):
    actions = []
    try:
        run_board(["heartbeat"])
    except Exception:
        pass
    for g in analysis["groups"]:
        if g["name"] == "retracted":
            for t in g["tasks"]:
                try:
                    close_task(t["id"], "Closed: retracted by a human")
                    actions.append("Closed and archived retracted task %s" % t["id"])
                except Exception as e:
                    actions.append("Could not close/archive %s: %s" % (t["id"], e))
        else:
            actions += consolidate(g, project)
    actions += archive_done_tasks(project)
    return actions


def main():
    p = argparse.ArgumentParser(description="Consolidate and tidy tasks on the board")
    p.add_argument("--project", "-p", help="project name (default: from the manifest)")
    p.add_argument("--apply", action="store_true", help="carry the consolidation out")
    p.add_argument("--archive-done", action="store_true", help="archive every completed task")
    p.add_argument("--json", action="store_true", help="JSON instead of text")
    args = p.parse_args()

    m = manifest()
    project = args.project or m.get("project") or "default"
    rules, rules_file = load_rules(m.get("root"))

    if args.archive_done:
        actions = archive_done_tasks(project)
        if args.json:
            print(json.dumps({"project": project, "archived": actions}, indent=2))
        else:
            print("Archived %d completed tasks for %s:" % (len(actions), project))
            for a in actions:
                print("- " + a)
        return

    analysis = analyze(get_tasks(project), rules)

    if args.apply:
        actions = apply_all(analysis, project, rules)
        if args.json:
            print(json.dumps({"project": project, "actions": actions}, indent=2))
        else:
            print("Applied task consolidation for %s:" % project)
            for a in actions:
                print("- " + a)
    elif args.json:
        print(json.dumps({
            "project": project,
            "rules": rules_file,
            "open_count": analysis["open_count"],
            "groups": {g["name"]: [t["id"] for t in g["tasks"]]
                       for g in analysis["groups"] if g["tasks"]},
        }, indent=2))
    else:
        print(format_report(analysis, project, rules, rules_file))


if __name__ == "__main__":
    main()
