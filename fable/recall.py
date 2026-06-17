"""Recall — page exact history back into a session under a token budget.

Progressive disclosure: search returns ranked thread summaries (cheap),
render_thread returns the raw turns with text verbatim and bulky
tool_results elided down to the budget, get_block returns one record
byte-identical. Output is wrapped in a <historical_context> sentinel so the
indexer and pruner can recognize recalled memory later (inception guard).
"""
import json
import os
import sqlite3
from typing import List, Optional

from fable import db as fdb
from fable.jsonl import read_span

CHARS_PER_TOKEN = 4
MIN_ELIDED_CHARS = 80
BUDGET_SLACK = 1.5  # hard output cap = budget chars * slack

# Search confidence: score_pct is an ABSOLUTE, saturating function of the raw
# BM25×weight score — NOT normalized within the returned batch — so a weak
# query's best-of-a-bad-lot no longer reads 100%. _SCORE_HALF is the raw score
# at which confidence = 50%; below _LOW_CONF_PCT a hit is flagged low_confidence
# (FTS5 always returns *something*, so a consumer needs a "this is noise" signal).
_SCORE_HALF = 150.0
_LOW_CONF_PCT = 40
_PROJECT_BOOST = 15   # soft current-project bonus (score_pct points) for ranking


class StaleIndexError(RuntimeError):
    pass


def _check_fresh(conn, paths) -> None:
    """A pointer into a REWRITTEN file is garbage — refuse to serve it.
    But transcripts are append-only between prunes: a file that merely GREW
    still holds every indexed offset intact, so reads stay valid (the
    active session would otherwise be permanently 'stale')."""
    for path in set(paths):
        row = conn.execute(
            "SELECT size, mtime FROM files WHERE path = ?", (path,)).fetchone()
        try:
            st = os.stat(path)
        except OSError:
            err = StaleIndexError(
                f"indexed file is gone: {path} — re-run `fable index`")
            err.path = path
            raise err
        if row and st.st_size > row[0]:
            continue  # append-only growth: indexed offsets are all valid
        if row and (row[0] != st.st_size or abs(row[1] - st.st_mtime) > 1e-6):
            err = StaleIndexError(
                f"{path} changed since it was indexed — re-run "
                f"`fable index`/`fable discover` before recalling from it")
            err.path = path
            raise err


def get_block(db_path: str, uuid: str) -> str:
    conn = fdb.connect(db_path)
    try:
        row = conn.execute(
            "SELECT f.path, r.offset, r.length FROM records r "
            "JOIN files f ON f.id = r.file_id WHERE r.uuid = ?",
            (uuid,)).fetchone()
        if row is None:
            raise KeyError(f"uuid not in index: {uuid}")
        _check_fresh(conn, [row[0]])
    finally:
        conn.close()
    return read_span(row[0], row[1], row[2]).decode("utf-8", errors="replace")


class _Piece:
    """One renderable fragment; elidable pieces shrink to fit the budget."""

    def __init__(self, text: str, elidable: bool = False, block_uuid: str = ""):
        self.text = text
        self.elidable = elidable
        self.block_uuid = block_uuid


def _turn_pieces(turn, obj) -> List[_Piece]:
    pieces = [_Piece(f"[{obj.get('type') or turn.type} {turn.uuid} {turn.ts}]")]
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        pieces.append(_Piece(content))
        return pieces
    if not isinstance(content, list):
        return pieces
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            # never emit nested sentinels: a turn that itself contains
            # recalled context gets a citation stub instead (inception guard)
            from fable.extract import replace_historical
            text, _ = replace_historical(block.get("text", ""))
            pieces.append(_Piece(text))
        elif kind == "thinking":
            think = block.get("thinking", "")
            if think:
                pieces.append(_Piece("(thinking) " + think, elidable=True,
                                     block_uuid=turn.uuid))
        elif kind == "tool_use":
            inp = block.get("input") if isinstance(block.get("input"), dict) else {}
            parts = [f"[tool_use {block.get('name', '?')}]"]
            for key, val in inp.items():
                if isinstance(val, str) and val:
                    parts.append(f"{key}: {val}")
            pieces.append(_Piece("\n".join(parts), elidable=len("\n".join(parts)) > 500,
                                 block_uuid=turn.uuid))
        elif kind == "tool_result":
            inner = block.get("content")
            texts = []
            if isinstance(inner, str):
                texts.append(inner)
            elif isinstance(inner, list):
                for b in inner:
                    if isinstance(b, dict) and b.get("type") == "text":
                        texts.append(b.get("text", ""))
            pieces.append(_Piece("[tool_result]\n" + "\n".join(texts),
                                 elidable=True, block_uuid=turn.uuid))
        elif kind == "image":
            pieces.append(_Piece("[image]"))
    return pieces


