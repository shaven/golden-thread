"""gt_schedule.py on Windows: the job table on Task Scheduler (0.20.1).

0.19.2 refused install/check/remove on Windows in words (there is no launchd). 0.20.1 runs the
same jobs through `schtasks`, per user and without admin, with the same proof: run the task once
and read Task Scheduler's own Last Result back.

No test may touch a real Task Scheduler, so `schtasks` here is a STUB -- a script gt_schedule.run is
pointed at whenever it is asked for `schtasks` -- that keeps the registered tasks in a JSON file and answers /Create, /Query /V /FO LIST, /Run and
/Delete in the shape Windows 11's schtasks does (the field names were read off a Windows 11 VM).
/Run does not execute the wrapper (a .cmd cannot run here); it records the Last Result the test
chose. The real Task Scheduler is proven on the VM -- see the 0.20.1 CHANGELOG entry.

The platform switch is gt_schedule.on_windows(), patched per test. os.name itself is NOT patched:
pathlib picks WindowsPath from os.name and cannot instantiate it on POSIX.
"""
import json
import os
import sys
import textwrap
from pathlib import Path
from unittest import mock

from _harness import SCRIPTS, Sandbox, load_module

SCHED = SCRIPTS / "gt_schedule.py"

FAKE_SCHTASKS = textwrap.dedent(r'''
    import json, os, sys, time
    state_p = os.environ["FAKE_SCHTASKS_STATE"]
    try:
        state = json.load(open(state_p))
    except Exception:
        state = {"tasks": {}, "calls": []}
    a = sys.argv[1:]
    state["calls"].append(a)
    def opt(name):
        return a[a.index(name) + 1] if name in a else None
    tn, rc = opt("/TN"), 0
    if a[0] == "/Create" and os.environ.get("FAKE_SCHTASKS_CREATE") == "refuse":
        print("ERROR: Access is denied.", file=sys.stderr)
        rc = 1
    elif a[0] == "/Create":
        state["tasks"][tn] = {"tr": opt("/TR"), "sc": opt("/SC"), "d": opt("/D"),
                              "st": opt("/ST"), "last": "267011", "ran": "N/A"}
        print("SUCCESS: The scheduled task \"%s\" has successfully been created." % tn)
    elif tn not in state["tasks"]:
        print("ERROR: The system cannot find the file specified.", file=sys.stderr)
        rc = 1
    elif a[0] == "/Query":
        t = state["tasks"][tn]
        print("")
        print("Folder: \\")
        print("HostName:                             WIN-HOST")
        print("TaskName:                             \\%s" % tn)
        print("Next Run Time:                        10/4/2026 10:00:00 PM")
        print("Status:                               Ready")
        print("Last Run Time:                        %s" % t["ran"])
        print("Last Result:                          %s" % t["last"])
        print("Task To Run:                          %s" % t["tr"])
    elif a[0] == "/Run":
        if os.environ.get("FAKE_SCHTASKS_RESULT") != "never":
            t = state["tasks"][tn]
            t["last"] = os.environ.get("FAKE_SCHTASKS_RESULT", "0")
            t["ran"] = "10/3/2026 7:%02d:%02d AM" % (len(state["calls"]) % 60, int(time.time()) % 60)
        print("SUCCESS: Attempted to run the scheduled task \"%s\"." % tn)
    elif a[0] == "/Delete":
        del state["tasks"][tn]
        print("SUCCESS: The scheduled task \"%s\" was successfully deleted." % tn)
    json.dump(state, open(state_p, "w"))
    sys.exit(rc)
''')


