# Fable Thread-Level Tag-Carding — Additive Spec

**Date:** 2026-06-15 · **Status:** draft · **Scope:** dev-only (taxonomy stays private; do not publish to `public/`)

## 0. Principles (non-negotiable)

1. **Additive, not standalone.** Every change is a delta inside the existing carder, tables, search, graph, cost, and dashboard. No new daemon, no new route, no parallel subsystem.
2. **Free + thread-level.** Tagging is one extra JSON field in the *same* `generate_card` call over the *same* rendered thread → $0 marginal (free OpenRouter rotation), one row per thread.
3. **Tags are a secondary facet over FTS — never the primary retrieval edge.** This is what kept the wikilink-rejection from re-biting: FTS still indexes everything; tags add structure for facets/graph/analytics. (Decision thread `8b109868`, 2026-06-12: explicit edges cap recall.)
4. **Taxonomy is private.** Loaded at runtime from `~/.fable/taxonomy.yaml` (outside any git tree). Public code ships only the loader + a generic `taxonomy.example.yaml`.
5. **Reuse, don't reinvent.** `card_attempts` grade = the `tagged_by` provenance/confidence signal. `cards.decisions` = decision_detail. `cards.topics` stays for back-compat.

---

## 1. Data model (additive DDL — `fable/db.py:187`, beside existing migrations)

One normalized table is the home for **all** tags (controlled dims + semantic families), mirroring the existing `terms` table shape:

```sql
CREATE TABLE IF NOT EXISTS thread_tags(
  prompt_id  TEXT NOT NULL,
  family     TEXT NOT NULL,   -- domain|activity|event|artifact|outcome|decision
                              -- | topic|technology|entity|pattern|intent|context
  value      TEXT NOT NULL,   -- lowercase snake_case, [a-z][a-z0-9_]{0,39}
  score      REAL NOT NULL DEFAULT 0,
  source     TEXT,            -- provider (openrouter/claude-cli)
  model      TEXT,            -- model that produced the tag
  created_at TEXT,
  PRIMARY KEY(prompt_id, family, value)
);
CREATE INDEX IF NOT EXISTS idx_thread_tags_fv     ON thread_tags(family, value);
CREATE INDEX IF NOT EXISTS idx_thread_tags_prompt ON thread_tags(prompt_id);
```

- **Controlled families** (`domain, activity, event, artifact, outcome, decision`) carry exactly one value per thread; **semantic families** carry several. Uniform table → one filter pattern, one facet query, one co-occurrence query.
- `cards.topics` is left untouched (graph keeps working); `family='topic'` rows are the same values, so the graph can migrate to `thread_tags` later with no break.
- `decision_detail` already lives in `cards.decisions`. `outcome`/`type` already on `cards`.
- **No `cards.tags` column needed** — the normalized table powers facets, co-occurrence, and tag×cost joins that a JSON blob can't.

**FTS:** in `store_card` (`fable/cards.py:103-104`) append `" ".join(tag values)` to the card `content` string → tags become full-text searchable with **zero** changes to the search path.

**Embeddings (optional):** append tag values to the card-vector source text at `fable/embeddings.py:144-148` so semantic search reflects tags.

---

## 2. The carder change (the single chokepoint)

### 2.1 Taxonomy loader — new `fable/taxonomy.py` (tiny, pure)
- `load_taxonomy()` → reads `~/.fable/taxonomy.yaml`; falls back to bundled `taxonomy.example.yaml`; if neither, returns `None` (carder then free-tags semantic families with no controlled vocab). `@lru_cache`.
- Mirrors memory-mcp `sidecar/taxonomy/default_domains.py`. Seed the private file from memory-mcp's `default_domains.yaml` + `dimensions.py` enums.

### 2.2 Prompt — extend `PROMPT` (`fable/cards.py:20-44`)
Add one field + a compact taxonomy block built from the loaded vocab (lifted from memory-mcp `tag_specialist.build_batch_user_prompt`):
```
  "tags": list of {"family": ..., "value": ...} objects, where:
     controlled (exactly one each): domain, activity, event, artifact, outcome, decision
     semantic (zero or more): topic, technology, entity, pattern, intent, context
     value = lowercase snake_case [a-z][a-z0-9_]{0,39}; prefer the KNOWN VOCABULARY;
     skip anything you can't express concisely.
```
+ inject `Domain:[...] Activity:[...] Event:[...] Artifact:[...] Outcome:[...]` enums and the `KNOWN VOCABULARY` per family. Same single call → inherits repair-retry + rotation.

### 2.3 Parse — `parse_card` (`fable/cards.py:67-84`)
Add `tags`: normalize to `[(family, value)]`; **validate** — controlled values must match the enum (drop misses); semantic values must pass the snake_case regex (drop misses); snap near-misses to known vocab. Never fail the card on a bad tag.

