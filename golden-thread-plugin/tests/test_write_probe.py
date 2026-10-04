"""macOS provenance EPERM: one interpreter for hooks, tools and jobs, chosen by a write probe (0.20.1).

2026-10-03, on the publishing Mac: Homebrew's python3.9 -- the default `python3` there -- got
`[Errno 1] Operation not permitted` replacing the vault's log.md (a file carrying
com.apple.provenance) while /usr/bin/python3 succeeded. gt's hooks and tools ran plain
`python3`. Pinned here, with the EPERM SIMULATED (it cannot be produced on demand):

  * gt_write_probe: a refused interpreter reads FAIL, a working one PASS, per place;
  * choose-interpreter passes over a candidate that fails the probe and records one that
    passes;
  * hook commands (macOS) and hook wrappers use the recorded interpreter; a recorded
    interpreter that is gone is a `badpath`, not a silent fail-open;
  * gt_spool / gt_log / safe_write / the broker turn an EPERM into ONE line naming the
    interpreter and the fix -- never a traceback -- and write nothing (the broker holds);
  * gt_doctor has a write-probe row.
"""
import errno
import io
import json
import os
import shutil
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from _harness import (PYTHON, SCRIPTS, TOOLS, GT, Sandbox, load_module,
                      skip_on_windows)

PROBE = SCRIPTS / "gt_write_probe.py"
SCHED = SCRIPTS / "gt_schedule.py"
EPERM = PermissionError(errno.EPERM, "Operation not permitted")


