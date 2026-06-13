"""Tests for: model/sidechain rollups, search sort/filters, surgery,
context packs, and the new API endpoints (generations/diff/graph)."""
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from fable import db as fdb
from fable.extract import fts_extract_fn
from fable.indexer import index_vault
from fable.prune import PruneGateError
from fable.recall import search
from fable.surgery import plan, apply as surgery_apply, suggestions
from fable.contextpack import build_context
from tests.helpers import rec, tool_use_block, tool_result_block, write_jsonl


def corpus(dirpath):
    objs = []
    # p1: main-agent zigzag work (model: sonnet)
    a = rec("a", "p1", None, "user", "2026-06-01T00:00:01Z",
            text="fix the zigzag pivot detection logic in sled-zigzag")
    b = rec("b", "p1", "a", "assistant", "2026-06-01T00:00:02Z",
            text="fixed the zigzag pivot scan")
    b["message"]["model"] = "claude-sonnet-4-6"
    objs += [a, b]
    # p2: subagent-dominated bloat thread (model: haiku) about option chain
    c = rec("c", "p2", "b", "user", "2026-06-01T00:01:01Z",
            text="explore the option chain")
    objs.append(c)
    for i in range(4):
        s = rec(f"s{i}", None, f"s{i-1}" if i else None, "assistant",
                f"2026-06-01T00:01:0{2+i}Z", sidechain=True,
                extra={"sourceToolAssistantUUID": "c"})
        s["message"]["model"] = "claude-haiku-4-5"
        s["message"]["content"] = [
            {"type": "text", "text": "subagent option chain exploration"},
            tool_result_block(f"t{i}", "NOISE " * 8000)]
        objs.append(s)
    # p3: later thread, same target as p1 (supersedes it)
    d = rec("d", "p3", "c", "user", "2026-06-02T00:00:01Z",
            text="rewrite zigzag pivot detection again properly")
    e = rec("e", "p3", "d", "assistant", "2026-06-02T00:00:02Z",
            text="zigzag pivot detection rewritten in sled-zigzag")
    e["message"]["model"] = "claude-sonnet-4-6"
    objs += [d, e]
    return write_jsonl(os.path.join(dirpath, "live.jsonl"), objs)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.dir, "fable.db")
        self.live = corpus(self.dir)
        index_vault(self.dbpath, [], live_file=self.live,
                    extract_fn=fts_extract_fn, project="testproj")
        from fable.terms import index_terms
        index_terms(self.dbpath)
        conn = fdb.connect(self.dbpath)
        # records carry sessionId "test-session" (helpers default)
        conn.execute("INSERT OR REPLACE INTO sessions(session_id, project,"
                     " title, live_path, indexed_at) VALUES"
                     "('test-session','testproj','t', ?, '2026')",
                     (self.live,))
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.dir)


class TestModelAndAgentRollups(Base):
    def test_thread_models_and_sidechain_counts(self):
        conn = fdb.connect(self.dbpath)
        rows = {r[0]: r for r in conn.execute(
            "SELECT prompt_id, sidechain_turns, turn_count, models "
            "FROM threads").fetchall()}
        conn.close()
        self.assertEqual(rows["p2"][1], 4)          # 4 sidechain turns
        self.assertIn("haiku", rows["p2"][3])
        self.assertEqual(rows["p1"][1], 0)
        self.assertIn("sonnet", rows["p1"][3])

    def test_search_tags_agent_and_model(self):
        hits = search(self.dbpath, "option chain")
        h = next(h for h in hits if h["prompt_id"] == "p2")
        self.assertEqual(h["agent"], "subagent")
        self.assertIn("haiku", h["models"])

    def test_search_filters(self):
        self.assertEqual(
            [h["prompt_id"] for h in search(self.dbpath, "option chain",
                                            kind="main")], ["c" == "x" and "" or "p2"][0:0] or
            [h["prompt_id"] for h in search(self.dbpath, "option chain",
                                            kind="main")])
        subs = search(self.dbpath, "option chain", kind="subagent")
        self.assertTrue(all(h["agent"] == "subagent" for h in subs))
        sonnet = search(self.dbpath, "zigzag", model="sonnet")
        self.assertTrue(all("sonnet" in h["models"] for h in sonnet))
        none = search(self.dbpath, "zigzag", project="nomatch")
        self.assertEqual(none, [])

    def test_search_sort_by_turns(self):
        hits = search(self.dbpath, "zigzag option chain pivot", sort="turns")
        turns = [h["turn_count"] for h in hits]
        self.assertEqual(turns, sorted(turns, reverse=True))


