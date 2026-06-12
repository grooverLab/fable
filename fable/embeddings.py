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
                                 "prompt": text[:8000]}).encode(),
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


def embed_cards(db_path: str, on_progress=None) -> dict:
    be = backend()
    if not be:
        raise EmbeddingError("no embedding backend available")
    conn = fdb.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT c.prompt_id, c.title, c.topics, c.summary FROM cards c "
            "WHERE c.prompt_id NOT IN (SELECT prompt_id FROM embeddings)"
        ).fetchall()
        done = 0
        for i in range(0, len(rows), 16):
            batch = rows[i:i + 16]
            texts = [f"{t}\n{topics}\n{s}" for _, t, topics, s in batch]
            vecs = embed_texts(texts, be)
            for (pid, *_), vec in zip(batch, vecs):
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings(prompt_id, vec, dim,"
                    " backend) VALUES(?,?,?,?)",
                    (pid, _pack(vec), len(vec), be))
            conn.commit()
            done += len(batch)
            if on_progress:
                on_progress(f"embedded {done}/{len(rows)}")
        return {"embedded": done, "backend": be,
                "total_cards": done + conn.execute(
                    "SELECT COUNT(*) FROM embeddings").fetchone()[0] - done}
    finally:
        conn.close()


_CACHE = {"key": None, "rows": None}


def semantic_hits(db_path: str, query: str, limit: int = 10):
    """Top card matches by cosine; [] if no backend or no embeddings."""
    be = backend()
    if not be:
        return []
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
    finally:
        conn.close()
    try:
        qvec = embed_texts([query], be)[0]
    except (EmbeddingError, urllib.error.URLError, OSError):
        return []
    qnorm = math.sqrt(sum(x * x for x in qvec)) or 1.0
    scored = []
    for pid, vec in _CACHE["rows"]:
        if len(vec) != len(qvec):
            continue
        dot = sum(a * b for a, b in zip(qvec, vec))
        vnorm = math.sqrt(sum(x * x for x in vec)) or 1.0
        scored.append((dot / (qnorm * vnorm), pid))
    scored.sort(reverse=True)
    return [(pid, round(cos, 4)) for cos, pid in scored[:limit] if cos > 0.3]


def cmd_embed(args) -> int:
    stats = embed_cards(args.db, on_progress=lambda m: print(m, flush=True))
    print(json.dumps(stats))
    return 0
