"""Private tagging taxonomy for the carder.

The controlled dimensions + open semantic families that the carder uses to tag
a thread. The *vocabulary* is private: it loads from ``~/.fable/taxonomy.json``
(outside any git tree — never published). If that file is absent, a small
generic fallback (the in-code ``_DEFAULT`` below) is used, so the public build
still tags — just without your curated vocabulary.

stdlib-only (json, no PyYAML) to keep the package dependency-free and the
Glama/Docker image tiny.
"""
from __future__ import annotations

import functools
import json
import os
import re

# value grammar shared with memory-mcp's tagger: lowercase snake_case, <=40 chars
_VALID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

# controlled families take EXACTLY ONE value (drawn from the enum);
# semantic families take ZERO OR MORE (seeded but open-vocabulary)
CONTROLLED_FAMILIES = ("domain", "activity", "event", "artifact", "outcome",
                       "decision")
# these families snap to their enum: a coined value folds via the synonyms map,
# or — mapping to nothing — is recorded in tag_proposals for triage (never
# silently dropped, so the taxonomy keeps evolving). The other controlled
# families (event, artifact) + all semantic families stay open-vocabulary.
# These four are domain-agnostic by design (see memory-mcp dimensions.py) so
# fable works as a general memory layer, not just for software.
STRICT_FAMILIES = ("domain", "outcome", "activity", "decision")
SEMANTIC_FAMILIES = ("topic", "technology", "entity", "pattern", "intent",
                     "context")
ALL_FAMILIES = CONTROLLED_FAMILIES + SEMANTIC_FAMILIES

# generic, safe-to-publish fallback (your real vocabulary lives in
# ~/.fable/taxonomy.json and overrides this entirely when present)
_DEFAULT = {
    "controlled": {
        "domain": ["software_engineering", "data_science", "devops",
                   "research", "writing", "analysis", "other"],
        "activity": ["plan", "research", "analyze", "generate", "modify",
                     "review", "debug", "execute", "compare", "organize"],
        "event": ["decision", "task", "error", "insight", "result"],
        "artifact": ["code", "function", "file", "module", "document",
                     "config", "test", "script"],
        "outcome": ["success", "partial_success", "failure", "blocked",
                    "abandoned"],
        "decision": ["architecture", "tool_selection", "implementation",
                     "refactor", "dependency", "config_change", "other"],
    },
    "semantic": {
        "topic": ["auth", "database", "api", "ui", "testing", "deployment",
                  "performance", "security", "logging", "search"],
        "technology": ["python", "javascript", "typescript", "react",
                       "docker", "postgres", "sqlite", "git"],
        "pattern": ["refactor", "retry", "caching", "pipeline", "validation",
                    "migration"],
        "intent": ["debug", "implement", "optimize", "explore", "compare",
                   "document"],
        "context": ["project", "experiment", "investigation",
                    "infrastructure"],
        "entity": [],
    },
    # generic software→general snaps so the public build (no private taxonomy)
    # still folds the common specifics onto the abstract activity verbs
    "synonyms": {
        "activity": {
            "implement": "generate", "build": "generate", "create": "generate",
            "write": "generate", "design": "generate", "develop": "generate",
            "refactor": "modify", "update": "modify", "edit": "modify",
            "test": "review", "verify": "review", "validate": "review",
            "deploy": "execute", "run": "execute", "commit": "execute",
            "fix": "debug", "investigate": "debug", "troubleshoot": "debug",
            "decide": "plan", "schedule": "plan",
        },
    },
    "blacklist": [],
}


def _user_path() -> str:
    return os.path.expanduser(os.environ.get(
        "FABLE_TAXONOMY", "~/.fable/taxonomy.json"))


@functools.lru_cache(maxsize=1)
def load_taxonomy() -> dict:
    """Return {'controlled': {...}, 'semantic': {...}}; user file or fallback."""
    path = _user_path()
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f) or {}
            return {"controlled": dict(data.get("controlled") or {}),
                    "semantic": dict(data.get("semantic") or {}),
                    "synonyms": dict(data.get("synonyms") or {}),
                    "blacklist": list(data.get("blacklist") or [])}
        except Exception:
            pass  # malformed user file → fall back, never break carding
    return _DEFAULT


