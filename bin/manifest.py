#!/usr/bin/env python3
"""Find and read the project manifest (DESIGN.md §3.2b).

Searches upwards for project.yaml from cwd, exactly as git finds .git, and stops at $HOME.
The manifest owns the project's *shape*; the board only holds a copy. That is why it is
never read from the board when the file exists — that is the requirement that the skill
works offline (K6).

  manifest.py            JSON with every field filled in
  manifest.py --sh       shell eval: ROOT, PROJECT, PHASE, ENTRY, REPOS, ...
  manifest.py --init <name>   write a minimal manifest in cwd
"""
import json, os, subprocess, sys

import yaml


def main_root(d):
    """A worktree has its own project.yaml, but is not the project root. If we compute
    paths from there the worktrees nest (`x-worktrees/x-worktrees/...`) and cleanup points
    at the wrong repo — T-67. Git knows where the primary checkout is; ask it."""
    try:
        r = subprocess.run(["git", "-C", d, "rev-parse", "--git-common-dir"],
                           capture_output=True, text=True, timeout=5)
    except OSError:
        return None
    if r.returncode:
        return None
    root = os.path.dirname(os.path.abspath(os.path.join(d, r.stdout.strip())))
    return root if os.path.exists(os.path.join(root, "project.yaml")) else None


def find(start=None):
    # An explicit root wins: agents run from worktrees and from /tmp.
    forced = os.environ.get("BOARD_PROJECT_ROOT")
    if forced:
        p = os.path.join(os.path.abspath(forced), "project.yaml")
        return p if os.path.exists(p) else None
    d = os.path.abspath(start or os.getcwd())
    home = os.path.expanduser("~")
    while True:
        p = os.path.join(d, "project.yaml")
        if os.path.exists(p):
            root = main_root(d)
            return os.path.join(root, "project.yaml") if root else p
        if d == home or d == "/" or os.path.dirname(d) == d:
            return None
        d = os.path.dirname(d)


def is_git(d):
    return os.path.isdir(os.path.join(d, ".git"))


def load(path):
    root = os.path.dirname(path)
    m = yaml.safe_load(open(path)) or {}
    if not m.get("project"):
        sys.exit("project.yaml is missing `project:` — %s" % path)
    m["root"] = root
    # The phase has no default. `build` as a silent fallback would have given a slacker
    # merge gate than the project deserves, and that is exactly what §10 forbids: an agent
    # must not end up with milder rules because something is missing.
    if m.get("phase") in (None, "", "unset"):
        sys.exit("project.yaml in %s is missing `phase:` — set idea|build|launch|live.\n"
                 "The phase drives the merge gate, the test level and prod deploys, and "
                 "must be set by a human." % root)
    m.setdefault("entry", "next-prompt.md")
    m.setdefault("repos", ["."] if is_git(root) else [])
    m.setdefault("worktrees", "../{repo}-worktrees/{branch}")
    m.setdefault("docs", ".")
    m.setdefault("forge", {})
    m.setdefault("environments", {})
    if m["phase"] not in ("idea", "build", "launch", "live"):
        sys.exit("unknown phase %r (idea|build|launch|live)" % m["phase"])
    # §10: a project with users must have an onboarding file, otherwise the agent only gets
    # the general rules — right for an idea-phase project, wrong for something in
    # production.
    if m["phase"] in ("launch", "live") and not m.get("onboarding"):
        m["_warn"] = ("phase %s without `onboarding:` — the skill refuses to start "
                      "(DESIGN.md §10)" % m["phase"])
    return m


def worktree_path(m, repo, branch):
    # repo "." (the default `repos: [.]`) or repo == the project name (the board stores
    # project names, not manifest repos) means "the root itself" — use the project name in
    # the pattern, otherwise the path becomes `.-worktrees/...` (T-67).
    if repo in ("", ".", m["project"]):
        repo_dir, name = m["root"], m["project"]
    else:
        repo_dir, name = os.path.join(m["root"], repo), repo
    pat = m["worktrees"].replace("{repo}", name).replace("{branch}", branch)
    return os.path.normpath(os.path.join(repo_dir, pat) if pat.startswith("..")
                            else os.path.join(m["root"], pat))


