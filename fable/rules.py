"""Auto-Rules — mine the standing directives a user repeats to their agents.

The carder already extracts, per thread:
  - `directives`  — rules/preferences the USER stated (how they want work done)
  - `lessons` / `gotchas` — what the AGENT learned (rule-like, source-tagged)

This module clusters those by recurrence into candidate **rules**: a directive
stated >=3x across >=2 sessions becomes a candidate the user approves with one
tap in the dashboard triage. No NER, no lexical patterns — the model already
hands us clean imperatives; we just normalize lightly and count.

Phase 1 = detect + approve (this file + the triage UI). Enforcement (injecting
approved rules via hooks) is Phase 2 and lives elsewhere.

Design notes:
- The `rules` table is BOTH the computed clusters AND the user's triage decisions.
  Re-clustering refreshes counts/evidence but PRESERVES status — an approved or
  rejected rule never silently reverts to `candidate`.
- Clustering is exact-canonical (deterministic, zero-dep). Paraphrase merge via
  embeddings is a deferred optional layer (the model's output is already clean).
"""
import json
import re
import datetime
from collections import defaultdict, Counter

from fable import db as fdb

# card fields that carry rule-like signal → singular source tag
_SOURCES = {"directives": "directive", "lessons": "lesson", "gotchas": "gotcha"}

# leading imperative filler peeled before clustering so "always read files" and
# "read files" land in one cluster. Conservative on purpose.
_FILLER = re.compile(
    r"^(please|always|never|make sure to|be sure to|ensure you|ensure that you|"
    r"you must|you should|you have to|do not forget to|remember to|"
    r"i want you to|i'd like you to|i would like you to)\s+", re.I)

_RULES_DDL = """CREATE TABLE IF NOT EXISTS rules(
    id INTEGER PRIMARY KEY,
    canonical_text TEXT,
    display_text TEXT,
    source TEXT DEFAULT 'directive',
    scope TEXT,
    project TEXT,
    status TEXT DEFAULT 'candidate',
    occurrence_count INTEGER,
    session_count INTEGER,
    project_count INTEGER,
    evidence TEXT,
    first_seen TEXT,
    last_seen TEXT,
    updated_at TEXT,
    UNIQUE(canonical_text, source))"""

# user-set statuses that re-clustering must never overwrite
_LOCKED = ("active", "muted", "rejected", "candidate")


