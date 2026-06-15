"""Aperture — author a manual, curated compaction event IN PLACE.

Claude Code's auto-compaction appends two records to the live transcript: a
`compact_boundary` (parentUuid:null — the wall) and an `isCompactSummary`
record, and lists the turns kept raw in `compactMetadata.preservedMessages.uuids`.
On the next `claude --resume <sid>` CC loads: summary + those preserved uuids +
everything after the wall. The session id never changes.

Aperture lets the *user* be the compactor: pick exactly the turns to keep in
focus, and fable appends the same two records to the SAME <sid>.jsonl. Nothing
is deleted; the pre-curation state is sealed as a new sacred vault version first,
so the full lineage is intact and the wall (which only references turns by uuid)
can be lifted again.

Schema fidelity by example: when the session already carries a real
compact_boundary / summary, we deep-copy it as the template and override only
uuid/timestamp/chain/compactMetadata — so the authored wall matches whatever
Claude Code's current format is, field for field.
"""
import copy
import json
import os
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path

from fable.jsonl import iter_records

_CTX_KEYS = ("userType", "cwd", "sessionId", "version",
             "gitBranch", "slug", "entrypoint")

# record types that aren't user-facing turns — hidden from the timeline by
# default but never touched on disk
_HIDE_TYPES = {"file-history-snapshot"}

DEFAULT_SUMMARY = ("[Manually curated context — only the turns selected below "
                   "were kept in focus. Earlier turns remain in the fable vault.]")


