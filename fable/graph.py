"""Memory graph — typed/directed/weighted edges over the vault, queried by the
AGENT (not just drawn for a human). The point: before the model edits a file it
should already know what past work + decisions touched it, so it doesn't repeat
mistakes or relitigate settled calls.

The schema is the FULL taxonomy from day one so neighbors/path and richer node
types drop in with NO migration:

  gnodes(id, type, label, ref, ts, meta)
      type ∈ thread|file|decision|entity|rule|lesson|gotcha|topic|symbol
  edges(src, dst, rel, level, weight, ref, ts)
      rel   ∈ touched|co_change|decided|alternative|governs|recurs|contains|belongs|relates
      level ∈ down|up|peer|cross     (the 4-level model: down/up/peer/cross-cut)

v1 (the thin agent-value slice) populates only a SUBSET:
  nodes: thread, file, decision
  edges: thread→file (touched, down), file↔file (co_change, peer),
         thread→decision (decided, down)
Edge weight = touch-frequency; recency lives on `ts`. Everything else
(entity/rule nodes, up/cross edges, neighbors/path) is future work the table
already supports.
"""
from __future__ import annotations

import json
import os
from fable import db as fdb

_GRAPH_DDL = """
CREATE TABLE IF NOT EXISTS gnodes(
  id    TEXT PRIMARY KEY,
  type  TEXT NOT NULL,
  label TEXT,
  ref   TEXT,
  ts    TEXT,
  meta  TEXT
);
CREATE INDEX IF NOT EXISTS idx_gnodes_type ON gnodes(type);

CREATE TABLE IF NOT EXISTS edges(
  src    TEXT NOT NULL,
  dst    TEXT NOT NULL,
  rel    TEXT NOT NULL,
  level  TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1.0,
  ref    TEXT,
  ts     TEXT,
  PRIMARY KEY (src, dst, rel)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_dst_rel ON edges(dst, rel);
"""

_EDIT_TOOLS = ("Edit", "Write", "MultiEdit")
_MAX_COCHANGE_FILES = 15   # skip co_change pairs for mega-threads (n² guard)

# Global / transient paths that are NOT a project's source. The global CLAUDE.md
# is edited from EVERY project; transcripts/temp dirs are cross-cutting too.
# Indexing them turns the graph into a cross-project hub — blast-radius would
# then leak threads + co-changes across unrelated projects. Excluded from the
# file scan so a file node belongs to exactly one project tree.
_HOME = os.path.expanduser("~")
_NONPROJECT_PREFIXES = (
    os.path.join(_HOME, ".claude") + os.sep,   # global CLAUDE.md, transcripts, skills, memory
    "/tmp/", "/private/tmp/", "/private/var/", "/var/folders/", "/var/tmp/",
)


def _is_project_file(path: str) -> bool:
    """True for a real source file inside a project tree — not a global config
    file, a transcript, or a temp path touched across every project."""
    p = path or ""
    return bool(p) and not p.startswith(_NONPROJECT_PREFIXES) \
        and not p.endswith(".jsonl")


def ensure_schema(conn):
    conn.executescript(_GRAPH_DDL)


# ── edge construction ───────────────────────────────────────────────────────

def _thread_file_touches(conn) -> dict:
    """{prompt_id: {canonical_path: touch_count}} — files EDITED in each thread,
    read from the file_path arg of Edit/Write/MultiEdit tool_use records (NOT
    prose-scraped, which pulls `myvix.rs`/code-spans out of discussion). Mirrors
    filetime.edited_files but keyed per thread, and folds path representations
    (vix.rs / src/vix.rs / /abs/vix.rs) onto one canonical key."""
    from fable.filetime import read_span, _canonical_map
    rows = conn.execute(
        "SELECT r.prompt_id, f.path, r.offset, r.length "
        "FROM records r JOIN files f ON f.id = r.file_id "
        "JOIN fts ft ON ft.uuid = r.uuid "
        "WHERE r.block_kinds LIKE '%tool_use%' AND (ft.content LIKE '%Edit%' "
        "OR ft.content LIKE '%Write%' OR ft.content LIKE '%MultiEdit%')"
    ).fetchall()
    per: dict = {}
    allpaths = set()
    for pid, fpath, offset, length in rows:
        if not pid:
            continue
        try:
            obj = json.loads(read_span(fpath, offset, length)
                             .decode("utf-8", "surrogateescape"))
        except (OSError, ValueError):
            continue
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for b in content:
            if (isinstance(b, dict) and b.get("type") == "tool_use"
                    and b.get("name") in _EDIT_TOOLS):
                inp = b.get("input") or {}
                fp = inp.get("file_path") or inp.get("path")
                if fp and isinstance(fp, str) and _is_project_file(fp):
                    per.setdefault(pid, {})
                    per[pid][fp] = per[pid].get(fp, 0) + 1
                    allpaths.add(fp)
    cmap = _canonical_map(list(allpaths)) if allpaths else {}
    out: dict = {}
    for pid, files in per.items():
        m: dict = {}
        for fp, n in files.items():
            k = cmap.get(fp, fp)
            m[k] = m.get(k, 0) + n
        out[pid] = m
    return out


