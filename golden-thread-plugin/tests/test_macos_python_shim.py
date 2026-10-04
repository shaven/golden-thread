"""macOS: skills' bare `python3` runs the interpreter that passed gt's write probe (0.20.1, M9).

The usability run on a Mac where macOS refuses Homebrew's python3 vault writes (a file carrying
com.apple.provenance): hooks and scheduled jobs ran the probed interpreter, but skills run the
python3 on PATH and the gt-vault MCP is started as `python3 -I`, so their writes still failed.
Windows already had the remedy -- a python3 shim in gt's bin dir, put first on PATH for Claude's
Bash commands through $CLAUDE_ENV_FILE, and the MCP started by absolute path. macOS now gets the
same, and ONLY when the PATH python3 failed the probe (interpreter.json "refused"): a python3
that works is never overridden.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from _harness import IS_WINDOWS, PYTHON, REPO, SCRIPTS, Sandbox, load_module

INSTALL = REPO / "install.sh"
BASH = shutil.which("bash")
MARK = "gt-python3-shim"


class EnvFileOnMacOS(Sandbox):
    def setUp(self):
        super().setUp()
        self.m = load_module(SCRIPTS / "gt_components.py", "gt_components_macshim")
        self.envfile = self.tmp / "claude-env.sh"
        self.shim = self.home / ".claude" / "golden-thread" / "bin" / "python3"

    def export(self, marked=True):
        self.shim.parent.mkdir(parents=True, exist_ok=True)
        self.shim.write_text("#!/bin/sh\n# %s\nexec %s \"$@\"\n"
                             % (MARK if marked else "someone else's", PYTHON))
        self.shim.chmod(0o755)
        with mock.patch.dict(os.environ, {"CLAUDE_ENV_FILE": str(self.envfile)}), \
                mock.patch.object(self.m.os, "name", "posix"), \
                mock.patch.object(self.m.sys, "platform", "darwin"):
            return self.m.export_shell_env(home=str(self.home))

    def test_a_gt_shim_is_put_first_on_path(self):
        self.assertTrue(self.export())
        self.assertIn("golden-thread/bin", self.envfile.read_text())

    def test_a_file_that_is_not_gts_is_never_put_on_path(self):
        self.assertFalse(self.export(marked=False))
        self.assertFalse(self.envfile.exists())

    def test_linux_never_gets_it(self):
        self.shim.parent.mkdir(parents=True, exist_ok=True)
        self.shim.write_text("# %s\n" % MARK)
        with mock.patch.dict(os.environ, {"CLAUDE_ENV_FILE": str(self.envfile)}), \
                mock.patch.object(self.m.os, "name", "posix"), \
                mock.patch.object(self.m.sys, "platform", "linux"):
            self.assertFalse(self.m.export_shell_env(home=str(self.home)))

    @unittest.skipIf(IS_WINDOWS, "a POSIX shell sourcing the env file")
    def test_a_skill_style_python3_runs_the_shims_interpreter_despite_path(self):
        """Homebrew's python3 first on PATH, as on the usability Mac: after Claude Code sources
        the env file, `python3 <script>` runs the shim's interpreter."""
        self.assertTrue(self.export())
        decoy = self.tmp / "homebrew-bin"
        decoy.mkdir()
        (decoy / "python3").write_text("#!/bin/sh\necho HOMEBREW\n")
        (decoy / "python3").chmod(0o755)
        script = self.tmp / "skill.py"
        script.write_text("import sys; print(sys.executable)\n")
        r = self.run_cmd([BASH, "-c", 'export PATH="%s:$PATH"; . "%s"; python3 "%s"'
                          % (decoy, self.envfile, script)])
        self.assertOk(r)
        want = subprocess.run([PYTHON, "-c", "import sys; print(sys.executable)"],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(os.path.realpath(r.stdout.strip()), os.path.realpath(want))


@unittest.skipUnless(sys.platform == "darwin", "install.sh's macOS interpreter block")
class InstallWritesTheShimOnlyWhenNeeded(Sandbox):
    def path_python(self):
        return subprocess.run(["python3", "-c", "import sys; print(sys.executable)"],
                              capture_output=True, text=True, env=self.env).stdout.strip()

    def seed(self, refused):
        rec = self.home / ".claude" / "golden-thread" / "interpreter.json"
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(json.dumps({"python": PYTHON, "refused": refused}))

    def install(self):
        p = self.run_cmd([BASH, INSTALL, "--no-vault"], timeout=600)
        self.assertOk(p, "install")
        return p

    def shim(self):
        return self.home / ".claude" / "golden-thread" / "bin" / "python3"

    def mcp_command(self):
        cache = self.home / ".claude" / "plugins" / "cache" / "golden-thread-plugin" / "gt"
        man = next(cache.glob("*/.claude-plugin/plugin.json"))
        return json.loads(man.read_text())["mcpServers"]["gt-vault"]["command"]

    def test_refused_path_python_gets_a_shim_and_an_absolute_mcp_then_converges(self):
        self.seed([self.path_python()])
        p = self.install()
        self.assertTrue(self.shim().is_file(), p.stdout[-2000:])
        text = self.shim().read_text()
        self.assertIn(MARK, text)
        self.assertIn(PYTHON, text)
        self.assertEqual(self.mcp_command(), PYTHON)
        # The probe now passes the PATH python3: the shim goes and the MCP is as shipped.
        self.seed([])
        self.install()
        self.assertFalse(self.shim().exists(), "a shim nobody needs was left behind")
        self.assertEqual(self.mcp_command(), "python3")

    def test_a_python3_that_passed_is_never_overridden(self):
        self.seed([])
        self.install()
        self.assertFalse(self.shim().exists())
        self.assertEqual(self.mcp_command(), "python3")


if __name__ == "__main__":
    unittest.main()
