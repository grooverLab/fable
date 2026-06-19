"""fable hook — Claude Code lifecycle hook handler.

The single most demanded capability in the ecosystem (per community
research): archive the FULL transcript automatically before compaction
destroys it. Claude Code invokes hooks with a JSON payload on stdin:

  PreCompact / SessionEnd payload includes:
    {"session_id": "...", "transcript_path": "/path/to/session.jsonl", ...}

Wire-up (settings.json):
  "hooks": {"PreCompact": [{"hooks": [{"type": "command",
      "command": "/path/to/fable/bin/fable --db /path/to/fable.db hook"}]}]}

The handler is fail-quiet by design: a hook must NEVER break a live
session, so all errors are swallowed into the hook log.
"""
import json
import os
import re
import sys
import traceback
from pathlib import Path


def _log(db_path, msg):
    try:
        log = Path(db_path).parent / "hook.log"
        with open(log, "a") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass


CHECKPOINT_TRACK_CAP = 300
CHECKPOINT_MAX_BYTES = 5_000_000
MUTATING_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read")


def _ckpt_dir(session_id):
    from fable.paths import checkpoints_dir
    base = checkpoints_dir()
    d = os.path.join(base, session_id or "unknown")
    os.makedirs(d, exist_ok=True)
    return d


def _post_tool(payload):
    """Close the invisible-mutation gap: scripts (sed, heredocs) change
    files without leaving content in the transcript. Track every file
    Claude touches via file tools; after each Bash call, snapshot any
    tracked file whose bytes changed. Snapshots become exact time-travel
    anchors."""
    import shutil
    import time as _t
    session = payload.get("session_id") or ""
    tool = payload.get("tool_name") or ""
    d = _ckpt_dir(session)
    state_path = os.path.join(d, "tracked.json")
    try:
        with open(state_path) as f:
            tracked = json.load(f)
    except (OSError, ValueError):
        tracked = {}

    def stat_of(p):
        try:
            st = os.stat(p)
            return [st.st_mtime, st.st_size]
        except OSError:
            return None

    changed_any = False
    if tool in MUTATING_TOOLS:
        inp = payload.get("tool_input") or {}
        p = inp.get("file_path") or inp.get("path")
        if isinstance(p, str) and p.startswith("/"):
            st = stat_of(p)
            if st and st[1] <= CHECKPOINT_MAX_BYTES:
                tracked[p] = st
                changed_any = True
                while len(tracked) > CHECKPOINT_TRACK_CAP:
                    tracked.pop(next(iter(tracked)))
    elif tool == "Bash":
        for p, old in list(tracked.items()):
            st = stat_of(p)
            if st is None:
                tracked.pop(p)
                changed_any = True
                continue
            if st == old:
                continue
            # a command mutated a file Claude is working on — checkpoint it
            ts = _t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime())
            name = f"{int(_t.time()*1000):x}-{os.path.basename(p)}"
            try:
                if st[1] <= CHECKPOINT_MAX_BYTES:
                    shutil.copy2(p, os.path.join(d, name))
                    with open(os.path.join(d, "checkpoints.jsonl"),
                              "a") as f:
                        f.write(json.dumps(
                            {"ts": ts, "path": p, "file": name,
                             "after": "Bash"}) + "\n")
            except OSError:
                pass
            tracked[p] = st
            changed_any = True
    if changed_any:
        try:
            with open(state_path, "w") as f:
                json.dump(tracked, f)
        except OSError:
            pass
    return {"ok": True}


CONTEXT_WINDOW = 200_000
AUTOPRUNE_COOLDOWN = 1800     # never twice within 30 min
AUTOPRUNE_MIN_BYTES = 2_000_000


def _context_pct(transcript: str) -> float:
    """Current context fill, estimated from the last assistant message's
    usage block (cumulative input + cache tokens ≈ what the model holds)."""
    try:
        size = os.path.getsize(transcript)
        with open(transcript, "rb") as f:
            f.seek(max(0, size - 300_000))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return 0.0
    for line in reversed(tail):
        if '"usage"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        msg = obj.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict):
            continue
        ctx = ((usage.get("input_tokens") or 0)
               + (usage.get("cache_read_input_tokens") or 0)
               + (usage.get("cache_creation_input_tokens") or 0))
        if ctx:
            return 100.0 * ctx / CONTEXT_WINDOW
    return 0.0


