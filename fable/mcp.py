"""fable mcp — Model Context Protocol server (stdio, stdlib only).

Exposes the recall engine as native tools to any MCP client (Claude Code,
or any agent framework):
  fable_search   — ranked threads for a query (+facets/filters)
  fable_thread   — budgeted high-fidelity render of one thread
  fable_block    — one record, byte-identical
  fable_context  — auto-assembled multi-thread context pack

Register in Claude Code:
  claude mcp add fable -- python3 -m fable mcp --db /path/to/fable.db
"""
import json
import sys

TOOLS = [
    {
        "name": "fable_search",
        "description": (
            "RECALL EARLIER CONVERSATION. Use this WHENEVER you need context "
            "that may be outside your window — after a compaction, deep in a "
            "long session, or to recall a past decision / discussion across "
            "ANY session. PREFER IT over guessing or trusting a compaction "
            "summary (the summary is lossy; this is the exact archive). "
            "Returns ranked threads with ids, turn/token counts, card titles, "
            "decisions and outcomes; then call fable_thread to read one verbatim."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "operative": {"type": "string", "description":
                              "action-verb facet, e.g. decide, fix"},
                "target": {"type": "string", "description":
                           "file/crate/identifier facet"},
                "project": {"type": "string"},
                "kind": {"type": "string", "enum": ["main", "subagent"]},
                "sort": {"type": "string",
                         "enum": ["relevance", "turns", "tokens", "recent"]},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fable_thread",
        "description": (
            "Read one conversation thread VERBATIM (user → assistant → tool "
            "turns, in order) under a token budget — the exact past turns, not "
            "a paraphrase. Use after fable_search (or with a known prompt_id) "
            "to recover precise detail a summary would have lost. Bulky tool "
            "results are elided with block pointers (fetch via fable_block)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt_id": {"type": "string"},
                "budget": {"type": "integer", "default": 8000},
                "raw": {"type": "boolean", "default": False},
            },
            "required": ["prompt_id"],
        },
    },
    {
        "name": "fable_block",
        "description": ("One transcript record by uuid, byte-identical — the "
                        "exact original bytes. Use to recover a specific tool "
                        "result or turn that a summary or thread view elided."),
        "inputSchema": {
            "type": "object",
            "properties": {"uuid": {"type": "string"}},
            "required": ["uuid"],
        },
    },
    {
        "name": "fable_prune",
        "description": ("Slim a session transcript NOW (tool noise, "
                        "images, bloat) with a vault backup sealed first — "
                        "nothing is lost, everything stays recallable. Use "
                        "when the user asks to prune/slim a session or "
                        "complains about context size. After pruning the "
                        "CURRENT session, tell the user to /exit and run "
                        "the returned resume command to load the slim "
                        "version."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description":
                               "session to prune (the current session's id "
                               "works — the rewrite is atomic and "
                               "append-safe)"},
                "strip_images": {"type": "boolean", "default": True},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "fable_remember",
        "description": ("Store a durable fact the user wants remembered "
                        "across all future sessions (auto-injected at "
                        "session start). Use when the user says 'remember "
                        "that...' or states a lasting preference/decision."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string"},
                "project": {"type": "string", "description":
                            "scope to a project (omit for global)"},
            },
            "required": ["fact"],
        },
    },
    {
        "name": "fable_context",
        "description": (
            "Auto-assemble a paste-ready context pack for a task: searches "
            "the archive, picks the strongest threads, splits the budget "
            "across them. Returns one sentinel-wrapped block."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "budget": {"type": "integer", "default": 12000},
                "max_threads": {"type": "integer", "default": 5},
                "project": {"type": "string"},
            },
            "required": ["query"],
        },
    },
]


def _call_tool(db_path, name, args):
    if name == "fable_search":
        from fable.recall import search
        hits = search(db_path, args["query"],
                      operative=args.get("operative"),
                      target=args.get("target"),
                      project=args.get("project"),
                      kind=args.get("kind"),
                      sort=args.get("sort", "relevance"),
                      limit=int(args.get("limit", 10)))
        return json.dumps(hits, indent=1)
    if name == "fable_thread":
        from fable.recall import render_thread
        return render_thread(db_path, args["prompt_id"],
                             budget=int(args.get("budget", 8000)),
                             raw=bool(args.get("raw", False)))
    if name == "fable_block":
        from fable.recall import get_block
        return get_block(db_path, args["uuid"])
    if name == "fable_prune":
        import json as _json
        import os as _os
        from pathlib import Path as _P
        from fable import db as _fdb
        from fable.discover import project_label
        from fable.paths import vault_dir
        from fable.prune import prune_file
        sid = args["session_id"]
        conn = _fdb.connect(db_path)
        row = conn.execute(
            "SELECT live_path FROM sessions WHERE session_id LIKE ?",
            (sid + "%",)).fetchone()
        conn.close()
        if not row or not row[0] or not _os.path.exists(row[0]):
            raise KeyError(f"no live transcript known for session {sid}")
        live = row[0]
        before = _os.path.getsize(live)
        project = project_label(_os.path.basename(_os.path.dirname(live)))
        root = vault_dir()
        report = prune_file(live, "resume",
                            backup_dir=_P(root) / project, replace=True,
                            strip_images=bool(args.get("strip_images", True)),
                            db_path=db_path, force=True)
        return _json.dumps({
            "before_bytes": before, "after_bytes": _os.path.getsize(live),
            "backup": report.get("backup"),
            "chain_valid": report.get("chain_valid"),
            "resume": f"claude --resume {row[0].rsplit('/', 1)[-1][:-6]}",
            "note": "if this is the CURRENT session: /exit first, then run "
                    "the resume command to load the slim transcript"})
    if name == "fable_remember":
        from fable.facts import add_fact
        fid = add_fact(db_path, args["fact"], project=args.get("project"))
        return f"remembered (#{fid}) — will be injected into future sessions"
    if name == "fable_context":
        from fable.contextpack import build_context
        return build_context(db_path, args["query"],
                             budget=int(args.get("budget", 12000)),
                             max_threads=int(args.get("max_threads", 5)),
                             project=args.get("project"))
    raise KeyError(f"unknown tool: {name}")


def handle(db_path, msg):
    method = msg.get("method", "")
    msg_id = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": msg.get("params", {}).get(
                "protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fable", "version": "0.1.0"},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params", {})
        try:
            text = _call_tool(db_path, params.get("name"),
                              params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}],
                      "isError": False}
        except Exception as e:  # tool errors go back in-band, never crash
            result = {"content": [{"type": "text",
                                   "text": f"error: {e}"}],
                      "isError": True}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if msg_id is not None:  # unknown request (not a notification)
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601,
                          "message": f"method not found: {method}"}}
    return None  # notification — no reply


def serve_stdio(db_path):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(db_path, msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


def cmd_mcp(args):
    from fable import db as fdb
    fdb.connect(args.db).close()  # fail fast if no index
    return serve_stdio(args.db)
