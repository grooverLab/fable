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
        # dominant model per session — one grouped pass, pick the max in python
        dom = {}
        for sid, model, c in conn.execute(
                "SELECT session_id, model, COUNT(*) FROM records "
                "WHERE model IS NOT NULL GROUP BY session_id, model"):
            if sid not in dom or c > dom[sid][1]:
                dom[sid] = (model, c)
        projects = {}
        for s in sessions:
            s["model"] = dom.get(s["session_id"], (None,))[0]
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
                  project=one("project"), session=one("session"),
                  tag=one("tag"))


def api_threads(db_path, params):
    session = (params.get("session") or [None])[0]
    conn = fdb.connect(db_path)
    try:
        rows = _rows(conn, """
            SELECT t.prompt_id, t.first_ts, t.last_ts, t.turn_count,
                   t.est_tokens, t.sidechain_turns, t.models,
                   c.title, c.type, c.outcome, c.summary
            FROM threads t LEFT JOIN cards c ON c.prompt_id = t.prompt_id
            WHERE t.session_id = ?
            ORDER BY t.first_ts""", (session,))
        # grade each carded thread from generation health (clean=A, retries drop it)
        att = {}
        for pid, n, ok in conn.execute(
                "SELECT prompt_id, COUNT(*), COALESCE(SUM(ok),0) "
                "FROM card_attempts WHERE prompt_id IS NOT NULL "
                "GROUP BY prompt_id"):
            att[pid] = (n, ok)
        for r in rows:
            if r.get("title"):
                n, ok = att.get(r["prompt_id"], (1, 1))
                fails = max(0, n - ok)
                r["grade"] = ("A" if fails == 0 else "B" if fails == 1
                              else "C" if fails == 2 else "D")
        return rows
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
        tag_sql = "SELECT family, value FROM thread_tags WHERE 1=1 "
        if project:
            tag_sql += ("AND prompt_id IN (SELECT prompt_id FROM threads "
                        "WHERE 1=1" + scope_sql + ") ")
        tag_sql += "GROUP BY family, value ORDER BY COUNT(*) DESC LIMIT 60"
        tags = [f"{f}:{v}" for f, v in conn.execute(tag_sql, scope_args)]
        return {"models": sorted(models), "projects": projects,
                "operatives": operatives, "tags": tags}
    finally:
        conn.close()


def api_tags(db_path, params):
    """Tag analytics over thread_tags — all LIVE queries, no warehouse:
    value distribution per family, cross-family co-occurrence (semantic
    clusters), tag x outcome, and tag x project."""
    SEM = ("topic", "technology", "pattern", "intent", "context", "entity")
    conn = fdb.connect(db_path)
    try:
        # 1) value distribution per family (top 15 each) — the "tag map"
        by_family = {}
        for fam, val, n in conn.execute(
                "SELECT family, value, COUNT(DISTINCT prompt_id) n "
                "FROM thread_tags GROUP BY family, value "
                "ORDER BY family, n DESC"):
            row = by_family.setdefault(fam, [])
            if len(row) < 15:
                row.append({"value": val, "n": n})
        # 2) cross-family co-occurrence among SEMANTIC families (clusters)
        ph = ",".join("?" * len(SEM))
        cooccur = [{"a": a, "b": b, "n": n} for a, b, n in conn.execute(
            "SELECT a.family||':'||a.value, b.family||':'||b.value, COUNT(*) n "
            "FROM thread_tags a JOIN thread_tags b "
            "ON a.prompt_id=b.prompt_id AND a.family<b.family "
            f"WHERE a.family IN ({ph}) AND b.family IN ({ph}) "
            "GROUP BY 1,2 HAVING n>=2 ORDER BY n DESC LIMIT 40",
            list(SEM) + list(SEM))]
        # 3) tag x outcome — a work tag co-occurring with the outcome tag
        outc = {}
        for tag, oc, n in conn.execute(
                "SELECT t.family||':'||t.value, o.value, COUNT(*) n "
                "FROM thread_tags t JOIN thread_tags o "
                "ON o.prompt_id=t.prompt_id AND o.family='outcome' "
                "WHERE t.family IN ('topic','technology','pattern','activity') "
                "GROUP BY 1,2"):
            d = outc.setdefault(tag, {"_total": 0})
            d[oc] = n
            d["_total"] += n
        outcome = sorted(({"tag": k, **v} for k, v in outc.items()),
                         key=lambda r: -r["_total"])[:20]
        # 4) tag x project — top topic/technology per project
        proj = {}
        for pj, tag, n in conn.execute(
                "SELECT s.project, t.family||':'||t.value, "
                "COUNT(DISTINCT t.prompt_id) n FROM thread_tags t "
                "JOIN threads th ON th.prompt_id=t.prompt_id "
                "JOIN sessions s ON s.session_id=th.session_id "
                "WHERE t.family IN ('topic','technology') "
                "AND s.project IS NOT NULL GROUP BY s.project, 2 "
                "ORDER BY s.project, n DESC"):
            row = proj.setdefault(pj, [])
            if len(row) < 8:
                row.append({"tag": tag, "n": n})
        total = conn.execute(
            "SELECT COUNT(DISTINCT prompt_id) FROM thread_tags").fetchone()[0]
        return {"tagged_threads": total, "by_family": by_family,
                "cooccur": cooccur, "outcome": outcome, "by_project": proj}
    finally:
        conn.close()


def api_tags_proposed(db_path, params):
    """Invented values (every family except the sacred domain) not yet in the
    known vocab — the triage queue."""
    from fable import taxonomy as tax
    t = tax.load_taxonomy()
    known = {}
    for grp in ("controlled", "semantic"):
        for f, vals in (t.get(grp) or {}).items():
            known[f] = set(vals or [])
    bl = {tuple(x) for x in (t.get("blacklist") or [])}
    conn = fdb.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT family, value, COUNT(DISTINCT prompt_id) n FROM thread_tags"
            " WHERE family != 'domain' GROUP BY family, value ORDER BY n DESC"
        ).fetchall()
    finally:
        conn.close()
    out = [{"family": f, "value": v, "threads": n} for f, v, n in rows
           if v not in known.get(f, set()) and (f, v) not in bl]
    return {"proposed": out[:300], "total": len(out)}


