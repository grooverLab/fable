"""Hybrid semantic layer — embed the CARDS, not raw chunks.

Keyword FTS misses conceptual matches ("auth token rotation" vs "jwt
refresh"). Cards are the right embedding unit: a few thousand dense
summaries instead of 150k raw records. Backends, local-first:
  ollama — http://localhost:11434 (any embed model, default nomic-embed-text)
  openai — OPENAI_API_KEY, text-embedding-3-small
No backend -> everything degrades gracefully to pure FTS.
"""
import json
import math
import os
import struct
import urllib.error
import urllib.request

from fable import db as fdb
from fable.openrouter import load_env

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "nomic-embed-text"


class EmbeddingError(RuntimeError):
    pass


def backend() -> str:
    """'ollama' | 'openai' | '' (disabled)."""
    load_env()
    try:
        req = urllib.request.Request(
            (os.environ.get("OLLAMA_URL") or OLLAMA_URL) + "/api/tags")
        with urllib.request.urlopen(req, timeout=1.5):
            return "ollama"
    except (urllib.error.URLError, OSError):
        pass
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return ""


def embed_texts(texts, be=None):
    be = be or backend()
    if not be:
        raise EmbeddingError(
            "no embedding backend — run Ollama locally (ollama pull "
            f"{OLLAMA_MODEL}) or set OPENAI_API_KEY")
    if be == "ollama":
        base = os.environ.get("OLLAMA_URL") or OLLAMA_URL
        model = os.environ.get("OLLAMA_EMBED_MODEL") or OLLAMA_MODEL
        out = []
        for text in texts:
            req = urllib.request.Request(
                base + "/api/embeddings",
                data=json.dumps({"model": model,
                                 "prompt": text[:2000]}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                out.append(json.loads(resp.read())["embedding"])
        return out
    # openai
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=json.dumps({"model": "text-embedding-3-small",
                         "input": [t[:8000] for t in texts]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())["data"]
    return [d["embedding"] for d in data]


def _pack(vec):
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob, dim):
    return struct.unpack(f"{dim}f", blob)


THREAD_TEXT_BUDGET = 1500  # chars (~375 tok): focused + fast, and stays well
# under embedding-model input caps (nomic-embed-text 500s on ~6k-char input).
# Focused text also embeds better than long tool-laden dumps.


def _thread_text(conn, prompt_id, budget=THREAD_TEXT_BUDGET):
    """A thread's own conversation text for embedding — pulled from the FTS
    index (already extracted + tool-noise-capped), concatenated and budgeted.
    No disk reads, no giant tool dumps to drown the signal."""
    rows = conn.execute(
        "SELECT content FROM fts WHERE prompt_id = ? AND kind != 'card'",
        (prompt_id,)).fetchall()
    return "\n".join(r[0] for r in rows if r[0]).strip()[:budget]


def _embed_pending(conn, be, kind, rows, on_progress=None):
    """rows: (prompt_id, text). Embed the non-empty ones, store as `kind`.
    A transient backend hiccup retries then SKIPS that batch (the run is
    incremental, so the next pass picks it up) — one timeout must never zero
    the whole pass. Returns count embedded."""
    import time as _t
    rows = [(pid, t) for pid, t in rows if t and t.strip()]
    done = 0
    for i in range(0, len(rows), 16):
        batch = rows[i:i + 16]
        vecs = None
        for attempt in range(3):
            try:
                vecs = embed_texts([t for _, t in batch], be)
                break
            except (EmbeddingError, urllib.error.URLError, OSError):
                _t.sleep(2 * (attempt + 1))
        if vecs is None:
            continue  # skip this batch; a later run will retry it
        for (pid, _), vec in zip(batch, vecs):
            conn.execute(
                "INSERT OR REPLACE INTO embeddings(prompt_id, kind, vec, dim,"
                " backend) VALUES(?,?,?,?,?)",
                (pid, kind, _pack(vec), len(vec), be))
        conn.commit()
        done += len(batch)
        if on_progress:
            on_progress(f"embedded {kind} {done}/{len(rows)}")
    return done


def embed(db_path: str, source: str = "both", on_progress=None) -> dict:
    """Embed card summaries and/or each thread's own text.

    source: 'card' | 'thread' | 'both'. The card vector is a distilled
    'what this was about'; the thread vector is the conversation's actual
    language and covers EVERY thread, carded or not. Both coexist and the
    best match wins at query time. Incremental: only embeds what's missing.
    """
    be = backend()
    if not be:
        raise EmbeddingError("no embedding backend available")
    conn = fdb.connect(db_path)
    try:
        out = {"backend": be, "card": 0, "thread": 0}
        if source in ("card", "both"):
            rows = conn.execute(
                "SELECT prompt_id, title || char(10) || COALESCE(topics,'')"
                " || char(10) || COALESCE(summary,'') FROM cards "
                "WHERE prompt_id NOT IN "
                "(SELECT prompt_id FROM embeddings WHERE kind='card')"
            ).fetchall()
            out["card"] = _embed_pending(conn, be, "card", rows, on_progress)
        if source in ("thread", "both"):
            need = {r[0] for r in conn.execute(
                "SELECT prompt_id FROM threads WHERE prompt_id NOT IN "
                "(SELECT prompt_id FROM embeddings WHERE kind='thread')")}
            # fts.prompt_id is UNINDEXED, so a per-thread WHERE is a full
            # scan — gather every thread's text in ONE pass and group it.
            texts = {}
            for pid, content in conn.execute(
                    "SELECT prompt_id, content FROM fts "
                    "WHERE prompt_id IS NOT NULL AND kind != 'card'"):
                if pid not in need or not content:
                    continue
                cur = texts.get(pid, "")
                if len(cur) < THREAD_TEXT_BUDGET:
                    texts[pid] = (cur + "\n" + content)[:THREAD_TEXT_BUDGET]
            out["thread"] = _embed_pending(conn, be, "thread",
                                           list(texts.items()), on_progress)
        out["embedded"] = out["card"] + out["thread"]
        return out
    finally:
        conn.close()


def embed_cards(db_path: str, on_progress=None) -> dict:
    """Back-compat entry (used by `fable watch`): embed both sources."""
    return embed(db_path, "both", on_progress)


_CACHE = {"key": None, "rows": None}


def _vectors(db_path: str):
    """All (prompt_id, vec) embedding rows, cached by (db_path, row count)."""
    conn = fdb.connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        if not n:
            return []
        key = (db_path, n)
        if _CACHE["key"] != key:
            _CACHE["rows"] = [
                (pid, _unpack(blob, dim))
                for pid, blob, dim in conn.execute(
                    "SELECT prompt_id, vec, dim FROM embeddings")]
            _CACHE["key"] = key
        return _CACHE["rows"]
    finally:
        conn.close()


def _neighbors(rows, qvec, limit, exclude=None):
    """Top prompt_ids by cosine to qvec across rows (best vec per prompt_id)."""
    qnorm = math.sqrt(sum(x * x for x in qvec)) or 1.0
    best = {}
    for pid, vec in rows:
        if pid == exclude or len(vec) != len(qvec):
            continue
        dot = sum(a * b for a, b in zip(qvec, vec))
        vnorm = math.sqrt(sum(x * x for x in vec)) or 1.0
        cos = dot / (qnorm * vnorm)
        if cos > best.get(pid, -2.0):
            best[pid] = cos
    scored = sorted(((c, pid) for pid, c in best.items()), reverse=True)
    return [(pid, round(c, 4)) for c, pid in scored[:limit] if c > 0.3]


def semantic_hits(db_path: str, query: str, limit: int = 10):
    """Top card matches by cosine; [] if no backend or no embeddings."""
    be = backend()
    if not be:
        return []
    rows = _vectors(db_path)
    if not rows:
        return []
    try:
        qvec = embed_texts([query], be)[0]
    except (EmbeddingError, urllib.error.URLError, OSError):
        return []
    return _neighbors(rows, qvec, limit)


def similar(db_path: str, prompt_id: str, limit: int = 8):
    """Nearest-neighbour threads to a given one (more-like-this), by card/
    thread embedding cosine. [] if no backend/embeddings or id not embedded —
    no re-embedding, it reuses the vectors already stored."""
    if not backend():
        return []
    rows = _vectors(db_path)
    selfvecs = [vec for pid, vec in rows if pid == prompt_id]
    if not selfvecs:
        return []
    return _neighbors(rows, selfvecs[0], limit, exclude=prompt_id)


def cmd_embed(args) -> int:
    source = getattr(args, "source", "both") or "both"
    stats = embed(args.db, source=source,
                  on_progress=lambda m: print(m, flush=True))
    print(json.dumps(stats))
    return 0
