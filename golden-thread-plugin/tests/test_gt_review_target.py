"""gt_review_target (0.20.1): the broker escalates design.md and global-memory/ by IDENTITY.

Until 0.20.1 the broker matched `name == "design.md"` / `startswith("global-memory/")` as exact
strings. On macOS APFS (case-insensitive, Unicode-normalising) a different spelling of the same
file, or a symlink to it, was APPLIED instead of escalated. These tests drive the real queue and
the real drain (`gt_write_queue.py` then `gt_broker.py drain`), which is also what the sandbox
inbox route ends in, and assert the target is untouched and the decision is `escalate`.

Case and U+017F spellings are different files on a case-sensitive volume (Linux), yet are
escalated there too: the spelling layer applies on every platform, deliberately (see the module
docstring). Symlink cases run wherever symlinks can be made (Windows without the privilege skips
them, with that reason).
"""
import json
import os
import unittest

from _harness import SCRIPTS, load_module, IS_WINDOWS
from test_gt_broker import BrokerBase

LONG_S = "deſign.md"
DESIGN_TEXT = "# design\n\noriginal\n"
GM_TEXT = "# memory\n\noriginal\n"


def can_symlink(tmp):
    src, dst = tmp / "_probe_target", tmp / "_probe_link"
    try:
        src.write_text("x")
        os.symlink(str(src), str(dst))
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        for p in (dst, src):
            try:
                p.unlink()
            except OSError:
                pass


class ReviewTargetsByIdentity(BrokerBase):
    def setUp(self):
        super().setUp()
        self.proj = self.vault / "Projects" / "quokka"
        (self.proj / "design.md").write_text(DESIGN_TEXT)
        self.gm = self.vault / "global-memory"
        self.gm.mkdir()
        (self.gm / "MEMORY.md").write_text(GM_TEXT)
        self.symlinks = can_symlink(self.tmp)

    def assertEscalated(self, rel, reason_has=None, op="append"):
        self.submit("- smuggled line", path=rel, op=op)
        self.drain()
        rows = [r for r in self.log_rows() if r["path"] == rel]
        self.assertEqual([r["decision"] for r in rows], ["escalate"],
                         "%s was not escalated: %s" % (rel, rows))
        if reason_has:
            self.assertIn(reason_has, rows[0]["reason"])
        self.assertEqual(self.queued(), [])
        self.assertNotIn("smuggled", (self.proj / "design.md").read_text())
        self.assertNotIn("smuggled", (self.gm / "MEMORY.md").read_text())
        return rows[0]

    def assertApplied(self, rel):
        self.submit("- ordinary line", path=rel)
        self.drain(expect=0)
        rows = [r for r in self.log_rows() if r["path"] == rel]
        self.assertEqual([r["decision"] for r in rows], ["apply"], rows)
        self.assertIn("ordinary line", (self.vault / rel).read_text())

    # -- the reviewer's vectors
    def test_the_plain_names_still_escalate(self):
        self.assertEscalated("Projects/quokka/design.md", "review target")
        self.assertEscalated("global-memory/MEMORY.md", "global-memory/")

    def test_upper_case_design_md(self):
        self.assertEscalated("Projects/quokka/DESIGN.md", "design.md")

    def test_long_s_design_md(self):
        self.assertEscalated("Projects/quokka/" + LONG_S, "design.md")

    def test_upper_case_global_memory(self):
        self.assertEscalated("GLOBAL-MEMORY/MEMORY.md", "global-memory/")

    def test_a_new_file_under_global_memory_variants_escalates(self):
        self.assertEscalated("Global-Memory/new-note.md", "global-memory/", op="create")

    @unittest.skipIf(IS_WINDOWS, "symlinks need a privilege Windows tests do not assume; the "
                                 "unit tests below cover the resolution")
    def test_symlink_dir_to_global_memory(self):
        if not self.symlinks:
            self.skipTest("cannot make symlinks here")
        os.symlink(os.path.join("..", "..", "global-memory"), str(self.proj / "gmlink"))
        self.assertEscalated("Projects/quokka/gmlink/MEMORY.md")

    @unittest.skipIf(IS_WINDOWS, "symlinks need a privilege Windows tests do not assume")
    def test_symlink_file_to_design_md(self):
        if not self.symlinks:
            self.skipTest("cannot make symlinks here")
        os.symlink("design.md", str(self.proj / "link.md"))
        self.assertEscalated("Projects/quokka/link.md", "design.md")

    @unittest.skipIf(IS_WINDOWS, "symlinks need a privilege Windows tests do not assume")
    def test_symlink_in_another_folder_to_design_md(self):
        if not self.symlinks:
            self.skipTest("cannot make symlinks here")
        other = self.vault / "Projects" / "other"
        other.mkdir()
        (other / "README.md").write_text("# other\n")
        os.symlink(os.path.join("..", "quokka", "design.md"), str(other / "notes.md"))
        self.assertEscalated("Projects/other/notes.md")

    @unittest.skipIf(IS_WINDOWS, "symlinks need a privilege Windows tests do not assume")
    def test_a_dangling_symlink_is_escalated_not_written_through(self):
        if not self.symlinks:
            self.skipTest("cannot make symlinks here")
        os.symlink("nothing-here.md", str(self.proj / "dangling.md"))
        self.assertEscalated("Projects/quokka/dangling.md", "dangling")

    @unittest.skipIf(IS_WINDOWS, "symlinks need a privilege Windows tests do not assume")
    def test_a_symlink_leaving_its_folder_is_escalated(self):
        if not self.symlinks:
            self.skipTest("cannot make symlinks here")
        (self.vault / "Projects" / "quokka" / "sub").mkdir()
        os.symlink(os.path.join("..", "research.md"), str(self.proj / "sub" / "r.md"))
        self.assertEscalated("Projects/quokka/sub/r.md", "leaves its own directory")

    # -- negatives: a name that merely CONTAINS the word is an ordinary file
    def test_lookalike_names_are_ordinary_files(self):
        for rel in ("Projects/quokka/xdesign.md", "Projects/quokka/design-notes.md",
                    "Projects/quokka/notes/global-memory/x.md",
                    "global-memory-x/notes.md", "Projects/quokka/mydesign.md"):
            (self.vault / rel).parent.mkdir(parents=True, exist_ok=True)
            self.assertApplied(rel)

    def test_an_ordinary_new_file_is_still_applied(self):
        self.assertApplied("Projects/quokka/notes.md")