def post_tags_promote(db_path, body):
    from fable import taxonomy as tax
    fam, val = body["family"], body["value"]
    tax.promote(fam, val)
    return {"ok": True, "promoted": f"{fam}:{val}"}


def post_tags_blacklist(db_path, body):
    from fable import taxonomy as tax
    fam, val = body["family"], body["value"]
    tax.blacklist_value(fam, val)
    conn = fdb.connect(db_path)               # drop existing rows immediately
    try:
        conn.execute("DELETE FROM thread_tags WHERE family=? AND value=?",
                     (fam, val))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "blacklisted": f"{fam}:{val}"}


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
    """Memory graph v2 — built from signals that actually exist:
    thread nodes (always labeled), FILE nodes (paths threads edited),
    TOPIC nodes (LLM card topics), semantic edges (embedding cosine),
    citations. TF-IDF trigram terms are gone; wikilinks kept if present."""
    session = (params.get("session") or [None])[0]
    cap = int((params.get("cap") or ["120"])[0])
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
                   t.session_id, c.title, c.type, c.topics
            FROM threads t LEFT JOIN cards c ON c.prompt_id = t.prompt_id
            WHERE t.prompt_id IN ({ph})""", pids)
        # every thread gets a human label: card title, else its first
        # user words from FTS
        untitled = [t["prompt_id"] for t in threads if not t["title"]]
        first_words = {}
        if untitled:
            uph = ",".join("?" * len(untitled))
            for pid, content in conn.execute(f"""
                    SELECT prompt_id, content FROM fts
                    WHERE prompt_id IN ({uph}) AND kind LIKE '%text%'
                    """, untitled):
                if pid not in first_words and content:
                    first_words[pid] = content.strip().split("\n")[0][:60]

        nodes, links = [], []
        for t in threads:
            nodes.append({
                "id": t["prompt_id"], "group": "thread",
                "label": (t["title"] or first_words.get(t["prompt_id"])
                          or t["prompt_id"][:8]),
                "type": t["type"], "tokens": t["est_tokens"],
                "turns": t["turn_count"], "carded": bool(t["title"])})

        # ── FILE nodes: path-shaped targets, df 2..50 ──
        file_rows = _rows(conn, f"""
            SELECT term, prompt_id, score FROM terms
            WHERE kind = 'target' AND prompt_id IN ({ph})""", pids)
        by_file = {}
        for fr in file_rows:
            by_file.setdefault(fr["term"], []).append(fr)
        shared_files = sorted(
            ((k, v) for k, v in by_file.items() if 2 <= len(v) <= 50),
            key=lambda kv: -len(kv[1]))[:60]
        for path, rows in shared_files:
            nid = f"file:{path}"
            nodes.append({"id": nid, "group": "file",
                          "label": path.split("/")[-1], "full": path,
                          "df": len(rows)})
            for fr in rows:
                links.append({"source": fr["prompt_id"], "target": nid,
                              "kind": "file",
                              "weight": min(fr["score"], 10)})

        # ── TOPIC nodes: LLM-chosen card topics, df>=2 ──
        topics = {}
        for t in threads:
            try:
                for topic in json.loads(t["topics"] or "[]"):
                    topic = str(topic).strip().lower()[:40]
                    if topic:
                        topics.setdefault(topic, set()).add(t["prompt_id"])
            except ValueError:
                pass
        for topic, members in sorted(topics.items(),
                                     key=lambda kv: -len(kv[1]))[:50]:
            if len(members) < 2:
                continue
            nid = f"topic:{topic}"
            nodes.append({"id": nid, "group": "topic", "label": topic,
                          "df": len(members)})
            for pid in members:
                links.append({"source": pid, "target": nid,
                              "kind": "topic", "weight": 3})

        # ── TAG nodes: carder taxonomy tags, df>=2 (family:value co-occurrence) ──
        tag_members = {}
        for fam, val, pid in conn.execute(f"""
                SELECT family, value, prompt_id FROM thread_tags
                WHERE prompt_id IN ({ph})""", pids):
            tag_members.setdefault((fam, val), set()).add(pid)
        for (fam, val), members in sorted(tag_members.items(),
                                          key=lambda kv: -len(kv[1]))[:60]:
            if len(members) < 2:
                continue
            nid = f"tag:{fam}:{val}"
            nodes.append({"id": nid, "group": "tag",
                          "label": f"{fam}:{val}", "family": fam,
                          "df": len(members)})
            for pid in members:
                links.append({"source": pid, "target": nid,
                              "kind": "tag", "weight": 3})

        # ── wikilinks (first-class when present; top 60 by spread so a
        # tag-happy archive can't bury the graph) ──
        wl = {}
        for term, pid, score in conn.execute(f"""
                SELECT term, prompt_id, score FROM terms
                WHERE kind = 'wikilink' AND prompt_id IN ({ph})""", pids):
            wl.setdefault(term, []).append(pid)
        for term, wpids in sorted(wl.items(),
                                  key=lambda kv: -len(kv[1]))[:60]:
            nid = f"wiki:{term}"
            nodes.append({"id": nid, "group": "wikilink",
                          "label": f"[[{term}]]", "df": len(wpids)})
            for pid in wpids:
                links.append({"source": pid, "target": nid,
                              "kind": "wikilink", "weight": 4})

        # ── semantic edges: embedding cosine between carded threads ──
        try:
            import struct
            vecs = {}
            for pid, blob, dim in conn.execute(f"""
                    SELECT prompt_id, vec, dim FROM embeddings
                    WHERE prompt_id IN ({ph})""", pids):
                vecs[pid] = struct.unpack(f"{dim}f", blob)
            keys = list(vecs)
            for i, a in enumerate(keys):
                va = vecs[a]
                na = sum(x * x for x in va) ** 0.5 or 1
                best = []
                for b in keys[i + 1:]:
                    vb = vecs[b]
                    nb = sum(x * x for x in vb) ** 0.5 or 1
                    cos = sum(x * y for x, y in zip(va, vb)) / (na * nb)
                    if cos > 0.72:
                        best.append((cos, b))
                for cos, b in sorted(best, reverse=True)[:3]:
                    links.append({"source": a, "target": b,
                                  "kind": "semantic",
                                  "weight": round(cos * 10, 1)})
        except Exception:
            pass

        # ── citations ──
        cites = _rows(conn, f"""
            SELECT DISTINCT r.prompt_id AS src, ci.ref AS dst
            FROM citations ci JOIN records r ON r.uuid = ci.from_uuid
            WHERE r.prompt_id IN ({ph})""", pids)
        ids = {n["id"] for n in nodes}
        for c in cites:
            if c["src"] in ids and c["dst"] in ids:
                links.append({"source": c["src"], "target": c["dst"],
                              "kind": "citation", "weight": 5})

        # drop isolated thread nodes — the sparse feel came from singletons
        degree = {}
        for l in links:
            degree[l["source"]] = degree.get(l["source"], 0) + 1
            degree[l["target"]] = degree.get(l["target"], 0) + 1
        nodes = [n for n in nodes
                 if degree.get(n["id"]) or n["group"] != "thread"]
        ids = {n["id"] for n in nodes}
        links = [l for l in links
                 if l["source"] in ids and l["target"] in ids]
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
        {**{k: v[k] for k in ("uuid", "ts", "tool", "ok", "note",
                              "bytes", "session_id", "prompt_id")},
         "derived": bool(v.get("derived"))}
        for v in versions]}


def api_filestory(db_path, params):
    """File evolution correlated with the reasoning behind each change: the
    file's versions grouped by the thread that produced them, each annotated
    with that thread's card (decision / outcome) and tags — what changed, why."""
    from fable.filetime import file_events, reconstruct
    path = (params.get("path") or [""])[0]
    versions = reconstruct(file_events(db_path, path))
    groups = []
    for i, v in enumerate(versions):
        pid = v.get("prompt_id")
        if groups and groups[-1]["prompt_id"] == pid:
            groups[-1]["to"] = i
            groups[-1]["edits"] += 1
            groups[-1]["bytes"] = v.get("bytes")
        else:
            groups.append({"prompt_id": pid, "from": i, "to": i, "edits": 1,
                           "ts": v.get("ts"), "bytes": v.get("bytes"),
                           "tool": v.get("tool"),
                           "session_id": v.get("session_id")})
    conn = fdb.connect(db_path)
    try:
        for g in groups:
            pid = g["prompt_id"]
            if not pid:
                continue
            card = conn.execute(
                "SELECT title, type, outcome, decisions FROM cards "
                "WHERE prompt_id = ?", (pid,)).fetchone()
            if card:
                try:
                    g["decisions"] = json.loads(card[3] or "[]")
                except (ValueError, TypeError):
                    g["decisions"] = []
                g["title"], g["type"], g["outcome"] = card[0], card[1], card[2]
                g["tags"] = ["%s:%s" % (f, val) for f, val in conn.execute(
                    "SELECT family, value FROM thread_tags WHERE prompt_id = ? "
                    "ORDER BY family", (pid,))]
    finally:
        conn.close()
    return {"path": path, "groups": groups}


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


