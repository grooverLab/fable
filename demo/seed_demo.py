#!/usr/bin/env python3
"""Build the fictional demo corpus + index (demo/demo.db).

Everything here is invented: fake projects, fake paths, generic dev
topics. Used for the README GIF and as a try-before-you-index sample:

    python3 demo/seed_demo.py
    ./bin/fable --db demo/demo.db serve
"""
import json
import os
import random
import sys
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)

from fable.cards import store_card                              # noqa: E402
from fable.extract import fts_extract_fn                        # noqa: E402
from fable import db as fdb                                     # noqa: E402
from fable.indexer import index_vault                           # noqa: E402
from fable.terms import index_terms                             # noqa: E402

random.seed(7)


def rid():
    return str(uuid.UUID(int=random.getrandbits(128), version=4))


TS = ["2026-05-0%dT1%d:%02d:%02dZ" % (d, h, m, s)
      for d in range(1, 8) for h in range(3) for m in (5, 25, 45)
      for s in (10, 40)]
_ts = iter(TS)


def ts():
    return next(_ts)


# (project, cwd, session_title, threads)
# thread = (title, type, outcome, decisions, [turns])
# turn = ("user"|"assistant", text) or ("tool", cmd, result)
CORPUS = [
    ("acme-shop", "/Users/demo/code/acme-shop", "checkout-service", [
        ("Fix the websocket reconnect loop in checkout", "workflow", "done",
         ["bound reconnects with exponential backoff + jitter",
          "cap retry window at 30s, surface a toast after 3 failures"],
         [("user", "the checkout websocket reconnects in a tight loop when "
                   "the gateway restarts — fix it"),
          ("tool", "grep -rn 'new WebSocket' src/checkout/",
           "src/checkout/live.ts:42: socket = new WebSocket(GATEWAY_URL)"),
          ("assistant", "found it: the onclose handler reconnects "
                        "immediately with no backoff. The websocket "
                        "reconnect fix: exponential backoff with jitter, "
                        "capped at 30s, plus a connection-state machine so "
                        "we never stack sockets."),
          ("tool", "npm test -- checkout.live",
           "PASS checkout.live — 14 tests, reconnect storm simulated, "
           "1 socket alive after 50 gateway restarts"),
          ("assistant", "fixed — reconnect loop is gone; backoff caps at "
                        "30s with jitter and the state machine prevents "
                        "socket stacking.")]),
        ("Decide JWT refresh strategy for the SPA", "decision", "decided",
         ["rotate refresh tokens on every use (detect replay)",
          "rejected: long-lived access tokens (can't revoke)"],
         [("user", "we keep getting 401 races when two tabs refresh the "
                   "JWT at once — decide a strategy"),
          ("assistant", "the JWT refresh race: two tabs both use the same "
                        "refresh token; second one gets rejected after "
                        "rotation. Options: (a) rotate-on-use with replay "
                        "detection and a shared BroadcastChannel lock, "
                        "(b) long-lived access tokens. Decision: (a) — "
                        "rejected (b) because revocation becomes "
                        "impossible."),
          ("tool", "curl -s api/auth/refresh -d token=...",
           '{"access":"eyJ...","refresh":"rotated","replay_guard":true}'),
          ("assistant", "decided: rotate refresh tokens on every use, "
                        "BroadcastChannel mutex across tabs, replay "
                        "detection server-side.")]),
        ("Migrate orders table to partitioned Postgres", "workflow", "done",
         ["partition by order_month, backfill in 10k batches"],
         [("user", "orders table is 80M rows, queries are crawling — "
                   "migrate to partitioned tables"),
          ("tool", "psql -c 'SELECT count(*) FROM orders'", "80411223"),
          ("assistant", "plan: declarative partitioning by order_month, "
                        "attach-and-backfill in 10k batches so the app "
                        "stays live, then swap the view."),
          ("tool", "psql -f migrate_partition.sql",
           "CREATE TABLE … ATTACH PARTITION … backfill 8041 batches OK"),
          ("assistant", "migration done — p95 on order lookups went from "
                        "2.1s to 90ms.")]),
    ]),
    ("api-gateway", "/Users/demo/code/api-gateway", "gateway-hardening", [
        ("Debug rate limiter letting bursts through", "workflow", "done",
         ["token bucket was refilled on read — moved refill to a timer"],
         [("user", "the rate limiter lets 3x bursts through at window "
                   "edges — debug it"),
          ("tool", "go test ./ratelimit/ -run Burst -v",
           "FAIL: allowed 312 requests in 1s window (limit 100)"),
          ("assistant", "classic fixed-window edge burst. Switching to a "
                        "token bucket — and the bug after that: refill "
                        "happened on read, so idle keys banked unlimited "
                        "tokens. Refill moved to a monotonic timer."),
          ("tool", "go test ./ratelimit/ -v",
           "PASS — 100±2 allowed across all window offsets"),
          ("assistant", "fixed and load-tested at 50k rps.")]),
        ("Decide Redis vs Memcached for session cache", "decision",
         "decided",
         ["Redis — need TTL per key + persistence through deploys",
          "rejected: Memcached (no persistence, cold-start stampede)"],
         [("user", "pick the session cache: Redis or Memcached?"),
          ("assistant", "Decision: Redis. We need per-key TTL, LRU with "
                        "persistence across deploys (Memcached cold-starts "
                        "caused login stampedes in staging), and pub/sub "
                        "for invalidation. Rejected Memcached on the "
                        "persistence gap alone.")]),
        ("Add OpenTelemetry tracing to the proxy hot path", "workflow",
         "done", ["sample at 1% with tail-based sampling for errors"],
         [("user", "we're blind in prod — add otel tracing to the proxy"),
          ("tool", "wrk -t8 -c400 -d30s http://localhost:8080/bench",
           "before: 41,200 rps · after instrumentation: 40,900 rps (-0.7%)"),
          ("assistant", "instrumented the hot path with otel spans; "
                        "head-sample 1%, tail-keep every error trace. "
                        "Overhead measured at 0.7% — shipping.")]),
    ]),
]


