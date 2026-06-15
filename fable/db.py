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
  files TEXT,
  outcome TEXT,
  summary TEXT,
  est_tokens INTEGER,
  source TEXT,
  model TEXT,
  created_at TEXT
);

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


def connect(path: str, create: bool = False) -> sqlite3.Connection:
    """Open the Map. Read paths must not silently create an empty index —
    pass create=True only from index/discover."""
    import os
    if not create and path != ":memory:" and not os.path.exists(path):
        raise FileNotFoundError(
            f"no fable index at {path!r} — run `fable index` or "
            f"`fable discover` first (or pass --db)")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    for ddl in ("ALTER TABLE sessions ADD COLUMN pinned INTEGER DEFAULT 0",
                "ALTER TABLE sessions ADD COLUMN tags TEXT"):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
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
