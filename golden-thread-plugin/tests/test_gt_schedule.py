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
        """gt_daily exits 1 for a day with nothing recorded. Calling that broken trains you to
        ignore the check — it did exactly that for a few minutes on 2026-09-27, before
        BENIGN_EXITS existed."""
        m = self.mod()
        self.assertIn("1", m.BENIGN_EXITS["daily"])

    def test_a_crash_code_is_never_benign_for_the_weekly_lint(self):
        """SUPERSEDED ASSERTION, inverted 2026-09-28. This asserted "1" was benign for
        lint-weekly on the belief that it exits 1 on findings. It never did (it returns 0 on
        every path it chooses); 1 was Python's uncaught-exception code, and treating it as
        normal hid three Mondays of PermissionError crashes."""
        m = self.mod()
        self.assertEqual(m.BENIGN_EXITS["lint-weekly"], {"0"})
        self.assertNotIn("3", m.BENIGN_EXITS["sweep"], "could-not-run is not a normal sweep")

    def fake_agents(self, m, *jobs, code="0", why=None):
        """Point the module at a sandbox LaunchAgents and stub launchd -- the real domain is
        the developer's (see SCOPE NOTE), so these tests never ask it anything."""
        m.AGENTS = self.tmp / "LaunchAgents"
        m.HOOKS = self.tmp / "hooks"
        m.AGENTS.mkdir(parents=True, exist_ok=True)
        m.HOOKS.mkdir(parents=True, exist_ok=True)
        for job in jobs:
            m.plist_path(job).write_bytes(b"")
            (m.HOOKS / m.JOBS[job][0]).write_text("")
        m.last_exit = lambda job: (code, why)

    def test_job_status_uses_the_benign_table(self):
        m = self.mod()
        self.fake_agents(m, "daily", "lint-weekly", code="1")
        self.assertEqual(m.job_status("daily")[1], [], "exit 1 is a normal night for daily")
        problems = m.job_status("lint-weekly")[1]
        self.assertTrue(any("last exit code = 1" in p for p in problems), problems)

    def test_only_installed_jobs_are_listed(self):
        """A job never installed is a choice; the doctor must not report it."""
        m = self.mod()
        self.fake_agents(m, "lint-weekly")
        self.assertEqual(m.installed_jobs(), ["lint-weekly"])

    def test_the_sweep_job_names_its_vault_and_tree(self):
        """Under launchd the cwd is `/`; gt_sweep's --path defaults to the cwd."""
        m = self.mod()
        doc = m.build_plist("sweep", "/tmp/v", ["/tmp/tree"], *m.JOBS["sweep"][1:4])
        args = doc["ProgramArguments"]
        self.assertEqual(args[args.index("--vault") + 1], "/tmp/v")
        self.assertEqual(args[args.index("--path") + 1], "/tmp/tree")
        self.assertEqual(doc["StartCalendarInterval"],
                         {"Hour": 7, "Minute": 30, "Weekday": 1})
        lint = m.JOBS["lint-weekly"]
        self.assertNotEqual((lint[1], lint[2]), (7, 30), "sweep collides with lint-weekly")

    def install_sweep_script(self, *extra):
        hooks = self.home / ".claude" / "golden-thread" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        for n in ("gt_sweep.py",) + extra:
            (hooks / n).write_text((SCRIPTS / n).read_text())

    def test_sweep_install_needs_exactly_one_tree(self):
        self.install_sweep_script()
        proc = self.py(SCHED, "install", "sweep", "--vault", str(self.tmp))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("exactly one --repo", proc.stderr)

    def test_sweep_install_refuses_when_its_members_cannot_run(self):
        """gt_sweep alone in the hooks dir -- or its members without packs/ -- reports
        could-not-run on every tree. The pre-flight refuses before anything is scheduled."""
        self.install_sweep_script()
        proc = self.py(SCHED, "install", "sweep", "--vault", str(self.tmp),
                       "--repo", str(self.tmp))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("CANNOT INSTALL", proc.stderr)
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists(),
                         "a plist was written for a job that cannot run")

    def test_every_job_has_a_benign_exit_entry(self):
        """A job added without one would have its normal exit read as a failure."""
        m = self.mod()
        self.assertEqual(sorted(m.JOBS), sorted(m.BENIGN_EXITS))


