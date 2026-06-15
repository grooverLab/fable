"""Dual-embedding: schema migration + thread-text extraction (no backend)."""
import os, shutil, sqlite3, tempfile, unittest
from fable import db as fdb
from fable.extract import fts_extract_fn
from fable.indexer import index_vault
from fable.terms import index_terms
from tests.helpers import rec, write_jsonl


class TestEmbedMigration(unittest.TestCase):
    def test_old_schema_migrates_to_kind(self):
        d = tempfile.mkdtemp()
        try:
            dbp = os.path.join(d, "f.db")
            c = sqlite3.connect(dbp)
            c.executescript(
                "CREATE TABLE embeddings(prompt_id TEXT PRIMARY KEY,"
                " vec BLOB NOT NULL, dim INTEGER NOT NULL, backend TEXT);"
                "INSERT INTO embeddings VALUES('p1', X'0000', 1, 'ollama');")
            c.commit(); c.close()
            conn = fdb.connect(dbp)  # triggers migration
            cols = [r[1] for r in conn.execute("PRAGMA table_info(embeddings)")]
            self.assertIn("kind", cols)
            self.assertEqual(
                conn.execute("SELECT prompt_id, kind FROM embeddings"
                             ).fetchone(), ("p1", "card"))
            conn.close()
        finally:
            shutil.rmtree(d)


class TestThreadText(unittest.TestCase):
    def test_thread_text_from_fts(self):
        d = tempfile.mkdtemp()
        try:
            dbp = os.path.join(d, "f.db")
            live = write_jsonl(os.path.join(d, "live.jsonl"), [
                rec("a", "p1", None, "user", "2026-06-01T00:00:01Z",
                    text="how do we fix the websocket reconnect"),
                rec("b", "p1", "a", "assistant", "2026-06-01T00:00:02Z",
                    text="add exponential backoff to the reconnect loop")])
            index_vault(dbp, [], live_file=live, extract_fn=fts_extract_fn)
            index_terms(dbp)
            from fable.embeddings import _thread_text
            conn = fdb.connect(dbp)
            txt = _thread_text(conn, "p1")
            conn.close()
            self.assertIn("websocket", txt.lower())
            self.assertIn("backoff", txt.lower())
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
