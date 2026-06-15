"""Prune slimmers: per-category detection (read-only analysis)."""
import unittest

from fable.slimmers import analyze


def msg(uuid, content):
    return {"uuid": uuid, "message": {"role": "assistant", "content": content}}


class TestSlimmers(unittest.TestCase):
    def test_per_category_detection(self):
        msgs = [
            msg("a", [{"type": "thinking", "thinking": "reasoning " * 20}]),
            msg("b", [{"type": "thinking", "thinking": "x", "signature": "SIG"}]),
            msg("c", [{"type": "tool_use", "id": "r1", "name": "Read",
                       "input": {"file_path": "/x.py"}}]),
            msg("d", [{"type": "tool_result", "tool_use_id": "r1",
                       "content": "OLD READ " * 50}]),
            msg("e", [{"type": "tool_use", "id": "r2", "name": "Read",
                       "input": {"file_path": "/x.py"}}]),
            msg("f", [{"type": "tool_result", "tool_use_id": "r2",
                       "content": "NEW"}]),
            msg("g", [{"type": "tool_use", "id": "e1", "name": "Edit",
                       "input": {"file_path": "/y.py",
                                 "old_string": "a" * 200, "new_string": "b" * 200}}]),
            msg("h", [{"type": "tool_use", "id": "e2", "name": "Edit",
                       "input": {"file_path": "/y.py",
                                 "old_string": "c", "new_string": "d"}}]),
        ]
        cats = {c["key"]: c for c in analyze(msgs)}

        # duplicate reads: earlier result counted, latest kept
        self.assertEqual(cats["dup_reads"]["count"], 1)
        self.assertIn("d", cats["dup_reads"]["uuids"])
        self.assertNotIn("f", cats["dup_reads"]["uuids"])
        self.assertGreater(cats["dup_reads"]["bytes"], 0)

        # superseded edits: earlier edit counted, latest kept
        self.assertEqual(cats["superseded"]["count"], 1)
        self.assertIn("g", cats["superseded"]["uuids"])

        # thinking: only UNSIGNED counted (signed must survive verbatim)
        self.assertEqual(cats["thinking"]["count"], 1)
        self.assertIn("a", cats["thinking"]["uuids"])
        self.assertNotIn("b", cats["thinking"]["uuids"])

        # default toggles: reads/superseded ON, thinking OFF (kept)
        self.assertTrue(cats["dup_reads"]["default"])
        self.assertTrue(cats["superseded"]["default"])
        self.assertFalse(cats["thinking"]["default"])

    def test_no_false_positives_on_single_use(self):
        msgs = [
            msg("c", [{"type": "tool_use", "id": "r1", "name": "Read",
                       "input": {"file_path": "/only.py"}}]),
            msg("d", [{"type": "tool_result", "tool_use_id": "r1",
                       "content": "once"}]),
        ]
        cats = {c["key"]: c for c in analyze(msgs)}
        self.assertEqual(cats["dup_reads"]["count"], 0)
        self.assertEqual(cats["superseded"]["count"], 0)

    def test_apply_stubs_selected_keeps_chain_and_signed(self):
        from fable.slimmers import apply
        STUB = "[fable: slimmed — recover from the vault]"
        msgs = [
            msg("a", [{"type": "thinking", "thinking": "reasoning " * 20}]),
            msg("b", [{"type": "thinking", "thinking": "y", "signature": "SIG"}]),
            msg("c", [{"type": "tool_use", "id": "r1", "name": "Read",
                       "input": {"file_path": "/x.py"}}]),
            msg("d", [{"type": "tool_result", "tool_use_id": "r1",
                       "content": "OLD " * 50}]),
            msg("e", [{"type": "tool_use", "id": "r2", "name": "Read",
                       "input": {"file_path": "/x.py"}}]),
            msg("f", [{"type": "tool_result", "tool_use_id": "r2",
                       "content": "NEW"}]),
            msg("g", [{"type": "tool_use", "id": "e1", "name": "Edit",
                       "input": {"file_path": "/y.py",
                                 "old_string": "a" * 200, "new_string": "b" * 200}}]),
            msg("h", [{"type": "tool_use", "id": "e2", "name": "Edit",
                       "input": {"file_path": "/y.py",
                                 "old_string": "c", "new_string": "d"}}]),
        ]
        out, n = apply(msgs, {"dup_reads", "superseded"})
        self.assertEqual(len(out), len(msgs))                 # chain preserved
        self.assertEqual(out[3]["message"]["content"][0]["content"], STUB)   # old read stubbed
        self.assertEqual(out[5]["message"]["content"][0]["content"], "NEW")  # latest kept
        self.assertEqual(out[6]["message"]["content"][0]["input"]["old_string"], STUB)  # old edit
        self.assertEqual(out[7]["message"]["content"][0]["input"]["old_string"], "c")   # latest edit kept
        self.assertEqual(out[1]["message"]["content"][0]["signature"], "SIG")           # signed untouched
        self.assertNotEqual(out[0]["message"]["content"][0]["thinking"], STUB)          # thinking not selected
        # estimate ↔ action lock-step
        cats = {c["key"]: c for c in analyze(msgs)}
        self.assertEqual(n, cats["dup_reads"]["count"] + cats["superseded"]["count"])

    def test_apply_thinking_keeps_signed(self):
        from fable.slimmers import apply
        msgs = [msg("a", [{"type": "thinking", "thinking": "x" * 50}]),
                msg("b", [{"type": "thinking", "thinking": "y", "signature": "S"}])]
        out, n = apply(msgs, {"thinking"})
        self.assertEqual(n, 1)                                       # only unsigned
        self.assertEqual(out[1]["message"]["content"][0]["thinking"], "y")  # signed kept


if __name__ == "__main__":
    unittest.main()