def _normalize(text):
    """Light canonical form for clustering near-identical directives. The model
    already emits clean imperatives, so this stays conservative: lowercase, drop
    surrounding punctuation, collapse whitespace, peel stacked leading filler."""
    t = (text or "").strip().lower()
    t = re.sub(r"[\"'`.!?,;:()\[\]]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    prev = None
    while prev != t:
        prev = t
        t = _FILLER.sub("", t).strip()
    return t


def cluster(items):
    """items: iterable of (text, source, prompt_id, session_id, project, ts).
    Returns {(source, canonical): aggregate}. Pure / testable, no DB."""
    groups = defaultdict(lambda: {
        "texts": [], "pids": set(), "sessions": set(), "projects": set(),
        "first": None, "last": None, "n": 0})
    for text, source, pid, sid, proj, ts in items:
        canon = _normalize(text)
        if len(canon) < 4:
            continue
        g = groups[(source, canon)]
        g["texts"].append(text)
        g["n"] += 1
        if pid:
            g["pids"].add(pid)
        if sid:
            g["sessions"].add(sid)
        if proj:
            g["projects"].add(proj)
        if ts:
            g["first"] = ts if g["first"] is None else min(g["first"], ts)
            g["last"] = ts if g["last"] is None else max(g["last"], ts)
    return groups


def _embed_cached(conn, canons):
    """Embed each canonical text once, cached in `rule_vecs` (recompute only new
    ones). {canon: vec} for those the backend could embed."""
    import struct
    from fable import embeddings as emb
    conn.execute("CREATE TABLE IF NOT EXISTS rule_vecs("
                 "canon TEXT PRIMARY KEY, vec BLOB, dim INT)")
    uniq = list(dict.fromkeys(canons))
    have = {}
    for c in uniq:
        row = conn.execute("SELECT vec, dim FROM rule_vecs WHERE canon=?",
                           (c,)).fetchone()
        if row:
            have[c] = list(struct.unpack(f"{row[1]}f", row[0]))
    missing = [c for c in uniq if c not in have]
    for i in range(0, len(missing), 64):       # embed in chunks; tolerate failures
        chunk = missing[i:i + 64]
        try:
            vecs = emb.embed_texts(chunk)
        except Exception:
            continue
        for c, v in zip(chunk, vecs):
            have[c] = v
            conn.execute("INSERT OR REPLACE INTO rule_vecs(canon, vec, dim) "
                         "VALUES(?,?,?)", (c, struct.pack(f"{len(v)}f", *v),
                                           len(v)))
    conn.commit()
    return have


def _merge_paraphrases(groups, conn, sim=0.80):
    """Merge exact-canonical DIRECTIVE clusters that are semantic PARAPHRASES
    (cosine >= sim) into one — so 'read the file before editing', 'read each file
    first' and 'always read before you edit' count as ONE rule and can cross the
    threshold. Greedy single-pass agglomeration (biggest first), embeddings
    cached. Lessons/gotchas are left exact-canonical. No backend → unchanged."""
    import math
    try:
        from fable import embeddings as emb
        if not emb.backend():
            return groups
    except Exception:
        return groups
    dirs = [(canon, g) for (src, canon), g in groups.items()
            if src == "directive"]
    if len(dirs) < 2:
        return groups
    vecs = _embed_cached(conn, [c for c, _ in dirs])
    norm = {c: (math.sqrt(sum(x * x for x in v)) or 1.0)
            for c, v in vecs.items()}
    order = sorted(range(len(dirs)), key=lambda i: -dirs[i][1]["n"])
    reps = []            # [(rep_canon, rep_vec, [member indices])]
    for i in order:
        c = dirs[i][0]
        v = vecs.get(c)
        best, bj = sim, -1
        if v is not None:
            for j, (rc, rv, _) in enumerate(reps):
                if rv is None:
                    continue
                cos = sum(a * b for a, b in zip(v, rv)) / (norm[c] * norm[rc])
                if cos >= best:
                    best, bj = cos, j
        if bj >= 0:
            reps[bj][2].append(i)
        else:
            reps.append((c, v, [i]))
    merged = {k: v for k, v in groups.items() if k[0] != "directive"}
    for rc, _rv, idxs in reps:
        mg = {"texts": [], "pids": set(), "sessions": set(), "projects": set(),
              "first": None, "last": None, "n": 0}
        for i in idxs:
            g = dirs[i][1]
            mg["texts"] += g["texts"]
            mg["n"] += g["n"]
            mg["pids"] |= g["pids"]
            mg["sessions"] |= g["sessions"]
            mg["projects"] |= g["projects"]
            if g["first"]:
                mg["first"] = (g["first"] if mg["first"] is None
                               else min(mg["first"], g["first"]))
            if g["last"]:
                mg["last"] = (g["last"] if mg["last"] is None
                              else max(mg["last"], g["last"]))
        # representative canon = the most-occurring member
        rep = dirs[max(idxs, key=lambda i: dirs[i][1]["n"])][0]
        merged[("directive", rep)] = mg
    return merged


def recluster(db_path, min_occ=3, min_sessions=2):
    """Read directives/lessons/gotchas from every card, cluster, upsert `rules`.
    Refreshes counts/evidence; preserves user triage status. Returns a summary."""
    conn = fdb.connect(db_path)
    try:
        conn.execute(_RULES_DDL)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(cards)")]
        present = [f for f in _SOURCES if f in cols]
        if not present:
            return {"clusters": 0, "candidates": 0, "scanned_cards": 0}

        sess_of = dict(conn.execute("SELECT prompt_id, session_id FROM threads"))
        cwd = dict(conn.execute("SELECT session_id, project FROM sessions"))
        wp = {}
        try:
            from fable import tasktime
            wp = tasktime.work_projects(db_path, conn)
        except Exception:
            pass

        items, scanned = [], 0
        sel = "prompt_id, created_at, " + ", ".join(present)
        for row in conn.execute(f"SELECT {sel} FROM cards"):
            pid, ts = row[0], row[1]
            sid = sess_of.get(pid)
            proj = wp.get(sid) or cwd.get(sid)
            has = False
            for i, field in enumerate(present):
                blob = row[2 + i]
                if not blob or blob in ("[]", ""):
                    continue
                try:
                    lst = json.loads(blob) or []
                except (ValueError, TypeError):
                    continue
                for text in lst:
                    items.append((str(text), _SOURCES[field], pid, sid, proj, ts))
                    has = True
            scanned += 1 if has else 0

        groups = cluster(items)
        groups = _merge_paraphrases(groups, conn)   # merge directive paraphrases
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        candidates = 0
        written = 0
        for (source, canon), g in groups.items():
            occ, nsess, nproj = g["n"], len(g["sessions"]), len(g["projects"])
            is_cand = occ >= min_occ and nsess >= min_sessions
            if is_cand:
                candidates += 1
            scope = "global" if nproj >= 2 else "project"
            project = None if scope == "global" else next(iter(g["projects"]), None)
            display = max(g["texts"], key=len) if g["texts"] else canon
            evidence = json.dumps(sorted(g["pids"])[:20])
            prev = conn.execute(
                "SELECT status FROM rules WHERE canonical_text=? AND source=?",
                (canon, source)).fetchone()
            if prev:
                # keep user's status; only auto-promote subthreshold→candidate
                status = prev[0]
                if status == "subthreshold" and is_cand:
                    status = "candidate"
                conn.execute(
                    "UPDATE rules SET display_text=?, status=?, scope=?, project=?,"
                    " occurrence_count=?, session_count=?, project_count=?,"
                    " evidence=?, last_seen=?, updated_at=? "
                    "WHERE canonical_text=? AND source=?",
                    (display, status, scope, project, occ, nsess, nproj,
                     evidence, g["last"], now, canon, source))
            else:
                conn.execute(
                    "INSERT INTO rules(canonical_text, display_text, source, scope,"
                    " project, status, occurrence_count, session_count,"
                    " project_count, evidence, first_seen, last_seen, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (canon, display, source, scope, project,
                     "candidate" if is_cand else "subthreshold",
                     occ, nsess, nproj, evidence, g["first"], g["last"], now))
            written += 1
            if written % 50 == 0:
                conn.commit()      # release the write lock every ~50 clusters
        conn.commit()
        return {"clusters": len(groups), "candidates": candidates,
                "scanned_cards": scanned}
    finally:
        conn.close()


