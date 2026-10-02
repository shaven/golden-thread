"""gt_upgrade backups never overwrite each other (0.18.1).

Backup names are stamped to the second. Two upgrades inside one second -- an install straight
after another, which the faster cached test install made routine -- replaced the earlier
tarball, and test_install_vault_upgrade began failing intermittently with "no backup tarball".
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _harness import REPO, latest_version_dir

GT = latest_version_dir(REPO / "golden-thread")


def load():
    sys.path.insert(0, str(GT / "scripts"))
    spec = importlib.util.spec_from_file_location("gt_upgrade_t", GT / "scripts" / "gt_upgrade.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class BackupNames(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text("x")
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home")})
        self.env.start()
        self.m = load()

    def tearDown(self):
        self.env.stop()

    def test_two_backups_in_one_second_are_both_kept(self):
        fixed = self.m.datetime.datetime(2026, 10, 1, 22, 0, 0)
        with mock.patch.object(self.m.datetime, "datetime", wraps=self.m.datetime.datetime) as dt:
            dt.now.return_value = fixed
            a = self.m._backup(self.vault)
            b = self.m._backup(self.vault)
        self.assertNotEqual(a, b)
        self.assertTrue(Path(a).is_file() and Path(b).is_file())
        self.assertTrue(b.endswith("vault-20261001-220000-2.tar.gz"), b)

    def test_unused_counts_up_past_every_taken_name(self):
        d = self.tmp / "d"
        d.mkdir()
        for n in ("x.base.1", "x.base.1-2", "x.base.1-3"):
            (d / n).write_text("")
        self.assertEqual(self.m._unused(d, "x.base.1").name, "x.base.1-4")


if __name__ == "__main__":
    unittest.main()
