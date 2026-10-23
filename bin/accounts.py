#!/usr/bin/env python3
"""The harness accounts on this machine — which LOGIN a session runs on (DESIGN.md §3.2d).

Two Claude logins are two quotas, and a customer's project must run on the customer's
login. The board cannot see that; the environment the harness starts in decides it
(CLAUDE_CONFIG_DIR, CODEX_HOME, ...). This file names those environments, so that a
session can say which account it is and `next-run` can start one on purpose.

  ~/.config/next/accounts.yaml   (or $NEXT_ACCOUNTS)

    claude-privat:
      harness: claude-code
      env: {CLAUDE_CONFIG_DIR: ~/.claude}
      default: true            # also this account when the env var is not set at all
    claude-kunde:
      harness: claude-code
      env: {CLAUDE_CONFIG_DIR: ~/.claude-kunde}
    codex-privat:
      harness: codex
      env: {CODEX_HOME: ~/.codex}
      cmd: [codex]             # default: the harness's own binary

  accounts.py list                 JSON: every account
  accounts.py detect <harness>     the account the current environment is, or nothing
  accounts.py env <name>           shell: exports for that account, and NEXT_CMD
"""
import json, os, shlex, sys

import yaml

CMD = {"claude-code": ["claude"], "codex": ["codex"], "antigravity": ["antigravity"],
       "grok": ["grok"]}


def load():
    p = os.environ.get("NEXT_ACCOUNTS") or os.path.expanduser("~/.config/next/accounts.yaml")
    if not os.path.exists(p):
        return {}
    m = yaml.safe_load(open(p)) or {}
    if not isinstance(m, dict) or not all(isinstance(v, dict) and v.get("harness") for v in m.values()):
        sys.exit("%s: every account needs a harness:" % p)
    return m


def norm(v):
    return os.path.normpath(os.path.expanduser(str(v)))


def detect(harness, accts, environ=os.environ):
    for name, a in accts.items():
        if a["harness"] != harness:
            continue
        env = a.get("env") or {}
        if all(norm(environ[k]) == norm(v) if environ.get(k) else a.get("default")
               for k, v in env.items()):
            return name
    return ""


def main(argv):
    accts = load()
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        print(json.dumps(accts))
    elif cmd == "detect" and len(argv) == 3:
        print(detect(argv[2], accts))
    elif cmd == "env" and len(argv) == 3:
        a = accts.get(argv[2])
        if not a:
            sys.exit("unknown account %s — add it to ~/.config/next/accounts.yaml" % argv[2])
        for k, v in (a.get("env") or {}).items():
            print("export %s=%s" % (k, shlex.quote(norm(v))))
        print("export BOARD_ACCOUNT=%s BOARD_HARNESS=%s" % (shlex.quote(argv[2]), shlex.quote(a["harness"])))
        print("NEXT_CMD=(%s)" % " ".join(shlex.quote(c) for c in a.get("cmd") or CMD.get(a["harness"], [a["harness"]])))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
