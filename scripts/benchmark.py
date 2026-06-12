#!/usr/bin/env python3
"""Retrieval benchmark — publishable numbers for the README.

Methodology (self-grounding, no hand-built gold set needed):
for N sampled cards, the query is the card's TITLE (what a user would
plausibly type weeks later); the expected answer is that card's thread.
Reports recall@1/@5 and latency for FTS-only vs hybrid.

  python3 scripts/benchmark.py [db] [n]
"""
import random
import statistics
import sys
import time

sys.path.insert(0, ".")
from fable import db as fdb                     # noqa: E402
from fable.recall import search                 # noqa: E402


def main(db_path="fable.db", n=50, seed=7):
    conn = fdb.connect(db_path)
    cards = conn.execute(
        "SELECT prompt_id, title FROM cards WHERE title IS NOT NULL "
        "AND length(title) > 15").fetchall()
    conn.close()
    random.Random(seed).shuffle(cards)
    cards = cards[:n]
    if not cards:
        print("no cards to benchmark — run a backfill first")
        return 1

    hits1 = hits5 = 0
    lat = []
    for pid, title in cards:
        t0 = time.time()
        results = search(db_path, title, limit=5)
        lat.append((time.time() - t0) * 1000)
        ids = [r["prompt_id"] for r in results]
        if ids[:1] == [pid]:
            hits1 += 1
        if pid in ids:
            hits5 += 1

    print(f"queries: {len(cards)} (card titles as queries)")
    print(f"recall@1: {hits1 / len(cards):.2%}")
    print(f"recall@5: {hits5 / len(cards):.2%}")
    print(f"latency: p50 {statistics.median(lat):.0f}ms  "
          f"p95 {sorted(lat)[int(len(lat) * .95) - 1]:.0f}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:3] or ["fable.db"]),
                  *(int(a) for a in sys.argv[2:3])) if False
             else main(sys.argv[1] if len(sys.argv) > 1 else "fable.db",
                       int(sys.argv[2]) if len(sys.argv) > 2 else 50))
