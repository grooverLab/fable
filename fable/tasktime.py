"""Task ledger — the durable backlog reconstructed from Task tool calls.

Claude Code's task list is ephemeral (per-session, lost on compaction) and its
ids are never written into TaskCreate's input — they live only in the tool
RESULT ("Task #N created successfully: <subject>"). Pruning stubs tool_results
to "[pruned]", but fable keeps every generation of every session, so a result
that was stubbed in one generation survives intact in another (the fat-union).
We harvest those results across ALL generations/sources to recover exact ids
(~87% of the corpus), interpolate the rest from anchors (ids are sequential),
then join TaskUpdate status by (session, id, ts-window) so list resets — where
a project's list is cleared and #1 starts over — resolve to the right task.

The WORK project a task belongs to is derived from the files its session edited
(see work_projects / #61), not the session's cwd — the two differ when you work
on project B from project A's directory.
"""
import functools
import hashlib
import json
import os
import re
from collections import defaultdict

from fable import db as fdb

_SUBMARKERS = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod",
               "CLAUDE.md", "deno.json")
_RESULT_RE = re.compile(r'Task #(\d+) (?:created|updated)[^:]*:\s*(.*)', re.S)


@functools.lru_cache(maxsize=4096)
def _root_of_dir(d):
    """Project name for a directory: the .git repo root if any (walking up —
    .git only sits at repo roots, so it beats an inner package.json/src), else
    the outermost dir carrying a build marker, else the dir's own name."""
    outer, cur = None, d
    for _ in range(14):
        if not cur or cur == "/":
            break
        try:
            entries = set(os.listdir(cur))
        except OSError:
            entries = set()
        if ".git" in entries:
            return os.path.basename(cur)
        if entries & set(_SUBMARKERS):
            outer = os.path.basename(cur)
        cur = os.path.dirname(cur)
    if outer:
        return outer
    parts = [p for p in d.split("/") if p]
    return parts[-1] if parts else None


def _project_root(path):
    return _root_of_dir(os.path.dirname(path))


_SESSION_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def _session_of(path, sid):
    """Resolve the real session id: files.session_id if set, else the uuid
    embedded in the path. Backup/duplicate copies (…/backups/<proj>/<uuid>/vN)
    are indexed with NULL session_id but carry the uuid as a path component, so
    they'd otherwise lose their project + completions."""
    if sid:
        return sid
    for part in reversed(path.split("/")):
        if _SESSION_RE.fullmatch(part.replace(".jsonl", "")):
            return part.replace(".jsonl", "")
    m = _SESSION_RE.search(path)
    return m.group(0) if m else None


def work_projects(db_path, conn=None):
    """session_id -> dominant work-project (project-marker root of edited files,
    weighted by edit volume). Sessions that edited no files are absent."""
    own = conn is None
    conn = conn or fdb.connect(db_path)
    try:
        roots = defaultdict(lambda: defaultdict(int))
        for sid, term, cnt in conn.execute(
                """SELECT th.session_id, t.term, COALESCE(t.count, 1)
                   FROM terms t JOIN threads th ON th.prompt_id = t.prompt_id
                   WHERE t.kind = 'target' AND t.term LIKE '%/%'"""):
            proj = _project_root(term)
            if proj:
                roots[sid][proj] += cnt
        return {sid: max(c, key=c.get) for sid, c in roots.items()}
    finally:
        if own:
            conn.close()


def _blocks(rec):
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return content if isinstance(content, list) else []


def _result_text(block):
    c = block.get("content")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") if isinstance(x, dict) else str(x)
                     for x in c)
    return str(c)