def _fit_budget(pieces: List[_Piece], budget_chars: int) -> List[str]:
    fixed = sum(len(p.text) + 1 for p in pieces if not p.elidable)
    elidable = [p for p in pieces if p.elidable]
    remaining = max(0, budget_chars - fixed)
    total_elidable = sum(len(p.text) for p in elidable)

    out = []
    if total_elidable <= remaining:
        out = [p.text for p in pieces]
    else:
        cap = max(MIN_ELIDED_CHARS, remaining // max(1, len(elidable)))
        for p in pieces:
            if p.elidable and len(p.text) > cap:
                out.append(p.text[:cap].rstrip()
                           + f"\n… [truncated — fable block {p.block_uuid}]")
            else:
                out.append(p.text)

    # hard cap: even "sacred" text cannot flood the caller's context window
    hard = int(budget_chars * BUDGET_SLACK)
    total = 0
    capped = []
    for text in out:
        if total + len(text) > hard:
            keep = max(0, hard - total)
            capped.append(text[:keep].rstrip()
                          + "\n… [thread exceeds budget — raise --budget or "
                            "drill in with fable block <uuid>]")
            return capped
        capped.append(text)
        total += len(text) + 1
    return capped


def render_thread(db_path: str, prompt_id: str, budget: int = 8000,
                  raw: bool = False, sentinel: bool = True) -> str:
    from fable.threads import reconstruct
    conn = fdb.connect(db_path)
    try:
        view = reconstruct(conn, prompt_id)
        session = conn.execute(
            "SELECT session_id FROM threads WHERE prompt_id = ?",
            (prompt_id,)).fetchone()
        if not session or not session[0]:
            session = conn.execute(
                "SELECT value FROM meta WHERE key = 'session_id'").fetchone()
        session = session[0] if session and session[0] else "unknown"
    finally:
        conn.close()
    if not view.main and not view.orphans and not view.sidechains:
        raise KeyError(f"thread not in index: {prompt_id}")

    conn = fdb.connect(db_path)
    try:
        _check_fresh(conn, [t.path for t in
                            view.main + view.orphans + view.sidechains])
    finally:
        conn.close()

    if raw:
        lines = [read_span(t.path, t.offset, t.length).decode("utf-8", "replace")
                 for t in view.main + view.orphans + view.sidechains]
        body = "\n".join(lines)
    else:
        pieces: List[_Piece] = []
        header = (f"== thread {prompt_id} | {len(view.main)} turns"
                  + (f" | +{len(view.orphans)} edit-branch" if view.orphans else "")
                  + (f" | +{len(view.sidechains)} sidechain" if view.sidechains else "")
                  + " ==")
        pieces.append(_Piece(header))
        sections = [(view.main, None),
                    (view.orphans, "-- edit-branches (abandoned retries) --"),
                    (view.sidechains, "-- sidechain (subagent) --")]
        for turns, label in sections:
            if not turns:
                continue
            if label:
                pieces.append(_Piece(label))
            for t in turns:
                obj = json.loads(read_span(t.path, t.offset, t.length)
                                 .decode("utf-8", "surrogateescape"))
                pieces.extend(_turn_pieces(t, obj))
        body = "\n".join(_fit_budget(pieces, budget * CHARS_PER_TOKEN))
        body = body.encode("utf-8", "replace").decode("utf-8")

    if sentinel:
        return (f'<historical_context session="{session}" thread="{prompt_id}" '
                f'arcs="{prompt_id}">\n{body}\n</historical_context>')
    return body


def _fts_query(query: str) -> Optional[str]:
    words = [w for w in query.replace('"', " ").split() if w.strip()]
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in words)


SORT_KEYS = {
    "relevance": lambda h: -(h["score"] or 0),
    "turns": lambda h: -(h["turn_count"] or 0),
    "tokens": lambda h: -(h["est_tokens"] or 0),
    "recent": lambda h: h["last_ts"] or "",
}


