"""fable CLI — the agent-facing surface.

Designed to be called from a live Claude Code session via Bash:
  fable search "zigzag pivot" --operative decide
  fable thread <promptId> --budget 8000
  fable block <uuid>
"""
import argparse
import glob
import json
import os
import sys

from fable import db as fdb


def _default_db():
    return os.environ.get("FABLE_DB", "fable.db")


def _vault_paths(values):
    paths = []
    for v in values or []:
        if os.path.isdir(v):
            paths.extend(sorted(glob.glob(os.path.join(v, "*.jsonl"))))
        else:
            paths.append(v)
    return paths


def cmd_index(args):
    import datetime
    from fable.discover import session_title
    from fable.extract import fts_extract_fn
    from fable.indexer import index_vault
    from fable.terms import index_terms
    stats = index_vault(args.db, _vault_paths(args.vault), live_file=args.live,
                        extract_fn=fts_extract_fn)
    if not stats.get("session_id"):
        # cached re-index scans nothing; recover the id from meta
        conn = fdb.connect(args.db)
        row = conn.execute(
            "SELECT value FROM meta WHERE key='session_id'").fetchone()
        conn.close()
        if row:
            stats["session_id"] = row[0]
    if stats.get("session_id"):
        conn = fdb.connect(args.db)
        conn.execute(
            "INSERT OR REPLACE INTO sessions(session_id, project, title,"
            " live_path, indexed_at) VALUES(?,?,?,?,?)",
            (stats["session_id"],
             os.path.basename(os.path.dirname(os.path.abspath(args.live)))
             if args.live else "manual",
             session_title(args.live) if args.live else None,
             args.live,
             datetime.datetime.now(datetime.timezone.utc).isoformat()))
        conn.execute(
            "UPDATE records SET session_id = ? WHERE session_id IS NULL",
            (stats["session_id"],))
        conn.commit()
        conn.close()
    tstats = index_terms(args.db)
    out = {**stats, **{f"terms_{k}": v for k, v in tstats.items()}}
    print(json.dumps(out, indent=2))
    return 0


def cmd_discover(args):
    from fable.discover import discover, DEFAULT_PROJECTS_DIR
    stats = discover(args.db,
                     projects_dir=args.projects_dir or DEFAULT_PROJECTS_DIR,
                     backup_roots=args.backups,
                     project_filter=args.project,
                     include_vaults=not args.no_vaults,
                     progress=lambda msg: print(f"  indexing {msg}",
                                                file=sys.stderr))
    print(json.dumps(stats, indent=2))
    return 0


def cmd_watch(args):
    """Zero-maintenance mode: incremental discover on a timer. The indexer
    skips unchanged files, so idle cycles cost ~nothing."""
    import time
    from fable.discover import discover
    print(f"fable watch: every {args.interval}s (Ctrl-C to stop)")
    while True:
        try:
            stats = discover(args.db, project_filter=args.project)
            line = (f"indexed {stats['records_indexed']} new records "
                    f"across {stats['sessions']} sessions")
            if not args.no_embed:
                try:
                    from fable.embeddings import embed_cards, backend
                    if backend():
                        e = embed_cards(args.db)
                        if e["embedded"]:
                            line += f", embedded {e['embedded']} new cards"
                except Exception:
                    pass
            print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)
        except Exception as e:
            print(f"watch cycle failed (will retry): {e}", file=sys.stderr)
        time.sleep(args.interval)


def cmd_context(args):
    from fable.contextpack import build_context
    print(build_context(args.db, " ".join(args.query), budget=args.budget,
                        max_threads=args.max_threads, project=args.project))
    return 0


