"""install.sh rolls a failed install back (0.20.0).

Until 0.19.2 a failure after the installer started writing left a half-installed machine -- its
own words were "nothing is rolled back": superseded caches already pruned, settings.json and
installed_plugins.json registered, hooks copied. Now everything the installer may write under the
user's home is copied aside before its first write and put back on any non-zero exit except 4
(the plugin installed completely; only the vault decision is missing).

The failure is forced the way test_install_plugins forces one: the SANDBOX copy of the release
gets a gt_machine_migrate.py that fails, so the install stops with exit 7 -- after the plugin
files, hooks and settings.json registrations are written, and before the vault step.
"""
import hashlib
import json
import os
import shutil
import sys

from _harness import GT, IS_WINDOWS, REPO, WIKI, Sandbox

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
FAILING_MIGRATOR = "import sys\nprint('FAILED forced-for-test')\nsys.exit(1)\n"


def tree_digest(root):
    """{relative path: content hash | 'dir' | 'link -> target'} under root, minus the
    golden-thread backups dir (backups only grow, and are kept by design)."""
    out = {}
    for dirpath, dirs, files in os.walk(str(root)):
        rel_dir = os.path.relpath(dirpath, str(root))
        if rel_dir.replace(os.sep, "/") == "golden-thread":
            dirs[:] = [d for d in dirs if d != "backups"]
        for d in dirs:
            p = os.path.join(dirpath, d)
            rel = os.path.relpath(p, str(root)).replace(os.sep, "/")
            out[rel] = "link -> %s" % os.readlink(p) if os.path.islink(p) else "dir"
        for f in files:
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, str(root)).replace(os.sep, "/")
            if os.path.islink(p):
                out[rel] = "link -> %s" % os.readlink(p)
            else:
                with open(p, "rb") as fh:
                    out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


class RollbackCase(Sandbox):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "src" / "golden-thread-plugin"
        self.repo.mkdir(parents=True)
        shutil.copy2(REPO / "install.sh", self.repo / "install.sh")
        shutil.copytree(GT, self.src, ignore=IGNORE)
        shutil.copytree(WIKI, self.repo / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)
        self.manifest()
        self.tmpdir = self.tmp / "tmpdir"
        self.tmpdir.mkdir()
        self.env["TMPDIR"] = str(self.tmpdir)
        self.claude = self.home / ".claude"

    @property
    def src(self):
        return self.repo / "golden-thread" / GT.name

    def manifest(self):
        self.assertOk(self.py(self.src / "scripts" / "gt_components.py", "manifest", self.src))

    def break_migrations(self):
        (self.src / "scripts" / "gt_machine_migrate.py").write_text(FAILING_MIGRATOR)
        self.manifest()

    def install(self, *args):
        return self.sh(self.repo / "install.sh", *args, timeout=300)

    def user_state(self):
        """What a user who already runs Claude Code has: their own settings, hook and plugin."""
        (self.claude / "plugins").mkdir(parents=True)
        (self.claude / "settings.json").write_text(json.dumps({
            "theme": "dark", "env": {"MY_VAR": "1"},
            "enabledPlugins": {"other@elsewhere": True},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}},
            indent=2) + "\n")
        (self.claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {"other@elsewhere": [{"version": "1.0.0"}]}}) + "\n")
        (self.claude / "plugins" / "known_marketplaces.json").write_text(json.dumps(
            {"elsewhere": {"source": {"source": "directory", "path": "/x"}}}) + "\n")
        (self.claude / "CLAUDE.md").write_text("# mine\n")

    def assertNoSnapshotLeft(self):
        self.assertEqual([p.name for p in self.tmpdir.iterdir()
                          if p.name.startswith("gt-install")], [],
                         "the install left its temp dir or snapshot behind")


