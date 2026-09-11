"""dev/sync-gt-src.sh — gt-src gets exactly the checked-in files of the newest release.

Runs against a throwaway git repo shaped like the plugin root, never the real one.
"""
import json
import shutil
import tarfile
import unittest
from pathlib import Path

from _harness import Sandbox, REPO

SYNC = REPO / "dev" / "sync-gt-src.sh"
SCRUB = REPO / "dev" / "scrub_check.py"


class SyncGtSrcTest(Sandbox):
    def setUp(self):
        super().setUp()
        if not shutil.which("rsync") or not shutil.which("git"):
            self.skipTest("rsync and git required")
        self.terms = self.tmp / "terms.txt"
        self.terms.write_text("\\bzorblax\\b\n")
        r = self.tmp / "repo" / "golden-thread-plugin"
        (r / "dev").mkdir(parents=True)
        shutil.copy(SYNC, r / "dev" / "sync-gt-src.sh")
        shutil.copy(SCRUB, r / "dev" / "scrub_check.py")
        for plugin, versions in (("golden-thread", ("0.9.9", "0.10.0")), ("golden-thread-wiki", ("0.1.0", "0.1.1"))):
            for v in versions:
                d = r / plugin / v
                (d / ".claude-plugin").mkdir(parents=True)
                (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": v}))
                (d / "skills").mkdir()
                (d / "skills" / "note.md").write_text(f"{plugin} {v}\n")
        (r / "install.sh").write_text("#!/bin/sh\necho hi\n")
        (r / "MANUAL.md").write_text("# manual\n")
        (r / "golden-thread-plugin.zip").write_text("build artifact\n")
        (r / ".gitignore").write_text("*.zip\n")
        self.repo = r
        self.git_init(self.tmp / "repo")
        self.dest = self.tmp / "gt-src"

    def sync(self, *args, verify=False):
        args = args if verify else (*args, "--no-verify")
        return self.sh(self.repo / "dev" / "sync-gt-src.sh", *args,
                       env={"GT_SRC": str(self.dest), "GT_SCRUB_TERMS": str(self.terms)})

    def files(self):
        return sorted(str(p.relative_to(self.dest)) for p in self.dest.rglob("*") if p.is_file())

    def test_publishes_only_tracked_files_of_the_newest_release(self):
        self.assertOk(self.sync())
        got = self.files()
        self.assertIn("golden-thread/0.10.0/skills/note.md", got, "0.10.0 must beat 0.9.9 numerically")
        self.assertNotIn("golden-thread/0.9.9/skills/note.md", got)
        self.assertIn("golden-thread-wiki/0.1.1/skills/note.md", got)
        self.assertNotIn("golden-thread-wiki/0.1.0/skills/note.md", got)
        self.assertIn("install.sh", got)
        self.assertNotIn("golden-thread-plugin.zip", got, "untracked build artifacts must not publish")
        src = json.loads((self.dest / "SOURCE.json").read_text())
        self.assertEqual((src["gt"], src["gt_wiki"]), ("0.10.0", "0.1.1"))

    def test_refuses_dirty_tree(self):
        (self.repo / "MANUAL.md").write_text("# edited\n")
        proc = self.sync()
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(self.dest.exists() and any(self.dest.iterdir()))

    def test_scrub_hit_aborts_publish(self):
        (self.repo / "MANUAL.md").write_text("built for zorblax\n")
        self.run_cmd(["git", "-C", self.tmp / "repo", "commit", "-qam", "leak"])
        proc = self.sync()
        self.assertEqual(proc.returncode, 1)
        self.assertFalse((self.dest / "MANUAL.md").exists())

    def test_dry_run_writes_nothing(self):
        self.assertOk(self.sync("--dry-run"))
        self.assertEqual(self.files(), [])

    def test_verification_rejects_an_incomplete_tree(self):
        # The toy repo has no selftest.sh, hooks/ or scripts/ — the shape of the
        # half-populated gt-src found on 2026-09-11. Publishing it must not report success.
        proc = self.sync(verify=True)
        self.assertEqual(proc.returncode, 3, proc.stdout)
        self.assertIn("missing selftest.sh", proc.stdout)
        self.assertIn("is empty", proc.stdout)

    def test_backs_up_and_deletes_stray_files(self):
        self.dest.mkdir()
        (self.dest / "stray.txt").write_text("left by someone else\n")
        self.assertOk(self.sync())
        self.assertNotIn("stray.txt", self.files())
        backups = list((self.home / ".claude" / "golden-thread" / "backups").glob("gt-src-*.tar.gz"))
        self.assertEqual(len(backups), 1)
        with tarfile.open(backups[0]) as t:
            self.assertIn("./stray.txt", t.getnames())


if __name__ == "__main__":
    unittest.main()
