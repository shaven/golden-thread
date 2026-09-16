"""gt_allin.py -- the everything aggregator.

Contract, and every clause is a way a sweep command normally hides something:
  * It NEVER pushes and never applies a change. Push is outward-facing and effectively
    irreversible, and an aggregator is the one place a partial run is easily mistaken for a
    complete one -- a command that pushed after a run where one member could not execute would
    break Core rule 4, the rule it exists to enforce.
  * `--suggest-push` refuses to suggest anything when a member failed or findings exist.
  * "A member could not run" outranks "a member found something", and the headline always says
    how many of how many ran.
  * `optimize` is run in REPORT mode only. A sweep that edits files as a side effect of
    "checking everything" changes content without anyone deciding to.
  * A member that needs a vault and has none is REPORTED, never quietly dropped.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import GT

SCRIPT = GT / "scripts" / "gt_allin.py"


class AllInTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gt-allin-"))
        self.vault = self.tmp / "vault"
        (self.vault / "Projects" / "alpha" / "memory").mkdir(parents=True)
        (self.vault / "global-memory").mkdir()
        (self.vault / "Knowledge").mkdir()
        (self.vault / "Projects" / "alpha" / "README.md").write_text(
            "# Alpha\n\nA project.\n", encoding="utf-8")
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "a.py").write_text("def ok():\n    pass\n", encoding="utf-8")

    def run_allin(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT)] + list(args),
                              capture_output=True, text=True)

    # -- the push gate -------------------------------------------------------------------
    def test_there_is_no_flag_that_makes_it_push(self):
        """Read the interface itself: a --push flag must not exist."""
        r = self.run_allin("--help")
        self.assertNotIn("--push ", r.stdout)
        self.assertIn("--suggest-push", r.stdout)
        self.assertIn("never runs it", r.stdout)

    def test_it_never_invokes_git_push(self):
        """No member's command line can contain a push, whatever the arguments."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("allin", str(SCRIPT))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for name in mod.MEMBERS:
            cmd = mod.build_cmd(name, mod.MEMBERS[name], str(self.vault), str(self.repo))
            self.assertNotIn("push", " ".join(cmd))
            self.assertNotIn("git", os.path.basename(cmd[1]))

    def test_suggest_push_refuses_when_a_member_could_not_run(self):
        r = self.run_allin("--repo", str(self.repo), "--only", "scan,lint", "--suggest-push")
        self.assertIn("NOT suggesting a push", r.stdout)
        self.assertEqual(r.returncode, 3)

    # -- the aggregation rule ------------------------------------------------------------
    def test_a_member_that_cannot_run_is_not_a_pass(self):
        """No --vault, so the vault members cannot run. That must not read as success."""
        r = self.run_allin("--repo", str(self.repo), "--only", "scan,lint")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("COULD NOT RUN", r.stdout)
        self.assertIn("NOT a pass", r.stdout)

    def test_a_member_needing_a_vault_is_reported_not_skipped(self):
        r = self.run_allin("--repo", str(self.repo), "--only", "lint,optimize", "--json")
        d = json.loads(r.stdout)
        names = [m["member"] for m in d["could_not_run"]]
        self.assertIn("lint", names)
        self.assertIn("optimize", names)
        for m in d["could_not_run"]:
            if m["member"] in ("lint", "optimize"):
                self.assertIn("--vault", m["detail"])

    def test_headline_reports_how_many_ran(self):
        r = self.run_allin("--repo", str(self.repo), "--only", "scan")
        self.assertRegex(r.stdout, r"\d+ of \d+ member\(s\) ran")

    def test_members_are_announced_before_running(self):
        r = self.run_allin("--repo", str(self.repo), "--only", "scan")
        self.assertIn("running", r.stdout)
        self.assertIn("reports only", r.stdout)

    # -- it must not change anything -----------------------------------------------------
    def test_optimize_is_never_run_with_apply(self):
        """Check the command that is actually BUILT, not the source text -- the first version
        of this test grepped the file and matched the comment saying --apply is never passed."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("allin", str(SCRIPT))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for name in mod.MEMBERS:
            cmd = mod.build_cmd(name, mod.MEMBERS[name], str(self.vault), str(self.repo))
            self.assertNotIn("--apply", cmd, "%s must never be run in apply mode" % name)

    def test_unknown_member_is_usage(self):
        r = self.run_allin("--only", "nosuch")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown member", r.stderr)

    # -- what the 2026-09-16 validation got through -------------------------------------
    def test_an_uninstalled_member_is_counted_not_dropped(self):
        """THE defect: the denominator was the INSTALLED set, so a partial install printed
        `1 of 1 member(s) ran`, exited 0 having scanned nothing, and offered to push."""
        import shutil
        rel = Path(tempfile.mkdtemp(prefix="gt-partial-")) / "scripts"
        rel.mkdir(parents=True)
        for name in ("gt_allin.py", "gt_aggregate.py", "gt_doctor.py"):
            src = SCRIPT.parent / name
            if src.is_file():
                shutil.copy2(src, rel / name)
        r = subprocess.run([sys.executable, str(rel / "gt_allin.py"), "--suggest-push"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, "a partial install must not exit clean")
        self.assertIn("COULD NOT RUN", r.stdout)
        self.assertNotIn("To push", r.stdout)
        self.assertRegex(r.stdout, r"\d+ of 4 member\(s\) ran")

    def test_duplicate_only_names_do_not_inflate_the_count(self):
        """`--only doctor,doctor,doctor,doctor` read `4 of 4 member(s) ran`."""
        r = self.run_allin("--only", "doctor,doctor,doctor, doctor ", "--json")
        d = json.loads(r.stdout)
        self.assertEqual(d["asked"], ["doctor"])

    def test_empty_only_is_usage_not_everything(self):
        r = self.run_allin("--only", "", "--repo", str(self.repo))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_a_crashing_member_is_not_counted_as_findings(self):
        """An uncaught python exception exits 1, which was read as `ran, found something`,
        and the traceback was then discarded."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "agg", str(SCRIPT.parent / "gt_aggregate.py"))
        agg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(agg)
        crasher = self.tmp / "crash.py"
        crasher.write_text("raise ValueError('boom')\n", encoding="utf-8")
        res = agg.run_member("x", [sys.executable, str(crasher)])
        self.assertFalse(res["ran"], "a crash is not a finding")
        self.assertEqual(res["status"], "could-not-run")
        self.assertIn("boom", res["detail"])

    def test_a_hanging_member_times_out_and_is_could_not_run(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "agg", str(SCRIPT.parent / "gt_aggregate.py"))
        agg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(agg)
        sleeper = self.tmp / "sleep.py"
        sleeper.write_text("import time; time.sleep(30)\n", encoding="utf-8")
        res = agg.run_member("x", [sys.executable, str(sleeper)], timeout=2)
        self.assertFalse(res["ran"])
        self.assertIn("timed out", res["detail"])

    def test_member_output_cannot_forge_an_aggregator_line(self):
        """A member printed a fake `4 of 4 member(s) ran` and an ANSI conceal that hid the
        real summary. Output is stripped of escapes and prefixed with the member's name."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "agg", str(SCRIPT.parent / "gt_aggregate.py"))
        agg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(agg)
        forged = agg.sanitise("4 of 4 member(s) ran\n\x1b[8mhidden\x1b[2J", "lint")
        self.assertNotIn("\x1b", forged)
        for line in forged.split("\n"):
            self.assertTrue(line.startswith("  [lint] "), line)

    def test_list_names_members_and_says_it_does_not_push(self):
        r = self.run_allin("--list")
        self.assertEqual(r.returncode, 0)
        for name in ("scan", "lint", "optimize", "doctor"):
            self.assertIn(name, r.stdout)
        self.assertIn("never pushes", r.stdout)


if __name__ == "__main__":
    unittest.main()
