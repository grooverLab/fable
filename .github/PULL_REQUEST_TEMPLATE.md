**What & why**

**Checklist**
- [ ] `python3 -m unittest discover -s tests` green
- [ ] Core stays stdlib-only (no new pip deps)
- [ ] Transcript writes (if any) go through backup → gate → atomic protocol
- [ ] JSON output fields are additive only
- [ ] Benchmark before/after pasted if ranking/search touched (`scripts/benchmark.py`)
