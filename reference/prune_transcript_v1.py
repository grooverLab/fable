#!/usr/bin/env python3
"""
prune_transcript_v1.py — Claude Code JSONL transcript pruner

Three modes:
  resume   — strip bloat, keep compaction walls, for Claude Code session resume
  extract  — strip bloat, remove compaction walls, stitch chain, for knowledge extraction
  handoff  — generate SESSION-HANDOFF.md from transcript for new session

Usage:
  python3 prune_transcript_v1.py input.jsonl --mode resume --backup-dir ./backups
  python3 prune_transcript_v1.py input.jsonl --mode extract --backup-dir ./backups -o out.jsonl
  python3 prune_transcript_v1.py input.jsonl --mode handoff -o SESSION-HANDOFF.md
  python3 prune_transcript_v1.py input.jsonl --mode resume --replace --backup-dir ./backups
  python3 prune_transcript_v1.py input.jsonl --mode resume --dry-run
"""

import json
import hashlib
import shutil
import argparse
import time
from pathlib import Path


# ──────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────

# Types to drop entirely (no UUID chain participation)
DROP_TYPES = {
    "progress",
    "file-history-snapshot",
    "queue-operation",
    "last-prompt",
    "ai-title",
}

# System subtypes to drop
DROP_SUBTYPES = {
    "turn_duration",
    "stop_hook_summary",
    "api_error",
}

# Metadata types — keep last occurrence only, prepended at output
METADATA_TYPES = {"custom-title", "agent-name", "mode", "permission-mode"}

# Tool input fields to preserve (everything else stripped)
KEEP_INPUT_FIELDS = {
    "file_path", "path",
    "command",
    "description",
    "pattern",
    "query",
    "url",
    "prompt",
}

# Fallback input stubs per tool — fields the renderer expects
_TOOL_STUB_INPUTS = {
    "Edit":      {"file_path": "/pruned", "old_string": "", "new_string": ""},
    "Write":     {"file_path": "/pruned", "content": ""},
    "Read":      {"file_path": "/pruned"},
    "Bash":      {"command": "# pruned", "description": "pruned"},
    "Grep":      {"pattern": "pruned"},
    "Glob":      {"pattern": "pruned"},
    "Agent":     {"prompt": "pruned", "description": "pruned"},
    "WebSearch": {"query": "pruned"},
    "WebFetch":  {"url": "https://pruned"},
}

COMMAND_TRUNCATE = 200
PROMPT_TRUNCATE = 200

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


# ──────────────────────────────────────────────
# 0. SESSION DISCOVERY
# ──────────────────────────────────────────────

def find_project_dir(project_name):
    """Find the Claude projects directory matching a project name.

    Matches against the last segment of the encoded path.
    e.g. 'code-graph' matches '-Users-anoopgrover-Desktop-...-code-graph'
    """
    if not CLAUDE_PROJECTS_DIR.exists():
        return None

    matches = []
    for d in CLAUDE_PROJECTS_DIR.iterdir():
        if not d.is_dir():
            continue
        # Match against last path segment (the project folder name)
        if d.name.endswith(f"-{project_name}") or d.name == project_name:
            matches.append(d)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Multiple projects match '{project_name}':")
        for i, m in enumerate(matches):
            print(f"  {i + 1}. {m.name}")
        choice = input("Select [1-{}]: ".format(len(matches)))
        try:
            return matches[int(choice) - 1]
        except (ValueError, IndexError):
            print("Invalid selection.")
            return None
    return None


def get_session_title(jsonl_path):
    """Read custom-title from a JSONL file (first 200 lines only for speed)."""
    title = None
    try:
        with open(jsonl_path) as f:
            for i, line in enumerate(f):
                if i > 200:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("type") == "custom-title":
                        title = obj.get("customTitle", "")
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return title


def list_sessions(project_dir):
    """List all JSONL session files in a project directory with metadata."""
    sessions = []
    for f in sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = f.stat()
        title = get_session_title(f)
        sessions.append({
            "path": f,
            "session_id": f.stem,
            "title": title or "untitled",
            "size_mb": stat.st_size / (1024 * 1024),
            "mtime": stat.st_mtime,
        })
    return sessions


