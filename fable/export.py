"""Export threads/sessions as Markdown or standalone HTML.

Shared transcripts are the organic distribution loop — every export
carries a "made with fable" footer.
"""
import html as html_mod
import json

from fable import db as fdb
from fable.jsonl import read_span
from fable.threads import reconstruct

FOOTER_MD = "\n---\n*exported with [fable](https://github.com/grooverLab/fable) — full-fidelity memory for Claude Code*\n"
TOOL_RESULT_CAP = 4000


def _turn_md(turn, obj):
    out = [f"### {obj.get('type') or turn.type} · `{turn.uuid[:8]}` · {turn.ts or ''}\n"]
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        out.append(content + "\n")
        return out
    for block in content or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            out.append(block.get("text", "") + "\n")
        elif kind == "thinking" and block.get("thinking"):
            out.append("> *(thinking)* " +
                       block["thinking"].replace("\n", "\n> ") + "\n")
        elif kind == "tool_use":
            inp = block.get("input") if isinstance(block.get("input"), dict) else {}
            args = "\n".join(f"{k}: {v}" for k, v in inp.items()
                             if isinstance(v, str))
            out.append(f"**tool: {block.get('name', '?')}**\n```\n{args}\n```\n")
        elif kind == "tool_result":
            inner = block.get("content")
            texts = []
            if isinstance(inner, str):
                texts.append(inner)
            elif isinstance(inner, list):
                texts = [b.get("text", "") for b in inner
                         if isinstance(b, dict) and b.get("type") == "text"]
            body = "\n".join(texts)
            if len(body) > TOOL_RESULT_CAP:
                body = body[:TOOL_RESULT_CAP] + "\n… (truncated)"
            out.append(f"```\n{body}\n```\n")
        elif kind == "image":
            out.append("*[image]*\n")
    return out


def export_thread_md(db_path: str, prompt_id: str) -> str:
    conn = fdb.connect(db_path)
    try:
        view = reconstruct(conn, prompt_id)
        card = conn.execute(
            "SELECT title, type, outcome, summary FROM cards "
            "WHERE prompt_id = ?", (prompt_id,)).fetchone()
    finally:
        conn.close()
    if not view.main and not view.orphans:
        raise KeyError(f"thread not in index: {prompt_id}")
    lines = []
    if card:
        lines += [f"# {card[0]}", "",
                  f"*{card[1]} · {card[2] or ''}*", "",
                  card[3] or "", ""]
    else:
        lines += [f"# thread {prompt_id}", ""]
    for turn in view.main:
        obj = json.loads(read_span(turn.path, turn.offset, turn.length)
                         .decode("utf-8", "surrogateescape"))
        lines += _turn_md(turn, obj)
    return "\n".join(lines) + FOOTER_MD


HTML_SHELL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{title}</title><style>
body{{max-width:860px;margin:40px auto;padding:0 20px;background:#0b0d10;
color:#cdd6e4;font:15px/1.6 -apple-system,sans-serif}}
h1{{color:#ffb454}} h3{{color:#ffb454;border-top:1px solid #232a35;
padding-top:14px;font-size:13px;font-weight:600}}
pre{{background:#171b22;border:1px solid #232a35;border-radius:6px;
padding:12px;overflow:auto;font-size:12.5px;white-space:pre-wrap}}
blockquote{{color:#6b7689;border-left:3px solid #232a35;margin-left:0;
padding-left:14px}} code{{color:#6cc7d9}}
footer{{margin-top:40px;border-top:1px solid #232a35;padding-top:12px;
color:#6b7689;font-size:12px}} a{{color:#ffb454}}
</style></head><body>{body}
<footer>exported with <a href="https://github.com/grooverLab/fable">fable</a>
— full-fidelity memory for Claude Code</footer></body></html>"""


def export_thread_html(db_path: str, prompt_id: str) -> str:
    md = export_thread_md(db_path, prompt_id)
    md = md.split("\n---\n*exported with")[0]
    body, in_code = [], False
    title = "fable export"
    for line in md.splitlines():
        if line.startswith("```"):
            body.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            body.append(html_mod.escape(line))
            continue
        esc = html_mod.escape(line)
        if line.startswith("# "):
            title = line[2:]
            body.append(f"<h1>{esc[2:]}</h1>")
        elif line.startswith("### "):
            body.append(f"<h3>{esc[4:]}</h3>")
        elif line.startswith("> "):
            body.append(f"<blockquote>{esc[5:]}</blockquote>")
        elif line.startswith("**tool:"):
            body.append(f"<p><code>{esc.replace('**', '')}</code></p>")
        else:
            body.append(f"<p>{esc}</p>" if line.strip() else "")
    return HTML_SHELL.format(title=html_mod.escape(title),
                             body="\n".join(body))


def cmd_export(args) -> int:
    fmt = args.format
    content = (export_thread_html(args.db, args.prompt_id) if fmt == "html"
               else export_thread_md(args.db, args.prompt_id))
    if getattr(args, "gist", False):
        import subprocess
        import tempfile
        suffix = ".html" if fmt == "html" else ".md"
        with tempfile.NamedTemporaryFile(
                "w", suffix=suffix, delete=False,
                prefix=f"fable-{args.prompt_id[:8]}-") as f:
            f.write(content)
            path = f.name
        proc = subprocess.run(["gh", "gist", "create", path],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"gh gist create failed: {proc.stderr[:200]}")
        print(proc.stdout.strip())  # the gist URL (secret by default)
        return 0
    if args.output:
        with open(args.output, "w") as f:
            f.write(content)
        print(f"exported -> {args.output}")
    else:
        print(content)
    return 0
