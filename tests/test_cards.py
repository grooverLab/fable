import json
import os
import shutil
import tempfile
import unittest

from fable import db as fdb
from fable.cards import run_cards, parse_card, CardError
from fable.extract import fts_extract_fn
from fable.indexer import index_vault
from tests.helpers import rec, write_jsonl
from tests.test_openrouter import MockServerBase, ok_body

GOOD_CARD = {
    "title": "Fix zigzag pivot detection",
    "type": "workflow",
    "topics": ["zigzag", "pivot"],
    "decisions": ["rewrite pivot scan"],
    "files": ["sled-zigzag/src/lib.rs"],
    "outcome": "tests pass",
    "summary": "Rewrote the pivot scan; 14 tests green.",
}


class TestParseCard(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(parse_card(json.dumps(GOOD_CARD))["title"],
                         GOOD_CARD["title"])

    def test_fenced_json(self):
        text = "```json\n" + json.dumps(GOOD_CARD) + "\n```"
        self.assertEqual(parse_card(text)["type"], "workflow")

    def test_chatter_around_json(self):
        text = "Here is the card:\n" + json.dumps(GOOD_CARD) + "\nHope it helps!"
        self.assertEqual(parse_card(text)["outcome"], "tests pass")

    def test_invalid_type_coerced(self):
        # has decisions -> decision; without decisions -> workflow
        bad = dict(GOOD_CARD, type="epic saga")
        self.assertEqual(parse_card(json.dumps(bad))["type"], "decision")
        bad2 = dict(GOOD_CARD, type="epic saga", decisions=[])
        self.assertEqual(parse_card(json.dumps(bad2))["type"], "workflow")

    def test_missing_title_raises(self):
        bad = {k: v for k, v in GOOD_CARD.items() if k != "title"}
        with self.assertRaises(CardError):
            parse_card(json.dumps(bad))

    def test_no_json_raises(self):
        with self.assertRaises(CardError):
            parse_card("I cannot help with that.")


class TestRunCards(MockServerBase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")
        objs = []
        for pid in ("p1", "p2"):
            for i in range(3):
                objs.append(rec(f"{pid}-{i}", pid,
                                f"{pid}-{i-1}" if i else None,
                                "user" if i % 2 == 0 else "assistant",
                                f"2026-06-01T00:0{i}:00Z",
                                text=f"substantial discussion about {pid} "
                                     "zigzag pivots and option chains " * 30))
        live = write_jsonl(os.path.join(self.dir, "live.jsonl"), objs)
        index_vault(self.dbpath, [], live_file=live, extract_fn=fts_extract_fn)

    def tearDown(self):
        shutil.rmtree(self.dir)
        super().tearDown()

    def kw(self):
        return dict(model="test-model", api_key="test-key",
                    base_url=self.base, retry_wait=0.01)

    def test_generates_and_stores_cards(self):
        self.server.script = [(200, ok_body(json.dumps(GOOD_CARD)))] * 2
        stats = run_cards(self.dbpath, **self.kw())
        self.assertEqual(stats["generated"], 2)
        conn = fdb.connect(self.dbpath)
        rows = conn.execute(
            "SELECT prompt_id, title, source, model FROM cards").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][2], "openrouter")

    def test_resume_skips_existing(self):
        self.server.script = [(200, ok_body(json.dumps(GOOD_CARD)))] * 2
        run_cards(self.dbpath, **self.kw())
        stats = run_cards(self.dbpath, **self.kw())
        self.assertEqual(stats["generated"], 0)
        self.assertEqual(stats["skipped_existing"], 2)

    def test_repair_retry_on_invalid_json(self):
        self.server.script = [
            (200, ok_body("not json at all")),
            (200, ok_body(json.dumps(GOOD_CARD))),   # repair for thread 1
            (200, ok_body(json.dumps(GOOD_CARD))),   # thread 2
        ]
        stats = run_cards(self.dbpath, **self.kw())
        self.assertEqual(stats["generated"], 2)

    def test_failures_recorded_not_fatal(self):
        self.server.script = [
            (200, ok_body("garbage")),
            (200, ok_body("more garbage")),          # repair also fails -> fail
            (200, ok_body(json.dumps(GOOD_CARD))),   # second thread fine
        ]
        stats = run_cards(self.dbpath, **self.kw())
        self.assertEqual(stats["generated"], 1)
        self.assertEqual(stats["failed"], 1)

    def test_run_level_backoff_retries_same_thread(self):
        # chat-level retries off (retries=0): a 429 surfaces immediately and
        # the run-level loop must back off and retry the SAME thread
        self.server.script = [
            (429, {"error": "rate"}),
            (200, ok_body(json.dumps(GOOD_CARD))),   # thread 1, attempt 2
            (200, ok_body(json.dumps(GOOD_CARD))),   # thread 2
        ]
        waits = []
        stats = run_cards(self.dbpath, retries=0, thread_retries=2,
                          backoff_schedule=[0.01, 0.02],
                          sleep_fn=waits.append, **self.kw())
        self.assertEqual(stats["generated"], 2)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(waits, [0.01])

    def test_abort_after_consecutive_hard_failures(self):
        # persistent 429s (daily cap): do not burn the whole list — abort
        self.server.script = [(429, {"error": "rate"})] * 6
        stats = run_cards(self.dbpath, retries=0, thread_retries=0,
                          abort_after=2, sleep_fn=lambda s: None, **self.kw())
        self.assertTrue(stats["aborted"])
        self.assertEqual(stats["failed"], 2)
        self.assertEqual(stats["generated"], 0)

    def test_limit_and_min_tokens(self):
        self.server.script = [(200, ok_body(json.dumps(GOOD_CARD)))]
        stats = run_cards(self.dbpath, limit=1, **self.kw())
        self.assertEqual(stats["generated"], 1)
        stats = run_cards(self.dbpath, min_tokens=10 ** 9, **self.kw())
        self.assertEqual(stats["generated"], 0)


if __name__ == "__main__":
    unittest.main()