class TaskSchedulerCase(Sandbox):
    def setUp(self):
        super().setUp()
        self.m = load_module(SCHED, "gt_schedule_tasks")
        self.m.on_windows = lambda: True
        self.m.HOOKS = self.tmp / "hooks"
        self.m.LOGS = self.tmp / "logs"
        self.m.TASKS = self.m.LOGS / "jobs"
        self.m.AGENTS = self.tmp / "LaunchAgents"            # must stay empty on Windows
        self.m.INTERPRETER_RECORD = self.m.LOGS / "interpreter.json"
        self.m.PROOF_WAIT = 3
        sleep = mock.patch.object(self.m.time, "sleep", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)
        for d in (self.m.HOOKS, self.m.LOGS):
            d.mkdir(parents=True)
        for script, *_ in self.m.JOBS.values():
            (self.m.HOOKS / script).write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        # The stub is reached through gt_schedule.run, by NAME: a test must never reach the real
        # schtasks.exe (on Windows it would register real tasks) or launchctl.
        self.state = self.tmp / "schtasks.json"
        self.stub = self.tmp / "fake_schtasks.py"
        self.stub.write_text(FAKE_SCHTASKS, encoding="utf-8")
        self.launchctl_log = self.tmp / "launchctl.log"
        self.env_patch = mock.patch.dict(os.environ, {"FAKE_SCHTASKS_STATE": str(self.state)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        # The daily job's own --check runs before anything is scheduled; let it pass.
        self.m.run_real = self.m.run

        def run(args, timeout=180):
            if len(args) > 1 and str(args[1]).endswith(".py") and "--check" in args:
                class R:
                    returncode, stdout, stderr = 0, "", ""
                return R()
            if args and args[0] == "schtasks":
                args = [sys.executable, str(self.stub)] + list(args[1:])
            elif args and args[0] == "launchctl":
                with open(str(self.launchctl_log), "a", encoding="utf-8") as fh:
                    fh.write(" ".join(map(str, args)) + "\n")
                class F:
                    returncode, stdout, stderr = 1, "", "launchctl must not run on Windows"
                return F()
            return self.m.run_real(args, timeout)
        self.m.run = run

    def tasks(self):
        return json.loads(self.state.read_text())["tasks"] if self.state.exists() else {}

    def calls(self):
        return json.loads(self.state.read_text())["calls"] if self.state.exists() else []

    def install(self, job="daily", *extra, result=None):
        if result is not None:
            os.environ["FAKE_SCHTASKS_RESULT"] = result
        argv = ["install", job, "--vault", str(self.vault), *extra]
        with mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
            rc = self.m.main(argv)
        said = "".join(c.args[0] for c in out.write.call_args_list + err.write.call_args_list)
        return rc, said

    def assertNoLaunchd(self):
        self.assertFalse(self.launchctl_log.exists(), "launchctl ran on Windows")
        self.assertFalse(self.m.AGENTS.exists(), "a plist was written on Windows")


class InstallOnTaskScheduler(TaskSchedulerCase):
    def test_install_registers_runs_and_proves_the_daily_job(self):
        rc, said = self.install()
        self.assertEqual(rc, self.m.OK, said)
        self.assertIn("PROVEN through Task Scheduler", said)
        name = self.m.label_for("daily")
        task = self.tasks()[name]
        self.assertEqual((task["sc"], task["st"], task["d"]), ("DAILY", "22:00", None))
        self.assertEqual(task["tr"], '"%s"' % self.m.wrapper_path("daily"),
                         "the task runs the wrapper, quoted (a profile path may hold a space)")
        self.assertIn(["/Run", "/TN", name], self.calls())
        self.assertNoLaunchd()

    def test_a_weekly_job_runs_on_its_weekday(self):
        rc, said = self.install("lint-weekly")
        self.assertEqual(rc, self.m.OK, said)
        task = self.tasks()[self.m.label_for("lint-weekly")]
        self.assertEqual((task["sc"], task["d"], task["st"]), ("WEEKLY", "MON", "07:00"))

    def test_the_wrapper_is_crlf_utf8_mode_and_logs_like_launchd(self):
        self.install()
        raw = self.m.wrapper_path("daily").read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), "a .cmd must be CRLF throughout")
        text = raw.decode("utf-8")
        self.assertIn("set PYTHONUTF8=1", text)
        self.assertIn('"%s"' % (self.m.HOOKS / "gt_daily.py"), text)
        self.assertIn('"--vault" "%s"' % self.vault, text)
        self.assertIn('>> "%s"' % (self.m.LOGS / "daily.out"), text)
        self.assertIn('2>> "%s"' % (self.m.LOGS / "daily.err"), text)
        self.assertTrue(text.rstrip().endswith("exit /b %ERRORLEVEL%"))

    def test_a_percent_in_a_path_is_not_expanded_by_cmd(self):
        self.assertEqual(self.m._cmd_quote("C:\\v\\100%done"), '"C:\\v\\100%%done"')

    def test_the_spec_keeps_the_arguments_a_plist_would(self):
        self.install()
        args = self.m.job_args("daily")
        self.assertEqual(args[1:3], [str(self.m.HOOKS / "gt_daily.py"), "--vault"])
        self.assertEqual(self.m.installed_jobs(), ["daily"])

    def test_a_failing_proof_run_is_not_proven(self):
        rc, said = self.install(result="3")
        self.assertEqual(rc, self.m.PROBLEM)
        self.assertIn("exited 3", said)
        self.assertNotIn("PROVEN", said)

    def test_a_task_that_never_starts_is_not_proven_and_says_why(self):
        # From an SSH session (no desktop logon) Task Scheduler accepts /Run and starts nothing:
        # seen on a Windows 11 VM. The task stays registered; the user is told how to prove it.
        rc, said = self.install(result="never")
        self.assertEqual(rc, self.m.PROBLEM)
        self.assertIn("NOT PROVEN", said)
        self.assertIn("logged on at the desktop", said)
        self.assertIn(self.m.label_for("daily"), self.tasks())