def api_sessionfiles(db_path, params):
    from fable.filetime import session_files
    session = (params.get("session") or [""])[0]
    return session_files(db_path, session)


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


def _grade(fails):
    """Card quality from generation health: clean first try = A, each retry
    that failed before success drops the letter."""
    return "A" if fails <= 0 else "B" if fails == 1 else "C" if fails == 2 else "D"


def api_cards(db_path, params):
    conn = fdb.connect(db_path)
    try:
        rows = _rows(conn, """
            SELECT c.*, t.session_id, t.turn_count, t.first_ts,
                   t.sidechain_turns, t.models, s.project
            FROM cards c LEFT JOIN threads t ON t.prompt_id = c.prompt_id
            LEFT JOIN sessions s ON s.session_id = t.session_id
            ORDER BY t.first_ts DESC""")
        att = {}
        for pid, n, ok in conn.execute(
                "SELECT prompt_id, COUNT(*), COALESCE(SUM(ok),0) "
                "FROM card_attempts WHERE prompt_id IS NOT NULL "
                "GROUP BY prompt_id"):
            att[pid] = (n, ok)
        for r in rows:
            n, ok = att.get(r["prompt_id"], (1, 1))
            r["gen_attempts"] = n
            r["gen_fails"] = max(0, n - ok)
            r["grade"] = _grade(r["gen_fails"])
        return rows
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
    from fable.paths import vault_dir
    conn = fdb.connect(db_path)
    try:
        row = conn.execute(
            "SELECT project FROM sessions WHERE session_id = ?",
            (session_id,)).fetchone()
    finally:
        conn.close()
    project = (row[0] if row and row[0] else "manual")
    return os.path.join(vault_dir(), project)


# ── card backfill runner (one at a time, progress polled by the UI) ──
BACKFILL = {"running": False, "project": None, "done": 0, "total": 0,
            "generated": 0, "failed": 0, "started": 0.0, "finished": None,
            "error": None}
_BACKFILL_LOCK = threading.Lock()
_WORKER = {"active": False}  # is a server-side queue-drain worker running?


def _friendly_error(msg):
    """Map a raw provider/backfill error to clear UI guidance."""
    m = (msg or "").lower()
    if any(k in m for k in ("401", "user not found", "invalid api key",
                            "unauthorized", "no api key", "403")):
        return "provider key invalid or expired — update it in Settings"
    if any(k in m for k in ("429", "rate", "quota", "limit")):
        return ("rate limited / daily quota reached — it retries automatically;"
                " resume later")
    if any(k in m for k in ("empty", "non-json", "expecting value",
                            "overloaded", "unexpected response")):
        return "provider returned empty/garbled responses (overloaded) — retrying"
    if any(k in m for k in ("connection", "timed out", "timeout", "urlopen",
                            "refused", "could not reach")):
        return "couldn't reach the provider — check your network / Ollama"
    return (msg or "")[:140]


