"""fable context — one command from task description to paste-ready pack.

Searches the archive, picks the strongest threads, splits the token budget
across them (weighted by relevance), and emits a single sentinel-wrapped
block: card summaries up top (the map), raw budgeted thread renders below
(the territory).
"""
from typing import Optional

from fable import db as fdb
from fable.recall import render_thread, search

MIN_PER_THREAD = 800


def build_context(db_path: str, query: str, budget: int = 12000,
                  max_threads: int = 5, project: Optional[str] = None) -> str:
    hits = search(db_path, query, limit=max_threads, project=project)
    if not hits:
        return f"<!-- fable: no archive matches for {query!r} -->"

    total_score = sum(h["score"] or 1 for h in hits) or 1
    overhead = 200 * len(hits)
    usable = max(budget - overhead, MIN_PER_THREAD * len(hits))

    sections, arcs = [], []
    header = [f"context pack: {query}", ""]
    for h in hits:
        share = max(int(usable * (h["score"] or 1) / total_score),
                    MIN_PER_THREAD)
        arcs.append(h["prompt_id"])
        title = h["title"] or "(uncarded thread)"
        header.append(
            f"- [{h['type'] or 'thread'}] {title} — {h['turn_count']} turns,"
            f" {h['first_ts'] or '?'}  ({h['prompt_id']})")
        body = render_thread(db_path, h["prompt_id"], budget=share,
                             sentinel=False)
        sections.append(body)

    conn = fdb.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='session_id'").fetchone()
        session = row[0] if row else "multi"
    finally:
        conn.close()

    body = "\n".join(header) + "\n\n" + "\n\n".join(sections)
    fdb.log_op(db_path, "context", q=query)
    return (f'<historical_context session="{session}" '
            f'thread="pack" arcs="{",".join(arcs)}">\n{body}\n'
            f"</historical_context>")
