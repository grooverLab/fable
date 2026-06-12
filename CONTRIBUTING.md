# Contributing to fable

Thanks for caring about transcript memory. Ground rules — they're short.

## Principles (PRs that violate these will be asked to change)

1. **Local-first, zero-dependency core.** The `fable/` package is Python
   stdlib only. No pip dependencies in the core — ever. Optional
   integrations (embeddings, providers) must degrade gracefully when
   their backend is absent.
2. **Original transcripts are sacred.** Only two code paths may modify a
   live transcript (`prune`, `surgery apply`) and both must keep the
   protocol: vault backup → index the backup → immutable-copy gate →
   atomic temp+fsync+rename. Everything else opens files read-only.
3. **Tolerant parsing.** Unknown record types are indexed at full
   fidelity and rendered generically — never dropped, never fatal.
   Anthropic changes the JSONL schema often; crashing on a new message
   type is a regression.
4. **Frozen JSON fields.** Names in `--json` / API output are additive
   only. Renames break scripts and will be reverted.

## Workflow

```bash
python3 -m unittest discover -s tests   # must be green before and after
```

- Tests use synthetic fixtures only (`tests/helpers.py`) — never commit
  real transcripts, and never write outside temp dirs in tests.
- One logical change per PR. Include a test that fails without your fix.
- Run `python3 scripts/benchmark.py` if you touch ranking/search and
  paste before/after numbers in the PR description.

## Reporting bugs

Use the issue templates. For indexing bugs, attach the output of
`fable stats --json` and (if possible) a minimal synthetic JSONL that
reproduces — not your real transcript.

## Security / privacy

fable is local-only by design. If you find any code path that sends
transcript content anywhere except the user-configured card/embedding
provider, that's a critical bug — report it privately first.
