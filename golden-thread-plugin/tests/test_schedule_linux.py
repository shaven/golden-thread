"""gt_schedule.py on Linux: a systemd --user timer, or a tagged crontab line (0.20.1, M11).

0.20.0 wrote a launchd plist on Linux and called launchctl, which Linux does not have: every
Linux job was a file nothing ran, and it was still listed as installed. Now Linux registers the
job with systemd --user (preferred) or cron, and an old plist migrates to it.

No test touches a real systemd user manager or crontab: `systemctl` and `crontab` are FAKES reached
through gt_schedule.run, keeping their state in this object. The real route is proven on a Linux
runner by hand (see the 0.20.1 CHANGELOG entry).
"""
import io
import json
import os
import unittest
import plistlib
from unittest import mock

from _harness import SCRIPTS, Sandbox, load_module

SCHED = SCRIPTS / "gt_schedule.py"


class FakeLinux:
    def __init__(self, case, systemd=True, refuse_enable=False, exit_status="0"):
        self.case, self.systemd, self.refuse_enable = case, systemd, refuse_enable
        self.exit_status = exit_status
        self.enabled, self.started, self.crontab, self.calls = set(), set(), None, []

    def __call__(self, args, timeout=180, input=None):
        args = [str(a) for a in args]
        self.calls.append(args)

        class R:
            returncode, stdout, stderr = 0, "", ""
        r = R()
        m = self.case.m
        if args[0] == "systemctl":
            if not self.systemd:
                r.returncode, r.stderr = 1, "Failed to connect to bus: No medium found"
                return r
            verb = args[2]
            unit = args[-1]
            if verb in ("show-environment", "daemon-reload"):
                return r
            if verb == "enable":
                if self.refuse_enable:
                    r.returncode, r.stderr = 1, "Failed to enable unit: Access denied"
                else:
                    self.enabled.add(unit)
            elif verb == "disable":
                self.enabled.discard(unit)
            elif verb == "is-enabled":
                if unit not in self.enabled:
                    r.returncode, r.stdout = 1, "disabled\n"
                else:
                    r.stdout = "enabled\n"
            elif verb == "start":
                self.started.add(unit)
            elif verb == "show":
                unit = args[3]
                if unit.replace(".service", ".timer") not in self.enabled:
                    r.stdout = "LoadState=not-found\n"
                elif unit in self.started:
                    r.stdout = ("ExecMainStatus=%s\nExecMainStartTimestampMonotonic=123\n"
                                "LoadState=loaded\n" % self.exit_status)
                else:
                    r.stdout = "ExecMainStatus=0\nExecMainStartTimestampMonotonic=0\nLoadState=loaded\n"
            return r
        if args[0] == "crontab":
            if args[1] == "-l":
                if self.crontab is None:
                    r.returncode, r.stderr = 1, "no crontab for gt"
                else:
                    r.stdout = self.crontab
            elif args[1] == "-":
                self.crontab = input
            return r
        if args[:2] == ["sh", "-c"]:
            for job in m.JOBS:
                if str(m.exit_path(job)) in args[2]:
                    m.exit_path(job).write_text(self.exit_status + "\n")
            return r
        if "--check" in args:
            return r
        if args[0] == "launchctl":
            raise AssertionError("launchctl ran on Linux: %s" % args)
        r.returncode, r.stderr = 127, "unexpected: %s" % args
        return r


class LinuxCase(Sandbox):
    def setUp(self):
        super().setUp()
        m = self.m = load_module(SCHED, "gt_schedule_linux")
        m.on_windows = lambda: False
        m.on_linux = lambda: True
        m.AGENTS = self.tmp / "LaunchAgents"
        m.HOOKS = self.tmp / "hooks"
        m.LOGS = self.tmp / "logs"
        m.TASKS = m.LOGS / "jobs"
        m.SYSTEMD_USER = self.tmp / "config" / "systemd" / "user"
        m.INTERPRETER_RECORD = m.LOGS / "interpreter.json"
        for d in (m.HOOKS, m.LOGS):
            d.mkdir(parents=True)
        for script, *_ in m.JOBS.values():
            (m.HOOKS / script).write_text("")
        m._is_real_home = lambda: True        # the fakes stand in for the real scheduler
        w = mock.patch.object(m.shutil, "which", lambda name: "/usr/bin/%s" % name)
        w.start()
        self.addCleanup(w.stop)
        self.fake = m.run = FakeLinux(self)

    def main(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), \
                mock.patch.dict("os.environ", env or {}):
            rc = self.m.main(list(argv))
        return rc, out.getvalue() + err.getvalue()

    def install(self, job="daily", **kw):
        return self.main("install", job, "--vault", str(self.tmp), *kw.get("extra", ()),
                         env=kw.get("env"))


