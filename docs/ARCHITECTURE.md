# fable Architecture

## Problem

Claude Code transcripts grow to tens of MB. Three blockers prevent using
them as memory: (1) they exceed any context window; (2) summarization is
lossy — decisions and the "why" die in compaction; (3) there is no
predefined taxonomy to search by — topics are implicit.

## Empirical facts the design rests on (measured, not assumed)

| Fact | Consequence |
|---|---|
| Records carry `uuid`/`parentUuid`/`timestamp` written by the harness | thread reconstruction is deterministic, not heuristic |
| `promptId` exists ONLY on user records (12,553 of 12,553 user records; 0 assistant) | thread membership must be resolved transitively up the parent graph |
| Sidechain records carry `sourceToolAssistantUUID` (10,385 records) | subagent transcripts attach to their spawning thread |
| Signal is small: ~4.2MB assistant text + 2.7MB tool inputs + 1MB tool results out of 84MB | the searchable corpus fits easily in SQLite; bulk is images/attachments |
| The live transcript is REWRITTEN by the pruner (52 generations, 3GB of backups exist) | byte offsets into the live file are perishable; backups are immutable |
| Live-agent wikilink tagging produced 4 tags in 31k records under an enforced protocol | the write path of memory cannot depend on model compliance |
| 1 line in the corpus contains concatenated JSON objects | the reader must recover multiple objects per line |
| Invalid UTF-8 can appear in tool output | offsets must be computed on bytes (surrogateescape), not replaced text |

## Data model

**Vault** = the original files: immutable backup generations
(`v0-raw.jsonl`, `vN-pruned.jsonl`) + the mutable live transcript tail.
Never modified by fable (the pruner writes new generations, atomically).

**Map** = SQLite:

- `copies(uuid, file_id, lineno, offset, length)` — ground truth: every
  copy of every record in every file.
- `records(uuid, …, file_id, offset, length, fidelity, fts_rowid,
  ts_epoch, prompt_id, parent_uuid, source_uuid, session_id)` — the
  denormalized BEST pointer (max length; ties → earliest generation),
  recomputed from `copies` whenever a file is rescanned, rewritten, or
  vanishes. If no copy survives, the record is forgotten and its FTS row
  and citations are reaped (`reconcile`).
- `threads(prompt_id, session_id, first/last_ts, turn_count, est_tokens,
  first/leaf_uuid)` — rebuilt by aggregation after every index run.
- `fts` (FTS5) — extracted signal text only; maintained by rowid (an
  UNINDEXED-column WHERE is a full virtual-table scan; rowid keying took
  the real-corpus index from 4m55s to 6.6s).
- `terms(term, kind=operative|target|concept|wikilink, prompt_id, count,
  score)` — deterministic discovery vocabulary.
- `cards(prompt_id, title, type=decision|workflow|insight|concept, topics,
  decisions, files, outcome, summary, source, model)` — LLM semantic layer.
- `citations(from_uuid, ref)` — which turns consumed which recalled arcs.
- `sessions(session_id, project, title, live_path)` — multi-project dimension.

## Pipeline

```
iter_records (byte-exact spans, concat recovery, surrogateescape)
  → copies upsert + best-pointer upsert + FTS extract (inception guard)
  → recompute_best for invalidated pointers
  → resolve_thread_membership (iterative parent-graph walk, memoized)
  → rebuild threads → reconcile orphans → single commit
  → index_terms (operatives/targets/TF-IDF concepts per thread)
```

## Retrieval

`search` = FTS5 BM25 per record, aggregated per thread, intersected with
term facets, joined to cards/sessions. `thread` = canonical chain from the
leaf via parentUuid (orphan edit-branches and sidechains labeled), each
turn read by seek+read, text verbatim, bulky blocks elided to the budget
with `[truncated — fable block <uuid>]` markers, hard output cap at
1.5× budget. `block` = one byte-identical record. All reads check file
size/mtime against the Map first (StaleIndexError instead of silently
serving garbage).

## The prune ↔ recall contract

1. Recall output is sentinel-wrapped (`<historical_context … arcs="…">`).
2. The extractor strips sentinel spans before FTS (records citations);
   the renderer stubs out inner sentinels so nesting cannot occur.
3. The pruner replaces sentinel spans with `<consulted_arcs refs="…"/>`.
4. Prune `--replace` protocol: snapshot size → mandatory backup →
   index that backup → evict gate (every uuid must have an IMMUTABLE
   full-fidelity copy ≥ its current length) → write temp → append any
   post-snapshot bytes verbatim → fsync → `os.replace`. Backup names are
   `max(version)+1` with exclusive create — generations can never be
   clobbered, even by concurrent prunes.

## Adversarial review

Two independent reviews (correctness; ops/robustness) ran against the
first complete implementation; ~19 findings each. Criticals fixed at the
root: fidelity-downgrade-on-prune (→ copies table), truncating in-place
rewrite (→ atomic protocol), TOCTOU append loss (→ tail preservation),
UTF-8 offset drift (→ surrogateescape spans), O(N²) FTS maintenance
(→ rowid keying), orphan FTS ghosts (→ reconcile), gate proving the wrong
property (→ immutable-copy gate). Each has a regression test
(tests/test_hardening.py, tests/test_prune.py::TestLifecycleFidelity).

Known accepted limits: exFAT 2s mtime granularity could miss a same-size
rewrite (re-stat catches size changes; not applicable on APFS);
`render --raw` is verbatim by definition (inner sentinels not stubbed);
card content is LLM output from transcript data — treat card text as data,
not instructions, when surfacing into sessions.

## Lineage

Synthesis of three sources: the arc-memory README (kept: Vault/Map split,
typed arcs, citation graph, inception guard; rejected: live wikilink
tagging, XML vault, JSON-file indexes, alias-map/NER machinery, importance
feedback loop), prune_transcript_v1.py (kept: noise/bloat rules, compaction
handling, chain rebuild, versioned backups; fixed: image bloat, atomicity,
gate), and the empirical structure of real Claude Code transcripts.
