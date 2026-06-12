"""fable vault gc — reclaim redundant vault generations, provably lossless.

A generation is deletable iff EVERY record copy it holds exists at
equal-or-better fidelity in some other retained file. Greedy biggest-first,
recomputed after each removal so mutually-redundant pairs are never both
deleted. Files are MOVED to a trash directory, never rm'd — you delete the
trash yourself after you're satisfied.
"""
import os
import shutil
from typing import List

from fable import db as fdb


def _deletable(conn, file_id: int, excluded: set) -> bool:
    ph = ",".join("?" * len(excluded)) or "0"
    row = conn.execute(f"""
        SELECT 1 FROM copies c WHERE c.file_id = ?
        AND NOT EXISTS (
            SELECT 1 FROM copies c2
            JOIN files f2 ON f2.id = c2.file_id AND f2.immutable = 1
            WHERE c2.uuid = c.uuid AND c2.length >= c.length
            AND c2.file_id != ? AND c2.file_id NOT IN ({ph}))
        LIMIT 1""", [file_id, file_id, *excluded]).fetchone()
    return row is None


def plan_gc(db_path: str) -> List[dict]:
    """Greedy: which immutable generations are fully redundant?"""
    conn = fdb.connect(db_path)
    try:
        files = conn.execute(
            "SELECT id, path, size FROM files WHERE immutable = 1 "
            "ORDER BY size DESC").fetchall()
        doomed: set = set()
        out = []
        changed = True
        while changed:
            changed = False
            for fid, path, size in files:
                if fid in doomed or not os.path.exists(path):
                    continue
                if _deletable(conn, fid, doomed):
                    doomed.add(fid)
                    out.append({"file_id": fid, "path": path,
                                "size": size or 0})
                    changed = True
        return out
    finally:
        conn.close()


def apply_gc(db_path: str, plan: List[dict], trash_dir: str) -> dict:
    os.makedirs(trash_dir, exist_ok=True)
    moved, freed = 0, 0
    conn = fdb.connect(db_path)
    try:
        for item in plan:
            src = item["path"]
            if not os.path.exists(src):
                continue
            # final re-verification against current state before touching it
            if not _deletable(conn, item["file_id"],
                              {p["file_id"] for p in plan
                               if p["file_id"] != item["file_id"]
                               and not os.path.exists(p["path"])} ):
                continue
            dest = os.path.join(trash_dir, f"{item['file_id']}-"
                                + os.path.basename(src))
            shutil.move(src, dest)
            moved += 1
            freed += item["size"]
            # forget the file; best pointers recompute from survivors
            affected = [u for (u,) in conn.execute(
                "SELECT uuid FROM records WHERE file_id = ?",
                (item["file_id"],))]
            conn.execute("DELETE FROM copies WHERE file_id = ?",
                         (item["file_id"],))
            conn.execute("DELETE FROM files WHERE id = ?",
                         (item["file_id"],))
            conn.commit()
            if affected:
                from fable.indexer import _recompute_best
                from fable.extract import fts_extract_fn
                stats = {"records_repointed": 0, "records_forgotten": 0,
                         "recompute_read_errors": 0}
                _recompute_best(conn, set(affected),
                                extract_fn=fts_extract_fn, stats=stats)
                conn.commit()
                if stats["records_forgotten"]:
                    raise RuntimeError(
                        f"GC INVARIANT VIOLATED on {src} — restore from "
                        f"{dest} and report this bug")
    finally:
        conn.close()
    fdb.log_op(db_path, "vault_gc", moved=moved, freed_bytes=freed)
    return {"moved": moved, "freed_bytes": freed, "trash": trash_dir}


def cmd_vault(args) -> int:
    import json
    plan = plan_gc(args.db)
    total = sum(p["size"] for p in plan)
    if not getattr(args, "apply", False):
        print(json.dumps({"reclaimable_files": len(plan),
                          "reclaimable_bytes": total,
                          "reclaimable_gb": round(total / 1e9, 2),
                          "note": "dry run — pass --apply to move these to "
                                  "the trash dir (never deleted)"},
                         indent=2))
        return 0
    trash = args.trash or os.path.join(
        os.path.dirname(os.path.abspath(args.db)), "vault-trash")
    result = apply_gc(args.db, plan, trash)
    print(json.dumps(result, indent=2))
    return 0
