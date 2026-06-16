"""SQLite Map: schema and connection helpers.

The Map never stores transcript payloads (FTS text excepted) — records carry
(file, offset, length) pointers into immutable vault files. uuid is the
primary key because it is the only identity stable across prune rewrites.
"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY,
  project TEXT,
  title TEXT,
  live_path TEXT,
  indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  label TEXT,
  generation INTEGER,
  immutable INTEGER NOT NULL DEFAULT 1,
  size INTEGER,
  mtime REAL,
  session_id TEXT,
  project TEXT
);

CREATE TABLE IF NOT EXISTS records(
  uuid TEXT PRIMARY KEY,
  prompt_id TEXT,
  parent_uuid TEXT,
  type TEXT,
  role TEXT,
  ts TEXT,
  is_sidechain INTEGER NOT NULL DEFAULT 0,
  file_id INTEGER NOT NULL REFERENCES files(id),
  lineno INTEGER,
  offset INTEGER NOT NULL,
  length INTEGER NOT NULL,
  fidelity INTEGER NOT NULL,
  text_bytes INTEGER NOT NULL DEFAULT 0,
  has_images INTEGER NOT NULL DEFAULT 0,
  block_kinds TEXT,
  session_id TEXT,
  source_uuid TEXT,
  fts_rowid INTEGER,
  ts_epoch REAL,
  model TEXT,
  in_tokens INTEGER,
  out_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS idx_records_prompt ON records(prompt_id);
CREATE INDEX IF NOT EXISTS idx_records_file ON records(file_id);
CREATE INDEX IF NOT EXISTS idx_records_session ON records(session_id);

-- every copy of every uuid across all generations. records holds the BEST
-- pointer; copies is the ground truth that lets the best pointer be
-- recomputed when files are rewritten, deleted, or arrive late.
CREATE TABLE IF NOT EXISTS copies(
  uuid TEXT NOT NULL,
  file_id INTEGER NOT NULL REFERENCES files(id),
  lineno INTEGER,
  offset INTEGER NOT NULL,
  length INTEGER NOT NULL,
  PRIMARY KEY(uuid, file_id)
);
CREATE INDEX IF NOT EXISTS idx_copies_file ON copies(file_id);

CREATE TABLE IF NOT EXISTS threads(
  prompt_id TEXT PRIMARY KEY,
  first_ts TEXT,
  last_ts TEXT,
  turn_count INTEGER,
  text_bytes INTEGER,
  est_tokens INTEGER,
  first_uuid TEXT,
  leaf_uuid TEXT,
  session_id TEXT,
  sidechain_turns INTEGER DEFAULT 0,
  models TEXT
);
CREATE INDEX IF NOT EXISTS idx_threads_session ON threads(session_id);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  content, uuid UNINDEXED, prompt_id UNINDEXED, kind UNINDEXED
);

CREATE TABLE IF NOT EXISTS terms(
  term TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('operative','target','concept','wikilink')),
  prompt_id TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  score REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(term, kind, prompt_id)
);
CREATE INDEX IF NOT EXISTS idx_terms_prompt ON terms(prompt_id);

CREATE TABLE IF NOT EXISTS cards(
  prompt_id TEXT PRIMARY KEY,
  title TEXT,
  type TEXT,
  topics TEXT,
  decisions TEXT,
  ideas TEXT,
  features TEXT,
  lessons TEXT,
  gotchas TEXT,
  open_questions TEXT,
  directives TEXT,
  files TEXT,
  outcome TEXT,
  summary TEXT,
  est_tokens INTEGER,
  source TEXT,
  model TEXT,
  created_at TEXT
);

-- thread-level tags emitted by the carder (controlled dimensions + open
-- semantic families). Mirrors the `terms` table shape; a SECONDARY facet over
-- FTS, never the primary retrieval edge. One row per (thread, family, value).
CREATE TABLE IF NOT EXISTS thread_tags(
  prompt_id TEXT NOT NULL,
  family TEXT NOT NULL,
  value TEXT NOT NULL,
  score REAL NOT NULL DEFAULT 0,
  source TEXT,
  model TEXT,
  created_at TEXT,
  PRIMARY KEY(prompt_id, family, value)
);
CREATE INDEX IF NOT EXISTS idx_thread_tags_fv ON thread_tags(family, value);
CREATE INDEX IF NOT EXISTS idx_thread_tags_prompt ON thread_tags(prompt_id);

CREATE TABLE IF NOT EXISTS citations(
  from_uuid TEXT NOT NULL,
  ref TEXT NOT NULL,
  PRIMARY KEY(from_uuid, ref)
);

-- /remember: durable cross-session facts, injected at SessionStart
CREATE TABLE IF NOT EXISTS ops(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  kind TEXT NOT NULL,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_ops_kind ON ops(kind);

-- card-generation telemetry: one row per attempt (success or failure),
-- so the dashboard can show reliability per provider x model
CREATE TABLE IF NOT EXISTS card_attempts(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  prompt_id TEXT,
  provider TEXT,
  model TEXT,
  ok INTEGER NOT NULL DEFAULT 0,
  reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_card_attempts_pm ON card_attempts(provider, model);

CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY,
  fact TEXT NOT NULL,
  project TEXT,
  source TEXT DEFAULT 'user',
  created_at TEXT,
  active INTEGER DEFAULT 1
);

-- semantic layer: per-thread vectors (packed float32), backend-tagged.
-- kind='card' (distilled summary) and kind='thread' (the conversation's own
-- text) coexist; semantic_hits takes the best match per thread.
CREATE TABLE IF NOT EXISTS embeddings(
  prompt_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'card',
  vec BLOB NOT NULL,
  dim INTEGER NOT NULL,
  backend TEXT,
  PRIMARY KEY(prompt_id, kind)
);
"""


