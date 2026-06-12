import os
import shutil
import tempfile
import unittest

from fable import db as fdb
from fable.extract import record_text, strip_historical, fts_extract_fn
from fable.indexer import index_vault
from tests.helpers import rec, tool_use_block, tool_result_block, write_jsonl


class TestRecordText(unittest.TestCase):
    def texts(self, obj):
        return record_text(obj)

    def test_user_and_assistant_text(self):
        obj = rec("a", "p", text="fix the zigzag pivot")
        self.assertIn(("text", "fix the zigzag pivot"), self.texts(obj))

    def test_string_content(self):
        obj = rec("a", "p")
        obj["message"]["content"] = "plain string body"
        self.assertIn(("text", "plain string body"), self.texts(obj))

    def test_thinking_extracted(self):
        obj = rec("a", "p")
        obj["message"]["content"] = [{"type": "thinking",
                                      "thinking": "the pool is unbounded",
                                      "signature": "x" * 500}]
        out = self.texts(obj)
        self.assertIn(("thinking", "the pool is unbounded"), out)
        # signature never leaks into searchable text
        self.assertFalse(any("xxxx" in t for _, t in out))

    def test_tool_use_inputs_flattened(self):
        obj = rec("a", "p")
        obj["message"]["content"] = [tool_use_block("t1", "Bash", {
            "command": "cargo test -p sled-zigzag",
            "description": "run zigzag tests",
            "timeout": 5000})]
        out = self.texts(obj)
        joined = " ".join(t for _, t in out)
        self.assertIn("cargo test -p sled-zigzag", joined)
        self.assertIn("run zigzag tests", joined)
        self.assertNotIn("5000", joined)  # non-text fields skipped

    def test_tool_result_capped(self):
        obj = rec("a", "p", rtype="user")
        obj["message"]["content"] = [tool_result_block("t1", "line\n" * 4096)]
        out = self.texts(obj)
        result_text = next(t for k, t in out if k == "tool_result")
        self.assertLessEqual(len(result_text), 2048)

    def test_images_and_base64_dropped(self):
        obj = rec("a", "p")
        obj["message"]["content"] = [
            {"type": "image", "source": {"type": "base64", "data": "AAAA" * 5000}},
            {"type": "text", "text": "see screenshot"},
        ]
        out = self.texts(obj)
        joined = " ".join(t for _, t in out)
        self.assertIn("see screenshot", joined)
        self.assertNotIn("AAAA", joined)

    def test_long_base64ish_tokens_stripped_from_text(self):
        blob = "iVBORw0KGgoAAAANSUhEUg" + "Ab0cD1" * 200
        obj = rec("a", "p", text=f"header {blob} trailer")
        joined = " ".join(t for _, t in record_text(obj))
        self.assertIn("header", joined)
        self.assertIn("trailer", joined)
        self.assertNotIn(blob, joined)


class TestInceptionGuard(unittest.TestCase):
    def test_strip_historical_removes_span_and_reports_refs(self):
        text = ('before <historical_context session="s" thread="p9" arcs="p9">'
                "old recalled turns here</historical_context> after")
        cleaned, refs = strip_historical(text)
        self.assertEqual(cleaned, "before  after")
        self.assertEqual(refs, ["p9"])

    def test_multiple_spans(self):
        text = ('<historical_context arcs="a1">x</historical_context> mid '
                '<historical_context arcs="a2,a3">y</historical_context>')
        cleaned, refs = strip_historical(text)
        self.assertEqual(cleaned.strip(), "mid")
        self.assertEqual(sorted(refs), ["a1", "a2", "a3"])

    def test_unclosed_span_dropped_to_end(self):
        text = 'keep <historical_context arcs="a1">never closed...'
        cleaned, refs = strip_historical(text)
        self.assertEqual(cleaned.strip(), "keep")
        self.assertEqual(refs, ["a1"])


class TestFtsIntegration(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def index(self, objs):
        live = write_jsonl(os.path.join(self.dir, "live.jsonl"), objs)
        return index_vault(self.dbpath, vault_files=[], live_file=live,
                           extract_fn=fts_extract_fn)

    def test_search_hits_with_prompt_id(self):
        self.index([
            rec("a", "p1", None, "user", text="fix the zigzag pivot detection"),
            rec("b", "p2", None, "user", text="build the option chain table"),
        ])
        conn = fdb.connect(self.dbpath)
        rows = conn.execute(
            "SELECT uuid, prompt_id FROM fts WHERE fts MATCH 'zigzag'").fetchall()
        conn.close()
        self.assertEqual(rows, [("a", "p1")])

    def test_recalled_context_not_indexed_and_citation_recorded(self):
        self.index([rec("a", "p1", None, "user",
                        text='question <historical_context arcs="p7">zigzag '
                             "history paste</historical_context> tail")])
        conn = fdb.connect(self.dbpath)
        hits = conn.execute("SELECT uuid FROM fts WHERE fts MATCH 'zigzag'").fetchall()
        cites = conn.execute("SELECT from_uuid, ref FROM citations").fetchall()
        conn.close()
        self.assertEqual(hits, [])
        self.assertEqual(cites, [("a", "p7")])

    def test_reindex_does_not_duplicate_fts_rows(self):
        objs = [rec("a", "p1", None, "user", text="unique marker phrase")]
        live = write_jsonl(os.path.join(self.dir, "live.jsonl"), objs)
        index_vault(self.dbpath, [], live_file=live, extract_fn=fts_extract_fn)
        # touch mtime to force rescan
        os.utime(live, (os.stat(live).st_atime, os.stat(live).st_mtime + 5))
        index_vault(self.dbpath, [], live_file=live, extract_fn=fts_extract_fn)
        conn = fdb.connect(self.dbpath)
        n = conn.execute(
            "SELECT COUNT(*) FROM fts WHERE fts MATCH 'marker'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
