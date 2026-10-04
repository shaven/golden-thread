"""Native Windows (Git Bash) install -- 0.19.2. Every defect here was seen on a Windows 11 VM.

The VM is the proof; these tests are the part of it a POSIX runner can hold on to. Each one
simulates the Windows condition that broke the install and fails on the 0.19.1 code:

  * `uname -s` answers MINGW64_NT-... (a fake `uname` first on PATH);
  * `python3` is %LOCALAPPDATA%\\Microsoft\\WindowsApps\\python3.exe -- the Store stub that
    prints "Python was not found" and exits 9009 (a script under .../Microsoft/WindowsApps/);
  * native Windows Python writes CRLF (a wrapper that appends \\r to every line);
  * os.sep is "\\" and relpath answers "hooks\\x" (patched into the module under test);
  * os.name is "nt" (patched for the one call that branches on it).

What none of this can show -- hooks run by Claude Code for Windows itself, a real Store Python
-- is out of reach of a POSIX runner and is said so in the 0.19.2 CHANGELOG entry.
"""
import json
import os
import posixpath
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from _harness import GT, HOOKS, IS_WINDOWS, REPO, SCRIPTS, Sandbox, load_module

INSTALL = REPO / "install.sh"
INSTALL_CMD = REPO / "install.cmd"
BASH = shutil.which("bash")
STORE_MSG = "The Microsoft Store Python does not count"
FIX_URL = "https://www.python.org/downloads/"


class WindowsSandbox(Sandbox):
    """A PATH holding only what the preflight needs, plus the Windows conditions."""

    def setUp(self):
        if IS_WINDOWS:
            self.skipTest("POSIX-only: simulates Windows (fake uname, a shell-script Store stub, "
                          "symlinked tools); on Windows the real install is what is tested")
        super().setUp()
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        self.script(self.fake / "uname", 'echo "MINGW64_NT-10.0-22621"')
        for tool in ("tr", "sed"):
            os.symlink(shutil.which(tool), self.fake / tool)
        self.store = self.tmp / "AppData" / "Local" / "Microsoft" / "WindowsApps"
        self.store.mkdir(parents=True)

    def script(self, path, body):
        path.write_text("#!/bin/sh\n%s\n" % body)
        path.chmod(0o755)
        return path

    def stub(self, name):
        """The Store's App Execution Alias: not Python at all."""
        return self.script(self.store / name,
                           'echo "Python was not found; run without arguments to install from '
                           'the Microsoft Store" >&2; exit 9009')

    def crlf_python(self, path):
        """A python that writes CRLF, as native Windows Python does, and whose
        sys.executable is itself (the resolver keeps sys.executable, not the PATH name)."""
        return self.script(path, 'CR=$(printf "\\r"); REAL="%s"\n'
                                 '"$REAL" "$@" | sed "s/\\$/$CR/" | sed "s#^$REAL$CR\\$#$0$CR#"'
                           % sys.executable)

    def install(self, *args, path, extra_env=None):
        env = {"PATH": path}
        env.update(extra_env or {})
        return self.run_cmd([BASH, INSTALL, *args], env=env)

    def assertRefusedBeforeWriting(self, p):
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 1, out)
        self.assertIn("No usable Python found", out)
        self.assertIn(FIX_URL, out)
        self.assertIn("Add python.exe to PATH", out)
        self.assertEqual(list((self.home / ".claude").iterdir()), [],
                         "a refused install must leave nothing behind")
        return out


