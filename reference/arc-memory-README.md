# Arc Memory Engine: Architectural Specification

## 1. System Objective
The **Arc Memory Engine** is a standalone, deterministic, and lossless memory system designed to power any agentic workflow—from personal assistants and research analysts to coding swarms and marketing managers.

It solves the problem of context window exhaustion and "summarization rot" by introducing **Streaming Collective Memory**. It preserves the exact, verbatim history of an agent's reasoning and execution, bounded into logical "**[[Arcs]]**," and indexed via a deterministic, multi-dimensional **[[Map]]**.

**Core Goals:**
*   **Zero Information Loss:** Preserve every detail, dead-end, and decision exactly as it occurred.
*   **Infinite Context Scaling:** Allow a project or persona to exist indefinitely without the active context window ever exceeding a lean baseline.
*   **Grounded Retrieval:** Provide the entire narrative arc when an agent needs memory, rather than isolated, hallucinated fragments.
*   **Zero-Install Infrastructure:** Operate entirely on the filesystem using paginated XML and JSON indexing, requiring no external database services (e.g., MongoDB, Redis).

---

## 2. The Deterministic Protocol: Explicit `[[Wikilinks]]`

The foundational principle of the Arc Memory Engine is **Explicit Edges**. Instead of relying on background LLMs to "guess" or "summarize" what a conversation was about (which is costly and brittle), the system forces the active agent to build the knowledge graph natively as it works.

### 2.1 The System Prompt (The Universal Law)
Every agent operating within the engine's scope is bound by a strict system prompt instruction:

> *"When reasoning in your `<thought>` blocks or providing final text responses, you MUST wrap core components, subjects, locations, and architectural concepts in double brackets (e.g., `[[Q3_Marketing_Strategy]]`, `[[src/auth.py]]`, `[[Competitor_Analysis]]`). Furthermore, you MUST use specific bracketed operatives to describe your actions (e.g., `[[draft]]`, `[[investigate]]`, `[[refactor]]`, `[[analyze]]`)."*

### 2.2 Universal Tagging Vocabulary & Guidelines
The `[[Knowledge_Graph]]` is domain-agnostic. The tagging vocabulary is built on core principles designed to fit any arbitrary use case—from software engineering and marketing management to personal assistants and research analysts. The categories below define the *structure* of the vocabulary, with examples provided merely as illustrative subsets of infinite possibilities.

1.  **`[[Operatives]]` (Actions):** Verbs defining the explicit *intent* or operation of the turn.
    *   *Principle:* Operatives must be normalized, standardized verbs representing state changes or cognitive actions.
    *   *Illustrative Examples:* `[[implement]]`, `[[refactor]]`, `[[analyze]]`, `[[draft]]`, `[[investigate]]`, `[[plan]]`, `[[book]]`.
2.  **`[[Targets]]` (Locations/Artifacts):** The literal, bounded object or destination being acted upon.
    *   *Principle:* Targets should be exact, addressable nouns. Avoid ambiguous pronouns.
    *   *Illustrative Examples:* `[[src/api/router.ts]]` (Coding), `[[Q3_Financial_Report.pdf]]` (Research), `[[Flight_AA123]]` (Assistant).
3.  **`[[Concepts]]` (The "Why" / Entities):** The abstract ideas, business rules, constraints, or error types driving the action.
    *   *Principle:* Concepts provide the relational context and the underlying logic connecting operatives to targets.
    *   *Illustrative Examples:* `[[Deadlock_Error]]` (Coding), `[[Brand_Voice]]` (Marketing), `[[Dietary_Restriction_Vegan]]` (Assistant).

**Formatting Guidelines:**
*   **Format:** Target names should be literal paths or distinct nouns. Concepts must use `Snake_Case` or `PascalCase` (e.g., `[[Rate_Limit_Policy]]`) to ensure clean regex extraction and prevent multi-word fragmentation during O(1) lookups.
*   **Consistency:** The system (via the `[[Local_Wikifier]]` and system prompts) enforces a consistent vocabulary to prevent graph fragmentation (e.g., merging `[[fixing]]` and `[[resolved]]` into a unified `[[fix]]` operative).