def _drain_worker(db_path):
    """One server-side worker drains the DB job queue, one job after another.
    The control state (running/stop/queue) lives in the DB, so the dashboard
    always shows the truth and can always control a run — no matter who
    started it (sidebar, this worker, or a CLI process)."""
    import time as _time
    from fable import cards as _c
    _WORKER["active"] = True
    try:
        while True:
            st0 = _c.read_backfill_state(db_path)
            if st0.get("stop"):
                break
            if st0.get("paused"):
                break   # paused: leave the queue intact, just stop draining
            job = _c.pop_job(db_path)
            if not job:
                break
            BACKFILL.update({"running": True, "project": job.get("project"),
                             "session": job.get("session"),
                             "provider": job.get("provider"),
                             "model": job.get("model"),
                             "done": 0, "total": job.get("candidates", 0),
                             "generated": 0, "failed": 0,
                             "started": _time.time(), "finished": None,
                             "error": None, "stop_requested": False})
            try:
                stats = _c.run_cards(
                    db_path, project=job.get("project"),
                    session=job.get("session"),
                    provider=job.get("provider"), model=job.get("model"),
                    on_state=lambda s: BACKFILL.update(s),
                    should_stop=lambda: BACKFILL.get("stop_requested"))
                BACKFILL["error"] = (stats["errors"][-1]["error"][:200]
                                     if stats.get("aborted")
                                     and stats["errors"] else None)
            except Exception as e:
                BACKFILL["error"] = str(e)[:300]
            BACKFILL["running"] = False
            BACKFILL["finished"] = _time.time()
            # paused mid-job → re-queue the partially-done job so resume
            # re-cards whatever threads remain uncarded, then stop draining
            if _c.read_backfill_state(db_path).get("paused"):
                _c.enqueue_job(db_path, job)
                break
    finally:
        _WORKER["active"] = False
        _c.clear_stop(db_path)


def post_cards_run(db_path, body):
    from fable.cards import run_cards, enqueue_job, clear_stop
    from fable.providers import PROVIDERS
    project = body.get("project") or None
    session = body.get("session") or None
    provider = body.get("provider") or "openrouter"
    model = body.get("model") or None
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {PROVIDERS}")
    LAST_PROVIDER[0] = provider
    dry = run_cards(db_path, project=project, session=session, dry_run=True)
    if not dry["candidates"]:
        raise ValueError("no uncarded threads ≥200 tokens in scope — "
                         "short threads (<200 tok) are skipped by design")
    label = (f"session {session[:8]}" if session
             else (project or "all projects"))
    job = {"project": project, "session": session, "provider": provider,
           "model": model, "label": label, "candidates": dry["candidates"]}
    with _BACKFILL_LOCK:
        position = enqueue_job(db_path, job)
        if not _WORKER["active"]:
            clear_stop(db_path)
            threading.Thread(target=_drain_worker, args=(db_path,),
                             daemon=True).start()
            started = True
        else:
            started = False
    return {"queued": True, "position": position, "label": label,
            "started": started, "candidates": dry["candidates"]}


def post_cards_stop(db_path, body):
    import time as _time
    from fable.cards import (request_stop, read_backfill_state,
                             _update_backfill_state)
    # DB stop flag → honored by the server worker AND any external/CLI run;
    # also clears the queue
    request_stop(db_path)
    BACKFILL["stop_requested"] = True
    # if nothing is actively updating the state (an orphaned run after a
    # restart), force the visible state to stopped so the UI shows the truth
    st = read_backfill_state(db_path)
    fresh = (_time.time() - st.get("updated", 0)) < 30
    if not (_WORKER["active"] or fresh):
        _update_backfill_state(db_path, lambda s: {
            **s, "running": False, "finished": _time.time(), "queue": []})
    return {"stopped": True}


def post_cards_dequeue(db_path, body):
    from fable.cards import remove_job
    remove_job(db_path, int(body.get("index", -1)))
    return {"ok": True}


def post_cards_pause(db_path, body):
    # pause = stop draining + abort the current run gracefully (cards already
    # generated are kept), but KEEP the queue so resume continues where it left
    from fable.cards import request_pause
    request_pause(db_path)
    BACKFILL["stop_requested"] = True
    return {"paused": True}


def post_cards_resume(db_path, body):
    from fable.cards import clear_pause, clear_stop
    clear_pause(db_path)
    clear_stop(db_path)
    BACKFILL["stop_requested"] = False
    with _BACKFILL_LOCK:
        if not _WORKER["active"]:
            threading.Thread(target=_drain_worker, args=(db_path,),
                             daemon=True).start()
    return {"resumed": True}


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
    meta = {}
    if "autoprune_enabled" in body:
        meta["autoprune_enabled"] = "1" if body["autoprune_enabled"] else "0"
    if "autoprune_pct" in body:
        meta["autoprune_pct"] = str(max(50, min(95,
                                                float(body["autoprune_pct"]))))
    if "recard_mode" in body:
        meta["recard_mode"] = "1" if body["recard_mode"] else "0"
    if "externalize_enabled" in body:
        meta["externalize_enabled"] = "1" if body["externalize_enabled"] else "0"
    if not updates and not meta:
        raise ValueError("nothing to save")
    if updates:
        save_env(updates)
    if meta:
        conn = fdb.connect(db_path)
        for k, v in meta.items():
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (k, v))
        conn.commit()
        conn.close()
    return api_settings(db_path, {})


def post_setup(db_path, body):
    """Onboarding from the dashboard: point fable's vault at a chosen folder.

    Writes ~/.fable/config.json (same as `fable setup`). The current vault is
    auto-registered as a legacy read-root so older generations stay visible.
    Takes effect for new prune/surgery/compaction writes after a restart."""
    from fable import paths
    from fable.setup import run_setup
    vault = (body.get("vault") or "").strip()
    if not vault:
        raise ValueError("vault path required")
    old_vault = paths.vault_dir()
    legacy = list(paths.load_config().get("backup_roots", []) or [])
    if old_vault and os.path.abspath(os.path.expanduser(old_vault)) != \
            os.path.abspath(os.path.expanduser(vault)):
        legacy.append(old_vault)
    return run_setup(vault=vault, legacy_roots=legacy)