def scan_sources(db_path, conn=None):
    """Parse every indexed source file once. Returns (creates, updates), each
    deduped by tool_use_id across generations. A create carries its exact task
    id whenever the TaskCreate result survived intact in ANY generation."""
    own = conn is None
    conn = conn or fdb.connect(db_path)
    try:
        wp = work_projects(db_path, conn)
        cwd = dict(conn.execute("SELECT session_id, project FROM sessions"))
        creates, updates = {}, {}        # tool_use_id -> dict
        for path, sid in conn.execute("SELECT path, session_id FROM files"):
            sid = _session_of(path, sid)
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8", errors="surrogateescape") as f:
                    blob = f.read()
            except OSError:
                continue
            if "TaskCreate" not in blob and "TaskUpdate" not in blob:
                continue
            recs = []
            for line in blob.splitlines():
                line = line.strip()
                if line:
                    try:
                        recs.append(json.loads(line))
                    except ValueError:
                        pass
            results = {}
            for r in recs:
                for b in _blocks(r):
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        results[b.get("tool_use_id")] = _result_text(b)
            wpp, cwdp = wp.get(sid) or cwd.get(sid), cwd.get(sid)
            for r in recs:
                ts = r.get("timestamp", "")
                for b in _blocks(r):
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    name, inp, tu = (b.get("name") or "", b.get("input"),
                                     b.get("id"))
                    if not isinstance(inp, dict):
                        continue
                    if name == "TaskCreate":
                        res = results.get(tu, "")
                        m = (_RESULT_RE.search(res)
                             if res and res != "[pruned]" else None)
                        prev = creates.get(tu)
                        if prev is None or (prev["task_id"] is None and m):
                            creates[tu] = {
                                "ts": ts, "session": sid, "project": wpp,
                                "cwd": cwdp, "uuid": r.get("uuid"),
                                "desc": (inp.get("description")
                                         or inp.get("subject") or ""),
                                "task_id": int(m.group(1)) if m else None}
                    elif name == "TaskUpdate":
                        # inputs get pruned to {"description":"pruned"}; recover
                        # the real taskId/status from whichever generation kept
                        # them (fat-union), preferring a copy that has them.
                        tid = str(inp.get("taskId") or "")
                        prev = updates.get(tu)
                        if prev is None or (not prev["task_id"] and tid):
                            updates[tu] = {"ts": ts, "session": sid,
                                           "task_id": tid,
                                           "status": inp.get("status")}
        # M3 provenance: map each create's record uuid -> its thread prompt_id
        uids = [c.get("uuid") for c in creates.values() if c.get("uuid")]
        pid = {}
        for i in range(0, len(uids), 400):
            chunk = uids[i:i + 400]
            ph = ",".join("?" * len(chunk))
            pid.update(conn.execute(
                f"SELECT uuid, prompt_id FROM records WHERE uuid IN ({ph})",
                chunk))
        for c in creates.values():
            c["prompt_id"] = pid.get(c.get("uuid"))
        return list(creates.values()), list(updates.values())
    finally:
        if own:
            conn.close()


def _fill_ids(cs):
    """cs = one session's creates, sorted by ts. Fill missing ids from the
    recovered anchors: ids increment by 1 per create, so an un-recovered create
    is prev+1, and a recovered anchor re-snaps the run (a drop marks a reset).
    Marks interpolated ids with _interp so the display can flag them."""
    last = None
    for c in cs:
        if c["task_id"] is not None:
            last = c["task_id"]
        elif last is not None:
            last += 1
            c["task_id"], c["_interp"] = last, True
    first = next((i for i, c in enumerate(cs) if c["task_id"] is not None), None)
    if first is not None:                       # back-fill leading gap
        base = cs[first]["task_id"]
        for j in range(first - 1, -1, -1):
            base -= 1
            if base >= 1:
                cs[j]["task_id"], cs[j]["_interp"] = base, True