### 2.3 Semantic Consistency (The Alias Map)
To prevent graph fragmentation from minor variations in terminology (e.g., `[[option_chain]]`, `[[OptionChain]]`, `[[optionChains]]`), the engine utilizes a canonical entity registry.
1.  **The Alias Map:** A lightweight JSON structure (`alias_map.json`) mapping synonyms and variants to a single canonical `[[Concept]]` or `[[Target]]`.
2.  **Resolution:** During both the *Write Path* (extraction) and the *Read Path* (interception), any extracted `[[wikilink]]` is passed through the Alias Map. If a variant is detected, it is silently normalized to the canonical UUID/string before interacting with the `[[Frontmatter_Map]]`.
3.  **Governance:** The `[[Local_Wikifier]]` is trained exclusively on the canonical terms to minimize the necessity of alias resolution.

### 2.4 User Persona & Psychographic Tagging (Developer-Centric Context)
To enable the agent to tailor its responses, tone, and architectural suggestions to the specific user it is interacting with, the engine introduces a specialized tagging dimension for human users.
1.  **`[[User_Persona]]` Tags:** These are persistent tags associated with a specific user profile (e.g., `[[Anoop]]`). They define working style, temperament, architectural preferences, and even psychographic traits.
    *   *Illustrative Examples:* `[[Preference_Concise]]`, `[[Architecture_EventDriven]]`, `[[Risk_Profile_Conservative]]`, `[[Tone_Analytical]]`.
2.  **Continuous Profiling:** As the user interacts with the system (e.g., rejecting a monolithic architecture in favor of microservices, or asking for shorter explanations), the `[[Local_Wikifier]]` or a background evaluation loop dynamically appends these new trait tags to the user's root node in the `[[Knowledge_Graph]]`.
3.  **Contextual Tailoring:** Before generating a response, the active agent pulls the relevant `[[User_Persona]]` tags from the Map and injects them into the system prompt. The model then inherently adjusts its output to match the developer's exact working style and psychographic profile, creating a truly personalized AGI experience.

---

## 3. The Data Model: The Vault and The Map

The system is split into two distinct, file-based structures: The **[[Vault]]** (Heavy Storage) and The **[[Map]]** (Lightweight Index).

### 3.1 The Vault (Paginated XML)
This is an append-only store of verbatim conversation history. To support high concurrency and fast `[[grep]]`/regex parsing without loading massive files into RAM, the Vault is paginated by size or chronological boundaries (e.g., `arc_2026_04.xml`).

```xml
<?xml version="1.0" encoding="UTF-8"?>
<vault period="2026_04" status="active">
  <!-- Arc Types: concept | decision | workflow | insight -->
  <arc id="arc-991" type="decision" timestamp="2026-04-08T10:30:00Z" source_session="session-123">
    <meta>
      <temporal>
        <created_at>2026-04-08T10:30:00Z</created_at>
        <last_accessed>2026-04-08T10:30:00Z</last_accessed>
        <importance_score>1.0</importance_score>
      </temporal>
      <operatives>
        <op>[[investigate]]</op>
      </operatives>
      <targets>
        <target>[[Q3_Marketing_Strategy]]</target>
      </targets>
      <concepts>
        <concept>[[Brand_Voice]]</concept>
        <concept>[[Target_Demographic_A]]</concept>
      </concepts>
      <consulted_arcs>
        <arc_ref>arc-412</arc_ref>
        <arc_ref>arc-805</arc_ref>
      </consulted_arcs>
    </meta>
    <transcript format="jsonl">
      <![CDATA[
{"role": "user", "content": "Review the Q3 strategy against our new demographic."}
{"role": "assistant", "content": "<thought>I will [[investigate]] the [[Q3_Marketing_Strategy]] to ensure it aligns with the [[Brand_Voice]] for [[Target_Demographic_A]].</thought>"}
{"role": "tool_call", "name": "read_document", "input": {"doc": "Q3_Strategy.pdf"}}
      ]]>
    </transcript>
  </arc>
</vault>
```