def _auto_prune(db_path: str, payload: dict):
    """When enabled (Settings tab), prune the live transcript automatically
    once context crosses the configured threshold — and tell the user how
    to resume into the slim session."""
    import time as _t
    transcript = payload.get("transcript_path")
    session = payload.get("session_id") or ""
    if not transcript or not os.path.exists(transcript):
        return None
    try:
        from fable import db as fdb
        conn = fdb.connect(db_path)
    except FileNotFoundError:
        return None
    try:
        cfg = dict(conn.execute(
            "SELECT key, value FROM meta WHERE key IN "
            "('autoprune_enabled','autoprune_pct')").fetchall())
        if cfg.get("autoprune_enabled") != "1":
            return None
        threshold = float(cfg.get("autoprune_pct") or 80)
        row = conn.execute("SELECT value FROM meta WHERE key = ?",
                           (f"autoprune_last:{session}",)).fetchone()
        if row and _t.time() - float(row[0]) < AUTOPRUNE_COOLDOWN:
            return None
    finally:
        conn.close()

    if os.path.getsize(transcript) < AUTOPRUNE_MIN_BYTES:
        return None
    pct = _context_pct(transcript)
    if pct < threshold:
        return None

    from fable.discover import project_label
    from fable.paths import vault_dir
    from fable.prune import prune_file
    before = os.path.getsize(transcript)
    project = project_label(os.path.basename(os.path.dirname(transcript)))
    backup_root = vault_dir()
    try:
        prune_file(transcript, "resume",
                   backup_dir=Path(backup_root) / project,
                   replace=True, strip_images=True,
                   db_path=db_path, force=True)
    except Exception as e:
        _log(db_path, f"autoprune failed: {e}")
        return None
    after = os.path.getsize(transcript)
    conn = fdb.connect(db_path)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                 (f"autoprune_last:{session}", str(_t.time())))
    conn.commit()
    conn.close()
    fdb.log_op(db_path, "autoprune", session=session, pct=round(pct, 1),
               before=before, after=after)
    return (f"fable auto-prune: context at {pct:.0f}% — transcript slimmed "
            f"{before / 1e6:.1f}MB → {after / 1e6:.1f}MB (vault backup "
            f"sealed; nothing lost). To load the slim session: /exit then "
            f"`claude --resume {session}`")


def _tail_index(db_path, transcript):
    """Roadmap #1: keep the Map fresh with the turns just appended to the live
    transcript — cheap tail index, no seal. Best-effort: an indexing hiccup
    must never affect the running session."""
    if not transcript or not os.path.exists(transcript):
        return
    try:
        from fable.extract import fts_extract_fn
        from fable.indexer import index_live_tail
        index_live_tail(db_path, transcript, extract_fn=fts_extract_fn)
        _ensure_session_row(db_path, transcript)
    except Exception as e:
        _log(db_path, f"tail-index failed: {e}")


