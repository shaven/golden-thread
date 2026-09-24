"""selftest.sh: the verdict must be able to say FAILED.

selftest.sh accumulates failures in a shell variable (`fail`, set by `bad()`) and
reports on it once at the end. That design has one way to break silently: put the
code that calls `bad()` on the right-hand side of a PIPE, and it runs in a subshell
whose variable assignments are discarded when the subshell exits.

That is exactly what happened. Until 2026-09-24 the SessionStart loop was written

    python3 - <<'PY' | while IFS= read -r cmd; do ... bad "..." ... done

so every failure it found was thrown away at `done`. The run that caught it printed
three FAIL lines and then `SELFTEST PASSED`, exit 0 -- and dev/release-check.sh gates
a release on that exit code, so the release gate was reading a verdict that could not
fail. The three failures were themselves false positives, which is why it survived so
long: the only thing the bug ever hid was noise.

These tests are structural on purpose. The behavioural proof -- injecting a `bad` call
into the loop and watching the script exit 1 -- takes a full install and four minutes,
which is too slow to run on every commit; it was run by hand when the fix landed. What
a fast test CAN pin is the shape that made the failure possible, and the shape is the
part a future edit is likely to reintroduce.
"""
import re
import unittest

from _harness import REPO

SELFTEST = REPO / "selftest.sh"


class SelftestVerdictTest(unittest.TestCase):
    def setUp(self):
        self.text = SELFTEST.read_text()

    def test_no_failure_accumulator_runs_in_a_pipeline_subshell(self):
        """No `| while` in selftest.sh: bash runs that loop in a subshell.

        The check is deliberately blunt. A `| while` that happens NOT to call `bad`
        is still worth flagging here, because the next edit to it probably will --
        and reading from a file or a process substitution costs nothing.
        """
        offenders = [
            (n, line.strip())
            for n, line in enumerate(self.text.splitlines(), 1)
            # Comments are skipped: the fix's own comment quotes the broken idiom in
            # order to explain it, and a test that forbids DESCRIBING the bug would
            # push the next author to delete the explanation rather than the code.
            if not line.lstrip().startswith("#") and re.search(r"\|\s*while\b", line)
        ]
        self.assertEqual(
            offenders, [],
            "selftest.sh pipes into a `while` loop, which bash runs in a subshell -- "
            "any `fail=1` set inside is discarded, and the script reports PASSED while "
            "failing. Read from a file (`done < \"$TMP/list.txt\"`) instead.\n"
            + "\n".join("  line %d: %s" % o for o in offenders))

    def test_verdict_is_derived_from_the_accumulator(self):
        """PASSED/FAILED and the exit code both come from `fail`, not from the last command."""
        self.assertIn('if [ "$fail" = 0 ]; then echo "SELFTEST PASSED', self.text)
        self.assertIn("\nexit $fail\n", self.text)

    def test_bad_sets_the_accumulator(self):
        """`bad()` must record, not merely print -- a printing-only bad() is the same bug."""
        # Matched against the definition line alone: assertRegex on the whole script
        # dumps all 100-odd lines into the failure message, which buries the finding.
        defn = [l for l in self.text.splitlines() if l.startswith("bad()")]
        self.assertEqual(len(defn), 1, "expected exactly one bad() definition: %s" % defn)
        self.assertIn("fail=1", defn[0],
                      "bad() prints but does not record: %s" % defn[0])

    def test_empty_sessionstart_list_is_itself_a_failure(self):
        """A loop over nothing finds nothing.

        This was the 2026-09-10 failure on a second machine: settings.json had no
        SessionStart entries, so the loop ran zero times and every hook "passed".
        The list must be asserted non-empty before it is walked.
        """
        self.assertIn('[ -s "$TMP/sessionstart.txt" ] || bad', self.text)

    def test_every_shipped_sessionstart_hook_has_a_classification(self):
        """Each hook gt wires at SessionStart is named in the case statement.

        Not for correctness -- the default arm accepts an unknown hook that exits
        cleanly -- but so that adding a hook without deciding what it should SAY is a
        visible omission rather than a silent pass. Asserting one blanket rule for all
        of them is what produced the false positives in the first place.
        """
        # Collect the hook names named in case arms, so a miss reports as a short list
        # of names rather than dumping the whole script into the failure message.
        classified = set()
        for arm in re.findall(r"^\s*([\w.|]+)\)\s*$", self.text, re.M):
            classified.update(p for p in arm.split("|") if p)

        wired = {"gt_components.py", "gt_workers.py", "gt_version_check.py",
                 "gt_push_check.py", "inject_core_rules.sh", "gt_report_card.py",
                 "gt_usage_brief.py", "gt_watch.py"}
        self.assertEqual(
            sorted(wired - classified), [],
            "wired at SessionStart but with no arm in selftest.sh's case statement, so "
            "nothing decides whether their silence is correct: %s"
            % sorted(wired - classified))


if __name__ == "__main__":
    unittest.main()
