# fable roadmap

Direction, not promises. Open a Discussion to influence priority.

## Now
- Card backfill quality + multi-provider hardening
- Windows pass (#2)

## Next
- Whole-session HTML archive export (#3)
- Daily cost time-series JSON (#4)
- `examples/` — real workflow walkthroughs (recall, compose, surgery)
- Official MCP Registry listing
- File time-travel: SHIPPED 2026-06-13 — bidirectional (backward derivation from snapshots)\n- Heredoc mining: treat `cat > f <<EOF` Bash inputs as Write-anchors (recovers sed/script-heavy stretches) (`fable file <path>`, Files tab)
- Post-compaction healing v2: inject the diff of what compaction dropped

## Later
- Memory graph: clustering + time-travel view
- Multi-machine vault sync (local-first, no cloud dependency)
- Pluggable transcript formats (Cursor, Copilot CLI, opencode)

## Non-goals
- Cloud sync as default, telemetry of any kind, pip dependencies in core,
  rewriting source transcripts (views are projected, never stored).
