"""fable serve — read-only dashboard over the Map.

Stdlib http.server + one self-contained HTML page. Every endpoint is a
read; the dashboard can never mutate the index or the vault.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from fable import db as fdb

DASHBOARD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "dashboard.html")


def _rows(conn, sql, args=()):
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def api_stats(db_path, params):
    conn = fdb.connect(db_path)
    try:
        out = {}
        for key, sql in [
            ("records", "SELECT COUNT(*) FROM records"),
            ("copies", "SELECT COUNT(*) FROM copies"),
            ("threads", "SELECT COUNT(*) FROM threads"),
            ("sessions", "SELECT COUNT(*) FROM sessions"),
            ("cards", "SELECT COUNT(*) FROM cards"),
            ("terms", "SELECT COUNT(*) FROM terms"),
            ("files", "SELECT COUNT(*) FROM files"),
            ("citations", "SELECT COUNT(*) FROM citations"),
        ]:
            out[key] = conn.execute(sql).fetchone()[0]
        out["db_bytes"] = (os.path.getsize(db_path)
                           if os.path.exists(db_path) else 0)
        out["vault_bytes"] = conn.execute(
            "SELECT COALESCE(SUM(size),0) FROM files").fetchone()[0]
        out["card_types"] = dict(conn.execute(
            "SELECT type, COUNT(*) FROM cards GROUP BY type").fetchall())
        return out
    finally:
        conn.close()


def api_projects(db_path, params):
    conn = fdb.connect(db_path)
    try:
        sessions = _rows(conn, """
            SELECT s.session_id, s.project, s.title, s.live_path,
                   COALESCE(s.pinned,0) AS pinned, s.tags,
                   COUNT(t.prompt_id) AS threads,
                   COALESCE(SUM(t.est_tokens),0) AS est_tokens,
                   MAX(t.last_ts) AS last_ts
            FROM sessions s LEFT JOIN threads t ON t.session_id = s.session_id
            GROUP BY s.session_id
            ORDER BY s.project, pinned DESC, last_ts DESC""")
        projects = {}
        for s in sessions:
            projects.setdefault(s["project"] or "unknown", []).append(s)
        return [{"project": p, "sessions": ss} for p, ss in
                sorted(projects.items())]
    finally:
        conn.close()


def api_search(db_path, params):
    from fable.recall import search
    one = lambda k: (params.get(k) or [None])[0]
    return search(db_path, one("q") or "",
                  operative=one("op"), target=one("target"),
                  limit=int(one("n") or 25),
                  sort=one("sort") or "relevance",
                  kind=one("kind"), model=one("model"),
                  project=one("project"))


def api_threads(db_path, params):
    session = (params.get("session") or [None])[0]
    conn = fdb.connect(db_path)
    try:
        return _rows(conn, """
            SELECT t.prompt_id, t.first_ts, t.last_ts, t.turn_count,
                   t.est_tokens, t.sidechain_turns, t.models,
                   c.title, c.type, c.outcome
            FROM threads t LEFT JOIN cards c ON c.prompt_id = t.prompt_id
            WHERE t.session_id = ?
            ORDER BY t.first_ts""", (session,))
    finally:
        conn.close()


def api_facets(db_path, params):
    """Distinct values for the filter dropdowns, optionally project-scoped."""
    project = (params.get("project") or [None])[0]
    conn = fdb.connect(db_path)
    try:
        scope_sql, scope_args = "", []
        if project:
            scope_sql = (" AND session_id IN (SELECT session_id FROM "
                         "sessions WHERE project LIKE ?)")
            scope_args = [f"%{project}%"]
        models = set()
        for (csv,) in conn.execute(
                "SELECT DISTINCT models FROM threads WHERE models IS NOT "
                "NULL" + scope_sql, scope_args):
            models.update(m.strip() for m in csv.split(",") if m.strip())
        projects = [r[0] for r in conn.execute(
            "SELECT DISTINCT project FROM sessions WHERE project IS NOT NULL "
            "ORDER BY project")]
        op_sql = ("SELECT term FROM terms WHERE kind='operative' ")
        if project:
            op_sql += ("AND prompt_id IN (SELECT prompt_id FROM threads "
                       "WHERE 1=1" + scope_sql + ") ")
        op_sql += "GROUP BY term ORDER BY SUM(count) DESC LIMIT 30"
        operatives = [r[0] for r in conn.execute(op_sql, scope_args)]
        return {"models": sorted(models), "projects": projects,
                "operatives": operatives}
    finally:
        conn.close()


def _session_cwd(db_path, session_id):
    """The real cwd of a session, read from its transcript records."""
    live = _live_path(db_path, session_id)
    with open(live) as f:
        for i, line in enumerate(f):
            if i > 50:
                break
            try:
                cwd = json.loads(line).get("cwd")
                if cwd:
                    return cwd
            except json.JSONDecodeError:
                continue
    raise KeyError(f"no cwd found in session {session_id}")


def post_compose(db_path, body):
    from fable.compose import compose
    cwd = body.get("cwd")
    if not cwd and body.get("session"):
        cwd = _session_cwd(db_path, body["session"])
    if not cwd:
        raise ValueError("compose needs a cwd or an anchor session")
    return compose(db_path, body["threads"], body.get("title") or "workspace",
                   cwd=cwd, strip_thinking=bool(body.get("strip_thinking")))


def post_session_meta(db_path, body):
    """Pin/tag a session from the sidebar."""
    conn = fdb.connect(db_path)
    try:
        if "pinned" in body:
            conn.execute("UPDATE sessions SET pinned = ? WHERE session_id = ?",
                         (1 if body["pinned"] else 0, body["session_id"]))
        if "tags" in body:
            conn.execute("UPDATE sessions SET tags = ? WHERE session_id = ?",
                         (body["tags"], body["session_id"]))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def api_thread(db_path, params):
    from fable.recall import render_thread
    prompt_id = (params.get("id") or [""])[0]
    budget = int((params.get("budget") or ["8000"])[0])
    raw = (params.get("raw") or ["0"])[0] == "1"
    text = render_thread(db_path, prompt_id, budget=budget, raw=raw)
    conn = fdb.connect(db_path)
    try:
        card = _rows(conn, "SELECT * FROM cards WHERE prompt_id = ?",
                     (prompt_id,))
    finally:
        conn.close()
    return {"prompt_id": prompt_id, "text": text,
            "card": card[0] if card else None}


def api_generations(db_path, params):
    """Per-record copy history across vault generations for one thread."""
    prompt_id = (params.get("id") or [""])[0]
    conn = fdb.connect(db_path)
    try:
        recs = _rows(conn, """
            SELECT r.uuid, r.type, r.role, r.ts, r.length AS best_length
            FROM records r WHERE r.prompt_id = ?
            ORDER BY r.ts_epoch, r.lineno""", (prompt_id,))
        for r in recs:
            r["copies"] = _rows(conn, """
                SELECT c.file_id, f.label, f.generation, c.length
                FROM copies c JOIN files f ON f.id = c.file_id
                WHERE c.uuid = ? ORDER BY f.generation""", (r["uuid"],))
        gens = _rows(conn, """
            SELECT f.id AS file_id, f.label, f.generation, f.immutable,
                   COUNT(c.uuid) AS records, SUM(c.length) AS bytes
            FROM files f JOIN copies c ON c.file_id = f.id
            WHERE c.uuid IN (SELECT uuid FROM records WHERE prompt_id = ?)
            GROUP BY f.id ORDER BY f.generation""", (prompt_id,))
        return {"records": recs, "generations": gens}
    finally:
        conn.close()


def api_diff(db_path, params):
    """Unified diff of one record between two files (generations)."""
    import difflib
    from fable.jsonl import read_span
    uuid = (params.get("uuid") or [""])[0]
    fa = int((params.get("a") or ["0"])[0])
    fb = int((params.get("b") or ["0"])[0])
    conn = fdb.connect(db_path)
    try:
        def fetch(fid):
            row = conn.execute(
                "SELECT f.path, f.label, c.offset, c.length FROM copies c "
                "JOIN files f ON f.id = c.file_id "
                "WHERE c.uuid = ? AND c.file_id = ?", (uuid, fid)).fetchone()
            if not row:
                raise KeyError(f"no copy of {uuid} in file {fid}")
            obj = json.loads(read_span(row[0], row[2], row[3])
                             .decode("utf-8", "surrogateescape"))
            pretty = json.dumps(obj, indent=1, sort_keys=True,
                                ensure_ascii=False)
            return row[1], pretty.splitlines()
        label_a, lines_a = fetch(fa)
        label_b, lines_b = fetch(fb)
    finally:
        conn.close()
    diff = list(difflib.unified_diff(lines_a, lines_b,
                                     fromfile=label_a, tofile=label_b,
                                     lineterm="", n=2))
    return {"uuid": uuid, "a": label_a, "b": label_b,
            "identical": not diff, "diff": diff[:2000]}


def api_graph(db_path, params):
    """Obsidian-style graph: thread nodes + shared term nodes + citations."""
    session = (params.get("session") or [None])[0]
    cap = int((params.get("cap") or ["100"])[0])
    conn = fdb.connect(db_path)
    try:
        scope_sql = "SELECT prompt_id FROM threads"
        scope_args = []
        if session:
            scope_sql += " WHERE session_id = ?"
            scope_args.append(session)
        scope_sql += " ORDER BY est_tokens DESC LIMIT ?"
        scope_args.append(cap)
        pids = [r[0] for r in conn.execute(scope_sql, scope_args)]
        if not pids:
            return {"nodes": [], "links": []}
        ph = ",".join("?" * len(pids))

        threads = _rows(conn, f"""
            SELECT t.prompt_id, t.est_tokens, t.turn_count, t.first_ts,
                   c.title, c.type FROM threads t
            LEFT JOIN cards c ON c.prompt_id = t.prompt_id
            WHERE t.prompt_id IN ({ph})""", pids)

        term_rows = _rows(conn, f"""
            SELECT term, kind, prompt_id, score FROM terms
            WHERE kind IN ('target','concept','wikilink')
            AND prompt_id IN ({ph})""", pids)
        by_term = {}
        for tr in term_rows:
            by_term.setdefault((tr["term"], tr["kind"]), []).append(tr)
        shared = {k: v for k, v in by_term.items() if 2 <= len(v) <= 40}
        top_terms = sorted(shared.items(),
                           key=lambda kv: -sum(t["score"] for t in kv[1]))[:80]

        nodes = [{"id": t["prompt_id"], "group": "thread",
                  "label": t["title"] or t["prompt_id"][:8],
                  "type": t["type"], "tokens": t["est_tokens"],
                  "turns": t["turn_count"]} for t in threads]
        links = []
        for (term, kind), rows in top_terms:
            nid = f"term:{term}"
            nodes.append({"id": nid, "group": kind, "label": term,
                          "df": len(rows)})
            for tr in rows:
                links.append({"source": tr["prompt_id"], "target": nid,
                              "weight": round(tr["score"], 2)})

        cites = _rows(conn, f"""
            SELECT DISTINCT r.prompt_id AS src, ci.ref AS dst
            FROM citations ci JOIN records r ON r.uuid = ci.from_uuid
            WHERE r.prompt_id IN ({ph})""", pids)
        known = {n["id"] for n in nodes}
        for c in cites:
            if c["src"] in known and c["dst"] in known:
                links.append({"source": c["src"], "target": c["dst"],
                              "weight": 5, "kind": "citation"})
        return {"nodes": nodes, "links": links}
    finally:
        conn.close()


def api_suggestions(db_path, params):
    from fable.surgery import suggestions
    session = (params.get("session") or [""])[0]
    return suggestions(db_path, session)


def api_context(db_path, params):
    from fable.contextpack import build_context
    return {"pack": build_context(
        db_path, (params.get("q") or [""])[0],
        budget=int((params.get("budget") or ["12000"])[0]),
        max_threads=int((params.get("n") or ["5"])[0]))}


# USD per MTok: (input, output, cache_read, cache_write). Substring match.
PRICING = {
    "opus": (15.0, 75.0, 1.5, 18.75),
    "fable": (15.0, 75.0, 1.5, 18.75),
    "sonnet": (3.0, 15.0, 0.3, 3.75),
    "haiku": (1.0, 5.0, 0.1, 1.25),
}


def _price(model):
    for key, p in PRICING.items():
        if key in (model or ""):
            return p
    return PRICING["sonnet"]


def api_costs(db_path, params):
    """ccusage-style analytics: API-equivalent value of the indexed work."""
    conn = fdb.connect(db_path)
    try:
        rows = _rows(conn, """
            SELECT COALESCE(s.project,'unknown') AS project, r.model,
                   SUM(COALESCE(r.in_tokens,0)) AS tin,
                   SUM(COALESCE(r.out_tokens,0)) AS tout,
                   SUM(COALESCE(r.cache_read_tokens,0)) AS tcr,
                   SUM(COALESCE(r.cache_write_tokens,0)) AS tcw,
                   COUNT(*) AS records
            FROM records r LEFT JOIN sessions s ON s.session_id = r.session_id
            WHERE r.model IS NOT NULL
            GROUP BY project, r.model ORDER BY tout DESC""")
    finally:
        conn.close()
    total = 0.0
    by_project, by_model = {}, {}
    for r in rows:
        pin, pout, pcr, pcw = _price(r["model"])
        cost = (r["tin"] * pin + r["tout"] * pout
                + r["tcr"] * pcr + r["tcw"] * pcw) / 1e6
        r["cost_usd"] = round(cost, 2)
        total += cost
        by_project[r["project"]] = round(
            by_project.get(r["project"], 0) + cost, 2)
        by_model[r["model"]] = round(by_model.get(r["model"], 0) + cost, 2)
    return {"rows": rows, "total_usd": round(total, 2),
            "by_project": by_project, "by_model": by_model,
            "note": ("API-equivalent value at current pricing; subscription "
                     "usage costs you $0 — this is what the indexed work "
                     "would have cost on the API")}


def api_dashboard(db_path, params):
    """Full usage+cost telemetry: daily series, model/project breakdowns,
    fable engine metrics, provider/card-generation metrics."""
    conn = fdb.connect(db_path)

    def _p(name):
        v = params.get(name) or ""
        return v[0] if isinstance(v, list) else v
    project, session = _p("project"), _p("session")
    pj_join, pj_where, pj_args = "", "", []
    if session:
        pj_where, pj_args = "AND records.session_id = ?", [session]
        project = project or (conn.execute(
            "SELECT project FROM sessions WHERE session_id = ?",
            (session,)).fetchone() or [""])[0]
    elif project:
        pj_join = ("JOIN sessions s ON s.session_id = records.session_id")
        pj_where = "AND s.project = ?"
        pj_args = [project]
    try:
        daily = _rows(conn, f"""
            SELECT substr(ts,1,10) AS day, model,
                   SUM(COALESCE(in_tokens,0)) AS tin,
                   SUM(COALESCE(out_tokens,0)) AS tout,
                   SUM(COALESCE(cache_read_tokens,0)) AS tcr,
                   SUM(COALESCE(cache_write_tokens,0)) AS tcw,
                   COUNT(*) AS msgs
            FROM records {pj_join}
            WHERE model IS NOT NULL AND ts IS NOT NULL {pj_where}
            GROUP BY day, model ORDER BY day""", pj_args)
        for r in daily:
            pin, pout, pcr, pcw = _price(r["model"])
            r["cost"] = round((r["tin"] * pin + r["tout"] * pout
                               + r["tcr"] * pcr + r["tcw"] * pcw) / 1e6, 2)
        sessions_daily = _rows(conn, """
            SELECT substr(first_ts,1,10) AS day, COUNT(*) AS threads,
                   SUM(est_tokens) AS tok
            FROM threads WHERE first_ts IS NOT NULL
            GROUP BY day ORDER BY day""")
        eligible = conn.execute(
            "SELECT COUNT(*) FROM threads WHERE est_tokens >= 200"
        ).fetchone()[0]
        carded = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        fable = {
            "records": conn.execute(
                "SELECT COUNT(*) FROM records").fetchone()[0],
            "copies": conn.execute(
                "SELECT COUNT(*) FROM copies").fetchone()[0],
            "threads": conn.execute(
                "SELECT COUNT(*) FROM threads").fetchone()[0],
            "sessions": conn.execute(
                "SELECT COUNT(*) FROM sessions").fetchone()[0],
            "cards": carded,
            "card_coverage_pct": min(100.0, round(100 * carded / max(eligible, 1), 1)),
            "eligible_threads": eligible,
            "terms": conn.execute(
                "SELECT COUNT(*) FROM terms").fetchone()[0],
            "facts": conn.execute(
                "SELECT COUNT(*) FROM facts WHERE active=1").fetchone()[0],
            "embeddings": conn.execute(
                "SELECT COUNT(*) FROM embeddings").fetchone()[0],
            "vault_bytes": conn.execute(
                "SELECT COALESCE(SUM(size),0) FROM files "
                "WHERE immutable=1").fetchone()[0],
            "live_bytes": conn.execute(
                "SELECT COALESCE(SUM(size),0) FROM files "
                "WHERE immutable=0").fetchone()[0],
            "db_bytes": (os.path.getsize(db_path)
                         if os.path.exists(db_path) else 0),
        }
        provider_cards = _rows(conn, """
            SELECT source, model, COUNT(*) AS n FROM cards
            GROUP BY source, model ORDER BY n DESC""")
        ops_summary = _rows(conn, """
            SELECT kind, COUNT(*) AS n, MAX(ts) AS last FROM ops
            GROUP BY kind ORDER BY n DESC""")
        ops_recent = _rows(conn, """
            SELECT ts, kind, detail FROM ops ORDER BY id DESC LIMIT 10""")
        projects_rollup = _rows(conn, """
            SELECT s.project,
                   COUNT(DISTINCT s.session_id) AS sessions,
                   COUNT(t.prompt_id) AS threads,
                   SUM(COALESCE(t.est_tokens,0)) AS tok,
                   SUM(CASE WHEN c.prompt_id IS NULL THEN 0 ELSE 1 END)
                       AS carded
            FROM sessions s
            LEFT JOIN threads t ON t.session_id = s.session_id
            LEFT JOIN cards c ON c.prompt_id = t.prompt_id
            GROUP BY s.project ORDER BY tok DESC""")
        try:
            from fable.embeddings import backend as _emb_backend
            emb = _emb_backend() or "off"
        except Exception:
            emb = "off"
        hook_installed = False
        try:
            with open(os.path.expanduser("~/.claude/settings.json")) as fh:
                hook_installed = "fable hook" in fh.read()
        except OSError:
            pass
        health = {
            "db_bytes": os.path.getsize(db_path),
            "wal_bytes": (os.path.getsize(db_path + "-wal")
                          if os.path.exists(db_path + "-wal") else 0),
            "embeddings_backend": emb,
            "hook_installed": hook_installed,
        }
        top_sessions = _rows(conn, f"""
            SELECT s.session_id, s.project, s.title,
                   COUNT(t.prompt_id) AS threads,
                   SUM(t.est_tokens) AS tok, MAX(t.last_ts) AS last_ts
            FROM sessions s JOIN threads t ON t.session_id = s.session_id
            {"WHERE s.project = ?" if project else ""}
            GROUP BY s.session_id ORDER BY tok DESC LIMIT 10""",
            [project] if project else [])
    finally:
        conn.close()
    costs = api_costs(db_path, {})
    backfill = api_backfill_progress(db_path, {})
    return {"daily": daily, "sessions_daily": sessions_daily,
            "fable": fable, "provider_cards": provider_cards,
            "top_sessions": top_sessions, "costs": costs,
            "backfill": backfill, "project": project or None,
            "session": session or None, "ops_summary": ops_summary,
            "ops_recent": ops_recent, "projects_rollup": projects_rollup,
            "health": health}


def api_files(db_path, params):
    from fable.filetime import known_files
    q = (params.get("q") or [""])[0]
    return known_files(db_path, q)


def api_filehist(db_path, params):
    from fable.filetime import file_events, reconstruct
    path = (params.get("path") or [""])[0]
    versions = reconstruct(file_events(db_path, path))
    return {"path": path, "versions": [
        {k: v[k] for k in ("uuid", "ts", "tool", "ok", "note", "bytes",
                           "session_id", "prompt_id")}
        for v in versions]}


def api_filediff(db_path, params):
    from fable.filetime import file_events, reconstruct, file_diff
    path = (params.get("path") or [""])[0]
    a = int((params.get("a") or ["0"])[0])
    b = int((params.get("b") or ["0"])[0])
    versions = reconstruct(file_events(db_path, path))
    return {"path": path, "a": a, "b": b,
            "diff": file_diff(versions, a, b)}


def api_fileversion(db_path, params):
    from fable.filetime import file_events, reconstruct
    path = (params.get("path") or [""])[0]
    i = int((params.get("i") or ["0"])[0])
    versions = reconstruct(file_events(db_path, path))
    v = versions[i]
    return {"i": i, "content": v["content"], "ok": v["ok"],
            "note": v["note"], "ts": v["ts"], "tool": v["tool"]}


def api_export(db_path, params):
    from fable.export import export_thread_md, export_thread_html
    prompt_id = (params.get("id") or [""])[0]
    fmt = (params.get("fmt") or ["md"])[0]
    content = (export_thread_html(db_path, prompt_id) if fmt == "html"
               else export_thread_md(db_path, prompt_id))
    return {"content": content, "filename": f"fable-{prompt_id[:8]}.{fmt}"}


def api_facts(db_path, params):
    from fable.facts import list_facts
    return list_facts(db_path, include_inactive=False)


def post_facts(db_path, body):
    from fable.facts import add_fact, forget_fact
    if body.get("forget"):
        return {"forgotten": forget_fact(db_path, int(body["forget"]))}
    fid = add_fact(db_path, body["fact"], project=body.get("project"))
    return {"id": fid}


def api_cards(db_path, params):
    conn = fdb.connect(db_path)
    try:
        return _rows(conn, """
            SELECT c.*, t.session_id, t.turn_count, t.first_ts,
                   t.sidechain_turns, t.models, s.project
            FROM cards c LEFT JOIN threads t ON t.prompt_id = c.prompt_id
            LEFT JOIN sessions s ON s.session_id = t.session_id
            ORDER BY t.first_ts DESC""")
    finally:
        conn.close()


ROUTES = {
    "/api/stats": api_stats,
    "/api/projects": api_projects,
    "/api/search": api_search,
    "/api/threads": api_threads,
    "/api/thread": api_thread,
    "/api/cards": api_cards,
    "/api/generations": api_generations,
    "/api/diff": api_diff,
    "/api/graph": api_graph,
    "/api/suggestions": api_suggestions,
    "/api/context": api_context,
    "/api/facets": api_facets,
}


def post_surgery_plan(db_path, body):
    from fable.surgery import plan
    report, _ = plan(db_path, _live_path(db_path, body["session"]),
                     body["drops"])
    report.pop("chain_broken", None)
    return report


def post_surgery_apply(db_path, body):
    from fable.surgery import apply as surgery_apply
    if not body.get("confirm"):
        raise ValueError("apply requires confirm: true")
    return surgery_apply(db_path, _live_path(db_path, body["session"]),
                         body["drops"], body.get("backup_dir")
                         or _default_backup_dir(db_path, body["session"]),
                         force=bool(body.get("force")))


def _live_path(db_path, session_id):
    conn = fdb.connect(db_path)
    try:
        row = conn.execute(
            "SELECT live_path FROM sessions WHERE session_id = ?",
            (session_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row[0] or not os.path.exists(row[0]):
        raise KeyError(f"no live transcript on disk for session "
                       f"{session_id} — surgery needs a live file")
    return row[0]


def _default_backup_dir(db_path, session_id):
    from fable.discover import DEFAULT_BACKUP_ROOTS
    conn = fdb.connect(db_path)
    try:
        row = conn.execute(
            "SELECT project FROM sessions WHERE session_id = ?",
            (session_id,)).fetchone()
    finally:
        conn.close()
    project = (row[0] if row and row[0] else "manual")
    for root in DEFAULT_BACKUP_ROOTS:
        if os.path.isdir(root):
            return os.path.join(root, project)
    return os.path.join(os.path.dirname(os.path.abspath(db_path)),
                        "backups", project)


# ── card backfill runner (one at a time, progress polled by the UI) ──
BACKFILL = {"running": False, "project": None, "done": 0, "total": 0,
            "generated": 0, "failed": 0, "started": 0.0, "finished": None,
            "error": None}
_BACKFILL_LOCK = threading.Lock()


def post_cards_run(db_path, body):
    import time as _time
    from fable.cards import run_cards
    from fable.providers import PROVIDERS
    project = body.get("project") or None
    session = body.get("session") or None
    provider = body.get("provider") or "openrouter"
    model = body.get("model") or None
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {PROVIDERS}")
    with _BACKFILL_LOCK:
        external = api_backfill_progress(db_path, {})
        if BACKFILL["running"] or external.get("running"):
            raise ValueError(
                "a backfill is already running "
                f"({external.get('done', 0)}/{external.get('total', 0)}) — "
                "stop it first or wait")
        # dry-run to size the job before committing
        dry = run_cards(db_path, project=project, session=session,
                        dry_run=True)
        if not dry["candidates"]:
            raise ValueError("no uncarded threads in scope — nothing to do")
        BACKFILL.update({"running": True, "project": project,
                         "session": session,
                         "provider": provider, "model": model,
                         "done": 0, "total": dry["candidates"],
                         "generated": 0, "failed": 0,
                         "started": _time.time(), "finished": None,
                         "error": None, "stop_requested": False})

    def state(s):
        BACKFILL.update(s)

    def work():
        try:
            stats = run_cards(
                db_path, project=project, session=session, on_state=state,
                provider=provider, model=model,
                should_stop=lambda: BACKFILL.get("stop_requested"))
            BACKFILL["error"] = (stats["errors"][-1]["error"][:200]
                                 if stats.get("aborted") and stats["errors"]
                                 else None)
        except Exception as e:
            BACKFILL["error"] = str(e)[:300]
        finally:
            BACKFILL["running"] = False
            BACKFILL["finished"] = _time.time()

    threading.Thread(target=work, daemon=True).start()
    return {"started": True, "project": project, "provider": provider,
            "candidates": BACKFILL["total"]}


def post_cards_stop(db_path, body):
    if not BACKFILL["running"]:
        return {"stopped": False, "reason": "nothing running"}
    BACKFILL["stop_requested"] = True
    return {"stopped": True}


def post_settings(db_path, body):
    """Save provider API keys from the dashboard into .env (0600)."""
    from fable.openrouter import save_env
    updates = {}
    if body.get("openrouter_key"):
        updates["OPENROUTER_API_KEY"] = body["openrouter_key"].strip()
    if body.get("anthropic_key"):
        updates["ANTHROPIC_API_KEY"] = body["anthropic_key"].strip()
    if body.get("openrouter_model"):
        updates["OPENROUTER_MODEL"] = body["openrouter_model"].strip()
    if not updates:
        raise ValueError("nothing to save")
    save_env(updates)
    return api_settings(db_path, {})


def api_settings(db_path, params):
    from fable.openrouter import load_env, env_path
    from fable.providers import availability
    load_env()

    def mask(key):
        val = os.environ.get(key, "")
        return (val[:7] + "…" + val[-4:]) if len(val) > 14 else bool(val)
    return {"providers": availability(),
            "openrouter_key": mask("OPENROUTER_API_KEY"),
            "anthropic_key": mask("ANTHROPIC_API_KEY"),
            "openrouter_model": os.environ.get("OPENROUTER_MODEL", ""),
            "env_path": env_path()}


def api_backfill_progress(db_path, params):
    import time as _time
    out = dict(BACKFILL)
    if not out["running"]:
        # a run started from the CLI (or another process) persists its
        # state into the DB — surface it so the UI always shows the truth
        conn = fdb.connect(db_path)
        try:
            row = conn.execute("SELECT value FROM meta "
                               "WHERE key='backfill_state'").fetchone()
        finally:
            conn.close()
        if row:
            state = json.loads(row[0])
            state.setdefault("error", None)
            # heartbeat staleness: a killed run never writes running=false
            if (state.get("running") and
                    _time.time() - state.get("updated", 0) > 300):
                state["running"] = False
                state["error"] = "previous run died — restart to resume"
                state["finished"] = state.get("updated")
            if state.get("running") and state.get("started"):
                state["external"] = True
                out = {**out, **state}
            elif state.get("finished") and not out.get("finished"):
                out = {**out, **state}
    if out.get("running") and out.get("done") and out.get("started"):
        rate = out["done"] / max(_time.time() - out["started"], 1)
        out["eta_seconds"] = int((out["total"] - out["done"])
                                 / max(rate, 1e-6))
    return out


ROUTES["/api/backfill"] = api_backfill_progress
ROUTES["/api/settings"] = api_settings
ROUTES["/api/costs"] = api_costs
ROUTES["/api/dashboard"] = api_dashboard
ROUTES["/api/export"] = api_export
ROUTES["/api/files"] = api_files
ROUTES["/api/filehist"] = api_filehist
ROUTES["/api/filediff"] = api_filediff
ROUTES["/api/fileversion"] = api_fileversion
ROUTES["/api/facts"] = api_facts

def post_prune_plan(db_path, body):
    from fable.prune import preview
    return preview(_live_path(db_path, body["session"]))


def post_prune_apply(db_path, body):
    import time as _time
    from fable.prune import prune_file
    if not body.get("confirm"):
        raise ValueError("apply requires confirm: true")
    live = _live_path(db_path, body["session"])
    if (_time.time() - os.path.getmtime(live) < 60
            and not body.get("force")):
        raise ValueError("session looks ACTIVE (modified <60s ago) — "
                         "close it first, or pass force")
    report = prune_file(
        live, "resume",
        backup_dir=_default_backup_dir(db_path, body["session"]),
        replace=True, strip_images=bool(body.get("strip_images")),
        db_path=db_path, force=bool(body.get("force")))
    from fable.extract import fts_extract_fn
    from fable.indexer import index_vault
    index_vault(db_path, [], live_file=live, extract_fn=fts_extract_fn)
    report["reindexed"] = True
    return report


POST_ROUTES = {
    "/api/surgery/plan": post_surgery_plan,
    "/api/surgery/apply": post_surgery_apply,
    "/api/prune/plan": post_prune_plan,
    "/api/prune/apply": post_prune_apply,
    "/api/cards/run": post_cards_run,
    "/api/cards/stop": post_cards_stop,
    "/api/settings": post_settings,
    "/api/facts": post_facts,
    "/api/session/meta": post_session_meta,
    "/api/compose": post_compose,
}


class Handler(BaseHTTPRequestHandler):
    db_path = "fable.db"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            try:
                with open(DASHBOARD, "rb") as f:
                    body = f.read()
                self._send(200, body, "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"dashboard.html missing", "text/plain")
            return
        fn = ROUTES.get(parsed.path)
        if fn is None:
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        try:
            payload = fn(self.db_path, parse_qs(parsed.query))
            self._send(200, json.dumps(payload).encode(),
                       "application/json")
        except (KeyError, ValueError, FileNotFoundError, RuntimeError) as e:
            self._send(400, json.dumps({"error": str(e)}).encode(),
                       "application/json")

    def do_POST(self):
        parsed = urlparse(self.path)
        fn = POST_ROUTES.get(parsed.path)
        if fn is None:
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            payload = fn(self.db_path, body)
            self._send(200, json.dumps(payload).encode(),
                       "application/json")
        except (KeyError, ValueError, FileNotFoundError,
                RuntimeError, json.JSONDecodeError) as e:
            self._send(400, json.dumps({"error": str(e)}).encode(),
                       "application/json")

    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve(db_path: str, port: int = 8765, open_browser: bool = True):
    handler = type("BoundHandler", (Handler,), {"db_path": db_path})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{httpd.server_port}"
    print(f"fable dashboard: {url}  (db: {db_path})  Ctrl-C to stop")
    if open_browser:
        import webbrowser
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def cmd_serve(args):
    fdb.connect(args.db).close()  # fail fast if index missing
    return serve(args.db, port=args.port, open_browser=not args.no_browser)
