"""dev/sync-gt-src.sh — gt-src gets exactly the checked-in files of the newest release and the one before.

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
PLUGINS = REPO / "dev" / "plugins.py"


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
        shutil.copy(PLUGINS, r / "dev" / "plugins.py")
        # A third plugin proves the sync iterates over what is discovered, not a named pair.
        for plugin, name, versions in (("golden-thread", "gt", ("0.9.8", "0.9.9", "0.10.0")),
                                       ("golden-thread-wiki", "gt-wiki", ("0.1.0", "0.1.1", "0.1.2")),
                                       ("gt-extra", "gt-extra", ("1.0.0", "1.2.0"))):
            for v in versions:
                d = r / plugin / v
                (d / ".claude-plugin").mkdir(parents=True)
                (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name, "version": v}))
                (d / "skills").mkdir()
                (d / "skills" / "note.md").write_text(f"{plugin} {v}\n")
        (r / "install.sh").write_text("#!/bin/sh\necho hi\n")
        (r / "MANUAL.md").write_text("# manual\n")
        (r.parent / "CHANGELOG.md").write_text("# changelog\n")   # a repo-root file
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
        """Paths relative to the published PLUGIN root (gt-src/golden-thread-plugin/ since 0.17.3),
        so the release-selection assertions below read as before; repo-root files keep their
        own path prefixed with '../'."""
        out = []
        for p in self.dest.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(self.dest).as_posix()
            out.append(rel[len("golden-thread-plugin/"):] if rel.startswith("golden-thread-plugin/")
                       else "../" + rel)
        return sorted(out)

    def test_publishes_tracked_files_of_the_newest_release_and_the_one_before(self):
        # Newest + previous travel, so a machine installing from gt-src can roll back with
        # `install.sh <previous>` (owner decision, 2026-09-14). Anything older stays home.
        self.assertOk(self.sync())
        got = self.files()
        self.assertIn("golden-thread/0.10.0/skills/note.md", got, "0.10.0 must beat 0.9.9 numerically")
        self.assertIn("golden-thread/0.9.9/skills/note.md", got, "the release before the newest must travel")
        self.assertNotIn("golden-thread/0.9.8/skills/note.md", got, "two releases back stays home")
        self.assertIn("golden-thread-wiki/0.1.2/skills/note.md", got)
        self.assertIn("golden-thread-wiki/0.1.1/skills/note.md", got)
        self.assertNotIn("golden-thread-wiki/0.1.0/skills/note.md", got)
        self.assertIn("gt-extra/1.2.0/skills/note.md", got, "a third plugin must publish too")
        self.assertIn("gt-extra/1.0.0/skills/note.md", got, "a plugin with two releases ships both")
        self.assertIn("install.sh", got)
        self.assertNotIn("golden-thread-plugin.zip", got, "untracked build artifacts must not publish")
        src = json.loads((self.dest / "SOURCE.json").read_text())
        self.assertEqual((src["gt"], src["gt_wiki"], src["gt_extra"]), ("0.10.0", "0.1.2", "1.2.0"))
        self.assertEqual(src["layout"], "repository")
        # 0.17.3 (owner): gt-src takes the GitHub repo's layout, repo root included
        self.assertIn("../CHANGELOG.md", got, "repo-root files must publish at gt-src's root")

    def test_checksums_let_the_receiving_machine_verify_every_file(self):
        """Owner, 2026-09-28: the installing machine must be able to tell it has the newest
        version of every file. SHA256SUMS covers each published file, and its own sha256 is the
        single tree digest in SOURCE.json."""
        import hashlib, subprocess
        self.assertOk(self.sync("--no-verify"))
        sums = (self.dest / "SHA256SUMS").read_text()
        listed = {l.split("  ", 1)[1][2:] for l in sums.splitlines()}
        on_disk = {p.relative_to(self.dest).as_posix() for p in self.dest.rglob("*")
                   if p.is_file()} - {"SHA256SUMS", "SOURCE.json"}
        self.assertEqual(listed, on_disk, "SHA256SUMS must list exactly the published files")
        src = json.loads((self.dest / "SOURCE.json").read_text())
        self.assertEqual(src["tree_sha256"],
                         hashlib.sha256((self.dest / "SHA256SUMS").read_bytes()).hexdigest())
        ok = subprocess.run(["shasum", "-a", "256", "-c", "--quiet", "SHA256SUMS"],
                            cwd=self.dest, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        # a changed file must be caught by the receiving side's own check
        (self.dest / "golden-thread-plugin" / "install.sh").write_text("tampered\n")
        bad = subprocess.run(["shasum", "-a", "256", "-c", "--quiet", "SHA256SUMS"],
                             cwd=self.dest, capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)

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
        self.assertFalse((self.dest / "golden-thread-plugin" / "MANUAL.md").exists())

    def test_dry_run_writes_nothing(self):
        self.assertOk(self.sync("--dry-run"))
        self.assertEqual(self.files(), [])

    def test_verification_rejects_an_incomplete_tree(self):
        # The toy repo has no selftest.sh, hooks/ or scripts/ — the shape of the
        # half-populated gt-src found on 2026-09-11. Publishing it must not report success.
        proc = self.sync(verify=True)
        self.assertEqual(proc.returncode, 3, proc.stdout)
        self.assertIn("missing golden-thread-plugin/selftest.sh", proc.stdout)
        self.assertIn("is empty", proc.stdout)
        self.assertIn("missing golden-thread-plugin/gt-extra/1.2.0/MANIFEST.json", proc.stdout,
                      "every plugin must arrive with its MANIFEST.json")

    def test_backs_up_and_deletes_stray_files(self):
        self.dest.mkdir()
        (self.dest / "stray.txt").write_text("left by someone else\n")
        self.assertOk(self.sync())
        self.assertNotIn("../stray.txt", self.files())
        backups = list((self.home / ".claude" / "golden-thread" / "backups").glob("gt-src-*.tar.gz"))
        self.assertEqual(len(backups), 1)
        with tarfile.open(backups[0]) as t:
            self.assertIn("./stray.txt", t.getnames())


if __name__ == "__main__":
    unittest.main()
