import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

# Prepare environment before importing board
d = tempfile.mkdtemp()
db_path = os.path.join(d, "test_grants.db")
policy_path = os.path.join(d, "policy.json")

with open(policy_path, "w") as f:
    json.dump({
        "grants": {
            "demo": {
                "claude-code@*": ["merge"],
                "codex@*": []
            }
        },
        "roles": {
            "coordinator": {"singleton": True, "requires": ["merge"]},
            "implementer": {"requires": []}
        }
    }, f)

os.environ["BOARD_DB"] = db_path
os.environ["BOARD_POLICY"] = policy_path
os.environ["BOARD_TOKEN"] = "agent-token"
os.environ["BOARD_HUMAN_TOKEN"] = "human-token"
os.environ["BOARD_HUMAN"] = "human"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import board


class TestGrants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Start board HTTP server on random free port
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), board.Handler)
        cls.port = cls.server.server_port
        cls.base_url = f"http://127.0.0.1:{cls.port}/api/v1"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, data=None, token="agent-token"):
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8")
                return status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                content = json.loads(raw) if raw else {}
            except Exception:
                content = {"raw": raw}
            return e.code, content

    def test_grants_full_lifecycle(self):
        # 1. Register an agent with codex harness (no policy grants for codex)
        reg_data = {
            "harness": "codex",
            "host": "testhost",
            "project": "demo",
            "session": "sess-codex-1"
        }
        status, reg_res = self.request("POST", "/agents", reg_data, token="agent-token")
        self.assertEqual(status, 200)
        aid = reg_res["id"]
        self.assertEqual(reg_res.get("grants"), [])

        # 2. Query GET /agents/<aid>/grants
        status, g_get = self.request("GET", f"/agents/{aid}/grants?project=demo", token="agent-token")
        self.assertEqual(status, 200)
        self.assertEqual(g_get["id"], aid)
        self.assertEqual(g_get["project"], "demo")
        self.assertEqual(g_get["grants"], [])

        # 3. Agent token attempts to grant -> 403 needs_human_token
        status, res = self.request("POST", f"/agents/{aid}/grants", {"grant": "merge"}, token="agent-token")
        self.assertEqual(status, 403)
        self.assertTrue(res.get("needs_human_token"))

        # 4. Human token grants 'merge' and 'deploy-prod'
        status, res = self.request("POST", f"/agents/{aid}/grants", {"grants": ["merge", "deploy-prod"]}, token="human-token")
        self.assertEqual(status, 200)
        self.assertEqual(res["id"], aid)
        self.assertEqual(res["project"], "demo")
        self.assertEqual(res["grants"], ["deploy-prod", "merge"])

        # Verify source='human' in database
        row = board.db.execute("SELECT source FROM grants WHERE agent=? AND project=? AND grant_name=?",
                               (aid, "demo", "merge")).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "human")

        # 5. Agent registers / heartbeats again -> dynamic human grants must persist!
        status, rereg_res = self.request("POST", "/agents", reg_data, token="agent-token")
        self.assertEqual(status, 200)
        self.assertIn("merge", rereg_res["grants"])
        self.assertIn("deploy-prod", rereg_res["grants"])

        # 6. Dynamic grants allow task claim with needs_grants and gate/merge
        # Create task requiring 'deploy-prod'
        status, task = self.request("POST", "/tasks", {
            "agent": aid,
            "project": "demo",
            "title": "prod release",
            "needs_grants": ["deploy-prod", "merge"],
            "repo": "web"
        }, token="agent-token")
        self.assertEqual(status, 200)
        tid = task["id"]

        # Claim task
        status, claim_res = self.request("POST", f"/tasks/{tid}/claim", {"agent": aid}, token="agent-token")
        self.assertEqual(status, 200)
        self.assertEqual(claim_res["owner"], aid)

        # gate/merge check
        status, gate_res = self.request("GET", f"/tasks/{tid}/gate/merge?agent={aid}", token="agent-token")
        self.assertEqual(status, 200)
        # It shouldn't fail due to missing grants
        reasons = gate_res.get("reasons", [])
        self.assertFalse(any("missing grants" in r for r in reasons))

        # 7. Agent token attempts to revoke -> 403 needs_human_token
        status, res = self.request("DELETE", f"/agents/{aid}/grants/deploy-prod?project=demo", token="agent-token")
        self.assertEqual(status, 403)
        self.assertTrue(res.get("needs_human_token"))

        # 8. Human token revokes 'deploy-prod'
        status, res = self.request("DELETE", f"/agents/{aid}/grants/deploy-prod?project=demo", token="human-token")
        self.assertEqual(status, 200)
        self.assertEqual(res["grants"], ["merge"])

        # Check in DB that deploy-prod is gone
        row = board.db.execute("SELECT * FROM grants WHERE agent=? AND project=? AND grant_name=?",
                               (aid, "demo", "deploy-prod")).fetchone()
        self.assertIsNone(row)

        # 9. Verify event log has agent.granted and agent.revoked
        evs = board.db.execute("SELECT * FROM events WHERE project='demo' AND type IN ('agent.granted', 'agent.revoked')").fetchall()
        types = [e["type"] for e in evs]
        self.assertIn("agent.granted", types)
        self.assertIn("agent.revoked", types)


if __name__ == "__main__":
    unittest.main()
