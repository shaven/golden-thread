"""gt_schedule.py — installing gt's launchd jobs, without a hand step.

SCOPE NOTE, and it is a real limit rather than laziness. These tests never call `launchctl`.
A launchd user domain is `gui/<uid>` — the REAL user's domain — no matter what HOME a test
sandbox sets, so a test that ran `install` would bootstrap an actual scheduled job onto the
developer's machine and a test that ran `remove` could boot out a job they rely on. So what is
covered here is everything up to the launchctl boundary: the plist gt would write, the
refusals, and the benign-exit table.

The launchctl half is proven differently and deliberately: `install` kickstarts the job and
reads launchd's own `last exit code` back, so the proof happens on the machine at install time,
where it means something. A terminal run proves nothing about a scheduled job on this Mac —
that is the documented lesson the whole script is shaped around.
"""
import plistlib
import unittest

from _harness import Sandbox, SCRIPTS, load_module

SCHED = SCRIPTS / "gt_schedule.py"


class ScheduleTest(Sandbox):
    def mod(self):
        return load_module(SCHED, "gt_schedule_under_test")

    def test_list_names_every_job_and_its_default_time(self):
        proc = self.py(SCHED, "list")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("daily", proc.stdout)
        self.assertIn("22:00", proc.stdout, "the daily job's 22:00 default is not shown")
        self.assertIn("lint-weekly", proc.stdout)

    def test_the_daily_job_is_scheduled_at_2200_by_default(self):
        m = self.mod()
        doc = m.build_plist("daily", "/tmp/v", [], *m.JOBS["daily"][1:4])
        self.assertEqual(doc["StartCalendarInterval"]["Hour"], 22)
        self.assertEqual(doc["StartCalendarInterval"]["Minute"], 0)
        self.assertNotIn("Weekday", doc["StartCalendarInterval"],
                         "the daily job must not be pinned to one weekday")

    def test_the_plist_names_the_vault_explicitly(self):
        """Core rule 2: never let the target be inferred. A scheduled job has no cwd worth
        trusting and no session to ask."""
        m = self.mod()
        doc = m.build_plist("daily", "/tmp/somevault", ["/tmp/repo1"], 22, 0, None)
        args = doc["ProgramArguments"]
        self.assertIn("--vault", args)
        self.assertIn("/tmp/somevault", args)
        self.assertIn("/tmp/repo1", args)

    def test_the_plist_sets_PATH_explicitly(self):
        """A launchd job inherits almost nothing. `git` missing from PATH is the quiet way
        this produces an empty report rather than an error."""
        m = self.mod()
        doc = m.build_plist("daily", "/tmp/v", [], 22, 0, None)
        path = doc.get("EnvironmentVariables", {}).get("PATH", "")
        self.assertIn("/usr/bin", path)
        self.assertTrue(path, "no PATH was set for the job")

    def test_it_runs_the_installed_copy_outside_cloudstorage(self):
        """The documented working pattern: the job's script must not live under
        ~/Library/CloudStorage, so the plist points at ~/.claude/golden-thread/hooks/."""
        m = self.mod()
        doc = m.build_plist("daily", "/tmp/v", [], 22, 0, None)
        target = [a for a in doc["ProgramArguments"] if a.endswith("gt_daily.py")]
        self.assertTrue(target, "the plist does not run gt_daily.py")
        self.assertNotIn("CloudStorage", target[0],
                         "the scheduled job points into CloudStorage, which is the documented "
                         "way for it to fail quietly")
        self.assertIn(".claude/golden-thread/hooks", target[0])

    def test_install_refuses_when_the_script_is_not_installed(self):
        """Refusing is the point: scheduling a job whose script is absent creates a job that
        fails every night at 22:00 and tells nobody."""
        proc = self.py(SCHED, "install", "daily", "--vault", str(self.tmp))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("CANNOT INSTALL", proc.stderr)
        self.assertIn("install.sh", proc.stderr, "the refusal does not say how to fix it")

    def test_a_normal_outcome_is_not_reported_as_a_failure(self):
        """The weekly lint exits 1 whenever it finds something, which is most weeks. Calling
        that broken trains you to ignore the check — it did exactly that for a few minutes on
        2026-09-27, before BENIGN_EXITS existed."""
        m = self.mod()
        self.assertIn("1", m.BENIGN_EXITS["lint-weekly"])
        self.assertIn("1", m.BENIGN_EXITS["daily"])

    def test_every_job_has_a_benign_exit_entry(self):
        """A job added without one would have its normal exit read as a failure."""
        m = self.mod()
        self.assertEqual(sorted(m.JOBS), sorted(m.BENIGN_EXITS))


if __name__ == "__main__":
    unittest.main()
