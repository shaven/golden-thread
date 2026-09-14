"""dev/scrub_check.py — nothing employer- or machine-specific reaches the public tree.

Uses an invented term ('zorblax') so this test file itself contains nothing to scrub.
"""
import unittest
import zipfile

from _harness import Sandbox, REPO

SC = REPO / "dev" / "scrub_check.py"


class ScrubCheckTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.terms = self.tmp / "terms.txt"
        self.terms.write_text("# comment line\n\\bzorblax\\b\nfoo#bar\n", encoding="utf-8")
        self.tree = self.tmp / "tree"
        self.tree.mkdir()

    def scan(self, *paths, terms=True, extra=()):
        env = {"GT_SCRUB_TERMS": str(self.terms)} if terms else {"GT_SCRUB_TERMS": str(self.tmp / "none.txt")}
        return self.py(SC, *(paths or [self.tree]), *extra, env=env)

    def test_clean_tree_passes(self):
        (self.tree / "a.md").write_text("nothing to see\n")
        self.assertEqual(self.scan().returncode, 0)

    def test_hit_in_text_fails_with_location(self):
        (self.tree / "a.md").write_text("line one\nmade by Zorblax corp\n")
        proc = self.scan()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("a.md:2", proc.stdout)

    def test_hit_inside_zip_is_found(self):
        with zipfile.ZipFile(self.tree / "pkg.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("inner/readme.txt", "hello zorblax\n")
        proc = self.scan()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("pkg.zip!inner/readme.txt", proc.stdout)

    def test_hash_inside_pattern_is_not_a_comment(self):
        (self.tree / "a.md").write_text("x foo#bar y\n")
        self.assertEqual(self.scan().returncode, 1)

    def test_missing_terms_file_cannot_pass(self):
        (self.tree / "a.md").write_text("anything\n")
        self.assertEqual(self.scan(terms=False).returncode, 2,
                         "with no terms every tree would pass — that must be exit 2, never clean")

    def test_pdf_without_pypdf_is_unscanned_not_clean(self):
        (self.tree / "doc.pdf").write_bytes(b"%PDF-1.4\n%fake\n")
        proc = self.scan()
        try:
            import pypdf  # noqa: F401
            self.skipTest("pypdf importable here; the unscanned path cannot be exercised")
        except ImportError:
            self.assertEqual(proc.returncode, 2)
            self.assertIn("UNSCANNED", proc.stdout)

    def test_no_pdf_flag_skips_knowingly(self):
        (self.tree / "doc.pdf").write_bytes(b"%PDF-1.4\n%fake\n")
        self.assertEqual(self.scan(extra=("--no-pdf",)).returncode, 0)

    # -- --repo: everything a push publishes (0.12.9) -------------------------------
    # The gate used to scrub a list of plugin directories, and a file at the repository
    # root that named internal systems went public unscanned. These pin the scope.

    def _repo(self):
        repo = self.git_init(self.tmp / "repo", commit=False)
        (repo / "plugin").mkdir()
        (repo / "plugin" / "a.md").write_text("clean\n")
        return repo

    def test_repo_scans_a_committed_file_at_the_repository_root(self):
        repo = self._repo()
        (repo / "HANDOFF.md").write_text("notes about zorblax\n")
        self.run_cmd(["git", "-C", repo, "add", "-A"])
        self.run_cmd(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t",
                      "commit", "-q", "-m", "x"])
        proc = self.scan(extra=("--repo", repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("HANDOFF.md:1", proc.stdout)

    def test_repo_scans_untracked_files_before_they_are_committed(self):
        repo = self._repo()
        (repo / "plugin" / "new.md").write_text("zorblax\n")
        proc = self.scan(extra=("--repo", repo))
        self.assertEqual(proc.returncode, 1,
                         "a new file in an uncommitted release must be scanned before the commit")

    def test_repo_skips_ignored_files(self):
        repo = self._repo()
        (repo / ".gitignore").write_text("local/\n")
        (repo / "local").mkdir()
        (repo / "local" / "scratch.md").write_text("zorblax\n")
        proc = self.scan(extra=("--repo", repo))
        self.assertEqual(proc.returncode, 0, "an ignored file is never pushed: " + proc.stdout)

    def test_repo_that_git_cannot_list_is_unscanned_not_clean(self):
        not_a_repo = self.tmp / "plain"
        not_a_repo.mkdir()
        proc = self.scan(extra=("--repo", not_a_repo))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("UNSCANNED", proc.stdout)

    def test_skips_git_and_pycache(self):
        (self.tree / ".git").mkdir()
        (self.tree / ".git" / "config").write_text("zorblax\n")
        (self.tree / "__pycache__").mkdir()
        (self.tree / "__pycache__" / "x.txt").write_text("zorblax\n")
        self.assertEqual(self.scan().returncode, 0)


if __name__ == "__main__":
    unittest.main()

class ScrubRemotePdf(Sandbox):
    """PDF text needs a real reader; this machine need not be the one that has it.

    No network in tests: these pin the DECISION logic (does it look for a host, does
    it say so when it cannot) rather than a real remote scan.
    """

    def setUp(self):
        super().setUp()
        self.terms = self.tmp / "terms.txt"
        self.terms.write_text("\\bzorblax\\b\n", encoding="utf-8")

    def scrub(self, *args, **kw):
        env = {"GT_SCRUB_TERMS": str(self.terms)}
        env.update(kw.pop("env", {}))
        return self.py(REPO / "dev" / "scrub_check.py", *args, env=env, **kw)

    def _pdf(self):
        p = self.tmp / "x.pdf"
        p.write_bytes(b"%PDF-1.4\n% not a real pdf\n")
        return p

    def test_unscanned_names_the_remedy(self):
        """An UNSCANNED file is not a clean file -- say how to fix it."""
        out = self.scrub(self._pdf()).stdout
        if "pypdf not importable" not in out:
            self.skipTest("pypdf is importable here; the fallback path is not exercised")
        self.assertIn("GT_PDF_HOST", out)
        self.assertIn("pdf_host", out)

    def test_unreachable_host_reports_rather_than_passing(self):
        """A host that cannot be reached must not read as a clean scan."""
        r = self.scrub(self._pdf(), env={"GT_PDF_HOST": "gt-invalid.invalid"})
        out = r.stdout + r.stderr
        if "pypdf not importable" in out:
            self.skipTest("pypdf is importable here")
        self.assertNotEqual(r.returncode, 0,
                            "an unreachable PDF host must not exit clean")
        self.assertIn("UNSCANNED", out, f"no UNSCANNED line; got: {out!r}")
