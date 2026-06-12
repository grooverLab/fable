"""File time-travel — reconstruct any file's edit history from the archive.

Transcripts accidentally version everything: every Edit (old/new strings),
every Write (full content), timestamped across every session. This module
assembles that into a per-file timeline and rebuilds file versions by
replaying the operations in order.

Fidelity note: pruned transcript copies stub Edit inputs; the index always
points at the best surviving copy, so events read from vault generations
keep their full before/after. Events whose inputs were lost are kept in
the timeline but flagged (ok=False) and break reconstruction until the
next full Write.
"""
import difflib
import json
from typing import List, Optional

from fable import db as fdb
from fable.jsonl import read_span

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _fts_candidates(conn, path_query: str) -> List[str]:
    """Narrow 100k+ records to the few that mention the path. FTS tokenizes
    paths on punctuation, so match on the path's distinctive tokens."""
    import re
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", path_query) if len(t) > 1]
    if not tokens:
        return []
    match = " ".join(f'"{t}"' for t in tokens[-4:])  # last segments are most distinctive
    try:
        rows = conn.execute(
            "SELECT uuid FROM fts WHERE fts MATCH ? AND uuid IS NOT NULL",
            (match,)).fetchall()
    except Exception:
        return []
    return [r[0] for r in rows]


def file_events(db_path: str, path_query: str) -> List[dict]:
    """Chronological Edit/Write events touching files matching path_query
    (exact path or suffix match)."""
    conn = fdb.connect(db_path)
    try:
        uuids = _fts_candidates(conn, path_query)
        if not uuids:
            return []
        events = []
        CHUNK = 500
        for i in range(0, len(uuids), CHUNK):
            chunk = uuids[i:i + CHUNK]
            ph = ",".join("?" * len(chunk))
            rows = conn.execute(f"""
                SELECT r.uuid, r.ts, r.ts_epoch, r.prompt_id, r.session_id,
                       f.path, r.offset, r.length
                FROM records r JOIN files f ON f.id = r.file_id
                WHERE r.uuid IN ({ph}) AND r.block_kinds LIKE '%tool_use%'
                """, chunk).fetchall()
            for (uuid, ts, ts_epoch, prompt_id, session_id,
                 fpath, offset, length) in rows:
                try:
                    obj = json.loads(read_span(fpath, offset, length)
                                     .decode("utf-8", "surrogateescape"))
                except (OSError, ValueError):
                    continue
                msg = obj.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not (isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and block.get("name") in EDIT_TOOLS):
                        continue
                    inp = block.get("input")
                    if not isinstance(inp, dict):
                        continue
                    target = inp.get("file_path") or inp.get("path") or ""
                    if not (target == path_query
                            or target.endswith("/" + path_query.lstrip("/"))
                            or target.endswith(path_query)):
                        continue
                    events.append(_event(block["name"], inp, target, uuid,
                                         ts, ts_epoch, prompt_id, session_id))
        events.sort(key=lambda e: (e["ts_epoch"] or 0, e["uuid"]))
        return events
    finally:
        conn.close()


def _event(tool, inp, target, uuid, ts, ts_epoch, prompt_id, session_id):
    ev = {"tool": tool, "file_path": target, "uuid": uuid, "ts": ts,
          "ts_epoch": ts_epoch, "prompt_id": prompt_id,
          "session_id": session_id, "ok": True, "note": ""}
    if tool == "Write":
        body = inp.get("content")
        ev["content"] = body if isinstance(body, str) else None
        ev["ok"] = ev["content"] is not None and ev["content"] != ""
        if not ev["ok"]:
            ev["note"] = "content lost to pruning"
    elif tool == "MultiEdit":
        edits = inp.get("edits")
        ev["edits"] = edits if isinstance(edits, list) else []
        ev["ok"] = bool(ev["edits"]) and all(
            e.get("old_string") for e in ev["edits"])
        if not ev["ok"]:
            ev["note"] = "edit strings lost to pruning"
    else:  # Edit / NotebookEdit
        ev["old"], ev["new"] = inp.get("old_string"), inp.get("new_string")
        ev["replace_all"] = bool(inp.get("replace_all"))
        ev["ok"] = bool(ev.get("old"))
        if not ev["ok"]:
            ev["note"] = "edit strings lost to pruning"
    return ev


