import json
import os
import shutil
import tempfile
import unittest

from fable import db as fdb
from fable.extract import fts_extract_fn
from fable.indexer import index_vault
from fable.recall import get_block
from fable.vaultgc import plan_gc, apply_gc
from tests.helpers import rec, write_jsonl


class TestVaultGc(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")
        a = rec("a", "p1", None, "user", text="alpha content here")
        b = rec("b", "p1", "a", "assistant", text="beta content here")
        c = rec("c", "p2", "b", "user", text="gamma only in v1 and live")
        # v0: a, b (full). v1: a, b (identical) + c  -> v0 fully redundant
        self.v0 = write_jsonl(os.path.join(self.dir, "v", "v0-raw.jsonl"),
                              [a, b])
        self.v1 = write_jsonl(os.path.join(self.dir, "v", "v1-pruned.jsonl"),
                              [a, b, c])
        self.live = write_jsonl(os.path.join(self.dir, "live.jsonl"),
                                [a, b, c])
        index_vault(self.dbpath, [self.v0, self.v1], live_file=self.live,
                    extract_fn=fts_extract_fn)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_plan_finds_only_fully_redundant(self):
        plan = plan_gc(self.dbpath)
        paths = {os.path.basename(p["path"]) for p in plan}
        # exactly one of the two identical-content generations may go,
        # never both (mutual redundancy guard)
        self.assertEqual(len(paths & {"v0-raw.jsonl", "v1-pruned.jsonl"}), 1)
        self.assertNotIn("v1-pruned.jsonl", paths) if "v0-raw.jsonl" in paths \
            else self.assertNotIn("v0-raw.jsonl", paths)

    def test_apply_moves_to_trash_and_recall_still_works(self):
        plan = plan_gc(self.dbpath)
        trash = os.path.join(self.dir, "trash")
        result = apply_gc(self.dbpath, plan, trash)
        self.assertEqual(result["moved"], len(plan))
        self.assertTrue(all(os.path.exists(os.path.join(
            trash, f)) for f in os.listdir(trash)))
        # every record still byte-recallable from survivors
        for uuid, text in [("a", "alpha"), ("b", "beta"), ("c", "gamma")]:
            self.assertIn(text, get_block(self.dbpath, uuid))
        # nothing dangling
        conn = fdb.connect(self.dbpath)
        dangle = conn.execute(
            "SELECT COUNT(*) FROM copies c LEFT JOIN files f "
            "ON f.id = c.file_id WHERE f.id IS NULL").fetchone()[0]
        conn.close()
        self.assertEqual(dangle, 0)

    def test_unique_content_generation_never_collected(self):
        # add a generation holding the ONLY copy of a record
        d = rec("d", "p3", None, "user", text="unique snowflake record")
        v2 = write_jsonl(os.path.join(self.dir, "v", "v2-pruned.jsonl"), [d])
        index_vault(self.dbpath, [self.v0, self.v1, v2],
                    live_file=self.live, extract_fn=fts_extract_fn)
        plan = plan_gc(self.dbpath)
        self.assertNotIn("v2-pruned.jsonl",
                         {os.path.basename(p["path"]) for p in plan})


if __name__ == "__main__":
    unittest.main()
