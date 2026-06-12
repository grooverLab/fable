"""Pruner v2 — eviction half of the fable memory lifecycle.

Port of prune_transcript_v1.py with three additions:
  --strip-images   replace base64 image payloads (the bloat v1 missed)
  inception strip  <historical_context> spans -> <consulted_arcs> stubs
  evict gate       --replace refuses to rewrite the live file unless every
                   record is already indexed at full fidelity (--force skips)

Modes: resume (keep compaction walls), extract (remove walls, stitch chain),
handoff (write SESSION-HANDOFF.md, read-only).
"""
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from fable.extract import replace_historical
from fable.jsonl import iter_records

DROP_TYPES = {"progress", "file-history-snapshot", "queue-operation",
              "last-prompt", "ai-title"}
DROP_SUBTYPES = {"turn_duration", "stop_hook_summary", "api_error"}
METADATA_TYPES = {"custom-title", "agent-name", "mode", "permission-mode"}
KEEP_INPUT_FIELDS = {"file_path", "path", "command", "description",
                     "pattern", "query", "url", "prompt"}
TOOL_STUB_INPUTS = {
    "Edit": {"file_path": "/pruned", "old_string": "", "new_string": ""},
    "Write": {"file_path": "/pruned", "content": ""},
    "Read": {"file_path": "/pruned"},
    "Bash": {"command": "# pruned", "description": "pruned"},
    "Grep": {"pattern": "pruned"},
    "Glob": {"pattern": "pruned"},
    "Agent": {"prompt": "pruned", "description": "pruned"},
    "WebSearch": {"query": "pruned"},
    "WebFetch": {"url": "https://pruned"},
}
COMMAND_TRUNCATE = 200
PROMPT_TRUNCATE = 200


class PruneGateError(RuntimeError):
    pass


# ── backup ───────────────────────────────────────────────

def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


_VER_RE = re.compile(r"^v(\d+)-")


def backup(input_path: Path, backup_dir: Path):
    """Versioned, never-overwriting backup.

    Version = max(existing)+1 (a count would re-use gaps and clobber an
    existing generation); the copy lands in a temp file and moves into
    place with an exclusive create, so concurrent prunes cannot collide.
    """
    session_dir = backup_dir / input_path.stem
    session_dir.mkdir(parents=True, exist_ok=True)
    versions = [int(m.group(1)) for f in session_dir.glob("v*.jsonl")
                if (m := _VER_RE.match(f.name))]
    while True:
        if not versions:
            version, dest = 0, session_dir / "v0-raw.jsonl"
        else:
            version = max(versions) + 1
            dest = session_dir / f"v{version}-pruned.jsonl"
        tmp = session_dir / f".{dest.name}.tmp"
        shutil.copy2(input_path, tmp)
        try:
            fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            os.unlink(tmp)
            versions.append(version)
            continue
        os.close(fd)
        os.replace(tmp, dest)
        break
    if _md5(input_path) != _md5(dest):
        raise RuntimeError(f"backup verification failed for {dest}")
    return version, dest


# ── read + classify ──────────────────────────────────────

def read_transcript(path):
    messages, uuid_chain, metadata = [], {}, {}
    parse_errors = []
    for rec in iter_records(str(path),
                            on_error=lambda ln, e: parse_errors.append(ln)):
        obj = rec.obj
        messages.append(obj)
        if obj.get("uuid"):
            uuid_chain[obj["uuid"]] = obj.get("parentUuid")
        if obj.get("type") in METADATA_TYPES:
            metadata[obj["type"]] = obj
    return {"messages": messages, "uuid_chain": uuid_chain,
            "metadata": metadata, "parse_errors": parse_errors}


# ── drop noise ───────────────────────────────────────────

def _is_synthetic(msg):
    raw = msg.get("message", {})
    return isinstance(raw, dict) and raw.get("model") == "<synthetic>"


def _is_exit_lifecycle(msg):
    raw = msg.get("message", {})
    if not isinstance(raw, dict) or raw.get("role") != "user":
        return False
    content = raw.get("content", "")
    if isinstance(content, str):
        return (content.startswith("<local-command-caveat>")
                or "<command-name>/exit</command-name>" in content
                or content.startswith("<local-command-stdout>"))
    return False


def drop_noise(messages):
    kept, dropped = [], 0
    for msg in messages:
        if (msg.get("type") in DROP_TYPES
                or (msg.get("type") == "system"
                    and msg.get("subtype") in DROP_SUBTYPES)
                or msg.get("type") in METADATA_TYPES
                or _is_synthetic(msg)
                or _is_exit_lifecycle(msg)):
            dropped += 1
            continue
        kept.append(msg)
    return kept, dropped


# ── compaction ───────────────────────────────────────────

