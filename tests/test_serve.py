import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from fable.extract import fts_extract_fn
from fable.indexer import index_vault
from fable.serve import Handler
from tests.helpers import rec, write_jsonl


class TestServe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.dbpath = os.path.join(cls.dir, "fable.db")
        objs = [
            rec("a", "p1", None, "user", "2026-06-01T00:00:01Z",
                text="fix the zigzag pivot"),
            rec("b", "p1", "a", "assistant", "2026-06-01T00:00:02Z",
                text="fixed in sled-zigzag"),
        ]
        live = write_jsonl(os.path.join(cls.dir, "live.jsonl"), objs)
        index_vault(cls.dbpath, [], live_file=live,
                    extract_fn=fts_extract_fn, session_id="s1",
                    project="alpha")
        handler = type("H", (Handler,), {"db_path": cls.dbpath})
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.dir)

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def test_dashboard_served(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"fable", body)

    def test_stats(self):
        status, body = self.get("/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["records"], 2)

    def test_search(self):
        status, body = self.get("/api/search?q=zigzag")
        hits = json.loads(body)
        self.assertEqual(hits[0]["prompt_id"], "p1")

    def test_thread(self):
        status, body = self.get("/api/thread?id=p1&budget=1000")
        data = json.loads(body)
        self.assertIn("fix the zigzag pivot", data["text"])

    def test_thread_missing_is_400(self):
        status, body = self.get("/api/thread?id=nope")
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))

    def test_unknown_route_404(self):
        status, _ = self.get("/api/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