def build_edges(db_path: str) -> dict:
    """Materialize gnodes + edges from the vault. Idempotent: wipes and rebuilds
    (the table is small relative to the corpus). Cheap to re-run from the watch
    loop. Returns counts."""
    conn = fdb.connect(db_path)
    try:
        ensure_schema(conn)
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM gnodes")

        cards = {}
        ccols = [r[1] for r in conn.execute("PRAGMA table_info(cards)")]
        extra = [c for c in ("salient_entities", "lessons", "gotchas")
                 if c in ccols]
        sel = "prompt_id, title, type, decisions, outcome, created_at" + \
              ("".join(", " + c for c in extra))
        for row in conn.execute(f"SELECT {sel} FROM cards"):
            pid, title, ctype, decisions, outcome, ts = row[:6]
            d = {"title": title, "type": ctype, "decisions": decisions,
                 "outcome": outcome, "ts": ts}
            for i, c in enumerate(extra):
                d[c] = row[6 + i]
            cards[pid] = d
        thread_ts = dict(conn.execute(
            "SELECT prompt_id, last_ts FROM threads"))

        # thread → work-project (file-root derived, falls back to cwd label) for
        # the 'belongs' (up) edges
        sess_of = dict(conn.execute("SELECT prompt_id, session_id FROM threads"))
        proj_of = dict(conn.execute("SELECT session_id, project FROM sessions"))
        try:
            from fable import tasktime
            wp = tasktime.work_projects(db_path, conn)
        except Exception:
            wp = {}

        def thread_project(pid):
            s = sess_of.get(pid)
            return (wp.get(s) or proj_of.get(s)) if s else None

        nodes = {}   # id -> (type, label, ref, ts, meta)
        edges = {}   # (src, dst, rel) -> [level, weight, ref, ts]

        def add_node(nid, ntype, label, ref=None, ts=None, meta=None):
            if nid not in nodes:
                nodes[nid] = (ntype, label, ref, ts,
                              json.dumps(meta) if meta else None)

        def add_edge(src, dst, rel, level, weight, ref, ts):
            k = (src, dst, rel)
            if k in edges:
                edges[k][1] += weight
            else:
                edges[k] = [level, weight, ref, ts]

        # ── thread→file (touched) + file↔file (co_change) ──
        touches = _thread_file_touches(conn)
        for pid, files in touches.items():
            tid = f"thread:{pid}"
            c = cards.get(pid) or {}
            tts = thread_ts.get(pid) or c.get("ts")
            add_node(tid, "thread", (c.get("title") or pid[:8]), pid, tts,
                     {"outcome": c.get("outcome"), "type": c.get("type")})
            paths = sorted(files)
            for path, n in files.items():
                fid = f"file:{path}"
                add_node(fid, "file", path.split("/")[-1], None, None,
                         {"full": path})
                add_edge(tid, fid, "touched", "down", float(n), pid, tts)
            if 2 <= len(paths) <= _MAX_COCHANGE_FILES:
                for i in range(len(paths)):
                    for j in range(i + 1, len(paths)):
                        a, b = f"file:{paths[i]}", f"file:{paths[j]}"
                        add_edge(a, b, "co_change", "peer", 1.0, pid, tts)

        # ── per-card nodes: decisions, salient entities, lessons, gotchas,
        #    and the project the thread belongs to (up edge) ──
        def _jlist(blob):
            try:
                return [str(x).strip() for x in (json.loads(blob or "[]") or [])
                        if str(x).strip()]
            except (ValueError, TypeError):
                return []
        for pid, c in cards.items():
            decs = _jlist(c.get("decisions"))
            sal = _jlist(c.get("salient_entities"))
            lessons = _jlist(c.get("lessons"))
            gotchas = _jlist(c.get("gotchas"))
            tid = f"thread:{pid}"
            has_thread_node = tid in nodes      # from the file-touch pass
            if not (decs or sal or lessons or gotchas or has_thread_node):
                continue
            tts = thread_ts.get(pid) or c.get("ts")
            add_node(tid, "thread", (c.get("title") or pid[:8]), pid, tts,
                     {"outcome": c.get("outcome"), "type": c.get("type")})
            proj = thread_project(pid)
            if proj:
                add_node(f"project:{proj}", "project", proj)
                add_edge(tid, f"project:{proj}", "belongs", "up", 1.0, pid, tts)
            for i, d in enumerate(decs):
                add_node(f"decision:{pid}:{i}", "decision", d[:120], pid, tts,
                         {"text": d})
                add_edge(tid, f"decision:{pid}:{i}", "decided", "down", 1.0,
                         pid, tts)
            for e in sal:
                e = e.lower()[:60]
                add_node(f"entity:{e}", "entity", e)
                add_edge(tid, f"entity:{e}", "about", "down", 1.0, pid, tts)
            for i, l in enumerate(lessons):
                add_node(f"lesson:{pid}:{i}", "lesson", l[:120], pid, tts,
                         {"text": l})
                add_edge(tid, f"lesson:{pid}:{i}", "learned", "down", 1.0,
                         pid, tts)
            for i, g in enumerate(gotchas):
                add_node(f"gotcha:{pid}:{i}", "gotcha", g[:120], pid, tts,
                         {"text": g})
                add_edge(tid, f"gotcha:{pid}:{i}", "hit", "down", 1.0, pid, tts)

        # ── cross-cut: a standing rule GOVERNS the threads it was mined from ──
        try:
            rrows = conn.execute(
                "SELECT canonical_text, display_text, evidence, status, last_seen"
                " FROM rules WHERE status IN ('active','candidate')").fetchall()
        except Exception:
            rrows = []
        for canon, disp, ev, status, ls in rrows:
            rid = f"rule:{(canon or '')[:60]}"
            add_node(rid, "rule", (disp or canon or "")[:120], None, ls,
                     {"status": status})
            for p in _jlist(ev):
                if f"thread:{p}" in nodes:
                    add_edge(rid, f"thread:{p}", "governs", "cross", 1.0, None, ls)

        conn.executemany(
            "INSERT OR REPLACE INTO gnodes(id, type, label, ref, ts, meta) "
            "VALUES(?,?,?,?,?,?)",
            [(nid, t, lbl, ref, ts, meta)
             for nid, (t, lbl, ref, ts, meta) in nodes.items()])
        conn.executemany(
            "INSERT OR REPLACE INTO edges(src, dst, rel, level, weight, ref, ts)"
            " VALUES(?,?,?,?,?,?,?)",
            [(s, d, rel, lv, w, ref, ts)
             for (s, d, rel), (lv, w, ref, ts) in edges.items()])
        conn.commit()
        by_type = {}
        for (t, *_rest) in nodes.values():
            by_type[t] = by_type.get(t, 0) + 1
        by_rel = {}
        for (_s, _d, rel), _v in edges.items():
            by_rel[rel] = by_rel.get(rel, 0) + 1
        return {"nodes": len(nodes), "edges": len(edges),
                "node_types": by_type, "edge_rels": by_rel}
    finally:
        conn.close()


