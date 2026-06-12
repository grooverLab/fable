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


def run_hook(db_path: str, payload: dict) -> dict:
    transcript = payload.get("transcript_path")
    event = payload.get("hook_event_name", "?")
    session = payload.get("session_id", "?")

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
            healed = _compaction_recovery(db_path, session)
            if healed:
                parts.append(healed)
        return {"ok": True, "event": event, "inject": "\n".join(parts)}

    if not transcript or not os.path.exists(transcript):
        return {"ok": False, "reason": "no transcript_path"}

    from fable.discover import DEFAULT_BACKUP_ROOTS, project_label
    from fable.extract import fts_extract_fn
    from fable.indexer import index_vault
    from fable.prune import backup as vault_backup

    # project label from the encoded ~/.claude/projects dirname
    project = project_label(os.path.basename(os.path.dirname(transcript)))
    backup_root = next((r for r in DEFAULT_BACKUP_ROOTS if os.path.isdir(r)),
                       str(Path(db_path).parent / "backups"))
    backup_dir = Path(backup_root) / project

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
                "hookEventName": "SessionStart",
                "additionalContext": result["inject"]}}))
        _log(args.db, json.dumps({"payload_event":
                                  payload.get("hook_event_name"),
                                  **{k: v for k, v in result.items()
                                     if k != "inject"}}))
    except Exception:
        _log(args.db, "hook error:\n" + traceback.format_exc())
    return 0
