"""gt_check_report.py — a check's verdict, filed in the vault.

The contract that matters most here is a NEGATIVE one: the report must never carry a
finding's content. gt_secrets exists because a credential must not reach a log, and a vault
file is a log that gets committed and pushed. A report naming `path:line` for a credential
would republish the location of every secret found, in a file that outlives the fix.

The second contract is the posture: recording is bookkeeping and must never block, which is
the opposite of the pre-commit gate's fail-closed rule. Both tools touch the same scanner,
so the asymmetry is easy to "tidy up" later and is pinned here.
"""
import json
import unittest

from _harness import Sandbox, SCRIPTS

REP = SCRIPTS / "gt_check_report.py"
FAKE = "AKIA" + "IOSFODNN7EXAMPLE"


class CheckReportBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        (self.vault / "Projects" / "golden-thread").mkdir(parents=True)

    def record(self, *args, expect=0):
        proc = self.py(REP, "record", "--vault", self.vault, *args)
        self.assertEqual(proc.returncode, expect,
                         "exit %d\n%s\n%s" % (proc.returncode, proc.stdout, proc.stderr))
        return proc

    def show(self, *args):
        return self.py(REP, "show", "--vault", self.vault, *args)

    def report(self, check="secrets"):
        p = self.vault / ".gt" / "checks" / ("%s.md" % check)
        return p.read_text() if p.is_file() else ""


class WhatIsRecorded(CheckReportBase):
    def test_a_verdict_and_a_count_land_in_the_vault(self):
        self.record("--check", "secrets", "--verdict", "clean", "--count", "0",
                    "--scope", "the plugin tree")
        body = self.report()
        self.assertIn("secrets", body)
        self.assertIn("clean", body)
        self.assertIn("the plugin tree", body)

    def test_history_accumulates_newest_last(self):
        """One line per run: the question "is this getting noisier?" needs a series."""
        self.record("--check", "secrets", "--verdict", "clean", "--count", "0")
        self.record("--check", "secrets", "--verdict", "findings", "--count", "2")
        lines = [l for l in self.report().splitlines() if l.startswith("- ")]
        self.assertEqual(len(lines), 2)
        self.assertIn("findings", lines[-1])

    def test_cannot_run_is_recorded_as_itself_not_as_clean(self):
        """The mislabel this release keeps finding. A check that did not happen is not a pass."""
        self.record("--check", "secrets", "--verdict", "cannot-run", "--detail", "scanner absent")
        body = self.report()
        self.assertIn("cannot-run", body)
        self.assertNotIn("clean", body.split("cannot-run")[1])

    def test_an_unknown_verdict_is_refused(self):
        """Three verdicts, closed set. A free-text verdict could not be counted later."""
        proc = self.py(REP, "record", "--vault", self.vault, "--check", "x",
                       "--verdict", "probably-fine")
        self.assertNotEqual(proc.returncode, 0)


class WhatIsNeverRecorded(CheckReportBase):
    def test_there_is_no_way_to_put_a_finding_value_in_a_report(self):
        """Not "the tool declines to" — there is no parameter for it.

        A --finding or --path flag would be the shape of the 0.16.0 leak: the value reaching
        a file because some caller passed it. The CLI simply has nowhere to put one.
        """
        proc = self.py(REP, "record", "--vault", self.vault, "--check", "secrets",
                       "--verdict", "findings", "--path", "conf.py", "--value", FAKE)
        self.assertNotEqual(proc.returncode, 0, "the CLI accepted a finding's value")

    def test_a_detail_string_is_the_only_free_text_and_it_is_the_callers_word(self):
        """--detail exists for "scanner absent"-type notes. It is recorded verbatim, so the
        contract is on the CALLERS: no shipped caller may pass a finding through it. This
        test documents that boundary rather than pretending the field is sanitised."""
        self.record("--check", "secrets", "--verdict", "cannot-run", "--detail", "no scanner")
        self.assertIn("no scanner", self.report())

    def test_no_shipped_caller_passes_a_path_or_value_through_detail(self):
        """The assertion that makes the test above meaningful."""
        import re
        runner = (SCRIPTS.parent.parent.parent / "tests" / "run.sh")
        hook = SCRIPTS.parent / "templates" / "githooks" / "pre-commit"
        for f in (runner, hook):
            if not f.is_file():
                continue
            for m in re.finditer(r"--detail\s+(\S+)", f.read_text()):
                self.assertNotIn("$", m.group(1),
                                 "%s interpolates a variable into --detail, which is how a "
                                 "finding would reach the vault" % f.name)