def resolve_session(project_name=None, session_id=None, input_path=None):
    """Resolve a session JSONL file from project name, session ID, or direct path.

    Priority: input_path > session_id > project_name (interactive picker)
    Returns: Path to JSONL file, or None.
    """
    # Direct path — use as-is
    if input_path:
        p = Path(input_path)
        if p.exists():
            return p
        print(f"ERROR: File not found: {p}")
        return None

    # Session ID — search all project dirs
    if session_id:
        # Support short IDs (first 8 chars)
        for d in CLAUDE_PROJECTS_DIR.iterdir():
            if not d.is_dir():
                continue
            for f in d.glob("*.jsonl"):
                if f.stem == session_id or f.stem.startswith(session_id):
                    return f
        print(f"ERROR: No session found matching '{session_id}'")
        return None

    # Project name — find dir, list sessions, let user pick
    if project_name:
        project_dir = find_project_dir(project_name)
        if not project_dir:
            print(f"ERROR: No project found matching '{project_name}'")
            return None

        sessions = list_sessions(project_dir)
        if not sessions:
            print(f"No sessions found in {project_dir}")
            return None

        if len(sessions) == 1:
            s = sessions[0]
            print(f"Found 1 session: {s['title']} ({s['size_mb']:.1f}MB)")
            return s["path"]

        print(f"\nSessions in '{project_name}':")
        print(f"{'#':>3}  {'Title':<35} {'Size':>8}  {'Session ID'}")
        print(f"{'─'*3}  {'─'*35} {'─'*8}  {'─'*36}")
        for i, s in enumerate(sessions):
            print(f"{i+1:>3}  {s['title']:<35} {s['size_mb']:>6.1f}MB  {s['session_id'][:12]}...")
        print()
        choice = input(f"Select session [1-{len(sessions)}]: ")
        try:
            return sessions[int(choice) - 1]["path"]
        except (ValueError, IndexError):
            print("Invalid selection.")
            return None

    return None