# ── queries (the agent-facing surface) ──────────────────────────────────────

def _resolve_file(conn, query: str):
    """Match a loose file query ('cards.py', 'fable/cards.py', an abs path) to a
    canonical file node. Exact canonical → exact basename → unique suffix →
    unique substring. Returns the canonical path or None."""
    q = (query or "").strip()
    if not q:
        return None
    files = [r[0][5:] for r in conn.execute(
        "SELECT id FROM gnodes WHERE type='file'")]
    if not files:
        return None
    if q in files:
        return q
    base = q.split("/")[-1]
    exact = [f for f in files if f.split("/")[-1] == base]
    if len(exact) == 1:
        return exact[0]
    suf = [f for f in files if f.endswith(q)]
    if len(suf) == 1:
        return suf[0]
    sub = [f for f in files if q in f]
    if len(sub) == 1:
        return sub[0]
    # ambiguous basename: prefer the most-edited
    if exact:
        ranked = conn.execute(
            "SELECT dst, SUM(weight) w FROM edges WHERE rel='touched' "
            "AND dst IN (%s) GROUP BY dst ORDER BY w DESC LIMIT 1"
            % ",".join("?" * len(exact)),
            [f"file:{f}" for f in exact]).fetchone()
        if ranked:
            return ranked[0][5:]
    return None