class ItNeverBlocks(CheckReportBase):
    def test_a_missing_vault_is_reported_and_exits_zero(self):
        proc = self.py(REP, "record", "--vault", self.tmp / "nope",
                       "--check", "secrets", "--verdict", "clean")
        self.assertEqual(proc.returncode, 0,
                         "recording failed a caller — bookkeeping must never block")
        self.assertIn("no vault", proc.stderr)

    def test_the_result_is_still_stated_when_it_cannot_be_written(self):
        """If the record cannot be filed, the verdict must still reach the operator."""
        proc = self.py(REP, "record", "--vault", self.tmp / "nope",
                       "--check", "secrets", "--verdict", "findings")
        self.assertIn("findings", proc.stderr)


class ClaimsAreHonoured(CheckReportBase):
    def test_a_claimed_report_is_skipped_and_said_out_loud(self):
        """Core rule 1: never write a file another live session has claimed."""
        sessions = self.vault / "Projects" / "golden-thread" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "deadbeef-1111-2222-3333-444455556666_2026-09-27_1100.md").write_text(
            "status: active\nfiles_claimed:\n- `.gt/checks/secrets.md`\n")
        proc = self.record("--check", "secrets", "--verdict", "clean")
        self.assertIn("claimed", proc.stderr)
        self.assertIn("deadbeef", proc.stderr)
        self.assertEqual(self.report(), "", "wrote over another session's claim")


class TheLegacyLocation(CheckReportBase):
    def test_a_vault_that_already_reports_under_projects_keeps_doing_so(self):
        """An upgrade must not silently move a report to a folder the owner has never seen
        (owner requirement, 2026-09-14)."""
        legacy = self.vault / "Projects" / "golden-thread" / "checks"
        legacy.mkdir(parents=True)
        self.record("--check", "secrets", "--verdict", "clean")
        self.assertTrue((legacy / "secrets.md").is_file(),
                        "the report ignored the existing legacy folder")
        self.assertFalse((self.vault / ".gt" / "checks" / "secrets.md").exists())


class TheTwoResolversAgree(unittest.TestCase):
    """gt_lint_weekly.py duplicates gt_paths.report_dir and CANNOT import it: it installs to
    ~/.claude/golden-thread/hooks/ and runs standalone from there with no path to scripts/.

    The duplication is therefore load-bearing. But today's other defect was a duplicated
    test fixture where only one copy got fixed, so the two implementations are pinned as
    equivalent here rather than trusted to stay in step.
    """

    def test_both_resolvers_pick_the_same_directory_in_all_three_cases(self):
        import tempfile
        from pathlib import Path
        from _harness import load_module
        paths = load_module(SCRIPTS / "gt_paths.py", "gt_paths_x")
        weekly = load_module(SCRIPTS / "gt_lint_weekly.py", "gt_lint_weekly_x")

        with tempfile.TemporaryDirectory() as d:
            v = Path(d)
            # 1. nothing configured, no legacy folder -> the modern default
            self.assertEqual(Path(weekly.report_dir(str(v), {})).resolve(),
                             paths.report_dir(v, {}, kind="lint").resolve())
            # 2. the legacy folder exists -> it wins
            (v / "Projects" / "golden-thread" / "lint").mkdir(parents=True)
            self.assertEqual(Path(weekly.report_dir(str(v), {})).resolve(),
                             paths.report_dir(v, {}, kind="lint").resolve())
            # 3. an explicit config key -> it wins over both
            cfg = {"lint_report_dir": "somewhere/else"}
            self.assertEqual(Path(weekly.report_dir(str(v), cfg)).resolve(),
                             paths.report_dir(v, cfg, kind="lint").resolve())


class Show(CheckReportBase):
    def test_show_says_how_many_runs_and_the_last_one(self):
        self.record("--check", "secrets", "--verdict", "clean", "--count", "0")
        self.record("--check", "secrets", "--verdict", "findings", "--count", "1")
        proc = self.show()
        self.assertIn("secrets", proc.stdout)
        self.assertIn("2 run(s)", proc.stdout)

    def test_no_reports_yet_exits_one_not_zero(self):
        """"Nothing recorded" and "recorded clean" are different answers."""
        self.assertEqual(self.show().returncode, 1)

    def test_json_is_parseable(self):
        self.record("--check", "secrets", "--verdict", "clean")
        proc = self.show("--json")
        self.assertIn("secrets", json.loads(proc.stdout)["checks"])


if __name__ == "__main__":
    unittest.main()
