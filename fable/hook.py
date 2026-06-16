"""fable hook — Claude Code lifecycle hook handler.

The single most demanded capability in the ecosystem (per community
research): archive the FULL transcript automatically before compaction
destroys it. Claude Code invokes hooks with a JSON payload on stdin:

  PreCompact / SessionEnd payload includes:
    {"session_id": "...", "transcript_path": "/path/to/session.jsonl", ...}

Wire-up (settings.json):
  "hooks": {"PreCompact": [{"hooks": [{"type": "command",
      "command": "/path/to/fable/bin/fable --db /path/to/fable.db hook"}]}]}

The handler is fail-quiet by design: a hook must NEVER break a live
session, so all errors are swallowed into the hook log.
"""
import json
import os
import sys
import traceback
from pathlib import Path


def _log(db_path, msg):
    try:
        log = Path(db_path).parent / "hook.log"
        with open(log, "a") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass


CHECKPOINT_TRACK_CAP = 300
CHECKPOINT_MAX_BYTES = 5_000_000
MUTATING_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read")


def _ckpt_dir(session_id):
    from fable.paths import checkpoints_dir
    base = checkpoints_dir()
    d = os.path.join(base, session_id or "unknown")
    os.makedirs(d, exist_ok=True)
    return d


def _post_tool(payload):
    """Close the invisible-mutation gap: scripts (sed, heredocs) change
    files without leaving content in the transcript. Track every file
    Claude touches via file tools; after each Bash call, snapshot any
    tracked file whose bytes changed. Snapshots become exact time-travel
    anchors."""
    import shutil
    import time as _t
    session = payload.get("session_id") or ""
    tool = payload.get("tool_name") or ""
    d = _ckpt_dir(session)
    state_path = os.path.join(d, "tracked.json")
    try:
        with open(state_path) as f:
            tracked = json.load(f)
    except (OSError, ValueError):
        tracked = {}

    def stat_of(p):
        try:
            st = os.stat(p)
            return [st.st_mtime, st.st_size]
        except OSError:
            return None

    changed_any = False
    if tool in MUTATING_TOOLS:
        inp = payload.get("tool_input") or {}
        p = inp.get("file_path") or inp.get("path")
        if isinstance(p, str) and p.startswith("/"):
            st = stat_of(p)
            if st and st[1] <= CHECKPOINT_MAX_BYTES:
                tracked[p] = st
                changed_any = True
                while len(tracked) > CHECKPOINT_TRACK_CAP:
                    tracked.pop(next(iter(tracked)))
    elif tool == "Bash":
        for p, old in list(tracked.items()):
            st = stat_of(p)
            if st is None:
                tracked.pop(p)
                changed_any = True
                continue
            if st == old:
                continue
            # a command mutated a file Claude is working on — checkpoint it
            ts = _t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime())
            name = f"{int(_t.time()*1000):x}-{os.path.basename(p)}"
            try:
                if st[1] <= CHECKPOINT_MAX_BYTES:
                    shutil.copy2(p, os.path.join(d, name))
                    with open(os.path.join(d, "checkpoints.jsonl"),
                              "a") as f:
                        f.write(json.dumps(
                            {"ts": ts, "path": p, "file": name,
                             "after": "Bash"}) + "\n")
            except OSError:
                pass
            tracked[p] = st
            changed_any = True
    if changed_any:
        try:
            with open(state_path, "w") as f:
                json.dump(tracked, f)
        except OSError:
            pass
    return {"ok": True}


CONTEXT_WINDOW = 200_000
AUTOPRUNE_COOLDOWN = 1800     # never twice within 30 min
AUTOPRUNE_MIN_BYTES = 2_000_000


def _context_pct(transcript: str) -> float:
    """Current context fill, estimated from the last assistant message's
    usage block (cumulative input + cache tokens ≈ what the model holds)."""
    try:
        size = os.path.getsize(transcript)
        with open(transcript, "rb") as f:
            f.seek(max(0, size - 300_000))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return 0.0
    for line in reversed(tail):
        if '"usage"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        msg = obj.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict):
            continue
        ctx = ((usage.get("input_tokens") or 0)
               + (usage.get("cache_read_input_tokens") or 0)
               + (usage.get("cache_creation_input_tokens") or 0))
        if ctx:
            return 100.0 * ctx / CONTEXT_WINDOW
    return 0.0


