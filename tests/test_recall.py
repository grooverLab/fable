import json
import os
import shutil
import tempfile
import unittest

from fable import db as fdb
from fable.extract import fts_extract_fn
from fable.indexer import index_vault
from fable.recall import search, render_thread, get_block
from fable.terms import index_terms
from fable.threads import reconstruct
from tests.helpers import (rec, tool_use_block, tool_result_block, write_jsonl)


def build_corpus(dirpath):
    """Two threads: p1 zigzag work (tools, image, edit-branch, sidechain),
    p2 option chain chat."""
    a = rec("a", "p1", None, "user", "2026-06-01T00:00:01Z",
            text="please fix the zigzag pivot detection")
    b = rec("b", "p1", "a", "assistant", "2026-06-01T00:00:02Z",
            text="I decided to rewrite the pivot scan in sled-zigzag")
    b["message"]["content"].append(
        tool_use_block("t1", "Bash", {"command": "cargo test -p sled-zigzag",
                                      "description": "run tests"}))
    c = rec("c", "p1", "b", "user", "2026-06-01T00:00:03Z")
    c["message"]["content"] = [
        tool_result_block("t1", "test zigzag_pivots ... ok\n" * 800),
        {"type": "image", "source": {"type": "base64", "data": "A" * 2000}},
    ]
    d = rec("d", "p1", "c", "assistant", "2026-06-01T00:00:04Z",
            text="all 14 zigzag tests pass now")
    # edit-branch: an abandoned retry hanging off b
    x = rec("x", "p1", "b", "assistant", "2026-06-01T00:00:03.500Z",
            text="abandoned alternative answer")
    # sidechain record (subagent transcript) — attaches via source uuid
    s = rec("s", "p1", None, "assistant", "2026-06-01T00:00:03.700Z",
            text="subagent exploration notes", sidechain=True,
            extra={"sourceToolAssistantUUID": "b"})

    e = rec("e", "p2", "d", "user", "2026-06-01T00:01:01Z",
            text="now build the option chain rotation table")
    f = rec("f", "p2", "e", "assistant", "2026-06-01T00:01:02Z",
            text="building the option chain table with OI columns")

    live = write_jsonl(os.path.join(dirpath, "live.jsonl"),
                       [a, b, c, x, s, d, e, f])
    return live, {"a": a, "b": b, "c": c, "d": d, "x": x, "s": s, "e": e, "f": f}


class RecallBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")
        self.live, self.objs = build_corpus(self.dir)
        index_vault(self.dbpath, [], live_file=self.live,
                    extract_fn=fts_extract_fn)
        index_terms(self.dbpath)

    def tearDown(self):
        shutil.rmtree(self.dir)


class TestThreadReconstruction(RecallBase):
    def test_canonical_chain_order_and_orphans(self):
        conn = fdb.connect(self.dbpath)
        view = reconstruct(conn, "p1")
        conn.close()
        self.assertEqual([t.uuid for t in view.main], ["a", "b", "c", "d"])
        self.assertEqual([t.uuid for t in view.orphans], ["x"])
        self.assertEqual([t.uuid for t in view.sidechains], ["s"])


class TestBlock(RecallBase):
    def test_block_byte_identical(self):
        raw = get_block(self.dbpath, "b")
        self.assertEqual(json.loads(raw), self.objs["b"])


class TestRenderThread(RecallBase):
    def test_text_verbatim_and_toolresult_elided_under_budget(self):
        out = render_thread(self.dbpath, "p1", budget=500)
        self.assertIn("please fix the zigzag pivot detection", out)
        self.assertIn("all 14 zigzag tests pass now", out)
        self.assertIn("cargo test -p sled-zigzag", out)
        self.assertIn("[truncated — fable block c]", out)
        self.assertIn("[image]", out)
        self.assertNotIn("A" * 100, out)
        # budget approximately respected (chars ~= 4 * tokens, generous slack)
        self.assertLess(len(out), 500 * 4 * 2)

    def test_full_toolresult_when_budget_allows(self):
        out = render_thread(self.dbpath, "p1", budget=50000)
        self.assertNotIn("[truncated", out)
        self.assertIn("test zigzag_pivots ... ok", out)

    def test_sentinel_wrapping(self):
        out = render_thread(self.dbpath, "p1", budget=500)
        self.assertTrue(out.startswith("<historical_context "))
        self.assertIn('thread="p1"', out)
        self.assertIn('arcs="p1"', out)
        self.assertTrue(out.rstrip().endswith("</historical_context>"))
        bare = render_thread(self.dbpath, "p1", budget=500, sentinel=False)
        self.assertNotIn("historical_context", bare)

    def test_orphans_and_sidechains_labeled(self):
        out = render_thread(self.dbpath, "p1", budget=50000)
        self.assertIn("abandoned alternative answer", out)
        self.assertIn("edit-branch", out)
        self.assertIn("sidechain", out)

    def test_raw_mode_returns_verbatim_jsonl(self):
        out = render_thread(self.dbpath, "p1", budget=10 ** 9, raw=True,
                            sentinel=False)
        lines = [json.loads(l) for l in out.strip().splitlines()]
        self.assertEqual(lines[0], self.objs["a"])
        self.assertEqual(lines[1], self.objs["b"])


class TestSearch(RecallBase):
    def test_search_ranks_threads(self):
        hits = search(self.dbpath, "zigzag pivot")
        self.assertEqual(hits[0]["prompt_id"], "p1")
        self.assertGreaterEqual(hits[0]["matches"], 2)

    def test_search_other_thread(self):
        hits = search(self.dbpath, "option chain")
        self.assertEqual(hits[0]["prompt_id"], "p2")

    def test_facet_intersection(self):
        hits = search(self.dbpath, "zigzag", operative="decide",
                      target="sled-zigzag")
        self.assertEqual([h["prompt_id"] for h in hits], ["p1"])
        none = search(self.dbpath, "zigzag", operative="deploy")
        self.assertEqual(none, [])

    def test_search_includes_thread_metadata(self):
        hits = search(self.dbpath, "zigzag")
        h = hits[0]
        self.assertIn("turn_count", h)
        self.assertIn("est_tokens", h)
        self.assertIn("first_ts", h)


if __name__ == "__main__":
    unittest.main()
