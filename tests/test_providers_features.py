"""Tests: providers (anthropic mock, claude-cli fake bin, inception guard),
stop control, facts + SessionStart injection, export, costs, settings."""
import json
import os
import shutil
import stat
import tempfile
import unittest

from fable import db as fdb
from fable.extract import fts_extract_fn
from fable.indexer import index_vault
from fable.providers import complete, ProviderError
from tests.helpers import rec, write_jsonl
from tests.test_openrouter import MockServerBase


class TestOllamaProvider(MockServerBase):
    def test_chat_roundtrip(self):
        self.server.script = [(200, {"message": {
            "role": "assistant", "content": "OLLAMA-OK"}})]
        out = complete("hi", provider="ollama", model="llama3.2",
                       base_url=self.base, retry_wait=0.01)
        self.assertEqual(out, "OLLAMA-OK")
        self.assertEqual(self.server.requests[0]["body"]["model"],
                         "llama3.2")

    def test_missing_model_hint(self):
        self.server.script = [(404, {"error": "model 'nope' not found"})]
        with self.assertRaises(ProviderError) as cm:
            complete("hi", provider="ollama", model="nope",
                     base_url=self.base, retry_wait=0.01)
        self.assertIn("ollama pull", str(cm.exception))

    def test_unreachable_message(self):
        with self.assertRaises(ProviderError) as cm:
            complete("hi", provider="ollama",
                     base_url="http://127.0.0.1:1", retries=0)
        self.assertIn("not reachable", str(cm.exception))