**Key Features:**
*   **CDATA Safety:** Raw JSONL transcripts are wrapped in `<![CDATA[ ... ]]>`.
*   **Typed Arcs:** `<arc type="...">` categorizes memory (e.g., decision vs. workflow) to aid retrieval prioritization.
*   **Temporal Metadata:** Enables Recency Weighting, scoring decay, and usage tracking for future context ranking.
*   **Citation Graph (`<consulted_arcs>`):** Tracks which previous arcs were injected into this context, forming a traceable lineage of thought while preventing recursive memory bloat.

### 3.2 The Dual Index System
To achieve true O(1) performance and avoid single-file bottlenecks, the index is split into two layers:

1.  **The Semantic Map (`memory_map.json`):** Maps entities (Tags/Files) to a list of Arc UUIDs.
2.  **The Physical Index (`arc_index.json`):** Maps a specific Arc UUID to its exact physical location (Vault File + Byte Offset).

```json
// arc_index.json
{
  "arc-991": {
    "file": "arc_2026_04.xml",
    "offset": 10245,
    "score": 1.2
  }
}
```

---

## 4. The 4-Phase Lifecycle

### Phase 1: Accumulation & Context Pruning (The Citation Graph)
1.  **Buffer:** As an agent operates, the turns (User Prompt -> Thought -> Tool -> Outcome) are held in an in-memory buffer.
2.  **Context Purging:** To avoid **Memory Inception** (recursive bloat where injected memories are saved inside new memories), any `<historical_context>` blocks injected during the read path are strictly regex-stripped from the turn buffer before storage.
3.  **Citation Graphing:** The UUIDs of the stripped context arcs are preserved and added to the `<consulted_arcs>` metadata stub. This establishes a directed edge showing which historical decisions influenced the current turn without duplicating the raw payload.
4.  **Threshold Trigger:** Once a logical boundary is reached, the purged buffer is frozen.

### Phase 2: Deterministic Extraction (The Parser)
1.  **Extract Entities:** Runs a regex `\[\[(.*?)\]\]` over the blocks to extract explicit tags.
2.  **Alias Resolution:** Normalizes extracted tags against `alias_map.json`.
3.  **Extract Outcomes:** Scans tool results for success/failure flags.

### Phase 3: Storage & Indexing
1.  **Vault Append:** The system wraps the buffer in the XML `<arc>` format, assigns a `type`, initializes the `<temporal>` scoring, includes `<consulted_arcs>`, and appends it to the active vault page.
2.  **Index Update:** The system updates `arc_index.json` with the new byte offset and `memory_map.json` with the semantic links.