def ledger(db_path):
    """Fuse creates + updates into a task ledger keyed by exact ids.
    Returns {tasks, created, open, completed, by_project, recovered, ...}."""
    conn = fdb.connect(db_path)
    try:
        creates, updates = scan_sources(db_path, conn)
    finally:
        conn.close()
    recovered = sum(1 for c in creates if c["task_id"] is not None)

    by_sess = defaultdict(list)
    for c in creates:
        by_sess[c["session"]].append(c)
    tasks = []
    for sid, cs in by_sess.items():
        cs.sort(key=lambda c: c["ts"])
        _fill_ids(cs)
        for c in cs:
            desc = c["desc"].strip()
            tasks.append({
                "id": c["task_id"], "interp": c.get("_interp", False),
                "ts": c["ts"], "session": sid, "project": c["project"],
                "prompt_id": c.get("prompt_id"), "cwd": c.get("cwd"),
                "subject": desc.splitlines()[0][:90] if desc else "(empty)"})

    # status join: each update applies to the most recent same-(session,id)
    # create at or before it — so a reset's #5 doesn't inherit the old #5.
    upd = defaultdict(list)
    for u in updates:
        if u["task_id"].isdigit() and u["status"]:
            upd[(u["session"], int(u["task_id"]))].append((u["ts"], u["status"]))
    for v in upd.values():
        v.sort()
    creates_by_key = defaultdict(list)
    for t in tasks:
        if t["id"] is not None:
            creates_by_key[(t["session"], t["id"])].append(t)
    for key, clist in creates_by_key.items():
        clist.sort(key=lambda t: t["ts"])
        ups = upd.get(key, [])
        for i, t in enumerate(clist):
            hi = clist[i + 1]["ts"] if i + 1 < len(clist) else "~"
            window = [s for uts, s in ups if t["ts"] <= uts < hi]
            t["status"] = window[-1] if window else "pending"
    for t in tasks:
        t.setdefault("status", "unknown")
        t["drifted"] = t["status"] not in ("completed", "deleted")

    tasks.sort(key=lambda t: t["ts"])
    by_project = defaultdict(lambda: {"total": 0, "open": 0})
    for t in tasks:
        p = by_project[t["project"] or "?"]
        p["total"] += 1
        p["open"] += int(t["drifted"])
    return {
        "tasks": tasks, "created": len(tasks),
        "open": sum(1 for t in tasks if t["drifted"]),
        "completed": sum(1 for t in tasks if t["status"] == "completed"),
        "recovered": recovered,
        "by_project": dict(by_project),
        "status_changes": sum(len(v) for v in upd.values())}


# ── materialization (M1): cache the ledger in a `tasks` table so the dashboard
#    reads instant SQL instead of re-scanning the multi-GB fat-union on every
#    request. materialize() runs the slow scan ONCE; the live capture hook (M2)
#    keeps the table current after that. ──
_TASKS_DDL = """CREATE TABLE IF NOT EXISTS tasks (
    session TEXT, task_id INTEGER, ts TEXT, project TEXT, subject TEXT,
    status TEXT, drifted INTEGER, interp INTEGER, prompt_id TEXT,
    source TEXT DEFAULT 'task', cwd TEXT)"""
_META_DDL = "CREATE TABLE IF NOT EXISTS tasks_meta (key TEXT PRIMARY KEY, value TEXT)"
# user overlay (M5): remove / prioritize / annotate. Kept SEPARATE from `tasks`
# so materialize()'s DELETE+INSERT never clobbers a user's edits.
_OVERLAY_DDL = """CREATE TABLE IF NOT EXISTS task_meta (
    session TEXT, task_id INTEGER, removed INTEGER DEFAULT 0,
    priority INTEGER, note TEXT, updated_ts TEXT,
    PRIMARY KEY (session, task_id))"""


