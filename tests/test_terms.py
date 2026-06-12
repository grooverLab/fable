import os
import shutil
import tempfile
import unittest

from fable import db as fdb
from fable.extract import fts_extract_fn
from fable.indexer import index_vault
from fable.terms import (match_operatives, extract_targets, extract_wikilinks,
                         index_terms)
from tests.helpers import rec, write_jsonl


class TestOperatives(unittest.TestCase):
    def test_stem_variants_normalize(self):
        text = "I fixed the bug after investigating, now planning the refactor"
        ops = match_operatives(text)
        self.assertIn("fix", ops)
        self.assertIn("investigate", ops)
        self.assertIn("plan", ops)
        self.assertIn("refactor", ops)

    def test_decide_reject_pair(self):
        ops = match_operatives("we decided to use sqlite and rejected mongo")
        self.assertIn("decide", ops)
        self.assertIn("reject", ops)

    def test_plain_english_not_operatives(self):
        ops = match_operatives("the weather was nice and we walked home")
        self.assertEqual(dict(ops), {})


class TestTargets(unittest.TestCase):
    def test_paths_and_files(self):
        t = extract_targets(
            "see crates/vj-server/src/ws_voice.rs and /abs/etc/config.yaml "
            "plus standalone main.py")
        self.assertIn("crates/vj-server/src/ws_voice.rs", t)
        self.assertIn("/abs/etc/config.yaml", t)
        self.assertIn("main.py", t)

    def test_identifiers(self):
        t = extract_targets("call iter_records then ConnectionPool.acquire via "
                            "promptId and sled-zigzag")
        self.assertIn("iter_records", t)
        self.assertIn("ConnectionPool", t)
        self.assertIn("promptId", t)
        self.assertIn("sled-zigzag", t)

    def test_backtick_spans(self):
        t = extract_targets("run `fable thread p9 --budget 8000` to retrieve")
        self.assertIn("fable thread p9 --budget 8000", t)

    def test_plain_english_rejected(self):
        t = extract_targets("The Simple answer is that nothing here matters.")
        self.assertEqual(dict(t), {})


class TestWikilinks(unittest.TestCase):
    def test_extracted(self):
        self.assertEqual(extract_wikilinks("the [[ZigZag]] uses [[fix]]"),
                         ["ZigZag", "fix"])


class TestThreadConcepts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")
        objs = []
        # thread p1: heavily about option chain rotation
        for i in range(6):
            objs.append(rec(f"a{i}", "p1", None, "user",
                            text="the option chain rotation needs strike "
                                 "selection and open interest filters"))
        # thread p2: heavily about zigzag pivots
        for i in range(6):
            objs.append(rec(f"b{i}", "p2", None, "user",
                            text="zigzag pivot depth and reversal threshold "
                                 "tuning for trend detection"))
        # thread p3: generic chatter
        objs.append(rec("c0", "p3", None, "user",
                        text="thanks, that looks good to me"))
        live = write_jsonl(os.path.join(self.dir, "live.jsonl"), objs)
        index_vault(self.dbpath, [], live_file=live, extract_fn=fts_extract_fn)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_concepts_discriminate_threads(self):
        index_terms(self.dbpath, top_k=5)
        conn = fdb.connect(self.dbpath)
        p1 = [r[0] for r in conn.execute(
            "SELECT term FROM terms WHERE prompt_id='p1' AND kind='concept' "
            "ORDER BY score DESC").fetchall()]
        p2 = [r[0] for r in conn.execute(
            "SELECT term FROM terms WHERE prompt_id='p2' AND kind='concept' "
            "ORDER BY score DESC").fetchall()]
        conn.close()
        self.assertTrue(any("option chain" in t for t in p1), p1)
        self.assertTrue(any("zigzag" in t or "pivot" in t for t in p2), p2)
        # no cross-contamination at the top
        self.assertFalse(any("zigzag" in t for t in p1), p1)

    def test_operatives_and_targets_stored_per_thread(self):
        conn = fdb.connect(self.dbpath)
        conn.execute("DELETE FROM fts")
        conn.execute(
            "INSERT INTO fts(content, uuid, prompt_id, kind) VALUES(?,?,?,?)",
            ("I fixed sled_signals.rs after investigating", "z1", "p9", "text"))
        conn.commit()
        conn.close()
        index_terms(self.dbpath, top_k=5)
        conn = fdb.connect(self.dbpath)
        rows = {(r[0], r[1]) for r in conn.execute(
            "SELECT term, kind FROM terms WHERE prompt_id='p9'").fetchall()}
        conn.close()
        self.assertIn(("fix", "operative"), rows)
        self.assertIn(("investigate", "operative"), rows)
        self.assertIn(("sled_signals.rs", "target"), rows)

    def test_reindex_terms_idempotent(self):
        index_terms(self.dbpath, top_k=5)
        conn = fdb.connect(self.dbpath)
        n1 = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        conn.close()
        index_terms(self.dbpath, top_k=5)
        conn = fdb.connect(self.dbpath)
        n2 = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        conn.close()
        self.assertEqual(n1, n2)


if __name__ == "__main__":
    unittest.main()