### Phase 4: Context Injection & Ranking (Progressive Disclosure)
This is how the active agent leverages the memory deterministically.
1.  **Intercept:** The engine scans incoming text for known `[[Targets]]` or `[[Concepts]]`.
2.  **Resolution:** The system intersects the `memory_map.json` to find matching UUIDs.
3.  **Ranking (Critical Step):** If multiple Arcs match, the system sorts them by calculating a combined score using the `<temporal>` metadata (Recency + `importance_score`) and the Arc `type` priority (e.g., `decision` > `insight`).
4.  **Injection:** The engine uses `arc_index.json` to stream the top-ranked `<arc>` blocks from the Vault and silently prepends them inside a strict `<historical_context>` tag.
5.  **Feedback Loop:** Once an Arc is successfully utilized in a turn (i.e., the agent didn't revert the change), a background process increments its `importance_score` in the Vault/Index.

---

## 5. Pre-Processing & Legacy Integration

### 5.1 User Prompt Pre-Processing (The Local Wikifier)
While the active agent is forced by its system prompt to generate `[[wikilinks]]`, human users write in plain text. To bridge this gap:
1.  **Local Inference:** A highly specialized, extremely small, locally hosted model (e.g., a fine-tuned quantized Llama 3 8B, Phi-3, or a dedicated NER model like GLiNER) intercepts the user's prompt.
2.  **Translation:** It performs Named Entity Recognition (NER) and Intent Mapping based on the project's known vocabulary.
3.  **Output:** *"Review the Q3 strategy against our new demographic."* becomes *"Can you [[investigate]] the [[Q3_Marketing_Strategy]] against [[Target_Demographic_A]]?"*
4.  **Trigger:** This enriched prompt guarantees the Orchestrator hits the `memory_map.json` perfectly before waking the heavy reasoning model.

### 5.2 The Retroactive "Wikifier" (Backfilling History)
To integrate legacy transcripts (e.g., 100+ hour sessions from previous un-tagged systems):
1.  **Batch Processing:** A one-off script feeds historical `.jsonl` transcripts through a fast LLM.
2.  **The Prompt:** The model is instructed to *only* wrap key concepts in double brackets according to the Universal Tagging Vocabulary, without altering the original verbatim text or tool calls.
3.  **Indexing:** The deterministic parser then indexes this newly "wikified" history into the Map, making years of legacy data instantly queryable by the new standalone engine.

---

## Appendix A: The Interim Sandbox Strategy (Retrofitting Active Sessions)

For developers who need to implement Arc Memory concepts on a running, massive session (e.g., a 1.7 Billion token cache session in Claude Code) without breaking the context window, an interim "Sidecar Sandbox" approach is used.

### The Mechanics of the Interim Approach

1. **The Compaction Firewall (Manual Purge)**
   *   The user manually triggers a compaction event (e.g., `/compact` in Claude Code).
   *   This clears the active VRAM context and drops the prompt token count back to a lean baseline, while keeping the 3 Billion token cache thread intact on the provider's backend.
   *   The historical messages transition from "Active VRAM" to "Cold Disk Storage" behind the compaction boundary in the `.jsonl` transcript.

2. **The Retroactive `arc_tagger` (Shadow Building)**
   *   A background Python orchestrator reads the `.jsonl` transcript and chunks it by **Logical Turns** (from `user` prompt to the next `user` prompt).
   *   To prevent choking the local model, the script temporarily strips massive `tool_result` payloads (e.g., 50k-line file reads) from the chunk.
   *   The remaining text (thoughts and prompts) is passed to a local NER model (like GLiNER or Haiku via API). The model is instructed to wrap action verbs, file paths, and domain concepts in `[[wikilinks]]`.
   *   The massive `tool_result` payloads are stitched back in.
   *   The newly `[[wikified]]` JSON objects are appended to a new, parallel file: `tagged_[session_id].jsonl` (The Shadow Transcript).

3. **Just-In-Time (JIT) Context Retrieval**
   *   When historical context is needed, the user runs a local script: `python arc_query.py "[[Vedha_Point]]"`.
   *   The script uses regex to `grep` the `tagged_[session_id].jsonl` Shadow Transcript for Turns containing the intersection of the queried `[[wikilinks]]`.
   *   It pulls the full, raw JSON Turn and wraps it in a strict `<historical_context>` XML block.
   *   The script copies this block to the user's clipboard, allowing them to paste it directly into the active session prompt alongside their new instruction.

4. **Context Pruning (Preventing Memory Inception)**
   *   Once the injected `<historical_context>` block has served its purpose, a pre-processing step in the pruning script (e.g., `prune_transcript_v3.py`) scans the active `.jsonl` file.
   *   It uses regex to find any `content` wrapped in `<historical_context>...</historical_context>` and deletes it entirely to prevent recursive memory bloat.
   *   It leaves behind a tiny citation stub: `<consulted_arcs>[[Vedha_Point]]</consulted_arcs>`.

This interim method allows users to manually prototype Progressive Disclosure, Semantic Blast Radius, and Explicit Wikilinking on production data without requiring a full architectural rebuild of the host AI CLI.
