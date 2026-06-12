"""Deterministic term extraction — the offline version of arc-memory's
Operative/Target/Concept trichotomy.

The live agent is never asked to tag anything (that failed empirically:
4 wikilinks in 31k records). Instead:
  operatives — closed verb list, stem-matched
  targets    — regex: paths, filenames, snake_case, CamelCase, crate names,
               backtick spans
  concepts   — RAKE-style noun phrases ranked by per-thread TF-IDF, so the
               topic vocabulary emerges from the corpus with no taxonomy
  wikilinks  — bonus signal if any survive in the text
"""
import math
import re
from collections import Counter
from typing import Dict, List

from fable import db as fdb

OPERATIVES = [
    "add", "analyze", "audit", "backfill", "benchmark", "build", "clean",
    "commit", "compact", "configure", "connect", "create", "debug", "decide",
    "delete", "deploy", "design", "document", "draft", "fix", "implement",
    "index", "integrate", "investigate", "merge", "migrate", "optimize",
    "parse", "plan", "profile", "prune", "refactor", "reject", "remove",
    "rename", "resolve", "restore", "retrieve", "revert", "review",
    "scaffold", "search", "simplify", "strip", "test", "tune", "validate",
    "verify", "wire", "write",
]


def _variants(op: str):
    yield op
    yield op + "s"
    yield op + "es"
    yield op + "ed"
    yield op + "ing"
    if op.endswith("e"):
        yield op[:-1] + "ed"
        yield op[:-1] + "ing"
    else:
        yield op + op[-1] + "ed"   # plan -> planned
        yield op + op[-1] + "ing"  # plan -> planning
    if op.endswith("y"):
        yield op[:-1] + "ied"      # verify -> verified
        yield op[:-1] + "ies"


_OP_LOOKUP: Dict[str, str] = {}
for _op in OPERATIVES:
    for _v in _variants(_op):
        _OP_LOOKUP.setdefault(_v, _op)

_WORD = re.compile(r"[a-z]+")
_WIKILINK = re.compile(r"\[\[([^\[\]\n]{1,80})\]\]")
_BACKTICK = re.compile(r"`([^`\n]{2,120})`")
_PATH = re.compile(r"(?<![\w/])/?(?:[\w.@-]+/)+[\w.@-]*\w")
_FILENAME = re.compile(
    r"\b[\w-]+\.(?:rs|py|pine|ts|tsx|js|jsx|mjs|md|ya?ml|toml|json|jsonl|"
    r"sql|sh|css|html|db|csv|txt|lock)\b")
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_CAMEL = re.compile(r"\b[A-Za-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+\b")
_CRATE = re.compile(r"\b(?:sled|arena|qt|vj|fable)-[a-z0-9][a-z0-9-]*\b")

STOPWORDS = frozenset("""
a about above after again all also am an and any are as at be because been
before being below between both but by can cannot could did do does doing
down during each few for from further had has have having he her here hers
him his how i if in into is it its itself just let like looks good me more
most my myself need needs no nor not now of off on once only or other our
ours out over own same she should so some such than that the their theirs
them then there these they this those through to too under until up very
was we were what when where which while who whom why will with would you
your yours yourself thanks please ok okay yes sure right left make makes
made get gets got use used using want wants new old way thing things
""".split())


def match_operatives(text: str) -> Counter:
    ops = Counter()
    for word in _WORD.findall(text.lower()):
        canon = _OP_LOOKUP.get(word)
        if canon:
            ops[canon] += 1
    return ops


def extract_targets(text: str) -> Counter:
    targets = Counter()
    for m in _BACKTICK.finditer(text):
        targets[m.group(1).strip()] += 1
    for pattern in (_PATH, _FILENAME, _SNAKE, _CAMEL, _CRATE):
        for m in pattern.finditer(text):
            tok = m.group(0)
            if pattern is _PATH and "." not in tok.rsplit("/", 1)[-1]:
                continue  # directories are too noisy; require an extension
            targets[tok] += 1
    return targets


def extract_wikilinks(text: str) -> List[str]:
    return _WIKILINK.findall(text)


_PHRASE_TOKEN = re.compile(r"[a-z][a-z-]+")
_FRAGMENT_SPLIT = re.compile(r"[.,;:!?()\[\]{}\n\r\t|<>\"']+")


def candidate_phrases(text: str) -> Counter:
    """RAKE-style: runs of non-stopword tokens between stopwords/punctuation."""
    phrases = Counter()
    for fragment in _FRAGMENT_SPLIT.split(text.lower()):
        run: List[str] = []
        for tok in fragment.split():
            word = _PHRASE_TOKEN.fullmatch(tok)
            if word and word.group(0) not in STOPWORDS:
                run.append(word.group(0))
            else:
                _flush(run, phrases)
                run = []
        _flush(run, phrases)
    return phrases


def _flush(run: List[str], phrases: Counter):
    if not run:
        return
    for size in (3, 2, 1):
        if len(run) >= size:
            for i in range(len(run) - size + 1):
                phrase = " ".join(run[i:i + size])
                if size > 1 or len(phrase) >= 4:
                    phrases[phrase] += 1


def index_terms(db_path: str, top_k: int = 12) -> dict:
    """Rebuild the terms table from FTS content, grouped by thread."""
    conn = fdb.connect(db_path)
    try:
        per_thread: Dict[str, List[str]] = {}
        for prompt_id, content in conn.execute(
                "SELECT prompt_id, content FROM fts "
                "WHERE prompt_id IS NOT NULL"):
            per_thread.setdefault(prompt_id, []).append(content)

        thread_phrases: Dict[str, Counter] = {}
        df: Counter = Counter()
        rows = []
        for pid, chunks in per_thread.items():
            # cap per-thread text: a mega-thread would otherwise explode the
            # 1-3gram Counter into millions of entries (multi-GB RSS)
            text = "\n".join(chunks)[:200_000]
            for op, n in match_operatives(text).items():
                rows.append((op, "operative", pid, n, float(n)))
            for tgt, n in extract_targets(text).items():
                rows.append((tgt, "target", pid, n, float(n)))
            for wl in set(extract_wikilinks(text)):
                rows.append((wl, "wikilink", pid, 1, 1.0))
            phrases = candidate_phrases(text)
            thread_phrases[pid] = phrases
            df.update(set(phrases))

        n_threads = max(len(per_thread), 1)
        for pid, phrases in thread_phrases.items():
            scored = [(tf * math.log(1 + n_threads / df[ph]), ph, tf)
                      for ph, tf in phrases.items()]
            scored.sort(reverse=True)
            for score, ph, tf in scored[:top_k]:
                rows.append((ph, "concept", pid, tf, score))

        conn.execute("DELETE FROM terms")
        conn.executemany(
            "INSERT OR REPLACE INTO terms(term, kind, prompt_id, count, score) "
            "VALUES(?,?,?,?,?)", rows)
        conn.commit()
        return {"threads": len(per_thread), "terms": len(rows)}
    finally:
        conn.close()