def blast_radius(db_path: str, file: str, limit: int = 8) -> dict:
    """What past work touched THIS file — the threads that edited it (most
    recent first) with their decisions, plus files that co-change with it.
    The pre-edit 'don't repeat the past' briefing."""
    conn = fdb.connect(db_path)
    try:
        ensure_schema(conn)
        canon = _resolve_file(conn, file)
        if not canon:
            return {"file": file, "matched": None, "threads": [],
                    "co_changed": [],
                    "note": "no recorded edits to a file matching that — "
                            "fable only graphs files touched via Edit/Write."}
        fid = f"file:{canon}"
        rows = conn.execute(
            "SELECT src, weight, ts FROM edges WHERE dst=? AND rel='touched' "
            "ORDER BY ts DESC", (fid,)).fetchall()
        threads = []
        for src, w, ts in rows[:limit]:
            pid = src.split(":", 1)[1]
            card = conn.execute(
                "SELECT title, decisions, gotchas, lessons, outcome "
                "FROM cards WHERE prompt_id=?", (pid,)).fetchone()
            title, decisions, gotchas, lessons, outcome = (
                card or (None, None, None, None, None))

            def _lst(blob):
                try:
                    return [str(x) for x in (json.loads(blob or "[]") or [])]
                except (ValueError, TypeError):
                    return []
            threads.append({
                "prompt_id": pid, "title": title or pid[:8],
                "edited_x": int(w), "ts": (ts or "")[:10], "outcome": outcome,
                "decisions": _lst(decisions), "gotchas": _lst(gotchas),
                "lessons": _lst(lessons)})
        co = conn.execute(
            "SELECT CASE WHEN src=? THEN dst ELSE src END, weight FROM edges "
            "WHERE rel='co_change' AND (src=? OR dst=?) "
            "ORDER BY weight DESC LIMIT 10", (fid, fid, fid)).fetchall()
        co_changed = [{"file": o.split(":", 1)[1], "together_x": int(w)}
                      for o, w in co]
        return {"file": canon, "matched": canon,
                "touched_by_threads": len(rows),
                "threads": threads, "co_changed": co_changed}
    finally:
        conn.close()


def provenance(db_path: str, file: str, limit: int = 12) -> dict:
    """The decisions that shaped this file, newest→oldest — 'why is it like
    this'. Flattens the decisions of every thread that edited the file."""
    br = blast_radius(db_path, file, limit=50)
    if not br.get("matched"):
        return {"file": file, "matched": None, "decisions": [],
                "note": br.get("note")}
    out = []
    for t in br["threads"]:
        for d in t["decisions"]:
            out.append({"decision": d, "thread": t["title"],
                        "ts": t["ts"], "prompt_id": t["prompt_id"]})
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    return {"file": br["matched"], "decisions": out,
            "note": None if out else "no decisions recorded against this file"}


def _resolve_node(conn, query):
    """Resolve a loose query to a (id, label, type) node. Tries: exact node id,
    file match, then entity/thread/project id, then a label substring."""
    q = (query or "").strip()
    if not q:
        return None
    row = conn.execute("SELECT id,label,type FROM gnodes WHERE id=?",
                       (q,)).fetchone()
    if row:
        return row
    cf = _resolve_file(conn, q)
    if cf:
        row = conn.execute("SELECT id,label,type FROM gnodes WHERE id=?",
                           (f"file:{cf}",)).fetchone()
        if row:
            return row
    for cand in (f"entity:{q.lower()}", f"thread:{q}", f"project:{q}"):
        row = conn.execute("SELECT id,label,type FROM gnodes WHERE id=?",
                           (cand,)).fetchone()
        if row:
            return row
    return conn.execute(
        "SELECT id,label,type FROM gnodes WHERE label LIKE ? "
        "ORDER BY length(label) LIMIT 1", (f"%{q}%",)).fetchone()


