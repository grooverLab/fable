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
            "Search the indexed Claude Code transcript archive (all "
            "projects/sessions). Returns ranked conversation threads with "
            "thread ids, turn counts, token estimates, card titles, "
            "decisions and outcomes. Use fable_thread to retrieve one."),
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
            "Retrieve one conversation thread (user → assistant → tool "
            "turns, in order, verbatim text) under a token budget. Bulky "
            "tool results are elided with block pointers."),
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
        "description": "One transcript record by uuid, byte-identical.",
        "inputSchema": {
            "type": "object",
            "properties": {"uuid": {"type": "string"}},
            "required": ["uuid"],
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
