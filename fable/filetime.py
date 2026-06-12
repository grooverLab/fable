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
        reads = {}
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
                            and block.get("type") == "tool_use"):
                        continue
                    inp = block.get("input")
                    if not isinstance(inp, dict):
                        continue
                    target = inp.get("file_path") or inp.get("path") or ""
                    if not (target == path_query
                            or target.endswith("/" + path_query.lstrip("/"))
                            or target.endswith(path_query)):
                        continue
                    name = block.get("name")
                    if name in EDIT_TOOLS:
                        events.append(_event(name, inp, target, uuid,
                                             ts, ts_epoch, prompt_id,
                                             session_id))
                    elif (name == "Read" and not inp.get("offset")
                          and not inp.get("limit")):
                        # full-file Read: its result is a ground-truth
                        # snapshot we can re-anchor the chain on
                        reads[uuid] = {"tool": "Read", "file_path": target,
                                       "uuid": uuid, "ts": ts,
                                       "ts_epoch": ts_epoch,
                                       "prompt_id": prompt_id,
                                       "session_id": session_id,
                                       "tool_id": block.get("id"),
                                       "ok": False, "note": ""}
        if reads:
            ph = ",".join("?" * len(reads))
            rrows = conn.execute(f"""
                SELECT r.parent_uuid, f.path, r.offset, r.length
                FROM records r JOIN files f ON f.id = r.file_id
                WHERE r.parent_uuid IN ({ph})""",
                list(reads)).fetchall()
            for parent, fpath, offset, length in rrows:
                ev = reads.get(parent)
                if ev is None or ev.get("content") is not None:
                    continue
                try:
                    obj = json.loads(read_span(fpath, offset, length)
                                     .decode("utf-8", "surrogateescape"))
                except (OSError, ValueError):
                    continue
                msg = obj.get("message")
                blocks = (msg.get("content")
                          if isinstance(msg, dict) else None)
                if not isinstance(blocks, list):
                    continue
                for b in blocks:
                    if (isinstance(b, dict)
                            and b.get("type") == "tool_result"
                            and b.get("tool_use_id") == ev["tool_id"]):
                        inner = b.get("content")
                        texts = []
                        if isinstance(inner, str):
                            texts = [inner]
                        elif isinstance(inner, list):
                            texts = [x.get("text", "") for x in inner
                                     if isinstance(x, dict)
                                     and x.get("type") == "text"]
                        snap = _parse_read_snapshot(
                            "\n".join(texts)) if texts else None
                        if snap is not None:
                            ev["content"], ev["ok"] = snap, True
            events.extend(ev for ev in reads.values() if ev.get("ok"))
        events.sort(key=lambda e: (e["ts_epoch"] or 0, e["uuid"]))
        return events
    finally:
        conn.close()


_NUMBERED = None


def _parse_read_snapshot(text):
    """Claude Code Read results carry the file as numbered lines
    ('     1\tcontent' or '  1→content'). Returns the file content if the
    result is a complete-from-line-1 snapshot, else None."""
    import re
    global _NUMBERED
    if _NUMBERED is None:
        _NUMBERED = re.compile(r"^\s*(\d+)(?:\t|→)(.*)$")
    if not isinstance(text, str) or not text.strip():
        return None
    lines = text.split("\n")
    out, expect = [], 1
    matched = 0
    for ln in lines:
        m = _NUMBERED.match(ln)
        if not m:
            if not ln.strip():
                continue
            if matched > 3:
                break  # trailing notices after the snapshot
            return None
        if int(m.group(1)) != expect:
            return None
        out.append(m.group(2))
        expect += 1
        matched += 1
    if matched >= 2000:
        return None  # Read truncates at 2000 lines — partial, never anchor
    return "\n".join(out) + "\n" if matched else None


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


def _invert(ev, after):
    """State BEFORE an Edit, derived from the state after it. Returns None
    when inversion is ambiguous (deletions, replace_all, missing strings)."""
    if ev.get("replace_all"):
        return None
    if ev["tool"] == "MultiEdit":
        cur = after
        for e in reversed(ev.get("edits") or []):
            old, new = e.get("old_string", ""), e.get("new_string", "")
            if not old or not new or new not in cur:
                return None
            cur = cur.replace(new, old, 1)
        return cur
    old, new = ev.get("old"), ev.get("new")
    if not old or not new or new not in (after or ""):
        return None
    return after.replace(new, old, 1)