def refusing_python(path):
    """An executable that answers like Python 3.8+ but exits 13 (refused) for the probe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\ncase "$2" in *gt-write-probe*) exit 13 ;; esac\n'
                    'exec "%s" "$@"\n' % sys.executable, encoding="utf-8")
    path.chmod(0o755)
    return path


class Probe(Sandbox):
    def setUp(self):
        super().setUp()
        self.wp = load_module(PROBE, "gt_write_probe_t")
        self.where = self.tmp / "vault"
        self.where.mkdir()

    def test_a_working_python_passes_and_leaves_nothing(self):
        self.assertEqual(self.wp.probe_one(sys.executable, str(self.where)), "pass")
        self.assertEqual(list(self.where.iterdir()), [])

    @skip_on_windows("a shell-script interpreter and mode bits are POSIX")
    def test_a_refused_python_fails(self):
        bad = refusing_python(self.tmp / "bin" / "python3")
        self.assertEqual(self.wp.probe_one(str(bad), str(self.where)), "refused")
        p = self.py(PROBE, "check", str(bad), "--vault", self.where)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("FAIL", p.stdout)

    @skip_on_windows("chmod 0500 is how a refusal is forced here")
    def test_an_unwritable_place_is_refused(self):
        if os.geteuid() == 0:
            self.skipTest("root writes anyway")
        self.where.chmod(0o500)
        try:
            self.assertEqual(self.wp.probe_one(sys.executable, str(self.where)), "refused")
        finally:
            self.where.chmod(0o700)

    def test_the_existing_file_is_opened_never_changed(self):
        log = self.where / "log.md"
        log.write_bytes(b"owner's bytes\n")
        before = log.stat().st_mtime_ns
        self.assertEqual(self.wp.probe_one(sys.executable, str(self.where), str(log)), "pass")
        self.assertEqual(log.read_bytes(), b"owner's bytes\n")
        self.assertEqual(log.stat().st_mtime_ns, before)

    def test_the_eperm_line_names_the_interpreter_and_the_fix(self):
        line = self.wp.eperm_message("/v/log.md", EPERM, python="/opt/brew/python3")
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("/opt/brew/python3", line)
        self.assertIn("EPERM", line)
        self.assertIn("Full Disk Access", line)


@skip_on_windows("the write probe decides only on macOS; Windows keeps its own route")
class ChooseByWriteProbe(Sandbox):
    def setUp(self):
        super().setUp()
        self.m = load_module(SCHED, "gt_schedule_wprobe")
        self.m.LOGS = self.tmp / "logs"
        self.m.LOGS.mkdir()
        self.m.INTERPRETER_RECORD = self.m.LOGS / "interpreter.json"
        self.m.SYSTEM_PYTHON = str(self.tmp / "nope" / "python3")      # not a candidate
        self.m.sandboxed = lambda: False          # the probe runs only outside a sandbox
        self.m.can_probe = lambda vault: False    # the launchd probe is another test's job
        self.bad = refusing_python(self.tmp / "brew" / "python3.9")
        self.good = self.tmp / "sys" / "python3"
        self.good.parent.mkdir()
        self.good.write_text('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable)
        self.good.chmod(0o755)

    def choose(self, *cands):
        args = ["choose-interpreter", "--vault", str(self.tmp)]
        for c in cands:
            args += ["--candidate", str(c)]
        out = io.StringIO()
        with mock.patch.object(self.m.sys, "platform", "darwin"), redirect_stdout(out), \
                redirect_stderr(out):
            rc = self.m.main(args)
        return rc, out.getvalue()

    def test_a_refused_candidate_is_passed_over(self):
        rc, said = self.choose(self.bad, self.good)
        self.assertEqual(rc, self.m.OK, said)
        self.assertEqual(json.loads(self.m.INTERPRETER_RECORD.read_text())["python"],
                         str(self.good))
        self.assertIn("passed over", said)

    def test_a_recorded_interpreter_that_is_refused_is_replaced(self):
        self.m._record(str(self.bad))
        rc, _said = self.choose(self.good)
        self.assertEqual(json.loads(self.m.INTERPRETER_RECORD.read_text())["python"],
                         str(self.good))

    def test_nothing_passing_keeps_the_first_and_says_what_to_grant(self):
        rc, said = self.choose(self.bad)
        self.assertEqual(rc, self.m.PROBLEM)
        self.assertIn("Full Disk Access", said)

    def test_never_from_a_sandbox(self):
        self.m.sandboxed = lambda: True
        self.m.writes_ok = lambda py, vault: self.fail("probed from a sandbox")
        rc, _said = self.choose(self.good)
        self.assertEqual(rc, self.m.OK)


class HooksUseTheRecordedInterpreter(Sandbox):
    def setUp(self):
        super().setUp()
        self.gc = load_module(SCRIPTS / "gt_components.py", "gt_components_wprobe")
        self.rec = self.home / ".claude" / "golden-thread" / "python"
        self.rec.parent.mkdir(parents=True, exist_ok=True)

    @skip_on_windows("Windows names its own interpreter, -X utf8")
    def test_macos_hook_commands_name_the_recorded_interpreter(self):
        self.rec.write_text(sys.executable + "\n")
        with mock.patch.object(self.gc.sys, "platform", "darwin"):
            rows = self.gc.hook_commands(str(GT), home=str(self.home))
        py = [r for r in rows if r["script"].endswith(".py")]
        self.assertTrue(py)
        for r in py:
            self.assertTrue(r["command"].startswith(sys.executable + " -B ")
                            or r["command"].startswith("'%s' -B " % sys.executable),
                            r["command"])
        with mock.patch.object(self.gc.sys, "platform", "linux"):
            rows = self.gc.hook_commands(str(GT), home=str(self.home))
        self.assertTrue(all(r["command"].startswith("python3 -B ")
                            for r in rows if r["script"].endswith(".py")))

    @skip_on_windows("Windows names its own interpreter, -X utf8")
    def test_no_record_keeps_python3(self):
        with mock.patch.object(self.gc.sys, "platform", "darwin"):
            rows = self.gc.hook_commands(str(GT), home=str(self.home))
        self.assertTrue(all(r["command"].startswith("python3 -B ")
                            for r in rows if r["script"].endswith(".py")))

    def test_a_vanished_interpreter_is_a_badpath_not_silence(self):
        settings = self.tmp / "settings.json"
        gone = (self.tmp / "gone" / "python3.9").as_posix()
        target = (Path(self.gc.INSTALLED_HOOKS) / "gt_workers.py").as_posix()
        settings.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "%s -B %s check --hook" % (gone, target)}]}]}}))
        rows = self.gc.check_wiring(str(GT), str(settings), owner="install.sh")
        bad = [r for r in rows if r["script"] == "gt_workers.py"]
        self.assertEqual([(r["state"], r["detail"]) for r in bad], [("badpath", gone)], rows)

    @skip_on_windows("the darwin branch of a bash wrapper")
    def test_the_wrapper_runs_the_recorded_interpreter_on_macos(self):
        hooks = self.tmp / "gt" / "hooks"
        hooks.mkdir(parents=True)
        shutil.copy2(GT / "hooks" / "gt_python.sh", hooks / "gt_python.sh")
        fake = self.tmp / "fake-python"
        fake.write_text("#!/bin/sh\necho RECORDED \"$@\"\n")
        fake.chmod(0o755)
        (hooks.parent / "python").write_text(str(fake) + "\n")
        script = 'OSTYPE=darwin23; HERE="%s"; . "$HERE/gt_python.sh"; python3 -c x' % hooks
        p = self.run_cmd(["bash", "-c", script])
        self.assertEqual(p.stdout.strip(), "RECORDED -c x", p.stderr)
        script = 'OSTYPE=linux-gnu; HERE="%s"; . "$HERE/gt_python.sh"; type -t python3' % hooks
        p = self.run_cmd(["bash", "-c", script])
        self.assertEqual(p.stdout.strip(), "file", "Linux is unchanged")


class OneLineNotATraceback(Sandbox):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(TOOLS))
        self.addCleanup(sys.path.remove, str(TOOLS))
        self.S = load_module(TOOLS / "gt_spool.py", "gt_spool_wprobe")
        self.target = self.tmp / "log.md"
        self.target.write_text("the owner's bytes\n", encoding="utf-8")

    def test_gt_spool_raises_write_refused_with_one_line_and_writes_nothing(self):
        with mock.patch.object(self.S, "_replace", side_effect=EPERM):
            with self.assertRaises(self.S.WriteRefused) as cm:
                self.S.write_if_changed(self.target, "rendered\n")
        line = cm.exception.strerror
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn(sys.executable, line)
        self.assertIn("Full Disk Access", line)
        self.assertIsInstance(cm.exception, PermissionError)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "the owner's bytes\n")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()
                                if p.name.startswith("log.md.")), [], "a temp file was left")

    def test_gt_log_exits_5_with_the_line_and_no_traceback(self):
        vault = self.tmp / "vault"
        tools = vault / "Projects" / "golden-thread" / "tools"
        tools.mkdir(parents=True)
        for t in ("gt_log.py", "gt_spool.py", "safe_write.py"):
            if (TOOLS / t).is_file():
                shutil.copy2(TOOLS / t, tools / t)
        (vault / "log.md").write_bytes(b"# Log\n")          # LF: Windows text mode writes CRLF
        boot = ("import errno, sys; sys.path.insert(0, %r); import gt_spool as S\n"
                "def refuse(*a, **k): raise PermissionError(errno.EPERM, 'Operation not "
                "permitted')\n"
                "S._replace = refuse\n"
                "import gt_log; sys.exit(gt_log.main(sys.argv[1:]))\n" % str(tools))
        m = self.run_cmd([PYTHON, str(tools / "gt_log.py"), "migrate", "--vault", str(vault)])
        self.assertEqual(m.returncode, 0, m.stdout + m.stderr)
        before = (vault / "log.md").read_bytes()
        p = self.run_cmd([PYTHON, "-c", boot, "add", "2026-10-04 [work] probe -- a line",
                          "--vault", str(vault)])
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("Full Disk Access", p.stderr)
        self.assertEqual((vault / "log.md").read_bytes(), before, "log.md was written")

    def test_safe_write_says_which_interpreter_before_falling_back(self):
        sw = load_module(TOOLS / "safe_write.py", "safe_write_wprobe")
        err = io.StringIO()
        with mock.patch.object(sw, "_replace", side_effect=EPERM), redirect_stderr(err):
            path, strategy = sw.write(str(self.target), "new\n")
        self.assertIn("Full Disk Access", err.getvalue())
        self.assertIn(sys.executable, err.getvalue())
        self.assertNotEqual(strategy, "atomic")


class BrokerHoldsARefusedWrite(Sandbox):
    def test_a_refused_write_is_held_with_one_line(self):
        b = load_module(SCRIPTS / "gt_broker.py", "gt_broker_wprobe")
        line = b._refusal(Path("/v/Projects/x/research.md"), EPERM)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("Full Disk Access", line)
        self.assertIn("EPERM", line)
        src = (SCRIPTS / "gt_broker.py").read_text(encoding="utf-8")
        self.assertIn('self.record(r, "held", _refusal(path, exc))', src)


class DoctorRow(Sandbox):
    def test_the_doctor_has_a_write_probe_row(self):
        vault = self.tmp / "vault"
        vault.mkdir()
        (self.home / ".claude" / "golden-thread").mkdir(parents=True, exist_ok=True)
        p = self.py(SCRIPTS / "gt_doctor.py", "--only", "write-probe", "--json",
                    "--vault", vault)
        self.assertNotIn("Traceback", p.stderr)
        rows = [r for r in json.loads(p.stdout)["checks"] if r["check"] == "write-probe"]
        self.assertEqual(len(rows), 1, p.stdout)
        self.assertIn(rows[0]["state"], ("ok", "warn"), rows[0])
        self.assertIn("gt uses", rows[0]["summary"])
        self.assertIn("PASS", rows[0]["detail"])