class FoldUnit(unittest.TestCase):
    """The spelling layer, in process: the forms a file system folds that no queue request can
    carry (a path must end in .md, so `design.md.` or `design.md::$DATA` never reach a drain)."""

    @classmethod
    def setUpClass(cls):
        cls.rt = load_module(SCRIPTS / "gt_review_target.py", "gt_review_target_unit")

    def test_spellings_that_are_design_md(self):
        for name in ("design.md", "DESIGN.md", "Design.MD", LONG_S, " design.md",
                     "design.md.", "design.md ", "design.md::$DATA", "design.md:$DATA",
                     "de​sign.md", "DESIGN~1.MD", "design~2.md", "ｄesign.md"):
            self.assertTrue(self.rt._is_design(name), repr(name))

    def test_spellings_that_are_global_memory(self):
        for name in ("global-memory", "GLOBAL-MEMORY", "Global-Memory.", "GLOBAL~1",
                     "global​-memory"):
            self.assertTrue(self.rt._is_gm(name), repr(name))

    def test_names_that_are_not(self):
        for name in ("xdesign.md", "design.md.bak", "design-notes.md", "designs.md",
                     "design.mdx", "global-memory-x", "xglobal-memory", "global", "memory"):
            self.assertFalse(self.rt._is_design(name), repr(name))
            self.assertFalse(self.rt._is_gm(name), repr(name))

    def test_review_target_on_a_real_tree(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "vault"
            (v / "Projects" / "p").mkdir(parents=True)
            (v / "global-memory").mkdir()
            self.assertEqual(self.rt.review_target(v, "Projects/p/DESIGN~1.MD")[0], "design.md")
            self.assertEqual(self.rt.review_target(v, "GLOBAL~1/x.md")[0], "global-memory/")
            self.assertIsNone(self.rt.review_target(v, "Projects/p/xdesign.md"))
            self.assertIsNone(self.rt.review_target(v, "global-memory-x/a.md"))
            self.assertIsNone(self.rt.review_target(v, "Projects/p/global-memory/a.md"))

    # -- claim matching (0.20.1): one file, however it is spelled or reached
    def test_same_path_and_claim_covers(self):
        import tempfile
        from pathlib import Path
        rt = self.rt
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "vault"
            (v / "Projects" / "p" / "notes").mkdir(parents=True)
            (v / "Projects" / "p" / "research.md").write_text("x")
            claimed = "Projects/p/research.md"
            for variant in ("Projects/p/RESEARCH.md", "PROJECTS/P/Research.MD",
                            "Projects/p/re\u017fearch.md", "Projects/p/research.md.",
                            "Projects/p/research.md ", "Projects/p/research.md::$DATA",
                            "Projects/p/re\u200bsearch.md", "Projects/p/./research.md"):
                self.assertTrue(rt.same_path(v, claimed, variant), repr(variant))
                self.assertTrue(rt.claim_covers(v, claimed, variant), repr(variant))
            for other in ("Projects/p/research2.md", "Projects/p/xresearch.md",
                          "Projects/q/research.md", "Projects/p/research.md.bak"):
                self.assertFalse(rt.same_path(v, claimed, other), other)
                self.assertFalse(rt.claim_covers(v, claimed, other), other)
            self.assertTrue(rt.claim_covers(v, "Projects/p/notes", "Projects/P/NOTES/a.md"))
            self.assertTrue(rt.claim_covers(v, "Projects/p/notes/", "Projects/p/notes/sub/a.md"))
            self.assertFalse(rt.claim_covers(v, "Projects/p/notes", "Projects/p/notes-x/a.md"))
            self.assertFalse(rt.claim_covers(v, "", "Projects/p/notes/a.md"))

    def test_a_symlink_to_a_claimed_file_or_into_a_claimed_directory_is_the_same_path(self):
        import tempfile
        from pathlib import Path
        rt = self.rt
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "vault"
            (v / "Projects" / "p" / "notes").mkdir(parents=True)
            (v / "Projects" / "q").mkdir()
            (v / "Projects" / "p" / "research.md").write_text("x")
            try:
                os.symlink(os.path.join("..", "p", "research.md"),
                           str(v / "Projects" / "q" / "alias.md"))
                os.symlink(os.path.join("..", "p", "notes"), str(v / "Projects" / "q" / "nlink"),
                           target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("cannot make symlinks here (Windows without the privilege)")
            self.assertTrue(rt.same_path(v, "Projects/p/research.md", "Projects/q/alias.md"))
            self.assertTrue(rt.claim_covers(v, "Projects/p/notes", "Projects/q/nlink/new.md"))
            self.assertFalse(rt.same_path(v, "Projects/p/research.md", "Projects/q/other.md"))

    @unittest.skipUnless(IS_WINDOWS, "8.3 short names exist only on Windows (NTFS)")
    def test_an_8_3_short_name_of_a_claimed_file_is_the_same_path(self):
        import ctypes
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "vault"
            (v / "Projects" / "p").mkdir(parents=True)
            long = v / "Projects" / "p" / "a-long-research-note.md"
            long.write_text("x")
            buf = ctypes.create_unicode_buffer(1024)
            n = ctypes.windll.kernel32.GetShortPathNameW(str(long), buf, 1024)
            short = os.path.basename(buf.value) if n else ""
            if not short or short.lower() == long.name.lower():
                self.skipTest("this volume does not generate 8.3 short names")
            self.assertTrue(self.rt.same_path(v, "Projects/p/" + long.name, "Projects/p/" + short))
            self.assertTrue(self.rt.claim_covers(v, "Projects/p/" + long.name,
                                                 "Projects/p/" + short))

    def test_the_two_shipped_copies_are_identical_and_installed(self):
        from _harness import TOOLS
        self.assertEqual((SCRIPTS / "gt_review_target.py").read_bytes(),
                         (TOOLS / "gt_review_target.py").read_bytes())
        comp = load_module(SCRIPTS / "gt_components.py", "gt_components_rt")
        self.assertIn("gt_review_target.py", comp.HOOK_DIR_SCRIPTS)


if __name__ == "__main__":
    unittest.main()
