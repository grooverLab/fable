"""Shared test fixture builders: synthetic transcripts and vault generations."""
import json
import os


def rec(uuid, prompt_id=None, parent=None, rtype="assistant", ts=None,
        text=None, extra=None, sidechain=False):
    """Build a minimal Claude-Code-shaped transcript record.

    Mirrors the real harness: ONLY user records carry promptId — assistant
    and tool turns belong to a thread transitively via parentUuid.
    """
    obj = {
        "uuid": uuid,
        "parentUuid": parent,
        "type": rtype,
        "isSidechain": sidechain,
        "timestamp": ts or "2026-06-01T00:00:00.000Z",
        "sessionId": "test-session",
    }
    if prompt_id and rtype == "user":
        obj["promptId"] = prompt_id
    role = "user" if rtype == "user" else "assistant"
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    obj["message"] = {"role": role, "content": content}
    if extra:
        obj.update(extra)
    return obj


def tool_use_block(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def tool_result_block(tid, text):
    return {"type": "tool_result", "tool_use_id": tid,
            "content": [{"type": "text", "text": text}]}


def write_jsonl(path, objs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for o in objs:
            f.write(json.dumps(o, separators=(",", ":")) + "\n")
    return path