def write_retry(fn, *, attempts: int = 6, base: float = 0.05):
    """Run a DB-writing callable, retrying on a transient 'database is locked'.
    WAL serializes writers and busy_timeout handles most contention; this is the
    belt-and-suspenders for the rest under heavy multi-session write load. Any
    non-lock error propagates immediately; the lock error re-raises after the
    final attempt. Wrap user-triggered writes (recluster, task meta, …) so a
    momentary collision returns gracefully instead of 500-ing."""
    import time
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or i == attempts - 1:
                raise
            time.sleep(base * (2 ** i))   # 0.05, 0.1, 0.2, 0.4, 0.8, 1.6s


def connect(path: str, create: bool = False) -> sqlite3.Connection:
    """Open the Map. Read paths must not silently create an empty index —
    pass create=True only from index/discover."""
    import os
    if not create and path != ":memory:" and not os.path.exists(path):
        raise FileNotFoundError(
            f"no fable index at {path!r} — run `fable index` or "
            f"`fable discover` first (or pass --db)")
    if create and path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # WAL = many readers + one writer; writers serialize. 20s gives a waiting
    # writer plenty of room to acquire the lock under heavy multi-session load
    # (paired with chunked bulk writes so no writer holds it for seconds).
    conn.execute("PRAGMA busy_timeout=20000")
    conn.executescript(SCHEMA)
    for ddl in ("ALTER TABLE sessions ADD COLUMN pinned INTEGER DEFAULT 0",
                "ALTER TABLE sessions ADD COLUMN tags TEXT",
                "ALTER TABLE cards ADD COLUMN ideas TEXT",
                "ALTER TABLE cards ADD COLUMN features TEXT",
                "ALTER TABLE cards ADD COLUMN lessons TEXT",
                "ALTER TABLE cards ADD COLUMN gotchas TEXT",
                "ALTER TABLE cards ADD COLUMN open_questions TEXT",
                "ALTER TABLE cards ADD COLUMN directives TEXT",
                "ALTER TABLE cards ADD COLUMN card_gen INTEGER DEFAULT 1"):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
    # one-time: cards that already carry the latest schema (directives present)
    # were re-carded under the current generation — mark them gen 2 so a
    # RESUMABLE re-card skips them instead of redoing them. Guarded so it runs
    # once; cards missing directives stay gen 1 and get regenerated.
    try:
        if not conn.execute(
                "SELECT 1 FROM meta WHERE key='cardgen2_backfilled'").fetchone():
            conn.execute("UPDATE cards SET card_gen=2 "
                         "WHERE card_gen < 2 AND directives IS NOT NULL")
            conn.execute("INSERT OR REPLACE INTO meta(key, value) "
                         "VALUES('cardgen2_backfilled', '1')")
            conn.commit()
    except sqlite3.OperationalError:
        pass
    # migrate the embeddings table to the composite (prompt_id, kind) key so
    # card- and thread-vectors can coexist; existing rows become kind='card'
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(embeddings)")]
        if cols and "kind" not in cols:
            conn.executescript(
                "ALTER TABLE embeddings RENAME TO _embeddings_old;"
                "CREATE TABLE embeddings(prompt_id TEXT NOT NULL,"
                " kind TEXT NOT NULL DEFAULT 'card', vec BLOB NOT NULL,"
                " dim INTEGER NOT NULL, backend TEXT,"
                " PRIMARY KEY(prompt_id, kind));"
                "INSERT INTO embeddings(prompt_id,kind,vec,dim,backend)"
                " SELECT prompt_id,'card',vec,dim,backend FROM _embeddings_old;"
                "DROP TABLE _embeddings_old;")
            conn.commit()
    except sqlite3.OperationalError:
        pass
    return conn


def log_op(target, kind, **detail):
    """Append to fable's own activity journal (ops table). Never raises —
    telemetry must not break the operation it describes."""
    import json as _json
    try:
        own = isinstance(target, str)
        conn = connect(target) if own else target
        conn.execute("INSERT INTO ops(kind, detail) VALUES(?, ?)",
                     (kind, _json.dumps(detail)[:2000]))
        if own:
            conn.commit()
            conn.close()
    except Exception:
        pass
