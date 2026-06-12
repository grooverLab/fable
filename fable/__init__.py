"""Fable — high-fidelity context recall for Claude Code JSONL transcripts.

Vault: immutable backup generations + live transcript tail.
Map:   SQLite index (records by uuid -> file/offset/len, FTS5, terms, cards).
recall: page exact raw turns back into a session under a token budget.
prune: evict tool bloat from the live session, never before it is indexed.
"""

__version__ = "0.1.0"
