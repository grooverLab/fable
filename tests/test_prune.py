import json
import os
import shutil
import tempfile
import unittest

from fable.extract import fts_extract_fn
from fable.indexer import index_vault
from fable.prune import prune_file, PruneGateError
from tests.helpers import rec, tool_use_block, tool_result_block, write_jsonl


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


class PruneBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.backups = os.path.join(self.dir, "backups")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def make_live(self, objs, name="live.jsonl"):
        return write_jsonl(os.path.join(self.dir, name), objs)


class TestPruneCore(PruneBase):
    def corpus(self):
        title = {"type": "custom-title", "customTitle": "t", "sessionId": "s"}
        noise = {"type": "file-history-snapshot", "messageId": "m"}
        a = rec("a", "p1", None, "user", "2026-06-01T00:00:01Z", text="hi")
        b = rec("b", "p1", "a", "assistant", "2026-06-01T00:00:02Z",
                text="working on it")
        b["message"]["content"].append(tool_use_block("t1", "Read", {
            "file_path": "/x/y.rs", "limit": 400}))
        b["message"]["content"].append({
            "type": "thinking", "thinking": "signed thought",
            "signature": "SIG"})
        c = rec("c", "p1", "b", "user", "2026-06-01T00:00:03Z")
        c["message"]["content"] = [tool_result_block("t1", "X" * 9000)]
        c["toolUseResult"] = {"big": "Y" * 5000}
        d = rec("d", "p1", "c", "assistant", "2026-06-01T00:00:04Z",
                text="done")
        return [title, noise, a, b, c, d]

    def test_noise_dropped_metadata_prepended_bloat_stripped(self):
        live = self.make_live(self.corpus())
        out = os.path.join(self.dir, "out.jsonl")
        report = prune_file(live, "resume", backup_dir=self.backups,
                            output=out)
        objs = load_jsonl(out)
        self.assertEqual(objs[0]["type"], "custom-title")
        # file-history-snapshots are now KEPT (time-travel anchors)
        self.assertTrue(any(o.get("type") == "file-history-snapshot"
                             for o in objs))
        by_uuid = {o.get("uuid"): o for o in objs if o.get("uuid")}
        # tool_use kept meaningful fields, dropped the rest
        blocks = by_uuid["b"]["message"]["content"]
        tool = next(x for x in blocks if x["type"] == "tool_use")
        self.assertEqual(tool["input"]["file_path"], "/x/y.rs")
        self.assertNotIn("limit", tool["input"])
        # signed thinking preserved verbatim
        think = next(x for x in blocks if x["type"] == "thinking")
        self.assertEqual(think["signature"], "SIG")
        # tool_result pruned, toolUseResult stripped
        cres = by_uuid["c"]["message"]["content"][0]
        self.assertEqual(cres["content"], "[pruned]")
        self.assertNotIn("toolUseResult", by_uuid["c"])
        self.assertTrue(report["chain_valid"])

    def test_chain_reparented_after_drops(self):
        objs = self.corpus()
        # e's parent is a synthetic assistant that will be dropped
        synth = rec("synth", "p1", "d", "assistant", "2026-06-01T00:00:05Z",
                    text="")
        synth["message"]["model"] = "<synthetic>"
        e = rec("e", "p1", "synth", "user", "2026-06-01T00:00:06Z", text="ok")
        live = self.make_live(objs + [synth, e])
        out = os.path.join(self.dir, "out.jsonl")
        prune_file(live, "resume", backup_dir=self.backups, output=out)
        by_uuid = {o.get("uuid"): o for o in load_jsonl(out) if o.get("uuid")}
        self.assertNotIn("synth", by_uuid)
        self.assertEqual(by_uuid["e"]["parentUuid"], "d")

    def test_backup_v0_created_once(self):
        live = self.make_live(self.corpus())
        out = os.path.join(self.dir, "out.jsonl")
        prune_file(live, "resume", backup_dir=self.backups, output=out)
        v0 = os.path.join(self.backups, "live", "v0-raw.jsonl")
        self.assertTrue(os.path.exists(v0))
        prune_file(live, "resume", backup_dir=self.backups, output=out)
        files = os.listdir(os.path.join(self.backups, "live"))
        self.assertIn("v1-pruned.jsonl", files)


