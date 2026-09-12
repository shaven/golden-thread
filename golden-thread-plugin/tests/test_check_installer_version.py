"""dev/check_installer_version.py — changing the installer requires a version bump.

`install.sh` is the file a user RUNS, and it is not covered by MANIFEST.json: the
manifest hashes hooks/, scripts/ and templates/ INSIDE a version directory, and the
installer sits at the repo root. On 2026-09-12 an installer bug was fixed and committed
without a bump, so two published states both called themselves 0.12.2 — one that wires
the enforcement hooks and one that does not.

Contract:
  * exit 1, naming the commits, when a watched file changed after the newest version
    directory was added, or is dirty now;
  * exit 0 when it has not;
  * exit 0 with an explicit "cannot tell" outside a git checkout — never a false clean.
"""
import subprocess
import unittest

from _harness import Sandbox, REPO


CHECK = REPO / "dev" / "check_installer_version.py"


class InstallerVersion(Sandbox):
    def fake_repo(self, version="1.0.0"):
        """A miniature plugin root with real git history."""
        root = self.tmp / "plugin"
        (root / "golden-thread" / version / ".claude-plugin").mkdir(parents=True)
        (root / "golden-thread" / version / ".claude-plugin" / "plugin.json").write_text(
            '{"name": "gt", "version": "%s"}\n' % version)
        (root / "install.sh").write_text("#!/bin/sh\necho install\n")
        (root / "selftest.sh").write_text("#!/bin/sh\necho selftest\n")
        self.git_init(root)
        self.commit(root, "cut %s" % version)
        return root

    def commit(self, root, message):
        env = {**self.env, "HOME": str(self.home)}
        subprocess.run(["git", "-C", str(root), "add", "-A"], capture_output=True, env=env)
        subprocess.run(["git", "-C", str(root), "commit", "-m", message],
                       capture_output=True, env=env)

    def check(self, root):
        return self.py(CHECK, str(root))

    def test_clean_when_the_installer_has_not_changed(self):
        root = self.fake_repo()
        p = self.check(root)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("unchanged", p.stdout)

    def test_the_2026_09_12_mistake_is_caught(self):
        """Fix the installer, commit it, cut no version — exactly what happened."""
        root = self.fake_repo()
        (root / "install.sh").write_text("#!/bin/sh\necho install\necho fixed a bug\n")
        self.commit(root, "fix the installer wiring")
        p = self.check(root)
        self.assertEqual(p.returncode, 1, "an installer change with no bump passed")
        self.assertIn("install.sh changed", p.stdout)
        self.assertIn("1.0.0", p.stdout, "it must name the release that is now wrong")

    def test_uncommitted_installer_changes_are_caught(self):
        root = self.fake_repo()
        (root / "install.sh").write_text("#!/bin/sh\necho edited but not committed\n")
        p = self.check(root)
        self.assertEqual(p.returncode, 1)
        self.assertIn("uncommitted", p.stdout)

    def test_selftest_is_watched_too(self):
        """It is what someone runs to decide whether an install is healthy."""
        root = self.fake_repo()
        (root / "selftest.sh").write_text("#!/bin/sh\necho changed\n")
        self.commit(root, "change selftest")
        p = self.check(root)
        self.assertEqual(p.returncode, 1)
        self.assertIn("selftest.sh", p.stdout)

    def test_a_bump_clears_it(self):
        """Cutting the new version in the same commit is the fix, and it must pass."""
        root = self.fake_repo()
        (root / "install.sh").write_text("#!/bin/sh\necho install\necho fixed\n")
        newdir = root / "golden-thread" / "1.0.1" / ".claude-plugin"
        newdir.mkdir(parents=True)
        (newdir / "plugin.json").write_text('{"name": "gt", "version": "1.0.1"}\n')
        self.commit(root, "0.0.1: name the installer fix")
        p = self.check(root)
        self.assertEqual(p.returncode, 0, "a proper bump was still reported as a problem:\n"
                         + p.stdout)

    def test_outside_git_it_says_so_rather_than_clean(self):
        root = self.tmp / "nogit"
        (root / "golden-thread" / "1.0.0" / ".claude-plugin").mkdir(parents=True)
        (root / "golden-thread" / "1.0.0" / ".claude-plugin" / "plugin.json").write_text(
            '{"version": "1.0.0"}\n')
        (root / "install.sh").write_text("#!/bin/sh\n")
        p = self.check(root)
        self.assertEqual(p.returncode, 0)
        self.assertIn("cannot tell", p.stdout,
                      "outside a checkout it must not claim to have verified anything")

    def test_missing_installer_is_a_usage_error(self):
        root = self.tmp / "empty"
        root.mkdir()
        self.assertEqual(self.check(root).returncode, 2)


if __name__ == "__main__":
    unittest.main()
