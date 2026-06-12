import json
import os
import shutil
import tempfile
import unittest

from fable import db as fdb
from fable.indexer import index_vault
from fable.jsonl import read_span
from tests.helpers import rec, write_jsonl


def pruned_copy(obj):
    """Simulate prune: shorter serialization of the same uuid."""
    out = json.loads(json.dumps(obj))
    msg = out.get("message", {})
    if msg.get("content"):
        msg["content"] = [{"type": "text", "text": "[pruned]"}]
    return out


class TestIndexVault(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")

        # gen0: a, b at full fidelity (thread p1)
        self.a = rec("a", "p1", None, "user", "2026-06-01T00:00:01Z",
                     text="please fix the zigzag pivot detection logic")
        self.b = rec("b", "p1", "a", "assistant", "2026-06-01T00:00:02Z",
                     text="I will fix the pivot logic in sled-zigzag now")
        # gen1: a, b pruned; c, d at full fidelity (thread p2)
        self.c = rec("c", "p2", "b", "user", "2026-06-01T00:01:01Z",
                     text="now add the option chain table")
        self.d = rec("d", "p2", "c", "assistant", "2026-06-01T00:01:02Z",
                     text="adding the option chain table with strikes and OI columns")
        # live: c, d identical again plus new e (thread p2 continues)
        self.e = rec("e", "p2", "d", "assistant", "2026-06-01T00:01:03Z",
                     text="done, the table renders")

        self.gen0 = write_jsonl(os.path.join(self.dir, "vault", "v0-raw.jsonl"),
                                [self.a, self.b])
        self.gen1 = write_jsonl(os.path.join(self.dir, "vault", "v1-pruned.jsonl"),
                                [pruned_copy(self.a), pruned_copy(self.b), self.c, self.d])
        self.live = write_jsonl(os.path.join(self.dir, "live.jsonl"),
                                [pruned_copy(self.a), pruned_copy(self.b),
                                 self.c, self.d, self.e,
                                 {"type": "custom-title", "customTitle": "t"}])

    def tearDown(self):
        shutil.rmtree(self.dir)

    def index(self):
        return index_vault(self.dbpath,
                           vault_files=[self.gen0, self.gen1],
                           live_file=self.live)

    def fetch(self, sql, *args):
        conn = fdb.connect(self.dbpath)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def test_best_fidelity_copy_wins(self):
        self.index()
        rows = self.fetch(
            "SELECT r.uuid, f.path FROM records r JOIN files f ON f.id = r.file_id "
            "ORDER BY r.uuid")
        where = {u: os.path.basename(p) for u, p in rows}
        # a, b fullest in gen0; c, d first seen full in gen1 (tie vs live -> earlier)
        self.assertEqual(where["a"], "v0-raw.jsonl")
        self.assertEqual(where["b"], "v0-raw.jsonl")
        self.assertEqual(where["c"], "v1-pruned.jsonl")
        self.assertEqual(where["d"], "v1-pruned.jsonl")
        self.assertEqual(where["e"], "live.jsonl")

    def test_spans_round_trip_to_original_objects(self):
        self.index()
        rows = self.fetch(
            "SELECT r.uuid, f.path, r.offset, r.length FROM records r "
            "JOIN files f ON f.id = r.file_id")
        originals = {"a": self.a, "b": self.b, "c": self.c, "d": self.d, "e": self.e}
        for uuid, path, off, length in rows:
            self.assertEqual(json.loads(read_span(path, off, length)), originals[uuid])

    def test_threads_aggregated(self):
        self.index()
        rows = {r[0]: r for r in self.fetch(
            "SELECT prompt_id, turn_count, first_ts, last_ts, first_uuid, leaf_uuid "
            "FROM threads")}
        self.assertEqual(rows["p1"][1], 2)
        self.assertEqual(rows["p2"][1], 3)
        self.assertEqual(rows["p2"][2], "2026-06-01T00:01:01Z")
        self.assertEqual(rows["p2"][3], "2026-06-01T00:01:03Z")
        self.assertEqual(rows["p2"][4], "c")
        self.assertEqual(rows["p2"][5], "e")

    def test_reindex_idempotent(self):
        self.index()
        first = self.fetch("SELECT uuid, file_id, offset, fidelity FROM records ORDER BY uuid")
        self.index()
        second = self.fetch("SELECT uuid, file_id, offset, fidelity FROM records ORDER BY uuid")
        self.assertEqual(first, second)
        self.assertEqual(self.fetch("SELECT COUNT(*) FROM records")[0][0], 5)

    def test_new_generation_upgrades_fidelity(self):
        # index live only: a, b are pruned copies
        index_vault(self.dbpath, vault_files=[], live_file=self.live)
        before = {u: f for u, f in self.fetch("SELECT uuid, fidelity FROM records")}
        # now add the raw generation: a, b should upgrade, c..e untouched
        index_vault(self.dbpath, vault_files=[self.gen0, self.gen1], live_file=self.live)
        after = {u: f for u, f in self.fetch("SELECT uuid, fidelity FROM records")}
        self.assertGreater(after["a"], before["a"])
        self.assertGreater(after["b"], before["b"])
        self.assertEqual(after["e"], before["e"])

    def test_uuidless_records_skipped(self):
        stats = self.index()
        self.assertEqual(self.fetch(
            "SELECT COUNT(*) FROM records WHERE uuid IS NULL")[0][0], 0)
        self.assertGreaterEqual(stats["skipped_no_uuid"], 1)

    def test_live_file_reindexed_when_changed(self):
        self.index()
        # live grows by one record (append-only)
        f_extra = rec("f", "p2", "e", "user", "2026-06-01T00:02:00Z", text="thanks")
        with open(self.live, "a") as fh:
            fh.write(json.dumps(f_extra, separators=(",", ":")) + "\n")
        self.index()
        rows = self.fetch("SELECT uuid FROM records WHERE uuid='f'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.fetch(
            "SELECT turn_count FROM threads WHERE prompt_id='p2'")[0][0], 4)


if __name__ == "__main__":
    unittest.main()