@unittest.skipIf(os.name == "nt", "simulates Linux (POSIX paths and quoting in units); Windows has its own tests")
class Systemd(LinuxCase):
    def test_install_registers_a_timer_and_proves_it(self):
        rc, said = self.install()
        self.assertEqual(rc, self.m.OK, said)
        self.assertIn("PROVEN through systemd --user", said)
        label = self.m.label_for("daily")
        self.assertIn("%s.timer" % label, self.fake.enabled)
        self.assertIn("%s.service" % label, self.fake.started)
        svc, tmr = self.m.unit_paths(label)
        self.assertIn("OnCalendar=*-*-* 22:00:00", tmr.read_text())
        self.assertIn("ExecStart=", svc.read_text())
        self.assertIn("StandardOutput=append:%s" % (self.m.LOGS / "daily.out"), svc.read_text())
        self.assertEqual(json.loads(self.m.spec_path("daily").read_text())["Backend"], "systemd")
        self.assertFalse(self.m.AGENTS.exists(), "a launchd plist was written on Linux")
        self.assertEqual(self.m.installed_jobs(), ["daily"])
        self.assertIs(self.m.job_registered("daily"), True)
        self.assertEqual(self.m.job_status("daily"), ("0", []))

    def test_a_weekly_job_runs_on_its_weekday(self):
        self.assertEqual(self.install("lint-weekly")[0], self.m.OK)
        _svc, tmr = self.m.unit_paths(self.m.label_for("lint-weekly"))
        self.assertIn("OnCalendar=Mon *-*-* 07:00:00", tmr.read_text())

    def test_exec_start_quotes_specifiers_and_variables(self):
        self.assertEqual(self.m._systemd_quote('/a b/100%$x"'), '"/a b/100%%$$x\\""')

    def test_a_refused_enable_is_not_installed(self):
        self.fake.refuse_enable = True
        rc, said = self.install()
        self.assertEqual(rc, self.m.PROBLEM)
        self.assertIn("NOT INSTALLED", said)
        self.assertIn("Access denied", said)
        self.assertEqual(self.m.installed_jobs(), [])
        self.assertFalse(any(p.exists() for p in self.m.unit_paths(self.m.label_for("daily"))))

    def test_a_failing_proof_run_is_not_proven(self):
        self.fake.exit_status = "3"
        rc, said = self.install()
        self.assertEqual(rc, self.m.PROBLEM)
        self.assertIn("exited 3", said)

    def test_a_spec_the_scheduler_lost_is_a_problem(self):
        self.assertEqual(self.install()[0], self.m.OK)
        self.fake.enabled.clear()
        self.assertIs(self.m.job_registered("daily"), False)
        self.assertTrue(self.m.job_status("daily")[1])
        rc, said = self.main("list")
        self.assertIn("NOT registered", said)

    def test_remove_disables_and_deletes_and_says_so(self):
        self.assertEqual(self.install()[0], self.m.OK)
        rc, said = self.main("remove", "daily")
        self.assertEqual(rc, self.m.OK, said)
        self.assertIn("disabled the systemd --user timer", said)
        self.assertEqual(self.fake.enabled, set())
        self.assertEqual(self.m.installed_jobs(), [])
        self.assertFalse(any(p.exists() for p in self.m.unit_paths(self.m.label_for("daily"))))
        rc, said = self.main("remove", "daily")
        self.assertIn("nothing to remove", said)

    def test_reconcile_rewrites_the_spec_and_units(self):
        self.assertEqual(self.install()[0], self.m.OK)
        new_py = self.tmp / "py" / "python3"
        new_py.parent.mkdir()
        new_py.write_text("#!/bin/sh\n")
        new_py.chmod(0o755)
        self.m._record(str(new_py))
        rc, said = self.main("reconcile")
        self.assertEqual(rc, self.m.OK, said)
        self.assertEqual(self.m.job_interpreter("daily"), str(new_py))
        svc, _t = self.m.unit_paths(self.m.label_for("daily"))
        self.assertIn(str(new_py), svc.read_text())


