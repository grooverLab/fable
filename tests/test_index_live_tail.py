"""Roadmap #1: incremental turn-boundary tail indexing.

index_live_tail indexes only the turns appended to a live transcript since it
was last indexed, resolving membership against already-indexed ancestors and
refreshing only the touched threads — and falls back to a full reindex when
the file was rewritten (prune/compaction)."""
import json
import os
import shutil
import tempfile
import unittest

from fable import db as fdb
from fable.extract import fts_extract_fn
from fable.indexer import index_live_tail, index_vault
from fable.recall import get_block, search
from tests.helpers import rec, write_jsonl


def _append(path, objs):
    with open(path, "a") as f:
        for o in objs:
            f.write(json.dumps(o, separators=(",", ":")) + "\n")


class TestTailIndex(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "f.db")
        self.live = write_jsonl(os.path.join(self.dir, "s.jsonl"), [
            rec("u1", "p1", None, "user", "2026-06-01T00:00:01Z",
                text="first prompt"),
            rec("a1", None, "u1", "assistant", "2026-06-01T00:00:02Z",
                text="first answer"),
        ])
        index_vault(self.db, [], live_file=self.live, extract_fn=fts_extract_fn)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_tail_indexes_appended_turn(self):
        _append(self.live, [
            rec("u2", "p2", "a1", "user", "2026-06-01T00:01:00Z",
                text="second prompt about auth"),
            rec("a2", None, "u2", "assistant", "2026-06-01T00:01:01Z",
                text="second answer"),
        ])
        out = index_live_tail(self.db, self.live, extract_fn=fts_extract_fn)
        self.assertEqual(out["mode"], "tail")
        self.assertEqual(out["new_records"], 2)

        conn = fdb.connect(self.db)
        self.assertIsNotNone(
            conn.execute("SELECT 1 FROM records WHERE uuid='u2'").fetchone())
        # the assistant turn resolves to the NEW thread (incremental membership)
        self.assertEqual(
            conn.execute("SELECT prompt_id FROM records WHERE uuid='a2'")
            .fetchone()[0], "p2")
        # the new thread row exists and counts both of its turns
        self.assertEqual(
            conn.execute("SELECT turn_count FROM threads WHERE prompt_id='p2'")
            .fetchone()[0], 2)
        conn.close()
        # and it's immediately searchable
        self.assertTrue(any(h["prompt_id"] == "p2"
                            for h in search(self.db, "auth")))

    def test_byte_exact_pointer_for_appended_record(self):
        _append(self.live, [rec("u2", "p2", "a1", "user",
                                "2026-06-01T00:01:00Z", text="precise bytes")])
        index_live_tail(self.db, self.live, extract_fn=fts_extract_fn)
        self.assertIn("precise bytes", get_block(self.db, "u2"))

    def test_nochange_is_a_noop(self):
        out = index_live_tail(self.db, self.live, extract_fn=fts_extract_fn)
        self.assertEqual(out["mode"], "nochange")
        self.assertEqual(out["new_records"], 0)

    def test_shrink_triggers_full_reindex(self):
        # a prune rewrites the live file SMALLER -> tail would corrupt, so the
        # guard falls back to a full reindex
        write_jsonl(self.live, [
            rec("u1", "p1", None, "user", "2026-06-01T00:00:01Z", text="kept"),
        ])
        out = index_live_tail(self.db, self.live, extract_fn=fts_extract_fn)
        self.assertEqual(out["mode"], "full")
        conn = fdb.connect(self.db)
        # a1 is gone from the only file that held it -> best-pointer forgets it
        self.assertIsNone(
            conn.execute("SELECT 1 FROM records WHERE uuid='a1'").fetchone())
        conn.close()

    def test_offset_advances_only_newer_seen(self):
        _append(self.live, [rec("u2", "p2", "a1", "user",
                                "2026-06-01T00:01:00Z", text="t2")])
        index_live_tail(self.db, self.live, extract_fn=fts_extract_fn)
        _append(self.live, [rec("u3", "p3", "u2", "user",
                                "2026-06-01T00:02:00Z", text="t3")])
        out = index_live_tail(self.db, self.live, extract_fn=fts_extract_fn)
        self.assertEqual(out["new_records"], 1)   # only u3, not u2 again


if __name__ == "__main__":
    unittest.main()