def prompt_block() -> str:
    """The taxonomy section injected into the carder PROMPT (memory-mcp style).

    Returns '' only if the taxonomy is somehow empty (then the carder free-tags).
    """
    tax = load_taxonomy()
    ctrl = tax.get("controlled") or {}
    sem = tax.get("semantic") or {}
    clines = []
    for fam in CONTROLLED_FAMILIES:
        vals = ctrl.get(fam) or []
        if vals:
            clines.append(f"  {fam}: [{', '.join(vals)}]")
    slines = []
    for fam in SEMANTIC_FAMILIES:
        vals = sem.get(fam) or []
        if vals:
            slines.append(f"  {fam}: {', '.join(vals)}")
    if not clines and not slines:
        return ""
    parts = ["TAGGING TAXONOMY — populate the \"tags\" field from this. For "
             "EVERY family include ALL values that genuinely apply — there is "
             "no limit on how many; let the content decide (a thread may span "
             "several domains, activities, decisions, artifacts). Give each "
             "value a confidence 0.0-1.0. Never force a single value."]
    if clines:
        parts.append("Controlled families — for domain, outcome, activity and "
                     "decision the value MUST be one from its list below "
                     "(these are abstract on purpose: use the closest general "
                     "verb, e.g. 'generate' for implement/design/write, "
                     "'review' for test/verify — the specifics belong in the "
                     "semantic families). For the other controlled families "
                     "PREFER the listed values but you may coin a new "
                     "snake_case value if none fit (it goes to review):")
        parts.append("\n".join(clines))
    if slines:
        parts.append("Semantic families — value must be lowercase snake_case "
                     "[a-z][a-z0-9_]{0,39}; PREFER the known vocabulary, only "
                     "invent a new value if nothing fits; the family name "
                     "gives context (use \"retry\", not \"retry_pattern\"):")
        parts.append("\n".join(slines))
    return "\n".join(parts) + "\n"


_STEM = re.compile(r"(ing|ed|s)$")


def canonicalize(family: str, value: str):
    """Snap a STRICT-family value onto its enum. Returns:
      - the canonical enum value (use it),
      - "" if it's a known misfile to drop (synonyms maps it to "__drop__"),
      - None if it maps to nothing → caller should PROPOSE it (never lose it).
    Non-strict / semantic families pass through unchanged (open vocabulary)."""
    if family not in STRICT_FAMILIES:
        return value
    tax = load_taxonomy()
    enum = set((tax.get("controlled") or {}).get(family) or [])
    syn = (tax.get("synonyms") or {}).get(family) or {}
    if value in enum:
        return value
    if value in syn:
        m = syn[value]
        return "" if m == "__drop__" else m
    stem = _STEM.sub("", value)
    if stem != value:
        if stem in enum:
            return stem
        if stem in syn:
            m = syn[stem]
            return "" if m == "__drop__" else m
    return None


def validate_tags(raw):
    """Normalize the LLM's tags. Returns (tags, proposals):
      tags      = deduped [(family, value, confidence)]; STRICT controlled
                  families are snapped to their enum (synonyms fold variants).
      proposals = [(family, value)] the model coined for a STRICT family that
                  maps to nothing — held for triage, never silently dropped.
    Never raises.
    """
    bl = {tuple(x) for x in (load_taxonomy().get("blacklist") or [])}
    out: list = []
    proposals: list = []
    seen = set()
    pseen = set()
    for t in raw or []:
        if not isinstance(t, dict):
            continue
        fam = str(t.get("family", "")).strip().lower()
        val = str(t.get("value", "")).strip().lower()
        if fam not in ALL_FAMILIES or not val or not _VALID.match(val):
            continue
        if (fam, val) in bl:
            continue
        if fam in STRICT_FAMILIES:
            c = canonicalize(fam, val)
            if c == "":                      # known misfile → drop
                continue
            if c is None:                    # unknown → propose, don't tag
                if (fam, val) not in pseen:
                    pseen.add((fam, val))
                    proposals.append((fam, val))
                continue
            val = c
        try:
            conf = max(0.0, min(1.0, float(t.get("confidence", 0.7))))
        except (TypeError, ValueError):
            conf = 0.7
        key = (fam, val)
        if key not in seen:
            seen.add(key)
            out.append((fam, val, round(conf, 3)))
    return out, proposals


def _read_raw() -> dict:
    """The user's taxonomy file as a mutable dict (for promote/blacklist)."""
    import copy
    path = _user_path()
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f) or {}
        except Exception:
            pass
    return copy.deepcopy(_DEFAULT)


