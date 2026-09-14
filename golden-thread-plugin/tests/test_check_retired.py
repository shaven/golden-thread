"""dev/check_retired.py — a release that stops installing a hook must record it in retired.json.

Owner requirement (2026-09-14): an install from any older release must converge on a
fresh install. gt-src has no git history, so the release itself has to carry the list of
what to remove. These pin that the gate notices an unrecorded removal.
"""
import json
import shutil
import unittest

from _harness import Sandbox, REPO, GT

CHECK = REPO / "dev" / "check_retired.py"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


class CheckRetired(Sandbox):
    def setUp(self):
        super().setUp()
        self.old = self.tmp / "old"
        self.new = self.tmp / "new"
        shutil.copytree(GT, self.old, ignore=IGNORE)
        shutil.copytree(GT, self.new, ignore=IGNORE)
        self.retired = self.new / "retired.json"
        self.retired.write_text(json.dumps({"hook_files": [], "hook_registrations": []}))

    def check(self):
        return self.py(CHECK, self.new, self.old)

    def test_identical_releases_pass(self):
        self.assertOk(self.check())

    def test_a_hook_file_dropped_without_a_record_fails(self):
        (self.new / "hooks" / "guard_protected_paths.py").unlink()
        p = self.check()
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("guard_protected_paths.py", p.stdout)

    def test_the_same_drop_recorded_in_retired_json_passes(self):
        (self.new / "hooks" / "guard_protected_paths.py").unlink()
        self.retired.write_text(json.dumps({
            "hook_files": [{"source": "hooks/guard_protected_paths.py",
                            "installed_as": "guard_protected_paths.py"}],
            "hook_registrations": []}))
        self.assertOk(self.check())

    def test_missing_retired_json_cannot_pass(self):
        self.retired.unlink()
        self.assertEqual(self.check().returncode, 2,
                         "with no retired.json nothing is checked — that must never read as clean")


if __name__ == "__main__":
    unittest.main()
