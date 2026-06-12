# Fable Recall Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** High-fidelity context retrieval from massive Claude Code JSONL transcripts — index the immutable backup vault by uuid, search via FTS5 + extracted terms + LLM thread cards, page exact raw turns back into a live session under a token budget, and close the loop with a pruner that never evicts unindexed bytes.

**Architecture:** The live transcript is *working memory*, kept lean by the pruner. The backup generations (`v0-raw.jsonl`, `vN-pruned.jsonl` — immutable once written) plus the live tail are the *Vault*. A SQLite *Map* keys every record by `uuid` (stable across prune rewrites) and stores `(file, byte_offset, byte_len)` of the **best-fidelity copy** across generations. Retrieval is `seek(offset); read(len)` — never a file scan, never a summary. Threads are reconstructed from `promptId` + `parentUuid`, not heuristics. Recall output is wrapped in a `<historical_context>` sentinel; the indexer skips sentinel content (inception guard) and the pruner strips it, leaving citation stubs.

**Tech Stack:** Python 3.9+ stdlib only (`sqlite3` with FTS5, `urllib.request` for OpenRouter, `unittest`). No pip dependencies. OpenRouter free Gemma model for the card pass (key arrives later via `.env`).

---

## Empirical facts driving the design (measured on the real corpus)

- Live session `27ce47a8…`: 84MB, 34,988 lines, 1,869 distinct `promptId` threads, 12,553 user / 19,202 assistant records.
- Signal is small: ~4.2MB assistant text, 2.7MB tool_use inputs, 1.0MB tool_results. ~75MB is base64 images/attachments — **pruner v1 does not strip these** (v2 fixes).
- Real vault exists: 52 backup generations, 3.0GB. v0-raw is 6.6MB (first prune happened early); each generation seals the full-fidelity copies of messages appended since the previous prune. Union across generations = complete best-fidelity history.
- 1 line in the real file contains concatenated JSON objects (concurrent-write artifact) — reader must recover them (v1 pruner's `_parse_jsonl_line` approach).
- Wikilink tagging by the live agent empirically failed (4 tags in 31k records) → all term extraction is offline and deterministic; wikilinks are bonus signal only.

## File Structure

```
fable/
  README.md                      # quickstart, CLI reference, architecture
  .env.example                   # OPENROUTER_API_KEY=, OPENROUTER_MODEL=google/gemma-3-27b-it:free
  .gitignore                     # reference/, *.db, __pycache__
  bin/fable                      # thin shell wrapper → python3 -m fable
  fable/
    __init__.py
    __main__.py                  # dispatch to cli.main()
    cli.py                       # argparse: index, search, thread, block, cards, prune, stats
    jsonl.py                     # iter_records(path) → (lineno, offset, length, obj); concat recovery
    db.py                        # connect(path), SCHEMA DDL, migrations
    indexer.py                   # index_vault(db, vault_files) — generations, fidelity dedup, threads
    extract.py                   # record_text(obj) → list[(kind, text)]; inception-guard stripping
    terms.py                     # operatives (closed list), targets (regex), concepts (NP TF-IDF)
    threads.py                   # reconstruct(db, prompt_id) → ordered turns, canonical chain, sidechains
    recall.py                    # search/thread/block logic; budget elision; sentinel wrapping
    openrouter.py                # chat(messages) via urllib; retries; 429 backoff; .env loader
    cards.py                     # card prompt, JSON validation, resumable runner
    prune.py                     # v2 port: + image strip, + sentinel strip/citations, + evict gate
  tests/
    fixtures/                    # synthetic mini-transcripts incl. multi-generation vault
    test_jsonl.py  test_indexer.py  test_extract.py  test_terms.py
    test_threads.py  test_recall.py  test_openrouter.py  test_cards.py  test_prune.py
  reference/                     # copies only; never modified by code
    arc-memory-README.md, prune_transcript_v1.py,
    session-27ce47a8.jsonl (pristine), session-27ce47a8.stripped.jsonl (working),
    vault-sample/27ce47a8…/{v0-raw,v10,v30,v51,v52}.jsonl
  docs/ARCHITECTURE.md
```

## SQLite Schema (db.py)

```sql
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, label TEXT,
  generation INTEGER, immutable INTEGER, size INTEGER, mtime REAL);
CREATE TABLE IF NOT EXISTS records(
  uuid TEXT PRIMARY KEY,
  prompt_id TEXT, parent_uuid TEXT, type TEXT, role TEXT, ts TEXT,
  is_sidechain INTEGER DEFAULT 0,
  file_id INTEGER, lineno INTEGER, offset INTEGER, length INTEGER,
  fidelity INTEGER,          -- serialized byte length of best copy seen
  text_bytes INTEGER, has_images INTEGER, block_kinds TEXT);
CREATE INDEX IF NOT EXISTS idx_records_prompt ON records(prompt_id);
CREATE TABLE IF NOT EXISTS threads(
  prompt_id TEXT PRIMARY KEY, first_ts TEXT, last_ts TEXT,
  turn_count INTEGER, text_bytes INTEGER, est_tokens INTEGER,
  first_uuid TEXT, leaf_uuid TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  content, uuid UNINDEXED, prompt_id UNINDEXED, kind UNINDEXED);
CREATE TABLE IF NOT EXISTS terms(
  term TEXT, kind TEXT CHECK(kind IN ('operative','target','concept','wikilink')),
  prompt_id TEXT, count INTEGER, score REAL,
  PRIMARY KEY(term, kind, prompt_id));
CREATE TABLE IF NOT EXISTS cards(
  prompt_id TEXT PRIMARY KEY, title TEXT, type TEXT, topics TEXT,
  decisions TEXT, files TEXT, outcome TEXT, summary TEXT,
  est_tokens INTEGER, source TEXT, model TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS citations(
  from_uuid TEXT, ref TEXT, PRIMARY KEY(from_uuid, ref));
```

Fidelity rule: a uuid seen in multiple files keeps the copy with the larger serialized length (pruned copies are strictly shorter by construction). Ties → earlier generation.

## CLI Surface

```
fable index  --db fable.db --vault DIR_OR_FILES... [--live FILE]
fable search QUERY [--db] [--operative V] [--target T] [--type decision] [-n 10]
fable thread PROMPT_ID [--budget 8000] [--raw] [--no-sentinel]
fable block UUID [--raw]
fable cards run [--limit N] [--min-tokens N] [--model M] [--dry-run]
fable cards show PROMPT_ID
fable prune FILE --mode resume|extract|handoff [--strip-images] [--db fable.db] [--force] ...
fable stats [--db]
```

## Tasks

### Task 1: Scaffold
- [ ] git init in fable/; package skeleton; `.env.example`; `.gitignore` excluding `reference/` + `*.db`; `bin/fable` wrapper; generate `session-27ce47a8.stripped.jsonl` (image source.data → `"<stripped>"` placeholder) with a throwaway script in `scripts/strip_images.py`; commit.

### Task 2: jsonl.py (TDD)
- [ ] Test: iter_records yields correct (lineno, offset, length, obj) for normal file; offsets verified by re-seek+parse.
- [ ] Test: line with two concatenated objects yields both, same lineno/offset span.
- [ ] Test: malformed line is skipped and reported via callback, iteration continues.
- [ ] Implement with incremental `json.JSONDecoder.raw_decode`; run; commit.

### Task 3: db.py + indexer.py (TDD)
- [ ] Fixture: 3-generation synthetic vault (gen0 full, gen1 pruned copies + new full records, live tail).
- [ ] Test: after index_vault, every uuid points at its fullest copy (fidelity = max length).
- [ ] Test: re-index is idempotent; adding a new generation upgrades only affected rows.
- [ ] Test: threads table aggregates promptId correctly (turn_count, first/last ts).
- [ ] Test: records without uuid (custom-title etc.) are skipped without error.
- [ ] Implement; run; commit.

### Task 4: extract.py + FTS (TDD)
- [ ] Test: user/assistant text and thinking extracted; tool_use inputs flattened (command, file_path, pattern, query, prompt, description); tool_result text capped at 2KB head; image blocks → nothing; base64-looking strings dropped.
- [ ] Test: `<historical_context arc="X">…</historical_context>` spans are removed from extracted text and produce citations rows (from_uuid, ref=X).
- [ ] Test: FTS populated at index time; `fable search` returns BM25-ranked hits with prompt_id.
- [ ] Implement; run; commit.

### Task 5: terms.py (TDD)
- [ ] Test: operative matching is stem-based against closed list (fix/fixed/fixing → fix), only in user/assistant text.
- [ ] Test: target regexes catch `/abs/path/file.rs`, `crates/x/src/y.rs`, `sled-zigzag`, `snake_case_fn`, `CamelCaseType`, backtick spans; reject plain English.
- [ ] Test: concept NP extraction returns stopword-filtered noun phrases ranked by per-thread TF-IDF; top-K per thread.
- [ ] Test: surviving `[[wikilinks]]` extracted as kind=wikilink.
- [ ] Implement; run; commit.

### Task 6: threads.py + recall.py + cli.py (TDD)
- [ ] Test: canonical chain = walk parentUuid up from latest leaf; edit-branches flagged as orphans; sidechain records grouped separately.
- [ ] Test: `block` returns byte-identical raw JSON line from vault via seek (compare against direct file read).
- [ ] Test: thread rendering under --budget: text blocks verbatim, oversized tool_results elided with `[truncated — fable block <uuid>]`, image blocks → `[image]`; budget respected within tolerance.
- [ ] Test: output wrapped in `<historical_context session=… thread=… arcs=…>` sentinel; `--no-sentinel` disables.
- [ ] Test: search facets `--operative decide --target zigzag` intersect terms with FTS results.
- [ ] Implement; run; commit.

### Task 7: openrouter.py + cards.py (TDD, mock server)
- [ ] Test: .env loading (no override of existing env); missing key → clear actionable error.
- [ ] Test: chat() against local http.server mock: success, 429 → backoff retry, 5xx → retry, malformed JSON → CardError.
- [ ] Test: card prompt includes elided thread text; response validated against required fields; one repair-retry on invalid JSON.
- [ ] Test: runner resumes — threads with existing cards skipped; --limit honored; failures recorded, don't abort run.
- [ ] Implement; run; commit.

### Task 8: Sample cards via Claude subagents
- [ ] Select ~30 highest-signal threads (est_tokens, distinct files); generate cards via subagents reading `fable thread` output; insert source='claude-subagent'; verify `fable search` hits them.

### Task 9: prune.py v2 (TDD)
- [ ] Port v1 behaviors with tests: noise drop, compaction resume/extract, chain rebuild, backup versioning, validation.
- [ ] Test: --strip-images replaces base64 image sources with placeholder, records original byte count.
- [ ] Test: `<historical_context>` spans stripped from message text, replaced by `<consulted_arcs refs="…"/>` stub.
- [ ] Test: evict gate — `--replace` aborts listing unindexed uuids when live file has records absent from --db index (or present at lower fidelity); `--force` overrides; without --db, warn loudly.
- [ ] Implement; run; commit.

### Task 10: Integration on real corpus
- [ ] Index 5 vault generations + pristine live copy; assert uuid counts, dedup behavior, timing < a few minutes, db size sane.
- [ ] Run demo queries: zigzag, SLED, "option chain", prune, openrouter, RRG; verify thread retrieval round-trips byte-identical.
- [ ] Record numbers for README; commit.

### Task 11: Docs
- [ ] README.md (quickstart, CLI, lifecycle diagram, card-pass instructions for when the key arrives), docs/ARCHITECTURE.md (decisions + rejected alternatives), final commit.

## Self-Review Notes
- Spec coverage: indexing (T3-5), retrieval (T6), precision/threading (T6), discovery without taxonomy (T5 concepts + T7/8 cards), lossless guarantee (fidelity dedup T3 + evict gate T9), inception guard (T4 extract + T9 prune), OpenRouter (T7), both transcript copies (T1), v1 image-bloat gap (T9).
- All paths exact; schema and CLI fixed above; test cases enumerated per task.
