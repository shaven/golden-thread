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

    def test_skips_git_and_pycache(self):
        (self.tree / ".git").mkdir()
        (self.tree / ".git" / "config").write_text("zorblax\n")
        (self.tree / "__pycache__").mkdir()
        (self.tree / "__pycache__" / "x.txt").write_text("zorblax\n")
        self.assertEqual(self.scan().returncode, 0)


if __name__ == "__main__":
    unittest.main()