def api_settings(db_path, params):
    from fable.openrouter import load_env, env_path
    from fable.providers import availability
    load_env()

    def mask(key):
        val = os.environ.get(key, "")
        return (val[:7] + "…" + val[-4:]) if len(val) > 14 else bool(val)
    conn = fdb.connect(db_path)
    try:
        cfg = dict(conn.execute(
            "SELECT key, value FROM meta WHERE key IN "
            "('autoprune_enabled','autoprune_pct','recard_mode',"
            "'externalize_enabled')").fetchall())
    finally:
        conn.close()
    from fable import paths
    return {"providers": availability(),
            "openrouter_key": mask("OPENROUTER_API_KEY"),
            "anthropic_key": mask("ANTHROPIC_API_KEY"),
            "openrouter_model": os.environ.get("OPENROUTER_MODEL", ""),
            "env_path": env_path(),
            "autoprune_enabled": cfg.get("autoprune_enabled") == "1",
            "autoprune_pct": float(cfg.get("autoprune_pct") or 80),
            "recard_mode": cfg.get("recard_mode") == "1",
            "externalize_enabled": cfg.get("externalize_enabled") != "0",
            "storage": {"home": paths.home(), "db": db_path,
                        "vault": paths.vault_dir(),
                        "checkpoints": paths.checkpoints_dir(),
                        "config": paths.config_path(),
                        "backup_roots": paths.backup_roots()}}


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
    # always surface the queue from the DB so the UI can show pending jobs
    try:
        conn = fdb.connect(db_path)
        row = conn.execute("SELECT value FROM meta "
                           "WHERE key='backfill_state'").fetchone()
        conn.close()
        dbst = json.loads(row[0]) if row else {}
        out["queue"] = (dbst.get("queue") or [])
        out["paused"] = bool(dbst.get("paused"))
    except Exception:
        out["queue"] = []
        out["paused"] = False
    if out.get("error"):
        out["error_raw"] = out["error"]
        out["error"] = _friendly_error(out["error"])
    return out


def api_cards_health(db_path, params):
    """Card-generation reliability per provider x model + failure reasons."""
    conn = fdb.connect(db_path)
    try:
        rows = _rows(conn, """
            SELECT provider, model, COUNT(*) AS attempts, SUM(ok) AS ok,
                   COUNT(*) - SUM(ok) AS failed, MAX(ts) AS last_ts
            FROM card_attempts GROUP BY provider, model
            ORDER BY attempts DESC""")
        for r in rows:
            r["ok"] = r["ok"] or 0
            r["success_rate"] = round(100 * r["ok"] / max(r["attempts"], 1), 1)
            r["reasons"] = dict(conn.execute(
                "SELECT reason, COUNT(*) FROM card_attempts WHERE provider IS ?"
                " AND model IS ? AND ok = 0 AND reason IS NOT NULL "
                "GROUP BY reason ORDER BY COUNT(*) DESC",
                (r["provider"], r["model"])).fetchall())
        tot = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(ok),0) FROM card_attempts").fetchone()
        return {"by": rows, "attempts": tot[0] or 0, "ok": tot[1] or 0}
    finally:
        conn.close()


def api_logs(db_path, params):
    """Everything fable tracked: recent ops, card-generation attempts, the
    errors.log tail, and current backfill state — one place for the user."""
    conn = fdb.connect(db_path)
    try:
        ops = _rows(conn, "SELECT ts, kind, detail FROM ops "
                          "ORDER BY id DESC LIMIT 60")
        attempts = _rows(conn, "SELECT ts, provider, model, ok, reason "
                               "FROM card_attempts ORDER BY ROWID DESC LIMIT 40")
    finally:
        conn.close()
    errors = ""
    try:
        elog = os.path.join(os.path.dirname(os.path.abspath(db_path)),
                            "errors.log")
        if os.path.exists(elog):
            with open(elog) as fh:
                errors = "".join(fh.readlines()[-120:])
    except OSError:
        pass
    return {"ops": ops, "attempts": attempts, "errors": errors,
            "backfill": api_backfill_progress(db_path, {})}


ROUTES["/api/logs"] = api_logs
ROUTES["/api/cards/health"] = api_cards_health
ROUTES["/api/backfill"] = api_backfill_progress
ROUTES["/api/settings"] = api_settings
ROUTES["/api/costs"] = api_costs
ROUTES["/api/dashboard"] = api_dashboard
ROUTES["/api/tags"] = api_tags
ROUTES["/api/tags/proposed"] = api_tags_proposed
ROUTES["/api/export"] = api_export
ROUTES["/api/files"] = api_files
def api_filediff2(db_path, params):
    """Side-by-side diff rows (difflib opcodes) for the Files tab."""
    from fable.filetime import file_events, reconstruct
    import difflib
    one = lambda k: (params.get(k) or [None])[0]
    path, a, b = one("path"), int(one("a") or 0), int(one("b") or 0)
    versions = reconstruct(file_events(db_path, path))
    va, vb = versions[a], versions[b]
    if va["content"] is None or vb["content"] is None:
        raise ValueError("one of the selected versions is not "
                         "reconstructable")
    A, B = va["content"].splitlines(), vb["content"].splitlines()
    rows = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, A, B, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append({"op": "eq", "al": i1 + k + 1, "a": A[i1 + k],
                             "bl": j1 + k + 1, "b": B[j1 + k]})
        else:
            la, lb = i2 - i1, j2 - j1
            for k in range(max(la, lb)):
                rows.append({
                    "op": tag,
                    "al": i1 + k + 1 if k < la else None,
                    "a": A[i1 + k] if k < la else None,
                    "bl": j1 + k + 1 if k < lb else None,
                    "b": B[j1 + k] if k < lb else None})
        if len(rows) > 8000:
            rows.append({"op": "eq", "al": None, "a": "… diff truncated …",
                         "bl": None, "b": "… diff truncated …"})
            break
    return {"rows": rows,
            "a": {"i": a, "ts": va["ts"], "tool": va["tool"]},
            "b": {"i": b, "ts": vb["ts"], "tool": vb["tool"]}}


ROUTES["/api/filediff2"] = api_filediff2
ROUTES["/api/filehist"] = api_filehist
ROUTES["/api/filestory"] = api_filestory
ROUTES["/api/filediff"] = api_filediff
ROUTES["/api/fileversion"] = api_fileversion
ROUTES["/api/sessionfiles"] = api_sessionfiles
ROUTES["/api/facts"] = api_facts