def materialize(db_path):
    """Backfill the tasks table from the fat-union ledger (the slow scan runs
    HERE, once). Reads then hit the table; the M2 hook keeps it live."""
    lg = ledger(db_path)
    conn = fdb.connect(db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS tasks")   # fresh schema each rebuild
        conn.execute(_TASKS_DDL)
        conn.execute(_META_DDL)
        conn.execute(_OVERLAY_DDL)          # ensure overlay exists; never cleared
        conn.executemany(
            "INSERT INTO tasks(session,task_id,ts,project,subject,status,"
            "drifted,interp,prompt_id,source,cwd) "
            "VALUES(?,?,?,?,?,?,?,?,?,'task',?)",
            [(t["session"], t["id"], t["ts"], t["project"], t["subject"],
              t["status"], int(t["drifted"]), int(t["interp"]),
              t.get("prompt_id"), t.get("cwd")) for t in lg["tasks"]])
        inline = extract_inline(db_path, conn)        # M8: checkbox todos
        conn.executemany(
            "INSERT INTO tasks(session,task_id,ts,project,subject,status,"
            "drifted,interp,prompt_id,source,cwd) "
            "VALUES(?,?,?,?,?,?,?,0,?,'inline',?)",
            [(it["session"], it["task_id"], it["ts"], it["project"],
              it["subject"], it["status"], it["drifted"], it["prompt_id"],
              it["cwd"]) for it in inline])
        conn.execute("INSERT OR REPLACE INTO tasks_meta VALUES('status_changes',?)",
                     (str(lg["status_changes"]),))
        conn.commit()
    finally:
        conn.close()
    return {"materialized": len(lg["tasks"]), "inline": len(inline),
            "completed": lg["completed"], "open": lg["open"]}


def read(db_path, rebuild_if_empty=True):
    """Read the materialized ledger from the tasks table — fast. Same shape as
    ledger(). Triggers a one-time materialize() if the table is empty."""
    conn = fdb.connect(db_path)
    try:
        conn.execute(_TASKS_DDL)
        conn.execute(_META_DDL)
        conn.execute(_OVERLAY_DDL)
        rows = conn.execute(
            "SELECT t.session,t.task_id,t.ts,t.project,t.subject,t.status,"
            "t.drifted,t.interp,t.prompt_id,t.source,m.priority,m.note "
            "FROM tasks t LEFT JOIN task_meta m "
            "ON m.session=t.session AND m.task_id=t.task_id "
            "WHERE COALESCE(m.removed,0)=0").fetchall()
        sc = conn.execute(
            "SELECT value FROM tasks_meta WHERE key='status_changes'").fetchone()
        removed = conn.execute(
            "SELECT COUNT(*) FROM task_meta WHERE removed=1").fetchone()[0]
    finally:
        conn.close()
    if not rows and rebuild_if_empty and not removed:
        materialize(db_path)
        return read(db_path, rebuild_if_empty=False)
    tasks = [{"session": r[0], "id": r[1], "ts": r[2], "project": r[3],
              "subject": r[4], "status": r[5], "drifted": bool(r[6]),
              "interp": bool(r[7]), "prompt_id": r[8], "source": r[9],
              "priority": r[10], "note": r[11]} for r in rows]
    # priority first (P0=0 highest, nulls last), then chronological
    tasks.sort(key=lambda t: (t["priority"] is None,
                              t["priority"] if t["priority"] is not None else 0,
                              t["ts"]))
    by_project = defaultdict(lambda: {"total": 0, "open": 0})
    for t in tasks:
        p = by_project[t["project"] or "?"]
        p["total"] += 1
        p["open"] += int(t["drifted"])
    return {
        "tasks": tasks, "created": len(tasks),
        "open": sum(1 for t in tasks if t["drifted"]),
        "completed": sum(1 for t in tasks if t["status"] == "completed"),
        "recovered": sum(1 for t in tasks
                         if t["id"] is not None and not t["interp"]),
        "removed": removed,
        "by_project": dict(by_project),
        "status_changes": int(sc[0]) if sc else 0}


def open_for_project(db_path, project, limit=8):
    """Open tasks for the SessionStart reminder (M7). Matched on cwd-project OR
    work-project — Claude Code scopes its task list by the cwd dir, so this
    fires even when you work on project B from project A's directory. Priority
    first, then most recent. Excludes user-removed. Returns (rows, total)."""
    if not project:
        return [], 0
    conn = fdb.connect(db_path)
    try:
        conn.execute(_TASKS_DDL)
        conn.execute(_OVERLAY_DDL)
        where = ("(t.cwd=? OR t.project=?) AND t.drifted=1 "
                 "AND COALESCE(m.removed,0)=0")
        join = ("FROM tasks t LEFT JOIN task_meta m "
                "ON m.session=t.session AND m.task_id=t.task_id")
        rows = conn.execute(
            f"SELECT t.task_id,t.status,t.subject,m.priority {join} "
            f"WHERE {where} ORDER BY (m.priority IS NULL), m.priority, "
            f"t.ts DESC LIMIT ?", (project, project, limit)).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) {join} WHERE {where}",
            (project, project)).fetchone()[0]
        return rows, total
    finally:
        conn.close()