def list_all_projects():
    """List all Claude Code projects with session counts."""
    if not CLAUDE_PROJECTS_DIR.exists():
        print(f"No projects directory found at {CLAUDE_PROJECTS_DIR}")
        return

    print(f"\nClaude Code projects (use last segment for --project):")
    print(f"{'Directory':<60} {'#':>5}  {'Size':>10}")
    print(f"{'─'*60} {'─'*5}  {'─'*10}")

    for d in sorted(CLAUDE_PROJECTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        sessions = list(d.glob("*.jsonl"))
        if not sessions:
            continue
        total_size = sum(f.stat().st_size for f in sessions)
        # Show the encoded directory name — it IS the project identifier
        # User passes the last segment to --project for matching
        print(f"{d.name:<60} {len(sessions):>5}  {total_size/(1024*1024):>8.1f}MB")


# ──────────────────────────────────────────────
# 1. BACKUP
# ──────────────────────────────────────────────

def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(input_path, backup_dir):
    """Versioned backup. Returns (version, backup_path)."""
    session_id = input_path.stem
    session_dir = backup_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    raw_backup = session_dir / "v0-raw.jsonl"

    if not raw_backup.exists():
        # First prune — save the original
        shutil.copy2(input_path, raw_backup)
        dest = raw_backup
        version = 0
    else:
        # Subsequent prune — save current state
        existing = sorted(session_dir.glob("v*-pruned.jsonl"))
        next_version = len(existing) + 1
        dest = session_dir / f"v{next_version}-pruned.jsonl"
        shutil.copy2(input_path, dest)
        version = next_version

    # Verify
    src_md5 = md5_file(input_path)
    dst_md5 = md5_file(dest)
    if src_md5 != dst_md5:
        raise RuntimeError(f"Backup verification failed: {src_md5} != {dst_md5}")

    print(f"Backup: v{version} at {dest} (MD5: {src_md5})")
    return version, dest


# ──────────────────────────────────────────────
# 2. READ + CLASSIFY
# ──────────────────────────────────────────────

def _parse_jsonl_line(line):
    """Parse one or more concatenated JSON objects from a single line.

    Handles the case where concurrent writes produce lines like:
        {"type":"foo",...}{"type":"bar",...}
    Returns a list of parsed objects — never drops data.
    """
    decoder = json.JSONDecoder()
    objects = []
    pos = 0
    length = len(line)
    while pos < length:
        while pos < length and line[pos] in ' \t':
            pos += 1
        if pos >= length:
            break
        obj, end = decoder.raw_decode(line, pos)
        objects.append(obj)
        pos = end
    return objects


def read_transcript(input_path):
    """Read entire JSONL, classify records, build chain map."""
    messages = []
    uuid_chain = {}          # uuid → parentUuid (ALL records)
    compaction_indices = []  # indices of compact_boundary records
    summary_indices = []     # indices of isCompactSummary records
    metadata = {}            # type → last occurrence
    type_counts = {}         # type → count
    session_id = None
    original_chars = 0
    total_input_tokens = 0

    with open(input_path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            original_chars += len(line)
            try:
                parsed_objects = _parse_jsonl_line(line)
            except json.JSONDecodeError as e:
                print(f"  ⚠ Skipping malformed line {line_num}: {e}")
                continue
            for obj in parsed_objects:
                idx = len(messages)
                messages.append(obj)

                mtype = obj.get("type", "unknown")
                subtype = obj.get("subtype", "")
                type_counts[mtype] = type_counts.get(mtype, 0) + 1

                # UUID chain
                uid = obj.get("uuid")
                puid = obj.get("parentUuid")
                if uid:
                    uuid_chain[uid] = puid

                # Session ID
                if not session_id:
                    session_id = obj.get("sessionId")

                # Compaction boundaries
                if mtype == "system" and subtype == "compact_boundary":
                    compaction_indices.append(idx)

                # Compaction summaries
                if obj.get("isCompactSummary"):
                    summary_indices.append(idx)

                # Metadata
                if mtype in METADATA_TYPES:
                    metadata[mtype] = obj

                # Token counting from usage fields
                msg_obj = obj.get("message", {})
                if isinstance(msg_obj, dict) and "usage" in msg_obj:
                    usage = msg_obj["usage"]
                    total_input_tokens += (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )

    return {
        "messages": messages,
        "uuid_chain": uuid_chain,
        "compaction_indices": compaction_indices,
        "summary_indices": summary_indices,
        "metadata": metadata,
        "type_counts": type_counts,
        "session_id": session_id,
        "original_chars": original_chars,
        "total_input_tokens": total_input_tokens,
    }


# ──────────────────────────────────────────────
# 3. DROP NOISE
# ──────────────────────────────────────────────

def _is_synthetic(msg):
    """Detect synthetic assistant messages (model='<synthetic>')."""
    raw = msg.get("message", {})
    return isinstance(raw, dict) and raw.get("model") == "<synthetic>"


def _is_exit_lifecycle(msg):
    """Detect /exit command lifecycle user messages."""
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
    """Remove non-semantic record types."""
    kept = []
    dropped = 0
    for msg in messages:
        mtype = msg.get("type", "")
        subtype = msg.get("subtype", "")

        if mtype in DROP_TYPES:
            dropped += 1
            continue
        if mtype == "system" and subtype in DROP_SUBTYPES:
            dropped += 1
            continue
        # Drop all metadata occurrences here — we'll prepend the last one later
        if mtype in METADATA_TYPES:
            dropped += 1
            continue
        if _is_synthetic(msg):
            dropped += 1
            continue
        if _is_exit_lifecycle(msg):
            dropped += 1
            continue
        kept.append(msg)

    return kept, dropped


# ──────────────────────────────────────────────
# 4. HANDLE COMPACTION
# ──────────────────────────────────────────────

def handle_compaction_resume(messages, compaction_indices, summary_indices):
    """Resume mode: keep compaction walls and summaries in place. No-op."""
    return messages, None


def handle_compaction_extract(messages, compaction_indices, summary_indices):
    """Extract mode: remove walls + summaries, build bridge map for chain stitching."""
    # Collect UUIDs and bridge info from compact_boundary records
    remove_uuids = set()
    bridge_map = {}  # first_post_boundary_uuid → logicalParentUuid

    # Find indices to remove
    remove_indices = set(compaction_indices) | set(summary_indices)

    # For each compact_boundary, capture the bridge
    for ci in compaction_indices:
        boundary = messages[ci]
        logical_parent = boundary.get("logicalParentUuid")
        if logical_parent:
            # Find the first non-removed message after this boundary
            for j in range(ci + 1, len(messages)):
                if j not in remove_indices:
                    uid = messages[j].get("uuid")
                    if uid:
                        bridge_map[uid] = logical_parent
                    break

    # Filter out compaction records
    kept = [msg for i, msg in enumerate(messages) if i not in remove_indices]
    removed_count = len(remove_indices)

    return kept, bridge_map


# ──────────────────────────────────────────────
# 5. PRUNE CONTENT BLOCKS
# ──────────────────────────────────────────────

def prune_tool_input(name, original_input):
    """Preserve useful fields from tool input, strip bulk content."""
    if not isinstance(original_input, dict):
        return dict(_TOOL_STUB_INPUTS.get(name, {"description": "pruned"}))

    stub = dict(_TOOL_STUB_INPUTS.get(name, {"description": "pruned"}))

    for key in KEEP_INPUT_FIELDS:
        if key in original_input:
            val = original_input[key]
            if key == "command" and isinstance(val, str):
                val = val[:COMMAND_TRUNCATE]
            elif key == "prompt" and isinstance(val, str):
                val = val[:PROMPT_TRUNCATE]
            stub[key] = val

    return stub


def prune_content_blocks(content):
    """Prune tool_use/tool_result bulk, keep text and thinking."""
    if isinstance(content, str):
        return content
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
            pruned.append({
                "type": "tool_use",
                "id": block.get("id", ""),
                "name": name,
                "input": prune_tool_input(name, block.get("input", {})),
            })
        elif btype == "tool_result":
            pruned.append({
                "type": "tool_result",
                "tool_use_id": block.get("tool_use_id", ""),
                "content": "[pruned]",
            })
        elif btype == "thinking":
            thinking_text = block.get("thinking", "")
            if "signature" in block:
                # Signed thinking blocks MUST be preserved — they are
                # tamper-detection seals the API validates on resume.
                pruned.append(block)
            elif not thinking_text:
                continue
            else:
                pruned.append({"type": "thinking", "thinking": thinking_text})
        else:
            pruned.append(block)

    return pruned


def prune_message(msg):
    """Prune a single message's content and metadata."""
    raw = msg.get("message", {})
    if isinstance(raw, dict):
        if "content" in raw:
            raw["content"] = prune_content_blocks(raw["content"])
        # Strip toolUseResult (redundant with tool_result in content)
        raw.pop("toolUseResult", None)

    # Strip top-level toolUseResult too
    msg.pop("toolUseResult", None)


# ──────────────────────────────────────────────
# 6. REBUILD UUID CHAIN
# ──────────────────────────────────────────────

def rebuild_chain(messages, full_uuid_chain, bridge_map=None):
    """Ensure every kept message has a valid parentUuid."""
    kept_uuids = set()
    for msg in messages:
        uid = msg.get("uuid")
        if uid:
            kept_uuids.add(uid)

    reparented = 0
    for msg in messages:
        puid = msg.get("parentUuid")
        if puid is None:
            continue
        if puid in kept_uuids:
            continue

        uid = msg.get("uuid")

        # Check bridge map first (extract mode — compaction boundaries)
        if bridge_map and uid in bridge_map:
            bridge_target = bridge_map[uid]
            if bridge_target in kept_uuids:
                msg["parentUuid"] = bridge_target
                reparented += 1
                continue

        # Walk up the original chain to find nearest kept ancestor
        visited = set()
        current = puid
        found = False
        while current and current not in visited:
            if current in kept_uuids:
                msg["parentUuid"] = current
                reparented += 1
                found = True
                break
            visited.add(current)
            current = full_uuid_chain.get(current)

        if not found:
            msg["parentUuid"] = None
            reparented += 1

    return reparented


# ──────────────────────────────────────────────
# 7. PRESERVE METADATA
# ──────────────────────────────────────────────

def ensure_metadata(messages, metadata, session_id):
    """Prepend custom-title and agent-name to output."""
    if "custom-title" not in metadata:
        fallback_id = session_id or "unknown"
        metadata["custom-title"] = {
            "type": "custom-title",
            "customTitle": f"PRUNED - {fallback_id}",
            "sessionId": fallback_id,
        }

    entries = list(metadata.values())
    return entries + messages, len(entries)


# ──────────────────────────────────────────────
# 8. VALIDATE
# ──────────────────────────────────────────────

def validate(messages):
    """Check integrity of pruned output."""
    kept_uuids = set()
    tool_use_ids = set()
    tool_result_refs = []

    for msg in messages:
        uid = msg.get("uuid")
        if uid:
            kept_uuids.add(uid)

        # Collect tool_use ids and tool_result references
        raw = msg.get("message", {})
        if isinstance(raw, dict):
            content = raw.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            tool_use_ids.add(block.get("id", ""))
                        elif block.get("type") == "tool_result":
                            tool_result_refs.append(block.get("tool_use_id", ""))

    # UUID chain check
    chain_broken = []
    for msg in messages:
        puid = msg.get("parentUuid")
        if puid is not None and puid not in kept_uuids:
            chain_broken.append(msg.get("uuid", "?"))

    # Tool chain check
    tool_orphans = [ref for ref in tool_result_refs if ref and ref not in tool_use_ids]

    # Token count from usage fields
    token_count = 0
    for msg in messages:
        raw = msg.get("message", {})
        if isinstance(raw, dict) and "usage" in raw:
            usage = raw["usage"]
            token_count += (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
            )

    return {
        "chain_valid": len(chain_broken) == 0,
        "chain_broken": chain_broken,
        "tool_chain_valid": len(tool_orphans) == 0,
        "tool_orphans_count": len(tool_orphans),
        "token_count": token_count,
        "message_count": len(messages),
    }


# ──────────────────────────────────────────────
# 9. WRITE
# ──────────────────────────────────────────────

def write_output(messages, output_path):
    """Write pruned JSONL."""
    total_chars = 0
    with open(output_path, "w") as f:
        for msg in messages:
            line = json.dumps(msg, separators=(",", ":"))
            total_chars += len(line)
            f.write(line + "\n")
    return total_chars


# ──────────────────────────────────────────────
# 10. HANDOFF
# ──────────────────────────────────────────────

def generate_handoff(data, output_path):
    """Generate SESSION-HANDOFF.md from transcript. Read-only, no pruning."""
    messages = data["messages"]
    session_id = data["session_id"]
    metadata = data["metadata"]

    title = metadata.get("custom-title", {}).get("customTitle", session_id)

    # Collect file paths from tool_use blocks
    file_paths = set()
    for msg in messages:
        raw = msg.get("message", {})
        if not isinstance(raw, dict):
            continue
        content = raw.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    inp = block.get("input", {})
                    if isinstance(inp, dict):
                        for key in ("file_path", "path"):
                            if key in inp and isinstance(inp[key], str):
                                file_paths.add(inp[key])

    # Collect compaction summaries
    summaries = []
    for idx in data["summary_indices"]:
        msg = messages[idx]
        raw = msg.get("message", {})
        if isinstance(raw, dict):
            content = raw.get("content", "")
            if isinstance(content, str) and content:
                summaries.append(content[:2000])

    # Collect last N user and assistant text messages
    user_texts = []
    asst_texts = []
    for msg in messages:
        raw = msg.get("message", {})
        if not isinstance(raw, dict):
            continue
        role = raw.get("role", "")
        content = raw.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(parts)
        text = text.strip()
        if not text or len(text) < 20:
            continue
        if role == "user":
            user_texts.append(text)
        elif role == "assistant":
            asst_texts.append(text)

    # Build handoff document
    lines = [
        f"# Session Handoff — {title}",
        f"",
        f"**Session ID:** `{session_id}`",
        f"**Messages:** {len(messages)}",
        f"**Compactions:** {len(data['compaction_indices'])}",
        f"**Generated by:** prune_transcript_v1.py --mode handoff",
        "",
    ]

    if summaries:
        lines.append("## Compaction Summaries")
        lines.append("")
        for i, s in enumerate(summaries):
            lines.append(f"### Summary {i + 1}")
            lines.append("")
            lines.append(s)
            lines.append("")

    if file_paths:
        lines.append("## Files Modified")
        lines.append("")
        for fp in sorted(file_paths):
            lines.append(f"- `{fp}`")
        lines.append("")

    lines.append("## Last User Messages")
    lines.append("")
    for text in user_texts[-5:]:
        lines.append(f"> {text[:300]}")
        lines.append("")

    lines.append("## Last Assistant Messages")
    lines.append("")
    for text in asst_texts[-5:]:
        lines.append(f"{text[:500]}")
        lines.append("---")
        lines.append("")

    output_path = Path(output_path)
    output_path.write_text("\n".join(lines))
    print(f"Handoff written to: {output_path}")
    return output_path


# ──────────────────────────────────────────────
# 11. REPORT
# ──────────────────────────────────────────────

def report(original, pruned, mode, version=None, backup_path=None):
    """Print reduction stats."""
    reduction = (1 - pruned["chars"] / original["chars"]) * 100 if original["chars"] else 0

    print(f"\n{'='*60}")
    print(f"  Mode:         {mode}")
    if version is not None:
        print(f"  Backup:       v{version} at {backup_path}")
    print(f"  Compactions:  {original['compactions']} found", end="")
    if mode == "extract":
        print(f" → removed (chain stitched)")
    elif mode == "resume":
        print(f" → kept in place")
    else:
        print()
    print(f"{'='*60}")
    print(f"  Original:     {original['count']:,} messages, ~{original['tokens']:,} tokens")
    print(f"  Pruned:       {pruned['count']:,} messages, ~{pruned['tokens']:,} tokens")
    print(f"  Reduction:    {reduction:.1f}%")
    print(f"  UUID chain:   {'VALID' if pruned['chain_valid'] else 'BROKEN (' + str(len(pruned['chain_broken'])) + ' orphans)'}")
    print(f"  Tool chain:   {'VALID' if pruned['tool_chain_valid'] else str(pruned['tool_orphans_count']) + ' orphaned tool_results'}")
    print(f"{'='*60}")

    if reduction < 5:
        print()
        print("  WARNING: Only {:.1f}% reduction. Transcript is already lean.".format(reduction))
        print("  Consider starting a new session with --mode handoff.")


# ──────────────────────────────────────────────
# 12. MAIN ORCHESTRATOR
# ──────────────────────────────────────────────

def prune(input_path, mode, backup_dir=None, output_path=None, replace=False, dry_run=False):
    input_path = Path(input_path)

    if not input_path.exists():
        print(f"ERROR: File not found: {input_path}")
        return

    # Active session warning
    mtime = input_path.stat().st_mtime
    if time.time() - mtime < 60:
        print(f"WARNING: File modified {int(time.time() - mtime)}s ago — session may be active.")
        print("         Pruning an active session may lose recent messages.")

    # ── Handoff mode (read-only, no backup) ──
    if mode == "handoff":
        data = read_transcript(input_path)
        out = output_path or input_path.parent / "SESSION-HANDOFF.md"
        generate_handoff(data, out)
        return

    # ── Backup (resume + extract modes) ──
    version, backup_path = None, None
    if backup_dir and not dry_run:
        version, backup_path = backup(input_path, Path(backup_dir))

    # ── Step 1: Read + classify ──
    data = read_transcript(input_path)
    messages = data["messages"]
    original_stats = {
        "count": len(messages),
        "chars": data["original_chars"],
        "tokens": data["total_input_tokens"],
        "compactions": len(data["compaction_indices"]),
    }
    print(f"Read: {original_stats['count']:,} messages, "
          f"~{original_stats['tokens']:,} tokens, "
          f"{original_stats['compactions']} compactions")

    # ── Step 2: Drop noise ──
    messages, noise_dropped = drop_noise(messages)
    print(f"Dropped noise: {noise_dropped} records")

    # ── Step 3: Handle compaction (mode-dependent) ──
    bridge_map = None
    if mode == "resume":
        messages, bridge_map = handle_compaction_resume(
            messages, data["compaction_indices"], data["summary_indices"])
    elif mode == "extract":
        # Recalculate indices after noise drop (they shifted)
        # Simpler: re-identify compaction records in the filtered list
        new_compaction = []
        new_summary = []
        for i, msg in enumerate(messages):
            if msg.get("type") == "system" and msg.get("subtype") == "compact_boundary":
                new_compaction.append(i)
            if msg.get("isCompactSummary"):
                new_summary.append(i)
        messages, bridge_map = handle_compaction_extract(
            messages, new_compaction, new_summary)
        removed = len(new_compaction) + len(new_summary)
        if removed:
            print(f"Removed {removed} compaction records, stitching chain")

    # ── Step 4: Prune content blocks ──
    for msg in messages:
        prune_message(msg)

    # ── Step 5: Rebuild UUID chain ──
    reparented = rebuild_chain(messages, data["uuid_chain"], bridge_map)
    print(f"Re-parented: {reparented} messages")

    # ── Step 6: Prepend metadata ──
    messages, meta_count = ensure_metadata(
        messages, data["metadata"], data["session_id"])

    # ── Step 7: Validate ──
    validation = validate(messages)

    # ── Step 8: Determine output path ──
    if replace:
        final_output = input_path
    elif output_path:
        final_output = Path(output_path)
    else:
        final_output = input_path.with_suffix(input_path.suffix + ".pruned")

    # ── Step 9: Write ──
    if dry_run:
        pruned_chars = sum(len(json.dumps(m, separators=(",", ":"))) for m in messages)
        print(f"\n[DRY RUN] Would write {validation['message_count']} messages to: {final_output}")
    else:
        pruned_chars = write_output(messages, final_output)
        # Verify JSON validity
        errors = 0
        with open(final_output) as f:
            for i, line in enumerate(f):
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    errors += 1
        if errors:
            print(f"WARNING: {errors} JSON parse errors in output!")
        else:
            print(f"Written: {final_output} (JSON valid)")

    # ── Step 10: Report ──
    pruned_stats = {
        "count": validation["message_count"],
        "chars": pruned_chars,
        "tokens": validation["token_count"],
        "chain_valid": validation["chain_valid"],
        "chain_broken": validation["chain_broken"],
        "tool_chain_valid": validation["tool_chain_valid"],
        "tool_orphans_count": validation["tool_orphans_count"],
    }
    report(original_stats, pruned_stats, mode, version, backup_path)


def main():
    parser = argparse.ArgumentParser(
        description="Prune Claude Code JSONL transcripts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
modes:
  resume    Strip tool bloat, keep compaction walls. For Claude Code session resume.
  extract   Strip tool bloat, remove compaction walls, stitch chain. For knowledge extraction.
  handoff   Generate SESSION-HANDOFF.md for starting a new session.

session selection (pick ONE):
  positional arg     Full path to JSONL file
  --project NAME     Project name (interactive session picker)
  --session ID       Session ID or short prefix (searches all projects)
  --list             List all projects and exit

examples:
  %(prog)s --project code-graph --mode extract --backup-dir ./backups
  %(prog)s --session 2057e181 --mode resume --backup-dir ./backups
  %(prog)s session.jsonl --mode resume --replace --backup-dir ./backups
  %(prog)s --list
""")
    parser.add_argument("input", nargs="?", default=None,
                        help="Input JSONL file path (or use --project/--session)")
    parser.add_argument("--project", "-p",
                        help="Project name (last segment of path, e.g. 'code-graph')")
    parser.add_argument("--session", "-s",
                        help="Session ID or short prefix (e.g. '2057e181')")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List all projects and sessions")
    parser.add_argument("--mode", "-m",
                        choices=["resume", "extract", "handoff"],
                        help="Pruning mode")
    parser.add_argument("--backup-dir", "-b",
                        help="Backup directory (required for resume/extract)")
    parser.add_argument("--output", "-o",
                        help="Output file path (default: input.pruned or SESSION-HANDOFF.md)")
    parser.add_argument("--replace", action="store_true",
                        help="Replace input file after backup (resume/extract only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only, no writes")

    args = parser.parse_args()

    # ── List mode ──
    if args.list:
        list_all_projects()
        return

    # ── Resolve session file ──
    input_path = resolve_session(
        project_name=args.project,
        session_id=args.session,
        input_path=args.input,
    )
    if not input_path:
        parser.error("No session file resolved. Use --project, --session, or provide a file path.")

    # ── Mode is required for actual pruning ──
    if not args.mode:
        parser.error("--mode is required (resume, extract, or handoff)")

    # ── Validation ──
    if args.mode in ("resume", "extract"):
        if not args.backup_dir and not args.dry_run:
            parser.error(f"--backup-dir is required for --mode {args.mode} (unless --dry-run)")
    if args.replace and args.output:
        parser.error("--replace and --output are mutually exclusive")
    if args.replace and not args.backup_dir:
        parser.error("--replace requires --backup-dir")

    print(f"Session: {input_path.stem}")
    title = get_session_title(input_path)
    if title:
        print(f"Title:   {title}")
    print()

    prune(str(input_path), args.mode, args.backup_dir, args.output, args.replace, args.dry_run)


if __name__ == "__main__":
    main()