def _auto_prune(db_path: str, payload: dict):
    """When enabled (Settings tab), prune the live transcript automatically
    once context crosses the configured threshold — and tell the user how
    to resume into the slim session."""
    import time as _t
    transcript = payload.get("transcript_path")
    session = payload.get("session_id") or ""
    if not transcript or not os.path.exists(transcript):
        return None
    try:
        from fable import db as fdb
        conn = fdb.connect(db_path)
    except FileNotFoundError:
        return None
    try:
        cfg = dict(conn.execute(
            "SELECT key, value FROM meta WHERE key IN "
            "('autoprune_enabled','autoprune_pct')").fetchall())
        if cfg.get("autoprune_enabled") != "1":
            return None
        threshold = float(cfg.get("autoprune_pct") or 80)
        row = conn.execute("SELECT value FROM meta WHERE key = ?",
                           (f"autoprune_last:{session}",)).fetchone()
        if row and _t.time() - float(row[0]) < AUTOPRUNE_COOLDOWN:
            return None
    finally:
        conn.close()

    if os.path.getsize(transcript) < AUTOPRUNE_MIN_BYTES:
        return None
    pct = _context_pct(transcript)
    if pct < threshold:
        return None

    from fable.discover import project_label
    from fable.paths import vault_dir
    from fable.prune import prune_file
    before = os.path.getsize(transcript)
    project = project_label(os.path.basename(os.path.dirname(transcript)))
    backup_root = vault_dir()
    try:
        prune_file(transcript, "resume",
                   backup_dir=Path(backup_root) / project,
                   replace=True, strip_images=True,
                   db_path=db_path, force=True)
    except Exception as e:
        _log(db_path, f"autoprune failed: {e}")
        return None
    after = os.path.getsize(transcript)
    conn = fdb.connect(db_path)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                 (f"autoprune_last:{session}", str(_t.time())))
    conn.commit()
    conn.close()
    fdb.log_op(db_path, "autoprune", session=session, pct=round(pct, 1),
               before=before, after=after)
    return (f"fable auto-prune: context at {pct:.0f}% — transcript slimmed "
            f"{before / 1e6:.1f}MB → {after / 1e6:.1f}MB (vault backup "
            f"sealed; nothing lost). To load the slim session: /exit then "
            f"`claude --resume {session}`")


def _tail_index(db_path, transcript):
    """Roadmap #1: keep the Map fresh with the turns just appended to the live
    transcript — cheap tail index, no seal. Best-effort: an indexing hiccup
    must never affect the running session."""
    if not transcript or not os.path.exists(transcript):
        return
    try:
        from fable.extract import fts_extract_fn
        from fable.indexer import index_live_tail
        index_live_tail(db_path, transcript, extract_fn=fts_extract_fn)
        _ensure_session_row(db_path, transcript)
    except Exception as e:
        _log(db_path, f"tail-index failed: {e}")