class Cron(LinuxCase):
    def setUp(self):
        super().setUp()
        self.fake.systemd = False

    def test_without_a_user_manager_cron_is_used(self):
        self.fake.crontab = "0 1 * * * /usr/bin/backup  # mine\n"
        rc, said = self.install()
        self.assertEqual(rc, self.m.OK, said)
        lines = self.fake.crontab.splitlines()
        self.assertEqual(lines[0], "0 1 * * * /usr/bin/backup  # mine", "a user line was touched")
        ours = [l for l in lines if l.endswith("# gt-schedule:io.goldenthread.gt-daily")]
        self.assertEqual(len(ours), 1, lines)
        self.assertTrue(ours[0].startswith("0 22 * * * "))
        self.assertEqual(json.loads(self.m.spec_path("daily").read_text())["Backend"], "cron")
        self.assertEqual(self.m.job_status("daily"), ("0", []))
        backups = list((self.m.LOGS / "backups").glob("crontab.*"))
        self.assertTrue(backups, "the old crontab was not backed up")

    def test_a_percent_is_escaped_for_cron(self):
        doc = self.m.build_plist("daily", "/v/100%", [], 22, 0, None)
        self.assertIn("100\\%", self.m.cron_line("daily", doc))

    def test_reinstall_replaces_our_line_only(self):
        self.assertEqual(self.install()[0], self.m.OK)
        self.assertEqual(self.install(extra=("--hour", "5"))[0], self.m.OK)
        ours = [l for l in self.fake.crontab.splitlines() if "gt-schedule:" in l]
        self.assertEqual(len(ours), 1)
        self.assertTrue(ours[0].startswith("0 5 "))

    def test_remove_takes_out_our_line(self):
        self.fake.crontab = "5 5 * * * x\n"
        self.assertEqual(self.install()[0], self.m.OK)
        rc, said = self.main("remove", "daily")
        self.assertEqual(rc, self.m.OK, said)
        self.assertEqual(self.fake.crontab, "5 5 * * * x\n")
        self.assertIn("removed the crontab line", said)


class FromTheOldPlist(LinuxCase):
    """0.20.0 left a launchd plist on Linux. It is a broken job until migrated."""

    def old_plist(self):
        self.m.AGENTS.mkdir(parents=True)
        doc = self.m.build_plist("daily", "/tmp/v", [], 22, 0, None)
        doc["Label"] = self.m.legacy_label_for("daily")
        self.m.legacy_plist_path("daily").write_bytes(plistlib.dumps(doc))
        return doc

    def test_the_old_plist_is_reported_as_never_running(self):
        self.old_plist()
        self.assertEqual(self.m.installed_jobs(), ["daily"])
        self.assertIs(self.m.job_registered("daily"), False)
        problems = self.m.job_status("daily")[1]
        self.assertTrue(any("never ran" in p for p in problems), problems)

    def test_migrate_labels_registers_it_for_real(self):
        doc = self.old_plist()
        rc, said = self.main("migrate-labels")
        self.assertEqual(rc, self.m.OK, said)
        self.assertIn("%s.timer" % self.m.label_for("daily"), self.fake.enabled)
        self.assertFalse(self.m.legacy_plist_path("daily").exists())
        self.assertEqual(self.m.job_args("daily"), doc["ProgramArguments"])

    def test_a_refused_migration_keeps_the_plist(self):
        self.old_plist()
        self.fake.refuse_enable = True
        rc, said = self.main("migrate-labels")
        self.assertEqual(rc, self.m.PROBLEM)
        self.assertTrue(self.m.legacy_plist_path("daily").exists())
        self.assertFalse(self.m.spec_path("daily").exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
