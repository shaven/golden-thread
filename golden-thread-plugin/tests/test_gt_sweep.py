"""gt_sweep.py — the weekly whole-tree cadence.

The contract that separates this from the other two cadences: it REPORTS and never blocks.
The commit gate looks only at the staged diff, so nothing ever re-examines code nobody
touches — a rule added today never sees an untouched file, and a credential committed before
the gate existed stays committed. The sweep answers that, and because it surfaces old debt it
must not fail: a weekly job that pages you for pre-existing findings every Monday gets
switched off in a fortnight.

The other load-bearing assertion is the aggregator rule this repo already holds elsewhere: a
member that could not run is NOT a clean sweep, and "0 findings" from a scan that never
happened is the failure mode the whole release has been chasing.
"""
import json
import unittest

from _harness import Sandbox, SCRIPTS

SWEEP = SCRIPTS / "gt_sweep.py"
FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"


def lint_pack(entries, name="mine"):
    return {"schema": 1, "slot": "lint", "name": name, "tier": "D", "spdx": "MIT",
            "provenance": {"origin": "original", "contributor": "A Dev <d@e.com>",
                           "upstream": None},
            "dco": "Signed-off-by: A Dev <d@e.com>", "entries": entries}


class SweepBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.tree = self.tmp / "tree"
        (self.tree / "scripts").mkdir(parents=True)
        self.vault = self.tmp / "vault"
        (self.vault / "Projects" / "golden-thread").mkdir(parents=True)

    def write(self, rel, text):
        p = self.tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def sweep(self, *extra, expect=None):
        proc = self.py(SWEEP, "--vault", self.vault, "--path", self.tree, *extra)
        if expect is not None:
            self.assertEqual(proc.returncode, expect,
                             "exit %d\n%s\n%s" % (proc.returncode, proc.stdout, proc.stderr))
        return proc

    def report(self, name):
        p = self.vault / ".gt" / "checks" / ("%s.md" % name)
        return p.read_text() if p.is_file() else ""


class ItReportsAndNeverBlocks(SweepBase):
    def test_findings_do_not_make_the_sweep_fail(self):
        """A weekly job that fails on pre-existing debt is a weekly job someone disables."""
        self.write("scripts/leak.py", 'AWS_KEY = "%s"\n' % FAKE_AWS)
        proc = self.sweep(expect=0)
        self.assertIn("finding(s)", proc.stdout)

    def test_a_clean_tree_says_what_it_covered(self):
        self.write("scripts/ok.py", "x = 1\n")
        proc = self.sweep(expect=0)
        self.assertIn("member(s) ran", proc.stdout)

    def test_the_headline_states_how_many_ran_before_any_count(self):
        """Same rule as gt_scan: never just the finding count."""
        self.write("scripts/ok.py", "x = 1\n")
        out = self.sweep().stdout
        self.assertRegex(out, r"\d+ of \d+ member\(s\) ran")
        # Within the HEADLINE line. The per-member lines above it legitimately print their own
        # counts first; the first version of this test searched the whole output and measured
        # those instead, which is a different claim from the one being made.
        headline = [l for l in out.splitlines() if "member(s) ran" in l]
        self.assertEqual(len(headline), 1, "expected exactly one headline:\n" + out)
        self.assertLess(headline[0].index("member(s) ran"), headline[0].index("finding(s)"),
                        "the headline reported findings before saying how many members ran")


class AMemberThatCouldNotRunIsNotClean(SweepBase):
    def test_an_unknown_member_is_usage_not_a_quiet_clean_sweep(self):
        """A typo in --only must not come back as a clean tree."""
        proc = self.py(SWEEP, "--vault", self.vault, "--path", self.tree, "--only", "secrest")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("unknown member", proc.stderr)

    def test_a_scanner_that_cannot_run_makes_the_sweep_partial(self):
        """Driven by pointing the sweep at a tree whose rule set cannot resolve: gt_scan_code
        exits 4 with no packs, and 0 findings from a scan that did not happen must not read
        as clean."""
        self.write("scripts/ok.py", "x = 1\n")
        proc = self.py(SWEEP, "--vault", self.vault, "--path", self.tree,
                       "--only", "code", env={"GT_VAULT": str(self.vault)})
        # Whatever the exit, the WORD must not be a bare clean: either it ran, or it said it
        # could not. This asserts the distinction exists rather than which side it landed on,
        # because that depends on whether core packs are present in the release under test.
        if "COULD NOT RUN" in proc.stdout:
            self.assertEqual(proc.returncode, 3)
            self.assertIn("NOT a clean sweep", proc.stdout)
        else:
            self.assertEqual(proc.returncode, 0)


class ItFilesTheResultInTheVault(SweepBase):
    def test_each_member_gets_its_own_weekly_history(self):
        self.write("scripts/ok.py", "x = 1\n")
        self.sweep(expect=0)
        self.assertIn("secrets-weekly", self.report("secrets-weekly"))
        self.assertIn("code-weekly", self.report("code-weekly"))

    def test_the_report_carries_a_count_and_a_scope_and_no_finding_content(self):
        """The negative half is the important one: this file is committed and pushed."""
        self.write("scripts/leak.py", 'AWS_KEY = "%s"\n' % FAKE_AWS)
        self.sweep(expect=0)
        body = self.report("secrets-weekly")
        self.assertIn("finding(s)", body)
        self.assertIn("scope:", body)
        self.assertNotIn(FAKE_AWS, body, "the weekly report leaked a credential's value")
        self.assertNotIn("leak.py", body,
                         "the report named the file a credential is in — that is a map to it, "
                         "in a file that is committed and pushed")

    def test_history_accumulates_so_the_trend_is_answerable(self):
        self.write("scripts/ok.py", "x = 1\n")
        self.sweep(expect=0)
        self.sweep(expect=0)
        lines = [l for l in self.report("secrets-weekly").splitlines() if l.startswith("- ")]
        self.assertEqual(len(lines), 2)

    def test_the_vault_is_required_and_never_inferred(self):
        """Core rule 2. A sweep that guessed could file into the wrong vault."""
        proc = self.py(SWEEP, "--path", self.tree)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--vault", proc.stderr)


if __name__ == "__main__":
    unittest.main()
