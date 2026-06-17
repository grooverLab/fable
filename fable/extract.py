"""Searchable-text extraction and the memory-inception guard.

Only signal goes into FTS: human/assistant text, thinking, tool_use inputs
that carry meaning (commands, paths, queries), and a capped head of each
tool_result. Base64 and image payloads never reach the index.

Inception guard: text inside <historical_context ...> spans is recalled
memory pasted back into a session. Indexing it would let memories
recursively swallow themselves, so spans are stripped and recorded as
citation edges instead.
"""
import re
from typing import List, Tuple

TOOL_RESULT_CAP = 2048

# tool_use input fields that carry searchable meaning (mirrors pruner v1)
TOOL_INPUT_FIELDS = ("command", "description", "file_path", "path",
                     "pattern", "query", "url", "prompt")

_BASE64ISH = re.compile(r"[A-Za-z0-9+/=_-]{200,}")
_HC_OPEN = re.compile(r"<historical_context\b([^>]*)>", re.IGNORECASE)
_HC_SPAN = re.compile(
    r"<historical_context\b[^>]*>.*?(?:</historical_context>|\Z)",
    re.IGNORECASE | re.DOTALL)
_ATTR = re.compile(r'(\w+)="([^"]*)"')
# anything fable itself injects (the externalisation reminder, the proactive
# <fable-scout> teaser, <fable-open-tasks>, <fable-memory> recovery) must NEVER
# enter the index — else a memory tags a memory → exponential bloat. Strip every
# <fable-…> span the way recalled-memory spans are stripped.
_FABLE_DIRECTIVE = re.compile(
    r"<fable-[a-z0-9-]+\b[^>]*>.*?(?:</fable-[a-z0-9-]+>|\Z)",
    re.IGNORECASE | re.DOTALL)


def strip_historical(text: str) -> Tuple[str, List[str]]:
    """Remove <historical_context> spans; return (cleaned_text, refs)."""
    refs: List[str] = []
    for m in _HC_OPEN.finditer(text):
        attrs = dict(_ATTR.findall(m.group(1)))
        raw = attrs.get("arcs") or attrs.get("thread") or ""
        refs.extend(r.strip() for r in raw.split(",") if r.strip())
    cleaned = _HC_SPAN.sub("", text)
    return cleaned, refs


def replace_historical(text: str) -> Tuple[str, List[str]]:
    """Replace each <historical_context> span with a <consulted_arcs> stub.

    Used by the pruner: the recalled payload is evicted but the citation
    edge survives in the live transcript.
    """
    all_refs: List[str] = []

    def _stub(match):
        open_tag = _HC_OPEN.search(match.group(0))
        attrs = dict(_ATTR.findall(open_tag.group(1))) if open_tag else {}
        raw = attrs.get("arcs") or attrs.get("thread") or ""
        refs = [r.strip() for r in raw.split(",") if r.strip()]
        all_refs.extend(refs)
        return f'<consulted_arcs refs="{",".join(refs)}"/>'

    return _HC_SPAN.sub(_stub, text), all_refs


def _clean(text: str) -> str:
    return _BASE64ISH.sub(" ", text)


def extract_full(obj) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Return ([(kind, text), ...], citation_refs) for one record."""
    pairs: List[Tuple[str, str]] = []
    refs: List[str] = []

    def add_text(kind, text):
        if not text:
            return
        cleaned, found = strip_historical(text)
        refs.extend(found)
        cleaned = _FABLE_DIRECTIVE.sub("", cleaned)
        # surrogateescape chars from invalid UTF-8 can't enter SQLite
        cleaned = _clean(cleaned).strip()
        cleaned = cleaned.encode("utf-8", "replace").decode("utf-8")
        if cleaned:
            pairs.append((kind, cleaned))

    msg = obj.get("message")
    if not isinstance(msg, dict):
        return pairs, refs
    content = msg.get("content")

    if isinstance(content, str):
        add_text("text", content)
        return pairs, refs
    if not isinstance(content, list):
        return pairs, refs

    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            add_text("text", block.get("text", ""))
        elif kind == "thinking":
            add_text("thinking", block.get("thinking", ""))
        elif kind == "tool_use":
            inp = block.get("input")
            parts = [block.get("name", "")]
            if isinstance(inp, dict):
                for field in TOOL_INPUT_FIELDS:
                    val = inp.get(field)
                    if isinstance(val, str) and val:
                        parts.append(val)
            add_text("tool_use", "\n".join(p for p in parts if p))
        elif kind == "tool_result":
            inner = block.get("content")
            texts = []
            if isinstance(inner, str):
                texts.append(inner)
            elif isinstance(inner, list):
                for b in inner:
                    if isinstance(b, dict) and b.get("type") == "text":
                        texts.append(b.get("text", ""))
            joined = "\n".join(texts)[:TOOL_RESULT_CAP]
            add_text("tool_result", joined)
        # image and unknown block types: never indexed

    return pairs, refs


def record_text(obj) -> List[Tuple[str, str]]:
    return extract_full(obj)[0]


def fts_extract_fn(conn, uuid: str, obj, old_rowid=None):
    """indexer extract_fn hook: refresh this record's FTS row + citations.

    Keyed by rowid, not the UNINDEXED uuid column — a WHERE on an UNINDEXED
    fts5 column is a full virtual-table scan (O(N) per record, O(N^2)
    overall). Returns the new rowid for the records table to remember.
    """
    pairs, refs = extract_full(obj)
    if old_rowid:
        conn.execute("DELETE FROM fts WHERE rowid = ?", (old_rowid,))
    rowid = None
    if pairs:
        content = "\n".join(t for _, t in pairs)
        kinds = ",".join(sorted({k for k, _ in pairs}))
        cur = conn.execute(
            "INSERT INTO fts(content, uuid, prompt_id, kind) VALUES(?,?,?,?)",
            (content, uuid, obj.get("promptId"), kinds))
        rowid = cur.lastrowid
    for ref in refs:
        conn.execute(
            "INSERT OR IGNORE INTO citations(from_uuid, ref) VALUES(?,?)",
            (uuid, ref))
    return rowid
