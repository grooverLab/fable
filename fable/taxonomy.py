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
# only these families enforce their enum (unknown values are dropped); every
# other family lets gpt-oss coin new values, which surface in triage.
# domain is sacred — it stays the defined set.
STRICT_FAMILIES = ("domain",)
SEMANTIC_FAMILIES = ("topic", "technology", "entity", "pattern", "intent",
                     "context")
ALL_FAMILIES = CONTROLLED_FAMILIES + SEMANTIC_FAMILIES

# generic, safe-to-publish fallback (your real vocabulary lives in
# ~/.fable/taxonomy.json and overrides this entirely when present)
_DEFAULT = {
    "controlled": {
        "domain": ["software_engineering", "data_science", "devops",
                   "research", "writing", "analysis", "other"],
        "activity": ["plan", "implement", "debug", "review", "refactor",
                     "test", "research", "document", "analyze"],
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
        parts.append("Controlled families — for DOMAIN the value MUST be one "
                     "from its list below; for the other controlled families "
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


def validate_tags(raw) -> list:
    """Normalize the LLM's tags to a deduped list of (family, value).

    Drops anything invalid: unknown family, bad snake_case, or — for controlled
    families with a defined enum — a value not in the enum. Never raises.
    """
    tax = load_taxonomy()
    ctrl = tax.get("controlled") or {}
    bl = {tuple(x) for x in (tax.get("blacklist") or [])}
    out: list = []
    seen = set()
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
            allowed = ctrl.get(fam) or []
            if allowed and val not in allowed:
                continue
        try:
            conf = max(0.0, min(1.0, float(t.get("confidence", 0.7))))
        except (TypeError, ValueError):
            conf = 0.7
        key = (fam, val)
        if key not in seen:
            seen.add(key)
            out.append((fam, val, round(conf, 3)))
    return out


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
    """Add an invented value to the known semantic vocabulary (un-blacklist it)."""
    data = _read_raw()
    lst = data.setdefault("semantic", {}).setdefault(family, [])
    if value not in lst:
        lst.append(value)
    data["blacklist"] = [x for x in (data.get("blacklist") or [])
                         if list(x) != [family, value]]
    _save(data)


def blacklist_value(family: str, value: str) -> None:
    """Suppress an invented value — dropped from all future tagging."""
    data = _read_raw()
    bl = data.setdefault("blacklist", [])
    if [family, value] not in [list(x) for x in bl]:
        bl.append([family, value])
    _save(data)
