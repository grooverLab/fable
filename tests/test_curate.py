"""Aperture: timeline read, manual-wall authoring, tool-pair completion,
append-in-place (never rewrite), and vault sealing."""
import glob
import json
import os
import shutil
import tempfile
import unittest

from fable import curate
from tests.helpers import rec, tool_result_block, tool_use_block, write_jsonl


def transcript():
    u1 = rec("u1", "p1", None, "user", text="start the auth work")
    a1 = rec("a1", None, "u1", "assistant", text="running a command")
    a1["message"]["content"].append(tool_use_block("t1", "Bash", {"command": "ls"}))
    tr1 = rec("tr1", None, "a1", "user")
    tr1["message"]["content"] = [tool_result_block("t1", "file listing output")]
    u2 = rec("u2", "p2", "tr1", "user", text="now write the handler")
    a2 = rec("a2", None, "u2", "assistant", text="done, here is the handler")
    return [u1, a1, tr1, u2, a2]


class TestAperture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.live = write_jsonl(os.path.join(self.dir, "s.jsonl"), transcript())
        self.vault = os.path.join(self.dir, "vault", "proj")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_timeline_flags(self):
        rows = curate.timeline(self.live)
        self.assertEqual(len(rows), 5)
        by = {r["uuid"]: r for r in rows}
        self.assertTrue(by["a1"]["has_tool_use"])
        self.assertTrue(by["tr1"]["has_tool_result"])
        self.assertEqual(by["u1"]["role"], "user")
        self.assertIn("t1", by["a1"]["tool_ids"])
        # lane assignment: a tool_result is role:user but must NOT be a
        # user-lane turn — it ran on the machine
        self.assertEqual(by["u1"]["kind"], "user")        # genuine prompt
        self.assertEqual(by["a1"]["kind"], "assistant")   # model + tool_use
        self.assertEqual(by["tr1"]["kind"], "tool")       # role:user, but machine
        self.assertEqual(by["a2"]["kind"], "assistant")

    def test_apply_appends_two_records_never_rewrites(self):
        before = open(self.live).read()
        rep = curate.apply(self.live, ["u2", "a2"], summary_text="kept the handler",
                           backup_dir=self.vault)
        after_lines = open(self.live).read().splitlines()
        # the original bytes are still a prefix — only appended to
        self.assertTrue(open(self.live).read().startswith(before))
        self.assertEqual(len(after_lines), 5 + 2)

        wall, summ = (json.loads(after_lines[-2]), json.loads(after_lines[-1]))
        self.assertIsNone(wall["parentUuid"])               # the wall severs the chain
        self.assertEqual(wall["subtype"], "compact_boundary")
        self.assertEqual(wall["compactMetadata"]["trigger"], "manual")
        self.assertTrue(wall["compactMetadata"]["fableCurated"])
        self.assertEqual(wall["logicalParentUuid"], "a2")   # true predecessor kept
        self.assertEqual(summ["parentUuid"], wall["uuid"])
        self.assertTrue(summ["isCompactSummary"])
        self.assertEqual(summ["uuid"],
                         wall["compactMetadata"]["preservedSegment"]["anchorUuid"])
        self.assertEqual(summ["message"]["content"], "kept the handler")
        self.assertEqual(set(wall["compactMetadata"]["preservedMessages"]["uuids"]),
                         {"u2", "a2"})
        self.assertEqual(rep["resume"], "claude --resume test-session")

    def test_focus_auto_completes_tool_pairs(self):
        # focus ONLY the tool_result — its tool_use turn must be pulled in too,
        # or Claude Code rejects an orphaned tool_result on resume
        curate.apply(self.live, ["tr1"], backup_dir=self.vault)
        wall = json.loads(open(self.live).read().splitlines()[-2])
        uuids = set(wall["compactMetadata"]["preservedMessages"]["uuids"])
        self.assertIn("tr1", uuids)
        self.assertIn("a1", uuids)          # the matching tool_use, auto-included

    def test_seals_a_vault_version_before_appending(self):
        curate.apply(self.live, ["a2"], backup_dir=self.vault)
        sealed = glob.glob(os.path.join(self.vault, "**", "v*.jsonl"),
                           recursive=True)
        self.assertTrue(sealed)
        # the sealed copy is the PRE-curation state (no wall in it)
        body = open(sealed[0]).read()
        self.assertNotIn("compact_boundary", body)

    def test_empty_focus_refused(self):
        with self.assertRaises(ValueError):
            curate.apply(self.live, [], backup_dir=self.vault)

    def test_backup_dir_required(self):
        with self.assertRaises(ValueError):
            curate.apply(self.live, ["a2"], backup_dir=None)

    def test_plan_previews_without_writing(self):
        before = open(self.live).read()
        p = curate.plan(self.live, ["tr1"])
        self.assertEqual(open(self.live).read(), before)      # untouched
        self.assertIn("a1", p["auto_included"])               # pair surfaced
        self.assertGreaterEqual(p["focus_count"], 2)


if __name__ == "__main__":
    unittest.main()
