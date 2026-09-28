"""gt_code_review.py — the deterministic half of code review.

gt does not perform the review and these tests do not pretend it does. "Does this abstraction
earn its keep" has no mechanical oracle, so NOTHING here asserts that a finding is correct.
What is tested is the harness, which is most of what makes a review trustworthy:

  * ZERO DIMENSIONS IS NOT A CLEAN REVIEW. gt ships none on purpose, so the empty case is the
    normal case and must never read as success;
  * a finding that cannot be CHECKED is rejected before a human sees it — a file that does not
    exist, a line past the end of one, an unknown dimension, an invalid severity;
  * a finding marked unconfirmed never reaches a report;
  * a previously declined finding does not come back as new;
  * an empty scope is an outcome, not a silent upgrade to reviewing everything.
"""
import json
import unittest

from _harness import Sandbox, SCRIPTS

REVIEW = SCRIPTS / "gt_code_review.py"


def review_pack(entries, name="mine"):
    return {"schema": 1, "slot": "review", "name": name, "tier": "D", "spdx": "MIT",
            "provenance": {"origin": "original", "contributor": "A Dev <d@e.com>",
                           "upstream": None},
            "dco": "Signed-off-by: A Dev <d@e.com>", "entries": entries}


class ReviewBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        self.packs = self.vault / "Projects" / "golden-thread" / "packs"
        self.packs.mkdir(parents=True)
        self.tree = self.tmp / "tree"
        (self.tree / "scripts").mkdir(parents=True)
        (self.tree / "scripts" / "a.py").write_text("def f():\n    return 1\n")

    def put(self, entries, name="mine"):
        (self.packs / ("review.%s.pack.json" % name)).write_text(
            json.dumps(review_pack(entries, name), indent=2))

    def dim(self, **over):
        d = {"id": "cli-correctness", "title": "CLI behaviour",
             "rubric": "Every failure path exits non-zero."}
        d.update(over)
        return d

    def run_review(self, *args, expect=None):
        proc = self.py(REVIEW, *args)
        if expect is not None:
            self.assertEqual(proc.returncode, expect,
                             "exit %d\n%s\n%s" % (proc.returncode, proc.stdout, proc.stderr))
        return proc

    def findings_file(self, rows):
        p = self.tmp / "findings.json"
        p.write_text(json.dumps(rows))
        return str(p)


class ZeroDimensionsIsNotACleanReview(ReviewBase):
    """gt ships no dimensions, so the empty case is the DEFAULT case. If it reported success,
    every user who never configured one would be told their code had been reviewed."""

    def test_dimensions_with_none_configured_exits_nonzero(self):
        proc = self.run_review("dimensions", "--vault", self.vault)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)

    def test_it_says_nothing_would_be_reviewed_and_how_to_fix_it(self):
        out = self.run_review("dimensions", "--vault", self.vault).stdout + \
            self.run_review("dimensions", "--vault", self.vault).stderr
        self.assertIn("NOTHING WOULD BE REVIEWED", out)
        self.assertIn("review.", out, "it did not say where to put a pack")

    def test_plan_refuses_rather_than_planning_an_empty_review(self):
        self.assertEqual(self.run_review("plan", str(self.tree), "--vault", self.vault)
                         .returncode, 3)

    def test_validate_refuses_when_nothing_is_configured(self):
        f = self.findings_file([{"dimension": "x", "file": "scripts/a.py", "line": 1,
                                 "severity": "warn", "message": "m"}])
        self.assertEqual(self.run_review("validate", f, "--root", str(self.tree),
                                         "--vault", self.vault).returncode, 3)


class TheUsersOwnDimensionsAreTheOnlyDimensions(ReviewBase):
    def test_a_local_pack_supplies_a_dimension(self):
        self.put([self.dim()])
        proc = self.run_review("dimensions", "--vault", self.vault, expect=0)
        self.assertIn("cli-correctness", proc.stdout)
        self.assertIn("1 dimension(s) configured", proc.stdout)

    def test_gt_ships_no_review_pack_of_its_own(self):
        """The framework is shipped; the opinions are not. A core review pack would make gt's
        idea of a good review everyone's default."""
        core = SCRIPTS.parent / "packs" / "core"
        self.assertEqual(sorted(p.name for p in core.glob("review.*.pack.json")), [],
                         "gt has started shipping review dimensions")

    def test_a_dimension_can_scope_itself_to_a_file_glob(self):
        self.put([self.dim(files="*scripts/*.py"), self.dim(id="other", files="*.md")])
        out = json.loads(self.run_review("plan", str(self.tree), "--vault", self.vault,
                                         "--json", expect=0).stdout)
        by_id = {r["dimension"]: r for r in out["plan"]}
        self.assertEqual(len(by_id["cli-correctness"]["files"]), 1)
        self.assertEqual(len(by_id["other"]["files"]), 0)

    def test_the_rubric_reaches_the_plan_verbatim(self):
        """The plan is the handoff to whatever does the judging; a rubric gt paraphrased would
        be gt's opinion wearing the user's name."""
        self.put([self.dim(rubric="EXACT TEXT 12345")])
        out = json.loads(self.run_review("plan", str(self.tree), "--vault", self.vault,
                                         "--json", expect=0).stdout)
        self.assertEqual(out["plan"][0]["rubric"], "EXACT TEXT 12345")