def _time_bounds(since: Optional[str], until: Optional[str]):
    """Validate + normalize since/until. ISO-8601 sorts lexically, so a date or
    a full timestamp both compare correctly. A date-only `until` (YYYY-MM-DD) is
    made INCLUSIVE of that whole day; a date-only `since` is already inclusive.
    A malformed/impossible date raises ValueError (so search and timeline FAIL
    LOUDLY and consistently, instead of one silently dropping the filter)."""
    import datetime

    def _check(v, label):
        if not v:
            return None
        v = v.strip()
        try:                       # validate the date part (catches 2026-13-45,
            datetime.date.fromisoformat(v[:10])   # 'not-a-date', typos, …)
        except ValueError:
            raise ValueError(
                f"invalid {label} date {v!r} — use YYYY-MM-DD or an ISO "
                f"timestamp")
        return v

    since = _check(since, "since")
    until = _check(until, "until")
    if until and len(until) == 10:
        until = until + "T23:59:59.999Z"
    return since, until


def scout_search(db_path: str, query: str, limit: int = 8) -> List[dict]:
    """The scout's lean hot path: FTS-bm25 over CARD rows only, top-`limit`,
    carrying just the fields the teaser needs. No semantic merge and NO
    200-candidate metadata loop (that loop is what made search() ~1.7s) — so
    this runs in tens of ms. Card hits keep the same 25x weight search() gives
    them, so score_pct and the scout floor stay calibrated."""
    fts_query = _fts_query(query)
    if not fts_query:
        return []
    conn = fdb.connect(db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT prompt_id, -rank AS r, "
                "snippet(fts, 0, '«', '»', '…', 16) AS snip "
                "FROM fts WHERE fts MATCH ? AND kind='card' "
                "ORDER BY rank LIMIT ?", (fts_query, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for pid, r, snip in rows:
            score = (r or 0) * 25
            card = conn.execute(
                "SELECT title, type, outcome, decisions FROM cards "
                "WHERE prompt_id=?", (pid,)).fetchone() or (None, None,
                                                            None, None)
            meta = conn.execute(
                "SELECT t.last_ts, s.project FROM threads t LEFT JOIN sessions s"
                " ON s.session_id=t.session_id WHERE t.prompt_id=?",
                (pid,)).fetchone() or (None, None)
            try:
                decisions = json.loads(card[3]) if card[3] else []
            except Exception:
                decisions = []
            pct = round(100 * score / (score + _SCORE_HALF)) if score else 0
            out.append({
                "prompt_id": pid, "score": round(score, 3), "score_pct": pct,
                "low_confidence": pct < _LOW_CONF_PCT,
                "snippet": " ".join((snip or "").split())[:240],
                "title": card[0], "type": card[1], "outcome": card[2],
                "decisions": decisions, "last_ts": meta[0], "project": meta[1],
            })
        return out
    finally:
        conn.close()


# cosine→confidence calibration for the scout. nomic 'same-topic' cosine tops
# ~0.78, so map [_COS_FLOOR, _COS_CEIL] -> [0,100]. PROVISIONAL — Phase 3
# (fire/conversion log) replaces these with values learned from real usage.
_COS_FLOOR = 0.50
_COS_CEIL = 0.66


def scout_vector_search(db_path: str, query: str, project: str = None,
                        limit: int = 8) -> List[dict]:
    """Semantic scout matcher — cosine over CARD vectors. Finds the right thread
    even when the prompt's wording differs (an indirect prompt still matches).
    Confidence = rescaled cosine: bounded and NOT gamed by query length, unlike
    bm25. Empty if no embedding backend (caller falls back to scout_search)."""
    from fable import embeddings as emb
    hits = emb.scout_vector_hits(db_path, query, project=project, limit=limit)
    if not hits:
        return []
    conn = fdb.connect(db_path)
    try:
        out = []
        for pid, cos in hits:
            pct = round(100 * max(0.0, min(
                1.0, (cos - _COS_FLOOR) / (_COS_CEIL - _COS_FLOOR))))
            card = conn.execute(
                "SELECT title, type, outcome, decisions FROM cards "
                "WHERE prompt_id=?", (pid,)).fetchone() or (None, None,
                                                            None, None)
            meta = conn.execute(
                "SELECT t.last_ts, s.project FROM threads t LEFT JOIN sessions s"
                " ON s.session_id=t.session_id WHERE t.prompt_id=?",
                (pid,)).fetchone() or (None, None)
            try:
                decisions = json.loads(card[3]) if card[3] else []
            except Exception:
                decisions = []
            out.append({
                "prompt_id": pid, "score": round(cos, 4), "score_pct": pct,
                "low_confidence": pct < _LOW_CONF_PCT, "snippet": None,
                "title": card[0], "type": card[1], "outcome": card[2],
                "decisions": decisions, "last_ts": meta[0], "project": meta[1],
                "cosine": round(cos, 4),
            })
        return out
    finally:
        conn.close()


def search(db_path: str, query: str, operative: Optional[str] = None,
           target: Optional[str] = None, limit: int = 10,
           sort: str = "relevance", kind: Optional[str] = None,
           model: Optional[str] = None,
           project: Optional[str] = None,
           session: Optional[str] = None,
           tag: Optional[str] = None,
           since: Optional[str] = None,
           until: Optional[str] = None,
           offset: int = 0, semantic: bool = True,
           boost_project: str = None) -> List[dict]:
    """kind: 'main' | 'subagent' (majority-sidechain threads);
    model/project: substring match; session: exact session scope;
    since/until: ISO date or timestamp window on the thread's activity;
    sort: relevance|turns|tokens|recent."""
    fts_query = _fts_query(query)
    since, until = _time_bounds(since, until)
    has_filters = any([operative, target, kind, model, project, session, tag,
                       since, until])
    if fts_query is None and not has_filters and sort == "relevance":
        return []
    conn = fdb.connect(db_path)
    try:
        if fts_query is not None:
            # a hit on a CARD (title/decisions/summary) is worth far more
            # than an incidental hit in raw tool output
            sql = ("SELECT prompt_id, COUNT(*) AS matches, SUM(s) AS score "
                   "FROM (SELECT prompt_id, -rank * (CASE WHEN kind='card' "
                   "THEN 25 ELSE 1 END) AS s FROM fts "
                   "WHERE fts MATCH ?) WHERE prompt_id IS NOT NULL ")
            args: list = [fts_query]
        else:
            # browse mode: no query, just filters/sort over all threads
            sql = ("SELECT prompt_id, NULL AS matches, 0.0 AS score "
                   "FROM threads WHERE 1=1 ")
            args = []
        if operative:
            sql += ("AND prompt_id IN (SELECT prompt_id FROM terms "
                    "WHERE kind='operative' AND term = ?) ")
            args.append(operative)
        if target:
            sql += ("AND prompt_id IN (SELECT prompt_id FROM terms "
                    "WHERE kind='target' AND term = ?) ")
            args.append(target)
        if tag:
            # 'family:value' filters that pair; a bare 'value' matches any family
            fam, sep, val = tag.partition(":")
            if sep and val:
                sql += ("AND prompt_id IN (SELECT prompt_id FROM thread_tags "
                        "WHERE family = ? AND value = ?) ")
                args.extend([fam, val])
            else:
                sql += ("AND prompt_id IN (SELECT prompt_id FROM thread_tags "
                        "WHERE value = ?) ")
                args.append(fam)
        if since:
            sql += ("AND prompt_id IN (SELECT prompt_id FROM threads "
                    "WHERE last_ts >= ?) ")
            args.append(since)
        if until:
            sql += ("AND prompt_id IN (SELECT prompt_id FROM threads "
                    "WHERE first_ts <= ?) ")
            args.append(until)
        if fts_query is not None:
            # over-fetch so post-filters/sorts still fill the limit
            sql += "GROUP BY prompt_id ORDER BY score DESC LIMIT ?"
        else:
            sql += "ORDER BY est_tokens DESC LIMIT ?"
        args.append(max((limit + offset) * 5, 200))
        try:
            hits = conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError as e:
            raise ValueError(f"unsupported search query {query!r}: {e}")

        # hybrid: card-embedding cosine matches merge in, scaled to the FTS
        # score ceiling so they can actually compete (graceful off). NOT when a
        # prompt-level SQL filter (tag/operative/target) is active — the booster
        # runs after that SQL, so it would leak unfiltered prompts past the facet.
        if semantic and fts_query is not None and not (tag or operative or target):
            try:
                from fable.embeddings import semantic_hits
                have = {h[0] for h in hits}
                top = max((h[2] or 0 for h in hits), default=10) or 10
                hits = list(hits) + [
                    (pid, None, cos * top) for pid, cos in
                    semantic_hits(db_path, query) if pid not in have]
            except Exception:
                pass

        results = []
        for prompt_id, matches, score in hits:
            meta = conn.execute(
                "SELECT turn_count, est_tokens, first_ts, last_ts,"
                " session_id, sidechain_turns, models "
                "FROM threads WHERE prompt_id = ?",
                (prompt_id,)).fetchone()
            if meta is None:
                continue  # ghost: an fts entry whose thread was pruned away —
                #            never surface an all-null record to the consumer
            card = conn.execute(
                "SELECT title, type, outcome, decisions FROM cards "
                "WHERE prompt_id = ?",
                (prompt_id,)).fetchone() or (None, None, None, None)
            sess = conn.execute(
                "SELECT project, title FROM sessions WHERE session_id = ?",
                (meta[4],)).fetchone() or (None, None)
            tags = [f"{f}:{v}" for f, v in conn.execute(
                "SELECT family, value FROM thread_tags WHERE prompt_id = ?"
                " ORDER BY family", (prompt_id,))]
            try:
                decisions = json.loads(card[3]) if card[3] else []
            except Exception:
                decisions = []
            turn_count = meta[0] or 0
            sidechain_turns = meta[5] or 0
            agent = ("subagent" if turn_count and
                     sidechain_turns * 2 > turn_count else "main")
            # matched passage: the best-ranked FTS row for this thread (a card
            # match preferred), so a consumer sees WHY it matched without
            # opening the whole thread. «…» mark the matched terms. None for
            # browse mode or semantic-only hits (no lexical match to show).
            snippet = None
            if fts_query is not None:
                try:
                    srow = conn.execute(
                        "SELECT snippet(fts, 0, '«', '»', '…', 16) FROM fts "
                        "WHERE prompt_id = ? AND fts MATCH ? "
                        "ORDER BY (kind='card') DESC, rank LIMIT 1",
                        (prompt_id, fts_query)).fetchone()
                    if srow and srow[0]:
                        snippet = " ".join(srow[0].split())[:240]
                except sqlite3.OperationalError:
                    pass
            results.append({
                "prompt_id": prompt_id, "matches": matches,
                "score": round(score or 0.0, 3),
                "snippet": snippet,
                "turn_count": meta[0], "est_tokens": meta[1],
                "first_ts": meta[2], "last_ts": meta[3],
                "session_id": meta[4], "project": sess[0],
                "session_title": sess[1],
                "sidechain_turns": sidechain_turns, "agent": agent,
                "models": meta[6],
                "title": card[0], "type": card[1], "outcome": card[2],
                "tags": tags,
                "decisions": decisions,
            })

        if kind in ("main", "subagent"):
            results = [h for h in results if h["agent"] == kind]
        if model:
            results = [h for h in results
                       if model.lower() in (h["models"] or "").lower()]
        if project:
            results = [h for h in results
                       if project.lower() in (h["project"] or "").lower()]
        if session:
            results = [h for h in results
                       if (h["session_id"] or "").startswith(session)]
        # date window as a POST-filter too (not just SQL): the semantic booster
        # adds prompt_ids AFTER the SQL date filter, so without this an
        # out-of-window/inverted range would leak semantic hits. Now search and
        # timeline agree (inverted/empty window → empty).
        if since:
            results = [h for h in results if (h["last_ts"] or "") >= since]
        if until:
            results = [h for h in results if (h["first_ts"] or "") <= until]
        # absolute, saturating confidence — NOT batch-relative — so a weak
        # query's best hit reads ~low, not 100%, and is comparable across
        # queries. low_confidence: a hit a consumer should not anchor on.
        if fts_query is not None:
            for h in results:
                s = h["score"] or 0
                h["score_pct"] = round(100 * s / (s + _SCORE_HALF))
                h["low_confidence"] = h["score_pct"] < _LOW_CONF_PCT
        results.sort(key=SORT_KEYS.get(sort, SORT_KEYS["relevance"]),
                     reverse=(sort == "recent"))
        if boost_project and fts_query is not None:
            # SOFT boost — current-project gets a fixed bonus so it outranks a
            # COMPARABLE global hit, but a much stronger global match can still
            # surface (a hard partition buried strong cross-project answers under
            # weak local ones).
            bp = boost_project.lower()
            results.sort(key=lambda h: -((h.get("score_pct") or 0)
                         + (_PROJECT_BOOST if bp in (h.get("project") or "").lower()
                            else 0)))
        page = results[offset:offset + limit]
        fdb.log_op(db_path, "search", q=query or "", hits=len(page))
        return page
    finally:
        conn.close()


def timeline(db_path: str, since: Optional[str] = None,
             until: Optional[str] = None, project: Optional[str] = None,
             limit: int = 50, offset: int = 0) -> dict:
    """Browse threads BY TIME — the 'what was I working on around <date>'
    view, no search query needed. since/until take a date (YYYY-MM-DD) or a
    full ISO timestamp; a date-only `until` is inclusive of that whole day.
    Returns threads in the window newest-first with project/title/type/tags
    for scanning and a prompt_id to open via fable_thread. total_in_window is
    the true count so truncation by `limit` is never silent."""
    since, until = _time_bounds(since, until)
    conn = fdb.connect(db_path)
    try:
        where = ["t.first_ts IS NOT NULL"]
        args: list = []
        if since:
            where.append("t.last_ts >= ?")
            args.append(since)
        if until:
            where.append("t.first_ts <= ?")
            args.append(until)
        if project:
            where.append("t.session_id IN (SELECT session_id FROM sessions "
                         "WHERE LOWER(project) LIKE ?)")
            args.append(f"%{project.lower()}%")
        clause = " AND ".join(where)
        total = conn.execute(
            f"SELECT COUNT(*) FROM threads t WHERE {clause}",
            args).fetchone()[0]
        # window aggregates + per-day rollup over the FULL window (not just the
        # returned page) — answers "how much did I work" / "which days"
        from collections import Counter, defaultdict
        day = defaultdict(lambda: {"threads": 0, "est_tokens": 0,
                                   "projects": Counter()})
        tot_turns = tot_tokens = 0
        for d, p, tok, turns in conn.execute(
                f"SELECT substr(t.first_ts,1,10), COALESCE(s.project,'?'), "
                f"COALESCE(t.est_tokens,0), COALESCE(t.turn_count,0) "
                f"FROM threads t LEFT JOIN sessions s "
                f"ON s.session_id = t.session_id WHERE {clause}", args):
            a = day[d]
            a["threads"] += 1
            a["est_tokens"] += tok
            a["projects"][p] += 1
            tot_turns += turns
            tot_tokens += tok
        by_day = [{"date": d, "threads": a["threads"],
                   "est_tokens": a["est_tokens"],
                   "top_projects": [p for p, _ in a["projects"].most_common(3)]}
                  for d, a in sorted(day.items(), reverse=True)]
        rows = conn.execute(
            f"SELECT t.prompt_id, t.first_ts, t.last_ts, t.turn_count, "
            f"t.est_tokens, t.session_id FROM threads t WHERE {clause} "
            f"ORDER BY t.first_ts DESC LIMIT ? OFFSET ?",
            args + [int(limit), int(offset)]).fetchall()
        threads = []
        for pid, fts, lts, tc, tok, sid in rows:
            sess = conn.execute(
                "SELECT project, title FROM sessions WHERE session_id = ?",
                (sid,)).fetchone() or (None, None)
            card = conn.execute(
                "SELECT title, type, outcome FROM cards WHERE prompt_id = ?",
                (pid,)).fetchone() or (None, None, None)
            tags = [f"{f}:{v}" for f, v in conn.execute(
                "SELECT family, value FROM thread_tags WHERE prompt_id = ?"
                " ORDER BY family", (pid,))]
            threads.append({
                "prompt_id": pid, "first_ts": fts, "last_ts": lts,
                "turn_count": tc, "est_tokens": tok,
                "session_id": sid, "project": sess[0],
                "session_title": sess[1], "title": card[0],
                "type": card[1], "outcome": card[2], "tags": tags})
        fdb.log_op(db_path, "timeline", q=f"{since or ''}..{until or ''}",
                   hits=len(threads))
        return {"since": since, "until": until, "project": project,
                "total_in_window": total, "offset": offset,
                "returned": len(threads),
                "totals": {"threads": total, "turns": tot_turns,
                           "est_tokens": tot_tokens},
                "by_day": by_day, "threads": threads}
    finally:
        conn.close()


def overview(db_path: str, project: Optional[str] = None) -> dict:
    """Cold-start corpus map — the 'home screen' an agent reads BEFORE searching.
    Threads, open tasks, activity span, top technologies + topics and recent
    titles, ALL grouped by ONE project key per session (its work-project where
    known, else its cwd-project) so thread counts and task counts reconcile —
    a project never reads '3000 threads, 0 tasks' because the two were keyed
    differently. Pass `project` to scope to one."""
    from collections import Counter, defaultdict
    from fable import tasktime
    wp = tasktime.work_projects(db_path)            # {session_id: work-project}
    conn = fdb.connect(db_path)
    try:
        cwd_map = dict(conn.execute(
            "SELECT session_id, project FROM sessions"))

        def proj_of(sid):
            return wp.get(sid) or cwd_map.get(sid) or "?"

        agg: dict = {}   # project -> {threads, first, last, tokens, recent[]}
        for sid, fts, lts, tok, title in conn.execute(
                "SELECT t.session_id, t.first_ts, t.last_ts, "
                "COALESCE(t.est_tokens,0), c.title FROM threads t "
                "LEFT JOIN cards c ON c.prompt_id = t.prompt_id "
                "ORDER BY t.last_ts DESC"):
            a = agg.setdefault(proj_of(sid), {
                "threads": 0, "first": None, "last": None,
                "tokens": 0, "recent": []})
            a["threads"] += 1
            a["tokens"] += tok
            if lts and (a["last"] is None or lts > a["last"]):
                a["last"] = lts
            if fts and (a["first"] is None or fts < a["first"]):
                a["first"] = fts
            if title and len(a["recent"]) < 5 and title not in a["recent"]:
                a["recent"].append(title)
        # open tasks by the SAME key (this is what reconciles with threads)
        open_by = Counter()
        for tt in tasktime.read(db_path).get("tasks", []):
            if tt.get("drifted") and tt.get("status") in (
                    "pending", "in_progress"):
                open_by[proj_of(tt.get("session"))] += 1
        tech = defaultdict(Counter)
        topics = defaultdict(Counter)
        for sid, fam, val in conn.execute(
                "SELECT th.session_id, tg.family, tg.value FROM thread_tags tg "
                "JOIN threads th ON th.prompt_id = tg.prompt_id "
                "WHERE tg.family IN ('technology','topic')"):
            (tech if fam == "technology" else topics)[proj_of(sid)][val] += 1
        projects = []
        for p, a in sorted(agg.items(), key=lambda kv: -kv[1]["threads"]):
            if project and project.lower() not in p.lower():
                continue
            projects.append({
                "name": p, "threads": a["threads"],
                "open_tasks": open_by.get(p, 0),
                "first_active": a["first"], "last_active": a["last"],
                "est_tokens": a["tokens"],
                "top_technologies": [v for v, _ in tech[p].most_common(5)],
                "top_topics": [v for v, _ in topics[p].most_common(5)],
                "recent_titles": a["recent"]})
        firsts = [p["first_active"] for p in projects if p["first_active"]]
        lasts = [p["last_active"] for p in projects if p["last_active"]]
        return {
            "totals": {"projects": len(projects),
                       "threads": sum(p["threads"] for p in projects),
                       "open_tasks": sum(p["open_tasks"] for p in projects)},
            "span": {"first": min(firsts) if firsts else None,
                     "last": max(lasts) if lasts else None},
            "projects": projects}
    finally:
        conn.close()


def _jsonlist(s):
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def executive(db_path: str, top: int = 10) -> dict:
    """High-level, NARRATABLE briefing of the WHOLE archive — the 'state of every
    project' for a voice/assistant layer to speak. Reuses overview() and enriches
    each project with its most recent decisions + an activity status, plus a
    ready-to-speak `narration`. NOT a per-query recall tool: the bird's-eye
    'what's going on across everything', for relaying — not for routing."""
    import datetime
    from collections import defaultdict
    from fable import tasktime
    ov = overview(db_path)
    wp = tasktime.work_projects(db_path)
    conn = fdb.connect(db_path)
    try:
        cwd_map = dict(conn.execute("SELECT session_id, project FROM sessions"))

        def proj_of(sid):
            return wp.get(sid) or cwd_map.get(sid) or "?"

        dec_by = defaultdict(list)
        for sid, dec in conn.execute(
                "SELECT t.session_id, c.decisions FROM cards c "
                "JOIN threads t ON t.prompt_id = c.prompt_id "
                "WHERE c.decisions IS NOT NULL AND c.decisions != '[]' "
                "ORDER BY t.last_ts DESC"):
            p = proj_of(sid)
            if len(dec_by[p]) < 3:
                for d in _jsonlist(dec)[:1]:
                    dec_by[p].append(d)
    finally:
        conn.close()
    now = datetime.datetime.now(datetime.timezone.utc)

    def status(last):
        if not last:
            return "dormant"
        try:
            d = (now - datetime.datetime.fromisoformat(
                last.replace("Z", "+00:00"))).days
        except Exception:
            return "unknown"
        return ("active" if d <= 2 else "recent" if d <= 14
                else "idle" if d <= 60 else "dormant")

    projs = []
    for p in ov["projects"][:top]:
        projs.append({
            "name": p["name"], "status": status(p["last_active"]),
            "last_active": p["last_active"], "threads": p["threads"],
            "open_tasks": p["open_tasks"], "about": p["top_topics"][:4],
            "tech": p["top_technologies"][:3], "recent": p["recent_titles"][:3],
            "decisions": dec_by.get(p["name"], [])[:2]})
    t = ov["totals"]
    parts = [f"{t['projects']} projects, {t['threads']} threads, "
             f"{t['open_tasks']} open tasks across the archive."]
    for p in projs[:5]:
        s = (f"{p['name']}: {p['status']}, {p['threads']} threads, "
             f"{p['open_tasks']} open")
        if p["about"]:
            s += f", on {', '.join(p['about'][:2])}"
        if p["decisions"]:
            s += f"; last decided — {p['decisions'][0][:90]}"
        parts.append(s + ".")
    return {"totals": t, "span": ov["span"], "projects": projs,
            "narration": " ".join(parts)}


def resume(db_path: str, project: Optional[str] = None) -> dict:
    """'Where did I leave off?' — continuity for one project: last-active time,
    the most recent threads (what you were doing), the last decisions made, any
    unresolved open questions, and the top open tasks, plus a suggested next
    step. The cold-start answer every reopened project needs, in one call. With
    no `project`, resumes the most-recently-active one."""
    conn = fdb.connect(db_path)
    try:
        psql = ("SELECT s.project, MAX(t.last_ts), COUNT(DISTINCT t.prompt_id) "
                "FROM threads t JOIN sessions s ON s.session_id = t.session_id ")
        if project:
            prow = conn.execute(
                psql + "WHERE LOWER(s.project) LIKE ? GROUP BY s.project "
                "ORDER BY 2 DESC LIMIT 1",
                (f"%{project.lower()}%",)).fetchone()
        else:
            prow = conn.execute(
                psql + "GROUP BY s.project ORDER BY 2 DESC LIMIT 1").fetchone()
        if not prow or not prow[0]:
            return {"project": project, "found": False,
                    "note": "no indexed threads for that project"}
        proj, last_active, nthreads = prow
        recent, decisions, questions = [], [], []
        for pid, lts, title, typ, outcome, dec, oq in conn.execute(
                "SELECT t.prompt_id, t.last_ts, c.title, c.type, c.outcome, "
                "c.decisions, c.open_questions FROM threads t "
                "JOIN sessions s ON s.session_id = t.session_id "
                "LEFT JOIN cards c ON c.prompt_id = t.prompt_id "
                "WHERE s.project = ? ORDER BY t.last_ts DESC LIMIT 20",
                (proj,)):
            if len(recent) < 6:
                recent.append({"prompt_id": pid, "last_ts": lts,
                               "title": title or "(latest — not yet carded)",
                               "type": typ, "outcome": outcome})
            for d in _jsonlist(dec):
                if len(decisions) < 5:
                    decisions.append({"decision": d, "prompt_id": pid,
                                      "ts": lts})
            for q in _jsonlist(oq):
                if len(questions) < 5:
                    questions.append({"question": q, "prompt_id": pid})
        from fable import tasktime
        rows, total, _ = tasktime.open_for_project(db_path, proj, 6)
        open_tasks = [{"id": r[0], "status": r[1], "subject": r[2],
                       "priority": r[3], "work_project": r[4]} for r in rows]
        suggested = (open_tasks[0]["subject"] if open_tasks else
                     (questions[0]["question"] if questions else
                      "no open tasks — review recent threads"))
        fdb.log_op(db_path, "resume", q=proj, hits=nthreads)
        return {"project": proj, "found": True, "last_active": last_active,
                "threads": nthreads, "recent_threads": recent,
                "last_decisions": decisions, "open_questions": questions,
                "open_tasks": open_tasks, "open_task_total": total,
                "suggested_next": suggested}
    finally:
        conn.close()
