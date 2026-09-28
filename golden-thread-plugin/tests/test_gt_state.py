"""gt_state.py — write session state before the context runs out.

The load-bearing test here is the SIGNAL one. `readings.jsonl` carries context fill and
rate-limit usage side by side, and they are unrelated: a real reading from the session that
prompted this feature was `{"five_hour": 5, "seven_day": 10, "ctx_pct": 91}`. Triggering on the
allowance meter would have fired at entirely the wrong moment AND LOOKED CORRECT, because both
are percentages with plausible values. So one test asserts the tool never reads `rate_limits`
at all.

The rest: it fires once per crossing, it never claims room it cannot measure, and it never
breaks a turn.
"""
import json
import os
import unittest

from _harness import Sandbox, SCRIPTS

STATE = SCRIPTS / "gt_state.py"


class StateBase(Sandbox):
    def ledger(self, rows):
        d = self.home / ".claude" / "golden-thread" / "usage"
        d.mkdir(parents=True, exist_ok=True)
        (d / "readings.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def reading(self, ctx, session="sess1", **extra):
        r = {"ts": 1790597592, "five_hour": 5, "seven_day": 10, "ctx_pct": ctx,
             "session": session}
        r.update(extra)
        return r

    def check(self, *args):
        return self.py(STATE, "check", *args)

    def state_files(self):
        d = self.home / ".claude" / "golden-thread" / "state"
        return sorted(d.glob("state-*.md")) if d.is_dir() else []


class TheSignalIsContextNotAllowance(StateBase):
    def test_it_never_reads_rate_limits(self):
        """The design's whole risk, asserted at source level.

        A trigger on `five_hour` or `seven_day` would fire on an unrelated number and look
        right doing it. This is cheap and it is the one that keeps the feature honest.
        """
        # The module docstring EXPLAINS that it must not read the allowance meter, so it
        # names it. Strip the docstring and assert on the code: a first version asserted on
        # the whole file and failed on its own explanation.
        src = STATE.read_text()
        code = src.split('"""', 2)[-1]
        self.assertNotIn("rate_limits", code, "the code reads the allowance meter")
        self.assertNotIn("five_hour", code, "the code reads the five-hour allowance")
        self.assertIn("ctx_pct", code, "the code does not read the context reading at all")

    def test_a_high_allowance_with_low_context_does_not_fire(self):
        """The exact inversion: nearly out of weekly allowance, plenty of context."""
        self.ledger([self.reading(10, five_hour=99, seven_day=98)])
        out = json.loads(self.check("--json").stdout)
        self.assertEqual(out["ctx_pct"], 10)
        self.assertFalse(out["due"], "fired on allowance rather than context")

    def test_a_low_allowance_with_high_context_DOES_fire(self):
        """And the case that actually happened: five_hour 5, ctx_pct 91."""
        self.ledger([self.reading(91, five_hour=5, seven_day=10)])
        out = json.loads(self.check("--json").stdout)
        self.assertTrue(out["due"], "did not fire at 91% context")


class ItFiresOncePerCrossing(StateBase):
    def test_the_second_check_is_not_due(self):
        self.ledger([self.reading(90)])
        self.assertTrue(json.loads(self.check("--json").stdout)["due"])
        self.check("--write")
        self.assertFalse(json.loads(self.check("--json").stdout)["due"],
                         "it would write again on every turn after the crossing")

    def test_writing_produces_exactly_one_file(self):
        self.ledger([self.reading(90)])
        self.check("--write")
        self.check("--write")
        self.assertEqual(len(self.state_files()), 1)

    def test_below_the_threshold_nothing_is_written(self):
        self.ledger([self.reading(40)])
        self.check("--write")
        self.assertEqual(self.state_files(), [])


class ItNeverClaimsRoomItCannotMeasure(StateBase):
    def test_a_missing_ledger_is_cannot_tell_not_plenty_of_room(self):
        """The usage module may not be installed. Silence from a source that was never there
        must not read as a measurement saying everything is fine."""
        proc = self.check()
        self.assertIn("cannot tell", proc.stderr)
        self.assertNotIn("not yet", proc.stdout)

    def test_cannot_tell_still_exits_zero(self):
        """It must never block a turn — but see the test above: it does not claim room."""
        self.assertEqual(self.check().returncode, 0)

    def test_a_ledger_with_no_context_reading_is_also_cannot_tell(self):
        self.ledger([{"ts": 1, "five_hour": 50}])
        self.assertIn("cannot tell", self.check().stderr)

    def test_the_newest_reading_wins(self):
        self.ledger([self.reading(10), self.reading(20), self.reading(95)])
        self.assertEqual(json.loads(self.check("--json").stdout)["ctx_pct"], 95)


class TheWriteIsUsefulNotJustPresent(StateBase):
    def test_it_names_the_uncommitted_work(self):
        """A file with the right shape and no content is the failure mode to guard."""
        import subprocess
        repo = self.tmp / "repo"
        (repo / "sub").mkdir(parents=True)
        for cmd in (["init", "-q", "-b", "main", str(repo)],):
            subprocess.run(["git", *cmd], capture_output=True)
        (repo / "sub" / "dirty.py").write_text("x = 1\n")
        self.ledger([self.reading(90)])
        self.py(STATE, "check", "--write", "--repo", str(repo))
        files = self.state_files()
        self.assertEqual(len(files), 1, "no state file was written")
        body = files[0].read_text()
        self.assertIn("dirty.py", body,
                      "the state file names a directory rather than the file that changed")
        self.assertIn("branch", body)

    def test_it_says_WHY_it_was_written(self):
        self.ledger([self.reading(90)])
        self.py(STATE, "check", "--write")
        self.assertIn("context reached", self.state_files()[0].read_text())


class PreCompactIsTheBackstopAndSaysSo(StateBase):
    def test_the_hook_writes_even_when_the_threshold_was_never_crossed(self):
        self.ledger([self.reading(20)])
        self.py(STATE, "hook")
        self.assertEqual(len(self.state_files()), 1)

    def test_the_backstop_identifies_itself(self):
        """So nobody reads a backstop write as the early one having worked."""
        self.ledger([self.reading(20)])
        self.py(STATE, "hook")
        self.assertIn("backstop", self.state_files()[0].read_text())


class TheFlagPointMatchesTheUsageModule(unittest.TestCase):
    def test_the_duplicated_threshold_table_agrees_with_gt_usage(self):
        """CONTEXT_FLAG is a copy: gt_usage lives in a separate module version tree and gt
        cannot import it. A stale copy would fire at the wrong number, so it is pinned here —
        the same discipline as gt_paths.report_dir against gt_lint_weekly's copy."""
        from _harness import REPO, load_module
        import glob
        cands = sorted(glob.glob(str(REPO / "golden-thread-usage" / "*" / "scripts"
                                     / "gt_usage.py")))
        if not cands:
            self.skipTest("the usage module is not in this tree")
        usage = load_module(cands[-1], "gt_usage_for_state")
        state = load_module(STATE, "gt_state_for_flag")
        for mode, row in usage.ALERTS.items():
            if mode == "always":
                continue
            self.assertIn(mode, state.CONTEXT_FLAG, "mode %r is missing from CONTEXT_FLAG" % mode)
            self.assertEqual(state.CONTEXT_FLAG[mode], row[3],
                             "CONTEXT_FLAG[%r] has drifted from gt_usage.ALERTS" % mode)


if __name__ == "__main__":
    unittest.main()