def _ensure_session_row(db_path, transcript):
    """Register the live session in the sidebar. The tail-index writes the turns,
    but the `sessions` row that api_projects lists was discover-only — so a new
    project's session was searchable yet invisible in the tree until a discover
    ran. Create it the moment it's indexed; never clobber a discover-built row."""
    import datetime
    from fable import db as fdb
    from fable.discover import session_title, project_label, project_from_cwd
    sid = os.path.basename(transcript)
    if sid.endswith(".jsonl"):
        sid = sid[:-6]
    conn = fdb.connect(db_path)
    try:
        if conn.execute("SELECT 1 FROM sessions WHERE session_id=?",
                        (sid,)).fetchone():
            # existing (maybe an orphan-vault row) — just ensure live_path is set
            conn.execute(
                "UPDATE sessions SET live_path=COALESCE(live_path, ?) "
                "WHERE session_id=? AND (live_path IS NULL OR live_path='')",
                (transcript, sid))
        else:
            proj = (project_from_cwd(transcript)
                    or project_label(os.path.basename(os.path.dirname(transcript))))
            conn.execute(
                "INSERT INTO sessions(session_id, project, title, live_path,"
                " indexed_at) VALUES(?,?,?,?,?)",
                (sid, proj, session_title(transcript), transcript,
                 datetime.datetime.now(datetime.timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()


_EXTERNALIZE = (
    "<fable-externalize>\n"
    "Reason in TEXT — it is what fable indexes into memory (your thinking "
    "blocks and tool results get pruned; only your prose survives). Before a "
    "non-trivial action, state the decision and the rejected alternative; "
    "after a finding, write \"Found: …\". Name the files, errors and decisions "
    "explicitly so future sessions can recall the WHY, not just the WHAT.\n"
    "</fable-externalize>")


def _externalize_note(db_path: str) -> str:
    """Reasoning-externalisation reminder injected each user turn (default ON;
    suppressed only when meta externalize_enabled='0'). Sentinel-wrapped so the
    extractor strips it — it nudges the model without entering the index."""
    try:
        from fable import db as fdb
        conn = fdb.connect(db_path)
        row = conn.execute(
            "SELECT value FROM meta WHERE key='externalize_enabled'").fetchone()
        conn.close()
        if row and row[0] == "0":
            return ""
    except Exception:
        return ""
    return _EXTERNALIZE


def _capture_task(db_path, payload):
    """M2 live capture: on a TaskCreate/TaskUpdate PostToolUse, upsert the task
    into the materialized `tasks` table immediately — no fat-union rebuild. The
    result carries the exact id ('Task #N created: subject'); TaskUpdate input
    carries the status. Project is provisional (cwd basename) until a rebuild
    reconciles the true work-project. Never raises — hooks must not break tools."""
    tool = payload.get("tool_name") or ""
    if tool not in ("TaskCreate", "TaskUpdate"):
        return
    import re
    import time as _t
    from fable import db as fdb, tasktime
    inp = payload.get("tool_input") or {}
    resp = payload.get("tool_response")
    if isinstance(resp, dict):
        resp = resp.get("content") or json.dumps(resp)
    if isinstance(resp, list):
        resp = " ".join(x.get("text", "") if isinstance(x, dict) else str(x)
                        for x in resp)
    resp = str(resp or "")
    m = re.search(r'Task #(\d+) (?:created|updated)[^:]*:\s*(.*)', resp, re.S)
    tid = (int(m.group(1)) if m
           else int(inp["taskId"]) if str(inp.get("taskId", "")).isdigit()
           else None)
    if tid is None:
        return
    session = payload.get("session_id") or ""
    proj = os.path.basename(payload.get("cwd") or "") or None
    ts = _t.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        conn = fdb.connect(db_path)
        conn.execute(tasktime._TASKS_DDL)
        if tool == "TaskCreate":
            desc = inp.get("description") or ""
            subj = (m.group(2).strip()[:90] if m and m.group(2).strip()
                    else desc.splitlines()[0][:90] if desc else "(empty)")
            cur = conn.execute(
                "UPDATE tasks SET subject=?, ts=? WHERE session=? AND task_id=?",
                (subj, ts, session, tid))
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO tasks(session,task_id,ts,project,subject,status,"
                    "drifted,interp,prompt_id,source,cwd) "
                    "VALUES(?,?,?,?,?,'pending',1,0,NULL,'task',?)",
                    (session, tid, ts, proj, subj, proj))
        else:
            status = inp.get("status")
            if status:
                conn.execute(
                    "UPDATE tasks SET status=?, drifted=? "
                    "WHERE session=? AND task_id=?",
                    (status, 0 if status in ("completed", "deleted") else 1,
                     session, tid))
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── proactive recall scout (UserPromptSubmit) ──────────────────────────────
# The demand engine: surface relevant memory at the moment of need so an agent
# that has never used fable still reaches for it. The gate is PROMPT-SIDE
# (past-work deixis + specific/rare entities) — never the search score, which
# the evidence showed fires HIGHER on generic questions. Results must then clear
# an absolute floor AND an entity-presence check before anything is injected:
# under-push beats over-push (a scout that cries wolf trains banner-blindness).
_ACK_RE = re.compile(
    r"^(thanks|thank you|ok|okay|yes|no|cool|great|perfect|nice|done|got it|"
    r"sure|yep|nope|kk)\b[.! ]*$", re.IGNORECASE)
_DEIXIS_RE = re.compile(
    r"\b(we|our|us|earlier|previously|already|last time|"
    r"where we left off|why did|how did we|continue|resume|pick up|"
    r"the .{1,40} we (built|made|wrote|chose|decided|did))\b", re.IGNORECASE)
# identifier/path/hyphenated/backticked/CapWordsMulti — plain-English prompts
# yield nothing here, which is exactly why "reverse a linked list" stays silent
_ENTITY_RE = re.compile(
    r"`([^`\n]{2,60})`"
    r"|\b([a-z][a-z0-9]*_[a-z0-9_]+)\b"
    r"|\b([a-z]+[A-Z][A-Za-z0-9]+)\b"
    r"|\b([\w./-]+\.(?:py|rs|ts|js|tsx|jsx|go|java|rb|sql|md|ya?ml|json|html|"
    r"css|sh|toml))\b"
    r"|\b([a-z][a-z0-9]+(?:-[a-z0-9]+)+)\b")
_GENERIC_ENT = {
    "python", "rust", "javascript", "typescript", "function", "class",
    "method", "test", "tests", "fix", "bug", "api", "server", "client",
    "code", "file", "files", "error", "data", "run", "build", "app", "db",
    "sql", "json", "yaml", "html", "css", "git", "node", "npm", "docker",
    "read-only", "up-to-date", "auto-complete", "real-time", "end-to-end"}
_SCOUT_FLOOR = 70   # hold unless confident; below 70 is not a good-enough sign
#                     to push. The cosine band (recall._COS_*) is set so genuine
#                     matches read ~75-100% and noise reads <50%.


_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "what", "how", "why", "when",
    "does", "need", "needs", "want", "wants", "let", "lets", "get", "make",
    "use", "using", "add", "adds", "can", "will", "should", "would", "could",
    "about", "into", "from", "your", "yours", "their", "them", "they", "have",
    "has", "had", "was", "were", "been", "are", "is", "be", "do", "did", "done",
    "now", "then", "here", "there", "some", "any", "all", "more", "most", "new",
    "old", "next", "last", "first", "current", "issue", "issues", "problem",
    "thing", "things", "stuff", "part", "way", "ways", "look", "looks", "like",
    "just", "also", "only", "still", "back", "over", "under", "again", "same"}


def _scout_entities(db_path, prompt):
    """Specific entities from the prompt — the fire signal. Two sources:
    identifier-shaped tokens (snake_case/path/hyphenated/camelCase/back-ticked —
    inherently specific), and notable plain words that match fable's KNOWN tag
    values ("VIX", "auto-rules" — specific because they're from your work). Both
    are kept only if not ubiquitous (DF below a corpus-share ceiling); the
    purely-generic (a word in no tag and no identifier) stays silent."""
    ids, words = set(), set()
    for m in _ENTITY_RE.finditer(prompt):
        v = next((g for g in m.groups() if g), "").strip().lower()
        if 3 <= len(v) <= 60 and v not in _GENERIC_ENT:
            ids.add(v)
    for w in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", prompt):  # 3+ chars (orb/vix)
        wl = w.lower()
        if wl not in _GENERIC_ENT and wl not in _STOPWORDS:
            words.add(wl)
    if not ids and not words:
        return []
    try:
        from fable import db as fdb
        conn = fdb.connect(db_path)
    except Exception:
        return list(ids)[:4]
    try:
        n = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] or 1
        ceil = max(60, n * 4 // 10)
        # known entities = tag values in the SPECIFIC families only (topic /
        # technology / entity / pattern). NOT activity/outcome/decision — those
        # are generic action words ("write", "fix") that would fire on anything.
        known = {r[0] for r in conn.execute(
            "SELECT DISTINCT value FROM thread_tags WHERE family IN"
            " ('topic','technology','entity','pattern')")} if words else set()
        # augment with the learned NER dictionary — adds file names + the
        # carder's salient_entities, richer than tag values alone (and it grows
        # as the dictionary is rebuilt on new cards). Defensive: tags still work
        # if the dictionary hasn't been built yet.
        if words:
            try:
                from fable import ner as _ner
                known |= set(_ner.load_dictionary(db_path))
            except Exception:
                pass
        candidates = ids | (words & known)
        kept = []
        for v in candidates:
            try:
                df = conn.execute(
                    "SELECT COUNT(DISTINCT prompt_id) FROM terms WHERE term=?",
                    (v,)).fetchone()[0]
            except Exception:
                df = 1
            # identifiers fire even at df 0 (inherently specific — Stage 3 then
            # decides if any memory exists); known-tag words need df >= 1
            if v in ids and df <= ceil:
                kept.append(v)
            elif v in known and 1 <= df <= ceil:
                kept.append(v)
        # most-specific first (identifiers, then longer) — drives the teaser
        # head and the search query
        kept.sort(key=lambda v: (any(c in v for c in "_-./"), len(v)),
                  reverse=True)
        return kept[:4]
    finally:
        conn.close()


def _scout_teaser(entities, hits):
    head = entities[0] if entities else "this"
    lines = [f"\U0001f9e0 fable — prior work on «{head}»:"]
    for h in hits:
        ts = (h.get("last_ts") or "")[:10]
        line = f"• {h.get('title') or h.get('prompt_id')}"
        if ts:
            line += f" ({ts})"
        decs = h.get("decisions") or []
        if decs:
            line += f"\n  decided: {decs[0][:140]}"
        lines.append(line)
    top = hits[0].get("prompt_id")
    lines.append(f'Recall verbatim: fable_thread("{top}")  ·  '
                 f'more: fable_search("{head}")')
    lines.append("Pointer surfaced from memory — verify before relying.")
    return "<fable-scout>\n" + "\n".join(lines) + "\n</fable-scout>"


def _scout_resume_teaser(r):
    proj = r.get("project") or ""
    lines = [f"\U0001f9e0 fable — resuming «{proj}» "
             f"(last active {(r.get('last_active') or '')[:10]}):"]
    for t in (r.get("recent_threads") or [])[:3]:
        lines.append(f"• {t.get('title') or t.get('prompt_id')}")
    decs = r.get("last_decisions") or []
    if decs:
        lines.append(f"last decision: {(decs[0].get('decision') or '')[:140]}")
    sn = r.get("suggested_next")
    if sn:
        lines.append(f"suggested next: {sn}")
    lines.append(f'Full picture: fable_resume("{proj}")')
    return "<fable-scout>\n" + "\n".join(lines) + "\n</fable-scout>"


# ── two-clock design: ALL intelligence is precomputed by the carder (offline);
# the per-turn path below only matches + does ONE fast bm25 query, so it can
# never hold the main model. Guardrails: a hard timeout backstop and an
# activation gate (silent until a project's index is mature — pushing on a thin
# index breeds banner-blindness; cold-start = silent, which is also correct).
_SCOUT_TIMEOUT_S = 0.50   # FTS-bm25 over the full corpus is ~180ms here; the
#                           backstop guards the tail, not the typical path
_SCOUT_VEC_TIMEOUT_S = 1.5  # embed + cosine is ~0.5s; only the firing path pays
_SCOUT_MIN_THREADS = 5    # activation gate: carded threads a project needs first


def _with_timeout(fn, seconds, default=None):
    """Run fn() but never let it stall the user's turn — a daemon worker is
    abandoned if it overruns (the hook runs synchronously before the turn)."""
    import threading
    box = {"v": default}

    def run():
        try:
            box["v"] = fn()
        except Exception:
            box["v"] = default
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    return box["v"]


def _scout_project(payload):
    """The current context's project = the cwd basename, which matches the
    cwd-derived `sessions.project` the threads are stored under. Same-context
    memory ranks above cross-project."""
    cwd = payload.get("cwd") or ""
    return os.path.basename(cwd) if cwd else None


def _project_mature(db_path, project):
    """Activation gate — a project must have accumulated enough carded threads
    before the scout pushes. Unknown cwd → don't block (the global corpus is
    mature). A brand-new project stays silent until it grows."""
    if not project:
        return True
    try:
        from fable import db as fdb
        conn = fdb.connect(db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM cards c JOIN threads t"
                " ON t.prompt_id = c.prompt_id JOIN sessions s"
                " ON s.session_id = t.session_id WHERE s.project LIKE ?",
                (f"%{project}%",)).fetchone()[0]
        finally:
            conn.close()
        return n >= _SCOUT_MIN_THREADS
    except Exception:
        return True


def _log_scout_fire(db_path, session_id, hits, query):
    """Record what the scout surfaced, so we can measure whether it got used
    (precision = used/fires → calibrates the floor). Never raises."""
    try:
        import datetime
        from fable import db as fdb
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn = fdb.connect(db_path)
        try:
            for h in hits:
                conn.execute(
                    "INSERT INTO scout_fires(session_id, prompt_id, score_pct,"
                    " query, created_at) VALUES(?,?,?,?,?)",
                    (session_id, h.get("prompt_id"), h.get("score_pct"),
                     (query or "")[:200], now))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _mark_scout_conversion(db_path, payload):
    """When the agent opens a thread the scout surfaced (fable_thread/block/
    recall on that prompt_id), mark the fire `used` — that's a conversion."""
    try:
        tool = payload.get("tool_name") or ""
        if not any(k in tool for k in ("fable_thread", "fable_block",
                                       "fable_recall")):
            return
        ti = payload.get("tool_input") or {}
        pid = ti.get("prompt_id") or ti.get("uuid") or ti.get("block_id") or ""
        if not pid:
            return
        from fable import db as fdb
        conn = fdb.connect(db_path)
        try:
            conn.execute("UPDATE scout_fires SET used=1 WHERE prompt_id=?"
                         " AND used=0", (pid,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _scout_hold(db_path, prompt, reason):
    """Record WHY the scout stayed silent — the invisible 'held' path made
    visible for diagnosis. Fires log to scout_fires; holds log to hook.log so a
    live turn can be inspected ('immature_project' / 'below_floor(best=68/70)' /
    'no_entity_no_deixis' / 'error:…')."""
    try:
        _log(db_path, json.dumps({"scout_held": reason,
                                  "prompt": (prompt or "")[:90]}))
    except Exception:
        pass
    return ""


def _scout(db_path, payload):
    """Staged proactive-recall gate. Returns a <fable-scout> teaser or ''."""
    prompt = (payload.get("prompt") or "").strip()
    if len(prompt) < 12 or _ACK_RE.match(prompt):              # Stage 0
        return _scout_hold(db_path, prompt, "short_or_ack")
    deixis = bool(_DEIXIS_RE.search(prompt))
    entities = _scout_entities(db_path, prompt)                # Stage 1
    if not deixis and not entities:
        return _scout_hold(db_path, prompt, "no_entity_no_deixis")
    project = _scout_project(payload)
    if not _project_mature(db_path, project):                  # activation gate
        return _scout_hold(db_path, prompt,
                           "immature_project:%s" % (project or "?"))
    try:
        from fable.recall import scout_search, scout_vector_search, resume
        if entities:                                           # Stage 2 retrieve
            # HYBRID — run BOTH and fire on the best of either. Vectors find the
            # thread even when wording differs; bm25 catches exact identifiers the
            # embedding misses. Each wins for different prompts, so neither is
            # gated behind the other (the old "fall back only if vectors empty"
            # was unreachable: a junk-but-non-empty vector list blocked bm25).
            vec = _with_timeout(   # GLOBAL — not project-scoped, so cross-project
                lambda: scout_vector_search(db_path, prompt, limit=12),
                _SCOUT_VEC_TIMEOUT_S, default=[]) or []  # recall works too
            lex = _with_timeout(
                lambda: scout_search(db_path, " ".join(entities), limit=8),
                _SCOUT_TIMEOUT_S, default=[]) or []
            best = {}                                          # prompt_id -> hit
            for h in vec:                       # vectors: cosine IS the confidence
                if not h.get("low_confidence") and \
                        (h.get("score_pct") or 0) >= _SCOUT_FLOOR:
                    best[h["prompt_id"]] = h
            for h in lex:                       # bm25: require entity-presence
                if h.get("low_confidence") or \
                        (h.get("score_pct") or 0) < _SCOUT_FLOOR:
                    continue
                blob = " ".join([h.get("title") or "", h.get("snippet") or "",
                                 " ".join(h.get("decisions") or [])]).lower()
                if not any(e in blob for e in entities):
                    continue
                cur = best.get(h["prompt_id"])
                if not cur or (h.get("score_pct") or 0) > (cur.get("score_pct") or 0):
                    best[h["prompt_id"]] = h
            if not best:
                raw = [(h.get("score_pct") or 0) for h in (vec + lex)]
                return _scout_hold(db_path, prompt,
                                   "below_floor(best=%d/%d,ent=%s)" % (
                                       max(raw) if raw else 0, _SCOUT_FLOOR,
                                       ",".join(entities[:3])))
            keep = sorted(best.values(), key=lambda h: -(  # soft project boost:
                (h.get("score_pct") or 0)                  # local outranks a
                + (15 if project and project.lower() in     # comparable global,
                   (h.get("project") or "").lower() else 0)))  # not a hard wall
            _log_scout_fire(db_path, payload.get("session_id"), keep[:3], prompt)
            return _scout_teaser(entities, keep[:3])
        cwd = payload.get("cwd") or ""                         # deixis → resume
        r = _with_timeout(
            lambda: resume(db_path,
                           project=os.path.basename(cwd) if cwd else None),
            _SCOUT_TIMEOUT_S)
        if not r or not r.get("found") or not r.get("recent_threads"):
            return _scout_hold(db_path, prompt, "resume_empty")
        return _scout_resume_teaser(r)
    except Exception:
        return ""


def run_hook(db_path: str, payload: dict) -> dict:
    transcript = payload.get("transcript_path")
    event = payload.get("hook_event_name", "?")
    session = payload.get("session_id", "?")

    if event == "PreToolUse":
        tool = payload.get("tool_name") or ""
        # compaction-read gate: a just-compacted session must READ (and attest)
        # its recovery threads before any edit — DENY until the gate is clear.
        # Fails open: a gate error must never wedge editing.
        if (tool in ("Edit", "Write", "MultiEdit")
                and os.environ.get("FABLE_COMPACTION_GATE", "on").lower()
                not in ("off", "0", "false", "no")):
            try:
                from fable import gate
                pend = gate.pending(db_path, session)
                if pend:
                    return {"ok": True, "event": "PreToolUse",
                            "deny": gate.deny_message(pend)}
            except Exception:
                pass
        # graph: before an Edit/Write, inject the target file's BLAST RADIUS —
        # the past threads + decisions that touched it — so the model doesn't
        # relitigate a settled call or repeat a known trap. Silent when the file
        # has no recorded history (no nagging on greenfield files).
        if tool in ("Edit", "Write", "MultiEdit"):
            inp = payload.get("tool_input") or {}
            fp = inp.get("file_path") or inp.get("path") or ""
            if fp:
                try:
                    from fable.graph import render_blast_radius
                    block = render_blast_radius(db_path, fp)
                    if block:
                        return {"ok": True, "event": "PreToolUse",
                                "inject": block}
                except Exception:
                    pass
        return {"ok": True, "event": "PreToolUse"}

    if event == "PostToolUse":
        result = _post_tool(payload)
        _capture_task(db_path, payload)        # M2: live task ledger
        _mark_scout_conversion(db_path, payload)  # scout precision loop
        msg = _auto_prune(db_path, payload)
        if msg:
            result["system_message"] = msg
        return result
    if event in ("Stop", "SubagentStop"):
        # turn boundary: the assistant (or a subagent) just finished, so the
        # new turn(s) are settled in the transcript — index the tail now
        _tail_index(db_path, transcript)
        return {"ok": True, "event": event}
    if event == "UserPromptSubmit":
        # the user may have edited tracked files in their IDE while Claude
        # was idle — sweep for silent changes before the next turn begins —
        # and tail-index so the just-submitted prompt enters the Map at once
        payload = dict(payload, tool_name="Bash")
        result = _post_tool(payload)
        _tail_index(db_path, transcript)
        parts = []
        note = _externalize_note(db_path)
        if note:
            parts.append(note)
        try:                                    # the proactive recall scout
            teaser = _scout(db_path, payload)
            if teaser:
                parts.append(teaser)
        except Exception as e:
            _log(db_path, json.dumps({"scout_error": str(e)[:160]}))
        if parts:
            result["inject"] = "\n\n".join(parts)
            result["event"] = "UserPromptSubmit"
        return result

    if event == "SessionStart":
        # auto-inject remembered facts into the fresh session — and after a
        # compaction, heal the amnesia: re-inject this session's own memory
        from fable.facts import render_facts
        cwd = payload.get("cwd") or ""
        project = os.path.basename(cwd) if cwd else None
        parts = []
        try:
            block = render_facts(db_path, project=project)
            if block:
                parts.append(block)
        except FileNotFoundError:
            pass
        try:    # Phase 2 enforcement — inject the user's APPROVED standing rules
            from fable.rules import render_rules
            rblock = render_rules(db_path, project=project)
            if rblock:
                parts.append(rblock)
        except Exception:
            pass
        if payload.get("source") == "compact" and session != "?":
            # a tool description is passive; THIS fires the instruction at the
            # exact moment the model would otherwise trust the lossy summary
            parts.append(
                "⚠ This session was COMPACTED — the earlier turns you see are "
                "a lossy summary. Before relying on the summary for any "
                "specific detail (decisions, code, exact wording), recall the "
                "real turns with the fable MCP tools (fable_search → "
                "fable_thread); fable preserved the full verbatim transcript.")
            healed = _compaction_recovery(db_path, session)
            if healed:
                parts.append(healed)
            try:                            # ARM the hard gate — the soft
                from fable import gate      # directive above isn't enough; block
                gate.arm(db_path, session)  # edits until the threads are attested
            except Exception:
                pass
        else:
            parts.append(
                "📚 fable recall is available (MCP tools fable_search, "
                "fable_thread, fable_block). When you need earlier "
                "conversation, a past decision, or a prior code discussion "
                "that isn't in your context, call fable_search instead of "
                "guessing — it indexes this and every past session, verbatim.")
        # M7: resurface open/drifted tasks so they don't get lost. The reminder
        # fires for the cwd dir, but the tasks usually belong to the
        # work-project(s) you build FROM that dir — label by work-project so
        # e.g. 61 fable + 42 qt tasks aren't mis-bannered as the cwd's name.
        try:
            from fable import tasktime
            rows, total, by_project = tasktime.open_for_project(
                db_path, project, 8)
            if rows:
                breakdown = " · ".join(
                    f"{p} {n}" for p, n in sorted(
                        by_project.items(), key=lambda kv: -kv[1]))
                lines = [
                    f"  #{r[0]} [{r[1]}] {(r[2] or '')[:72]}"
                    + (f"  ·P{r[3]}" if r[3] is not None else "")
                    + (f"  ({r[4]})" if r[4] and r[4] != project else "")
                    for r in rows]
                more = (f"\n  …and {total - len(rows)} more — open the Tasks tab"
                        if total > len(rows) else "")
                parts.append(
                    f"<fable-open-tasks cwd=\"{project}\" count=\"{total}\">\n"
                    f"{total} open task(s) surfaced from this directory, by "
                    f"work-project: {breakdown} — pick one up, or mark done / "
                    f"remove if stale:\n"
                    + "\n".join(lines) + more + "\n</fable-open-tasks>")
        except Exception:
            pass
        return {"ok": True, "event": event, "inject": "\n".join(parts)}

    if not transcript or not os.path.exists(transcript):
        return {"ok": False, "reason": "no transcript_path"}

    from fable.discover import project_label
    from fable.paths import vault_dir
    from fable.extract import fts_extract_fn
    from fable.indexer import index_vault
    from fable.prune import backup as vault_backup

    # project label from the encoded ~/.claude/projects dirname
    project = project_label(os.path.basename(os.path.dirname(transcript)))
    backup_dir = Path(vault_dir()) / project

    version, dest = vault_backup(Path(transcript), backup_dir)
    stats = index_vault(db_path, [str(dest)], live_file=transcript,
                        extract_fn=fts_extract_fn,
                        session_id=session, project=project)
    return {"ok": True, "event": event, "backup": str(dest),
            "version": version,
            "records_indexed": stats["records_indexed"]}


def _compaction_recovery(db_path: str, session_id: str, limit: int = 18) -> str:
    """Compaction is a CONTINUATION of the current work, so recovery comes ONLY
    from THIS session's own threads — HARD-gated on session_id (one project's
    transcript). Deliberately NO cross-session 'related' threads: feeding a
    freshly-compacted model anything from another project would only lead it
    astray. It hands the agent the last ~`limit` thread UUIDs and ENFORCES
    reading each IN FULL via fable_thread (not the lossy summary, not the card
    titles) — those threads ARE the working context. Includes uncarded recent
    threads (the latest turns matter most and may not be carded yet)."""
    from fable import gate
    own = gate.recovery_threads(db_path, session_id, limit)
    if not own:
        return ""
    L = ['<fable-memory source="compaction-recovery">',
         "⚠ STOP — do NOT 'resume as if nothing happened'. The compaction note "
         "above just told you to continue straight from the summary; here that "
         "is WRONG. This session was COMPACTED and the summary is LOSSY — it "
         "quietly dropped decisions, exact wording and open questions, so it "
         "READS complete while you are in fact missing context. Re-reading the "
         "real threads IS how you resume correctly — it is NOT a recap for the "
         "user.",
         f"⚠ Your FIRST action this turn — before any edit, answer, or line "
         f"of code — MUST be fable_thread() on the {len(own)} threads below "
         "(most recent first). Reading a source FILE is fine for code, but the "
         "file does NOT carry the DECISIONS or what's settled-vs-still-open — "
         "only these threads do, and that is exactly what the summary loses. "
         "Skip none of the recent ones; trust the summary for NOTHING; for "
         "anything older, fable_search — never guess.",
         "\nTHIS SESSION'S LAST THREADS (most recent first — read IN FULL, now):"]
    for pid, title in own:
        L.append(f'- {title or "(latest — not yet carded)"}  '
                 f'fable_thread("{pid}")')
    L.append("</fable-memory>")
    return "\n".join(L)


def cmd_hook(args) -> int:
    """Read the hook payload from stdin; never exit non-zero on failure
    (a broken hook must not block compaction or session end)."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = run_hook(args.db, payload)
        if result.get("deny"):
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": result["deny"]}}))
        elif result.get("inject"):
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": result.get("event", "SessionStart"),
                "additionalContext": result["inject"]}}))
        elif result.get("system_message"):
            print(json.dumps({"systemMessage": result["system_message"]}))
        _log(args.db, json.dumps({"payload_event":
                                  payload.get("hook_event_name"),
                                  **{k: v for k, v in result.items()
                                     if k not in ("inject", "deny")}}))
    except Exception:
        _log(args.db, "hook error:\n" + traceback.format_exc())
    return 0
