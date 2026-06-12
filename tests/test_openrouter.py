import http.server
import json
import os
import tempfile
import threading
import unittest

from fable.openrouter import chat, load_env, OpenRouterError


class MockHandler(http.server.BaseHTTPRequestHandler):
    """Scriptable responses: server.script is a list of (status, body_dict)."""

    def do_POST(self):
        self.server.requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(self.rfile.read(
                int(self.headers["Content-Length"]))),
        })
        status, body = self.server.script.pop(0)
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def ok_body(content):
    return {"choices": [{"message": {"content": content}}]}


class MockServerBase(unittest.TestCase):
    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), MockHandler)
        self.server.script = []
        self.server.requests = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        os.environ["OPENROUTER_RPM"] = "100000"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, **kw):
        return chat([{"role": "user", "content": "hi"}],
                    model="test-model", api_key="test-key",
                    base_url=self.base, retry_wait=0.01, **kw)


class TestChat(MockServerBase):
    def test_success_returns_content_and_sends_auth(self):
        self.server.script = [(200, ok_body("hello back"))]
        out = self.call()
        self.assertEqual(out, "hello back")
        req = self.server.requests[0]
        self.assertEqual(req["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(req["body"]["model"], "test-model")

    def test_429_retried_then_succeeds(self):
        self.server.script = [(429, {"error": "rate"}),
                              (200, ok_body("after backoff"))]
        self.assertEqual(self.call(), "after backoff")
        self.assertEqual(len(self.server.requests), 2)

    def test_5xx_retried(self):
        self.server.script = [(502, {"error": "bad gw"}),
                              (200, ok_body("recovered"))]
        self.assertEqual(self.call(), "recovered")

    def test_4xx_raises_immediately(self):
        self.server.script = [(401, {"error": "bad key"})]
        with self.assertRaises(OpenRouterError):
            self.call()
        self.assertEqual(len(self.server.requests), 1)

    def test_retries_exhausted_raises(self):
        self.server.script = [(429, {})] * 4
        with self.assertRaises(OpenRouterError):
            self.call(retries=3)

    def test_missing_key_actionable_error(self):
        with self.assertRaises(OpenRouterError) as ctx:
            chat([{"role": "user", "content": "x"}], model="m",
                 api_key="", base_url=self.base)
        self.assertIn("OPENROUTER_API_KEY", str(ctx.exception))


class TestLoadEnv(unittest.TestCase):
    def test_loads_without_overriding(self):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            f.write("# comment\nFOO_TEST_VAR=from_file\n"
                    "EXISTING_TEST_VAR=file_value\nBAD LINE\n")
        os.environ.pop("FOO_TEST_VAR", None)
        os.environ["EXISTING_TEST_VAR"] = "real_env"
        try:
            load_env(path)
            self.assertEqual(os.environ["FOO_TEST_VAR"], "from_file")
            self.assertEqual(os.environ["EXISTING_TEST_VAR"], "real_env")
        finally:
            os.environ.pop("FOO_TEST_VAR", None)
            os.environ.pop("EXISTING_TEST_VAR", None)
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