### 2.4 Store — `store_card` (`fable/cards.py:87-106`)
After the card INSERT: `INSERT OR REPLACE INTO thread_tags(...)` per `(family,value)` with `source`/`model`/`created_at`; append tag values to the FTS content join. Same transaction.

### 2.5 Provenance — reuse `card_attempts`
Tagging rides the same `generate_card` attempt, so the A–D grade (`_grade`, `fable/serve.py:679`) already covers tag quality. Optional: add `phase TEXT` to distinguish card vs tag attempts if we later split them.

### 2.6 Backfill existing carded threads
~6k threads are being carded now (no tags yet). Two additive paths in `run_cards` (`fable/cards.py:260`):
- **New threads:** tags inline (free, automatic).
- **Already-carded:** a tag-only pass — same loop filtered to `cards c WHERE NOT EXISTS (SELECT 1 FROM thread_tags t WHERE t.prompt_id=c.prompt_id)`, using a **tags-only prompt** (cheaper; reuses the rendered thread). Rides FREE_ROTATION + 429 failover unchanged.

---

## 3. Retrieval (additive)

- **Facets** — `api_facets` (`fable/serve.py:119-146`): add `tags` via `SELECT family,value,COUNT(*) FROM thread_tags GROUP BY family,value ORDER BY 3 DESC LIMIT N` (project-scoped with the existing `scope_sql`).
- **Filter** — `recall.search` (`fable/recall.py:257-264`): mirror the operative/target subquery → `AND prompt_id IN (SELECT prompt_id FROM thread_tags WHERE family=? AND value=?)`.
- **Result row** — `fable/recall.py:306-316`: add `tags` (small `SELECT family,value FROM thread_tags WHERE prompt_id=?`); render as badges in `hitHtml` (`fable/dashboard.html:1062`).
- **Dashboard control** — add `#f-tag` beside `#f-model`/`#f-project` (`fable/dashboard.html:433`), wire into `searchParams()` + the change-listener loop.
- **Phase 2:** tag-boosted ranking (infer query tags, boost same-tag hits in the FTS score).

---

## 4. Graph (additive — the knowledge graph)

`api_graph` (`fable/serve.py:333-352` builds topic nodes from `cards.topics`). Add an identical block right after for tags: group `(family,value)` over in-scope `pids` from `thread_tags`, keep df≥2, emit `group="tag"` nodes `id=tag:<family>:<value>`, edges `kind:"tag"`. Two threads sharing a tag co-occur through the shared node — same mechanic as topics. Register `"tag"` in `GCOLOR` + the toggle bar (`fable/dashboard.html:1234,1240`). Semantic/citation/pruning code below it absorbs the new edges automatically.

---

## 5. Analytics — what fable can derive (the prize)

Folded into the **STATS** lens (`loadDashboard`, `fable/dashboard.html:1999`) as extra `genwrap` panels, plus new fields in `api_costs`/`api_dashboard`. The join spine: `records.prompt_id → thread_tags.prompt_id` (cost/tokens/model per tag), `cards.outcome/type/decisions` (effectiveness), `cards.files`+`terms(kind=target)` (files per tag), `threads.first_ts`+`sessions` (time/project).

> ★ = a fusion memory-mcp's dashboard **could not** do — it had tags but no cost, model, files, projects, or compaction walls. These are fable's edge.

### Tier 1 — rides existing surfaces, ship first
1. **Tag co-occurrence knowledge graph** (GRAPH) — topic/technology/pattern clusters; node size = thread count; outcome ring.
2. **Tag facets on recall** (SEARCH) — filter by any `(family,value)`; "all `decision` threads about `database`."
3. ★ **Value-by-tag** (STATS) — tokens & $ per topic/technology/domain: *"you spent $X / 2.3M tokens on `topic:auth` this month."* `SUM(records tokens) GROUP BY thread_tags.family,value` × `_price()`.
4. **Tag × outcome success heatmap** (STATS) — the reusable X×Y grid: `domain×activity`, `technology×outcome`, `activity×outcome` success rates from `cards.outcome`.