class PythonResolution(WindowsSandbox):
    def test_the_store_stub_alone_is_refused_with_the_fix_named(self):
        self.stub("python3")
        self.stub("python")
        out = self.assertRefusedBeforeWriting(
            self.install("--list-modules", path="%s:%s" % (self.fake, self.store)))
        self.assertIn(STORE_MSG, out)
        self.assertNotIn("run without arguments to install", out,
                         "the stub was RUN: its path alone should have rejected it")

    def test_a_real_store_python_does_not_count_either(self):
        # A working interpreter, but under WindowsApps: its AppData virtualisation makes writes
        # under the user profile unreliable, and every file the installer writes is there.
        os.symlink(sys.executable, self.store / "python3")
        os.symlink(sys.executable, self.store / "python")
        out = self.assertRefusedBeforeWriting(
            self.install("--list-modules", path="%s:%s" % (self.fake, self.store)))
        self.assertIn(STORE_MSG, out)

    def test_a_clean_path_that_runs_a_store_python_is_refused(self):
        # The py launcher, or an alias outside WindowsApps, can still land on the Store build:
        # sys.executable is what decides, not the name on PATH.
        other = self.tmp / "elsewhere"
        other.mkdir()
        self.script(other / "python",
                    r"printf '312\r\nC:\\Users\\u\\AppData\\Local\\Microsoft\\WindowsApps\\'"
                    r"'PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\\python.exe\r\n'")
        out = self.assertRefusedBeforeWriting(
            self.install("--list-modules", path="%s:%s" % (self.fake, other)))
        self.assertIn(STORE_MSG, out)

    def test_a_python_below_the_minimum_is_refused(self):
        old = self.tmp / "py37"
        old.mkdir()
        self.script(old / "python", r"printf '307\r\n/c/Python37/python.exe\r\n'")
        out = self.assertRefusedBeforeWriting(
            self.install("--list-modules", path="%s:%s" % (self.fake, old)))
        self.assertIn("too old", out)

    def test_the_real_interpreter_behind_the_stub_is_found_and_its_crlf_stripped(self):
        # The VM's PATH: the stub first, python.org's python.exe (here: emitting CRLF, as native
        # Windows Python does) behind it, then the system tools.
        self.stub("python3")
        real = self.tmp / "Python312"
        real.mkdir()
        crlf = self.crlf_python(real / "python3")
        p = self.install("--list-modules",
                         path=os.pathsep.join([str(self.fake), str(self.store), str(real),
                                               os.environ["PATH"]]))
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("Python: %s" % crlf, out)
        self.assertNotIn("\r", p.stdout, "CRLF from Python reached bash")
        self.assertIn("demo", p.stdout)

    def test_posix_never_enters_the_windows_block(self):
        # No fake uname: the macOS/Linux path is the 0.19.1 one, byte for byte in behaviour.
        p = self.run_cmd([BASH, INSTALL, "--list-modules"])
        self.assertOk(p)
        self.assertNotIn("Python: ", p.stdout)

    def test_no_embedded_python_spawns_a_bare_python3(self):
        # A subprocess list naming "python3" goes past the bash function to Windows' process
        # search -- the Store stub again. sys.executable is the interpreter already running.
        text = INSTALL.read_text(encoding="utf-8")
        self.assertNotIn('["python3"', text)
        self.assertNotIn("['python3'", text)


class HookInterpreter(WindowsSandbox):
    """hooks/gt_python.sh: what every hook wrapper's `python3` means on Windows."""

    def run_sourced(self, ostype, path, here=None, extra=""):
        here = here or HOOKS
        cmd = ('OSTYPE=%s; HERE="%s"; . "%s"; %s type python3 | head -1; '
               'python3 -c "print(1); print(2)" | od -c | head -1'
               % (ostype, here, HOOKS / "gt_python.sh", extra))
        return self.run_cmd([BASH, "-c", cmd], env={"PATH": path})

    def test_every_wrapper_sources_it_before_its_first_python3(self):
        for sh in sorted(HOOKS.glob("*.sh")):
            if sh.name == "gt_python.sh":
                continue
            lines = sh.read_text(encoding="utf-8").splitlines()
            code = [i for i, ln in enumerate(lines)
                    if "python3" in ln and not ln.lstrip().startswith("#")]
            if not code:
                continue
            src = [i for i, ln in enumerate(lines) if 'gt_python.sh"' in ln
                   and ln.lstrip().startswith(". ")]
            self.assertTrue(src and src[0] < code[0],
                            "%s runs python3 without sourcing gt_python.sh first" % sh.name)

    def test_the_recorded_interpreter_beside_the_hooks_dir_is_used_with_crlf_stripped(self):
        gt = self.tmp / "golden-thread"
        (gt / "hooks").mkdir(parents=True)
        real = self.crlf_python(self.tmp / "realpy")
        (gt / "python").write_text("%s\r\n" % real)        # even a CRLF record reads clean
        self.stub("python3")
        p = self.run_sourced("msys", "%s:%s:%s" % (self.fake, self.store, os.environ["PATH"]),
                             here=gt / "hooks")
        self.assertOk(p)
        self.assertIn("function", p.stdout)
        self.assertNotIn("\\r", p.stdout, "CRLF reached the hook")

    def test_with_no_record_the_first_non_store_python_on_path_is_used(self):
        self.stub("python3")
        real = self.tmp / "Python312"
        real.mkdir()
        os.symlink(sys.executable, real / "python3")
        p = self.run_sourced("msys", "%s:%s:%s:%s" % (self.fake, self.store, real,
                                                      os.environ["PATH"]),
                             here=self.tmp / "nowhere", extra='echo "GT_PYTHON=$GT_PYTHON";')
        self.assertOk(p)
        self.assertIn("GT_PYTHON=%s" % (real / "python3"), p.stdout)

    def test_off_windows_it_changes_nothing(self):
        p = self.run_sourced("linux-gnu", os.environ["PATH"])
        self.assertOk(p)
        self.assertNotIn("function", p.stdout)