def reconstruct(events: List[dict]) -> List[dict]:
    """Replay events into file versions — bidirectionally.

    Forward pass: exact states wherever the chain is intact. Backward pass:
    a broken stretch is rebuilt from the NEXT anchor by inverting edits
    (their before/after strings are recorded verbatim), marked derived.
    Only stretches that end at a Write stay unknown — a Write's prior
    state is genuinely unrelated to its content."""
    versions = []
    evlist = []
    current: Optional[str] = None
    for ev in events:
        v = {"uuid": ev["uuid"], "ts": ev["ts"], "tool": ev["tool"],
             "session_id": ev["session_id"], "prompt_id": ev["prompt_id"],
             "ok": True, "note": ev.get("note", "")}
        if ev["tool"] == "Read":
            snap = ev.get("content")
            if snap is None:
                continue
            if current is not None and current == snap:
                continue  # checkpoint agrees — no new version
            v["note"] = ("re-anchored from Read snapshot"
                         if current is None else
                         "re-anchored — out-of-band changes detected")
            current = snap
        elif ev["tool"] == "Write":
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
        evlist.append(ev)

    # ── backward pass: rebuild broken stretches from the next anchor ──
    for i in range(len(versions) - 1, 0, -1):
        cur, prev = versions[i], versions[i - 1]
        if prev["content"] is not None or cur["content"] is None:
            continue
        ev = evlist[i]
        if ev["tool"] == "Write":
            continue  # hard barrier: state before a Write is unknowable
        if ev["tool"] == "Read":
            # the snapshot is the state just before the Read too (reads
            # don't modify) — derived, since another out-of-band change
            # could sit in between
            before = cur["content"]
        else:
            before = _invert(ev, cur["content"])
        if before is None:
            continue
        prev["content"] = before
        prev["bytes"] = len(before.encode())
        prev["ok"] = True
        prev["derived"] = True
        prev["note"] = ((prev["note"] + " · ") if prev["note"] else "") + \
            f"rebuilt backward from v{i}"
    return versions


def file_diff(versions: List[dict], a: int, b: int) -> List[str]:
    va, vb = versions[a], versions[b]
    if va["content"] is None or vb["content"] is None:
        raise ValueError("one of the selected versions is not "
                         "reconstructable (inputs lost or chain broken)")
    tag = lambda v, i: (f"v{i} {v['ts'] or ''} ({v['tool']}"
                        + (", derived" if v.get("derived") else "") + ")")
    return list(difflib.unified_diff(
        va["content"].splitlines(), vb["content"].splitlines(),
        fromfile=tag(va, a), tofile=tag(vb, b), lineterm=""))


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


def session_files(db_path: str, session_id: str) -> List[dict]:
    """Every file edited in one session, with per-file analytics —
    the Files tab's card grid when a transcript is selected."""
    conn = fdb.connect(db_path)
    try:
        rows = conn.execute("""
            SELECT r.uuid, r.ts, r.prompt_id, f.path, r.offset, r.length
            FROM records r JOIN files f ON f.id = r.file_id
            WHERE r.session_id = ? AND r.block_kinds LIKE '%tool_use%'
            ORDER BY r.ts_epoch""", (session_id,)).fetchall()
        agg = {}
        for uuid, ts, prompt_id, fpath, offset, length in rows:
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
                target = inp.get("file_path") or inp.get("path")
                if not target:
                    continue
                a = agg.setdefault(target, {
                    "path": target, "edits": 0, "writes": 0,
                    "first_ts": ts, "last_ts": ts, "threads": set(),
                    "last_tool": block["name"]})
                if block["name"] == "Write":
                    a["writes"] += 1
                else:
                    a["edits"] += 1
                a["last_ts"] = ts or a["last_ts"]
                a["last_tool"] = block["name"]
                if prompt_id:
                    a["threads"].add(prompt_id)
        out = []
        for a in agg.values():
            a["threads"] = len(a["threads"])
            a["total"] = a["edits"] + a["writes"]
            out.append(a)
        out.sort(key=lambda x: -x["total"])
        return out
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
