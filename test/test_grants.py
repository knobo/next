import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import urllib.parse
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

    def form_request(self, method, path, form_data=None, token="human-token", accept=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        if accept:
            headers["Accept"] = accept
        body = urllib.parse.urlencode(form_data).encode("utf-8") if form_data is not None else None

        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(NoRedirectHandler)
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with opener.open(req) as resp:
                return resp.status, dict(resp.headers), resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode("utf-8")

    def test_web_ui_grants_and_project_controls(self):
        # Register an agent for web UI tests
        reg_data = {
            "harness": "codex",
            "host": "testhost2",
            "project": "demo",
            "session": "sess-webui-1"
        }
        status, reg_res = self.request("POST", "/agents", reg_data, token="agent-token")
        self.assertEqual(status, 200)
        aid = reg_res["id"]

        # 1. Test HTML rendering in html_status
        # Non-human: no revoke buttons, no add-grant form, no pause/resume buttons
        html_non_human = board.html_status("demo", token="agent-token", human=False)
        self.assertNotIn(f"/agents/{aid}/grants/revoke", html_non_human)
        self.assertNotIn(f"/agents/{aid}/grants", html_non_human)
        self.assertNotIn("/projects/demo/pause", html_non_human)
        self.assertNotIn("/projects/demo/resume", html_non_human)

        # Human: add-grant form and pause button visible
        html_human = board.html_status("demo", token="human-token", human=True)
        self.assertIn(f"action='/agents/{aid}/grants'", html_human)
        self.assertIn(f"grants-list-{aid}", html_human)
        self.assertIn("action='/projects/demo/pause'", html_human)
        self.assertIn("⏸ pause", html_human)

        # 2. Form POST to add grant with agent-token (non-human) -> 403 Forbidden
        status, hdrs, body = self.form_request("POST", f"/agents/{aid}/grants",
                                               {"grant": "merge", "project": "demo"},
                                               token="agent-token")
        self.assertEqual(status, 403)
        self.assertIn("needs_human_token", body)

        # 3. Form POST to add grant with human-token -> 302 Redirect to /status?project=demo
        status, hdrs, body = self.form_request("POST", f"/agents/{aid}/grants",
                                               {"grant": "merge", "project": "demo"},
                                               token="human-token")
        self.assertEqual(status, 302)
        self.assertEqual(hdrs.get("Location"), "/status?project=demo")

        # Verify grant is now present in html_status for both non-human and human
        html_nh = board.html_status("demo", token="agent-token", human=False)
        self.assertIn("<span class='badge badge-sm badge-outline font-mono'>merge</span>", html_nh)
        self.assertNotIn(f"/agents/{aid}/grants/revoke", html_nh)

        html_h = board.html_status("demo", token="human-token", human=True)
        self.assertIn(f"action='/agents/{aid}/grants/revoke'", html_h)
        self.assertIn("value='merge'", html_h)
        self.assertIn("Trekk tilbake grant", html_h)

        # 4. Form POST to revoke grant with agent-token (non-human) -> 403 Forbidden
        status, hdrs, body = self.form_request("POST", f"/agents/{aid}/grants/revoke",
                                               {"grant": "merge", "project": "demo"},
                                               token="agent-token")
        self.assertEqual(status, 403)
        self.assertIn("needs_human_token", body)

        # 5. Form POST to revoke grant with human-token -> 302 Redirect to /status?project=demo
        status, hdrs, body = self.form_request("POST", f"/agents/{aid}/grants/revoke",
                                               {"grant": "merge", "project": "demo"},
                                               token="human-token")
        self.assertEqual(status, 302)
        self.assertEqual(hdrs.get("Location"), "/status?project=demo")

        # Verify grant is revoked
        status, g_get = self.request("GET", f"/agents/{aid}/grants?project=demo", token="agent-token")
        self.assertNotIn("merge", g_get["grants"])

        # 6. Form POST to pause project -> 302 Redirect
        status, hdrs, body = self.form_request("POST", "/projects/demo/pause",
                                               {"project": "demo"},
                                               token="human-token")
        self.assertEqual(status, 302)
        self.assertEqual(hdrs.get("Location"), "/status?project=demo")

        # Verify project is paused and resume button is rendered in human view
        html_paused = board.html_status("demo", token="human-token", human=True)
        self.assertIn("action='/projects/demo/resume'", html_paused)
        self.assertIn("▶ gjenoppta", html_paused)

        # 7. Form POST to resume project -> 302 Redirect
        status, hdrs, body = self.form_request("POST", "/projects/demo/resume",
                                               {"project": "demo"},
                                               token="human-token")
        self.assertEqual(status, 302)
        self.assertEqual(hdrs.get("Location"), "/status?project=demo")

        # Verify project is resumed
        html_resumed = board.html_status("demo", token="human-token", human=True)
        self.assertIn("action='/projects/demo/pause'", html_resumed)
        self.assertNotIn("action='/projects/demo/resume'", html_resumed)

        # 8. Test JSON response when Accept: application/json
        status, hdrs, body = self.form_request("POST", f"/agents/{aid}/grants",
                                               {"grant": "deploy-dev", "project": "demo"},
                                               token="human-token",
                                               accept="application/json")
        self.assertEqual(status, 200)
        json_resp = json.loads(body)
        self.assertIn("deploy-dev", json_resp["grants"])


if __name__ == "__main__":
    unittest.main()