def _save(data: dict) -> None:
    path = _user_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    load_taxonomy.cache_clear()


def promote(family: str, value: str) -> None:
    """Add an invented value to the known vocabulary (un-blacklist it). A
    controlled family's value joins that family's ENUM; a semantic family's
    value joins its seed list."""
    data = _read_raw()
    grp = "controlled" if family in CONTROLLED_FAMILIES else "semantic"
    lst = data.setdefault(grp, {}).setdefault(family, [])
    if value not in lst:
        lst.append(value)
    data["blacklist"] = [x for x in (data.get("blacklist") or [])
                         if list(x) != [family, value]]
    _save(data)


def add_synonym(family: str, value: str, target: str) -> None:
    """Snap a coined value onto an existing enum value (records value→target in
    the synonyms map). target='__drop__' marks it a misfile to discard."""
    data = _read_raw()
    data.setdefault("synonyms", {}).setdefault(family, {})[value] = target
    _save(data)


def drain_proposal(db_path: str, family: str, value: str,
                   canonical=None) -> int:
    """Resolve a triage proposal in the DB (model-free). With `canonical` set
    (promote → the value itself; snap → the target enum value) its
    tag_proposals rows are written into thread_tags as `canonical`; with
    canonical=None (blacklist) they are simply removed. Returns rows moved."""
    import datetime
    from fable import db as _fdb
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = _fdb.connect(db_path)
    try:
        moved = 0
        if canonical:
            for pid, ts in conn.execute(
                    "SELECT prompt_id, created_at FROM tag_proposals "
                    "WHERE family=? AND value=?", (family, value)).fetchall():
                conn.execute(
                    "INSERT OR REPLACE INTO thread_tags(prompt_id, family,"
                    " value, score, source, model, created_at)"
                    " VALUES(?,?,?,1.0,'triage','triage',?)",
                    (pid, family, canonical, ts or now))
                moved += 1
        conn.execute("DELETE FROM tag_proposals WHERE family=? AND value=?",
                     (family, value))
        conn.commit()
        return moved
    finally:
        conn.close()


def blacklist_value(family: str, value: str) -> None:
    """Suppress an invented value — dropped from all future tagging."""
    data = _read_raw()
    bl = data.setdefault("blacklist", [])
    if [family, value] not in [list(x) for x in bl]:
        bl.append([family, value])
    _save(data)


def recanonicalize(db_path: str) -> dict:
    """One-time fold of existing thread_tags onto the canonical enums. Snapshots
    the raw tags to thread_tags_raw first (so it stays re-runnable after a vocab
    change), then for each STRICT-family row: snap → keep; known misfile → drop;
    unknown → route to tag_proposals. Non-strict families are left untouched."""
    from fable import db as _fdb
    fams = STRICT_FAMILIES
    qs = ",".join("?" * len(fams))
    conn = _fdb.connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS thread_tags_raw "
                     "AS SELECT * FROM thread_tags WHERE 0")
        if not conn.execute("SELECT 1 FROM thread_tags_raw LIMIT 1").fetchone():
            conn.execute("INSERT INTO thread_tags_raw "
                         "SELECT * FROM thread_tags")
        rows = conn.execute(
            "SELECT prompt_id, family, value, score, source, model, created_at"
            f" FROM thread_tags WHERE family IN ({qs})", fams).fetchall()
        conn.execute(f"DELETE FROM thread_tags WHERE family IN ({qs})", fams)
        kept = dropped = proposed = 0
        seen = set()
        for pid, fam, val, score, src, model, ts in rows:
            c = canonicalize(fam, val)
            if c == "":
                dropped += 1
                continue
            if c is None:
                conn.execute(
                    "INSERT OR IGNORE INTO tag_proposals"
                    "(family, value, prompt_id, created_at, status)"
                    " VALUES(?,?,?,?,'proposed')", (fam, val, pid, ts))
                proposed += 1
                continue
            if (pid, fam, c) in seen:
                continue
            seen.add((pid, fam, c))
            conn.execute(
                "INSERT OR REPLACE INTO thread_tags"
                "(prompt_id, family, value, score, source, model, created_at)"
                " VALUES(?,?,?,?,?,?,?)", (pid, fam, c, score, src, model, ts))
            kept += 1
        conn.commit()
        return {"kept": kept, "dropped": dropped, "proposed": proposed}
    finally:
        conn.close()