class TestCompaction(PruneBase):
    def corpus(self):
        a = rec("a", "p1", None, "user", "2026-06-01T00:00:01Z", text="start")
        wall = {"type": "system", "subtype": "compact_boundary",
                "uuid": "wall", "parentUuid": "a",
                "timestamp": "2026-06-01T00:00:02Z",
                "logicalParentUuid": "a"}
        summary = {"type": "user", "uuid": "sum", "parentUuid": "wall",
                   "timestamp": "2026-06-01T00:00:03Z",
                   "isCompactSummary": True,
                   "message": {"role": "user", "content": "summary text"}}
        b = rec("b", "p1", "sum", "assistant", "2026-06-01T00:00:04Z",
                text="continuing")
        return [a, wall, summary, b]

    def test_resume_keeps_walls(self):
        live = self.make_live(self.corpus())
        out = os.path.join(self.dir, "out.jsonl")
        prune_file(live, "resume", backup_dir=self.backups, output=out)
        uuids = [o.get("uuid") for o in load_jsonl(out)]
        self.assertIn("wall", uuids)
        self.assertIn("sum", uuids)

    def test_extract_removes_walls_and_stitches(self):
        live = self.make_live(self.corpus())
        out = os.path.join(self.dir, "out.jsonl")
        prune_file(live, "extract", backup_dir=self.backups, output=out)
        objs = load_jsonl(out)
        uuids = [o.get("uuid") for o in objs]
        self.assertNotIn("wall", uuids)
        self.assertNotIn("sum", uuids)
        by_uuid = {o.get("uuid"): o for o in objs if o.get("uuid")}
        self.assertEqual(by_uuid["b"]["parentUuid"], "a")


class TestV2Features(PruneBase):
    def test_strip_images(self):
        a = rec("a", "p1", None, "user", "2026-06-01T00:00:01Z")
        a["message"]["content"] = [
            {"type": "image", "source": {"type": "base64", "data": "Z" * 5000}},
            {"type": "text", "text": "look at this"},
        ]
        live = self.make_live([a])
        out = os.path.join(self.dir, "out.jsonl")
        prune_file(live, "resume", backup_dir=self.backups, output=out,
                   strip_images=True)
        obj = next(o for o in load_jsonl(out) if o.get("uuid") == "a")
        blocks = obj["message"]["content"]
        self.assertFalse(any(b.get("type") == "image" for b in blocks))
        self.assertTrue(any("image stripped" in b.get("text", "")
                            for b in blocks))
        self.assertIn("look at this", blocks[-1]["text"])

    def test_historical_context_stripped_with_stub(self):
        a = rec("a", "p1", None, "user", "2026-06-01T00:00:01Z",
                text='question <historical_context arcs="p7,p8">huge recalled '
                     "payload</historical_context> more")
        live = self.make_live([a])
        out = os.path.join(self.dir, "out.jsonl")
        prune_file(live, "resume", backup_dir=self.backups, output=out)
        obj = next(o for o in load_jsonl(out) if o.get("uuid") == "a")
        text = obj["message"]["content"][0]["text"]
        self.assertNotIn("huge recalled payload", text)
        self.assertIn('<consulted_arcs refs="p7,p8"/>', text)
        self.assertIn("question", text)
        self.assertIn("more", text)


class TestEvictGate(PruneBase):
    def setUp(self):
        super().setUp()
        self.dbpath = os.path.join(self.dir, "fable.db")
        self.objs = [rec("a", "p1", None, "user", "2026-06-01T00:00:01Z",
                         text="indexed content here")]
        self.live = self.make_live(self.objs)

    def test_replace_without_backup_dir_blocked(self):
        with self.assertRaises(PruneGateError):
            prune_file(self.live, "resume", replace=True,
                       db_path=self.dbpath)

    def test_replace_with_backup_and_db_passes_gate(self):
        # protocol: backup -> index backup -> gate sees the immutable copy
        index_vault(self.dbpath, [], live_file=self.live,
                    extract_fn=fts_extract_fn)
        prune_file(self.live, "resume", backup_dir=self.backups,
                   replace=True, db_path=self.dbpath)
        self.assertTrue(any(o.get("uuid") == "a"
                            for o in load_jsonl(self.live)))

    def test_force_overrides_gate(self):
        prune_file(self.live, "resume", replace=True,
                   db_path=self.dbpath, force=True)

    def test_replace_without_db_requires_force(self):
        with self.assertRaises(PruneGateError):
            prune_file(self.live, "resume", backup_dir=self.backups,
                       replace=True)


