"""gt_optimize.py -- finding content that costs context and earns nothing back.

Contract:
  * IT NEVER WRITES. There was an --apply, restricted to a "safe" class, and an independent
    validation took it apart on 2026-09-16: it deleted LIVE index rows (URL-encoded names,
    titled links, mailto:, obsidian://, filenames with brackets -- seven of eight live rows in
    the fixture), rewrote the inside of fenced code blocks, converted CRLF files wholesale,
    turned mode 0600 into 0644, and applied stale line offsets to a file edited in between.
    Every one was a case the author had not considered, which is the point: "safe" was a claim
    about the world and the world kept producing exceptions.
  * --project is a SLUG. Joined unvalidated it walked out of the vault entirely, and a
    symlinked project root reached core-rules/ and global-memory/ past their refusals.
  * Sources/ is never proposed, at any depth.
  * The valuable findings were always the judgement ones anyway; they needed a reader from the
    start.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import GT

SCRIPT = GT / "scripts" / "gt_optimize.py"

LONG = "This sentence is long enough to count as a real fact worth deduplicating."


def tree_digest(root):
    """Every file's path and bytes, so 'nothing changed' is proved, not assumed."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            h.update(os.path.relpath(p, root).encode())
            h.update(open(p, "rb").read())
    return h.hexdigest()


class OptimizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gt-opt-"))
        self.vault = self.tmp / "vault"
        for d in ("global-memory", "Sources", "Knowledge",
                  "Projects/alpha/memory", "Projects/golden-thread/core-rules"):
            (self.vault / d).mkdir(parents=True)

    def write(self, rel, text):
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def run_opt(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "--vault", str(self.vault)]
                              + list(args), capture_output=True, text=True)

    def findings(self, *args):
        r = self.run_opt("--json", *args)
        self.assertIn(r.returncode, (0, 1, 3), r.stderr)
        return json.loads(r.stdout)

    # -- the reporting/acting line ------------------------------------------------------
    def test_it_never_writes_anything(self):
        self.write("Projects/alpha/memory/a.md", "%s\n%s\n" % (LONG, LONG))
        before = tree_digest(self.vault)
        for extra in ([], ["--json"], ["--project", "alpha"]):
            self.run_opt(*extra)
        self.assertEqual(tree_digest(self.vault), before, "this tool must never edit anything")

    def test_there_is_no_apply_flag(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                           capture_output=True, text=True)
        self.assertNotIn("--apply", r.stdout)
        self.assertNotIn("--include-global-memory", r.stdout)

    def test_project_must_be_a_slug_not_a_path(self):
        """`--project ../../elsewhere` edited files outside the vault entirely."""
        for bad in ("../../outside", "a/b", ".."):
            r = self.run_opt("--project", bad)
            self.assertEqual(r.returncode, 2, "%s must be refused: %s" % (bad, r.stdout))
            self.assertIn("slug", (r.stderr + r.stdout).lower())

    def test_a_symlinked_project_cannot_reach_a_protected_directory(self):
        os.symlink(str(self.vault / "Projects" / "golden-thread" / "core-rules"),
                   str(self.vault / "Projects" / "cr"))
        r = self.run_opt("--project", "cr")
        self.assertIn(r.returncode, (0, 1, 2), r.stdout)
        before = tree_digest(self.vault)
        self.run_opt("--project", "cr")
        self.assertEqual(tree_digest(self.vault), before)

    def test_a_line_inside_a_fenced_block_is_never_flagged(self):
        self.write("Projects/alpha/memory/q.md",
                   "```\n%s\n%s\n```\n" % (LONG, LONG))
        out = self.findings()
        self.assertEqual([f for f in out["findings"] if f["kind"] == "duplicate-line"], [])

    def test_a_template_section_label_is_never_flagged(self):
        self.write("Projects/alpha/decisions.md",
                   "- **Rejected alternatives**:\ntext one\n- **Rejected alternatives**:\n")
        out = self.findings()
        self.assertEqual([f for f in out["findings"] if f["kind"] == "duplicate-line"], [])

    def test_sources_are_never_touched_or_even_proposed(self):
        self.write("Sources/raw.md", "%s\n%s\n" % (LONG, LONG))
        out = self.findings()
        self.assertEqual([f for f in out["findings"] if f["path"].startswith("Sources/")], [])

    # -- the individual findings ---------------------------------------------------------
    def test_relative_date_is_judgement_not_safe(self):
        self.write("Projects/alpha/memory/r.md", "We rotated the key last week and it held.\n")
        out = self.findings()
        rel = [f for f in out["findings"] if f["kind"] == "relative-date"]
        self.assertEqual(len(rel), 1)
        self.assertEqual(rel[0]["class"], "judgement")

    def test_oversized_global_memory_is_reported_not_trimmed(self):
        self.write("global-memory/big.md", "\n".join("line %d of real content" % i
                                                     for i in range(60)) + "\n")
        out = self.findings()
        bloat = [f for f in out["findings"] if f["kind"] == "memory-bloat"]
        self.assertEqual(len(bloat), 1)
        self.assertEqual(bloat[0]["class"], "judgement")

    # -- interface -----------------------------------------------------------------------
    def test_vault_is_required_never_inferred(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--vault", r.stderr)

    def test_duplicate_line_is_reported_as_judgement(self):
        self.write("Projects/alpha/memory/a.md", "%s\n%s\n" % (LONG, LONG))
        dup = [f for f in self.findings()["findings"] if f["kind"] == "duplicate-line"]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["class"], "judgement")

    def test_dead_index_row_is_reported_not_removed(self):
        self.write("Projects/alpha/memory/MEMORY.md", "- [Gone](missing-file.md) — hook\n")
        before = tree_digest(self.vault)
        self.findings()
        self.assertEqual(tree_digest(self.vault), before)

    def test_clean_vault_exits_zero(self):
        self.write("Projects/alpha/memory/a.md", "One clear fact that stands on its own.\n")
        self.assertEqual(self.run_opt().returncode, 0)


if __name__ == "__main__":
    unittest.main()