def turn_records(session_id, cwd, thread):
    title, ctype, outcome, decisions, turns = thread
    pid = rid()
    out, parent = [], None
    for t in turns:
        u = rid()
        if t[0] == "user":
            rec = {"uuid": u, "parentUuid": parent, "type": "user",
                   "sessionId": session_id, "cwd": cwd, "promptId": pid,
                   "timestamp": ts(),
                   "message": {"role": "user", "content": t[1]}}
        elif t[0] == "assistant":
            rec = {"uuid": u, "parentUuid": parent, "type": "assistant",
                   "sessionId": session_id, "cwd": cwd, "timestamp": ts(),
                   "message": {"role": "assistant",
                               "model": "claude-sonnet-4-6",
                               "usage": {"input_tokens":
                                         random.randint(800, 4000),
                                         "output_tokens":
                                         random.randint(150, 600)},
                               "content": [{"type": "text", "text": t[1]}]}}
        else:  # tool pair: assistant tool_use + user tool_result
            tu = "toolu_" + u[:8]
            rec = {"uuid": u, "parentUuid": parent, "type": "assistant",
                   "sessionId": session_id, "cwd": cwd, "timestamp": ts(),
                   "message": {"role": "assistant",
                               "model": "claude-sonnet-4-6",
                               "content": [{"type": "tool_use", "id": tu,
                                            "name": "Bash",
                                            "input": {"command": t[1]}}]}}
            out.append(rec)
            parent = u
            u = rid()
            rec = {"uuid": u, "parentUuid": parent, "type": "user",
                   "sessionId": session_id, "cwd": cwd, "promptId": pid,
                   "timestamp": ts(),
                   "message": {"role": "user",
                               "content": [{"type": "tool_result",
                                            "tool_use_id": tu,
                                            "content": t[2]}]}}
        out.append(rec)
        parent = u
    return pid, out


def main():
    corpus_dir = os.path.join(BASE, "corpus")
    db_path = os.path.join(BASE, "demo.db")
    for p in (db_path, db_path + "-wal", db_path + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    cards = []
    for project, cwd, sess_title, threads in CORPUS:
        session_id = rid()
        enc = "-" + cwd.strip("/").replace("/", "-").replace("_", "-")
        pdir = os.path.join(corpus_dir, enc)
        os.makedirs(pdir, exist_ok=True)
        path = os.path.join(pdir, session_id + ".jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "custom-title",
                                "customTitle": sess_title,
                                "sessionId": session_id}) + "\n")
            for thread in threads:
                pid, recs = turn_records(session_id, cwd, thread)
                for r in recs:
                    f.write(json.dumps(r) + "\n")
                cards.append((pid, thread))
        index_vault(db_path, [], live_file=path,
                    extract_fn=fts_extract_fn,
                    session_id=session_id, project=project)
        conn = fdb.connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO sessions(session_id, project, title, "
            "live_path, indexed_at) VALUES(?,?,?,?,?)",
            (session_id, project, sess_title, path, "2026-05-07"))
        conn.commit()
        conn.close()
    index_terms(db_path)
    conn = fdb.connect(db_path)
    for pid, (title, ctype, outcome, decisions, turns) in cards:
        store_card(conn, pid, {
            "title": title, "type": ctype, "topics": [], "files": [],
            "decisions": decisions, "outcome": outcome,
            "summary": title}, source="demo", model="demo")
    conn.commit()
    conn.close()
    print("demo corpus ready:", db_path)
    for pid, (title, *_rest) in cards:
        print(f"  {pid}  {title}")


if __name__ == "__main__":
    main()
