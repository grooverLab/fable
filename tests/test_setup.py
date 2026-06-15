"""fable setup: single ~/.fable home, db migration, and Claude Code hook
repair (stale --db flags stripped so hooks resolve the moved db)."""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fable import paths
from fable.setup import repair_hooks, run_setup
from fable import serve
from fable.indexer import index_vault
from fable.extract import fts_extract_fn


class TestSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, ".fable")
        self.claude = os.path.join(self.tmp, ".claude")
        os.makedirs(self.claude)
        self.env = patch.dict(os.environ, {
            "FABLE_HOME": self.home,
            "CLAUDE_CONFIG_DIR": self.claude,
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp)

    def _write_settings(self, db):
        cmd = f"/opt/fable/bin/fable --db {db} hook"
        json.dump({"hooks": {ev: [{"hooks": [{"command": cmd}]}]
                             for ev in ("PreCompact", "SessionStart",
                                        "PostToolUse")}},
                  open(os.path.join(self.claude, "settings.json"), "w"))

    def test_repair_hooks_strips_stale_db_flag(self):
        old_db = "/old/place/fable.db"
        self._write_settings(old_db)
        out = repair_hooks()
        self.assertEqual(out["repaired"], 3)
        data = json.load(open(out["settings"]))
        for groups in data["hooks"].values():
            cmd = groups[0]["hooks"][0]["command"]
            self.assertNotIn("--db", cmd)
            self.assertTrue(cmd.endswith("/bin/fable hook"))
        # idempotent: a second pass finds nothing to do
        self.assertEqual(repair_hooks()["repaired"], 0)
        # a backup of the original was kept
        self.assertTrue(os.path.exists(out["settings"] + ".bak-fable"))

    def test_repair_hooks_quiet_without_settings(self):
        self.assertEqual(repair_hooks()["repaired"], 0)

    def test_run_setup_migrates_db_and_repairs_hooks(self):
        # a db sitting at a legacy location, plus hooks pinned to it
        legacy_db = os.path.join(self.tmp, "legacy", "fable.db")
        os.makedirs(os.path.dirname(legacy_db))
        index_vault(legacy_db, [], live_file=None, extract_fn=fts_extract_fn)
        self._write_settings(legacy_db)

        info = run_setup(migrate_db=legacy_db)

        self.assertEqual(info["db"], paths.default_db())
        self.assertTrue(os.path.exists(paths.default_db()))
        self.assertFalse(os.path.exists(legacy_db))   # moved, not copied
        self.assertEqual(info["hooks_repaired"], 3)   # migration fixed hooks

    def test_post_setup_keeps_existing_old_vault_as_readroot(self):
        # an established vault with real generations to preserve
        old_vault = os.path.join(self.home, "vault")
        os.makedirs(old_vault)
        paths.save_config({"vault": old_vault})
        db = paths.default_db()
        index_vault(db, [], live_file=None, extract_fn=fts_extract_fn)

        new_vault = os.path.join(self.tmp, "chosen-vault")
        res = serve.post_setup(db, {"vault": new_vault})

        self.assertEqual(res["vault"], new_vault)
        cfg = json.load(open(paths.config_path()))
        self.assertEqual(cfg["vault"], new_vault)
        # the previous vault stays a read-root so its history is still visible
        self.assertIn(old_vault, paths.backup_roots())

    def test_post_setup_rejects_empty_vault(self):
        # rejected before the db is touched, so no index needed
        with self.assertRaises(ValueError):
            serve.post_setup(paths.default_db(), {"vault": "  "})


if __name__ == "__main__":
    unittest.main()