_CHECKBOX = re.compile(r'^[ \t]*[-*][ \t]*\[([ xX])\][ \t]+(.+?)[ \t]*$', re.M)


def extract_inline(db_path, conn=None):
    """M8: mine inline todos from ASSISTANT prose — checkbox lists only
    (- [ ] = pending, - [x] = done). Numbered lists are excluded (6k+ records,
    no signal). Each item gets a stable hash id (so the remove/priority overlay
    works) and its source thread (prompt_id) for provenance. Returns a list of
    inline tasks shaped like the structured ones (source='inline')."""
    own = conn is None
    conn = conn or fdb.connect(db_path)
    try:
        wp = work_projects(db_path, conn)
        cwd = dict(conn.execute("SELECT session_id, project FROM sessions"))
        rows = conn.execute(
            "SELECT r.prompt_id, r.session_id, r.ts, f.content "
            "FROM records r JOIN fts f ON f.uuid=r.uuid "
            "WHERE r.role='assistant' "
            "AND (f.content LIKE '%- [ ]%' OR f.content LIKE '%- [x]%')").fetchall()
        items, seen = [], set()
        for pid, sid, ts, content in rows:
            proj = wp.get(sid) or cwd.get(sid)
            for mm in _CHECKBOX.finditer(content or ""):
                checked = mm.group(1).lower() == "x"
                text = re.sub(r"\s+", " ", mm.group(2)).strip()
                # strip leading markdown emphasis so dedup is stable
                text = re.sub(r"^\*+|\*+$", "", text).strip()
                if not (4 <= len(text) <= 200):
                    continue
                key = (sid, text.lower())
                if key in seen:
                    continue
                seen.add(key)
                tid = int(hashlib.md5(text.lower().encode()).hexdigest()[:7], 16)
                items.append({
                    "session": sid, "prompt_id": pid, "project": proj,
                    "cwd": cwd.get(sid), "ts": ts or "", "task_id": tid,
                    "subject": text[:90],
                    "status": "completed" if checked else "pending",
                    "drifted": 0 if checked else 1})
        return items
    finally:
        if own:
            conn.close()


def cmd_tasks(args) -> int:
    lg = ledger(args.db)
    pct = 100 * lg["recovered"] // max(lg["created"], 1)
    print(f"task ledger — {lg['created']} created · {lg['open']} open/drifted "
          f"· {lg['completed']} completed · {lg['status_changes']} status "
          f"changes · ids {pct}% exact ({lg['created'] - lg['recovered']} "
          f"interpolated)\n")
    print("by work-project:")
    for proj, c in sorted(lg["by_project"].items(),
                          key=lambda kv: -kv[1]["total"]):
        print(f"  {proj or '?':<28} {c['total']:>4} tasks  {c['open']:>3} open")
    show_open = getattr(args, "open", False)
    rows = [t for t in lg["tasks"] if t["drifted"]] if show_open else lg["tasks"]
    print(f"\n{'open/drifted' if show_open else 'all'} tasks (newest last):")
    for t in rows[-60:]:
        flag = "·" if t["status"] == "completed" else "○"
        ids = f"#{t['id']}{'?' if t['interp'] else ''}" if t["id"] else "#?"
        print(f"  {flag} {ids:<6} [{t['status']:<11}] "
              f"{(t['project'] or '?'):<14} {t['subject']}")
    return 0
