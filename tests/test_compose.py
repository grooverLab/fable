import json
import os
import shutil
import tempfile
import unittest

from fable.compose import compose
from fable.extract import fts_extract_fn
from fable.indexer import index_vault
from fable.prune import validate
from tests.helpers import rec, tool_use_block, tool_result_block, write_jsonl


class TestCompose(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")
        self.projects = os.path.join(self.dir, "projects")
        # two threads, as if from different work
        a = rec("a", "p1", None, "user", "2026-06-01T00:00:01Z",
                text="design the zigzag pivot")
        b = rec("b", "p1", "a", "assistant", "2026-06-01T00:00:02Z",
                text="pivot designed with ATR depth")
        b["message"]["content"].append(tool_use_block("t1", "Bash",
                                                      {"command": "test"}))
        c = rec("c", "p1", "b", "user", "2026-06-01T00:00:03Z")
        c["message"]["content"] = [tool_result_block("t1", "ok")]
        d = rec("d", "p2", "c", "user", "2026-06-02T00:00:01Z",
                text="now the option chain")
        e = rec("e", "p2", "d", "assistant", "2026-06-02T00:00:02Z",
                text="chain table built")
        e["message"]["content"].append({"type": "thinking",
                                        "thinking": "internal",
                                        "signature": "SIG"})
        live = write_jsonl(os.path.join(self.dir, "live.jsonl"),
                           [a, b, c, d, e])
        index_vault(self.dbpath, [], live_file=live,
                    extract_fn=fts_extract_fn)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def load(self, path):
        return [json.loads(l) for l in open(path) if l.strip()]

    def test_compose_reordered_workspace(self):
        # user picks p2 FIRST, then p1 — their order, not chronology
        result = compose(self.dbpath, ["p2", "p1"], "option chain focus",
                         cwd="/Users/x/work", projects_dir=self.projects)
        recs = self.load(result["path"])
        self.assertEqual(recs[0]["type"], "custom-title")
        self.assertIn("option chain focus", recs[0]["customTitle"])
        # order: seam, p2 turns, seam, p1 turns
        texts = json.dumps(recs)
        self.assertLess(texts.find("now the option chain"),
                        texts.find("design the zigzag pivot"))
        # seams present and explanatory
        seams = [r for r in recs if r.get("fableSeam")]
        self.assertEqual(len(seams), 2)
        self.assertIn("restitched by fable compose",
                      seams[0]["message"]["content"][0]["text"])
        # fresh uuids + provenance, chain valid, session ids consistent
        msg_recs = [r for r in recs if r.get("uuid")]
        self.assertTrue(all(r["sessionId"] == result["session_id"]
                            for r in msg_recs))
        originals = {"a", "b", "c", "d", "e"}
        self.assertFalse(originals & {r["uuid"] for r in msg_recs})
        self.assertEqual({r.get("fableComposedFrom")
                          for r in msg_recs} - {None}, originals)
        report = validate(msg_recs)
        self.assertTrue(report["chain_valid"])
        self.assertEqual(report["tool_orphans"], 0)
        # signed thinking preserved by default (experiment-validated)
        self.assertIn("SIG", texts)
        self.assertIn("claude --resume " + result["session_id"],
                      result["resume"])

    def test_strip_thinking(self):
        result = compose(self.dbpath, ["p2"], "no thinking",
                         cwd="/Users/x/work", projects_dir=self.projects,
                         strip_thinking=True)
        self.assertNotIn("SIG", open(result["path"]).read())

    def test_card_mode_weaves_summary_record(self):
        import sqlite3
        from fable.cards import parse_card, store_card
        from fable import db as fdb
        conn = fdb.connect(self.dbpath)
        store_card(conn, "p1", parse_card(json.dumps({
            "title": "Zigzag pivot design", "type": "decision",
            "topics": ["zigzag"], "decisions": ["chose ATR depth"],
            "files": [], "outcome": "done",
            "summary": "Pivot uses ATR-scaled depth."})),
            source="test", model="t")
        conn.close()
        result = compose(self.dbpath,
                         [{"id": "p1", "mode": "card"}, "p2"],
                         "mixed fidelity", cwd="/Users/x/work",
                         projects_dir=self.projects)
        text = open(result["path"]).read()
        # card summary present, raw p1 records absent, p2 raw present
        self.assertIn("chose ATR depth", text)
        self.assertNotIn("design the zigzag pivot", text)
        self.assertIn("now the option chain", text)
        self.assertIn("fable_thread p1", text)

    def test_card_mode_without_card_errors(self):
        with self.assertRaises(KeyError):
            compose(self.dbpath, [{"id": "p2", "mode": "card"}], "x",
                    cwd="/x", projects_dir=self.projects)

    def test_cwd_encoding_matches_claude_code(self):
        from fable.compose import _encode_cwd
        # verified against real ~/.claude/projects dirs: underscores,
        # dots and slashes all become dashes
        self.assertEqual(
            _encode_cwd("/Users/x/Desktop/Trading/01_Algos/PineScript"),
            "-Users-x-Desktop-Trading-01-Algos-PineScript")
        self.assertEqual(_encode_cwd("/a/b.c/d_e f"), "-a-b-c-d-e-f")

    def test_unknown_threads_error(self):
        with self.assertRaises(KeyError):
            compose(self.dbpath, ["nope"], "x", cwd="/x",
                    projects_dir=self.projects)


if __name__ == "__main__":
    unittest.main()