class ManifestSeparators(unittest.TestCase):
    """Windows relpath answers "hooks\\x"; MANIFEST.json is keyed "hooks/x" everywhere."""

    def setUp(self):
        self.gc = load_module(SCRIPTS / "gt_components.py", "gt_components_win")

    def windows_paths(self):
        if IS_WINDOWS:
            # The real thing: os.sep and relpath are already Windows'. Patching relpath with
            # posixpath's would feed it "C:\\..." paths it cannot read ("../C:/..." keys).
            import contextlib
            return [contextlib.nullcontext(), contextlib.nullcontext()]
        real = posixpath.relpath
        return [mock.patch.object(os, "sep", "\\"),
                mock.patch.object(os.path, "relpath",
                                  lambda p, s=os.curdir: real(p, s).replace("/", "\\"))]

    def test_build_manifest_keys_are_slash_separated(self):
        a, b = self.windows_paths()
        with a, b:
            files = self.gc.build_manifest(str(GT))["files"]
        self.assertTrue(files)
        self.assertEqual([k for k in files if "\\" in k], [])
        self.assertIn("hooks/inject_core_rules.sh", files)

    def test_the_mappers_read_slash_keys_on_windows(self):
        with mock.patch.object(os, "sep", "\\"):
            self.assertIsNotNone(self.gc.hooks_installed_map("hooks/inject_core_rules.sh"))
            self.assertIsNotNone(self.gc.packs_installed_path(
                "packs/core/lint.common.pack.json", "0.0.0"))

    def test_registry_loads_every_shipped_pack_with_windows_relpaths(self):
        reg = load_module(SCRIPTS / "gt_registry.py", "gt_registry_win")
        # _contained() compares realpath()s, which a POSIX runner cannot give Windows
        # separators; it is not what is under test, so it is held true.
        a, b = self.windows_paths()
        import contextlib
        held = contextlib.nullcontext() if IS_WINDOWS else \
            mock.patch.object(reg, "_contained", lambda *_: True)
        with a, b, held:
            packs, problems = reg.load_packs(vault=None)
        refused = [msg for _p, msg in problems if "MANIFEST" in msg]
        self.assertEqual(refused, [], "shipped packs refused on Windows separators")
        self.assertTrue(packs)


class HookCommands(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.gc = load_module(SCRIPTS / "gt_components.py", "gt_components_cmds")
        self.tmp = tempfile.TemporaryDirectory(prefix="gt-test-")
        self.addCleanup(self.tmp.cleanup)

    def test_windows_commands_name_a_real_interpreter_with_forward_slashes(self):
        exe = r"C:\Program Files\Python312\python.exe"
        with mock.patch.object(self.gc.os, "name", "nt"), \
                mock.patch.object(self.gc.sys, "executable", exe), \
                mock.patch.object(self.gc, "INSTALLED_HOOKS",
                                  r"C:\Users\u/.claude/golden-thread/hooks"):
            rows = self.gc.hook_commands(str(GT), str(REPO), home="/nonexistent")
        py = [r["command"] for r in rows if r["script"].endswith(".py")]
        self.assertTrue(py)
        for cmd in py:
            self.assertTrue(cmd.startswith("'C:/Program Files/Python312/python.exe' -X utf8 -B "),
                            cmd)
        for r in rows:
            self.assertNotIn("\\", r["command"], "a backslash is an escape to Git Bash")

    def test_core_rule_hook_commands_survive_git_bash(self):
        # vault_init wires the five Core-rule hooks itself. A bare str(path) on Windows is
        # C:\Users\..., and Git Bash ran "C:Users.claudegolden-threadhooks...": not found.
        from pathlib import PureWindowsPath
        vi = load_module(SCRIPTS / "vault_init.py", "vault_init_cmds")
        script = PureWindowsPath(r"C:\Users\John Smith\.claude\golden-thread\hooks"
                                 r"\inject_core_rules.sh")
        with mock.patch.object(vi.os, "name", "nt"):
            cmd = vi.hook_command(script)
        self.assertEqual(cmd, "'C:/Users/John Smith/.claude/golden-thread/hooks/"
                              "inject_core_rules.sh'")
        self.assertEqual(vi.hook_command(Path("/h/.claude/x.sh")), "/h/.claude/x.sh")

    def test_wiring_check_catches_a_command_the_shell_cannot_find(self):
        settings = Path(self.tmp.name) / "settings.json"
        settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command",
             "command": r"C:\gt\h\.claude\golden-thread\hooks\validate_response.sh"}]}]}}))
        rows = self.gc.check_wiring(str(GT), str(settings), owner="vault_init.py install-core-rules")
        bad = [r for r in rows if r["script"] == "validate_response.sh"]
        self.assertEqual([r["state"] for r in bad], ["badpath"], rows)

    def test_posix_commands_are_unchanged(self):
        # On Windows, the POSIX branch is simulated the other way round (os.name "posix").
        with mock.patch.object(self.gc.os, "name", "posix" if IS_WINDOWS else self.gc.os.name):
            rows = self.gc.hook_commands(str(GT), str(REPO), home="/nonexistent")
        for r in rows:
            if r["script"].endswith(".py"):
                self.assertTrue(r["command"].startswith("python3 -B "), r["command"])


