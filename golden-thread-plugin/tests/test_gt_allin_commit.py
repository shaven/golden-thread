"""gt_allin_commit.py -- committing only once the evidence exists, and never pushing.

Contract:
  * NO PUSH, ever, under any flag. A commit is reversible with `git reset` and nobody else saw
    it; a push is fetched by other people and acted on by CI.
  * The test receipt is checked HERE, not left to guard_test_before_commit. That guard is a
    PreToolUse hook: it sees the MODEL run `git commit` in a Bash call and never sees a SCRIPT
    run it through subprocess, so a script would walk straight past Core rule 4's enforcement.
  * "A check could not run" is fatal and --allow-findings cannot wave it through. An unknown is
    not a finding someone can accept.
  * Refuses on main/master unless told otherwise.
  * --dry-run runs every check and commits nothing.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import GT

SCRIPT = GT / "scripts" / "gt_allin_commit.py"


class CommitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gt-commit-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        (self.repo / "a.py").write_text("def ok():\n    pass\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")
        self.git("checkout", "-q", "-b", "work")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo)] + list(args),
                              capture_output=True, text=True)

    def stage(self, name="b.py", text="def two():\n    pass\n"):
        (self.repo / name).write_text(text, encoding="utf-8")
        self.git("add", "-A")

    def run_commit(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo),
                               "-m", "a message"] + list(args),
                              capture_output=True, text=True)

    def head_count(self):
        out = self.git("rev-list", "--count", "HEAD")
        return int(out.stdout.strip() or 0)

    # -- the push line -------------------------------------------------------------------
    def test_no_push_flag_exists(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                           capture_output=True, text=True)
        self.assertNotIn("--push", r.stdout)

    def test_it_never_runs_git_push(self):
        """Even on the success path, the word push only ever appears as advice to the human."""
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('"push"', src, "no git push argument may be constructed")

    # -- the refusals --------------------------------------------------------------------
    def test_nothing_staged_is_refused(self):
        r = self.run_commit()
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing is staged", r.stdout)

    def test_missing_test_receipt_refuses_and_commits_nothing(self):
        before = self.head_count()
        self.stage()
        r = self.run_commit("--allow-findings")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("REFUSING TO COMMIT", r.stdout)
        self.assertIn("receipt", r.stdout.lower())
        self.assertEqual(self.head_count(), before, "nothing may be committed")

    def test_allow_findings_cannot_wave_through_a_check_that_could_not_run(self):
        """An unknown is not a finding. This is the distinction the whole design rests on."""
        self.stage()
        r = self.run_commit("--allow-findings")
        self.assertEqual(r.returncode, 1)
        joined = r.stdout.lower()
        self.assertTrue("could not run" in joined or "receipt" in joined, r.stdout)

    def test_default_branch_is_refused_without_the_flag(self):
        self.git("checkout", "-q", "-B", "main")
        self.stage()
        r = self.run_commit("--allow-findings")
        self.assertIn("REFUSING TO COMMIT", r.stdout)
        self.assertIn("branch first", r.stdout)

    def test_dry_run_never_commits(self):
        before = self.head_count()
        self.stage()
        self.run_commit("--dry-run", "--allow-findings")
        self.assertEqual(self.head_count(), before)

    def test_it_shows_what_would_be_committed_before_deciding(self):
        self.stage("newfile.py")
        r = self.run_commit("--allow-findings")
        self.assertIn("newfile.py", r.stdout, "the file list comes before the verdict")
        self.assertLess(r.stdout.index("newfile.py"), r.stdout.index("REFUSING"))

    def test_repo_is_required(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "-m", "x"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--repo", r.stderr)

    def test_a_message_is_required(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo)],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
