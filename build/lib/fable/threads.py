"""Thread reconstruction from the uuid/parentUuid graph.

A thread is every record sharing a promptId. The canonical conversation is
the parent-chain reachable from the latest turn; records off that chain are
abandoned edit/retry branches; isSidechain records are subagent transcripts.
Nothing here is heuristic — the harness wrote these links.
"""
from typing import List, NamedTuple

class Turn(NamedTuple):
    uuid: str
    parent_uuid: str
    type: str
    role: str
    ts: str
    lineno: int
    path: str
    offset: int
    length: int
    is_sidechain: int


class ThreadView(NamedTuple):
    prompt_id: str
    main: List[Turn]        # canonical chain, conversation order
    orphans: List[Turn]     # abandoned edit/retry branches
    sidechains: List[Turn]  # subagent records, ts order


def reconstruct(conn, prompt_id: str) -> ThreadView:
    rows = conn.execute(
        "SELECT r.uuid, r.parent_uuid, r.type, r.role, r.ts, r.lineno,"
        " f.path, r.offset, r.length, r.is_sidechain "
        "FROM records r JOIN files f ON f.id = r.file_id "
        "WHERE r.prompt_id = ? ORDER BY r.ts_epoch, r.lineno",
        (prompt_id,)).fetchall()
    turns = [Turn(*row) for row in rows]

    sidechains = [t for t in turns if t.is_sidechain]
    mainline = [t for t in turns if not t.is_sidechain]
    by_uuid = {t.uuid: t for t in mainline}

    chain: List[Turn] = []
    seen = set()
    cur = mainline[-1] if mainline else None
    while cur is not None and cur.uuid not in seen:
        chain.append(cur)
        seen.add(cur.uuid)
        cur = by_uuid.get(cur.parent_uuid)
    chain.reverse()

    orphans = [t for t in mainline if t.uuid not in seen]
    return ThreadView(prompt_id, chain, orphans, sidechains)