def _now_iso():
    return (datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _tool_ids(obj):
    """Every tool id this record references (as a tool_use or a tool_result)."""
    ids = []
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("id"):
                ids.append(b["id"])
            elif b.get("type") == "tool_result" and b.get("tool_use_id"):
                ids.append(b["tool_use_id"])
    return ids


def _preview(obj):
    """role, a human preview, and tool-pairing flags for one record."""
    msg = obj.get("message") or {}
    role = msg.get("role") or obj.get("type") or "?"
    content = msg.get("content")
    has_use = has_result = False
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                parts.append(b.get("text", ""))
            elif t == "thinking":
                parts.append("💭 " + (b.get("thinking", "")[:240]))
            elif t == "tool_use":
                has_use = True
                parts.append(f"⚙ {b.get('name', 'tool')}")
            elif t == "tool_result":
                has_result = True
                parts.append("↩ tool result")
        text = "\n".join(p for p in parts if p)
    else:
        text = ""
    return role, text.strip(), has_use, has_result


def _thread_map(db_path, uuids):
    """uuid -> prompt_id (the owning thread) from the index — the same thread
    identity Surgery groups by, so one canvas can serve both modes."""
    from fable import db as fdb
    out = {}
    conn = fdb.connect(db_path)
    try:
        uu = [u for u in uuids if u]
        for i in range(0, len(uu), 800):
            part = uu[i:i + 800]
            q = ("SELECT uuid, prompt_id FROM records WHERE uuid IN (%s)"
                 % ",".join("?" * len(part)))
            for u, pid in conn.execute(q, part).fetchall():
                out[u] = pid
    finally:
        conn.close()
    return out


def timeline(live_path, db_path=None):
    """Chain-ordered turns for the session's live transcript (read-only).

    With db_path, each turn is tagged with its thread (`prompt_id`) so the
    Context Editor can group the spine into threads for Surgery mode."""
    rows = []
    for rec in iter_records(str(live_path)):
        obj = rec.obj
        typ = obj.get("type")
        if typ in _HIDE_TYPES:
            continue
        uid = obj.get("uuid")
        if not uid:
            continue
        role, text, has_use, has_result = _preview(obj)
        is_wall = obj.get("subtype") == "compact_boundary"
        is_summary = bool(obj.get("isCompactSummary"))
        if not text and not (is_wall or is_summary):
            continue  # nothing to show (empty/meta record) — keep file intact
        # role is NOT the lane: a tool_result is role:user (it ran on the
        # machine) and a tool_use lives in an assistant record. Only genuine
        # human prompts go to the user lane; model + machine output go left.
        if has_result:
            kind = "tool"
        elif role == "user" and not has_use:
            kind = "user"
        else:
            kind = "assistant"
        rows.append({
            "uuid": uid,
            "role": role,
            "kind": kind,
            "type": typ,
            "is_wall": is_wall,
            "is_summary": is_summary,
            "is_sidechain": bool(obj.get("isSidechain")),
            "ts": obj.get("timestamp"),
            "preview": text[:320],
            # keep walls/summaries whole so the compaction digest is readable;
            # cap ordinary turns to keep the timeline payload light
            "full": text if (is_wall or is_summary) else text[:8000],
            "tokens": len(json.dumps(obj, ensure_ascii=False)) // 4,
            "has_tool_use": has_use,
            "has_tool_result": has_result,
            "tool_ids": _tool_ids(obj),
            "thread": None,
        })
    if db_path:
        tmap = _thread_map(db_path, [r["uuid"] for r in rows])
        for r in rows:
            r["thread"] = tmap.get(r["uuid"])
    return rows


def complete_tool_pairs(focus, msgs):
    """Expand a focus set so every tool_use/tool_result keeps its partner —
    an orphan tool_result makes Claude Code reject the loaded context."""
    by_tool = {}
    for m in msgs:
        uid = m.get("uuid")
        if not uid:
            continue
        for tid in _tool_ids(m):
            by_tool.setdefault(tid, set()).add(uid)
    out = set(focus)
    for m in msgs:
        if m.get("uuid") in focus:
            for tid in _tool_ids(m):
                out |= by_tool.get(tid, set())
    return out


def _read(live_path):
    msgs = []
    for rec in iter_records(str(live_path)):
        msgs.append(rec.obj)
    return msgs


def plan(live_path, focus_uuids):
    """Pure preview: what the authored wall would preserve (no write)."""
    msgs = _read(live_path)
    present = {m.get("uuid") for m in msgs if m.get("uuid")}
    focus = complete_tool_pairs(set(u for u in focus_uuids if u in present), msgs)
    ordered = [m["uuid"] for m in msgs if m.get("uuid") in focus]
    auto = [u for u in ordered if u not in set(focus_uuids)]
    post_tokens = sum(len(json.dumps(m, ensure_ascii=False)) // 4
                      for m in msgs if m.get("uuid") in focus)
    pre_tokens = sum(len(json.dumps(m, ensure_ascii=False)) // 4 for m in msgs)
    return {"focus_count": len(ordered), "auto_included": auto,
            "post_tokens": post_tokens, "pre_tokens": pre_tokens,
            "masked_tokens": max(0, pre_tokens - post_tokens)}


def apply(live_path, focus_uuids, summary_text="", db_path=None,
          backup_dir=None, project=None):
    """Seal the current state as a sacred vault version, then APPEND a manual
    compact_boundary + summary to the SAME live transcript. Returns the resume
    instruction. The live file is only ever appended to — never rewritten."""
    live = Path(live_path)
    if not live.exists():
        raise FileNotFoundError(live)
    if backup_dir is None:
        raise ValueError("backup_dir is required — the pre-curation state must "
                         "be sealed into the vault first")

    msgs = _read(live)
    present = {m.get("uuid") for m in msgs if m.get("uuid")}
    focus = complete_tool_pairs(
        set(u for u in focus_uuids if u in present), msgs)
    ordered = [m["uuid"] for m in msgs if m.get("uuid") in focus]
    if not ordered:
        raise ValueError("focus set is empty — select at least one turn")

    last_uuid = next((m["uuid"] for m in reversed(msgs) if m.get("uuid")), None)

    # 1. seal current state — a new SACRED version, never an overwrite
    from fable.prune import backup as vault_backup
    version, sealed = vault_backup(live, Path(backup_dir))

    # 2. schema fidelity by example
    boundary_tmpl = next(
        (m for m in msgs if m.get("subtype") == "compact_boundary"), None)
    summary_tmpl = next(
        (m for m in msgs if m.get("isCompactSummary")), None)
    ctx_src = boundary_tmpl or next(
        (m for m in reversed(msgs) if m.get("sessionId")), {})
    ctx = {k: ctx_src[k] for k in _CTX_KEYS if ctx_src.get(k) is not None}

    wall_uuid = str(_uuid.uuid4())
    anchor_uuid = str(_uuid.uuid4())
    ts = _now_iso()
    pre_tokens = sum(len(json.dumps(m, ensure_ascii=False)) // 4 for m in msgs)
    post_tokens = sum(len(json.dumps(m, ensure_ascii=False)) // 4
                      for m in msgs if m.get("uuid") in focus)

    if boundary_tmpl:
        wall = copy.deepcopy(boundary_tmpl)
    else:
        wall = {"type": "system", "subtype": "compact_boundary",
                "isSidechain": False, "content": "", "isMeta": True,
                "level": "info"}
        wall.update(ctx)
    wall["parentUuid"] = None
    wall["logicalParentUuid"] = last_uuid
    wall["uuid"] = wall_uuid
    wall["timestamp"] = ts
    wall["compactMetadata"] = {
        "trigger": "manual",
        "fableCurated": True,
        "preTokens": pre_tokens,
        "postTokens": post_tokens,
        "durationMs": 0,
        "preservedSegment": {"headUuid": ordered[0],
                             "anchorUuid": anchor_uuid,
                             "tailUuid": ordered[-1]},
        "preservedMessages": {"anchorUuid": anchor_uuid,
                              "uuids": ordered, "allUuids": ordered},
    }

    if summary_tmpl:
        summary = copy.deepcopy(summary_tmpl)
    else:
        summary = {"type": "user", "isSidechain": False,
                   "isCompactSummary": True, "isVisibleInTranscriptOnly": True}
        summary.update(ctx)
    summary["parentUuid"] = wall_uuid
    summary["uuid"] = anchor_uuid
    summary["timestamp"] = ts
    summary["message"] = {"role": "user",
                          "content": summary_text.strip() or DEFAULT_SUMMARY}
    summary.pop("requestId", None)
    summary.pop("promptId", None)

    # 3. APPEND in place — the only mutation, and it's additive
    with open(live, "a", encoding="utf-8") as f:
        f.write(json.dumps(wall, ensure_ascii=False) + "\n")
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")

    # 4. index the sealed version + re-index the now-longer live file
    if db_path:
        from fable.extract import fts_extract_fn
        from fable.indexer import index_vault
        index_vault(db_path, [str(sealed)], live_file=str(live),
                    extract_fn=fts_extract_fn)
        from fable.db import log_op
        log_op(db_path, "curate", file=str(live), version=version,
               focus=len(ordered), post_tokens=post_tokens)

    sid = ctx.get("sessionId") or live.stem
    return {"ok": True, "version": version, "sealed": str(sealed),
            "focus_count": len(ordered), "pre_tokens": pre_tokens,
            "post_tokens": post_tokens, "wall_uuid": wall_uuid,
            "summary_uuid": anchor_uuid,
            "resume": f"claude --resume {sid}"}
