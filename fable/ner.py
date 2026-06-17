"""Entity dictionary (NER-lite) — the scout's learned entity recognizer.

Built from what the carder ALREADY labels: thread_tags in the specific families
(topic/technology/entity/pattern), card files, and salient_entities. No model,
no training, no dependency — it "learns" by rebuilding on new cards (the nightly
delta). A trained neural NER is the later upgrade IF the dictionary misses too
much; with vectors handling the actual matching, this only needs to recognize
which tokens in a prompt are real, project-relevant entities.
"""
import datetime
import json
import re

from fable import db as fdb

# tag families that name real entities — NOT the generic action families
# (activity/outcome/decision), whose values fire on everything.
_ENTITY_FAMILIES = ("topic", "technology", "entity", "pattern")
# generic words that slip into tags but aren't useful recognition keys
_STOP = {"python", "rust", "code", "file", "data", "test", "api", "error",
         "function", "server", "build", "app", "json", "sql", "git"}


def build_dictionary(db_path: str, on_progress=None) -> dict:
    """(Re)build the whole dictionary from every card's labels. Idempotent;
    aggregates entity -> (kind, freq, projects). Cheap enough to run nightly."""
    conn = fdb.connect(db_path)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    counts = {}

    def bump(ent, kind, project):
        ent = (ent or "").strip().lower()
        if not (3 <= len(ent) <= 60) or ent in _STOP:
            return
        d = counts.setdefault(ent, {"kind": kind, "freq": 0, "projects": set()})
        d["freq"] += 1
        if project:
            d["projects"].add(project)

    try:
        proj_of = {pid: proj for pid, proj in conn.execute(
            "SELECT t.prompt_id, s.project FROM threads t LEFT JOIN sessions s"
            " ON s.session_id = t.session_id")}
        # 1) tag values in the specific families
        ph = ",".join("?" * len(_ENTITY_FAMILIES))
        for pid, fam, val in conn.execute(
                "SELECT prompt_id, family, value FROM thread_tags "
                "WHERE family IN (%s)" % ph, _ENTITY_FAMILIES):
            bump(val, fam, proj_of.get(pid))
        # 2) salient_entities + files from the cards
        for pid, sal, files in conn.execute(
                "SELECT prompt_id, salient_entities, files FROM cards"):
            proj = proj_of.get(pid)
            try:
                for e in (json.loads(sal) if sal else []):
                    bump(e, "entity", proj)
            except Exception:
                pass
            try:
                for f in (json.loads(files) if files else []):
                    bump(str(f).split("/")[-1], "file", proj)
            except Exception:
                pass
        conn.execute("DELETE FROM ner_entities")
        for ent, d in counts.items():
            conn.execute(
                "INSERT INTO ner_entities(entity, kind, freq, projects,"
                " last_seen) VALUES(?,?,?,?,?)",
                (ent, d["kind"], d["freq"],
                 json.dumps(sorted(d["projects"])), now))
        conn.commit()
        if on_progress:
            on_progress(f"dictionary rebuilt: {len(counts)} entities")
        return {"entities": len(counts)}
    finally:
        conn.close()


_TOK = re.compile(r"[A-Za-z][A-Za-z0-9_./-]{2,}")


def load_dictionary(db_path: str):
    """{entity: (kind, projects_list)} — the recognizer's lookup table."""
    conn = fdb.connect(db_path)
    try:
        out = {}
        for ent, kind, projs in conn.execute(
                "SELECT entity, kind, projects FROM ner_entities"):
            try:
                pl = json.loads(projs) if projs else []
            except Exception:
                pl = []
            out[ent] = (kind, pl)
        return out
    finally:
        conn.close()


def recognize(db_path: str, text: str, project: str = None, known=None):
    """Entities from `text` that the dictionary knows. Pass a preloaded `known`
    (from load_dictionary) to avoid a DB hit on the hot path. Project-relevant
    entities first."""
    known = known if known is not None else load_dictionary(db_path)
    if not known:
        return []
    toks = {m.group(0).lower() for m in _TOK.finditer(text or "")}
    hits = [t for t in toks if t in known]
    if project:
        pl = project.lower()
        hits.sort(key=lambda t: 0 if any(
            pl in p.lower() for p in known[t][1]) else 1)
    return hits


def cmd_ner(args) -> int:
    """CLI: `fable ner build` (rebuild dictionary) / `fable ner show`."""
    if args.ner_cmd == "build":
        stats = build_dictionary(args.db, on_progress=print)
        print(json.dumps(stats))
        return 0
    if args.ner_cmd == "show":
        conn = fdb.connect(args.db)
        rows = conn.execute("SELECT entity, kind, freq FROM ner_entities "
                            "ORDER BY freq DESC LIMIT 40").fetchall()
        conn.close()
        for e, k, f in rows:
            print(f"{f:5} {k:11} {e}")
        return 0
    print("usage: fable ner build|show")
    return 1