def sh(m):
    out = ["ROOT=%s" % q(m["root"]), "PROJECT=%s" % q(m["project"]),
           "PHASE=%s" % q(m["phase"]), "ENTRY=%s" % q(m["entry"]),
           "DOCS=%s" % q(m["docs"]), "REPOS=%s" % q(" ".join(m["repos"])),
           "WORKTREES=%s" % q(m["worktrees"]),
           "FORGE_KIND=%s" % q(m["forge"].get("kind", "")),
           "FORGE_URL=%s" % q(m["forge"].get("url", "")),
           "FORGE_ORG=%s" % q(m["forge"].get("org", "")),
           "FORGE_LOGIN=%s" % q(m["forge"].get("login", "")),
           "ONBOARDING=%s" % q(m.get("onboarding", {}).get("file", "")
                               if isinstance(m.get("onboarding"), dict) else m.get("onboarding") or "")]
    if m.get("_warn"):
        out.append("MANIFEST_WARN=%s" % q(m["_warn"]))
    return "\n".join(out)


def q(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def remote(d):
    try:
        return subprocess.run(["git", "-C", d, "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def propose(cwd, name):
    """Read the project's shape off disk instead of guessing it. Phase and goal must be
    set by the human: the phase drives the merge gate and prod deploys, and it cannot be
    derived from files."""
    m = {"project": name, "phase": "unset", "goal": ""}
    if is_git(cwd):
        repos, probe = ["."], cwd
    else:
        repos = sorted(d for d in os.listdir(cwd)
                       if is_git(os.path.join(cwd, d)) and not d.endswith("-worktrees"))
        probe = os.path.join(cwd, repos[0]) if repos else cwd
        m["repos"] = repos
    for cand in ("next-prompt.md", "docs/next-prompt.md"):
        if os.path.exists(os.path.join(cwd, cand)):
            m["entry"] = cand
            break
    for cand in ("docs/AGENT_ONBOARDING.md", "AGENT_ONBOARDING.md", "CLAUDE.md"):
        if os.path.exists(os.path.join(cwd, cand)):
            m["onboarding"] = cand
            break
    if os.path.isdir(os.path.join(cwd, "docs")):
        m["docs"] = "docs/"
    url = remote(probe)
    if url:
        host = url.split("@")[-1].split(":")[0].replace("https://", "").split("/")[0]
        kind = "github" if "github.com" in url else "forgejo"
        m["forge"] = {"kind": kind, "url": "https://" + host}
    return m, repos


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "--init":
        name = a[1] if len(a) > 1 else os.path.basename(os.getcwd())
        if os.path.exists("project.yaml"):
            sys.exit("project.yaml already exists — edit that instead")
        m, repos = propose(os.getcwd(), name)
        open("project.yaml", "w").write(
            yaml.safe_dump(m, sort_keys=False, allow_unicode=True, default_flow_style=False)
            + "\n# phase: unset stops /next on purpose. idea = break things freely, no users.\n"
              "# build = under development. launch = heading for users. live = users in production.\n")
        print("wrote project.yaml:")
        print(open("project.yaml").read())
        print("MUST BE SET BY A HUMAN: `phase` (idea|build|launch|live) and `goal`.")
        print("The phase drives the merge gate, the test level and prod deploys — it cannot be")
        print("read out of the files, and an agent must not be able to guess its way to milder")
        print("rules (DESIGN §10).")
        if not repos:
            print("WARNING: found no git repos here — set `repos:` by hand")
        sys.exit(0)
    path = find()
    if not path:
        sys.exit("No project.yaml found above %s. Create one with `board project init`." % os.getcwd())
    m = load(path)
    print(sh(m) if "--sh" in a else json.dumps(m, ensure_ascii=False))
