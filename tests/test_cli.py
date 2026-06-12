import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from fable.cli import main
from tests.test_recall import build_corpus


class TestCliSmoke(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")
        self.live, self.objs = build_corpus(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--db", self.dbpath] + list(argv))
        return code, buf.getvalue()

    def test_index_search_thread_block_stats(self):
        code, out = self.run_cli("index", "--live", self.live)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["records_indexed"], 8)

        code, out = self.run_cli("search", "zigzag", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["prompt_id"], "p1")

        code, out = self.run_cli("thread", "p1", "--budget", "500")
        self.assertEqual(code, 0)
        self.assertIn("please fix the zigzag pivot detection", out)

        code, out = self.run_cli("block", "b")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), self.objs["b"])

        code, out = self.run_cli("stats")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["records"], 8)

    def test_missing_uuid_exits_nonzero(self):
        self.run_cli("index", "--live", self.live)
        code, _ = self.run_cli("block", "nope")
        self.assertEqual(code, 2)

    def test_search_no_matches_exits_one(self):
        self.run_cli("index", "--live", self.live)
        code, out = self.run_cli("search", "quantumfoam")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