def _ensure_session_row(db_path, transcript):
    """Register the live session in the sidebar. The tail-index writes the turns,
    but the `sessions` row that api_projects lists was discover-only — so a new
    project's session was searchable yet invisible in the tree until a discover
    ran. Create it the moment it's indexed; never clobber a discover-built row."""
    import datetime
    from fable import db as fdb
    from fable.discover import session_title, project_label
    sid = os.path.basename(transcript)
    if sid.endswith(".jsonl"):
        sid = sid[:-6]
    conn = fdb.connect(db_path)
    try:
        if conn.execute("SELECT 1 FROM sessions WHERE session_id=?",
                        (sid,)).fetchone():
            # existing (maybe an orphan-vault row) — just ensure live_path is set
            conn.execute(
                "UPDATE sessions SET live_path=COALESCE(live_path, ?) "
                "WHERE session_id=? AND (live_path IS NULL OR live_path='')",
                (transcript, sid))
        else:
            proj = project_label(os.path.basename(os.path.dirname(transcript)))
            conn.execute(
                "INSERT INTO sessions(session_id, project, title, live_path,"
                " indexed_at) VALUES(?,?,?,?,?)",
                (sid, proj, session_title(transcript), transcript,
                 datetime.datetime.now(datetime.timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()


_EXTERNALIZE = (
    "<fable-externalize>\n"
    "Reason in TEXT — it is what fable indexes into memory (your thinking "
    "blocks and tool results get pruned; only your prose survives). Before a "
    "non-trivial action, state the decision and the rejected alternative; "
    "after a finding, write \"Found: …\". Name the files, errors and decisions "
    "explicitly so future sessions can recall the WHY, not just the WHAT.\n"
    "</fable-externalize>")


def _externalize_note(db_path: str) -> str:
    """Reasoning-externalisation reminder injected each user turn (default ON;
    suppressed only when meta externalize_enabled='0'). Sentinel-wrapped so the
    extractor strips it — it nudges the model without entering the index."""
    try:
        from fable import db as fdb
        conn = fdb.connect(db_path)
        row = conn.execute(
            "SELECT value FROM meta WHERE key='externalize_enabled'").fetchone()
        conn.close()
        if row and row[0] == "0":
            return ""
    except Exception:
        return ""
    return _EXTERNALIZE


def _capture_task(db_path, payload):
    """M2 live capture: on a TaskCreate/TaskUpdate PostToolUse, upsert the task
    into the materialized `tasks` table immediately — no fat-union rebuild. The
    result carries the exact id ('Task #N created: subject'); TaskUpdate input
    carries the status. Project is provisional (cwd basename) until a rebuild
    reconciles the true work-project. Never raises — hooks must not break tools."""
    tool = payload.get("tool_name") or ""
    if tool not in ("TaskCreate", "TaskUpdate"):
        return
    import re
    import time as _t
    from fable import db as fdb, tasktime
    inp = payload.get("tool_input") or {}
    resp = payload.get("tool_response")
    if isinstance(resp, dict):
        resp = resp.get("content") or json.dumps(resp)
    if isinstance(resp, list):
        resp = " ".join(x.get("text", "") if isinstance(x, dict) else str(x)
                        for x in resp)
    resp = str(resp or "")
    m = re.search(r'Task #(\d+) (?:created|updated)[^:]*:\s*(.*)', resp, re.S)
    tid = (int(m.group(1)) if m
           else int(inp["taskId"]) if str(inp.get("taskId", "")).isdigit()
           else None)
    if tid is None:
        return
    session = payload.get("session_id") or ""
    proj = os.path.basename(payload.get("cwd") or "") or None
    ts = _t.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        conn = fdb.connect(db_path)
        conn.execute(tasktime._TASKS_DDL)
        if tool == "TaskCreate":
            desc = inp.get("description") or ""
            subj = (m.group(2).strip()[:90] if m and m.group(2).strip()
                    else desc.splitlines()[0][:90] if desc else "(empty)")
            cur = conn.execute(
                "UPDATE tasks SET subject=?, ts=? WHERE session=? AND task_id=?",
                (subj, ts, session, tid))
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO tasks(session,task_id,ts,project,subject,status,"
                    "drifted,interp,prompt_id,source,cwd) "
                    "VALUES(?,?,?,?,?,'pending',1,0,NULL,'task',?)",
                    (session, tid, ts, proj, subj, proj))
        else:
            status = inp.get("status")
            if status:
                conn.execute(
                    "UPDATE tasks SET status=?, drifted=? "
                    "WHERE session=? AND task_id=?",
                    (status, 0 if status in ("completed", "deleted") else 1,
                     session, tid))
        conn.commit()
        conn.close()
    except Exception:
        pass


def run_hook(db_path: str, payload: dict) -> dict:
    transcript = payload.get("transcript_path")
    event = payload.get("hook_event_name", "?")
    session = payload.get("session_id", "?")

    if event == "PostToolUse":
        result = _post_tool(payload)
        _capture_task(db_path, payload)        # M2: live task ledger
        msg = _auto_prune(db_path, payload)
        if msg:
            result["system_message"] = msg
        return result
    if event in ("Stop", "SubagentStop"):
        # turn boundary: the assistant (or a subagent) just finished, so the
        # new turn(s) are settled in the transcript — index the tail now
        _tail_index(db_path, transcript)
        return {"ok": True, "event": event}
    if event == "UserPromptSubmit":
        # the user may have edited tracked files in their IDE while Claude
        # was idle — sweep for silent changes before the next turn begins —
        # and tail-index so the just-submitted prompt enters the Map at once
        payload = dict(payload, tool_name="Bash")
        result = _post_tool(payload)
        _tail_index(db_path, transcript)
        note = _externalize_note(db_path)
        if note:
            result["inject"] = note
            result["event"] = "UserPromptSubmit"
        return result

    if event == "SessionStart":
        # auto-inject remembered facts into the fresh session — and after a
        # compaction, heal the amnesia: re-inject this session's own memory
        from fable.facts import render_facts
        cwd = payload.get("cwd") or ""
        project = os.path.basename(cwd) if cwd else None
        parts = []
        try:
            block = render_facts(db_path, project=project)
            if block:
                parts.append(block)
        except FileNotFoundError:
            pass
        if payload.get("source") == "compact" and session != "?":
            # a tool description is passive; THIS fires the instruction at the
            # exact moment the model would otherwise trust the lossy summary
            parts.append(
                "⚠ This session was COMPACTED — the earlier turns you see are "
                "a lossy summary. Before relying on the summary for any "
                "specific detail (decisions, code, exact wording), recall the "
                "real turns with the fable MCP tools (fable_search → "
                "fable_thread); fable preserved the full verbatim transcript.")
            healed = _compaction_recovery(db_path, session)
            if healed:
                parts.append(healed)
        else:
            parts.append(
                "📚 fable recall is available (MCP tools fable_search, "
                "fable_thread, fable_block). When you need earlier "
                "conversation, a past decision, or a prior code discussion "
                "that isn't in your context, call fable_search instead of "
                "guessing — it indexes this and every past session, verbatim.")
        # M7: resurface this project's open/drifted tasks so they don't get lost
        try:
            from fable import tasktime
            rows, total = tasktime.open_for_project(db_path, project, 8)
            if rows:
                lines = [
                    f"  #{r[0]} [{r[1]}] {(r[2] or '')[:78]}"
                    + (f"  ·P{r[3]}" if r[3] is not None else "") for r in rows]
                more = (f"\n  …and {total - len(rows)} more — open the Tasks tab"
                        if total > len(rows) else "")
                parts.append(
                    f"<fable-open-tasks project=\"{project}\" count=\"{total}\">\n"
                    f"{total} open task(s) in this project, resurfaced so they "
                    f"don't drift — pick one up, or mark done / remove if stale:\n"
                    + "\n".join(lines) + more + "\n</fable-open-tasks>")
        except Exception:
            pass
        return {"ok": True, "event": event, "inject": "\n".join(parts)}

    if not transcript or not os.path.exists(transcript):
        return {"ok": False, "reason": "no transcript_path"}

    from fable.discover import project_label
    from fable.paths import vault_dir
    from fable.extract import fts_extract_fn
    from fable.indexer import index_vault
    from fable.prune import backup as vault_backup

    # project label from the encoded ~/.claude/projects dirname
    project = project_label(os.path.basename(os.path.dirname(transcript)))
    backup_dir = Path(vault_dir()) / project

    version, dest = vault_backup(Path(transcript), backup_dir)
    stats = index_vault(db_path, [str(dest)], live_file=transcript,
                        extract_fn=fts_extract_fn,
                        session_id=session, project=project)
    return {"ok": True, "event": event, "backup": str(dest),
            "version": version,
            "records_indexed": stats["records_indexed"]}


def _compaction_recovery(db_path: str, session_id: str,
                         limit: int = 10) -> str:
    """Compaction summaries are lossy; fable's index is not. Re-inject the
    decisions/outcomes of this session's own threads (the PreCompact hook
    sealed them moments ago) so the model keeps what compaction erased."""
    import json as _json
    try:
        from fable import db as fdb
        conn = fdb.connect(db_path)
    except FileNotFoundError:
        return ""
    try:
        rows = conn.execute(
            "SELECT c.prompt_id, c.title, c.type, c.outcome, c.decisions "
            "FROM cards c JOIN threads t ON t.prompt_id = c.prompt_id "
            "WHERE t.session_id = ? ORDER BY t.last_ts DESC LIMIT ?",
            (session_id, limit)).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    lines = ["<fable-memory source=\"compaction-recovery\">",
             "Compaction just summarized this session lossily. fable holds "
             "the full-fidelity history; key context from THIS session:"]
    for pid, title, ctype, outcome, decisions in rows:
        lines.append(f"- [{ctype}] {title} — {outcome or ''} "
                     f"(full recall: fable_thread {pid})")
        try:
            for d in _json.loads(decisions or "[]")[:2]:
                lines.append(f"    decision: {d}")
        except ValueError:
            pass
    lines.append("Retrieve anything verbatim via the fable_search / "
                 "fable_thread tools.")
    lines.append("</fable-memory>")
    return "\n".join(lines)


def cmd_hook(args) -> int:
    """Read the hook payload from stdin; never exit non-zero on failure
    (a broken hook must not block compaction or session end)."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = run_hook(args.db, payload)
        if result.get("inject"):
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": result.get("event", "SessionStart"),
                "additionalContext": result["inject"]}}))
        elif result.get("system_message"):
            print(json.dumps({"systemMessage": result["system_message"]}))
        _log(args.db, json.dumps({"payload_event":
                                  payload.get("hook_event_name"),
                                  **{k: v for k, v in result.items()
                                     if k != "inject"}}))
    except Exception:
        _log(args.db, "hook error:\n" + traceback.format_exc())
    return 0
