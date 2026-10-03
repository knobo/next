import os, sys, tempfile, unittest
os.environ["BOARD_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import board


class TestReviewDone(unittest.TestCase):
    def test_review_does_not_reopen_finished_task(self):
        cols = [r[1] for r in board.db.execute("PRAGMA table_info(tasks)")]
        for st in ("done", "merged", "archived", "claimed"):
            tid = "T-" + st
            board.db.execute("INSERT INTO tasks (id, project, title, status, owner, created, updated) "
                             "VALUES (?,?,?,?,?,?,?)", (tid, "demo", "x", st, None, board.now(), board.now()))
            board.task_review(tid, "rev", {"open": 1})
            got = board.db.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]
            self.assertEqual(got, "in_review" if st == "claimed" else st)
            n = board.db.execute("SELECT COUNT(*) FROM events WHERE stream=? AND type='task.review_result'",
                                 ("task/" + tid,)).fetchone()[0]
            self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
