"""Discover and index every Claude Code project: live transcripts from
~/.claude/projects plus backup vault generations from pruner backup roots.

Read-only with respect to everything it scans — the only thing written is
the fable index database.
"""
import datetime
import glob
import json
import os
from typing import List, Optional

from fable import db as fdb
from fable.extract import fts_extract_fn
from fable.indexer import index_vault, rebuild_threads
from fable.terms import index_terms

DEFAULT_PROJECTS_DIR = os.path.join(
    os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")),
    "projects")
# vault read-roots come from fable.paths.backup_roots() — no hardcoded paths.


def project_label(dirname: str) -> str:
    """'-Users-x-Desktop-...-PineScript' -> 'PineScript' (fallback: as-is)."""
    seg = dirname.rstrip("-").rsplit("-", 1)[-1]
    return seg or dirname


def is_fable_generated(jsonl_path: str, max_lines: int = 30) -> bool:
    """Defense-in-depth against indexing our own card-generation sessions:
    every fable prompt carries a FABLE-GENERATED marker."""
    try:
        with open(jsonl_path) as f:
            for i, line in enumerate(f):
                if i > max_lines:
                    break
                if "FABLE-GENERATED" in line:
                    return True
    except OSError:
        pass
    return False


def session_title(jsonl_path: str, max_lines: int = 200) -> Optional[str]:
    title = None
    try:
        with open(jsonl_path) as f:
            for i, line in enumerate(f):
                if i > max_lines:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "custom-title":
                    title = obj.get("customTitle")
    except OSError:
        pass
    return title


def vaults_for_session(session_id: str, backup_roots: List[str]) -> List[str]:
    found = []
    for root in backup_roots:
        found.extend(glob.glob(os.path.join(root, "*", session_id, "*.jsonl")))
    return sorted(found)


def discover(db_path: str,
             projects_dir: str = DEFAULT_PROJECTS_DIR,
             backup_roots: Optional[List[str]] = None,
             project_filter: Optional[str] = None,
             include_vaults: bool = True,
             progress=None) -> dict:
    if backup_roots is None:
        from fable.paths import backup_roots as _default_roots
        backup_roots = _default_roots()
    backup_roots = [r for r in backup_roots if os.path.isdir(r)]

    totals = {"sessions": 0, "vault_files": 0, "records_indexed": 0,
              "parse_errors": 0}
    conn = fdb.connect(db_path, create=True)
    conn.close()

    if not os.path.isdir(projects_dir):
        raise FileNotFoundError(f"projects dir not found: {projects_dir}")

    try:
        _scan_projects(db_path, projects_dir, backup_roots, project_filter,
                       include_vaults, progress, totals)
    finally:
        # even on interrupt, leave the index searchable: resolve thread
        # membership and rebuild aggregates for whatever was committed
        conn = fdb.connect(db_path, create=True)
        rebuild_threads(conn)
        conn.commit()
        conn.close()
        tstats = index_terms(db_path)
        totals["terms"] = tstats["terms"]
    return totals


def _scan_projects(db_path, projects_dir, backup_roots, project_filter,
                   include_vaults, progress, totals):
    seen_sessions = set()
    for proj_dir in sorted(os.listdir(projects_dir)):
        full = os.path.join(projects_dir, proj_dir)
        if not os.path.isdir(full):
            continue
        if project_filter and project_filter.lower() not in proj_dir.lower():
            continue
        label = project_label(proj_dir)
        for live in sorted(glob.glob(os.path.join(full, "*.jsonl"))):
            session_id = os.path.splitext(os.path.basename(live))[0]
            if is_fable_generated(live):
                seen_sessions.add(session_id)
                continue
            vaults = (vaults_for_session(session_id, backup_roots)
                      if include_vaults else [])
            if progress:
                progress(f"{label}/{session_id} "
                         f"({len(vaults)} vault files)")
            stats = index_vault(db_path, vaults, live_file=live,
                                extract_fn=fts_extract_fn,
                                session_id=session_id, project=label,
                                rebuild=False)
            totals["sessions"] += 1
            totals["vault_files"] += len(vaults)
            totals["records_indexed"] += stats["records_indexed"]
            totals["parse_errors"] += stats["parse_errors"]

            conn = fdb.connect(db_path)
            conn.execute(
                "INSERT OR REPLACE INTO sessions(session_id, project, title,"
                " live_path, indexed_at) VALUES(?,?,?,?,?)",
                (session_id, label, session_title(live), live,
                 datetime.datetime.now(datetime.timezone.utc).isoformat()))
            conn.commit()
            conn.close()
            seen_sessions.add(session_id)

    if include_vaults:
        _scan_orphaned_vaults(db_path, backup_roots, seen_sessions,
                              project_filter, progress, totals)


def _scan_orphaned_vaults(db_path, backup_roots, seen_sessions,
                          project_filter, progress, totals):
    """Vault generations whose live transcript was deleted still hold
    history — index them too (session dir name = session id)."""
    for root in backup_roots:
        for proj in sorted(os.listdir(root)):
            proj_dir = os.path.join(root, proj)
            if not os.path.isdir(proj_dir):
                continue
            if project_filter and project_filter.lower() not in proj.lower():
                continue
            for session_id in sorted(os.listdir(proj_dir)):
                sdir = os.path.join(proj_dir, session_id)
                if session_id in seen_sessions or not os.path.isdir(sdir):
                    continue
                vaults = sorted(glob.glob(os.path.join(sdir, "*.jsonl")))
                if not vaults:
                    continue
                if progress:
                    progress(f"{proj}/{session_id} (orphaned vault, "
                             f"{len(vaults)} files)")
                stats = index_vault(db_path, vaults, live_file=None,
                                    extract_fn=fts_extract_fn,
                                    session_id=session_id, project=proj,
                                    rebuild=False)
                totals["sessions"] += 1
                totals["vault_files"] += len(vaults)
                totals["records_indexed"] += stats["records_indexed"]
                conn = fdb.connect(db_path)
                conn.execute(
                    "INSERT OR IGNORE INTO sessions(session_id, project,"
                    " title, live_path, indexed_at) VALUES(?,?,?,?,?)",
                    (session_id, proj, None, None,
                     datetime.datetime.now(
                         datetime.timezone.utc).isoformat()))
                conn.commit()
                conn.close()
