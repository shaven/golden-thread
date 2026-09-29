"""dev/tree_is_commit.py — the working tree the tests ran on must be the commit that ships.

The defect it exists for: an EMPTY packs/community/ was present in every tested tree and absent
from every commit (git tracks files, never directories), so 0.16.0–0.17.3 passed their gates and
failed to install from gt-src. `git status` called the tree clean; this must not.
"""
import unittest

from _harness import Sandbox, REPO

TOOL = REPO / "dev" / "tree_is_commit.py"


class TreeIsCommit(Sandbox):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        (self.repo / "pkg").mkdir(parents=True)
        (self.repo / "pkg" / "a.py").write_text("x = 1\n")
        (self.repo / ".gitignore").write_text("__pycache__/\n*.zip\n")
        self.git_init(self.repo)

    def check(self):
        return self.py(TOOL, self.repo)

    def test_a_clean_commit_matches(self):
        p = self.check()
        self.assertOk(p)
        self.assertIn("working tree matches HEAD", p.stdout)

    def test_an_empty_directory_is_caught_although_git_status_is_clean(self):
        (self.repo / "pkg" / "community").mkdir()
        status = self.run_cmd(["git", "-C", self.repo, "status", "--porcelain"])
        self.assertEqual(status.stdout.strip(), "", "the premise: git status cannot see it")
        p = self.check()
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("pkg/community/", p.stdout)

    def test_an_untracked_file_is_caught(self):
        (self.repo / "pkg" / "b.py").write_text("y = 2\n")
        p = self.check()
        self.assertEqual(p.returncode, 1)
        self.assertIn("pkg/b.py", p.stdout)

    def test_ignored_paths_are_expected_to_differ(self):
        (self.repo / "pkg" / "__pycache__").mkdir()
        (self.repo / "pkg" / "__pycache__" / "a.pyc").write_text("x")
        (self.repo / "build.zip").write_text("x")
        self.assertOk(self.check())

    def test_warn_reports_without_refusing_and_exclude_ignores(self):
        (self.repo / "old").mkdir()
        (self.repo / "old" / "empty").mkdir()
        p = self.py(TOOL, self.repo, "--warn", "old")
        self.assertOk(p)
        self.assertIn("WARNING", p.stdout)
        p = self.py(TOOL, self.repo, "--exclude", "old")
        self.assertOk(p)
        self.assertNotIn("old", p.stdout)


if __name__ == "__main__":
    unittest.main()
