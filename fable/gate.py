"""Compaction-read gate — DETERMINISTIC enforcement that a freshly-compacted
model READS its recovery threads before it edits or runs anything, instead of
trusting the lossy summary.

The soft version (hook._compaction_recovery injects a "read these UUIDs first"
directive) relies on the model obeying injected text — which fails in practice
(the model can rationalise skipping it). This gate removes the choice:

  • arm()              — at SessionStart(source=compact), freeze the session's
                         recovery set (the same threads the directive lists).
  • pending()          — what's still unread for a session.
  • PreToolUse hook    — if anything is pending, DENY Edit/Write/MultiEdit.
  • fable_attest()     — the model clears a thread by submitting a one-line
                         summary; validate_summary() checks it against the
                         thread's REAL content (the card / indexed text) so a
                         generic or empty summary is rejected. You cannot write
                         a faithful one-liner about a thread without reading it.

What this is NOT: proof of comprehension (impossible to verify). It forces the
content into context, removes the "I didn't have it" excuse, and makes skipping
more expensive than reading. It catches "didn't read at all" — not "read
shallowly". That is as close as a mechanism gets.

Everything FAILS OPEN: a broken gate must never brick real work (same contract
as the hook layer). State lives in one table, lazily created.
"""
import datetime
import re

from fable import db as fdb

# the N most-recent recovery threads are HARD-gated (must be attested before
# edits flow). Bounds the friction — gating all ~18 would mean 18 reads + 18
# summaries before any edit, which is heavy enough that the gate gets disabled.
# The most-recent threads carry the live working context, so they matter most.
HARD_GATE_N = 5
_MIN_SUMMARY_CHARS = 25
_MIN_OVERLAP = 2                       # distinctive tokens a summary must share

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_./\-]{3,}")   # identifier-ish, len>=4
_STOP = {
    "this", "that", "then", "than", "with", "from", "have", "been", "were",
    "will", "would", "could", "should", "about", "which", "their", "there",
    "thread", "session", "summary", "because", "before", "after", "into",
    "over", "they", "them", "your", "yours", "what", "when", "where", "while",
    "code", "file", "files", "data", "function", "value", "thing", "stuff",
    "some", "also", "only", "just", "like", "make", "made", "does", "done",
}

_DDL = """CREATE TABLE IF NOT EXISTS compaction_gate(
  session_id TEXT NOT NULL,
  uuid       TEXT NOT NULL,
  title      TEXT,
  attested   INTEGER NOT NULL DEFAULT 0,
  summary    TEXT,
  ts         TEXT,
  PRIMARY KEY (session_id, uuid)
);"""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _table_exists(conn):
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='compaction_gate'").fetchone() is not None


def recovery_threads(db_path, session_id, limit=18):
    """The session's last `limit` threads as [(uuid, title), ...], most-recent
    first — THE recovery set. Single source of truth shared by the SessionStart
    injection (hook._compaction_recovery) and this gate, so the threads the
    model is TOLD to read are exactly the ones it is BLOCKED on."""
    try:
        conn = fdb.connect(db_path)
    except Exception:
        return []
    try:
        return [(r[0], r[1]) for r in conn.execute(
            "SELECT t.prompt_id, c.title FROM threads t "
            "LEFT JOIN cards c ON c.prompt_id = t.prompt_id "
            "WHERE t.session_id = ? ORDER BY t.last_ts DESC LIMIT ?",
            (session_id, limit)).fetchall() if r[0]]
    finally:
        conn.close()


def arm(db_path, session_id, threads=None):
    """Arm the gate for a just-compacted session: the most-recent HARD_GATE_N
    recovery threads become blocking until attested. `threads` defaults to
    recovery_threads(). Idempotent — a re-fired SessionStart drops only stale
    UNattested rows and keeps any attestation already earned this session."""
    if threads is None:
        threads = recovery_threads(db_path, session_id)
    threads = threads[:HARD_GATE_N]
    if not threads:
        return 0
    try:
        conn = fdb.connect(db_path)
    except Exception:
        return 0
    try:
        conn.execute(_DDL)
        # a fresh compaction supersedes a prior unmet gate; keep what's already
        # attested so the model isn't forced to re-read mid-session
        conn.execute("DELETE FROM compaction_gate WHERE session_id=? AND "
                     "attested=0", (session_id,))
        for uuid, title in threads:
            conn.execute(
                "INSERT OR IGNORE INTO compaction_gate(session_id, uuid, title,"
                " attested, ts) VALUES(?,?,?,0,?)",
                (session_id, uuid, title, _now()))
        conn.commit()
        return len(threads)
    finally:
        conn.close()


def pending(db_path, session_id):
    """[(uuid, title), ...] still unattested for this session. Empty when the
    gate is unarmed (normal session) or fully satisfied."""
    try:
        conn = fdb.connect(db_path)
    except Exception:
        return []
    try:
        if not _table_exists(conn):
            return []
        return conn.execute(
            "SELECT uuid, title FROM compaction_gate WHERE session_id=? AND "
            "attested=0 ORDER BY ts", (session_id,)).fetchall()
    finally:
        conn.close()


