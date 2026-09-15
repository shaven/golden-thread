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


class MovedToAModule(Sandbox):
    """0.15.0: a hook-dir script and its registration moved from gt into a module. That is
    not a retirement -- the module installs the same file -- and a retired.json entry for it
    would make install.sh delete a live module file on every upgrade."""

    def setUp(self):
        super().setUp()
        root = self.tmp / "root"
        self.old = root / "golden-thread" / "0.0.1"
        self.new = root / "golden-thread" / GT.name
        shutil.copytree(GT, self.old, ignore=IGNORE)
        shutil.copytree(GT, self.new, ignore=IGNORE)
        comp = self.new / "scripts" / "gt_components.py"
        text = comp.read_text()
        start = text.index('    {"event": "SessionStart", "script": "gt_push_check.py",')
        end = text.index("},", start) + 2
        text = text[:start] + text[end:]
        text = text.replace('                    "gt_push_check.py",\n', "", 1)
        comp.write_text(text)
        self.retired = self.new / "retired.json"
        self.retired.write_text(json.dumps({"hook_files": [], "hook_registrations": []}))
        self.mod = root / "golden-thread-mover" / "1.0.0"
        (self.mod / ".claude-plugin").mkdir(parents=True)
        (self.mod / "scripts").mkdir()
        (self.mod / "scripts" / "gt_push_check.py").write_text("# moved\n")
        (self.mod / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "gt-mover", "version": "1.0.0"}))
        self.module(requires=">=%s" % GT.name)

    def module(self, requires):
        (self.mod / "module.json").write_text(json.dumps({
            "schema": 1, "name": "mover", "plugin": "gt-mover", "version": "1.0.0",
            "requires_gt": requires, "summary": "moved", "default": "on",
            "hooks": [{"event": "SessionStart", "script": "gt_push_check.py",
                       "args": ["check", "--hook"], "kind": "reporter"}],
            "hookdir_scripts": ["gt_push_check.py"]}))

    def check(self):
        return self.py(CHECK, self.new, self.old)

    def test_fixture_really_dropped_it_from_gt(self):
        shutil.rmtree(self.mod.parent)
        p = self.check()
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("no longer installs gt_push_check.py", p.stdout)
        self.assertIn("no longer registers gt_push_check.py", p.stdout)

    def test_a_module_declaring_it_passes_with_no_retired_entry(self):
        self.assertOk(self.check())

    def test_a_retired_entry_for_a_module_file_fails(self):
        self.retired.write_text(json.dumps({
            "hook_files": [{"source": "scripts/gt_push_check.py",
                            "installed_as": "gt_push_check.py"}],
            "hook_registrations": ["gt_push_check.py"]}))
        p = self.check()
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("module mover installs it", p.stdout)
        self.assertIn("module mover registers it", p.stdout)

    def test_a_module_that_does_not_admit_the_new_gt_does_not_count(self):
        self.module(requires=">=99.0.0")
        self.assertEqual(self.check().returncode, 1)

    def test_the_real_release_records_no_module_file_as_retired(self):
        data = json.loads((GT / "retired.json").read_text())
        names = {e.get("installed_as") for e in data.get("hook_files", [])}
        self.assertFalse(names & {"gt_watch.py", "gt_report_card.py"}, names)
        self.assertFalse(set(data.get("hook_registrations", []))
                         & {"gt_watch.py", "gt_report_card.py"})


if __name__ == "__main__":
    unittest.main()
