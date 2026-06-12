import json
import os
import tempfile
import unittest

from fable.jsonl import iter_records


def write_lines(lines):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "wb") as f:
        for line in lines:
            f.write(line if isinstance(line, bytes) else line.encode())
    return path


class TestIterRecords(unittest.TestCase):
    def tearDown(self):
        if hasattr(self, "path") and os.path.exists(self.path):
            os.unlink(self.path)

    def test_offsets_allow_exact_reseek(self):
        objs = [{"uuid": "a", "n": 1}, {"uuid": "b", "n": 2}, {"uuid": "c", "n": 3}]
        self.path = write_lines([json.dumps(o) + "\n" for o in objs])
        out = list(iter_records(self.path))
        self.assertEqual(len(out), 3)
        with open(self.path, "rb") as f:
            for rec in out:
                f.seek(rec.offset)
                raw = f.read(rec.length)
                self.assertEqual(json.loads(raw), rec.obj)
        self.assertEqual([r.lineno for r in out], [1, 2, 3])

    def test_concatenated_objects_on_one_line_recovered(self):
        line = '{"uuid":"a","n":1}{"uuid":"b","n":2}\n'
        self.path = write_lines(['{"uuid":"z"}\n', line])
        out = list(iter_records(self.path))
        self.assertEqual([r.obj.get("uuid") for r in out], ["z", "a", "b"])
        # both concatenated objects report the same lineno
        self.assertEqual(out[1].lineno, 2)
        self.assertEqual(out[2].lineno, 2)
        # their offset/length spans parse back to the same objects
        with open(self.path, "rb") as f:
            for rec in out:
                f.seek(rec.offset)
                self.assertEqual(json.loads(f.read(rec.length)), rec.obj)

    def test_malformed_line_skipped_iteration_continues(self):
        self.path = write_lines(['{"uuid":"a"}\n', "{broken\n", '{"uuid":"b"}\n'])
        errors = []
        out = list(iter_records(self.path, on_error=lambda ln, e: errors.append(ln)))
        self.assertEqual([r.obj["uuid"] for r in out], ["a", "b"])
        self.assertEqual(errors, [2])

    def test_blank_lines_ignored(self):
        self.path = write_lines(['{"uuid":"a"}\n', "\n", "   \n", '{"uuid":"b"}\n'])
        out = list(iter_records(self.path))
        self.assertEqual([r.obj["uuid"] for r in out], ["a", "b"])

    def test_no_trailing_newline(self):
        self.path = write_lines(['{"uuid":"a"}\n', '{"uuid":"b"}'])
        out = list(iter_records(self.path))
        self.assertEqual([r.obj["uuid"] for r in out], ["a", "b"])
        with open(self.path, "rb") as f:
            f.seek(out[1].offset)
            self.assertEqual(json.loads(f.read(out[1].length)), {"uuid": "b"})

    def test_invalid_utf8_offsets_stay_byte_exact(self):
        # one raw 0xFF byte inside a JSON string must not inflate the span
        good = b'{"uuid":"a","t":"ok"}\n'
        bad = b'{"uuid":"b","t":"x\xffy"}\n'
        tail = b'{"uuid":"c","t":"ok"}\n'
        self.path = write_lines([good, bad, tail])
        out = list(iter_records(self.path))
        self.assertEqual([r.obj["uuid"] for r in out], ["a", "b", "c"])
        with open(self.path, "rb") as f:
            for rec, expected in zip(out, [good, bad, tail]):
                f.seek(rec.offset)
                self.assertEqual(f.read(rec.length), expected.strip())

    def test_unicode_content_offsets_are_bytes(self):
        objs = [{"uuid": "a", "t": "नमस्ते 🙏"}, {"uuid": "b", "t": "plain"}]
        self.path = write_lines([json.dumps(o, ensure_ascii=False) + "\n" for o in objs])
        out = list(iter_records(self.path))
        with open(self.path, "rb") as f:
            for rec in out:
                f.seek(rec.offset)
                self.assertEqual(json.loads(f.read(rec.length).decode()), rec.obj)


if __name__ == "__main__":
    unittest.main()