def cmd_search(args):
    from fable.recall import search
    hits = search(args.db, " ".join(args.query), operative=args.operative,
                  target=args.target, limit=args.limit, sort=args.sort,
                  kind=args.kind, model=args.model, project=args.project)
    if args.json:
        print(json.dumps(hits, indent=2))
        return 0
    if not hits:
        print("no matches")
        return 1
    for h in hits:
        title = h["title"] or ""
        agent = "sub" if h.get("agent") == "subagent" else "main"
        print(f"{h['prompt_id']}  matches={h['matches']:<4} "
              f"score={h['score']:<8} turns={h['turn_count']} "
              f"~{h['est_tokens']}tok [{agent}] "
              f"{(h.get('models') or '?').split(',')[0]}  "
              f"{h['first_ts'] or '?'}  {title}")
    print("\nretrieve: fable thread <prompt_id> [--budget 8000]")
    return 0


def cmd_thread(args):
    from fable.recall import render_thread
    print(render_thread(args.db, args.prompt_id, budget=args.budget,
                        raw=args.raw, sentinel=not args.no_sentinel))
    return 0


def cmd_block(args):
    from fable.recall import get_block
    print(get_block(args.db, args.uuid))
    return 0


def cmd_stats(args):
    conn = fdb.connect(args.db)
    try:
        out = {}
        for label, sql in [
            ("files", "SELECT COUNT(*) FROM files"),
            ("records", "SELECT COUNT(*) FROM records"),
            ("threads", "SELECT COUNT(*) FROM threads"),
            ("fts_rows", "SELECT COUNT(*) FROM fts"),
            ("terms", "SELECT COUNT(*) FROM terms"),
            ("cards", "SELECT COUNT(*) FROM cards"),
            ("citations", "SELECT COUNT(*) FROM citations"),
        ]:
            out[label] = conn.execute(sql).fetchone()[0]
        row = conn.execute(
            "SELECT value FROM meta WHERE key='session_id'").fetchone()
        out["session_id"] = row[0] if row else None
        out["db_bytes"] = os.path.getsize(args.db) if os.path.exists(args.db) else 0
        print(json.dumps(out, indent=2))
        return 0
    finally:
        conn.close()


def cmd_cards(args):
    from fable.cards import cmd_cards as impl
    return impl(args)


def cmd_prune(args):
    from fable.prune import cmd_prune as impl
    return impl(args)