def post_prune_plan(db_path, body):
    import time as _time
    from fable.prune import preview
    live = _live_path(db_path, body["session"])
    rep = preview(live)
    age = _time.time() - os.path.getmtime(live)
    rep["seconds_since_modified"] = int(age)
    rep["active"] = age < 60
    rep["cooldown_remaining"] = max(0, int(60 - age))
    return rep


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


def api_curate_timeline(db_path, params):
    from fable import curate
    session = (params.get("session") or [None])[0]
    if not session:
        raise ValueError("session required")
    live = _live_path(db_path, session)
    # an ACTIVE session's live file outgrows the index between hook fires, so
    # its newest (post-compaction) turns have no prompt_id yet and fall out of
    # thread grouping. Reindex it — but only when it actually grew.
    try:
        conn = fdb.connect(db_path)
        row = conn.execute("SELECT size FROM files WHERE path = ?",
                           (live,)).fetchone()
        conn.close()
        if not row or row[0] != os.path.getsize(live):
            from fable.extract import fts_extract_fn
            from fable.indexer import index_vault
            index_vault(db_path, [], live_file=live, extract_fn=fts_extract_fn)
    except Exception:
        pass
    return curate.timeline(live, db_path=db_path)


def post_curate_plan(db_path, body):
    from fable import curate
    return curate.plan(_live_path(db_path, body["session"]),
                       body.get("focus") or [])


def post_curate_apply(db_path, body):
    import time as _time
    from fable import curate
    if not body.get("confirm"):
        raise ValueError("apply requires confirm: true")
    live = _live_path(db_path, body["session"])
    if (_time.time() - os.path.getmtime(live) < 60
            and not body.get("force")):
        raise ValueError("session looks ACTIVE (modified <60s ago) — Claude "
                         "Code is still writing to it; /exit first, or force")
    # seal lands in the per-user vault (~/.fable/vault/<project>) like every
    # other backup — same resolver, no hardcoded path
    return curate.apply(
        live, body.get("focus") or [], summary_text=body.get("summary") or "",
        db_path=db_path,
        backup_dir=_default_backup_dir(db_path, body["session"]))


ROUTES["/api/curate/timeline"] = api_curate_timeline


def api_prune_analyze(db_path, params):
    """Per-category prune savings for a session (read-only; nothing removed)."""
    from fable import slimmers
    session = (params.get("session") or [None])[0]
    if not session:
        raise ValueError("session required")
    return slimmers.analyze_session(_live_path(db_path, session))


ROUTES["/api/prune/analyze"] = api_prune_analyze


def post_prune_slim(db_path, body):
    """Apply per-category slimming to a session's live transcript — seals to the
    vault first, rewrites with stubbed blocks, reindexes. Refuses an actively-
    written file (<60s) unless force."""
    import time as _t
    from fable import slimmers
    if not body.get("confirm"):
        raise ValueError("apply requires confirm: true")
    session = body.get("session")
    if not session:
        raise ValueError("session required")
    live = _live_path(db_path, session)
    if (_t.time() - os.path.getmtime(live) < 60) and not body.get("force"):
        raise ValueError("session looks ACTIVE (modified <60s ago) — "
                         "/exit it in Claude Code first, or pass force")
    return slimmers.apply_session(live, body.get("categories") or [],
                                  _default_backup_dir(db_path, session),
                                  db_path=db_path)


POST_ROUTES = {
    "/api/curate/plan": post_curate_plan,
    "/api/curate/apply": post_curate_apply,
    "/api/surgery/plan": post_surgery_plan,
    "/api/surgery/apply": post_surgery_apply,
    "/api/prune/plan": post_prune_plan,
    "/api/prune/slim": post_prune_slim,
    "/api/prune/apply": post_prune_apply,
    "/api/cards/run": post_cards_run,
    "/api/cards/stop": post_cards_stop,
    "/api/cards/pause": post_cards_pause,
    "/api/cards/resume": post_cards_resume,
    "/api/cards/dequeue": post_cards_dequeue,
    "/api/settings": post_settings,
    "/api/setup": post_setup,
    "/api/facts": post_facts,
    "/api/session/meta": post_session_meta,
    "/api/compose": post_compose,
}


# ── parallel card-backfill pool ───────────────────────────────────────────
# Up to MAX_PARALLEL jobs run at once (same provider in practice — the sidebar
# picks one). Each job is independently run / pause / stop / cancel. Pausing a
# running job frees its slot so the next queued job starts — the parallel cap
# is hard. Replaces the old single _drain_worker (now dead code above).
import itertools as _it
MAX_PARALLEL = 3
_JOBS = {}
_JOB_SEQ = _it.count(1)
_POOL_LOCK = threading.Lock()


def _t_now():
    import time as _t
    return _t.time()


def _job_pub(j):
    return {k: j.get(k) for k in (
        "id", "project", "session", "provider", "model", "label",
        "candidates", "status", "done", "total", "generated", "failed",
        "error", "started", "finished", "rate_limited")}


def _run_job(jid):
    from fable import cards as _c
    j = _JOBS.get(jid)
    if not j:
        return
    db = j["_db"]
    j["started"] = _t_now()
    # the legacy GLOBAL DB stop flag makes run_cards exit on its first loop
    # iteration — the pool stops jobs individually via should_stop, so clear it
    try:
        _c.clear_stop(db)
    except Exception:
        pass
    try:
        stats = _c.run_cards(
            db, project=j["project"], session=j["session"],
            provider=j["provider"], model=j["model"],
            recard=j.get("recard", False),
            abort_after=4, backoff_schedule=(0,),
            on_state=lambda s: j.update(s),
            should_stop=lambda: j["_pause"] or j["_stop"] or j["_cancel"])
        j["generated"] = stats.get("generated", j.get("generated", 0))
        j["failed"] = stats.get("failed", j.get("failed", 0))
        errs = stats.get("errors") or []
        j["_rate"] = any(any(k in str(e).lower() for k in
                         ("429", "rate", "quota", "limit")) for e in errs)
        if stats.get("aborted") and errs:
            last = errs[-1]
            j["error"] = str(last.get("error") if isinstance(last, dict)
                             else last)[:160]
    except Exception as e:
        msg = str(e)
        j["_rate"] = any(k in msg.lower()
                         for k in ("429", "rate", "quota", "limit"))
        j["error"] = _friendly_error(msg)
        if not j["_rate"]:
            j["status"] = "error"
    finally:
        j["finished"] = _t_now()
        gen = j.get("generated") or 0
        if j.get("_cancel"):
            _JOBS.pop(jid, None)
        elif j["_stop"]:
            j["status"] = "stopped"
        elif j["_pause"]:
            j["status"] = "paused"
        elif j.get("_rate"):
            j["status"] = "paused"
            j["rate_limited"] = True
            j["error"] = ("rate-limited (HTTP 429 / quota) — paused; "
                          "resume when the quota resets")
        elif j.get("status") == "error":
            pass                                    # an exception already set it
        elif gen == 0 and (j.get("failed") or 0) > 0:
            j["status"] = "error"                   # ran but carded nothing
            if not j.get("error"):
                j["error"] = "all cards failed — check the provider (Logs)"
        else:
            j["status"] = "done"
            if j.get("recard"):
                global RECARD_DONE
                RECARD_DONE = {"at": j["finished"], "n": gen}
                try:                       # auto-flip the switch back off
                    c = fdb.connect(db)
                    c.execute("INSERT OR REPLACE INTO meta(key, value) "
                              "VALUES('recard_mode','0')")
                    c.commit()
                    c.close()
                except Exception:
                    pass
        _schedule(db)


