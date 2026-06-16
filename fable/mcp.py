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
            "decisions and outcomes; then call fable_thread to read one verbatim. "
            "To trace WHEN a file changed, use fable_files → fable_file_history."),
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
                "tag": {"type": "string", "description":
                        "taxonomy filter 'family:value' (e.g. 'topic:auth', "
                        "'decision:architecture', 'technology:rust'); a bare "
                        "value matches any family. DISCOVER valid tags first "
                        "via fable_tags."},
                "sort": {"type": "string",
                         "enum": ["relevance", "turns", "tokens", "recent"]},
                "since": {"type": "string", "description":
                          "only threads active on/after this date or ISO "
                          "timestamp (YYYY-MM-DD or full ISO)"},
                "until": {"type": "string", "description":
                          "only threads active on/before this date or ISO "
                          "timestamp; a bare date is inclusive of that day"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fable_timeline",
        "description": (
            "BROWSE PAST WORK BY TIME — answer 'what was I working on "
            "around <date>' / 'last week' / 'on June 14' with NO search query. "
            "Returns the threads active in a date window, newest-first, each "
            "with project, card title, type, outcome and tags — then open one "
            "verbatim with fable_thread. Use when the user anchors on WHEN "
            "rather than WHAT. Pass since/until as a date (YYYY-MM-DD) or full "
            "ISO timestamp; a bare `until` date includes that whole day."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description":
                          "window start — date or ISO timestamp"},
                "until": {"type": "string", "description":
                          "window end — date (inclusive of the day) or ISO"},
                "project": {"type": "string", "description":
                            "scope to a project (substring match)"},
                "limit": {"type": "integer", "default": 50},
            },
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
            "Use when you're ABOUT TO START a task that needs prior context and "
            "want it assembled ready-to-paste — ONE call instead of fable_search "
            "+ several fable_thread opens. (fable_search ranks threads for YOU to "
            "read and pick; fable_context auto-assembles the pack for you.) "
            "Searches the archive, picks the strongest threads, splits the budget "
            "across them, returns one sentinel-wrapped block."),
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
    {
        "name": "fable_recall",
        "description": (
            "Use DEEP IN A LONG SESSION when the start-of-session fact injection "
            "has scrolled out of context, or to re-check a stored preference / "
            "constraint before assuming. Reads the durable facts the user saved "
            "via /remember — lasting preferences, decisions and constraints. "
            "Optionally scope to a project."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description":
                            "scope to a project (omit for all/global)"},
            },
        },
    },
    {
        "name": "fable_files",
        "description": (
            "List the files Claude has edited — across the whole archive, or "
            "within one session — with edit/write counts and last-touched "
            "time. Use to DISCOVER what a past session changed before pulling "
            "a file's history. Filter by a path substring or a session id."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description":
                          "path substring filter, e.g. serve.py"},
                "session_id": {"type": "string", "description":
                               "limit to one session's files"},
                "limit": {"type": "integer", "default": 40},
            },
        },
    },
    {
        "name": "fable_file_history",
        "description": (
            "EVERY version of a file Claude ever edited, reconstructed from "
            "the transcript — each version's index, timestamp, tool, session "
            "and fidelity (exact replay vs rebuilt-backward). Use to see how a "
            "file evolved, or to find the two version indices to diff. Pass a "
            "file path (or a distinctive substring of it)."),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "fable_file_diff",
        "description": (
            "Unified diff between any two reconstructed versions of a file "
            "(version indices from fable_file_history) — recover exactly what "
            "changed between two past edits, or between a past version and the "
            "latest. Pass the file path and the two version indices a and b."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "a": {"type": "integer", "description": "before version index"},
                "b": {"type": "integer", "description": "after version index"},
            },
            "required": ["path", "a", "b"],
        },
    },
    {
        "name": "fable_tags",
        "description": (
            "DISCOVER the taxonomy tags fable assigns to threads, for precise "
            "tag-filtered recall. Call with NO args to list the tag FAMILIES "
            "(domain, activity, topic, technology, pattern, intent, outcome, "
            "decision…) with counts; call with family='<one>' to list THAT "
            "family's values. Then pass tag='family:value' to fable_search to "
            "scope recall to exactly that kind of work — progressive "
            "disclosure, so you never need the whole taxonomy up front."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "family": {"type": "string", "description":
                           "omit to list families; pass one (e.g. 'topic') "
                           "to list its tag values"},
            },
        },
    },
    {
        "name": "fable_tasks",
        "description": (
            "READ THE USER'S BACKLOG — the durable task ledger mined from every "
            "session's Task tool calls + inline checkbox todos. Use when the user "
            "asks what's pending / open / left to do, to pick the next task, or to "
            "check whether something is already tracked before adding it. Tasks "
            "carry their TRUE work-project (derived from the files a session "
            "touched, not its cwd), so a project filter is meaningful even for "
            "cross-project sessions. Returns a per-work-project summary + the "
            "matching tasks (id, subject, status, source, project, and a "
            "prompt_id for provenance via fable_thread). Read-only."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description":
                            "filter to one work-project (substring match)"},
                "status": {"type": "string",
                           "enum": ["open", "completed", "all"],
                           "description":
                           "default 'open' (raised but never completed)"},
                "source": {"type": "string",
                           "enum": ["task", "inline", "idea", "feature"],
                           "description": "filter by where the task came from"},
                "limit": {"type": "integer", "default": 30},
            },
        },
    },
    {
        "name": "fable_decisions",
        "description": (
            "WHY DID WE DECIDE X? — pull just the decision statements (the "
            "verbatim 'chose X over Y because Z' lines) from past threads "
            "matching a query, WITHOUT opening each thread. Use when the user "
            "asks why something was done a certain way, what approach was "
            "picked, or what was ruled out. Each decision carries its thread "
            "(prompt_id), title, project and date for provenance via "
            "fable_thread."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project": {"type": "string"},
                "since": {"type": "string", "description":
                          "only decisions from threads on/after this date/ISO"},
                "until": {"type": "string", "description":
                          "only decisions from threads on/before this date/ISO"},
                "limit": {"type": "integer", "default": 40, "description":
                          "threads to scan (each may yield several decisions)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fable_overview",
        "description": (
            "ORIENT FIRST — the cold-start map of the whole archive. Call at "
            "the START of a task when you don't yet know which project or "
            "thread holds what you need, instead of guessing with several "
            "searches. Returns, per work-project: thread count, open tasks, "
            "activity span, top technologies + topics, and recent card titles; "
            "plus global totals and date span. Pass `project` to drill into "
            "one."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description":
                            "scope to one work-project (substring match)"},
            },
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
                      tag=args.get("tag"),
                      sort=args.get("sort", "relevance"),
                      since=args.get("since"), until=args.get("until"),
                      limit=int(args.get("limit", 10)))
        return json.dumps(hits, indent=1)
    if name == "fable_timeline":
        from fable.recall import timeline
        return json.dumps(timeline(db_path, since=args.get("since"),
                                    until=args.get("until"),
                                    project=args.get("project"),
                                    limit=int(args.get("limit", 50))), indent=1)
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
        # honor a small/zero budget (0 was falsy → full default pack); floor at
        # 500 so a tiny budget yields a tiny-but-usable pack, never the full 12k
        budget = max(int(args.get("budget", 12000)), 500)
        return build_context(db_path, args["query"], budget=budget,
                             max_threads=int(args.get("max_threads", 5)),
                             project=args.get("project"))
    if name == "fable_recall":
        from fable.facts import list_facts
        return json.dumps(list_facts(db_path, project=args.get("project")),
                          indent=1)
    if name == "fable_files":
        from fable.filetime import known_files, session_files
        sid = args.get("session_id")
        rows = (session_files(db_path, sid) if sid
                else known_files(db_path, args.get("query", ""),
                                 limit=int(args.get("limit", 40))))
        return json.dumps(rows, indent=1)
    if name == "fable_file_history":
        from fable import db as _fdb
        from fable.filetime import file_events, reconstruct
        versions = reconstruct(file_events(db_path, args["path"]))
        # WHY each edit happened — pair every version with the card of the
        # thread that produced it (title/decision/outcome/tags): the SAME
        # what-changed-why correlation the dashboard file-story shows, which the
        # MCP wasn't passing through. fable owns the transcript, so it uniquely
        # knows the rationale; prompt_id stays the drill-down handle.
        pids = tuple({v.get("prompt_id") for v in versions if v.get("prompt_id")})
        why = {}
        if pids:
            conn = _fdb.connect(db_path)
            try:
                qs = ",".join("?" * len(pids))
                for pid, title, typ, outcome, dec in conn.execute(
                        f"SELECT prompt_id, title, type, outcome, decisions "
                        f"FROM cards WHERE prompt_id IN ({qs})", pids):
                    try:
                        decisions = json.loads(dec or "[]")
                    except (ValueError, TypeError):
                        decisions = []
                    tags = ["%s:%s" % (f, val) for f, val in conn.execute(
                        "SELECT family, value FROM thread_tags "
                        "WHERE prompt_id = ? ORDER BY family", (pid,))]
                    why[pid] = {"title": title, "type": typ,
                                "outcome": outcome, "decisions": decisions,
                                "tags": tags}
            finally:
                conn.close()
        return json.dumps([
            {"i": i, "ts": v.get("ts"), "tool": v.get("tool"),
             "ok": v.get("ok"), "derived": bool(v.get("derived")),
             "note": v.get("note"), "bytes": v.get("bytes"),
             "session_id": v.get("session_id"),
             "prompt_id": v.get("prompt_id"),
             "why": why.get(v.get("prompt_id"))}
            for i, v in enumerate(versions)], indent=1)
    if name == "fable_file_diff":
        from fable.filetime import file_events, reconstruct, file_diff
        versions = reconstruct(file_events(db_path, args["path"]))
        n = len(versions)
        a, b = int(args["a"]), int(args["b"])
        if not n:
            return f"no reconstructable versions for {args['path']!r}"
        if not (0 <= a < n and 0 <= b < n):
            return (f"version out of range for {args['path']!r}: has {n} "
                    f"version(s) (0–{n - 1}), got a={a}, b={b}")
        return "\n".join(file_diff(versions, a, b))
    if name == "fable_tags":
        from fable import db as _fdb
        fam = args.get("family")
        conn = _fdb.connect(db_path)
        try:
            if fam:
                rows = conn.execute(
                    "SELECT value, COUNT(DISTINCT prompt_id) n FROM thread_tags"
                    " WHERE family = ? GROUP BY value ORDER BY n DESC LIMIT 80",
                    (fam,)).fetchall()
                out = {"family": fam,
                       "values": [{"value": v, "threads": n} for v, n in rows],
                       "usage": "fable_search(query='…', tag='%s:<value>')" % fam}
            else:
                rows = conn.execute(
                    "SELECT family, COUNT(DISTINCT value),"
                    " COUNT(DISTINCT prompt_id) FROM thread_tags"
                    " GROUP BY family ORDER BY 3 DESC").fetchall()
                out = {"families": [{"family": f, "values": nv, "threads": nt}
                                    for f, nv, nt in rows],
                       "usage": "fable_tags(family='<one above>') for its "
                                "values, then fable_search(tag='family:value')"}
        finally:
            conn.close()
        return json.dumps(out, indent=1)
    if name == "fable_overview":
        from fable.recall import overview
        return json.dumps(overview(db_path, project=args.get("project")),
                          indent=1)
    if name == "fable_decisions":
        from fable.recall import search
        hits = search(db_path, args["query"], project=args.get("project"),
                      since=args.get("since"), until=args.get("until"),
                      limit=int(args.get("limit", 40)))
        out = []
        for h in hits:
            for d in (h.get("decisions") or []):
                out.append({"decision": d, "prompt_id": h["prompt_id"],
                            "title": h.get("title"), "type": h.get("type"),
                            "project": h.get("project"),
                            "score_pct": h.get("score_pct"),
                            "low_confidence": h.get("low_confidence"),
                            "last_ts": h.get("last_ts")})
        return json.dumps({"query": args["query"], "count": len(out),
                           "decisions": out}, indent=1)
    if name == "fable_tasks":
        from collections import Counter
        from fable import tasktime
        data = tasktime.read(db_path)
        proj = (args.get("project") or "").lower()
        status = args.get("status", "open")
        source = args.get("source")
        limit = int(args.get("limit", 30))
        matched = []
        for t in data.get("tasks", []):
            st = t.get("status")
            # "open" = drifted AND a live status. 'unknown' (a task whose
            # completion couldn't be resolved) is NOT open — counting it
            # overstates the backlog.
            if status == "open" and not (
                    t.get("drifted") and st in ("pending", "in_progress")):
                continue
            if status == "completed" and st != "completed":
                continue
            if source and (t.get("source") or "task") != source:
                continue
            if proj and proj not in (t.get("project") or "").lower():
                continue
            matched.append(t)
        matched.sort(key=lambda t: (t.get("ts") or ""), reverse=True)  # recent first
        # summary reflects THIS query's filters, not the global ledger
        by_project = dict(Counter(
            (t.get("project") or "?") for t in matched).most_common())
        tasks, seen = [], set()
        for t in matched:
            subj = " ".join((t.get("subject") or "").split())
            key = (t.get("project"), subj.lower())
            if key in seen:                 # drop near-duplicate mined tasks
                continue
            seen.add(key)
            if len(subj) > 96:              # truncate on a word boundary
                subj = subj[:96].rsplit(" ", 1)[0] + "…"
            tasks.append({
                "id": t.get("id"), "subject": subj, "status": t.get("status"),
                "source": t.get("source"), "project": t.get("project"),
                "ts": t.get("ts"), "priority": t.get("priority"),
                "prompt_id": t.get("prompt_id")})
            if len(tasks) >= limit:
                break
        return json.dumps({
            "status": status, "project": args.get("project"),
            "matched": len(matched), "shown": len(tasks),
            "by_project": by_project, "tasks": tasks}, indent=1)
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
    # Ensure an index exists, creating an empty one if needed, so the server
    # always starts and can answer introspection (initialize / tools/list) —
    # e.g. in a fresh container (Glama) or on first run. Tools return empty
    # results until `fable index` populates it.
    fdb.connect(args.db, create=True).close()
    return serve_stdio(args.db)