class CheckRemoveListReconcile(TaskSchedulerCase):
    def setUp(self):
        super().setUp()
        os.environ["FAKE_SCHTASKS_RESULT"] = "0"
        rc, said = self.install()
        self.assertEqual(rc, self.m.OK, said)

    def test_check_reads_task_schedulers_last_result(self):
        code, problems = self.m.job_status("daily")
        self.assertEqual((code, problems), ("0", []))
        self.assertEqual(self.m.main(["check", "daily"]), self.m.OK)

    def test_check_names_a_task_task_scheduler_does_not_have(self):
        self.m.run(["schtasks", "/Delete", "/TN", self.m.label_for("daily"), "/F"])
        _code, problems = self.m.job_status("daily")
        self.assertTrue(any("Task Scheduler does not have it" in p for p in problems), problems)

    def test_remove_deletes_the_task_and_its_files(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(self.m.main(["remove", "daily"]), self.m.OK)
        self.assertEqual(self.tasks(), {})
        self.assertFalse(self.m.wrapper_path("daily").exists())
        self.assertFalse(self.m.spec_path("daily").exists())
        self.assertEqual(self.m.installed_jobs(), [])
        self.assertNoLaunchd()

    def test_list_marks_the_installed_job(self):
        with mock.patch("sys.stdout") as out:
            self.m.main(["list"])
        rows = "".join(c.args[0] for c in out.write.call_args_list).splitlines()
        self.assertTrue(any(r.startswith("daily") and "[installed]" in r for r in rows), rows)

    def test_reconcile_rewrites_the_wrapper_and_never_re_registers(self):
        new_py = self.tmp / "Python313" / "python.exe"
        new_py.parent.mkdir()
        new_py.write_text("#!/bin/sh\n")
        new_py.chmod(0o755)
        self.m._record(str(new_py))
        creates = sum(1 for c in self.calls() if c[0] == "/Create")
        with mock.patch("sys.stdout"):
            self.assertEqual(self.m.main(["reconcile"]), self.m.OK)
        self.assertEqual(self.m.job_interpreter("daily"), str(new_py))
        self.assertIn('"%s"' % new_py, self.m.wrapper_path("daily").read_text(encoding="utf-8"))
        self.assertEqual(sum(1 for c in self.calls() if c[0] == "/Create"), creates)
        self.assertNoLaunchd()


class HonestOnTaskScheduler(TaskSchedulerCase):
    """0.20.1 (M11): a refused /Create is not an installed job; old labels migrate."""

    def setUp(self):
        super().setUp()
        os.environ["FAKE_SCHTASKS_RESULT"] = "0"
        self.addCleanup(os.environ.pop, "FAKE_SCHTASKS_CREATE", None)

    def test_a_refused_create_is_not_recorded(self):
        os.environ["FAKE_SCHTASKS_CREATE"] = "refuse"
        rc, said = self.install()
        self.assertEqual(rc, self.m.PROBLEM)
        self.assertIn("NOT INSTALLED", said)
        self.assertIn("Access is denied", said)
        self.assertFalse(self.m.spec_path("daily").exists(), "a refused task left its spec")
        self.assertFalse(self.m.wrapper_path("daily").exists())
        self.assertEqual(self.m.installed_jobs(), [])
        self.assertEqual(self.m.job_status("daily")[1][:1], ["no task file at %s"
                                                            % self.m.spec_path("daily")])

    def test_a_refused_create_keeps_the_previous_job_files(self):
        self.assertEqual(self.install()[0], self.m.OK)
        before = self.m.spec_path("daily").read_bytes()
        os.environ["FAKE_SCHTASKS_CREATE"] = "refuse"
        rc, _said = self.install("daily", "--hour", "5")
        self.assertEqual(rc, self.m.PROBLEM)
        self.assertEqual(self.m.spec_path("daily").read_bytes(), before)

    def legacy_task(self, job="daily"):
        self.assertEqual(self.install(job)[0], self.m.OK)
        old, new = self.m.legacy_label_for(job), self.m.label_for(job)
        st = json.loads(self.state.read_text())
        st["tasks"][old] = st["tasks"].pop(new)
        self.state.write_text(json.dumps(st))
        doc = json.loads(self.m.spec_path(job).read_text(encoding="utf-8"))
        doc["Label"] = old
        self.m.spec_path(job).write_text(json.dumps(doc), encoding="utf-8")
        return old, new

    def test_migrate_labels_moves_the_task(self):
        old, new = self.legacy_task()
        self.assertEqual(self.m.installed_label("daily"), old)
        with mock.patch("sys.stdout"):
            self.assertEqual(self.m.main(["migrate-labels"]), self.m.OK)
        self.assertEqual(sorted(self.tasks()), [new])
        doc = json.loads(self.m.spec_path("daily").read_text(encoding="utf-8"))
        self.assertEqual(doc["Label"], new)
        self.assertIn(new, self.m.wrapper_path("daily").read_text(encoding="utf-8"))
        self.assertNoLaunchd()

    def test_a_refused_migration_leaves_the_old_task(self):
        old, _new = self.legacy_task()
        os.environ["FAKE_SCHTASKS_CREATE"] = "refuse"
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(self.m.main(["migrate-labels"]), self.m.PROBLEM)
        self.assertEqual(sorted(self.tasks()), [old])
        self.assertEqual(json.loads(self.m.spec_path("daily").read_text())["Label"], old)

    def test_remove_names_only_what_it_removed(self):
        with mock.patch("sys.stdout") as out:
            self.assertEqual(self.m.main(["remove", "daily"]), self.m.OK)
        said = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("nothing to remove", said)
        self.assertNotIn("removed", said)


if __name__ == "__main__":
    import unittest
    unittest.main()