class AFindingMustBeCheckable(ReviewBase):
    """The part a model cannot do for itself, and the part gt can do deterministically."""

    def setUp(self):
        super().setUp()
        self.put([self.dim()])

    def reject_reason(self, row):
        f = self.findings_file([row])
        out = json.loads(self.run_review("validate", f, "--root", str(self.tree),
                                         "--vault", self.vault, "--json").stdout)
        self.assertEqual(len(out["rejected"]), 1, out)
        return out["rejected"][0]["why"]

    def test_a_hallucinated_file_is_rejected(self):
        why = self.reject_reason({"dimension": "cli-correctness", "file": "scripts/ghost.py",
                                  "line": 1, "severity": "warn", "message": "m"})
        self.assertIn("does not exist", why)

    def test_a_line_past_the_end_of_the_file_is_rejected(self):
        why = self.reject_reason({"dimension": "cli-correctness", "file": "scripts/a.py",
                                  "line": 900, "severity": "warn", "message": "m"})
        self.assertIn("outside", why)
        self.assertIn("2 line(s)", why, "the reason does not say how long the file actually is")

    def test_an_unconfigured_dimension_is_rejected(self):
        why = self.reject_reason({"dimension": "invented", "file": "scripts/a.py", "line": 1,
                                  "severity": "warn", "message": "m"})
        self.assertIn("not configured", why)

    def test_an_invalid_severity_is_rejected(self):
        why = self.reject_reason({"dimension": "cli-correctness", "file": "scripts/a.py",
                                  "line": 1, "severity": "catastrophic", "message": "m"})
        self.assertIn("severity", why)

    def test_an_unconfirmed_finding_never_reaches_a_report(self):
        """A verification pass that says no must be honoured, or the pass is decoration."""
        why = self.reject_reason({"dimension": "cli-correctness", "file": "scripts/a.py",
                                  "line": 1, "severity": "warn", "message": "m",
                                  "confirmed": False})
        self.assertIn("not confirmed", why)

    def test_a_valid_finding_survives(self):
        f = self.findings_file([{"dimension": "cli-correctness", "file": "scripts/a.py",
                                 "line": 2, "severity": "warn", "message": "real"}])
        out = json.loads(self.run_review("validate", f, "--root", str(self.tree),
                                         "--vault", self.vault, "--json").stdout)
        self.assertEqual(len(out["kept"]), 1, out)
        self.assertEqual(out["rejected"], [])

    def test_rejections_are_counted_in_the_summary_not_silently_dropped(self):
        rows = [{"dimension": "cli-correctness", "file": "scripts/a.py", "line": 2,
                 "severity": "warn", "message": "ok"},
                {"dimension": "cli-correctness", "file": "nope.py", "line": 1,
                 "severity": "warn", "message": "bad"}]
        out = self.run_review("validate", self.findings_file(rows), "--root", str(self.tree),
                              "--vault", self.vault).stdout
        self.assertIn("1 finding(s) kept, 1 rejected of 2 submitted", out)


class TheLedgerAndTheReport(ReviewBase):
    def setUp(self):
        super().setUp()
        self.put([self.dim()])

    def test_a_previously_declined_finding_does_not_return_as_new(self):
        """A review that re-proposes what was already dismissed stops being read."""
        ledger = self.tmp / "declined.jsonl"
        ledger.write_text(json.dumps({"dimension": "cli-correctness",
                                      "file": "scripts/a.py", "line": 2}) + "\n")
        f = self.findings_file([{"dimension": "cli-correctness", "file": "scripts/a.py",
                                 "line": 2, "severity": "warn", "message": "seen before"}])
        out = self.run_review("report", f, "--root", str(self.tree), "--vault", self.vault,
                              "--ledger", str(ledger)).stdout
        self.assertIn("suppressed by the ledger", out)
        self.assertNotIn("seen before", out)

    def test_the_report_states_what_was_rejected_as_well_as_what_was_kept(self):
        f = self.findings_file([{"dimension": "cli-correctness", "file": "ghost.py",
                                 "line": 1, "severity": "warn", "message": "m"}])
        out = self.run_review("report", f, "--root", str(self.tree),
                              "--vault", self.vault).stdout
        self.assertIn("Rejected before review", out)


class ScopeIsSharedNotReinvented(ReviewBase):
    def test_it_uses_the_same_code_definition_as_the_scanner(self):
        """Two answers to "what counts as code" would be two tools disagreeing about one tree,
        and the one nobody runs would be the one that was right."""
        src = REVIEW.read_text()
        self.assertIn("import gt_scan_code", src)
        self.assertIn("gt_scan_code.in_scope", src)

    def test_an_empty_scope_is_an_outcome_not_a_silent_whole_repo_review(self):
        self.put([self.dim(files="*.nonexistent")])
        empty = self.tmp / "empty"
        empty.mkdir()
        proc = self.run_review("plan", str(empty), "--vault", self.vault)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("nothing in scope", proc.stdout)


if __name__ == "__main__":
    unittest.main()
