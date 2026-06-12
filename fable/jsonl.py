"""Streaming JSONL reader with exact byte offsets.

Claude Code transcripts are JSONL, but concurrent writes occasionally
concatenate two JSON objects on one physical line (the real corpus has one
such line). Each parsed object is yielded with the byte offset and length
of its own span, so callers can seek+read it back verbatim.
"""
import json
from typing import Any, Callable, Iterator, NamedTuple, Optional


class Record(NamedTuple):
    lineno: int
    offset: int   # byte offset of this object's span in the file
    length: int   # byte length of the span
    obj: Any


_decoder = json.JSONDecoder()


def iter_records(
    path: str,
    on_error: Optional[Callable[[int, Exception], None]] = None,
) -> Iterator[Record]:
    """Yield Record for every JSON object in a JSONL file.

    Offsets are byte-accurate even for non-ASCII content; multiple
    concatenated objects on one line are yielded individually.
    """
    with open(path, "rb") as f:
        line_start = 0
        lineno = 0
        for raw in f:
            lineno += 1
            this_start = line_start
            line_start += len(raw)

            stripped = raw.strip()
            if not stripped:
                continue

            # surrogateescape round-trips invalid UTF-8 byte-exactly, so the
            # offsets below are true byte offsets even for malformed input
            # (errors="replace" would inflate lengths: U+FFFD is 3 bytes).
            text = raw.decode("utf-8", errors="surrogateescape")
            pos = 0
            while pos < len(text):
                while pos < len(text) and text[pos] in " \t\r\n":
                    pos += 1
                if pos >= len(text):
                    break
                try:
                    obj, end = _decoder.raw_decode(text, pos)
                except json.JSONDecodeError as e:
                    if on_error:
                        on_error(lineno, e)
                    break
                byte_off = this_start + len(
                    text[:pos].encode("utf-8", "surrogateescape"))
                byte_len = len(
                    text[pos:end].encode("utf-8", "surrogateescape"))
                yield Record(lineno, byte_off, byte_len, obj)
                pos = end


def read_span(path: str, offset: int, length: int) -> bytes:
    """Read an exact byte span — the recall fast path."""
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(length)
