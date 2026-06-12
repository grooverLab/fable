#!/usr/bin/env python3
"""One-off: produce an image-stripped working copy of a transcript.

Replaces base64 image source data with a small placeholder so test cycles
run against ~10MB instead of ~84MB. The pristine copy is never touched.
"""
import json
import sys


def strip_obj(obj):
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return 0
    content = msg.get("content")
    if not isinstance(content, list):
        return 0
    stripped = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            src = block.get("source")
            if isinstance(src, dict) and "data" in src:
                stripped += len(src.get("data") or "")
                src["data"] = "<stripped>"
    return stripped


def main(src, dst):
    decoder = json.JSONDecoder()
    total = 0
    with open(src) as f, open(dst, "w") as out:
        for line in f:
            text = line.strip()
            if not text:
                continue
            pos = 0
            while pos < len(text):
                while pos < len(text) and text[pos] in " \t":
                    pos += 1
                if pos >= len(text):
                    break
                try:
                    obj, end = decoder.raw_decode(text, pos)
                except json.JSONDecodeError:
                    out.write(text[pos:] + "\n")
                    break
                total += strip_obj(obj)
                out.write(json.dumps(obj, separators=(",", ":")) + "\n")
                pos = end
    print(f"stripped {total/1e6:.1f}MB of image data -> {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