class AFailureRestoresTheBeforeState(RollbackCase):
    def test_a_first_install_that_fails_leaves_the_users_setup_untouched(self):
        self.user_state()
        before = tree_digest(self.claude)
        self.break_migrations()
        p = self.install("--no-vault")
        self.assertEqual(p.returncode, 7, p.stdout[-3000:])
        self.assertIn("ROLLED BACK", p.stdout)
        after = tree_digest(self.claude)
        # The one thing left is the backups dir: settings.json was backed up before it was
        # registered, and a backup is never deleted.
        self.assertEqual(sorted(os.listdir(str(self.claude / "golden-thread"))), ["backups"])
        after.pop("golden-thread")
        self.assertEqual(after, before,
                         "a failed install changed the machine:\n" + p.stdout[-2000:])
        self.assertFalse((self.claude / "plugins" / "cache" / "golden-thread-plugin").exists())
        self.assertFalse((self.claude / "golden-thread" / "hooks").exists())
        self.assertNoSnapshotLeft()

    def test_a_failed_upgrade_puts_the_previous_install_back(self):
        self.user_state()
        self.assertOk(self.install("--no-vault"), "the first, good install")
        before = tree_digest(self.claude)
        self.assertIn("golden-thread/hooks/gt_schedule.py", before)
        # Something the next install would change: a hook file edited and a setting moved.
        hook = self.claude / "golden-thread" / "hooks" / "gt_schedule.py"
        hook.write_text(hook.read_text() + "\n# local edit\n")
        before = tree_digest(self.claude)
        self.break_migrations()
        p = self.install("--no-vault")
        self.assertEqual(p.returncode, 7, p.stdout[-3000:])
        self.assertIn("ROLLED BACK", p.stdout)
        self.assertEqual(tree_digest(self.claude), before)
        self.assertNoSnapshotLeft()


class AFailedReleaseGateIsRolledBackToo(RollbackCase):
    def test_exit_9_restores_the_before_state(self):
        # The post-install gate's daily-job row FAILs on a job that runs another script
        # (the fixture test_gt_doctor_postinstall uses for exit 9).
        self.user_state()
        job = {"Label": "com.markethaven.gt-daily",
               "ProgramArguments": [sys.executable, "/elsewhere/gt_daily.py"]}
        if IS_WINDOWS:
            # A Task Scheduler job: gt_schedule's spec beside the .cmd wrapper (job_file).
            jobs = self.claude / "golden-thread" / "jobs"
            jobs.mkdir(parents=True)
            job.update(StandardOutPath=str(jobs.parent / "daily.out"),
                       StandardErrorPath=str(jobs.parent / "daily.err"))
            (jobs / "gt-daily.json").write_bytes(
                (json.dumps(job, indent=1, sort_keys=True) + "\n").encode())
        else:
            import plistlib
            agents = self.home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            with (agents / "com.markethaven.gt-daily.plist").open("wb") as fh:
                plistlib.dump(job, fh)
        before = tree_digest(self.claude)
        p = self.install("--vault", str(self.tmp / "v"))
        self.assertEqual(p.returncode, 9, p.stdout[-3000:])
        self.assertIn("ROLLED BACK", p.stdout)
        after = tree_digest(self.claude)
        # the golden-thread dir itself (its backups/ only grows); what is in it is compared
        after.pop("golden-thread", None)
        before.pop("golden-thread", None)
        self.assertEqual(after, before)
        self.assertFalse((self.claude / "vault-config.json").exists())
        self.assertNoSnapshotLeft()


class WhatIsNotRolledBack(RollbackCase):
    def test_exit_4_keeps_the_completed_plugin_install(self):
        # Not a failure: the plugin is in place and the vault is a decision for the user.
        p = self.install()
        self.assertEqual(p.returncode, 4, p.stdout[-2000:])
        self.assertNotIn("ROLLED BACK", p.stdout)
        self.assertTrue(any((self.claude / "plugins" / "cache" / "golden-thread-plugin")
                            .glob("gt/*/.claude-plugin/plugin.json")))
        self.assertNoSnapshotLeft()

    def test_a_refusal_before_any_write_needs_no_rollback(self):
        self.user_state()
        before = tree_digest(self.claude)
        p = self.install("--with", "no-such-module", "--no-vault")
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("ROLLED BACK", p.stdout)
        self.assertEqual(tree_digest(self.claude), before)
        self.assertNoSnapshotLeft()

    def test_the_exemptions_are_exactly_exit_4_and_a_forced_exit_9(self):
        text = (REPO / "install.sh").read_text(encoding="utf-8")
        self.assertIn('[ "$rc" -ne 0 ] && [ "$rc" -ne 4 ]', text)
        self.assertIn('[ "$rc" -eq 9 ] && [ "${FORCE_MANIFEST:-no}" = yes ]', text)

    def test_a_good_install_leaves_no_snapshot(self):
        self.assertOk(self.install("--no-vault"))
        self.assertNoSnapshotLeft()


if __name__ == "__main__":
    import unittest
    unittest.main()