def handle_compaction_extract(messages):
    remove, bridge_map = set(), {}
    for i, msg in enumerate(messages):
        if (msg.get("type") == "system"
                and msg.get("subtype") == "compact_boundary"):
            remove.add(i)
        if msg.get("isCompactSummary"):
            remove.add(i)
    for i, msg in enumerate(messages):
        if i in remove and msg.get("subtype") == "compact_boundary":
            logical = msg.get("logicalParentUuid")
            if not logical:
                continue
            for j in range(i + 1, len(messages)):
                if j not in remove and messages[j].get("uuid"):
                    bridge_map[messages[j]["uuid"]] = logical
                    break
    kept = [m for i, m in enumerate(messages) if i not in remove]
    return kept, bridge_map


# ── content pruning ──────────────────────────────────────

def prune_tool_input(name, original):
    stub = dict(TOOL_STUB_INPUTS.get(name, {"description": "pruned"}))
    if not isinstance(original, dict):
        return stub
    for key in KEEP_INPUT_FIELDS:
        if key in original:
            val = original[key]
            if key == "command" and isinstance(val, str):
                val = val[:COMMAND_TRUNCATE]
            elif key == "prompt" and isinstance(val, str):
                val = val[:PROMPT_TRUNCATE]
            stub[key] = val
    return stub


def _strip_text(text, citations):
    cleaned, refs = replace_historical(text)
    citations.extend(refs)
    return cleaned


def prune_content_blocks(content, citations, strip_images=False):
    if isinstance(content, str):
        return _strip_text(content, citations)
    if not isinstance(content, list):
        return content
    pruned = []
    for block in content:
        if not isinstance(block, dict):
            pruned.append(block)
            continue
        btype = block.get("type", "")
        if btype == "tool_use":
            name = block.get("name", "unknown")
            pruned.append({"type": "tool_use", "id": block.get("id", ""),
                           "name": name,
                           "input": prune_tool_input(name, block.get("input"))})
        elif btype == "tool_result":
            pruned.append({"type": "tool_result",
                           "tool_use_id": block.get("tool_use_id", ""),
                           "content": "[pruned]"})
        elif btype == "thinking":
            if "signature" in block:
                pruned.append(block)  # signed seals must survive verbatim
            elif block.get("thinking"):
                pruned.append({"type": "thinking",
                               "thinking": block["thinking"]})
        elif btype == "image" and strip_images:
            src = block.get("source") or {}
            size = len(src.get("data") or "") if isinstance(src, dict) else 0
            pruned.append({"type": "text",
                           "text": f"[image stripped by fable prune: "
                                   f"{size} bytes]"})
        elif btype == "text":
            pruned.append({"type": "text",
                           "text": _strip_text(block.get("text", ""),
                                               citations)})
        else:
            pruned.append(block)
    return pruned


def prune_message(msg, citations, strip_images=False):
    raw = msg.get("message", {})
    if isinstance(raw, dict) and "content" in raw:
        raw["content"] = prune_content_blocks(raw["content"], citations,
                                              strip_images)
    if isinstance(raw, dict):
        raw.pop("toolUseResult", None)
    msg.pop("toolUseResult", None)


# ── chain rebuild + validate ─────────────────────────────

def rebuild_chain(messages, full_chain, bridge_map=None):
    kept = {m["uuid"] for m in messages if m.get("uuid")}
    reparented = 0
    for msg in messages:
        puid = msg.get("parentUuid")
        if puid is None or puid in kept:
            continue
        uid = msg.get("uuid")
        if bridge_map and uid in bridge_map and bridge_map[uid] in kept:
            msg["parentUuid"] = bridge_map[uid]
            reparented += 1
            continue
        cur, visited = puid, set()
        while cur and cur not in visited:
            if cur in kept:
                msg["parentUuid"] = cur
                break
            visited.add(cur)
            cur = full_chain.get(cur)
        else:
            msg["parentUuid"] = None
        reparented += 1
    return reparented