def cmd_costs(args):
    from fable.serve import api_costs, api_dashboard
    if args.daily:
        data = api_dashboard(args.db, {})
        daily = data["daily"]
        if args.json:
            print(json.dumps(daily, indent=2))
            return 0
        if not daily:
            print("no data")
            return 1
        current_day = None
        day_total = 0.0
        for r in daily:
            if r["day"] != current_day:
                if current_day:
                    print(f"  total: ${day_total:.4f}")
                current_day = r["day"]
                day_total = 0.0
                print(f"\n{r['day']}")
            day_total += r["cost"]
            print(f"  {r['model']:<40} ${r['cost']:.4f}")
        if current_day:
            print(f"  total: ${day_total:.4f}")
        return 0

    data = api_costs(args.db, {})
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print(f"Total: ${data['total_usd']:.2f}")
    print("\nBy project:")
    for proj, cost in sorted(data["by_project"].items(), key=lambda x: -x[1]):
        print(f"  {proj:<30} ${cost:.2f}")
    print("\nBy model:")
    for model, cost in sorted(data["by_model"].items(), key=lambda x: -x[1]):
        print(f"  {model:<40} ${cost:.2f}")
    print(f"\n{data['note']}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="fable",
                                description="Transcript vault recall engine")
    p.add_argument("--db", default=_default_db(),
                   help="index database path (env FABLE_DB, default fable.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_file = sub.add_parser(
        "file", help="file time-travel: edit history from the archive")
    p_file.add_argument("path", help="file path (or unique suffix)")
    p_file.add_argument("--show", type=int, default=None,
                        help="print version N's full content")
    p_file.add_argument("--diff", type=int, nargs=2, metavar=("A", "B"),
                        help="unified diff between versions A and B")
    p_file.set_defaults(fn=lambda a: __import__(
        "fable.filetime", fromlist=["cmd_file"]).cmd_file(a))

    sp = sub.add_parser("index", help="index vault generations + live file")
    sp.add_argument("--vault", nargs="*", default=[],
                    help="vault files or directories of *.jsonl generations")
    sp.add_argument("--live", help="live (mutable) transcript")
    sp.set_defaults(fn=cmd_index)

    sp = sub.add_parser("discover",
                        help="find and index all Claude Code projects")
    sp.add_argument("--projects-dir", default=None)
    sp.add_argument("--backups", nargs="*", default=None,
                    help="backup vault roots (default: transcript-pruning)")
    sp.add_argument("--project", help="only projects whose name contains this")
    sp.add_argument("--no-vaults", action="store_true")
    sp.set_defaults(fn=cmd_discover)

    sp = sub.add_parser("search", help="rank threads by relevance")
    sp.add_argument("query", nargs="+")
    sp.add_argument("--operative", help="facet: action verb, e.g. decide")
    sp.add_argument("--target", help="facet: file/crate/identifier")
    sp.add_argument("--sort", default="relevance",
                    choices=["relevance", "turns", "tokens", "recent"])
    sp.add_argument("--kind", choices=["main", "subagent"])
    sp.add_argument("--model", help="filter: model substring")
    sp.add_argument("--project", help="filter: project substring")
    sp.add_argument("-n", "--limit", type=int, default=10)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("context",
                        help="auto-assemble a budgeted context pack")
    sp.add_argument("query", nargs="+")
    sp.add_argument("--budget", type=int, default=12000)
    sp.add_argument("-n", "--max-threads", type=int, default=5)
    sp.add_argument("--project")
    sp.set_defaults(fn=cmd_context)

    sp = sub.add_parser("thread", help="retrieve a thread's raw turns")
    sp.add_argument("prompt_id")
    sp.add_argument("--budget", type=int, default=8000,
                    help="approx token budget for the rendering")
    sp.add_argument("--raw", action="store_true",
                    help="verbatim JSONL records instead of rendering")
    sp.add_argument("--no-sentinel", action="store_true")
    sp.set_defaults(fn=cmd_thread)

    sp = sub.add_parser("block", help="one record, byte-identical")
    sp.add_argument("uuid")
    sp.set_defaults(fn=cmd_block)

    sp = sub.add_parser("stats", help="index statistics")
    sp.set_defaults(fn=cmd_stats)

    sp = sub.add_parser("costs", help="API cost analytics")
    sp.add_argument("--json", action="store_true",
                    help="machine-readable JSON output")
    sp.add_argument("--daily", action="store_true",
                    help="break down costs by day and model")
    sp.set_defaults(fn=cmd_costs)

    sp = sub.add_parser("export", help="export a thread as md/html")
    sp.add_argument("prompt_id")
    sp.add_argument("--format", choices=["md", "html"], default="md")
    sp.add_argument("-o", "--output")
    sp.add_argument("--gist", action="store_true",
                    help="publish as a secret GitHub gist (needs gh CLI)")
    sp.set_defaults(fn=lambda a: __import__(
        "fable.export", fromlist=["cmd_export"]).cmd_export(a))

    sp = sub.add_parser("compose",
                        help="build a NEW resumable session from selected "
                             "threads (any session/project), in your order")
    sp.add_argument("title", nargs="+")
    sp.add_argument("-t", "--threads", action="append", required=True,
                    help="thread (prompt) id — repeat in desired order")
    sp.add_argument("--cwd", help="project dir the workspace belongs to "
                                  "(default: current dir)")
    sp.add_argument("--strip-thinking", action="store_true")
    sp.set_defaults(fn=lambda a: __import__(
        "fable.compose", fromlist=["cmd_compose"]).cmd_compose(a))

    sp = sub.add_parser("vault", help="vault gc: reclaim fully-redundant "
                                      "generations (lossless, to trash)")
    sp.add_argument("action", choices=["gc"])
    sp.add_argument("--apply", action="store_true",
                    help="actually move files (default: dry-run report)")
    sp.add_argument("--trash", help="trash dir (default: <db dir>/vault-trash)")
    sp.set_defaults(fn=lambda a: __import__(
        "fable.vaultgc", fromlist=["cmd_vault"]).cmd_vault(a))

    sp = sub.add_parser("watch", help="auto-index loop: discover (+embed) "
                                      "every N seconds")
    sp.add_argument("--interval", type=int, default=300)
    sp.add_argument("--project", help="limit discovery to one project")
    sp.add_argument("--no-embed", action="store_true")
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("remember", help="store a durable cross-session fact")
    sp.add_argument("fact", nargs="+")
    sp.add_argument("--project")
    sp.set_defaults(fn=lambda a: __import__(
        "fable.facts", fromlist=["cmd_remember"]).cmd_remember(a))

    sp = sub.add_parser("facts", help="list remembered facts")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=lambda a: __import__(
        "fable.facts", fromlist=["cmd_facts"]).cmd_facts(a))

    sp = sub.add_parser("forget", help="deactivate a fact by id")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=lambda a: __import__(
        "fable.facts", fromlist=["cmd_forget"]).cmd_forget(a))

    sp = sub.add_parser("embed", help="embed cards for semantic search "
                                      "(ollama or OPENAI_API_KEY)")
    sp.set_defaults(fn=lambda a: __import__(
        "fable.embeddings", fromlist=["cmd_embed"]).cmd_embed(a))

    sp = sub.add_parser("hook", help="Claude Code hook handler (stdin JSON):"
                                     " seals + indexes the transcript")
    sp.set_defaults(fn=lambda a: __import__(
        "fable.hook", fromlist=["cmd_hook"]).cmd_hook(a))

    sp = sub.add_parser("mcp", help="MCP stdio server (tools for agents)")
    sp.set_defaults(fn=lambda a: __import__(
        "fable.mcp", fromlist=["cmd_mcp"]).cmd_mcp(a))

    sp = sub.add_parser("serve", help="dashboard UI (read-only)")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(fn=lambda a: __import__(
        "fable.serve", fromlist=["cmd_serve"]).cmd_serve(a))

    sp = sub.add_parser("cards", help="LLM thread cards (OpenRouter)")
    csub = sp.add_subparsers(dest="cards_cmd", required=True)
    cr = csub.add_parser("run", help="generate cards for uncarded threads")
    cr.add_argument("--limit", type=int, default=0, help="max threads (0=all)")
    cr.add_argument("--min-tokens", type=int, default=200,
                    help="skip threads smaller than this")
    cr.add_argument("--model", default=None,
                    help="openrouter slug, or haiku/sonnet for other providers")
    cr.add_argument("--provider", default="openrouter",
                    choices=["openrouter", "anthropic", "claude-cli",
                             "ollama"],
                    help="claude-cli = headless `claude -p` (Max plan quota)")
    cr.add_argument("--project", help="only this project's threads")
    cr.add_argument("--dry-run", action="store_true")
    cs = csub.add_parser("show", help="show a thread's card")
    cs.add_argument("prompt_id")
    sp.set_defaults(fn=cmd_cards)

    sp = sub.add_parser("prune", help="prune a live transcript (v2)")
    sp.add_argument("input", help="live transcript path")
    sp.add_argument("--mode", choices=["resume", "extract", "handoff"],
                    required=True)
    sp.add_argument("--backup-dir")
    sp.add_argument("--output", "-o")
    sp.add_argument("--replace", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--strip-images", action="store_true",
                    help="replace base64 image payloads with placeholders")
    sp.add_argument("--force", action="store_true",
                    help="bypass the index-before-evict gate")
    sp.set_defaults(fn=cmd_prune)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (KeyError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:  # includes StaleIndexError
        print(f"error: {e}", file=sys.stderr)
        return 3
