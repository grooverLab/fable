"""fable compose — topic workspaces: a NEW resumable session built from
selected threads, in your chosen order, drawn from ANY session or project.

Empirically validated (signature replay experiment, 2026-06-12): Claude
resumes a restitched transcript cleanly — per-block thinking signatures
survive reordering, and the model reads the stitched order as its genuine
conversation history. The original sessions are never touched.

Every copied record gets a fresh uuid (so the index never confuses the
workspace with its sources) plus provenance fields, and threads are joined
by seam records that state where each piece originally came from.
"""
import json
import os
import uuid as uuidlib
from datetime import datetime, timezone
from typing import List, Optional

from fable import db as fdb
from fable.jsonl import read_span
from fable.threads import reconstruct


def _encode_cwd(cwd: str) -> str:
    """Claude Code's project-dir encoding: every non-alphanumeric char
    becomes a dash (verified against real dirs: 01_Algos -> 01-Algos)."""
    import re
    return re.sub(r"[^A-Za-z0-9-]", "-", cwd)


def _seam(session_id: str, parent: Optional[str], text: str) -> dict:
    return {
        "uuid": str(uuidlib.uuid4()),
        "parentUuid": parent,
        "type": "user",
        "isSidechain": False,
        "sessionId": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z"),
        "fableSeam": True,
        "message": {"role": "user", "content": [
            {"type": "text", "text": text}]},
    }


def compose(db_path: str, thread_ids: List[str], title: str,
            cwd: Optional[str] = None, strip_thinking: bool = False,
            projects_dir: Optional[str] = None) -> dict:
    if not thread_ids:
        raise ValueError("no threads selected")
    cwd = cwd or os.getcwd()
    projects_dir = projects_dir or os.path.expanduser("~/.claude/projects")
    target_dir = os.path.join(projects_dir, _encode_cwd(cwd))
    os.makedirs(target_dir, exist_ok=True)
    new_sid = str(uuidlib.uuid4())
    out_path = os.path.join(target_dir, new_sid + ".jsonl")

    conn = fdb.connect(db_path)
    out: List[dict] = [{"type": "custom-title",
                        "customTitle": f"⊞ {title}",
                        "sessionId": new_sid}]
    prev_leaf: Optional[str] = None
    threads_used = 0
    try:
        for item in thread_ids:
            # item is "thread-id" (full fidelity) or {"id":…, "mode":"card"}
            if isinstance(item, dict):
                tid, mode = item["id"], item.get("mode", "full")
            else:
                tid, mode = item, "full"
            if mode == "card":
                row = conn.execute(
                    "SELECT title, type, outcome, decisions, summary "
                    "FROM cards WHERE prompt_id = ?", (tid,)).fetchone()
                if row is None:
                    raise KeyError(f"thread {tid} has no card yet — weave "
                                   f"it FULL or run a backfill first")
                decs = ""
                try:
                    decs = "".join(f"\n  decision: {d}"
                                   for d in json.loads(row[3] or "[]"))
                except ValueError:
                    pass
                seam = _seam(new_sid, prev_leaf,
                             f"<fable-memory mode=\"card\">Prior work, "
                             f"summarized (full recall: fable_thread {tid}):"
                             f"\n[{row[1]}] {row[0]} — {row[2] or ''}"
                             f"{decs}\n{row[4] or ''}</fable-memory>")
                out.append(seam)
                prev_leaf = seam["uuid"]
                threads_used += 1
                continue
            view = reconstruct(conn, tid)
            turns = view.main
            if not turns:
                continue
            card = conn.execute(
                "SELECT title, outcome FROM cards WHERE prompt_id = ?",
                (tid,)).fetchone()
            src_session = conn.execute(
                "SELECT session_id FROM threads WHERE prompt_id = ?",
                (tid,)).fetchone()
            label = (card[0] if card and card[0] else tid[:8])
            seam = _seam(new_sid, prev_leaf,
                         f"<fable-seam>The following thread was restitched "
                         f"by fable compose from session "
                         f"{(src_session[0] or '?')[:8] if src_session else '?'} "
                         f"(originally {turns[0].ts or 'unknown date'}): "
                         f"“{label}”"
                         + (f" — outcome: {card[1]}" if card and card[1]
                            else "")
                         + ". Treat it as prior work being resumed."
                         f"</fable-seam>")
            out.append(seam)
            prev_leaf = seam["uuid"]

            idmap = {}
            for turn in turns:
                obj = json.loads(read_span(turn.path, turn.offset,
                                           turn.length)
                                 .decode("utf-8", "surrogateescape"))
                old_uuid = obj.get("uuid")
                new_uuid = str(uuidlib.uuid4())
                idmap[old_uuid] = new_uuid
                obj["fableComposedFrom"] = old_uuid
                obj["uuid"] = new_uuid
                obj["sessionId"] = new_sid
                old_parent = obj.get("parentUuid")
                obj["parentUuid"] = idmap.get(old_parent, prev_leaf)
                if strip_thinking:
                    msg = obj.get("message")
                    if isinstance(msg, dict) and isinstance(
                            msg.get("content"), list):
                        msg["content"] = [b for b in msg["content"]
                                          if not (isinstance(b, dict)
                                                  and b.get("type")
                                                  == "thinking")]
                out.append(obj)
                prev_leaf = new_uuid
            threads_used += 1
    finally:
        conn.close()

    if not threads_used:
        raise KeyError(f"none of the selected threads exist in the index")

    with open(out_path, "w") as f:
        for rec in out:
            f.write(json.dumps(rec, separators=(",", ":"),
                               ensure_ascii=True) + "\n")
    fdb.log_op(db_path, "compose", title=title, threads=len(thread_ids))
    return {"session_id": new_sid, "path": out_path,
            "threads": threads_used, "records": len(out),
            "resume": f"cd {cwd} && claude --resume {new_sid}"}


def cmd_compose(args) -> int:
    result = compose(args.db, args.threads, " ".join(args.title),
                     cwd=args.cwd, strip_thinking=args.strip_thinking)
    print(json.dumps(result, indent=2))
    return 0