def neighbors(db_path, node, hops=1, limit=20):
    """Typed, ranked nodes connected to a node (file / entity / decision /
    thread / rule / project). 1 hop by default; hops=2 adds the next ring. The
    generic 'what relates to X' primitive."""
    conn = fdb.connect(db_path)
    try:
        ensure_schema(conn)
        res = _resolve_node(conn, node)
        if not res:
            return {"node": node, "matched": None, "neighbors": []}
        nid, label, ntype = res
        seen = {nid}
        frontier = [nid]
        out = []
        for hop in range(max(1, min(int(hops), 2))):
            nxt = []
            for cur in frontier:
                for other, rel, level, w in conn.execute(
                        "SELECT CASE WHEN src=? THEN dst ELSE src END, rel, "
                        "level, weight FROM edges WHERE src=? OR dst=? "
                        "ORDER BY weight DESC", (cur, cur, cur)):
                    if other in seen:
                        continue
                    seen.add(other)
                    nxt.append(other)
                    g = conn.execute(
                        "SELECT type,label,ref FROM gnodes WHERE id=?",
                        (other,)).fetchone()
                    if g:
                        out.append({"id": other, "type": g[0], "label": g[1],
                                    "ref": g[2], "rel": rel, "level": level,
                                    "weight": int(w), "hop": hop + 1})
            frontier = nxt
        out.sort(key=lambda x: (x["hop"], -x["weight"]))
        return {"node": nid, "label": label, "type": ntype,
                "count": len(out), "neighbors": out[:limit]}
    finally:
        conn.close()


def path(db_path, a, b):
    """Shortest connection between two nodes — 'how does A relate to B'. BFS over
    the undirected edge set; returns the node chain or a no-connection note."""
    conn = fdb.connect(db_path)
    try:
        ensure_schema(conn)
        ra, rb = _resolve_node(conn, a), _resolve_node(conn, b)
        if not ra or not rb:
            return {"a": a, "b": b, "matched": False, "path": None,
                    "note": "could not resolve both endpoints to graph nodes"}
        start, goal = ra[0], rb[0]
        if start == goal:
            return {"a": start, "b": goal, "hops": 0,
                    "path": [{"id": start, "label": ra[1], "type": ra[2]}]}
        from collections import deque
        prev = {start: None}
        dq = deque([start])
        while dq:
            cur = dq.popleft()
            if cur == goal:
                break
            for (other,) in conn.execute(
                    "SELECT CASE WHEN src=? THEN dst ELSE src END FROM edges "
                    "WHERE src=? OR dst=?", (cur, cur, cur)):
                if other not in prev:
                    prev[other] = cur
                    dq.append(other)
            if len(prev) > 50000:
                break
        if goal not in prev:
            return {"a": start, "b": goal, "path": None,
                    "note": "no connection between them in the graph"}
        chain = []
        cur = goal
        while cur is not None:
            g = conn.execute("SELECT type,label FROM gnodes WHERE id=?",
                             (cur,)).fetchone()
            chain.append({"id": cur, "type": g[0] if g else "?",
                          "label": g[1] if g else cur})
            cur = prev[cur]
        chain.reverse()
        return {"a": start, "b": goal, "hops": len(chain) - 1, "path": chain}
    finally:
        conn.close()


# ── PreToolUse injection (auto-fires before an edit) ─────────────────────────

def render_blast_radius(db_path: str, file: str) -> str:
    """Compact <fable-blast-radius> block for the PreToolUse hook — empty string
    if the file has no recorded history (stay silent rather than nag)."""
    try:
        br = blast_radius(db_path, file, limit=5)
    except Exception:
        return ""
    if not br.get("matched") or not br.get("threads"):
        return ""
    canon = br["matched"]
    L = [f'<fable-blast-radius file="{canon}">',
         f"⚠ Before editing {canon.split('/')[-1]} — {br['touched_by_threads']}"
         f" past thread(s) touched it. What was decided/learned here (don't "
         f"relitigate or repeat):"]
    for t in br["threads"]:
        head = f"• {t['title']} ({t['ts']})"
        L.append(head)
        for d in t["decisions"][:3]:
            L.append(f"   ↳ decision: {d[:150]}")
        for g in t["gotchas"][:2]:
            L.append(f"   ↳ gotcha: {g[:150]}")
    if br["co_changed"]:
        cc = ", ".join(c["file"].split("/")[-1] for c in br["co_changed"][:5])
        L.append(f"co-changes with: {cc} — check if they need updating too.")
    L.append("Recall any in full: fable_thread(\"<prompt_id>\") · "
             "fable_search for more.")
    L.append("</fable-blast-radius>")
    return "\n".join(L)


def cmd_graph(args) -> int:
    print(json.dumps(build_edges(args.db), indent=2))
    return 0