### Tier 2 — derived intelligence
5. ★ **Cost-of-failure** — $ and tokens sunk in `outcome ∈ (failure,blocked,abandoned)` threads, by tag: *"wasted spend lives in `topic:backfill` + `technology:docker`."*
6. ★ **Model × tag ROI matrix** — success rate **and** cost by (model-that-did-the-work × domain/activity), from per-record `model` + `cards.outcome` + tokens: *"opus wins `decision` (85%) but sonnet ties on `implement` at half the cost."* Directly informs the user's own model selection.
7. **Decision log + effectiveness** — `decision` family + `cards.decisions` detail + subsequent outcome (thread order within session) → which decision *types* succeed. Mined, no authoring (memory-mcp `rebuild_decision_log` template).
8. ★ **Topic → file map** — `cards.files` / `terms(kind=target)` × tags → *"`topic:auth` lives in these 6 files."* A semantic file index; overlays onto the graph as file↔tag edges.
9. **Problem → solution rules** — recurring `(failure + topic/error)` → the `pattern` that resolved it in the next thread: *"websocket timeout → retry_with_backoff (5×)."*

### Tier 3 — temporal / behavioral / corpus
10. ★ **Topic lifecycle & trends** — first-seen / peak / decay per tag over `threads.first_ts`: *"`taxonomy` spiking, `backfill` cooling."* Sparkline per tag.
11. ★ **Thrash / loop detection** — same `(topic + failure)` recurring across many sessions before a success: *"you fought `backfill` across 8 sessions."*
12. **Work-rhythm** — activity mix by hour/day (when you `plan` vs `debug`).
13. ★ **Project fingerprints & transfer** — each project's tag signature; surface *"how I solved `auth` in project X"* while working in Y (fable is multi-project; memory-mcp wasn't).
14. ★ **Compaction correlation** — which tags precede compaction walls (context-blowing topics), from wall markers in the record stream.
15. **Prompt-type intelligence** — derive `prompt_type` from the `intent` family; **correction-rate by domain** = where the agent underperforms *for you*.
16. ★ **Expertise / interest map** — your topic/technology footprint across the whole corpus; a personal skills graph.
17. **Tag curation** — `tag_coverage_pct` (beside `card_coverage_pct`); a triage queue for low-confidence/novel `(family,value)` proposals (CONFIRM/LINK/SKIP), gated by the card grade.

---

## 6. Cost/telemetry hooks (additive — §5 data)
- `api_costs` (`fable/serve.py:452`): add `by_tag` (the `records ⋈ thread_tags` GROUP BY above; note many-to-many double-counts tokens across a thread's tags — label as "value attributed by tag").
- `api_dashboard` (`fable/serve.py:486`): add `tag_coverage_pct` beside `card_coverage_pct`; add `tag_outcome` matrix block.

---

## 7. Externalisation hook (parallel, not the carder)
Put the **reasoning-externalisation** nudge (think-out-loud, `Decision/Rejected`, `Found/Fixed-by`) on a `UserPromptSubmit` hook — terse (2-3 lines), **sentinel-wrapped** so fable's indexer ignores it (inception guard). This enriches the text fable indexes and beats system-prompt decay/compaction-drop. **Keep wikilink/tagging OFF the working agent** (the 4/8,126 data). Does not fire for subagents → another reason tagging stays carder-side.

---

## 8. Phasing
- **P0 — data:** `fable/taxonomy.py` loader + `~/.fable/taxonomy.yaml`; `thread_tags` DDL; carder prompt/parse/store + FTS; tag backfill pass. *(ships the tags)*
- **P1 — rides existing surfaces:** search facets + filter + result badges; graph `tag` co-occurrence nodes. *(immediate visible value)*
- **P2 — STATS analytics:** value-by-tag, tag×outcome heatmap, model×tag ROI, cost-of-failure.
- **P3 — derived intelligence:** decision log, topic→file map, problem→solution, trends, thrash, project transfer.
- **P4 — curation & polish:** tag triage + coverage, taxonomy editor, tag-boosted ranking, UserPromptSubmit externalisation hook.

## 9. Exact edit points (from the integration map)
1. `fable/db.py:187` — `thread_tags` DDL.
2. `fable/taxonomy.py` — new loader (+ `taxonomy.example.yaml`, private `~/.fable/taxonomy.yaml`).
3. `fable/cards.py:20-44` — `tags` field + taxonomy block in `PROMPT`.
4. `fable/cards.py:67-84` — parse/validate `tags`.
5. `fable/cards.py:87-106` — write `thread_tags` + FTS content append.
6. `fable/cards.py:260-445` — tag-only backfill branch in `run_cards`.
7. `fable/serve.py:119-146` — tag facets in `api_facets`.
8. `fable/recall.py:257-264,306-316` — tag filter + tags in result row.
9. `fable/serve.py:333-352` — tag co-occurrence in `api_graph`; `fable/dashboard.html:1234,1240` — register `tag` group.
10. `fable/serve.py:452-483,486-611` — `by_tag` cost + `tag_coverage_pct`/`tag_outcome`; `fable/dashboard.html:1999+` — STATS panels.
11. `fable/dashboard.html:421-436,1026-1052` — `#f-tag` search control.