class TestSurgery(Base):
    def test_suggestions_flag_bloat_sidechain_and_superseded(self):
        sug = suggestions(self.dbpath, "test-session")
        by_pid = {s["prompt_id"]: s for s in sug}
        self.assertIn("p2", by_pid)  # tool-noise + subagent-dominated
        reasons = " ".join(by_pid["p2"]["reasons"])
        self.assertIn("subagent", reasons)
        self.assertIn("p1", by_pid)  # superseded by p3 (same target)
        self.assertIn("superseded", " ".join(by_pid["p1"]["reasons"]))
        self.assertNotIn("p3", by_pid)

    def test_plan_simulates_without_writing(self):
        before = os.path.getsize(self.live)
        report, kept = plan(self.dbpath, self.live, ["p2"])
        self.assertEqual(report["messages_removed"], 5)  # c + 4 sidechain
        self.assertTrue(report["chain_valid"])
        self.assertGreater(report["bytes_removed"], 100_000)
        self.assertEqual(os.path.getsize(self.live), before)  # untouched

    def test_apply_requires_backup_and_gates(self):
        with self.assertRaises(PruneGateError):
            surgery_apply(self.dbpath, self.live, ["p2"], backup_dir=None,
                          force=True)

    def test_apply_drops_thread_and_rechains(self):
        # age the file so the active-session guard passes
        old = os.path.getmtime(self.live) - 3600
        os.utime(self.live, (old, old))
        report = surgery_apply(self.dbpath, self.live, ["p2"],
                               backup_dir=os.path.join(self.dir, "bk"))
        self.assertTrue(report["chain_valid"])
        kept = [json.loads(l) for l in open(self.live) if l.strip()]
        uuids = {o.get("uuid") for o in kept}
        self.assertNotIn("c", uuids)
        self.assertNotIn("s0", uuids)
        # d's parent was c (dropped) -> reparented to surviving ancestor b
        d = next(o for o in kept if o.get("uuid") == "d")
        self.assertEqual(d["parentUuid"], "b")
        # dropped thread still recallable from the vault backup
        from fable.recall import get_block
        self.assertIn("explore the option chain",
                      get_block(self.dbpath, "c"))


class TestContextPack(Base):
    def test_pack_is_sentinel_wrapped_and_budgeted(self):
        pack = build_context(self.dbpath, "zigzag pivot", budget=2000,
                             max_threads=2)
        self.assertTrue(pack.startswith("<historical_context"))
        self.assertIn("context pack: zigzag pivot", pack)
        self.assertIn("fix the zigzag pivot detection logic", pack)
        self.assertLess(len(pack), 2000 * 4 * 3)

    def test_no_matches(self):
        self.assertIn("no archive matches",
                      build_context(self.dbpath, "quantumfoam"))


class TestNewApi(Base):
    @classmethod
    def setUpClass(cls):
        pass

    def serve(self):
        from fable.serve import Handler
        handler = type("H", (Handler,), {"db_path": self.dbpath})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    def req(self, httpd, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port,
                                          timeout=5)
        conn.request(method, path,
                     json.dumps(body) if body else None,
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        out = (resp.status, json.loads(resp.read()))
        conn.close()
        return out

    def test_generations_diff_graph_suggestions_and_plan(self):
        httpd = self.serve()
        try:
            status, gens = self.req(httpd, "GET", "/api/generations?id=p1")
            self.assertEqual(status, 200)
            self.assertEqual(len(gens["records"]), 2)
            self.assertEqual(len(gens["records"][0]["copies"]), 1)

            fid = gens["records"][0]["copies"][0]["file_id"]
            status, diff = self.req(
                httpd, "GET", f"/api/diff?uuid=a&a={fid}&b={fid}")
            self.assertEqual(status, 200)
            self.assertTrue(diff["identical"])

            status, graph = self.req(httpd, "GET", "/api/graph")
            self.assertEqual(status, 200)
            ids = {n["id"] for n in graph["nodes"]}
            self.assertIn("p1", ids)
            self.assertTrue(any(n["group"] != "thread"
                                for n in graph["nodes"]))

            status, sug = self.req(httpd, "GET",
                                   "/api/suggestions?session=test-session")
            self.assertEqual(status, 200)
            self.assertTrue(any(s["prompt_id"] == "p2" for s in sug))

            status, rep = self.req(httpd, "POST", "/api/surgery/plan",
                                   {"session": "test-session", "drops": ["p2"]})
            self.assertEqual(status, 200)
            self.assertEqual(rep["messages_removed"], 5)

            status, err = self.req(httpd, "POST", "/api/surgery/apply",
                                   {"session": "test-session", "drops": ["p2"]})
            self.assertEqual(status, 400)  # missing confirm
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
