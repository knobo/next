"""T-638: `gate merge` reads the PR head's combined commit status from Forgejo."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

d = tempfile.mkdtemp()
os.environ["BOARD_DB"] = os.path.join(d, "test_gate_ci.db")
os.environ["BOARD_TOKEN"] = "test-token"
os.environ["BOARD_HUMAN"] = "human"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import board

FORGE = {"kind": "forgejo", "url": "https://git.example.com", "org": "acme"}


def task(**kw):
    t = {"id": "T-1", "project": "ci", "repo": "app", "pr": "7", "status": "in_review",
         "owner": None, "risk": "normal", "needs_grants": "[]"}
    t.update(kw)
    return t


def fake_forge(status, calls):
    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url.endswith("/pulls/7"):
            return io.BytesIO(json.dumps({"head": {"sha": "abc123"}}).encode())
        if req.full_url.endswith("/commits/abc123/status"):
            return io.BytesIO(json.dumps(status).encode())
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
    return urlopen


class TestGateCI(unittest.TestCase):
    def setUp(self):
        self.manifest({"forge": FORGE})

    def manifest(self, m):
        board.db.execute("INSERT OR REPLACE INTO projects (name,phase,manifest,updated) "
                         "VALUES ('ci','build',?, '')", (json.dumps(m),))

    def state(self, status, t=None):
        calls = []
        with patch("urllib.request.urlopen", fake_forge(status, calls)):
            return board.forge_ci_state(t or task()), calls

    def test_states(self):
        for s in ("success", "failure", "error", "pending"):
            got, calls = self.state({"state": s, "total_count": 1})
            self.assertEqual(got, s)
        self.assertEqual(calls[0], "https://git.example.com/api/v1/repos/acme/app/pulls/7")

    def test_pr_url_gives_slug(self):
        _, calls = self.state({"state": "success", "total_count": 1},
                              task(pr="https://git.example.com/other/repo/pulls/7"))
        self.assertEqual(calls[0], "https://git.example.com/api/v1/repos/other/repo/pulls/7")

    def test_no_statuses_is_none(self):
        self.assertIsNone(self.state({"state": "", "total_count": 0, "statuses": []})[0])

    def test_unreadable_forge_is_none(self):
        def boom(req, timeout=None):
            raise urllib.error.URLError("down")
        with patch("urllib.request.urlopen", boom):
            self.assertIsNone(board.forge_ci_state(task()))

    def test_no_forgejo_no_call(self):
        self.manifest({"forge": {"kind": "github", "url": "https://github.com", "org": "x"}})
        got, calls = self.state({"state": "failure", "total_count": 1})
        self.assertIsNone(got)
        self.assertEqual(calls, [])

    def test_gate_refuses_on_failure_and_pending_only(self):
        for ci, blocked in (("failure", True), ("pending", True), ("success", False), (None, False)):
            with patch.object(board, "task", lambda tid: task()), \
                 patch.object(board, "forge_ci_state", lambda t, ci=ci: ci):
                g = board.gate_merge("T-1", None)
            self.assertEqual(g["ci"], ci)
            self.assertEqual(any("CI on the PR head" in r for r in g["reasons"]), blocked, ci)


if __name__ == "__main__":
    unittest.main()
