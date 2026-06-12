"""Thread-level transcript surgery.

Surgery operates on whole threads (a user prompt + everything it caused),
never on individual lines — dropping half a tool exchange corrupts the
conversation. The flow is suggest -> plan (pure simulation) -> apply
(mandatory backup, evict gate on the dropped records, atomic rewrite,
re-index). parentUuid rechaining reuses the pruner's rebuild_chain.
"""
import json
import os
import time
from pathlib import Path
from typing import List, Optional

from fable import db as fdb
from fable.jsonl import iter_records
from fable.prune import (backup as vault_backup, rebuild_chain, validate,
                         _atomic_write, PruneGateError)


def suggestions(db_path: str, session_id: str, limit: int = 40) -> List[dict]:
    """Rank threads that are candidates for eviction from a live session."""
    conn = fdb.connect(db_path)
    try:
        rows = conn.execute("""
            SELECT t.prompt_id, t.turn_count, t.est_tokens, t.text_bytes,
                   t.sidechain_turns, t.first_ts, t.last_ts,
                   c.title, c.type, c.outcome,
                   (SELECT SUM(r.length) FROM records r
                    WHERE r.prompt_id = t.prompt_id) AS raw_bytes
            FROM threads t LEFT JOIN cards c ON c.prompt_id = t.prompt_id
            WHERE t.session_id = ? ORDER BY t.first_ts""",
            (session_id,)).fetchall()

        # topic signature per thread: top targets+concepts. A thread is
        # "superseded" when a LATER thread shares >= 2 signature terms.
        signature = {}
        for row in rows:
            pid = row[0]
            signature[pid] = {t[0] for t in conn.execute(
                "SELECT term FROM terms WHERE prompt_id = ? AND kind IN "
                "('target','concept') ORDER BY score DESC LIMIT 4", (pid,))}

        superseded_by = {}
        for i, row_i in enumerate(rows):
            pid_i = row_i[0]
            if not signature[pid_i]:
                continue
            for row_j in rows[i + 1:]:
                pid_j = row_j[0]
                shared = signature[pid_i] & signature[pid_j]
                if len(shared) >= 2:
                    superseded_by[pid_i] = (pid_j, sorted(shared)[:2])
                    break

        out = []
        for (pid, turns, tokens, text_bytes, side, first_ts, last_ts,
             title, ctype, outcome, raw_bytes) in rows:
            reasons = []
            raw_bytes = raw_bytes or 0
            text_bytes = text_bytes or 0
            if raw_bytes > 100_000 and text_bytes < raw_bytes * 0.03:
                reasons.append(
                    f"tool-noise heavy: {raw_bytes // 1024}KB raw, "
                    f"{text_bytes // 1024}KB signal")
            if turns and (side or 0) * 2 > turns:
                reasons.append(f"subagent-dominated ({side}/{turns} turns)")
            if pid in superseded_by:
                later, shared = superseded_by[pid]
                reasons.append(f"likely superseded by {later[:8]} "
                               f"(shared topic: {', '.join(shared)})")
            if reasons:
                out.append({"prompt_id": pid, "title": title,
                            "reasons": reasons, "est_tokens": tokens,
                            "turn_count": turns, "type": ctype,
                            "outcome": outcome, "first_ts": first_ts})
        out.sort(key=lambda s: -(s["est_tokens"] or 0))
        return out[:limit]
    finally:
        conn.close()


def _dropped_uuids(conn, drops: List[str]) -> set:
    ph = ",".join("?" * len(drops))
    return {u for (u,) in conn.execute(
        f"SELECT uuid FROM records WHERE prompt_id IN ({ph})", drops)}


def plan(db_path: str, live_path: str, drops: List[str]) -> dict:
    """Pure simulation: what would removing these threads do to the file?"""
    if not drops:
        raise ValueError("no threads selected")
    conn = fdb.connect(db_path)
    try:
        doomed = _dropped_uuids(conn, drops)
    finally:
        conn.close()

    kept, removed, removed_bytes = [], 0, 0
    full_chain = {}
    for rec in iter_records(live_path):
        uuid = rec.obj.get("uuid")
        if uuid:
            full_chain[uuid] = rec.obj.get("parentUuid")
        if uuid and uuid in doomed:
            removed += 1
            removed_bytes += rec.length
            continue
        kept.append(rec.obj)

    reparented = rebuild_chain(
        [m for m in kept if m.get("uuid")], full_chain)
    report = validate(kept)
    report.update({
        "threads_dropped": len(drops),
        "messages_removed": removed,
        "bytes_removed": removed_bytes,
        "est_tokens_removed": removed_bytes // 4,
        "messages_kept": len(kept),
        "reparented": reparented,
    })
    return report, kept


def apply(db_path: str, live_path: str, drops: List[str],
          backup_dir: str, force: bool = False) -> dict:
    """Backup -> index backup -> gate dropped records -> atomic rewrite ->
    re-index the live file. Refuses active sessions unless forced."""
    live = Path(live_path)
    if not live.exists():
        raise FileNotFoundError(live_path)
    if not backup_dir:
        raise PruneGateError("surgery requires a backup dir — the dropped "
                             "threads must land in the vault first")
    if time.time() - live.stat().st_mtime < 60 and not force:
        raise PruneGateError(
            f"{live.name} was modified "
            f"{int(time.time() - live.stat().st_mtime)}s ago — the session "
            f"looks active. Close it or pass force.")

    snapshot_size = os.path.getsize(live)
    version, backup_path = vault_backup(live, Path(backup_dir))

    from fable.extract import fts_extract_fn
    from fable.indexer import index_vault
    index_vault(db_path, [str(backup_path)], extract_fn=fts_extract_fn,
                rebuild=False)

    # gate: every dropped record must now have an immutable copy
    conn = fdb.connect(db_path)
    try:
        doomed = _dropped_uuids(conn, drops)
        missing = []
        for uuid in doomed:
            row = conn.execute(
                "SELECT 1 FROM copies c JOIN files f ON f.id = c.file_id "
                "WHERE c.uuid = ? AND f.immutable = 1 LIMIT 1",
                (uuid,)).fetchone()
            if row is None:
                missing.append(uuid)
    finally:
        conn.close()
    if missing and not force:
        raise PruneGateError(
            f"{len(missing)} dropped record(s) have no immutable vault copy "
            f"(e.g. {missing[:3]}) — aborting")

    report, kept = plan(db_path, str(live), drops)
    if not report["chain_valid"] and not force:
        raise PruneGateError(
            f"surgery would break the parent chain "
            f"({len(report['chain_broken'])} orphans) — aborting")

    report["chars_written"] = _atomic_write(kept, live, live, snapshot_size)
    report["backup"] = str(backup_path)
    report["backup_version"] = version

    index_vault(db_path, [], live_file=str(live),
                extract_fn=fts_extract_fn)
    report["reindexed"] = True
    fdb.log_op(db_path, "surgery", file=str(live), drops=len(drops),
               removed=report.get("messages_removed"))
    return report
