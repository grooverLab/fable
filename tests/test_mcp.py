import json
import os
import shutil
import subprocess
import tempfile
import unittest

from fable.extract import fts_extract_fn
from fable.indexer import index_vault
from tests.helpers import rec, write_jsonl

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestMcpServer(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")
        live = write_jsonl(os.path.join(self.dir, "live.jsonl"), [
            rec("a", "p1", None, "user", "2026-06-01T00:00:01Z",
                text="fix the zigzag pivot detection"),
            rec("b", "p1", "a", "assistant", "2026-06-01T00:00:02Z",
                text="zigzag pivot fixed"),
        ])
        index_vault(self.dbpath, [], live_file=live,
                    extract_fn=fts_extract_fn)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def rpc(self, messages):
        proc = subprocess.run(
            ["python3", "-m", "fable", "--db", self.dbpath, "mcp"],
            input="\n".join(json.dumps(m) for m in messages) + "\n",
            capture_output=True, text=True, cwd=REPO, timeout=30)
        return [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]

    def test_initialize_list_call_roundtrip(self):
        out = self.rpc([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "fable_search",
                        "arguments": {"query": "zigzag"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "fable_thread",
                        "arguments": {"prompt_id": "p1", "budget": 500}}},
        ])
        by_id = {m["id"]: m for m in out}
        self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "fable")
        names = {t["name"] for t in by_id[2]["result"]["tools"]}
        self.assertEqual(names, {"fable_search", "fable_thread",
                                 "fable_block", "fable_context",
                                 "fable_remember"})
        hits = json.loads(by_id[3]["result"]["content"][0]["text"])
        self.assertEqual(hits[0]["prompt_id"], "p1")
        self.assertIn("fix the zigzag pivot detection",
                      by_id[4]["result"]["content"][0]["text"])

    def test_tool_error_is_in_band(self):
        out = self.rpc([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "fable_block",
                        "arguments": {"uuid": "nope"}}},
        ])
        self.assertTrue(out[0]["result"]["isError"])
        self.assertIn("nope", out[0]["result"]["content"][0]["text"])

    def test_unknown_method_errors(self):
        out = self.rpc([{"jsonrpc": "2.0", "id": 9, "method": "bogus/x"}])
        self.assertEqual(out[0]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
