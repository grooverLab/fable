# fable roadmap

Direction, not promises. Open a Discussion to influence priority.

## Now
- Memory graph v2: file/topic/semantic edges, type filters, click-through
  (the wikilink-style graph, built from signals that actually exist)
- Card backfill quality + multi-provider hardening
- Windows pass (#2)

## Next
- Whole-session HTML archive export (#3)
- `examples/` — real workflow walkthroughs (recall, compose, surgery)
- Official MCP Registry listing
- Heredoc mining: treat `cat > f <<EOF` Bash inputs as Write-anchors
  (recovers historical sed/script-heavy stretches)
- Post-compaction healing v2: inject the diff of what compaction dropped
- Vault-copy Claude Code's rewind backups (their retention is undocumented)

## Later
- Memory graph: clustering + time-travel view
- Multi-machine vault sync (local-first, no cloud dependency)
- Pluggable transcript formats (Cursor, Copilot CLI, opencode)

## Shipped
- 2026-06-13 · Daily cost CLI (`fable costs --json --daily`) — first
  community contribution, thanks @JeanBiza (#6)
- 2026-06-13 · File time-travel (`fable file <path>`, Files tab):
  bidirectional reconstruction, rewind-checkpoint anchors, post-Bash
  checkpoint hook, IDE-edit sweep
- 2026-06-12 · Compose (topic workspaces), surgery, prune, diff viewer,
  MCP server, PreCompact auto-archive, compaction healing, dashboard

## Non-goals
- Cloud sync as default, telemetry of any kind, pip dependencies in core,
  rewriting source transcripts (views are projected, never stored).