class TestAnthropicProvider(MockServerBase):
    def test_messages_roundtrip(self):
        self.server.script = [(200, {"content": [
            {"type": "text", "text": "card json here"}]})]
        out = complete("hello", provider="anthropic", model="haiku",
                       api_key="sk-ant-test", base_url=self.base,
                       retry_wait=0.01)
        self.assertEqual(out, "card json here")
        req = self.server.requests[0]
        headers = {k.lower(): v for k, v in req["headers"].items()}
        self.assertEqual(headers["x-api-key"], "sk-ant-test")
        self.assertIn("haiku", req["body"]["model"])

    def test_missing_key(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        with self.assertRaises(ProviderError):
            complete("x", provider="anthropic", base_url=self.base)


class TestClaudeCliProvider(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.fake = os.path.join(self.dir, "claude")
        with open(self.fake, "w") as f:
            f.write("#!/bin/sh\n"
                    "echo \"CONFIG_DIR=$CLAUDE_CONFIG_DIR\" >&2\n"
                    "cat > /dev/null\n"
                    "echo '{\"title\": \"from cli\", \"type\": \"workflow\","
                    " \"topics\": [], \"decisions\": [], \"files\": [],"
                    " \"outcome\": \"ok\", \"summary\": \"s\"}'\n")
        os.chmod(self.fake, os.stat(self.fake).st_mode | stat.S_IEXEC)
        os.environ["FABLE_CLAUDE_SCRATCH"] = os.path.join(self.dir, "scratch")

    def tearDown(self):
        os.environ.pop("FABLE_CLAUDE_SCRATCH", None)
        shutil.rmtree(self.dir)

    def test_runs_in_scratch_config_dir(self):
        out = complete("prompt", provider="claude-cli", model="haiku",
                       claude_bin=self.fake)
        self.assertIn("from cli", out)
        # the scratch dir was created — sessions can never pollute the index
        self.assertTrue(os.path.isdir(os.environ["FABLE_CLAUDE_SCRATCH"]))

    def test_missing_binary(self):
        with self.assertRaises(ProviderError):
            complete("x", provider="claude-cli",
                     claude_bin="/no/such/claude")


class TestInceptionGuardDiscover(unittest.TestCase):
    def test_fable_generated_sessions_skipped(self):
        from fable.discover import discover
        d = tempfile.mkdtemp()
        try:
            projects = os.path.join(d, "projects")
            marked = rec("a", "p1", None, "user",
                         text="FABLE-GENERATED: this is an automated "
                              "indexing prompt")
            write_jsonl(os.path.join(projects, "-x-proj", "s-card.jsonl"),
                        [marked])
            normal = rec("b", "p2", None, "user", text="real human work")
            write_jsonl(os.path.join(projects, "-x-proj", "s-real.jsonl"),
                        [normal])
            db = os.path.join(d, "f.db")
            stats = discover(db, projects_dir=projects, backup_roots=[])
            self.assertEqual(stats["sessions"], 1)
            conn = fdb.connect(db)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM records WHERE uuid='a'").fetchone()[0],
                0)
            conn.close()
        finally:
            shutil.rmtree(d)


class FeatureBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")
        a = rec("a", "p1", None, "user", text="design the auth flow")
        b = rec("b", "p1", "a", "assistant", text="jwt with rotation chosen")
        b["message"]["model"] = "claude-sonnet-4-6"
        b["message"]["usage"] = {"input_tokens": 1000,
                                 "output_tokens": 500,
                                 "cache_read_input_tokens": 2000,
                                 "cache_creation_input_tokens": 100}
        live = write_jsonl(os.path.join(self.dir, "live.jsonl"), [a, b])
        index_vault(self.dbpath, [], live_file=live,
                    extract_fn=fts_extract_fn, project="proj")

    def tearDown(self):
        shutil.rmtree(self.dir)


class TestFacts(FeatureBase):
    def test_remember_render_forget(self):
        from fable.facts import add_fact, render_facts, forget_fact
        fid = add_fact(self.dbpath, "we use uv, never pip")
        add_fact(self.dbpath, "trading hours are IST", project="PineScript")
        block = render_facts(self.dbpath, project="PineScript")
        self.assertIn("we use uv", block)
        self.assertIn("trading hours", block)
        self.assertTrue(block.startswith("<fable-memory>"))
        # other-project scope hides project facts but keeps globals
        other = render_facts(self.dbpath, project="vj")
        self.assertIn("we use uv", other)
        self.assertNotIn("trading hours", other)
        forget_fact(self.dbpath, fid)
        self.assertNotIn("we use uv",
                         render_facts(self.dbpath) or "")

    def test_sessionstart_hook_injects(self):
        from fable.facts import add_fact
        from fable.hook import run_hook
        add_fact(self.dbpath, "always run make lint before commit")
        result = run_hook(self.dbpath, {
            "hook_event_name": "SessionStart",
            "cwd": "/Users/x/myproj"})
        self.assertIn("make lint", result["inject"])

    def test_compaction_recovery_injects_session_cards(self):
        from fable.cards import parse_card, store_card
        from fable.hook import run_hook
        conn = fdb.connect(self.dbpath)
        store_card(conn, "p1", parse_card(json.dumps({
            "title": "Choose jwt rotation for auth",
            "type": "decision", "topics": ["auth"],
            "decisions": ["chose jwt rotation over sessions"],
            "files": [], "outcome": "done",
            "summary": "s"})), source="test", model="t")
        conn.close()
        result = run_hook(self.dbpath, {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": "test-session"})
        self.assertIn("compaction-recovery", result["inject"])
        self.assertIn("Choose jwt rotation", result["inject"])
        self.assertIn("chose jwt rotation over sessions", result["inject"])
        # non-compact start does NOT inject session recovery
        plain = run_hook(self.dbpath, {
            "hook_event_name": "SessionStart",
            "source": "startup", "session_id": "test-session"})
        self.assertNotIn("compaction-recovery", plain["inject"])


class TestExport(FeatureBase):
    def test_markdown_has_content_and_footer(self):
        from fable.export import export_thread_md
        md = export_thread_md(self.dbpath, "p1")
        self.assertIn("design the auth flow", md)
        self.assertIn("jwt with rotation", md)
        self.assertIn("exported with [fable]", md)

    def test_html_is_standalone(self):
        from fable.export import export_thread_html
        html = export_thread_html(self.dbpath, "p1")
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn("made with", html.replace("exported with", "made with"))


class TestCosts(FeatureBase):
    def test_usage_indexed_and_priced(self):
        from fable.serve import api_costs
        out = api_costs(self.dbpath, {})
        self.assertEqual(len(out["rows"]), 1)
        row = out["rows"][0]
        self.assertEqual(row["tin"], 1000)
        self.assertEqual(row["tout"], 500)
        # sonnet: 1000*3 + 500*15 + 2000*0.3 + 100*3.75 per MTok
        self.assertAlmostEqual(row["cost_usd"], 0.01, places=2)
        self.assertGreater(out["total_usd"], 0)


class TestStopControl(FeatureBase):
    def test_should_stop_halts_run(self):
        from fable.cards import run_cards
        stats = run_cards(self.dbpath, should_stop=lambda: True,
                          min_tokens=0, sleep_fn=lambda s: None)
        self.assertTrue(stats["stopped"])
        self.assertEqual(stats["generated"], 0)


class TestSettingsEnv(unittest.TestCase):
    def test_save_env_merges_and_chmods(self):
        from fable.openrouter import save_env
        d = tempfile.mkdtemp()
        try:
            envfile = os.path.join(d, ".env")
            os.environ["FABLE_ENV"] = envfile
            with open(envfile, "w") as f:
                f.write("# comment\nOPENROUTER_MODEL=old\nKEEP=1\n")
            save_env({"OPENROUTER_MODEL": "new",
                      "ANTHROPIC_API_KEY": "sk-ant-x"})
            content = open(envfile).read()
            self.assertIn("OPENROUTER_MODEL=new", content)
            self.assertIn("KEEP=1", content)
            self.assertIn("ANTHROPIC_API_KEY=sk-ant-x", content)
            self.assertEqual(stat.S_IMODE(os.stat(envfile).st_mode), 0o600)
        finally:
            os.environ.pop("FABLE_ENV", None)
            os.environ.pop("ANTHROPIC_API_KEY", None)
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
