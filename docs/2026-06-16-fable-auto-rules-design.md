# Design Spec — fable Auto-Rules

- **Date:** 2026-06-16
- **Status:** Approved design, ready for implementation planning
- **Target project:** fable Recall (to be implemented by the fable agent — this file is a handoff artifact, intentionally placed outside the fable repo)
- **Provenance:** Brainstormed interactively; each decision below records the rejected alternative.

---

## 1. One-liner

Mine the directives a user *already repeats* to their agents across transcripts, cluster them, approve them with one tap in the dashboard, store them as typed/bucketed rules, and inject the right bucket at the right moment — so a rule stated three times becomes a standing rule **without the user ever writing `fable remember`**.

Built entirely on machinery fable already has: the transcript index, the intent/activity tagger, the dashboard triage, the budgeted context-pack, and the lifecycle hooks.

---

## 2. Locked decisions (and what was rejected)

| # | Decision | Rejected alternative & why |
|---|----------|----------------------------|
| D1 | **Signal = explicit repeated directives** the user already stated. | *Inferring rules from correction patterns* — an order of magnitude harder and hostage to model interpretation. Out of v1. |
| D2 | **Gate = detect → one-tap approve**, curated from the dashboard (reusing the existing tag-triage UX, adding a parallel rule-approval triage). | *Fully auto-activate* (risk: mis-clustered/context-specific rules silently steer the agent). *Passive-only suggestions* (nothing happens unless the user goes looking). |
| D3 | **Enforcement = hybrid.** Deterministic hooks for the unambiguous buckets; intent-classification (reusing fable's existing tagger) for the fuzzy ones. | *Pure lifecycle routing* (can't reach non-tool buckets like research/marketing). *Pure classification* (pays classifier risk even where a deterministic hook exists). |
| D4 | **Taxonomy = fixed core + emergent rest.** Small fixed set wired to mechanisms (`thinking`, `code`); everything else emergent + curated. | *Fully fixed list* (hand-maintained, can't absorb new work). *Fully emergent* (loses clean hook-binding; fragments into near-duplicate buckets). |
| D5 | **Scope** per rule = `global \| project`; auto-suggested `global` when the directive recurs across ≥2 projects, else `project`. | Single global scope only (would misapply project-specific conventions everywhere). |
| D6 | **Promotion threshold** = candidate after recurring in ~3 occurrences across ≥2 sessions. | Lower thresholds (noise); manual-only (defeats the purpose). |
| D7 | **Clustering backend = FTS5-first hybrid with optional embedding merge, graceful degradation.** | FTS5-only (under-clusters paraphrase). Embeddings-required (forces an Ollama daemon dependency, breaks the zero-daemon promise). |

---

## 3. Data model (two new tables)

**`rule`**
- `id`
- `canonical_text` — normalized directive
- `bucket_id` → `bucket`
- `scope` — `global | project`
- `project` — nullable (set when `scope = project`)
- `status` — `candidate | active | muted | rejected`
- `occurrence_count`
- `evidence` — list of `prompt_id` / turn refs where it appeared
- `first_seen`, `last_seen`
- `confidence`
- `approved_at`

**`bucket`**
- `id`
- `key` — e.g. `thinking`, `code`, `research.competitive`, `research.marketing`, `problem_solving`
- `kind` — `core | emergent`
- `enforcement` — hook event(s) and/or intent-tag match expression
- display metadata

`evidence` stores real `prompt_id`s → every rule links back to the actual threads via `fable_thread`. Provenance for free.

---

## 4. Extraction — the "detect" half

Runs as a background pass over the archive (like `fable cards run`), then incrementally on new sessions via the capture hooks already firing.

1. **Directive mining** — over **user turns only**, identify imperative/normative statements (`always X`, `never Y`, `use X not Z`, `prefer A`, `don't B`). Reuse the local NER/tagger plus a lightweight directive classifier. Emit normalized canonical forms.
2. **Cluster & dedup** — two stages, graceful degradation (D7):
   - **Stage 1 (mandatory, zero-dep):** normalize to canonical form (lowercase, lemmatize, expand contractions, strip filler) + lexical/fuzzy match. Handles the easy dedup deterministically.
   - **Stage 2 (optional):** if embeddings are available, merge clusters whose centroids are within a cosine threshold — catches paraphrase. If Ollama is off, skipped; residual duplicates are merged by hand in the dashboard.
3. **Promote to candidate** — cluster hits ≥3 occurrences across ≥2 sessions → `candidate` (D6). Scope auto-suggested `global` if ≥2 distinct projects, else `project` (D5).
4. **Bucket-assign** — try the fixed core first (is it about code? thinking?); else route via the intent/activity/domain classifier to an emergent bucket; mint a new emergent bucket if nothing fits well (curation can merge later).

---

## 5. Curation — the "approve" half (dashboard triage)

A rule-triage view beside the existing tag triage. Each candidate shows:
- canonical text
- bucket (editable)
- scope (editable)
- evidence: "seen 6× across 3 projects" → links to the actual threads
- suggested enforcement

One-tap actions: **Approve** (→ `active`) · **Edit** (text / bucket / scope) · **Reject** (→ never resurface this cluster) · **Mute** (keep, don't inject).

Bucket grooming: merge / rename emergent buckets; bind an emergent bucket to a hook if it earns a deterministic trigger.

---

## 6. Enforcement — the "apply" half (hybrid, D3)

**Deterministic core**, via hooks fable already owns:
- `thinking` → `UserPromptSubmit` injects active thinking-rules every turn (same mechanism as the `<fable-externalize>` block already running on the author's machine).
- `code` → `PreToolUse` on `Edit | Write | MultiEdit` injects code-rules at the moment of editing.

**Classifier-routed fuzzy buckets** (research, marketing, problem-solving, …):
- On each prompt, classify the current intent/activity (existing `intent:` / `activity:` tagger) and inject the matching buckets' active rules.

**Budget & precedence:**
- Rule section is budget-capped (reuse the budgeted context-pack machinery).
- Conflict order: `project` > `global`; specific bucket > general; recently-approved/edited wins; cap N rules per bucket.
- Rendered as a compact `<rules bucket="…">` block.

---

## 7. Observability & kill switches

- Per-bucket and global mute.
- Because capture hooks log everything, the dashboard can show **which rules actually fired in which sessions** → spot dead rules and prune them.

---

## 8. Open implementation questions (for the fable agent)

1. **`PreToolUse` `additionalContext` support** — confirm Claude Code honors injected context on `PreToolUse`. If not, fall back to a `UserPromptSubmit` preface when the turn looks code-bound.
2. **Embeddings status** — does the current `fable discover` / index already compute & store embeddings (Ollama enabled)? Determines whether Stage-2 clustering is free now or ships behind the optional flag (see §4.2, D7).

---

## 9. Out of scope (YAGNI — explicitly *not* in v1)

- Signal B (inferring rules from correction patterns).
- Deterministic core beyond `thinking` + `code` — everything else stays emergent.
- Auto-activation without approval.
- **Deferred, noted as the natural v2:** a feedback loop where fable notices the user has *stopped restating* a rule (it's working) or keeps *overriding* it (revise/mute).

---

## 10. Testing

- **Unit:** directive-classifier precision/recall on a labeled sample of user turns; dedup correctness; threshold + scope-inference logic.
- **Integration:** run on a real archive (the author's 3,001-thread corpus) — are the surfaced candidates sane? (dogfood).
- **Injection:** correct bucket fires for a simulated activity; budget cap respected; mute works.
- **Key quality metric — false-candidate rate.** The entire value proposition is "don't fill the rule section with garbage." Measure it directly.
