"""Backfill queue + cross-process stop flag (no provider calls)."""
import os
import shutil
import tempfile
import unittest

from fable import cards
from fable.extract import fts_extract_fn
from fable.indexer import index_vault
from fable.terms import index_terms
from tests.helpers import rec, write_jsonl


class TestQueueStop(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "f.db")
        objs = [rec("a", "p1", None, "user", "2026-06-01T00:00:01Z",
                    text="x " * 2000),
                rec("b", "p1", "a", "assistant", "2026-06-01T00:00:02Z",
                    text="y " * 2000)]
        live = write_jsonl(os.path.join(self.dir, "live.jsonl"), objs)
        index_vault(self.db, [], live_file=live, extract_fn=fts_extract_fn)
        index_terms(self.db)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_queue_fifo_and_remove(self):
        self.assertEqual(cards.enqueue_job(self.db, {"label": "a"}), 1)
        self.assertEqual(cards.enqueue_job(self.db, {"label": "b"}), 2)
        self.assertEqual(
            cards.read_backfill_state(self.db)["queue"][0]["label"], "a")
        cards.remove_job(self.db, 0)
        self.assertEqual(
            cards.read_backfill_state(self.db)["queue"][0]["label"], "b")
        self.assertEqual(cards.pop_job(self.db)["label"], "b")
        self.assertIsNone(cards.pop_job(self.db))

    def test_request_stop_clears_queue(self):
        cards.enqueue_job(self.db, {"label": "a"})
        cards.request_stop(self.db)
        st = cards.read_backfill_state(self.db)
        self.assertTrue(st["stop"])
        self.assertEqual(st["queue"], [])
        cards.clear_stop(self.db)
        self.assertFalse(cards.read_backfill_state(self.db)["stop"])

    def test_run_cards_honors_db_stop_flag(self):
        # a stop issued from anywhere (dashboard, another process) must halt
        # a run even when this process passed no should_stop callback
        cards.request_stop(self.db)
        st = cards.run_cards(self.db, provider="openrouter")  # no API call:
        self.assertTrue(st.get("stopped"))                   # breaks at i=1
        self.assertEqual(st["generated"], 0)


if __name__ == "__main__":
    unittest.main()
