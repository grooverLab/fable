"""Auto-prune: threshold trigger, cooldown, disabled-by-default."""
import json
import os
import shutil
import tempfile
import unittest

from fable import db as fdb
from fable.extract import fts_extract_fn
from fable.hook import run_hook
from fable.indexer import index_vault
from tests.helpers import rec, tool_result_block, write_jsonl


def fat_corpus():
    """>2MB live transcript whose last assistant reports 90% context."""
    objs = [rec("a", "p1", None, "user", "2026-06-01T00:00:01Z",
                text="start of session")]
    prev = "a"
    for i in range(60):
        r = rec(f"r{i}", None, prev, "assistant",
                f"2026-06-01T00:{i // 60:02d}:{i % 60:02d}Z")
        r["message"]["content"] = [tool_result_block(f"t{i}", "B" * 40_000)]
        objs.append(r)
        prev = f"r{i}"
    last = rec("z", None, prev, "assistant", "2026-06-01T01:00:00Z",
               text="latest turn")
    last["message"]["usage"] = {"input_tokens": 20_000,
                                "cache_read_input_tokens": 160_000,
                                "cache_creation_input_tokens": 0}
    objs.append(last)
    return objs


class TestAutoPrune(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "fable.db")
        self.live = write_jsonl(os.path.join(self.dir, "live.jsonl"),
                                fat_corpus())
        index_vault(self.db, [], live_file=self.live,
                    extract_fn=fts_extract_fn)
        # the hook derives the backup dir from DEFAULT_BACKUP_ROOTS or
        # <db dir>/backups — the tmp dir has no roots, so it falls back
        self.payload = {"hook_event_name": "PostToolUse",
                        "tool_name": "Bash", "session_id": "s-auto",
                        "transcript_path": self.live}

    def tearDown(self):
        shutil.rmtree(self.dir)
        os.environ.pop("FABLE_CHECKPOINTS", None)

    def _run(self):
        """run_hook with backup roots isolated to the tmp dir — never the
        machine's real vault."""
        from unittest.mock import patch
        with patch("fable.discover.DEFAULT_BACKUP_ROOTS",
                   [os.path.join(self.dir, "realroot")]):
            os.makedirs(os.path.join(self.dir, "realroot"), exist_ok=True)
            return run_hook(self.db, self.payload)

    def _enable(self, pct="50"):
        conn = fdb.connect(self.db)
        conn.execute("INSERT OR REPLACE INTO meta VALUES"
                     "('autoprune_enabled','1')")
        conn.execute("INSERT OR REPLACE INTO meta VALUES"
                     "('autoprune_pct',?)", (pct,))
        conn.commit()
        conn.close()

    def test_disabled_by_default(self):
        os.environ["FABLE_CHECKPOINTS"] = os.path.join(self.dir, "ck")
        before = os.path.getsize(self.live)
        out = self._run()
        self.assertNotIn("system_message", out)
        self.assertEqual(os.path.getsize(self.live), before)

    def test_triggers_above_threshold_with_message_and_cooldown(self):
        os.environ["FABLE_CHECKPOINTS"] = os.path.join(self.dir, "ck")
        self._enable("50")   # last usage = 180k/200k = 90% > 50%
        before = os.path.getsize(self.live)
        out = self._run()
        self.assertIn("auto-prune", out.get("system_message", ""))
        self.assertIn("claude --resume", out["system_message"])
        self.assertLess(os.path.getsize(self.live), before * 0.2)
        # vault backup sealed somewhere under the fallback root
        import glob
        self.assertTrue(glob.glob(os.path.join(
            self.dir, "realroot", "**", "v0-raw.jsonl"), recursive=True))
        # immediate second call: cooldown suppresses
        out2 = self._run()
        self.assertNotIn("system_message", out2)

    def test_below_threshold_no_action(self):
        os.environ["FABLE_CHECKPOINTS"] = os.path.join(self.dir, "ck")
        self._enable("95")   # 90% < 95%
        before = os.path.getsize(self.live)
        out = self._run()
        self.assertNotIn("system_message", out)
        self.assertEqual(os.path.getsize(self.live), before)


if __name__ == "__main__":
    unittest.main()