class LineEndings(unittest.TestCase):
    """Text mode on Windows writes CRLF; the vault files are compared byte for byte with LF
    templates. Whatever the platform, the install-path writers must ask for "\\n"."""

    def recorded_open(self, mod):
        calls = []
        real = open

        def spy(*a, **kw):
            calls.append(kw.get("newline"))
            return real(*a, **kw)
        return calls, mock.patch.object(mod, "open", spy, create=True)

    def test_vault_init_writes_lf(self):
        vi = load_module(SCRIPTS / "vault_init.py", "vault_init_win")
        calls, patch = self.recorded_open(vi)
        with patch, self._tmp() as d:
            vi.w_write(Path(d) / "x.md", "a\nb\n")
        self.assertEqual(calls, ["\n"])

    def test_gt_upgrade_writes_lf(self):
        up = load_module(SCRIPTS / "gt_upgrade.py", "gt_upgrade_win")
        calls, patch = self.recorded_open(up)
        with patch, self._tmp() as d:
            up.write_stamp(Path(d), "9.9.9", [])
        self.assertEqual(calls, ["\n"])

    def _tmp(self):
        import tempfile
        return tempfile.TemporaryDirectory(prefix="gt-test-")


class DoctorIsolation(unittest.TestCase):
    def test_smoke_env_isolates_userprofile_on_windows(self):
        doc = load_module(SCRIPTS / "gt_doctor.py", "gt_doctor_win")
        home = Path("/tmp/h")                 # made before os.name is patched: pathlib reads it
        with mock.patch.object(doc.os, "name", "nt"):
            env = doc._smoke_env(home)
        self.assertEqual(env.get("USERPROFILE"), str(home),
                         "Windows Python reads USERPROFILE, so a throwaway HOME alone "
                         "left the smoke test on the real user's ~/.claude")


    def test_the_doctor_runs_its_children_in_utf8_mode_on_windows(self):
        # By hand (`python gt_doctor.py post-install`) nothing sets PYTHONUTF8, and the smoke
        # run of gt_daily.py printed "→" into a cp1252 pipe and FAILED a healthy install.
        doc = load_module(SCRIPTS / "gt_doctor.py", "gt_doctor_utf8")
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(doc.os, "name", "nt"):
            os.environ.pop("PYTHONUTF8", None)
            doc._windows_utf8()
            self.assertEqual(os.environ.get("PYTHONUTF8"), "1")
            env = doc._smoke_env(self.home)
        self.assertEqual(env.get("PYTHONUTF8"), "1")

    def test_core_rules_row_judges_the_command_as_git_bash_reads_it(self):
        # The 0.19.1 entry equals str(path) yet never runs (bash strips the backslashes); the
        # 0.19.2 entry is "/" and quoted. The row must call the first dead, the second wired.
        import ntpath
        doc = load_module(SCRIPTS / "gt_doctor.py", "gt_doctor_core")
        path = r"C:\Users\John Smith\.claude\golden-thread\hooks\inject_core_rules.sh"
        with mock.patch.object(doc.os, "name", "nt"), mock.patch.object(doc.os, "path", ntpath):
            self.assertFalse(doc._runs_script(path, path))
            self.assertTrue(doc._runs_script(
                "'C:/Users/John Smith/.claude/golden-thread/hooks/inject_core_rules.sh'", path))
        self.assertTrue(doc._runs_script("/h/.claude/x.sh", "/h/.claude/x.sh"))

    def setUp(self):
        self.home = Path("/tmp/h")