def _schedule(db_path):
    with _POOL_LOCK:
        running = sum(1 for x in _JOBS.values() if x["status"] == "running")
        for j in sorted((x for x in _JOBS.values()
                         if x["status"] == "queued"), key=lambda x: x["id"]):
            if running >= MAX_PARALLEL:
                break
            j["status"] = "running"
            running += 1
            threading.Thread(target=_run_job, args=(j["id"],),
                             daemon=True).start()


def post_cards_run(db_path, body):
    from fable.cards import run_cards
    from fable.providers import PROVIDERS
    project = body.get("project") or None
    session = body.get("session") or None
    provider = body.get("provider") or "openrouter"
    model = body.get("model") or None
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {PROVIDERS}")
    LAST_PROVIDER[0] = provider
    # an explicit {recard:true} (the "re-card all now" button) always re-cards;
    # otherwise the Settings switch governs whether a run replaces existing cards
    recard = bool(body.get("recard")) or _recard_mode(db_path)
    dry = run_cards(db_path, project=project, session=session,
                    dry_run=True, recard=recard)
    if not dry["candidates"]:
        raise ValueError("no uncarded threads ≥200 tokens in scope — "
                         "short threads (<200 tok) are skipped by design")
    label = (f"session {session[:8]}" if session else (project or "all"))
    if recard:
        label = "re-card " + label
    jid = next(_JOB_SEQ)
    _JOBS[jid] = {"id": jid, "_db": db_path, "project": project,
                  "session": session, "provider": provider, "model": model,
                  "label": label, "candidates": dry["candidates"],
                  "recard": recard,
                  "status": "queued", "done": 0, "total": dry["candidates"],
                  "generated": 0, "failed": 0, "error": None,
                  "started": None, "finished": None,
                  "_pause": False, "_stop": False, "_cancel": False}
    _schedule(db_path)
    return {"queued": True, "id": jid, "label": label,
            "candidates": dry["candidates"], "started": _JOBS[jid]["status"]
            == "running"}


# ── live carding: an INDEPENDENT worker that cards new threads as you work.
#    It does NOT use the bulk pool (_JOBS) and "stop all" never touches it —
#    it cards directly via run_cards and reports through LIVE_STATE. ──
LAST_PROVIDER = ["openrouter"]
LIVE_STATE = {"active": True, "status": "idle", "last_card": None,
              "last_error": None}
RECARD_DONE = None  # {"at","n"} when a re-card pass finishes — for the UI toast


def _recard_mode(db_path) -> bool:
    """Settings toggle: when on, a backfill run REPLACES existing cards."""
    try:
        c = fdb.connect(db_path)
        row = c.execute(
            "SELECT value FROM meta WHERE key='recard_mode'").fetchone()
        c.close()
        return bool(row and row[0] == "1")
    except Exception:
        return False


def _live_card_loop(db_path):
    import time as _t
    from fable.cards import run_cards
    while True:
        _t.sleep(15)
        try:
            if not LIVE_STATE["active"]:
                LIVE_STATE["status"] = "off"
                continue
            conn = fdb.connect(db_path)
            row = conn.execute(
                "SELECT t.session_id FROM threads t "
                "LEFT JOIN cards c ON c.prompt_id = t.prompt_id "
                "WHERE c.prompt_id IS NULL AND t.est_tokens >= 200 "
                "GROUP BY t.session_id "
                "ORDER BY MAX(t.last_ts) DESC LIMIT 1").fetchone()
            conn.close()
            if not row:
                LIVE_STATE["status"] = "idle"
                continue
            LIVE_STATE["status"] = "carding"
            stats = run_cards(db_path, session=row[0],
                              provider=LAST_PROVIDER[0],
                              abort_after=2, backoff_schedule=(0,))
            errs = stats.get("errors") or []
            rate = any(any(k in str(e).lower() for k in
                       ("429", "rate", "quota", "limit")) for e in errs)
            if stats.get("generated"):
                LIVE_STATE.update(status="active", last_card=_t.time(),
                                  last_error=None)
            elif rate:
                LIVE_STATE.update(status="rate-limited",
                                  last_error="rate-limited / quota")
            else:
                LIVE_STATE["status"] = "idle"
        except Exception as e:
            LIVE_STATE.update(status="error", last_error=str(e)[:120])


def post_cards_job(db_path, body):
    """Per-job control: action = pause | resume | stop | cancel."""
    jid = int(body.get("id", 0))
    action = body.get("action")
    j = _JOBS.get(jid)
    if not j:
        return {"ok": False, "reason": "no such job"}
    if action == "pause":
        j["_pause"] = True
        if j["status"] == "queued":
            j["status"] = "paused"
    elif action == "resume":
        if j["status"] in ("paused", "stopped", "error"):
            j.update(_pause=False, _stop=False, _cancel=False,
                     status="queued", error=None, started=None,
                     finished=None)
            _schedule(db_path)
    elif action == "stop":
        j["_stop"] = True
        if j["status"] != "running":
            j["status"] = "stopped"
    elif action == "cancel":
        j["_cancel"] = True
        if j["status"] != "running":
            _JOBS.pop(jid, None)
    else:
        raise ValueError("action must be pause|resume|stop|cancel")
    return {"ok": True}


