"""Prune slimmers — per-category, composable byte/token savings analysis.

The current prune is blunt (stub every tool result). Slimmers are surgical: each
is a *detector* over the transcript that reports what it could remove and how
much that saves — WITHOUT removing anything. The dashboard shows them as
toggleable categories with per-category savings + a live before→after total;
apply (built on top of this, separately) runs only the categories you tick.

This module is read-only analysis. Nothing here mutates the transcript, so it is
safe to call on a live session. Each detector returns the set of *record uuids*
(plus an optional block predicate) it would slim, so a future apply step can act
on exactly what was estimated — the estimate and the action stay in lock-step.

Categories implemented here (the mechanical, low-risk ones):
  - thinking      assistant reasoning blocks (UNSIGNED only; signed thinking
                  must survive verbatim for replay) — default OFF (kept)
  - dup_reads     the same file read N times — keep the latest, the rest are
                  redundant — default ON
  - superseded    earlier Edit/Write to a file that a later edit replaced —
                  default ON

Deliberately NOT here yet (need judgement / more care, tracked separately):
  - tool_boilerplate  (keep results, strip command-echo/exit-code chrome)
  - resolved_errors   (error traces fixed downstream — risky; can drop a
                       still-open error — must be conservative/opt-in)
"""
import json


def _blocks(msg):
    """The content blocks of a record, or [] if it has none / is plain text."""
    content = (msg.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _bytes(obj):
    try:
        return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return 0


def _cat(key, label, why, default):
    return {"key": key, "label": label, "why": why, "default": default,
            "bytes": 0, "count": 0, "uuids": []}


def analyze(messages):
    """Per-category savings over a list of transcript records (dicts).

    Returns an ordered list of category dicts:
        {key, label, why, default, bytes, tokens, count, uuids}
    `bytes`/`tokens` are what that category would reclaim; `uuids` are the
    records it touches (for a matching apply step)."""
    thinking = _cat("thinking", "assistant thinking blocks",
                    "internal reasoning never shown to you", False)
    dup = _cat("dup_reads", "duplicate file reads",
               "same file read more than once — keep the latest", True)
    sup = _cat("superseded", "superseded code edits",
               "an earlier edit to a file that a later edit replaced", True)

    reads = {}            # file_path -> [(result_block, uuid)]   (in order)
    edits = {}            # file_path -> [(tool_use_block, uuid)] (in order)
    read_use_file = {}    # tool_use_id -> file_path  (Read calls)

    # pass 1: thinking blocks + index Read/Edit/Write tool_use by file
    for m in messages:
        uuid = m.get("uuid")
        for b in _blocks(m):
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "thinking" and not b.get("signature"):
                thinking["bytes"] += _bytes(b)
                thinking["count"] += 1
                thinking["uuids"].append(uuid)
            elif t == "tool_use":
                name = b.get("name")
                fp = (b.get("input") or {}).get("file_path")
                if name == "Read" and fp:
                    read_use_file[b.get("id")] = fp
                elif name in ("Edit", "Write", "MultiEdit") and fp:
                    edits.setdefault(fp, []).append((b, uuid))

    # pass 2: map tool_result back to the file its Read requested
    for m in messages:
        uuid = m.get("uuid")
        for b in _blocks(m):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                fp = read_use_file.get(b.get("tool_use_id"))
                if fp:
                    reads.setdefault(fp, []).append((b, uuid))

    # dup reads: every result except the latest, per file
    for rs in reads.values():
        for b, uuid in rs[:-1]:
            dup["bytes"] += _bytes(b)
            dup["count"] += 1
            dup["uuids"].append(uuid)

    # superseded edits: every edit except the latest, per file
    for es in edits.values():
        for b, uuid in es[:-1]:
            sup["bytes"] += _bytes(b)
            sup["count"] += 1
            sup["uuids"].append(uuid)

    out = []
    for c in (dup, sup, thinking):  # default-ON categories first
        c["tokens"] = c["bytes"] // 4
        out.append(c)
    return out


def analyze_session(live_path):
    """Read a live transcript and return its per-category slimming estimate."""
    from fable.jsonl import iter_records
    msgs = [rec.obj for rec in iter_records(str(live_path))]
    cats = analyze(msgs)
    total = sum(c["bytes"] for c in cats)
    return {"categories": cats, "reclaimable_bytes": total,
            "reclaimable_tokens": total // 4}


_STUB = "[fable: slimmed — recover from the vault]"


def _targets(messages):
    """Per-category (msg_index, block_index) blocks each slimmer would slim —
    the single source of truth so the estimate and the apply stay in lock-step."""
    reads, edits, read_use_file, thinking = {}, {}, {}, []
    for mi, m in enumerate(messages):
        for bi, b in enumerate(_blocks(m)):
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "thinking" and not b.get("signature"):
                thinking.append((mi, bi))
            elif t == "tool_use":
                name = b.get("name")
                fp = (b.get("input") or {}).get("file_path")
                if name == "Read" and fp:
                    read_use_file[b.get("id")] = fp
                elif name in ("Edit", "Write", "MultiEdit") and fp:
                    edits.setdefault(fp, []).append((mi, bi))
    for mi, m in enumerate(messages):
        for bi, b in enumerate(_blocks(m)):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                fp = read_use_file.get(b.get("tool_use_id"))
                if fp:
                    reads.setdefault(fp, []).append((mi, bi))
    return {
        "dup_reads": [t for rs in reads.values() for t in rs[:-1]],
        "superseded": [t for es in edits.values() for t in es[:-1]],
        "thinking": thinking,
    }


def apply(messages, selected):
    """Deep-copy `messages` with the SELECTED categories' heavy blocks stubbed.
    Records and the parent chain are preserved (only block *content* is
    replaced); SIGNED thinking is never a target, so it's never touched. Pure —
    writes nothing. Returns (slimmed_messages, blocks_slimmed)."""
    import copy
    msgs = copy.deepcopy(messages)
    tg = _targets(msgs)
    n = 0
    for key in selected:
        for mi, bi in tg.get(key, []):
            b = _blocks(msgs[mi])[bi]
            if key == "thinking" and "thinking" in b:
                b["thinking"] = _STUB
            elif key == "dup_reads":
                b["content"] = _STUB
            elif key == "superseded":
                inp = b.get("input") or {}
                for k in ("old_string", "new_string", "content"):
                    if k in inp:
                        inp[k] = _STUB
            n += 1
    return msgs, n


def apply_session(live_path, selected, backup_dir, db_path=None):
    """Slim the SELECTED categories in a live transcript, in place. Seals the
    pre-slim file to the vault FIRST (so it's fully recoverable), then rewrites
    the live file with the stubbed blocks, then reindexes. Signed thinking and
    the parent chain survive (apply never touches them). Returns a report."""
    import json
    import os
    from pathlib import Path
    from fable.jsonl import iter_records
    from fable.prune import backup
    live = str(live_path)
    msgs = [rec.obj for rec in iter_records(live)]
    new_msgs, n = apply(msgs, set(selected or []))
    if not n:
        return {"ok": False, "reason": "nothing selected / nothing to slim"}
    version, sealed = backup(Path(live), Path(backup_dir))   # → vault, recoverable
    tmp = live + ".fbtmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for m in new_msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    os.replace(tmp, live)
    reindexed = False
    if db_path:
        try:
            from fable.indexer import index_vault
            from fable.extract import fts_extract_fn
            index_vault(db_path, [str(sealed)], live_file=live,
                        extract_fn=fts_extract_fn)
            reindexed = True
        except Exception:
            pass
    return {"ok": True, "version": version, "blocks_slimmed": n,
            "sealed": str(sealed), "reindexed": reindexed}