class ScheduleOnWindows(Sandbox):
    def test_jobs_go_to_task_scheduler_not_launchd(self):
        # 0.19.2 refused these in words; 0.20.0 runs them on Task Scheduler
        # (tests/test_schedule_task_scheduler.py). Never launchctl, never os.getuid.
        sched = load_module(SCRIPTS / "gt_schedule.py", "gt_schedule_win")
        seen = []
        sched.run = lambda args, timeout=180: seen.append(list(args)) or mock.Mock(
            returncode=1, stdout="", stderr="")
        with mock.patch.object(sched.os, "name", "nt"), \
                mock.patch.object(sched, "domain", side_effect=AssertionError("launchd")), \
                mock.patch("sys.stdout"):
            rc = sched.main(["remove", "daily"])
        self.assertEqual(rc, sched.OK)
        self.assertEqual([a[0] for a in seen], ["schtasks"])


class InstallCmd(unittest.TestCase):
    """install.cmd: a launcher only. cmd.exe cannot run on a POSIX runner; the VM ran it."""

    def test_crlf_line_endings(self):
        raw = INSTALL_CMD.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), "an LF line in a .cmd file")
        attrs = (REPO.parent / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.cmd text eol=crlf", attrs)

    def test_it_only_launches_install_sh(self):
        text = INSTALL_CMD.read_text(encoding="utf-8")
        self.assertIn('"%GT_BASH%" "%GT_SH%" %GT_ARGS%', text)
        self.assertIn('set "GT_RC=%ERRORLEVEL%"', text)
        self.assertIn("exit /b %GT_RC%", text)
        self.assertIn("System32\\bash.exe", text, "WSL's bash must be skipped")
        self.assertIn("https://git-scm.com/download/win", text)
        self.assertIn("%GT_SH:\\=/%", text, "install.sh finds itself with dirname: needs '/'")


    def test_windows_switches_utf8_and_the_launcher_marker(self):
        """0.20.1 (M14, M15): /uninstall and /check reach install.sh as --uninstall --check;
        the console is switched to UTF-8 for the run and restored; install.sh is told its
        hints are read in cmd/PowerShell."""
        text = INSTALL_CMD.read_text(encoding="utf-8")
        for win, posix in (("/uninstall", "--uninstall"), ("/check", "--check"),
                           ("/yes", "--yes"), ("/?", "--help")):
            self.assertIn('if /i "%%GT_A%%"=="%s" set "GT_A=%s"' % (win, posix), text)
        self.assertIn("chcp 65001 >nul", text)
        self.assertIn("chcp %GT_CP% >nul", text)
        self.assertIn('set "GT_LAUNCHER=install.cmd"', text)

    @unittest.skipUnless(IS_WINDOWS, "cmd.exe")
    def test_cmd_runs_help_in_utf8(self):
        p = subprocess.run(["cmd.exe", "/c", str(INSTALL_CMD), "/?"], capture_output=True,
                           timeout=120)
        out = p.stdout.decode("utf-8", errors="strict")
        self.assertEqual(p.returncode, 0, out[-1500:])
        self.assertIn("install.sh —", out)
        self.assertIn("--uninstall", out)


class ShellAwareHints(Sandbox):
    """0.20.1 (M14): hints a cmd/PowerShell user can type -- py -3 or the interpreter's path,
    Windows paths, install.cmd -- and the installer by its full path everywhere else."""

    @unittest.skipIf(IS_WINDOWS, "Git Bash names the path in its own /c/... form")
    def test_posix_hints_name_the_installer_by_its_full_path(self):
        p = self.run_cmd([BASH, INSTALL, "--with"], timeout=120)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bash "%s"' % (REPO / "install.sh"), p.stdout)

    def test_install_sh_reads_the_launcher_marker(self):
        text = INSTALL.read_text(encoding="utf-8")
        self.assertIn('"${GT_LAUNCHER:-}" = install.cmd', text)
        self.assertIn('U_PY="py -3"', text)


if __name__ == "__main__":
    unittest.main()