def post_cards_stop(db_path, body):
    """Global stop — cancel every BULK job + clear the queue. Does NOT touch
    live carding, which is an independent worker."""
    for j in list(_JOBS.values()):
        j["_cancel"] = True
        if j["status"] != "running":
            _JOBS.pop(j["id"], None)
    return {"stopped": True}


def post_cards_pause(db_path, body):
    """Global pause — pause every job (running ones free their slots)."""
    for j in _JOBS.values():
        j["_pause"] = True
        if j["status"] == "queued":
            j["status"] = "paused"
    return {"paused": True}


def post_cards_resume(db_path, body):
    for j in _JOBS.values():
        if j["status"] in ("paused", "stopped", "error"):
            j.update(_pause=False, _stop=False, _cancel=False,
                     status="queued", error=None, started=None,
                     finished=None)
    _schedule(db_path)
    return {"resumed": True}


def post_cards_dequeue(db_path, body):       # back-compat alias → cancel by id
    return post_cards_job(db_path, {"id": body.get("id") or body.get("index"),
                                    "action": "cancel"})


def api_backfill_progress(db_path, params):
    jobs = [_job_pub(j) for j in sorted(_JOBS.values(), key=lambda x: x["id"])]
    rc = sum(1 for j in jobs if j["status"] == "running")
    qc = sum(1 for j in jobs if j["status"] in ("queued", "paused"))
    return {"jobs": jobs, "max_parallel": MAX_PARALLEL,
            "running": rc > 0, "running_count": rc, "queued_count": qc,
            "done": sum(j.get("done") or 0 for j in jobs
                        if j["status"] == "running"),
            "total": sum(j.get("total") or 0 for j in jobs
                         if j["status"] == "running"),
            "generated": sum(j.get("generated") or 0 for j in jobs),
            "failed": sum(j.get("failed") or 0 for j in jobs),
            "live": dict(LIVE_STATE),
            "recard_done": RECARD_DONE,
            "queue": [j for j in jobs
                      if j["status"] in ("queued", "paused")]}


# repoint the routes to the parallel-pool implementations (the dicts were
# built earlier with the old single-worker functions)
POST_ROUTES["/api/cards/run"] = post_cards_run
POST_ROUTES["/api/cards/stop"] = post_cards_stop
POST_ROUTES["/api/cards/pause"] = post_cards_pause
POST_ROUTES["/api/cards/resume"] = post_cards_resume
POST_ROUTES["/api/cards/dequeue"] = post_cards_dequeue
POST_ROUTES["/api/cards/job"] = post_cards_job
POST_ROUTES["/api/tags/promote"] = post_tags_promote
POST_ROUTES["/api/tags/blacklist"] = post_tags_blacklist
ROUTES["/api/backfill"] = api_backfill_progress


def _heal_stale(db_path, exc) -> bool:
    """A live transcript that grew since indexing is the normal state of
    the CURRENT session — re-index just that file and let the caller
    retry, instead of surfacing an error the user must fix by hand."""
    path = getattr(exc, "path", None)
    if not path or not os.path.exists(path):
        return False
    conn = fdb.connect(db_path)
    try:
        row = conn.execute(
            "SELECT immutable FROM files WHERE path = ?", (path,)).fetchone()
    finally:
        conn.close()
    if row and row[0]:
        return False  # immutable vault files should never drift — real error
    try:
        from fable.extract import fts_extract_fn
        from fable.indexer import index_vault
        index_vault(db_path, [], live_file=path, extract_fn=fts_extract_fn)
        return True
    except Exception:
        return False


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
        if parsed.path == "/mcp":
            self._send(405, b'{"jsonrpc":"2.0","id":null,"error":'
                       b'{"code":-32600,"message":"MCP transport is POST-only"}}',
                       "application/json")
            return
        fn = ROUTES.get(parsed.path)
        if fn is None:
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        from fable.recall import StaleIndexError
        try:
            try:
                payload = fn(self.db_path, parse_qs(parsed.query))
            except StaleIndexError as e:
                # live transcripts grow constantly — re-index and retry once
                if not _heal_stale(self.db_path, e):
                    raise
                payload = fn(self.db_path, parse_qs(parsed.query))
            self._send(200, json.dumps(payload).encode(),
                       "application/json")
        except (KeyError, ValueError, FileNotFoundError, RuntimeError) as e:
            self._send(400, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(),
                       "application/json")

    def _handle_mcp(self):
        """Stateless MCP Streamable-HTTP transport — same tools as `fable mcp`
        (stdio), but hosted in the dashboard so one launch brings up every
        service. Delegates each JSON-RPC message to mcp.handle()."""
        from fable import mcp as _mcp
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send(400, b'{"jsonrpc":"2.0","id":null,"error":'
                       b'{"code":-32700,"message":"parse error"}}',
                       "application/json")
            return
        msgs = payload if isinstance(payload, list) else [payload]
        replies = [r for r in (_mcp.handle(self.db_path, m) for m in msgs)
                   if r is not None]
        if not replies:                       # all notifications → 202, no body
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(replies[0] if len(replies) == 1
                          else replies).encode()
        self._send(200, body, "application/json")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/mcp":
            return self._handle_mcp()
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
        except ValueError as e:
            # expected user-facing validation guard (nothing to do / session
            # active / bad input) — clean 400, no traceback noise in errors.log
            self._send(400, json.dumps({"error": str(e)}).encode(),
                       "application/json")
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            try:
                with open(os.path.join(os.path.dirname(
                        os.path.abspath(self.db_path)), "errors.log"),
                        "a") as fh:
                    fh.write(f"\n=== POST {parsed.path} ===\n{tb}\n")
            except OSError:
                pass
            self._send(400, json.dumps(
                {"error": f"{type(e).__name__}: {e}"}).encode(),
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
    threading.Thread(target=_live_card_loop, args=(db_path,),
                     daemon=True).start()
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
