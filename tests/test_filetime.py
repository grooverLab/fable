"""File time-travel: timeline assembly, reconstruction, divergence, diff."""
import os
import shutil
import tempfile
import unittest

from fable.extract import fts_extract_fn
from fable.filetime import file_events, reconstruct, file_diff, known_files
from fable.indexer import index_vault
from fable.terms import index_terms
from tests.helpers import rec, tool_use_block, write_jsonl



def _b(r, blocks):
    r["message"]["content"] = blocks
    return r

def corpus(dirpath):
    objs = []
    # v0: full Write
    objs.append(rec("w0", "p1", None, "user", "2026-05-01T10:00:00Z",
                    text="create the config loader in src/loader.py"))
    objs.append(_b(rec("a0", None, "w0", "assistant", "2026-05-01T10:01:00Z"), [tool_use_block(
                        "t0", "Write",
                        {"file_path": "/repo/src/loader.py",
                         "content": "def load():\n    return None\n"})]))
    # v1: Edit
    objs.append(_b(rec("a1", None, "a0", "assistant",
                       "2026-05-01T10:02:00Z"), [tool_use_block(
                        "t1", "Edit",
                        {"file_path": "/repo/src/loader.py",
                         "old_string": "return None",
                         "new_string": "return json.load(open(PATH))"})]))
    # v2: MultiEdit in a later session-time
    objs.append(_b(rec("a2", None, "a1", "assistant", "2026-05-01T10:03:00Z"), [tool_use_block(
                        "t2", "MultiEdit",
                        {"file_path": "/repo/src/loader.py",
                         "edits": [
                            {"old_string": "def load():",
                             "new_string": "def load(path=PATH):"},
                            {"old_string": "open(PATH)",
                             "new_string": "open(path)"}]})]))
    # an Edit on a DIFFERENT file — must not appear in loader.py history
    objs.append(_b(rec("a3", None, "a2", "assistant", "2026-05-01T10:04:00Z"), [tool_use_block(
                        "t3", "Edit",
                        {"file_path": "/repo/src/other.py",
                         "old_string": "x", "new_string": "y"})]))
    # v3: a pruned-away Edit (inputs stubbed) then a recovering Write
    objs.append(_b(rec("a4", None, "a3", "assistant", "2026-05-01T10:05:00Z"), [tool_use_block(
                        "t4", "Edit",
                        {"file_path": "/repo/src/loader.py",
                         "old_string": "", "new_string": ""})]))
    objs.append(_b(rec("a5", None, "a4", "assistant", "2026-05-01T10:06:00Z"), [tool_use_block(
                        "t5", "Write",
                        {"file_path": "/repo/src/loader.py",
                         "content": "def load(path):\n    return read(path)\n"})]))
    return write_jsonl(os.path.join(dirpath, "live.jsonl"), objs)


class TestFileTime(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")
        live = corpus(self.dir)
        index_vault(self.dbpath, [], live_file=live,
                    extract_fn=fts_extract_fn, project="t")
        index_terms(self.dbpath)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_timeline_only_matching_file(self):
        events = file_events(self.dbpath, "src/loader.py")
        self.assertEqual([e["tool"] for e in events],
                         ["Write", "Edit", "MultiEdit", "Edit", "Write"])
        self.assertTrue(all(e["file_path"].endswith("loader.py")
                            for e in events))

    def test_reconstruction_and_divergence(self):
        versions = reconstruct(file_events(self.dbpath, "src/loader.py"))
        self.assertIn("json.load", versions[1]["content"])
        self.assertIn("def load(path=PATH):", versions[2]["content"])
        self.assertFalse(versions[3]["ok"])          # pruned edit breaks chain
        self.assertIsNone(versions[3]["content"])
        self.assertTrue(versions[4]["ok"])           # Write recovers
        self.assertIn("read(path)", versions[4]["content"])

    def test_diff_between_versions(self):
        versions = reconstruct(file_events(self.dbpath, "src/loader.py"))
        diff = "\n".join(file_diff(versions, 0, 2))
        self.assertIn("-def load():", diff)
        self.assertIn("+def load(path=PATH):", diff)
        with self.assertRaises(ValueError):
            file_diff(versions, 0, 3)               # broken version refuses

    def test_known_files(self):
        files = known_files(self.dbpath, "loader")
        self.assertTrue(any("loader.py" in f["path"] for f in files))


if __name__ == "__main__":
    unittest.main()