def reconstruct(events: List[dict]) -> List[dict]:
    """Replay events into file versions. content=None where the chain is
    broken (lost inputs or failed apply) until the next full Write."""
    versions = []
    current: Optional[str] = None
    for ev in events:
        v = {"uuid": ev["uuid"], "ts": ev["ts"], "tool": ev["tool"],
             "session_id": ev["session_id"], "prompt_id": ev["prompt_id"],
             "ok": True, "note": ev.get("note", "")}
        if ev["tool"] == "Write":
            current = ev.get("content")
            v["ok"] = current is not None
        elif not ev.get("ok") or current is None:
            current = None
            v["ok"] = False
            v["note"] = v["note"] or "chain broken — awaiting next Write"
        elif ev["tool"] == "MultiEdit":
            try:
                for e in ev["edits"]:
                    old = e.get("old_string", "")
                    if old not in current:
                        raise ValueError(old[:40])
                    current = current.replace(
                        old, e.get("new_string", ""),
                        -1 if e.get("replace_all") else 1)
            except ValueError:
                current, v["ok"] = None, False
                v["note"] = "edit did not apply (file changed outside Claude?)"
        else:
            old = ev.get("old", "")
            if old and old in current:
                current = current.replace(
                    old, ev.get("new") or "",
                    -1 if ev.get("replace_all") else 1)
            else:
                current, v["ok"] = None, False
                v["note"] = "edit did not apply (file changed outside Claude?)"
        v["content"] = current
        v["bytes"] = len(current.encode()) if current is not None else None
        versions.append(v)
    return versions


def file_diff(versions: List[dict], a: int, b: int) -> List[str]:
    va, vb = versions[a], versions[b]
    if va["content"] is None or vb["content"] is None:
        raise ValueError("one of the selected versions is not "
                         "reconstructable (inputs lost or chain broken)")
    return list(difflib.unified_diff(
        va["content"].splitlines(), vb["content"].splitlines(),
        fromfile=f"v{a} {va['ts'] or ''} ({va['tool']})",
        tofile=f"v{b} {vb['ts'] or ''} ({vb['tool']})", lineterm=""))


def known_files(db_path: str, query: str = "", limit: int = 40) -> List[dict]:
    """File paths Claude has edited, from the terms index (kind=target,
    looks-like-a-path), ranked by mention count."""
    conn = fdb.connect(db_path)
    try:
        rows = conn.execute("""
            SELECT term, SUM(count) AS n FROM terms
            WHERE kind='target' AND (term LIKE '%/%' OR term LIKE '%.%')
            AND term LIKE ? GROUP BY term ORDER BY n DESC LIMIT ?""",
            (f"%{query}%", limit)).fetchall()
        return [{"path": t, "mentions": n} for t, n in rows]
    finally:
        conn.close()


def cmd_file(args) -> int:
    events = file_events(args.db, args.path)
    if not events:
        print(f"no Edit/Write history for {args.path!r} in the archive")
        return 1
    versions = reconstruct(events)
    if args.diff:
        a, b = args.diff
        for line in file_diff(versions, a, b):
            print(line)
        return 0
    if args.show is not None:
        v = versions[args.show]
        if v["content"] is None:
            print(f"v{args.show} not reconstructable: {v['note']}")
            return 1
        print(v["content"])
        return 0
    print(f"{len(versions)} versions of {args.path}:")
    for i, v in enumerate(versions):
        mark = "ok" if v["ok"] else "??"
        size = f"{v['bytes']}B" if v["bytes"] is not None else "—"
        print(f"  v{i:<3} [{mark}] {v['ts'] or '?':<26} {v['tool']:<10}"
              f" {size:>9}  session {str(v['session_id'])[:8]}"
              + (f"  ({v['note']})" if v["note"] else ""))
    print(f"\nshow a version: fable file {args.path} --show N"
          f"\ndiff versions:  fable file {args.path} --diff A B")
    return 0