class OneRecordedInterpreter(Sandbox):
    """0.19.1 (request lint-weekly-uses-an-ungranted-interpreter). Each job ran whichever python
    installed it -- gt-lint-weekly /usr/bin/python3, gt-daily python3.9 -- so a privacy grant
    given to one never covered the other, and lint-weekly died with EPERM every Monday."""

    def setUp(self):
        super().setUp()
        self.m = load_module(SCHED, "gt_schedule_interp")
        self.m.AGENTS = self.tmp / "LaunchAgents"
        self.m.HOOKS = self.tmp / "hooks"
        self.m.LOGS = self.tmp / "logs"
        self.m.INTERPRETER_RECORD = self.m.LOGS / "interpreter.json"
        for d in (self.m.AGENTS, self.m.HOOKS, self.m.LOGS):
            d.mkdir(parents=True)
        self.py_bin = self.tmp / "bin" / "python3.9"
        self.py_bin.parent.mkdir()
        self.py_bin.write_text("#!/bin/sh\n")
        self.py_bin.chmod(0o755)

    def record(self):
        self.assertEqual(self.m.main(["record-interpreter", str(self.py_bin)]), 0)

    def write_plist(self, job, interpreter):
        doc = self.m.build_plist(job, "/tmp/v", ["/tmp/tree"], *self.m.JOBS[job][1:4])
        doc["ProgramArguments"][0] = interpreter
        with self.m.plist_path(job).open("wb") as fh:
            plistlib.dump(doc, fh)
        return doc

    def args0(self, job):
        with self.m.plist_path(job).open("rb") as fh:
            return plistlib.load(fh)["ProgramArguments"]

    def test_every_job_names_the_recorded_interpreter(self):
        self.record()
        for job in sorted(self.m.JOBS):
            with self.subTest(job=job):
                doc = self.m.build_plist(job, "/tmp/v", ["/tmp/tree"], *self.m.JOBS[job][1:4])
                self.assertEqual(doc["ProgramArguments"][0], str(self.py_bin))

    def test_a_record_that_is_not_an_executable_is_refused(self):
        self.assertNotEqual(self.m.main(["record-interpreter", str(self.tmp / "nope")]), 0)
        self.assertFalse(self.m.INTERPRETER_RECORD.exists())

    def test_reconcile_rewrites_only_a_job_on_another_interpreter(self):
        self.record()
        old = self.write_plist("lint-weekly", "/usr/bin/python3")
        self.write_plist("daily", str(self.py_bin))
        daily_before = self.m.plist_path("daily").read_bytes()
        self.assertEqual(self.m.main(["reconcile", "--no-reload"]), 0)
        args = self.args0("lint-weekly")
        self.assertEqual(args[0], str(self.py_bin))
        self.assertEqual(args[1:], old["ProgramArguments"][1:], "only the interpreter changes")
        self.assertEqual(self.m.plist_path("daily").read_bytes(), daily_before)

    def test_reconcile_without_a_record_changes_nothing(self):
        self.write_plist("lint-weekly", "/usr/bin/python3")
        before = self.m.plist_path("lint-weekly").read_bytes()
        self.assertEqual(self.m.main(["reconcile", "--no-reload"]), 0)
        self.assertEqual(self.m.plist_path("lint-weekly").read_bytes(), before)

    def test_an_eperm_under_cloudstorage_names_the_interpreter_and_the_folder(self):
        self.write_plist("lint-weekly", "/usr/bin/python3")
        (self.m.LOGS / "lint-weekly.err").write_text(
            "Traceback (most recent call last):\n"
            '  File "/x/gt_lint_weekly.py", line 108, in main\n'
            "PermissionError: [Errno 1] Operation not permitted: '/Users/u/Library/CloudStorage/"
            "Dropbox/Projects/Obsidian/Projects/golden-thread/lint/latest.md'\n")
        (self.m.HOOKS / "gt_lint_weekly.py").write_text("")
        self.m.last_exit = lambda job: ("1", None)
        text = "\n".join(self.m.job_status("lint-weekly")[1])
        self.assertIn("/usr/bin/python3", text)
        self.assertIn("/Users/u/Library/CloudStorage/Dropbox", text)
        self.assertIn("Full Disk Access", text)

    def test_a_plain_failure_gets_no_privacy_diagnosis(self):
        self.write_plist("lint-weekly", "/usr/bin/python3")
        (self.m.LOGS / "lint-weekly.err").write_text("ValueError: something else\n")
        (self.m.HOOKS / "gt_lint_weekly.py").write_text("")
        self.m.last_exit = lambda job: ("1", None)
        self.assertNotIn("Full Disk Access", "\n".join(self.m.job_status("lint-weekly")[1]))


if __name__ == "__main__":
    unittest.main()