def validate(messages):
    kept = {m["uuid"] for m in messages if m.get("uuid")}
    tool_ids, result_refs, broken = set(), [], []
    for msg in messages:
        puid = msg.get("parentUuid")
        if puid is not None and puid not in kept:
            broken.append(msg.get("uuid"))
        raw = msg.get("message", {})
        content = raw.get("content") if isinstance(raw, dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "tool_use":
                        tool_ids.add(b.get("id"))
                    elif b.get("type") == "tool_result":
                        result_refs.append(b.get("tool_use_id"))
    orphans = [r for r in result_refs if r and r not in tool_ids]
    return {"chain_valid": not broken, "chain_broken": broken,
            "tool_orphans": len(orphans)}


# ── evict gate ───────────────────────────────────────────

def check_evict_gate(input_path, db_path, force=False):
    """Every record about to be evicted must be recoverable from an
    IMMUTABLE indexed file. 'Indexed in the live file' is worthless — the
    rewrite is about to invalidate those very offsets."""
    if force:
        return {"checked": False, "forced": True}
    if not db_path or not os.path.exists(db_path):
        raise PruneGateError(
            "--replace rewrites the live transcript; without an index "
            "(--db) fable cannot prove the history is preserved. Index "
            "first (fable index/discover) or pass --force.")
    from fable import db as fdb
    conn = fdb.connect(db_path)
    try:
        violations = []
        for rec in iter_records(str(input_path)):
            uuid = rec.obj.get("uuid")
            if not uuid:
                continue
            row = conn.execute(
                "SELECT 1 FROM copies c JOIN files f ON f.id = c.file_id "
                "WHERE c.uuid = ? AND f.immutable = 1 AND c.length >= ? "
                "LIMIT 1", (uuid, rec.length)).fetchone()
            if row is None:
                violations.append(uuid)
    finally:
        conn.close()
    if violations:
        raise PruneGateError(
            f"evict gate: {len(violations)} record(s) have no immutable "
            f"full-fidelity copy in the index (e.g. {violations[:3]}). "
            f"The backup should have been indexed first — or pass --force.")
    return {"checked": True, "violations": 0}


def _atomic_write(out_messages, final: Path, live_path: Path,
                  snapshot_size: int):
    """Crash-safe replace: temp + fsync + rename, preserving any bytes the
    session appended after our snapshot (verbatim tail copy)."""
    tmp = final.with_name(final.name + ".fable-tmp")
    chars = 0
    with open(tmp, "wb") as f:
        for msg in out_messages:
            # default ensure_ascii escapes even lone surrogates (\udcXX)
            # from invalid-UTF-8 input, so nothing here can fail to encode
            line = json.dumps(msg, separators=(",", ":")).encode("ascii")
            chars += len(line)
            f.write(line + b"\n")
        if final == live_path:
            current = os.path.getsize(live_path)
            if current > snapshot_size:
                with open(live_path, "rb") as src:
                    src.seek(snapshot_size)
                    f.write(src.read(current - snapshot_size))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    return chars


# ── handoff ──────────────────────────────────────────────

def generate_handoff(data, output_path):
    messages = data["messages"]
    title = data["metadata"].get("custom-title", {}).get("customTitle", "?")
    file_paths = set()
    user_texts, asst_texts = [], []
    for msg in messages:
        raw = msg.get("message", {})
        if not isinstance(raw, dict):
            continue
        content = raw.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    text += b.get("text", "")
                elif b.get("type") == "tool_use":
                    inp = b.get("input", {})
                    if isinstance(inp, dict):
                        for k in ("file_path", "path"):
                            if isinstance(inp.get(k), str):
                                file_paths.add(inp[k])
        text = text.strip()
        if len(text) >= 20:
            (user_texts if raw.get("role") == "user" else asst_texts).append(text)
    lines = [f"# Session Handoff — {title}", "",
             f"**Messages:** {len(messages)}", ""]
    if file_paths:
        lines += ["## Files Touched", ""]
        lines += [f"- `{p}`" for p in sorted(file_paths)] + [""]
    lines += ["## Last User Messages", ""]
    lines += [f"> {t[:300]}\n" for t in user_texts[-5:]]
    lines += ["## Last Assistant Messages", ""]
    lines += [f"{t[:500]}\n---\n" for t in asst_texts[-5:]]
    Path(output_path).write_text("\n".join(lines))


def preview(live_path: str) -> dict:
    """Itemize where pruning would save bytes — read-only, no writes."""
    out = {"total_bytes": os.path.getsize(live_path), "messages": 0,
           "tool_result_bytes": 0, "image_bytes": 0, "noise_records": 0,
           "noise_bytes": 0, "tool_use_input_bytes": 0,
           "tool_use_result_field_bytes": 0, "signed_thinking_bytes": 0}
    for rec in iter_records(str(live_path)):
        obj = rec.obj
        out["messages"] += 1
        if (obj.get("type") in DROP_TYPES
                or (obj.get("type") == "system"
                    and obj.get("subtype") in DROP_SUBTYPES)
                or _is_synthetic(obj) or _is_exit_lifecycle(obj)):
            out["noise_records"] += 1
            out["noise_bytes"] += rec.length
            continue
        if obj.get("toolUseResult") is not None:
            out["tool_use_result_field_bytes"] += len(
                json.dumps(obj["toolUseResult"]))
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_result":
                out["tool_result_bytes"] += len(json.dumps(block)) - 30
            elif kind == "image":
                src = block.get("source")
                if isinstance(src, dict):
                    out["image_bytes"] += len(src.get("data") or "")
            elif kind == "tool_use":
                inp = block.get("input")
                if isinstance(inp, dict):
                    kept = sum(len(str(v)) for k, v in inp.items()
                               if k in KEEP_INPUT_FIELDS)
                    out["tool_use_input_bytes"] += max(
                        0, len(json.dumps(inp)) - kept)
            elif kind == "thinking" and "signature" in block:
                out["signed_thinking_bytes"] += len(json.dumps(block))
    out["est_savings_bytes"] = (out["tool_result_bytes"]
                                + out["noise_bytes"]
                                + out["tool_use_input_bytes"]
                                + out["tool_use_result_field_bytes"])
    out["est_savings_with_images_bytes"] = (out["est_savings_bytes"]
                                            + out["image_bytes"])
    out["est_tokens_saved"] = out["est_savings_bytes"] // 4
    out["est_tokens_saved_with_images"] = (
        out["est_savings_with_images_bytes"] // 4)
    return out


# ── orchestrator ─────────────────────────────────────────

def prune_file(input_path, mode, backup_dir=None, output=None, replace=False,
               dry_run=False, strip_images=False, db_path=None, force=False):
    """Transactional prune protocol (for --replace):
    snapshot -> backup (mandatory) -> index the backup -> gate against
    immutable copies -> atomic rewrite preserving any post-snapshot tail.
    A crash at any point leaves the live file either untouched or fully
    replaced, and the full-fidelity history always exists in the vault.
    """
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    snapshot_size = os.path.getsize(input_path)
    data = read_transcript(input_path)

    if mode == "handoff":
        out = output or input_path.parent / "SESSION-HANDOFF.md"
        generate_handoff(data, out)
        return {"mode": mode, "output": str(out)}

    if replace and not backup_dir and not force:
        raise PruneGateError(
            "--replace without --backup-dir would leave no recoverable copy "
            "of the evicted content. Add --backup-dir (or --force if you "
            "truly want this).")

    version, backup_path = None, None
    if backup_dir and not dry_run:
        version, backup_path = backup(input_path, Path(backup_dir))

    if replace and not dry_run:
        # make the just-written backup the gate's immutable evidence
        if db_path and backup_path:
            from fable.extract import fts_extract_fn
            from fable.indexer import index_vault
            index_vault(db_path, [str(backup_path)],
                        extract_fn=fts_extract_fn, rebuild=False)
        check_evict_gate(input_path, db_path, force=force)

    messages, noise_dropped = drop_noise(data["messages"])

    bridge_map = None
    if mode == "extract":
        messages, bridge_map = handle_compaction_extract(messages)

    citations = []
    for msg in messages:
        prune_message(msg, citations, strip_images=strip_images)

    reparented = rebuild_chain(messages, data["uuid_chain"], bridge_map)

    meta_entries = list(data["metadata"].values())
    out_messages = meta_entries + messages

    report = validate(out_messages)
    report.update({"mode": mode, "backup_version": version,
                   "noise_dropped": noise_dropped, "reparented": reparented,
                   "messages": len(out_messages),
                   "citations": sorted(set(citations)),
                   "parse_errors": len(data["parse_errors"])})

    if replace:
        final = input_path
    elif output:
        final = Path(output)
    else:
        final = input_path.with_suffix(input_path.suffix + ".pruned")

    if replace and not report["chain_valid"] and not force:
        raise PruneGateError(
            f"pruned output has a broken parent chain "
            f"({len(report['chain_broken'])} orphans) — refusing to replace "
            f"the live transcript with it. Inspect with --output, or --force.")

    if not dry_run:
        report["chars"] = _atomic_write(out_messages, final, input_path,
                                        snapshot_size)
        report["output"] = str(final)
        if version is not None:
            report["backup"] = str(backup_path)
    if db_path and not dry_run:
        from fable.db import log_op
        log_op(db_path, "prune", file=str(input_path), mode=mode,
               replace=replace, chars=report.get("chars"))
    return report


def cmd_prune(args):
    """CLI dispatch for `fable prune ...`."""
    mtime = os.stat(args.input).st_mtime
    if time.time() - mtime < 60:
        print(f"WARNING: {args.input} modified {int(time.time() - mtime)}s "
              "ago — session may be active.")
    try:
        report = prune_file(args.input, args.mode,
                            backup_dir=args.backup_dir, output=args.output,
                            replace=args.replace, dry_run=args.dry_run,
                            strip_images=args.strip_images,
                            db_path=args.db if os.path.exists(args.db) else None,
                            force=args.force)
    except PruneGateError as e:
        print(f"BLOCKED: {e}")
        return 3
    print(json.dumps(report, indent=2))
    return 0 if report.get("chain_valid", True) else 1