def _distinctive(blob):
    """Distinctive token set from text: identifier-ish, len>=4, minus stopwords
    and generic words. This is the 'answer key' a real summary must hit."""
    return {t for t in _TOKEN.findall((blob or "").lower()) if t not in _STOP}


def _thread_key(conn, uuid):
    """Answer key for a thread: distinctive tokens from its card (title +
    salient_entities + decisions). Falls back to the thread's own indexed text
    when it has no card yet (the most-recent threads often aren't carded)."""
    row = conn.execute(
        "SELECT title, salient_entities, decisions FROM cards WHERE prompt_id=?",
        (uuid,)).fetchone()
    blob = " ".join(str(x or "") for x in row) if row else ""
    key = _distinctive(blob)
    if len(key) >= _MIN_OVERLAP:
        return key
    # uncarded (or thin card): key off the thread's actual indexed text
    rows = conn.execute(
        "SELECT ft.content FROM records r JOIN fts ft ON ft.uuid = r.uuid "
        "WHERE r.prompt_id = ? LIMIT 40", (uuid,)).fetchall()
    return key | _distinctive(" ".join((r[0] or "") for r in rows))


def validate_summary(db_path, uuid, summary):
    """Deterministic check that `summary` plausibly came from READING the thread:
    non-trivial length AND it shares >= _MIN_OVERLAP distinctive tokens with the
    thread's real content. Returns (ok: bool, reason: str). Fails OPEN (accepts)
    only when the db is unreachable or the thread has no extractable key at all
    — never blocks on the gate's own inability to build an answer key."""
    s = (summary or "").strip()
    if len(s) < _MIN_SUMMARY_CHARS:
        return False, (f"summary too short ({len(s)}<{_MIN_SUMMARY_CHARS} "
                       "chars) — name what the thread actually decided")
    try:
        conn = fdb.connect(db_path)
    except Exception:
        return True, "db unavailable — fail open"
    try:
        key = _thread_key(conn, uuid)
    finally:
        conn.close()
    if not key:
        return True, "no answer key available — length-only check passed"
    overlap = key & _distinctive(s)
    if len(overlap) >= _MIN_OVERLAP:
        return True, "matched thread on " + ", ".join(sorted(overlap)[:5])
    return False, ("summary doesn't reference this thread's real content — read "
                   "it with fable_thread first, then name its specific "
                   "entities / decisions")


def attest(db_path, uuid, summary):
    """Clear one gated thread, gated on a validated summary. The caller (the MCP
    tool) need not know the session_id — a thread belongs to exactly one
    session's gate, found by uuid. Returns ok + remaining-pending for that
    session. Validation failure does NOT mark it read."""
    ok, reason = validate_summary(db_path, uuid, summary)
    if not ok:
        return {"ok": False, "uuid": uuid, "reason": reason}
    try:
        conn = fdb.connect(db_path)
    except Exception:
        return {"ok": True, "uuid": uuid, "note": "db unavailable — fail open"}
    try:
        conn.execute(_DDL)
        row = conn.execute("SELECT session_id FROM compaction_gate WHERE uuid=?",
                           (uuid,)).fetchone()
        if not row:
            return {"ok": True, "uuid": uuid,
                    "note": "not a gated thread (nothing to clear)"}
        sid = row[0]
        conn.execute("UPDATE compaction_gate SET attested=1, summary=?, ts=? "
                     "WHERE uuid=?", (s_trunc(summary), _now(), uuid))
        conn.commit()
        rem = conn.execute(
            "SELECT uuid, title FROM compaction_gate WHERE session_id=? AND "
            "attested=0 ORDER BY ts", (sid,)).fetchall()
    finally:
        conn.close()
    return {"ok": True, "uuid": uuid, "validated": reason,
            "remaining": len(rem),
            "still_pending": [{"uuid": u, "title": t} for u, t in rem]}


def s_trunc(s, n=400):
    s = s or ""
    return s if len(s) <= n else s[:n]


def deny_message(pend):
    """The PreToolUse deny reason: what must be read + attested before editing."""
    lines = [f'  • fable_thread("{u}")  then  fable_attest("{u}", '
             f'"<one line: what this thread decided>")'
             + (f'   — {t}' if t else "")
             for u, t in pend]
    return (
        f"⛔ COMPACTION-READ GATE — {len(pend)} recovery thread(s) unread.\n"
        "This session was compacted; the summary is lossy. Before any "
        "Edit/Write/MultiEdit you must READ each thread below and ATTEST it "
        "with a one-line summary (validated against the thread — a generic or "
        "empty summary is rejected):\n" + "\n".join(lines) +
        "\nfable_thread and fable_attest are NOT blocked — clear the gate, then "
        "your edit will go through. (This exists because a freshly-compacted "
        "model that trusts the summary ships wrong work.)\n"
        "If fable_attest is unavailable (an MCP server predating this feature), "
        "restart the session — or the user can disable the gate with "
        "FABLE_COMPACTION_GATE=off.")
