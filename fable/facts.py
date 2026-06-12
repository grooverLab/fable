"""/remember — durable cross-session facts, auto-injected at SessionStart.

The community-validated pattern (aider #2859, claude-mem's market): the
user states a fact once ("we use uv, never pip"), fable stores it, and
every future session in scope starts already knowing it — via the
SessionStart hook's additionalContext, no agent-maintained markdown files.
"""
import datetime
from typing import Optional

from fable import db as fdb


def add_fact(db_path: str, fact: str, project: Optional[str] = None,
             source: str = "user") -> int:
    conn = fdb.connect(db_path, create=True)
    try:
        cur = conn.execute(
            "INSERT INTO facts(fact, project, source, created_at) "
            "VALUES(?,?,?,?)",
            (fact.strip(), project,
             source, datetime.datetime.now(
                 datetime.timezone.utc).isoformat()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_facts(db_path: str, project: Optional[str] = None,
               include_inactive: bool = False):
    conn = fdb.connect(db_path)
    try:
        sql = ("SELECT id, fact, project, source, created_at, active "
               "FROM facts WHERE 1=1 ")
        args = []
        if not include_inactive:
            sql += "AND active = 1 "
        if project is not None:
            sql += "AND (project IS NULL OR project LIKE ?) "
            args.append(f"%{project}%")
        sql += "ORDER BY id"
        cols = ["id", "fact", "project", "source", "created_at", "active"]
        return [dict(zip(cols, r)) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def forget_fact(db_path: str, fact_id: int) -> bool:
    conn = fdb.connect(db_path)
    try:
        cur = conn.execute("UPDATE facts SET active = 0 WHERE id = ?",
                           (fact_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def render_facts(db_path: str, project: Optional[str] = None) -> str:
    """Compact block for SessionStart injection. Empty string if none."""
    facts = list_facts(db_path, project=project)
    if not facts:
        return ""
    lines = ["<fable-memory>",
             "Durable facts the user asked to remember "
             "(via `fable remember`):"]
    for f in facts:
        scope = f" [{f['project']}]" if f["project"] else ""
        lines.append(f"- {f['fact']}{scope}")
    lines.append("Recall more history any time: fable_search / fable_thread "
                 "MCP tools, or `fable search` in the shell.")
    lines.append("</fable-memory>")
    return "\n".join(lines)


def cmd_remember(args) -> int:
    fid = add_fact(args.db, " ".join(args.fact), project=args.project)
    print(f"remembered (#{fid})")
    return 0


def cmd_facts(args) -> int:
    facts = list_facts(args.db, include_inactive=args.all)
    if getattr(args, "json", False):
        import json
        print(json.dumps(facts, indent=1))
        return 0
    if not facts:
        print("no facts stored — add one: fable remember \"we use uv, "
              "never pip\"")
        return 0
    for f in facts:
        flag = "" if f["active"] else " (forgotten)"
        scope = f" [{f['project']}]" if f["project"] else " [global]"
        print(f"#{f['id']}{scope}{flag}  {f['fact']}")
    return 0


def cmd_forget(args) -> int:
    ok = forget_fact(args.db, args.id)
    print("forgotten" if ok else "no such fact")
    return 0 if ok else 1
