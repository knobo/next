import os
import sys
import tempfile
import unittest

# Prepare environment before importing board
d = tempfile.mkdtemp()
os.environ.setdefault("BOARD_DB", os.path.join(d, "test.db"))
os.environ.setdefault("BOARD_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import board


class ReapAllWorkspaces(unittest.TestCase):
    def test_reaper_sweeps_every_workspace(self):
        """The reaper thread calls reap_all(); a workspace it skipped would keep dead
        agents' leases and overdue questions forever."""
        with board.LOCK:
            board.workspace_create({"_admin": True, "name": "reapt"})
            board.use_ws("reapt")
            board.db.execute("INSERT INTO questions (id, project, kind, text, default_answer, "
                             "deadline, status) VALUES ('Q-1','p','question','?','yes',"
                             "'2020-01-01T00:00:00Z','open')")
            board.db.commit()
            board.use_ws("default")
            board.reap_all()
            row = board.ws_db("reapt").execute("SELECT status FROM questions WHERE id='Q-1'").fetchone()
        self.assertEqual(row["status"], "defaulted")


if __name__ == "__main__":
    unittest.main()
