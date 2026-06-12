import json
import os
import shutil
import tempfile
import unittest

from fable import db as fdb
from fable.discover import discover
from fable.recall import search
from tests.helpers import rec, write_jsonl


class TestDiscover(unittest.TestCase):
    """Simulated ~/.claude/projects + backups layout across two projects."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")
        self.projects = os.path.join(self.dir, "projects")
        self.backups = os.path.join(self.dir, "backups")

        # project alpha, session s-aaa: live + one backup generation
        a1 = rec("a1", "pa1", None, "user", "2026-06-01T00:00:01Z",
                 text="alpha project zigzag work")
        a1["sessionId"] = "s-aaa"
        a2 = rec("a2", "pa1", "a1", "assistant", "2026-06-01T00:00:02Z",
                 text="done with the zigzag in alpha")
        a2["sessionId"] = "s-aaa"
        title = {"type": "custom-title", "customTitle": "alpha-session",
                 "sessionId": "s-aaa"}
        write_jsonl(os.path.join(self.projects, "-Users-x-alpha", "s-aaa.jsonl"),
                    [title, a1, a2])
        # backup holds a fuller copy of a1 (longer text)
        a1_full = json.loads(json.dumps(a1))
        a1_full["message"]["content"][0]["text"] += " with extra detail preserved"
        write_jsonl(os.path.join(self.backups, "alpha", "s-aaa", "v0-raw.jsonl"),
                    [a1_full])

        # project beta, session s-bbb: live only
        b1 = rec("b1", "pb1", None, "user", "2026-06-01T01:00:01Z",
                 text="beta project option chain table")
        b1["sessionId"] = "s-bbb"
        write_jsonl(os.path.join(self.projects, "-Users-x-beta", "s-bbb.jsonl"),
                    [b1])

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_discover(self, **kw):
        return discover(self.dbpath, projects_dir=self.projects,
                        backup_roots=[self.backups], **kw)

    def test_all_projects_indexed_with_sessions(self):
        stats = self.run_discover()
        conn = fdb.connect(self.dbpath)
        sessions = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT session_id, project, title FROM sessions").fetchall()}
        self.assertIn("s-aaa", sessions)
        self.assertIn("s-bbb", sessions)
        self.assertEqual(sessions["s-aaa"][1], "alpha-session")
        recs = dict(conn.execute(
            "SELECT uuid, session_id FROM records").fetchall())
        conn.close()
        self.assertEqual(recs["a1"], "s-aaa")
        self.assertEqual(recs["b1"], "s-bbb")
        self.assertEqual(stats["sessions"], 2)

    def test_backup_vault_attached_and_fidelity_wins(self):
        self.run_discover()
        conn = fdb.connect(self.dbpath)
        path = conn.execute(
            "SELECT f.path FROM records r JOIN files f ON f.id=r.file_id "
            "WHERE r.uuid='a1'").fetchone()[0]
        conn.close()
        self.assertIn("v0-raw.jsonl", path)  # fuller backup copy won

    def test_search_results_carry_project_and_session(self):
        self.run_discover()
        hits = search(self.dbpath, "zigzag")
        self.assertEqual(hits[0]["prompt_id"], "pa1")
        self.assertEqual(hits[0]["session_id"], "s-aaa")
        self.assertIn("alpha", hits[0]["project"])

    def test_project_filter(self):
        self.run_discover(project_filter="beta")
        conn = fdb.connect(self.dbpath)
        sessions = [r[0] for r in conn.execute(
            "SELECT session_id FROM sessions").fetchall()]
        conn.close()
        self.assertEqual(sessions, ["s-bbb"])


if __name__ == "__main__":
    unittest.main()
