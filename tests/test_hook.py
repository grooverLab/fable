import json
import os
import shutil
import subprocess
import tempfile
import unittest

from fable import db as fdb
from tests.helpers import rec, write_jsonl

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestPreCompactHook(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")
        proj = os.path.join(self.dir, "-Users-x-myproj")
        self.live = write_jsonl(os.path.join(proj, "s-hook.jsonl"), [
            rec("a", "p1", None, "user", text="precious context about auth"),
            rec("b", "p1", "a", "assistant", text="decided on jwt rotation"),
        ])

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_hook(self, payload):
        # isolate the vault to the sandbox so the hook never writes to the
        # machine's real ~/.fable/vault
        from fable.hook import run_hook
        from unittest.mock import patch
        with patch("fable.paths.vault_dir",
                   return_value=os.path.join(self.dir, "vault")):
            return run_hook(self.dbpath, payload)

    def test_seals_backup_and_indexes_before_compaction(self):
        result = self.run_hook({
            "hook_event_name": "PreCompact",
            "session_id": "s-hook",
            "transcript_path": self.live,
        })
        self.assertTrue(result["ok"])
        self.assertTrue(os.path.exists(result["backup"]))
        # the content is now searchable even if compaction wrecks the live file
        with open(self.live, "w") as f:
            f.write("")  # simulate the documented compaction race wipe
        from fable.recall import search, get_block
        hits = search(self.dbpath, "jwt rotation")
        self.assertEqual(hits[0]["prompt_id"], "p1")
        self.assertIn("precious context", get_block(self.dbpath, "a"))

    def test_missing_transcript_is_quiet(self):
        result = self.run_hook({"hook_event_name": "PreCompact",
                                "transcript_path": "/nope.jsonl"})
        self.assertFalse(result["ok"])

    def test_cli_never_fails(self):
        proc = subprocess.run(
            ["python3", "-m", "fable", "--db", self.dbpath, "hook"],
            input="not even json", capture_output=True, text=True,
            cwd=REPO, timeout=30)
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
