"""Regression tests for the adversarial-review findings."""
import json
import os
import shutil
import tempfile
import unittest

from fable import db as fdb
from fable.extract import fts_extract_fn
from fable.indexer import index_vault
from fable.recall import render_thread, get_block, search, StaleIndexError
from tests.helpers import rec, write_jsonl


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def index_live(self, objs, name="live.jsonl"):
        live = write_jsonl(os.path.join(self.dir, name), objs)
        index_vault(self.dbpath, [], live_file=live,
                    extract_fn=fts_extract_fn)
        return live


class TestStaleIndexDetection(Base):
    def test_append_only_growth_is_not_stale(self):
        live = self.index_live([rec("a", "p1", None, "user", text="hello")])
        with open(live, "a") as f:  # active session keeps appending
            f.write(json.dumps(rec("b", "p1", "a", "user", text="more"))
                    + "\n")
        # indexed offsets are still valid — reads must succeed
        self.assertIn("hello", get_block(self.dbpath, "a"))
        self.assertIn("hello", render_thread(self.dbpath, "p1"))

    def test_rewrite_is_stale(self):
        live = self.index_live([
            rec("a", "p1", None, "user", text="hello padding padding")])
        # rewrite SMALLER (prune-style): offsets are garbage now
        from tests.helpers import write_jsonl
        write_jsonl(live, [rec("a", "p1", None, "user", text="hi")])
        with self.assertRaises(StaleIndexError):
            get_block(self.dbpath, "a")
        index_vault(self.dbpath, [], live_file=live,
                    extract_fn=fts_extract_fn)
        self.assertIn("hi", get_block(self.dbpath, "a"))


class TestNoNestedSentinels(Base):
    def test_recalled_turn_renders_as_stub(self):
        a = rec("a", "p1", None, "user",
                text='question <historical_context arcs="p7">old recalled '
                     "payload</historical_context> tail")
        self.index_live([a])
        out = render_thread(self.dbpath, "p1")
        self.assertEqual(out.count("<historical_context"), 1)  # outer only
        self.assertNotIn("old recalled payload", out)
        self.assertIn('<consulted_arcs refs="p7"/>', out)


class TestBudgetHardCap(Base):
    def test_fixed_text_cannot_flood_output(self):
        objs = [rec("a", "p1", None, "user", text="X" * 200_000)]
        self.index_live(objs)
        out = render_thread(self.dbpath, "p1", budget=500)
        self.assertLess(len(out), 500 * 4 * 2)
        self.assertIn("exceeds budget", out)


class TestOrphanReconcile(Base):
    def test_vanished_uuid_leaves_no_ghost_in_search(self):
        live = self.index_live([
            rec("a", "p1", None, "user", text="ghostword content"),
            rec("b", "p2", None, "user", text="other thread"),
        ])
        self.assertTrue(search(self.dbpath, "ghostword"))
        # live rewritten WITHOUT record a (e.g. extract-mode prune)
        write_jsonl(live, [rec("b", "p2", None, "user", text="other thread")])
        index_vault(self.dbpath, [], live_file=live,
                    extract_fn=fts_extract_fn)
        self.assertEqual(search(self.dbpath, "ghostword"), [])
        conn = fdb.connect(self.dbpath)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM records WHERE uuid='a'").fetchone()[0], 0)
        conn.close()

    def test_vanished_uuid_survives_via_vault(self):
        a = rec("a", "p1", None, "user", text="ghostword content")
        b = rec("b", "p2", None, "user", text="other thread")
        vault = write_jsonl(os.path.join(self.dir, "v0-raw.jsonl"), [a, b])
        live = write_jsonl(os.path.join(self.dir, "live.jsonl"), [a, b])
        index_vault(self.dbpath, [vault], live_file=live,
                    extract_fn=fts_extract_fn)
        write_jsonl(live, [b])  # a dropped from live entirely
        index_vault(self.dbpath, [vault], live_file=live,
                    extract_fn=fts_extract_fn)
        hits = search(self.dbpath, "ghostword")
        self.assertEqual(hits[0]["prompt_id"], "p1")
        self.assertIn("ghostword", get_block(self.dbpath, "a"))


class TestReadPathsDontCreateDb(Base):
    def test_search_on_missing_db_raises(self):
        missing = os.path.join(self.dir, "nope.db")
        with self.assertRaises(FileNotFoundError):
            search(missing, "anything")
        self.assertFalse(os.path.exists(missing))


class TestOrphanedVaultDiscovery(Base):
    def test_backups_without_live_file_are_indexed(self):
        from fable.discover import discover
        projects = os.path.join(self.dir, "projects")
        os.makedirs(os.path.join(projects, "-Users-x-alpha"))
        backups = os.path.join(self.dir, "backups")
        a = rec("a", "p1", None, "user", text="orphaned history words")
        a["sessionId"] = "s-gone"
        write_jsonl(os.path.join(backups, "alpha", "s-gone", "v0-raw.jsonl"),
                    [a])
        discover(self.dbpath, projects_dir=projects, backup_roots=[backups])
        hits = search(self.dbpath, "orphaned history")
        self.assertEqual(hits[0]["session_id"], "s-gone")


if __name__ == "__main__":
    unittest.main()


class TestStaleSelfHeal(unittest.TestCase):
    """A live transcript that grew after indexing must self-heal in the
    dashboard: re-index that file and retry, not surface an error."""

    def test_heal_stale_reindexes_live_file(self):
        import json as _json
        from fable.extract import fts_extract_fn
        from fable.indexer import index_vault
        from fable.recall import render_thread, StaleIndexError
        from fable.serve import _heal_stale
        from tests.helpers import rec, write_jsonl
        d = tempfile.mkdtemp()
        try:
            db = os.path.join(d, "f.db")
            live = write_jsonl(os.path.join(d, "live.jsonl"), [
                rec("a", "p1", None, "user", "2026-06-01T00:00:01Z",
                    text="hello world")])
            index_vault(db, [], live_file=live, extract_fn=fts_extract_fn)
            # a prune-style rewrite (smaller) makes offsets garbage
            from tests.helpers import write_jsonl
            write_jsonl(live, [
                rec("a", "p1", None, "user", "2026-06-01T00:00:01Z",
                    text="hello world"),
                rec("b", None, "a", "assistant",
                    "2026-06-01T00:00:02Z", text="reply")])
            os.truncate(live, os.path.getsize(live) - 2)  # force shrink
            write_jsonl(live, [
                rec("a", "p1", None, "user", "2026-06-01T00:00:01Z",
                    text="hello world")])
            try:
                render_thread(db, "p1", sentinel=False)
                self.fail("expected StaleIndexError")
            except StaleIndexError as e:
                self.assertTrue(_heal_stale(db, e))
            out = render_thread(db, "p1", sentinel=False)
            self.assertIn("hello world", out)

    # immutable vault files must NOT self-heal — drift there is a real error
        finally:
            shutil.rmtree(d)
