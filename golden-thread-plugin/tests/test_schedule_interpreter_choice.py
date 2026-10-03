"""The scheduled jobs' interpreter is CHOSEN, not overwritten (0.20.0).

2026-10-03, on the publishing Mac: install.sh recorded Homebrew's python3.9 -- the python it ran
under -- and under launchd that interpreter got PermissionError writing the vault (macOS privacy
grants are per interpreter; the granted one was /usr/bin/python3). The jobs had been repaired by
hand the night before; installing 0.19.2 undid it, `reconcile` died on a traceback when a plist
rewrite hit EPERM, and the daily job ended up gone.

Covered here: the record is kept across installs; with no record macOS prefers /usr/bin/python3
when it is a working 3.8+; with a job installed a launchd write probe decides (the probe itself
is stubbed -- a test must never bootstrap into the developer's real launchd domain, see
test_gt_schedule's scope note); a failed rewrite leaves the job exactly as it was and is reported
in words. The live probe is exercised on the publishing Mac.
"""
import json
import os
import plistlib
import sys
from pathlib import Path
from unittest import mock

from _harness import IS_WINDOWS, SCRIPTS, Sandbox, load_module, skip_on_windows, WIN_LAUNCHD

SCHED = SCRIPTS / "gt_schedule.py"


def fake_python(path):
    """An executable that answers like a Python 3.8+ (qualifies() asks it one question).
    On Windows a .cmd forwarding to this Python: a "#!" script cannot be run there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        path = path.with_name(path.name + ".cmd")
        path.write_text('@"%s" %%*\r\n' % sys.executable, encoding="utf-8")
        return path
    path.write_text('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable, encoding="utf-8")
    path.chmod(0o755)
    return path


def script(path, sh, cmd):
    """A tiny executable: `sh` as a POSIX shell script, or `cmd` as a .cmd on Windows (which
    cannot run a "#!" script). Returns the path actually written."""
    if IS_WINDOWS:
        path = path if path.suffix == ".cmd" else path.with_name(path.name + ".cmd")
        path.write_text("@echo off\r\n%s\r\n" % cmd, encoding="utf-8")
        return path
    path.write_text("#!/bin/sh\n%s\n" % sh, encoding="utf-8")
    path.chmod(0o755)
    return path


def broken_python(path):
    """Replace a fake python with one that fails, as the no-CLT /usr/bin/python3 stub does."""
    script(path, "exit 1", "exit /b 1")


class ChooseCase(Sandbox):
    def setUp(self):
        super().setUp()
        self.m = load_module(SCHED, "gt_schedule_choose")
        self.m.AGENTS = self.tmp / "LaunchAgents"
        self.m.HOOKS = self.tmp / "hooks"
        self.m.LOGS = self.tmp / "logs"
        self.m.TASKS = self.m.LOGS / "jobs"
        self.m.INTERPRETER_RECORD = self.m.LOGS / "interpreter.json"
        for d in (self.m.AGENTS, self.m.HOOKS, self.m.LOGS):
            d.mkdir(parents=True)
        self.brew = fake_python(self.tmp / "usr/local/opt/python@3.9/bin/python3.9")
        self.system = fake_python(self.tmp / "usr/bin/python3")
        self.m.SYSTEM_PYTHON = str(self.system)
        xcs = script(self.tmp / "xcode-select",       # the Command Line Tools are installed
                     "echo /Library/Developer/CommandLineTools",
                     "echo /Library/Developer/CommandLineTools")
        self.m.XCODE_SELECT = str(xcs)
        self.probed = []

    def recorded(self):
        return json.loads(self.m.INTERPRETER_RECORD.read_text(encoding="utf-8"))["python"]

    def choose(self, *argv, darwin=True):
        with mock.patch.object(self.m.sys, "platform", "darwin" if darwin else "linux"), \
                mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
            rc = self.m.main(["choose-interpreter", *argv])
        return rc, "".join(c.args[0] for c in out.write.call_args_list
                           + err.write.call_args_list)

    def job(self, job="daily", python=None):
        doc = self.m.build_plist(job, "/tmp/v", ["/tmp/tree"], *self.m.JOBS[job][1:4])
        doc["ProgramArguments"][0] = python or str(self.brew)
        with self.m.plist_path(job).open("wb") as fh:
            plistlib.dump(doc, fh)
        return self.m.plist_path(job)


class TheRecordIsKept(ChooseCase):
    def test_an_install_under_another_python_keeps_the_recorded_one(self):
        # The 0.19.2 defect: every install re-recorded the python running it.
        self.m._record(str(self.system))
        rc, said = self.choose("--candidate", str(self.brew))
        self.assertEqual(rc, self.m.OK, said)
        self.assertEqual(self.recorded(), str(self.system))
        self.assertIn("kept the recorded interpreter", said)

    def test_install_sh_asks_to_choose_rather_than_record(self):
        text = (Path(SCRIPTS).parents[2] / "install.sh").read_text(encoding="utf-8")
        self.assertIn("gt_schedule.py\" choose-interpreter --candidate", text)
        self.assertNotIn("gt_schedule.py\" record-interpreter", text)

    def test_a_record_that_no_longer_runs_is_replaced(self):
        gone = fake_python(self.tmp / "gone/python3")    # python3.cmd on Windows
        self.m._record(str(gone))
        gone.unlink()
        rc, said = self.choose("--candidate", str(self.brew), darwin=False)
        self.assertEqual(rc, self.m.OK, said)
        self.assertEqual(self.recorded(), str(self.brew))


class MacPrefersTheSystemPython(ChooseCase):
    @skip_on_windows(WIN_LAUNCHD)
    def test_with_no_record_macos_prefers_usr_bin_python3(self):
        rc, said = self.choose("--candidate", str(self.brew))
        self.assertEqual(rc, self.m.OK, said)
        self.assertEqual(self.recorded(), str(self.system))
        self.assertIn("survives Python upgrades", said)

    def test_without_the_command_line_tools_the_system_stub_is_never_run(self):
        script(Path(self.m.XCODE_SELECT), "exit 2", "exit /b 2")
        script(self.system, "touch %s/ran\necho True" % self.tmp,
               'type nul > "%s"\r\necho True' % (self.tmp / "ran"))
        rc, said = self.choose("--candidate", str(self.brew))
        self.assertEqual(self.recorded(), str(self.brew))
        self.assertFalse((self.tmp / "ran").exists(), "the no-CLT stub opens a dialog when run")

    def test_a_system_python_that_does_not_work_is_passed_over(self):
        broken_python(self.system)                          # e.g. the no-CLT stub
        rc, said = self.choose("--candidate", str(self.brew))
        self.assertEqual(rc, self.m.OK, said)
        self.assertEqual(self.recorded(), str(self.brew))

    def test_off_macos_the_install_python_is_used(self):
        rc, said = self.choose("--candidate", str(self.brew), darwin=False)
        self.assertEqual(self.recorded(), str(self.brew))


class ALaunchdProbeDecides(ChooseCase):
    def setUp(self):
        super().setUp()
        self.job()
        self.m.can_probe = lambda vault: True

    def probe_ok_for(self, *good):
        def probe(py, vault):
            self.probed.append(py)
            return py in [str(g) for g in good]
        self.m.probe_under_launchd = probe

    @skip_on_windows(WIN_LAUNCHD)
    def test_a_recorded_python_that_cannot_write_the_vault_is_replaced(self):
        self.m._record(str(self.brew))
        self.probe_ok_for(self.system)
        rc, said = self.choose("--candidate", str(self.brew), "--vault", str(self.tmp))
        self.assertEqual(rc, self.m.OK, said)
        self.assertEqual(self.probed, [str(self.brew), str(self.system)])
        self.assertEqual(self.recorded(), str(self.system))
        self.assertIn("cannot write", said)
        self.assertIn("wrote the vault under launchd", said)

    def test_when_nothing_can_write_it_says_what_to_grant(self):
        self.m._record(str(self.brew))
        self.probe_ok_for()
        rc, said = self.choose("--candidate", str(self.brew), "--vault", str(self.tmp))
        self.assertEqual(rc, self.m.PROBLEM)
        self.assertEqual(self.recorded(), str(self.brew), "the record is kept, not emptied")
        self.assertIn("Full Disk Access", said)

    def test_the_probe_is_a_one_file_write_in_the_vault(self):
        vault = self.tmp / "v"
        vault.mkdir()
        p = self.run_cmd([sys.executable, "-c", self.m.PROBE_CODE, str(vault)])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(list(vault.iterdir()), [], "the probe must leave nothing behind")
        if IS_WINDOWS or os.geteuid() == 0:   # no permission bits on Windows; root writes anyway
            return
        vault.chmod(0o500)
        try:
            p = self.run_cmd([sys.executable, "-c", self.m.PROBE_CODE, str(vault)])
        finally:
            vault.chmod(0o700)
        self.assertEqual(p.returncode, 13)


class NeverProbedInASandbox(ChooseCase):
    def test_a_sandbox_home_never_probes_launchd(self):
        # The real can_probe: a sandbox HOME is not the real user's.
        self.job()
        self.m.probe_under_launchd = lambda py, vault: self.fail("probed from a sandbox")
        rc, said = self.choose("--candidate", str(self.brew), "--vault", str(self.tmp))
        self.assertEqual(rc, self.m.OK, said)


class AFailedRewriteLeavesTheJob(ChooseCase):
    @skip_on_windows(WIN_LAUNCHD)
    def test_eperm_leaves_the_plist_and_is_said_in_words(self):
        path = self.job("daily", python="/usr/local/opt/python@3.9/bin/python3.9")
        before = path.read_bytes()
        self.m._record(str(self.system))

        def eperm(p, data):
            raise PermissionError(1, "Operation not permitted", str(p))
        self.m.atomic_write = eperm
        with mock.patch("sys.stdout"), mock.patch("sys.stderr") as err:
            rc = self.m.main(["reconcile", "--no-reload"])
        said = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertEqual(rc, self.m.PROBLEM)
        self.assertTrue(path.is_file(), "the job was removed by a failed rewrite")
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("Operation not permitted", said)
        self.assertIn("left as it was", said)
        self.assertIn("record-interpreter", said)

    @skip_on_windows("a read-only directory (chmod 0500) is how this forces the EPERM")
    def test_the_cli_reports_a_real_permission_error_without_a_traceback(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        doc = {"Label": "com.markethaven.gt-daily",
               "ProgramArguments": ["/opt/gt-test-old/bin/python3", "x.py"]}
        plist = agents / "com.markethaven.gt-daily.plist"
        plist.write_bytes(plistlib.dumps(doc))
        before = plist.read_bytes()
        logs = self.home / ".claude" / "golden-thread"
        logs.mkdir(parents=True)
        (logs / "interpreter.json").write_text(json.dumps({"python": sys.executable}))
        agents.chmod(0o500)
        try:
            p = self.py(SCHED, "reconcile", "--no-reload")
        finally:
            agents.chmod(0o700)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("left as it was", p.stderr)
        self.assertEqual(plist.read_bytes(), before)

    @skip_on_windows(WIN_LAUNCHD)
    def test_a_good_rewrite_is_atomic(self):
        path = self.job("daily", python=str(self.brew))
        self.m._record(str(self.system))
        with mock.patch("sys.stdout"):
            self.assertEqual(self.m.main(["reconcile", "--no-reload"]), self.m.OK)
        with path.open("rb") as fh:
            self.assertEqual(plistlib.load(fh)["ProgramArguments"][0], str(self.system))
        self.assertEqual([p.name for p in self.m.AGENTS.iterdir()], [path.name],
                         "no temporary file left beside the plist")


if __name__ == "__main__":
    import unittest
    unittest.main()