_KEYS = ["id", "canonical_text", "display_text", "source", "scope", "project",
         "status", "occurrence_count", "session_count", "project_count",
         "evidence", "first_seen", "last_seen"]


def read_rules(db_path, include_subthreshold=False):
    """Return clustered rules (candidates/active first), with status summary."""
    conn = fdb.connect(db_path)
    try:
        conn.execute(_RULES_DDL)
        q = ("SELECT id, canonical_text, display_text, source, scope, project,"
             " status, occurrence_count, session_count, project_count,"
             " evidence, first_seen, last_seen FROM rules")
        if not include_subthreshold:
            q += " WHERE status != 'subthreshold'"
        q += " ORDER BY occurrence_count DESC, session_count DESC"
        rows = conn.execute(q).fetchall()
        by_status = dict(Counter(
            r[0] for r in conn.execute("SELECT status FROM rules")))
    finally:
        conn.close()
    rules = []
    for r in rows:
        d = dict(zip(_KEYS, r))
        d["evidence"] = json.loads(d["evidence"] or "[]")
        rules.append(d)
    return {"rules": rules, "by_status": by_status,
            "sources": dict(Counter(d["source"] for d in rules))}


def render_rules(db_path, project=None):
    """Phase 2 — ENFORCEMENT. The ACTIVE (user-approved) standing rules, as an
    injectable block for the SessionStart hook: the directives the user has
    repeated and approved, fed back so the agent obeys them from turn one
    (instead of the user re-stating 'read the files' every session). Returns ''
    if nothing is approved yet."""
    try:
        active = [r for r in read_rules(db_path).get("rules", [])
                  if r.get("status") == "active"]
    except Exception:
        return ""
    if project:
        active = [r for r in active if not r.get("project")
                  or project.lower() in (r.get("project") or "").lower()]
    if not active:
        return ""
    active.sort(key=lambda r: -(r.get("occurrence_count") or 0))
    lines = ["<fable-rules>",
             "STANDING RULES you set across past sessions and approved — follow "
             "them as if stated this session; they are not optional:"]
    for r in active:
        txt = (r.get("display_text") or r.get("canonical_text") or "").strip()
        if txt:
            n = r.get("occurrence_count") or 0
            lines.append(f"- {txt}" + (f"  (you've said this {n}×)" if n else ""))
    lines.append("</fable-rules>")
    return "\n".join(lines)


def triage(db_path, rule_id, action, text=None, scope=None):
    """Apply a triage action to one rule. Persists across re-clustering."""
    status_for = {"approve": "active", "reject": "rejected", "mute": "muted",
                  "candidate": "candidate"}
    conn = fdb.connect(db_path)
    try:
        conn.execute(_RULES_DDL)
        if action in status_for:
            conn.execute("UPDATE rules SET status=? WHERE id=?",
                         (status_for[action], rule_id))
        elif action == "edit":
            sets, args = [], []
            if text is not None:
                sets.append("display_text=?")
                args.append(text)
            if scope in ("global", "project"):
                sets.append("scope=?")
                args.append(scope)
            if sets:
                args.append(rule_id)
                conn.execute(f"UPDATE rules SET {', '.join(sets)} WHERE id=?",
                             args)
        else:
            raise ValueError(f"unknown action: {action}")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()
