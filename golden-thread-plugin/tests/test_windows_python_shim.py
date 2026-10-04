"""A `python3` the model's shell finds on Windows (0.20.1).

Skills and docs tell Claude to run `python3 <tool>.py ...`. In Claude Code for Windows the Bash
tool is Git Bash, where `python3` is the Microsoft Store stub, so every such command failed with
"Python was not found". install.sh now writes a python3 shim (the resolved interpreter, UTF-8
mode, \\r stripped from piped output) to ~/.claude/golden-thread/bin and to ~/bin -- Git for
Windows' per-user bin, first on PATH in every login shell -- and gt_components.py's SessionStart
hook puts gt's bin dir on PATH for Claude Code through $CLAUDE_ENV_FILE.

Simulated on POSIX the way test_windows_install does it: a fake `uname`, the Store stub, and a
python that writes CRLF. The real Git Bash is proven on a Windows 11 VM.
"""
import os
import sys
from pathlib import Path
from unittest import mock

from _harness import IS_WINDOWS, SCRIPTS, Sandbox, load_module
from test_windows_install import BASH, INSTALL, WindowsSandbox

MARK = "gt-python3-shim"


class ShimInstalled(WindowsSandbox):
    def windows_install(self):
        self.stub("python3")
        real = self.tmp / "Python312"
        real.mkdir()
        self.real = self.crlf_python(real / "python3")
        p = self.install("--no-vault",
                         path=os.pathsep.join([str(self.fake), str(self.store), str(real),
                                               os.environ["PATH"]]))
        self.assertOk(p, "the simulated Windows install")
        return p

    def shims(self):
        return [self.home / ".claude" / "golden-thread" / "bin" / "python3",
                self.home / "bin" / "python3"]

    def test_both_shims_are_written_marked_and_executable(self):
        p = self.windows_install()
        for shim in self.shims():
            with self.subTest(shim=shim):
                self.assertTrue(shim.is_file(), p.stdout[-1500:])
                self.assertTrue(os.access(str(shim), os.X_OK))
                text = shim.read_text(encoding="utf-8")
                self.assertIn(MARK, text)
                self.assertIn(str(self.real), text)
        self.assertIn("python3 shim →", p.stdout)

    def test_the_shim_runs_the_real_python_utf8_crlf_stripped_exit_code_kept(self):
        self.windows_install()
        shim = self.shims()[1]
        r = self.run_cmd([BASH, "-c", '"%s" -c "import os; print(os.environ[\'PYTHONUTF8\']); '
                                      'print(2)"' % shim])
        self.assertOk(r)
        self.assertEqual(r.stdout, "1\n2\n", "a \\r or the stub's text reached the shell")
        # The CRLF stand-in reports sed's status, so point a copy at a plain python for this.
        plain = self.tmp / "plain-shim"
        plain.write_text("".join(
            "PY=%s\n" % sys.executable if ln.startswith("PY=") else ln
            for ln in shim.read_text(encoding="utf-8").splitlines(True)), encoding="utf-8")
        plain.chmod(0o755)
        r = self.run_cmd([BASH, "-c", '"%s" -c "import sys; sys.exit(3)" | cat' % plain])
        self.assertEqual(r.returncode, 0)            # (pipefail is off: cat's status)
        r = self.run_cmd([BASH, "-c", '"%s" -c "import sys; sys.exit(3)"' % plain])
        self.assertEqual(r.returncode, 3, "the shim must return Python's exit code, not tr's")

    def test_a_users_own_bin_python3_is_left_alone(self):
        mine = self.home / "bin" / "python3"
        mine.parent.mkdir(parents=True)
        mine.write_text("#!/bin/sh\nexec /my/python \"$@\"\n")
        mine.chmod(0o755)
        p = self.windows_install()
        self.assertEqual(mine.read_text(), "#!/bin/sh\nexec /my/python \"$@\"\n")
        self.assertIn("is not gt's; left as it is", p.stdout)
        self.assertTrue(self.shims()[0].is_file())

    def test_posix_installs_no_shim(self):
        p = self.run_cmd([BASH, INSTALL, "--no-vault"], timeout=300)
        self.assertOk(p)
        for shim in self.shims():
            self.assertFalse(shim.exists(), "macOS/Linux must not get a python3 shim")


class ClaudeCodeFindsIt(Sandbox):
    """gt_components.py check --hook (SessionStart) -> $CLAUDE_ENV_FILE."""

    def setUp(self):
        super().setUp()
        self.m = load_module(SCRIPTS / "gt_components.py", "gt_components_shellenv")
        self.envfile = self.tmp / "claude-env.sh"
        self.shim = self.home / ".claude" / "golden-thread" / "bin" / "python3"

    def export(self, nt=True, shim=True, envfile=True):
        if shim:
            self.shim.parent.mkdir(parents=True, exist_ok=True)
            self.shim.write_text("#!/bin/sh\necho shim\n")
            self.shim.chmod(0o755)
        elif self.shim.exists():
            self.shim.unlink()
        env = {"CLAUDE_ENV_FILE": str(self.envfile)} if envfile else {}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.m.os, "name", "nt" if nt else "posix"):
            if not envfile:
                os.environ.pop("CLAUDE_ENV_FILE", None)
            return self.m.export_shell_env(home=str(self.home))

    def test_on_windows_the_env_file_puts_gts_bin_first_and_utf8_on(self):
        self.assertTrue(self.export())
        # Git Bash answers in its own form (/tmp/... for %TEMP%); cygpath -m gives it back as
        # "C:/..." to compare with. POSIX: unchanged.
        found = 'cygpath -m "$(type -P python3)"' if IS_WINDOWS else "type -P python3"
        r = self.run_cmd([BASH, "-c", '. "%s"; %s; echo "U=$PYTHONUTF8"'
                          % (self.envfile.as_posix(), found)])
        self.assertOk(r)
        self.assertEqual(r.stdout.splitlines(),
                         [self.shim.as_posix() if IS_WINDOWS else str(self.shim), "U=1"])

    def test_it_is_written_once_per_file(self):
        self.export()
        self.assertFalse(self.export())
        self.assertEqual(self.envfile.read_text().count("golden-thread/bin"), 2)  # one line

    def test_never_off_windows_without_a_shim_or_without_the_variable(self):
        self.assertFalse(self.export(nt=False))
        self.assertFalse(self.export(shim=False))
        self.assertFalse(self.export(envfile=False))
        self.assertFalse(self.envfile.exists())

    def test_the_sessionstart_check_hook_calls_it(self):
        called = []
        self.m.export_shell_env = lambda home=None: called.append(1)
        self.m._emit = lambda *a, **k: None
        with mock.patch.object(sys, "argv", ["gt_components.py", "check", str(self.tmp),
                                             "--hook"]):
            self.m.main()
        self.assertEqual(called, [1])