class TestLifecycleFidelity(PruneBase):
    """F1 regression: full fidelity must survive prune -> reindex cycles."""

    def test_prune_cycle_preserves_full_fidelity(self):
        import glob as globmod
        from fable.recall import get_block
        dbpath = os.path.join(self.dir, "fable.db")
        a = rec("a", "p1", None, "user", "2026-06-01T00:00:01Z", text="go")
        b = rec("b", "p1", "a", "assistant", "2026-06-01T00:00:02Z",
                text="running")
        b["message"]["content"].append(tool_use_block("t1", "Bash",
                                                      {"command": "x"}))
        c = rec("c", "p1", "b", "user", "2026-06-01T00:00:03Z")
        c["message"]["content"] = [tool_result_block("t1", "PAYLOAD " * 2000)]
        live = self.make_live([a, b, c])
        original_c = load_jsonl(live)[2]

        index_vault(dbpath, [], live_file=live, extract_fn=fts_extract_fn)
        prune_file(live, "resume", backup_dir=self.backups, replace=True,
                   db_path=dbpath)
        # the live copy of c is now slim
        slim_c = next(o for o in load_jsonl(live) if o.get("uuid") == "c")
        self.assertEqual(slim_c["message"]["content"][0]["content"],
                         "[pruned]")
        # reindex everything (vault + rewritten live)
        vaults = globmod.glob(os.path.join(self.backups, "*", "*.jsonl"))
        index_vault(dbpath, vaults, live_file=live,
                    extract_fn=fts_extract_fn)
        # recall must return the ORIGINAL full record, not the slim one
        recalled = json.loads(get_block(dbpath, "c"))
        self.assertEqual(recalled, original_c)
        # and search must still find the tool_result content
        from fable.recall import search
        hits = search(dbpath, "PAYLOAD")
        self.assertEqual(hits[0]["prompt_id"], "p1")


class TestAtomicWrite(PruneBase):
    def test_tail_appended_after_snapshot_is_preserved(self):
        from fable.prune import _atomic_write
        from pathlib import Path
        l1 = json.dumps(rec("a", "p", None, "user", text="one")) + "\n"
        l2 = json.dumps(rec("b", "p", "a", "user", text="two")) + "\n"
        live = self.make_live([])
        with open(live, "w") as f:
            f.write(l1)
        snapshot = os.path.getsize(live)
        with open(live, "a") as f:  # session appends after our snapshot
            f.write(l2)
        _atomic_write([json.loads(l1)], Path(live), Path(live), snapshot)
        out = load_jsonl(live)
        self.assertEqual([o["uuid"] for o in out], ["a", "b"])
        self.assertFalse(os.path.exists(live + ".fable-tmp"))


class TestBackupNoClobber(PruneBase):
    def test_gap_in_versions_never_overwrites(self):
        from fable.prune import backup
        from pathlib import Path
        live = self.make_live([rec("a", "p", None, "user", text="x")])
        sdir = os.path.join(self.backups, "live")
        os.makedirs(sdir)
        for name in ("v0-raw.jsonl", "v1-pruned.jsonl", "v3-pruned.jsonl"):
            with open(os.path.join(sdir, name), "w") as f:
                f.write('{"marker":"' + name + '"}\n')
        version, dest = backup(Path(live), Path(self.backups))
        self.assertEqual(version, 4)
        with open(os.path.join(sdir, "v3-pruned.jsonl")) as f:
            self.assertIn("v3-pruned", f.read())  # untouched


if __name__ == "__main__":
    unittest.main()
