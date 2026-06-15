"""Index vault generations + live transcript into the Map.

Ground truth is the `copies` table: every copy of every uuid in every file.
`records` holds the denormalized BEST pointer (max length; ties to the
earliest generation). Because all copies are recorded, the best pointer can
always be recomputed when a file is rewritten (prune), deleted, or a fuller
generation arrives late — the index never has to trust a doomed row.

Mutable (live) files are fully re-scanned whenever size or mtime changes —
a prune rewrite shifts offsets, so that file's copies are dropped and
rebuilt, and every uuid whose best pointer touched the file is recomputed
from the surviving copies.
"""
import datetime
import json
import os
import re
from typing import Iterable, Optional, Set

from fable import db as fdb
from fable.jsonl import iter_records, read_span

_GEN_RE = re.compile(r"v(\d+)-")


def _generation_of(path: str) -> int:
    m = _GEN_RE.search(os.path.basename(path))
    return int(m.group(1)) if m else 1 << 30  # live/unknown sorts last


def parse_ts(ts) -> Optional[float]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.datetime.fromisoformat(
            ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _scan_blocks(obj):
    """Light pass over message content: kinds, text size, image presence."""
    msg = obj.get("message")
    kinds, text_bytes, has_images = [], 0, 0
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        kinds.append("str")
        text_bytes += len(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type", "?")
            kinds.append(kind)
            if kind == "image":
                has_images = 1
            elif kind == "text":
                text_bytes += len(block.get("text", ""))
            elif kind == "thinking":
                text_bytes += len(block.get("thinking", ""))
    return ",".join(kinds), text_bytes, has_images


def _file_row(conn, path: str, immutable: bool, session_id=None, project=None):
    """Return (file_id, needs_scan)."""
    st = os.stat(path)
    row = conn.execute(
        "SELECT id, size, mtime FROM files WHERE path = ?", (path,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO files(path, label, generation, immutable, size, mtime,"
            " session_id, project) VALUES(?,?,?,?,?,?,?,?)",
            (path, os.path.basename(path), _generation_of(path),
             1 if immutable else 0, st.st_size, st.st_mtime,
             session_id, project))
        return cur.lastrowid, True
    file_id, size, mtime = row
    unchanged = size == st.st_size and abs(mtime - st.st_mtime) < 1e-6
    if unchanged:
        return file_id, False
    conn.execute("UPDATE files SET size = ?, mtime = ? WHERE id = ?",
                 (st.st_size, st.st_mtime, file_id))
    return file_id, True


def _upsert_record(conn, obj, file_id, lineno, offset, length,
                   extract_fn=None, session_id=None):
    """Make `records` point at this copy and refresh its FTS row."""
    uuid = obj["uuid"]
    old = conn.execute("SELECT fts_rowid FROM records WHERE uuid = ?",
                       (uuid,)).fetchone()
    old_fts = old[0] if old else None
    kinds, text_bytes, has_images = _scan_blocks(obj)
    msg = obj.get("message")
    model = msg.get("model") if isinstance(msg, dict) else None
    if model == "<synthetic>":
        model = None
    usage = msg.get("usage") if isinstance(msg, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    fts_rowid = None
    if extract_fn is not None:
        fts_rowid = extract_fn(conn, uuid, obj, old_fts)
    conn.execute(
        "INSERT INTO records(uuid, prompt_id, parent_uuid, type, role, ts,"
        " is_sidechain, file_id, lineno, offset, length, fidelity,"
        " text_bytes, has_images, block_kinds, session_id, source_uuid,"
        " fts_rowid, ts_epoch, model, in_tokens, out_tokens,"
        " cache_read_tokens, cache_write_tokens) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(uuid) DO UPDATE SET "
        " prompt_id=excluded.prompt_id, parent_uuid=excluded.parent_uuid,"
        " type=excluded.type, role=excluded.role, ts=excluded.ts,"
        " is_sidechain=excluded.is_sidechain, file_id=excluded.file_id,"
        " lineno=excluded.lineno, offset=excluded.offset,"
        " length=excluded.length, fidelity=excluded.fidelity,"
        " text_bytes=excluded.text_bytes, has_images=excluded.has_images,"
        " block_kinds=excluded.block_kinds, session_id=excluded.session_id,"
        " source_uuid=excluded.source_uuid, fts_rowid=excluded.fts_rowid,"
        " ts_epoch=excluded.ts_epoch, model=excluded.model,"
        " in_tokens=excluded.in_tokens, out_tokens=excluded.out_tokens,"
        " cache_read_tokens=excluded.cache_read_tokens,"
        " cache_write_tokens=excluded.cache_write_tokens",
        (uuid, obj.get("promptId"), obj.get("parentUuid"), obj.get("type"),
         (obj.get("message") or {}).get("role")
         if isinstance(obj.get("message"), dict) else None,
         obj.get("timestamp"), 1 if obj.get("isSidechain") else 0,
         file_id, lineno, offset, length, length,
         text_bytes, has_images, kinds,
         obj.get("sessionId") or session_id,
         obj.get("sourceToolAssistantUUID"),
         fts_rowid, parse_ts(obj.get("timestamp")), model,
         usage.get("input_tokens"), usage.get("output_tokens"),
         usage.get("cache_read_input_tokens"),
         usage.get("cache_creation_input_tokens")))


def _index_one(conn, obj, file_id, lineno, offset, length, stats,
               extract_fn=None, session_id=None):
    """Index a single record: record its copy, and (if it's the fullest copy)
    its denormalized pointer + FTS. Returns the uuid, or None if skipped."""
    uuid = obj.get("uuid")
    if not uuid and obj.get("type") == "file-history-snapshot":
        # rewind checkpoints: tiny records pointing at full on-disk file
        # backups (~/.claude/file-history) — gold anchors for file
        # time-travel. Synthesize a stable identity.
        snap = obj.get("snapshot") or {}
        if snap.get("trackedFileBackups"):
            uuid = (f"fhs:{obj.get('messageId', '?')}:"
                    f"{snap.get('timestamp', '?')}")
            obj["uuid"] = uuid
            obj.setdefault("timestamp", snap.get("timestamp"))
    if not uuid:
        stats["skipped_no_uuid"] += 1
        return None
    stats["records_seen"] += 1

    conn.execute(
        "INSERT INTO copies(uuid, file_id, lineno, offset, length) "
        "VALUES(?,?,?,?,?) ON CONFLICT(uuid, file_id) DO UPDATE SET"
        " lineno=excluded.lineno, offset=excluded.offset,"
        " length=excluded.length WHERE excluded.length >= copies.length",
        (uuid, file_id, lineno, offset, length))

    existing = conn.execute(
        "SELECT fidelity FROM records WHERE uuid = ?", (uuid,)).fetchone()
    if existing is not None and length <= existing[0]:
        return uuid  # safe: the fuller copy is recorded in `copies`
    _upsert_record(conn, obj, file_id, lineno, offset, length,
                   extract_fn=extract_fn, session_id=session_id)
    stats["records_indexed"] += 1
    return uuid


def _index_file(conn, path: str, file_id: int, stats, extract_fn=None,
                session_id=None):
    for rec in iter_records(path, on_error=lambda ln, e: stats.__setitem__(
            "parse_errors", stats["parse_errors"] + 1)):
        obj = rec.obj
        if not stats.get("session_id") and obj.get("sessionId"):
            stats["session_id"] = obj["sessionId"]
        _index_one(conn, obj, file_id, rec.lineno, rec.offset, rec.length,
                   stats, extract_fn=extract_fn, session_id=session_id)


def _drop_fts(conn, uuid):
    row = conn.execute("SELECT fts_rowid FROM records WHERE uuid = ?",
                       (uuid,)).fetchone()
    if row and row[0]:
        conn.execute("DELETE FROM fts WHERE rowid = ?", (row[0],))


def _recompute_best(conn, uuids: Set[str], extract_fn=None, stats=None):
    """Re-point each uuid at its best surviving copy (or forget it)."""
    for uuid in uuids:
        best = conn.execute(
            "SELECT c.file_id, c.lineno, c.offset, c.length, f.path "
            "FROM copies c JOIN files f ON f.id = c.file_id "
            "WHERE c.uuid = ? ORDER BY c.length DESC, f.generation ASC, "
            "c.file_id ASC LIMIT 1", (uuid,)).fetchone()
        if best is None:
            _drop_fts(conn, uuid)
            conn.execute("DELETE FROM records WHERE uuid = ?", (uuid,))
            conn.execute("DELETE FROM citations WHERE from_uuid = ?", (uuid,))
            if stats is not None:
                stats["records_forgotten"] += 1
            continue
        file_id, lineno, offset, length, path = best
        cur = conn.execute(
            "SELECT file_id, offset, length FROM records WHERE uuid = ?",
            (uuid,)).fetchone()
        if cur == (file_id, offset, length):
            continue
        try:
            raw = read_span(path, offset, length)
            obj = json.loads(raw.decode("utf-8", "surrogateescape"))
        except (OSError, json.JSONDecodeError):
            if stats is not None:
                stats["recompute_read_errors"] += 1
            continue
        # records with a SYNTHESIZED identity (file-history-snapshot, whose
        # raw obj has no "uuid" key) carry it only in the index, not on disk —
        # stamp the canonical uuid back on so _upsert_record never KeyErrors.
        obj["uuid"] = uuid
        _upsert_record(conn, obj, file_id, lineno, offset, length,
                       extract_fn=extract_fn,
                       session_id=obj.get("sessionId"))
        if stats is not None:
            stats["records_repointed"] += 1


def _forget_missing_files(conn, affected: Set[str], stats):
    """Drop files that vanished from disk; their copies stop counting."""
    for file_id, path in conn.execute(
            "SELECT id, path FROM files").fetchall():
        if os.path.exists(path):
            continue
        for (uuid,) in conn.execute(
                "SELECT uuid FROM records WHERE file_id = ?", (file_id,)):
            affected.add(uuid)
        conn.execute("DELETE FROM copies WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        stats["files_forgotten"] += 1


def resolve_thread_membership(conn):
    """Assign every record to a thread.

    Real transcripts put promptId ONLY on user records; assistant and tool
    turns belong to a prompt transitively via parentUuid (and sidechain
    records via sourceToolAssistantUUID). Walk each unresolved record up
    the graph to the nearest explicit promptId, iteratively with memoization
    (chains run tens of thousands deep — no recursion).
    """
    parent, explicit, source, fts_rowids = {}, {}, {}, {}
    for uuid, p, pid, src, fr in conn.execute(
            "SELECT uuid, parent_uuid, prompt_id, source_uuid, fts_rowid "
            "FROM records"):
        parent[uuid] = p
        explicit[uuid] = pid
        source[uuid] = src
        fts_rowids[uuid] = fr

    memo = {}

    def resolve(start):
        chain = []
        cur, local = start, set()
        result = None
        while True:
            if cur is None or cur in local:
                break
            if cur in memo:
                result = memo[cur]
                break
            pid = explicit.get(cur)
            if pid:
                result = pid
                break
            local.add(cur)
            chain.append(cur)
            nxt = parent.get(cur)
            if nxt is None or nxt not in parent:
                nxt = source.get(cur)
            cur = nxt
        for c in chain:
            memo[c] = result
        return result

    updates = [(resolve(u), u) for u in parent
               if not explicit[u] and resolve(u)]
    conn.executemany("UPDATE records SET prompt_id = ? WHERE uuid = ?",
                     updates)
    fts_updates = [(pid, fts_rowids[u]) for pid, u in updates
                   if fts_rowids.get(u)]
    conn.executemany("UPDATE fts SET prompt_id = ? WHERE rowid = ?",
                     fts_updates)
    return len(updates)


def rebuild_threads(conn):
    resolve_thread_membership(conn)
    conn.execute("DELETE FROM threads")
    conn.execute("""
        INSERT INTO threads(prompt_id, first_ts, last_ts, turn_count,
                            text_bytes, est_tokens, first_uuid, leaf_uuid,
                            session_id, sidechain_turns, models)
        SELECT prompt_id, MIN(ts), MAX(ts), COUNT(*),
               SUM(text_bytes), SUM(text_bytes) / 4,
               (SELECT uuid FROM records r2 WHERE r2.prompt_id = r.prompt_id
                ORDER BY r2.ts_epoch ASC, r2.lineno ASC LIMIT 1),
               (SELECT uuid FROM records r3 WHERE r3.prompt_id = r.prompt_id
                ORDER BY r3.ts_epoch DESC, r3.lineno DESC LIMIT 1),
               MAX(session_id),
               SUM(is_sidechain),
               (SELECT GROUP_CONCAT(DISTINCT model) FROM records r4
                WHERE r4.prompt_id = r.prompt_id AND r4.model IS NOT NULL)
        FROM records r
        WHERE prompt_id IS NOT NULL
        GROUP BY prompt_id
    """)


def reconcile(conn):
    """Safety net: no FTS row or citation may outlive its record."""
    conn.execute("DELETE FROM fts WHERE uuid NOT IN "
                 "(SELECT uuid FROM records)")
    conn.execute("DELETE FROM citations WHERE from_uuid NOT IN "
                 "(SELECT uuid FROM records)")


def index_vault(db_path: str,
                vault_files: Iterable[str],
                live_file: Optional[str] = None,
                extract_fn=None,
                session_id: Optional[str] = None,
                project: Optional[str] = None,
                rebuild: bool = True) -> dict:
    """Index immutable vault generations plus an optional live transcript."""
    stats = {"records_seen": 0, "records_indexed": 0, "skipped_no_uuid": 0,
             "parse_errors": 0, "files_scanned": 0, "files_cached": 0,
             "files_forgotten": 0, "records_repointed": 0,
             "records_forgotten": 0, "recompute_read_errors": 0}
    conn = fdb.connect(db_path, create=True)
    try:
        affected: Set[str] = set()
        _forget_missing_files(conn, affected, stats)

        ordered = sorted(vault_files, key=_generation_of)
        plan = [(p, True) for p in ordered]
        if live_file:
            plan.append((live_file, False))

        for path, immutable in plan:
            file_id, needs_scan = _file_row(conn, path, immutable,
                                            session_id=session_id,
                                            project=project)
            if not needs_scan:
                stats["files_cached"] += 1
                continue
            # any rescan (mutable rewrite or changed file) invalidates this
            # file's copies and every best-pointer that relied on them
            for (uuid,) in conn.execute(
                    "SELECT uuid FROM records WHERE file_id = ?", (file_id,)):
                affected.add(uuid)
            conn.execute("DELETE FROM copies WHERE file_id = ?", (file_id,))
            stats["files_scanned"] += 1
            _index_file(conn, path, file_id, stats, extract_fn=extract_fn,
                        session_id=session_id)

        _recompute_best(conn, affected, extract_fn=extract_fn, stats=stats)

        if rebuild:
            rebuild_threads(conn)
        if stats.get("session_id"):
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) "
                "VALUES('session_id', ?)", (stats["session_id"],))
        reconcile(conn)
        conn.commit()
    finally:
        conn.close()
    return stats


# ── incremental turn-boundary indexing (roadmap #1) ──────────────────────
#
# An ACTIVE session's live file outgrows the index between triggers. Re-running
# index_vault per turn is too slow — _index_file re-parses the whole file and
# rebuild_threads re-resolves membership over the ENTIRE db. So instead: read
# only the bytes appended since last time, index those records (pointers, no
# copy), resolve their thread membership against already-indexed ancestors, and
# refresh ONLY the touched threads. O(new records), runs inside a Stop hook.


def _index_tail(conn, path, file_id, start_offset, start_lineno, stats,
                extract_fn=None):
    """Index only records appended after start_offset. Returns
    (new_uuids, safe_end): safe_end is the end of the last fully-parsed line,
    so a trailing partial line (mid-write) is left for the next pass."""
    dec = json.JSONDecoder()
    new_uuids, safe_end = [], start_offset
    with open(path, "rb") as f:
        f.seek(start_offset)
        pos, lineno = start_offset, start_lineno
        for raw in f:
            lineno += 1
            line_start = pos
            pos += len(raw)
            if not raw.strip():
                safe_end = pos
                continue
            text = raw.decode("utf-8", errors="surrogateescape")
            p, line_ok = 0, True
            while p < len(text):
                while p < len(text) and text[p] in " \t\r\n":
                    p += 1
                if p >= len(text):
                    break
                try:
                    obj, end = dec.raw_decode(text, p)
                except json.JSONDecodeError:
                    stats["parse_errors"] += 1
                    line_ok = False
                    break  # trailing partial — stop, re-read this line later
                byte_off = line_start + len(
                    text[:p].encode("utf-8", "surrogateescape"))
                byte_len = len(
                    text[p:end].encode("utf-8", "surrogateescape"))
                if not stats.get("session_id") and obj.get("sessionId"):
                    stats["session_id"] = obj["sessionId"]
                uuid = _index_one(conn, obj, file_id, lineno, byte_off,
                                  byte_len, stats, extract_fn=extract_fn)
                if uuid:
                    new_uuids.append(uuid)
                p = end
            if line_ok:
                safe_end = pos
            else:
                break
    return new_uuids, safe_end


def _resolve_new(conn, new_uuids):
    """Resolve prompt_id for newly-appended records by walking each up to its
    nearest already-indexed ancestor that has one. Returns the set of threads
    (prompt_ids) touched. Cheap: every ancestor is already resolved."""
    touched = set()
    for uuid in new_uuids:
        row = conn.execute(
            "SELECT prompt_id, parent_uuid, source_uuid, fts_rowid "
            "FROM records WHERE uuid = ?", (uuid,)).fetchone()
        if not row:
            continue
        pid, parent, source, fts_rowid = row
        if pid:
            touched.add(pid)
            continue
        cur, seen, resolved = (parent or source), set(), None
        while cur and cur not in seen:
            seen.add(cur)
            anc = conn.execute(
                "SELECT prompt_id, parent_uuid, source_uuid "
                "FROM records WHERE uuid = ?", (cur,)).fetchone()
            if not anc:
                break
            if anc[0]:
                resolved = anc[0]
                break
            cur = anc[1] or anc[2]
        if resolved:
            conn.execute("UPDATE records SET prompt_id = ? WHERE uuid = ?",
                         (resolved, uuid))
            if fts_rowid:
                conn.execute("UPDATE fts SET prompt_id = ? WHERE rowid = ?",
                             (resolved, fts_rowid))
            touched.add(resolved)
    return touched


_THREAD_AGG = """
    INSERT INTO threads(prompt_id, first_ts, last_ts, turn_count,
                        text_bytes, est_tokens, first_uuid, leaf_uuid,
                        session_id, sidechain_turns, models)
    SELECT prompt_id, MIN(ts), MAX(ts), COUNT(*),
           SUM(text_bytes), SUM(text_bytes) / 4,
           (SELECT uuid FROM records r2 WHERE r2.prompt_id = r.prompt_id
            ORDER BY r2.ts_epoch ASC, r2.lineno ASC LIMIT 1),
           (SELECT uuid FROM records r3 WHERE r3.prompt_id = r.prompt_id
            ORDER BY r3.ts_epoch DESC, r3.lineno DESC LIMIT 1),
           MAX(session_id), SUM(is_sidechain),
           (SELECT GROUP_CONCAT(DISTINCT model) FROM records r4
            WHERE r4.prompt_id = r.prompt_id AND r4.model IS NOT NULL)
    FROM records r WHERE prompt_id = ? GROUP BY prompt_id
"""


def _refresh_threads(conn, prompt_ids):
    """Re-aggregate only the given threads, not the whole table."""
    for pid in prompt_ids:
        conn.execute("DELETE FROM threads WHERE prompt_id = ?", (pid,))
        conn.execute(_THREAD_AGG, (pid,))


def index_live_tail(db_path: str, live_path: str, extract_fn=None) -> dict:
    """Turn-boundary fast path: index only the turns appended to a live
    transcript since it was last indexed. Falls back to a full index_vault
    when the file shrank / was rewritten (prune, compaction, surgery) or isn't
    known yet. Built to run inside a Stop / UserPromptSubmit hook."""
    if not (live_path and os.path.exists(live_path)):
        return {"mode": "absent", "new_records": 0}
    cur_size = os.path.getsize(live_path)
    conn = fdb.connect(db_path, create=True)
    try:
        row = conn.execute("SELECT id, size FROM files WHERE path = ?",
                           (live_path,)).fetchone()
    finally:
        conn.close()
    if row is None or cur_size < row[1]:
        out = index_vault(db_path, [], live_file=live_path,
                          extract_fn=extract_fn)
        out["mode"] = "full"
        return out
    file_id, last_size = row
    if cur_size == last_size:
        return {"mode": "nochange", "new_records": 0}

    conn = fdb.connect(db_path, create=True)
    try:
        stats = {"records_seen": 0, "records_indexed": 0,
                 "skipped_no_uuid": 0, "parse_errors": 0}
        max_lineno = conn.execute(
            "SELECT COALESCE(MAX(lineno), 0) FROM copies WHERE file_id = ?",
            (file_id,)).fetchone()[0]
        new_uuids, safe_end = _index_tail(conn, live_path, file_id, last_size,
                                          max_lineno, stats, extract_fn)
        st = os.stat(live_path)
        conn.execute("UPDATE files SET size = ?, mtime = ? WHERE id = ?",
                     (safe_end, st.st_mtime, file_id))
        touched = _resolve_new(conn, new_uuids)
        _refresh_threads(conn, touched)
        conn.commit()
        stats.update(mode="tail", new_records=len(new_uuids),
                     threads_touched=len(touched))
        return stats
    finally:
        conn.close()
