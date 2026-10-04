"""gt unlock usability fixes (0.20.1, usability run 2026-10-04: B3, M5, M6, M12 and the MINOR
unlock items). Each test fails on 0.20.0's code; the security properties they sit next to --
K never lowered silently, no grant without a factor, replay protection, per-seat grants -- are
asserted alongside, and the red-team / review suites still run unchanged.

Fixture TOTP seeds and codes never reach this file's output: assertions on CLI output check
for words, never print the whole output when it could carry a seed.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from _harness import IS_WINDOWS, PYTHON, SCRIPTS, Sandbox, load_module, rmtree
from _unlock_fixture import AuthorityCase

sys.path.insert(0, str(SCRIPTS))
import gt_ipc                     # noqa: E402
import gt_unlock_factors as F     # noqa: E402
import gt_unlock_totp as T        # noqa: E402
import gt_unlockd as D            # noqa: E402

CLI = SCRIPTS / "gt_unlock.py"


def _cli_module():
    return load_module(CLI, "gt_unlock_cli_for_usability")


class CliAgainstFixture(AuthorityCase):
    """Runs the REAL gt_unlock.py as a child process against the fixture authority (this test
    process stands in for `claude`, so the CLI child belongs to its session)."""

    def setUp(self):
        super().setUp()
        self.fake_home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.fake_home, ".claude"))

    def cli_env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        env.update({"HOME": self.fake_home, "GT_UNLOCK_HOME": self.home,
                    "GT_UNLOCK_TEST_SERVER_PID": str(os.getpid()), "GT_UNLOCK_NO_UI": "1",
                    "GT_UNLOCK_NO_START": "1", "PYTHONUTF8": "1"})
        if IS_WINDOWS:
            env["USERPROFILE"] = self.fake_home
        env.update(extra)
        return env

    def cli(self, *args, input="", timeout=60):
        return subprocess.run([PYTHON, str(CLI)] + [str(a) for a in args], input=input,
                              env=self.cli_env(), capture_output=True, text=True,
                              timeout=timeout)

    def no_platform(self):
        """A machine whose only usable factor is TOTP (a Mac without a sensor / CLT, a desktop
        Mac, a PC without Hello)."""
        self.platform.available = lambda: (False, "test: no sensor")


# ============================================================================ B3: run

class RunCommand(CliAgainstFixture):
    """B3: `gt_unlock.py run --scope X -- cmd` crashed every time (TypeError: unhashable list)
    because its REMAINDER positional overwrote the subcommand dest."""

    def _marker_cmd(self, marker, rc=0, read_env=None):
        code = ("import os, sys; open(%r, 'w').write('ran'); " % str(marker))
        if read_env:
            code += ("p = os.environ[%r]; sys.stdout.write('file-ok' if open(p).read() == "
                     "'fixture-value' else 'file-bad'); " % read_env)
        code += "sys.exit(%d)" % rc
        return [PYTHON, "-c", code]

    def test_run_is_refused_while_locked_and_runs_once_unlocked(self):
        self.standard()
        marker = os.path.join(self.tmp, "ran.txt")
        r = self.cli("run", "--scope", "gt:publish", "--", *self._marker_cmd(marker))
        self.assertNotIn("Traceback", r.stderr)
        # locked: `run` asks for an unlock, and with nobody to answer it is refused
        self.assertEqual(r.returncode, 12, r.stderr)
        self.assertIn("no code was entered", r.stderr)
        self.assertFalse(os.path.exists(marker), "the command ran without a grant")
        # unlock this session (the test process is its `claude`), then run again -- past the
        # refusal's prompt cooldown, which is not what this test is about
        self.auth.cooldown.clear()
        shim = self.child()
        g = self.call(shim, "unlock", {"tty": True}, answers=[self.code()])
        self.assertIn("result", g, g)
        r = self.cli("run", "--scope", "gt:publish", "--", *self._marker_cmd(marker, rc=3))
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(r.returncode, 3, r.stderr)              # the command's own exit code
        self.assertTrue(os.path.exists(marker))

    def test_run_hands_a_secret_file_and_deletes_it(self):
        self.standard()
        self.set_policy(door="session")
        src = os.path.join(self.tmp, "token.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("fixture-value")
        if not IS_WINDOWS:
            os.chmod(src, 0o600)
        shim = self.child()
        self.assertIn("result", self.call(shim, "unlock", {"tty": True}, answers=[self.code()]))
        marker = os.path.join(self.tmp, "ran.txt")
        seen = os.path.join(self.tmp, "path.txt")
        code = ("import os, sys; p = os.environ['TOK']; open(%r, 'w').write(p); "
                "sys.stdout.write('file-ok' if open(p).read() == 'fixture-value' else "
                "'file-bad'); open(%r, 'w').write('ran')" % (seen, marker))
        r = self.cli("run", "--scope", "gt:publish", "--secret-file", "TOK=file:" + src, "--",
                     PYTHON, "-c", code)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("file-ok", r.stdout)
        with open(seen, encoding="utf-8") as f:
            self.assertFalse(os.path.exists(f.read()), "the secret file outlived the command")

    def test_run_without_a_command_is_a_usage_error(self):
        r = self.cli("run", "--scope", "gt:publish")
        self.assertEqual(r.returncode, 2)
        self.assertIn("give a command after --", r.stderr)


class RunWithUnlockOff(Sandbox):

    def test_run_executes_when_unlock_is_off(self):
        marker = self.tmp / "ran.txt"
        r = self.py(CLI, "run", "--scope", "gt:publish", "--", PYTHON, "-c",
                    "open(%r, 'w').write('x'); raise SystemExit(4)" % str(marker))
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertTrue(marker.exists())

    def test_help_describes_each_command(self):
        r = self.py(CLI, "--help")
        self.assertOk(r)
        for words in ("prove you are present", "revoke every grant now",
                      "restart it after an upgrade", "how many factors must agree"):
            self.assertIn(words, " ".join(r.stdout.split()))
        r = self.py(CLI, "run", "--help")
        self.assertIn("run a command once the scope is unlocked", " ".join(r.stdout.split()))


# ============================================================================ M5: TOTP-only

MAC_DEFAULT = {"required": 2, "require_one_of": ["touchid"]}


class TotpOnlyMachine(CliAgainstFixture):
    """M5: a machine whose only usable factor is TOTP could not turn unlock on (the default asks
    for K=2 with Touch ID / Hello), and the only route was a hand-edited policy. The macOS /
    Windows default is written out, so the test means the same thing on Linux (K=1 there)."""

    def setUp(self):
        super().setUp()
        F.write_json(os.path.join(self.home, "policy.json"),
                     {"schema": 1, "enabled": False, "factors": dict(MAC_DEFAULT)})
        self.auth.reload()

    def test_refusal_names_the_exact_command_and_k(self):
        self.no_platform()
        self.enrol_totp()
        shim = self.child()
        r = self.call(shim, "policy_set", {"policy": {"schema": 1, "enabled": True,
                                                      "factors": dict(MAC_DEFAULT)},
                                           "tty": True}, answers=[self.code()])
        self.assertEqual(r["error"]["code"], "enrol_first", r)
        msg = r["error"]["message"]
        self.assertIn("gt_unlock.py policy enable --factors totp", msg)
        self.assertIn("how many factors must agree", msg)
        self.assertIn("never lowered silently", msg)
        with open(os.path.join(self.home, "policy.json"), encoding="utf-8") as f:
            self.assertFalse(json.load(f)["enabled"])

    def test_cli_without_a_terminal_refuses_and_names_the_command(self):
        self.no_platform()
        self.enrol_totp()
        r = self.cli("policy", "enable")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("gt_unlock.py policy enable --factors totp", r.stderr)
        self.assertIn("NOT turned on", r.stderr)
        with open(os.path.join(self.home, "policy.json"), encoding="utf-8") as f:
            self.assertFalse(json.load(f)["enabled"])

    def test_the_owner_chooses_totp_and_the_choice_is_recorded(self):
        self.no_platform()
        self.enrol_totp()
        cli = _cli_module()
        pol = {"schema": 1, "enabled": True}
        cli._apply_factor_choice(pol, ["totp"])
        self.assertEqual(pol["factors"]["required"], 1)
        self.assertEqual(pol["factors"]["require_one_of"], [])
        self.assertEqual(pol["factors"]["chosen"]["factors"], ["totp"])
        shim = self.child()
        # still needs the factor: no code, no change
        r = self.call(shim, "policy_set", {"policy": pol, "tty": True}, answers=[])
        self.assertIn("error", r)
        with open(os.path.join(self.home, "policy.json"), encoding="utf-8") as f:
            self.assertFalse(json.load(f)["enabled"])
        r = self.call(shim, "policy_set", {"policy": pol, "tty": True}, answers=[self.code()])
        self.assertEqual(r.get("result"), {"enabled": True}, r)
        st = self.auth.status()
        self.assertEqual(st["problems"], [])
        self.assertEqual(st["factors_chosen"]["factors"], ["totp"])
        with open(os.path.join(self.home, "audit.jsonl"), encoding="utf-8") as f:
            ev = [json.loads(x) for x in f if '"policy_set"' in x]
        self.assertEqual(ev[-1].get("k_chosen_by_owner"), "totp")
        self.assertEqual(ev[-1].get("k"), 1)
        # and `status` says it was a choice
        r = self.cli("status")
        self.assertIn("you chose totp", r.stdout)
        self.assertIn("how many must agree", r.stdout)

    def test_factors_flag_is_validated(self):
        cli = _cli_module()
        self.assertIsNone(cli._parse_factors("totp,bogus"))
        self.assertIsNone(cli._parse_factors("totp,totp"))
        self.assertEqual(cli._parse_factors("totp, touchid"), ["totp", "touchid"])
        pol = {}
        cli._apply_factor_choice(pol, ["totp", "touchid"])
        self.assertEqual((pol["factors"]["required"], pol["factors"]["require_one_of"]),
                         (2, ["touchid"]))

    @unittest.skipIf(IS_WINDOWS, "needs a POSIX pty to answer the prompts")
    def test_interactive_enable_offers_totp_and_needs_the_code(self):
        self.no_platform()
        self.enrol_totp()
        out, rc = self.in_pty(["policy", "enable"], [("[y/N]", "y"), ("> ", self.code())])
        self.assertEqual(rc, 0, out[-400:].replace(self.code(), "<code>"))
        self.assertIn("policy saved (unlock ON)", out)
        with open(os.path.join(self.home, "policy.json"), encoding="utf-8") as f:
            pol = json.load(f)
        self.assertEqual(pol["factors"]["chosen"]["factors"], ["totp"])

    def in_pty(self, args, script, timeout=30):
        import pty
        import select
        env = self.cli_env()                    # BEFORE the fork: it names this pid
        pid, fd = pty.fork()
        if pid == 0:                                       # child
            try:
                os.execve(PYTHON, [PYTHON, str(CLI)] + list(args), env)
            finally:
                os._exit(127)
        out, steps = "", list(script)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                r, _w, _x = select.select([fd], [], [], 0.2)
                if r:
                    try:
                        chunk = os.read(fd, 4096).decode("utf-8", "replace")
                    except OSError:
                        break
                    if not chunk:
                        break
                    out += chunk
                if steps and steps[0][0] in out:
                    out = out.replace(steps[0][0], "<prompted>", 1)
                    time.sleep(0.2)
                    os.write(fd, (steps.pop(0)[1] + "\n").encode())
        finally:
            # bounded: a child still running at the deadline is killed, never waited on forever
            done, status = os.waitpid(pid, os.WNOHANG)
            if not done:
                import signal
                os.kill(pid, signal.SIGKILL)
                _p, status = os.waitpid(pid, 0)
            os.close(fd)
        return out, os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") \
            else status >> 8


class PlatformOptional(AuthorityCase):
    """M5 (Windows / a Mac with a sensor): the platform factor is optional, but only a person's
    terminal may skip it -- never Claude Code's shell."""

    def test_skip_platform_is_refused_from_a_claude_session(self):
        shim = self.child()
        r = self.call(shim, "enroll", {"factor": "totp", "phase": "begin",
                                       "skip_platform": True})
        self.assertEqual(r["error"]["code"], "platform_first")
        self.assertIn("own terminal", r["error"]["message"])
        r = self.call(shim, "enroll", {"factor": "totp", "phase": "begin"})
        self.assertIn("--without-platform", r["error"]["message"])

    def test_skip_platform_from_a_persons_terminal_enrols_totp(self):
        import gt_unlockd_methods as M
        self.claude_pids.clear()                  # the child is now a terminal, not a session
        self.auth.interactive = lambda ident: True
        old = M._has_tty
        M._has_tty = lambda ident: True
        try:
            shim = self.child()
            r = self.call(shim, "enroll", {"factor": "totp", "phase": "begin",
                                           "skip_platform": True})
        finally:
            M._has_tty = old
        self.assertIn("result", r, r.get("error"))
        self.assertTrue(r["result"]["uri"].startswith("otpauth://"))

    def test_skip_platform_without_a_tty_is_refused(self):
        self.claude_pids.clear()
        self.auth.interactive = lambda ident: False
        shim = self.child()
        r = self.call(shim, "enroll", {"factor": "totp", "phase": "begin",
                                       "skip_platform": True})
        self.assertEqual(r["error"]["code"], "platform_first")


# ============================================================================ M6: one code

class OneCodePerCommand(AuthorityCase):
    """M6: a security setting change (gt:settings:security, step_up) on a TOTP-only machine
    asked for an unlock code and then a step-up code -- two codes from two 30 s windows."""

    def setUp(self):
        super().setUp()
        self.platform.available = lambda: (False, "test: no sensor")
        self.enrol_totp()
        self.set_policy(factors={"required": 1, "require_one_of": []})

    def test_the_unlock_just_collected_is_the_step_up(self):
        shim = self.child()
        r = self.call(shim, "check", {"scope": "gt:settings:security", "request": True,
                                      "tty": True}, answers=[self.code()])
        self.assertTrue(r["result"]["allowed"], r)

    def test_an_earlier_grant_still_steps_up(self):
        shim = self.child()
        self.assertIn("result", self.call(shim, "unlock", {"tty": True},
                                          answers=[self.code()]))
        r = self.call(shim, "check", {"scope": "gt:settings:security", "request": True,
                                      "tty": True}, answers=[])
        self.assertFalse(r["result"]["allowed"], r)
        r = self.call(shim, "check", {"scope": "gt:settings:security", "request": False})
        self.assertEqual(r["result"]["code"], "step_up")

    def test_a_recovery_code_unlock_is_not_a_step_up(self):
        self.assertFalse(self.auth.satisfies_step_up(["recovery"]))
        self.assertTrue(self.auth.satisfies_step_up(["totp"]))
        self.platform.available = lambda: (True, "test key")
        self.enrol_platform()
        self.assertFalse(self.auth.satisfies_step_up(["totp"]))
        self.assertTrue(self.auth.satisfies_step_up(["totp", "touchid"]))

    def test_replay_is_still_refused_and_says_how_long_to_wait(self):
        shim = self.child()
        code = self.code()
        self.assertIn("result", self.call(shim, "unlock", {"tty": True}, answers=[code]))
        self.call(shim, "lock")
        r = self.call(shim, "unlock", {"tty": True}, answers=[code])
        self.assertEqual(r["error"]["code"], "factor_replayed", r)
        self.assertIn("wait for the next code (about", r["error"]["message"])


class ReplayMessage(unittest.TestCase):

    def test_seconds_until_the_next_code(self):
        st = {"last_step": 100}
        self.assertIn("about 11 s", F.replayed_message(st, now=100 * 30 + 20))
        self.assertIn("enter the code your app shows now", F.replayed_message(st, now=101 * 30 + 5))


class SettingsOneCode(Sandbox):
    """M6 in gt_settings: `set unlock on` asked the gate for a code and then policy enable for
    another; a Linux sandbox change asked for codes before finding bwrap missing."""

    def setUp(self):
        super().setUp()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.config(vault_path=str(self.tmp))
        self.S = load_module(SCRIPTS / "gt_settings.py", "gt_settings_usability")
        self.S.CONFIG = str(self.home / ".claude" / "vault-config.json")
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home

    def _no_gate(self, name):
        raise AssertionError("the unlock gate was asked for %s" % name)

    def test_set_unlock_asks_policy_enable_only(self):
        import subprocess as sp
        calls = []
        self.S._unlock_gate = self._no_gate
        real = sp.call
        sp.call = lambda argv, *a, **k: calls.append(argv) or 0
        try:
            with redirect_stdout(io.StringIO()):
                rc = self.S.set_value("unlock", "on")
        finally:
            sp.call = real
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][-2:], ["policy", "enable"])

    def test_linux_sandbox_prereqs_are_checked_before_any_code(self):
        import gt_sandbox
        old = (gt_sandbox.platform_mode, gt_sandbox.linux_deps_missing)
        gt_sandbox.platform_mode = lambda: "linux"
        gt_sandbox.linux_deps_missing = lambda: ["bwrap", "socat"]
        self.S._unlock_gate = self._no_gate
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                rc = self.S.set_value("sandbox_mode", "on")
        finally:
            gt_sandbox.platform_mode, gt_sandbox.linux_deps_missing = old
        self.assertEqual(rc, 1)
        text = out.getvalue()
        self.assertIn("no code was asked for", text)
        self.assertIn("bubblewrap socat", text)
        self.assertNotIn("--force", text)

    def test_package_hint_follows_the_distro(self):
        rel = self.tmp / "os-release"
        cases = {'ID=fedora\n': "dnf", 'ID=ubuntu\nID_LIKE=debian\n': "apt-get",
                 'ID="rocky"\nID_LIKE="rhel centos fedora"\n': "dnf",
                 'ID=arch\n': "pacman", 'ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n':
                 "zypper"}
        for text, want in cases.items():
            rel.write_text(text, encoding="utf-8")
            hint = self.S._linux_package_hint(["bubblewrap", "socat"], os_release=str(rel),
                                              which=lambda n: None)
            self.assertIn(want, hint, text)
        hint = self.S._linux_package_hint(["socat"], os_release=str(self.tmp / "none"),
                                          which=lambda n: "/usr/bin/zypper" if n == "zypper"
                                          else None)
        self.assertIn("zypper install socat", hint)


# ============================================================================ M12: old code

class AuthorityRestartsOnUpgrade(Sandbox):
    """M12: the unlock authority survived reinstall, rollback and roll-forward on the same pid,
    running the old code. A fixture daemon runs from a copy of the scripts; the copy is then
    "upgraded" and `daemon restart-if-stale` must end it (dropping grants, never granting)."""

    def setUp(self):
        super().setUp()
        self.inst = self.tmp / "hooks"
        shutil.copytree(str(SCRIPTS), str(self.inst),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.env["GT_UNLOCK_NO_START"] = "0"            # this test starts the real daemon
        self.uhome = self.home / ".claude" / "golden-thread" / "unlock"
        self.addCleanup(self._stop_all)

    def cli(self, *args):
        return self.run_cmd([PYTHON, self.inst / "gt_unlock.py"] + list(args), timeout=90)

    def addr(self):
        return gt_ipc.default_address(str(self.uhome), "unlockd")

    def pid(self):
        try:
            return int((self.uhome / "unlockd.pid").read_text().strip())
        except (OSError, ValueError):
            return None

    def _stop_all(self):
        self.cli("daemon", "stop")
        pid = self.pid()                        # read once: a stopping daemon removes it
        if pid and gt_ipc.alive_at(self.addr(), timeout=0.3):
            import signal
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.monotonic() + 10
        while gt_ipc.alive_at(self.addr(), timeout=0.3) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(gt_ipc.alive_at(self.addr(), timeout=0.3), "leaked an unlock daemon")

    def upgrade(self, name="gt_unlock_totp.py"):
        p = self.inst / name
        p.write_text(p.read_text(encoding="utf-8") + "\n# upgraded\n", encoding="utf-8")

    def test_current_code_is_left_running(self):
        self.assertOk(self.cli("daemon", "start"))
        pid = self.pid()
        r = self.cli("daemon", "restart-if-stale")
        self.assertOk(r)
        self.assertIn("current code", r.stdout)
        self.assertEqual(self.pid(), pid)
        self.assertTrue(gt_ipc.alive_at(self.addr(), timeout=0.5))

    def test_changed_code_is_reported_then_stopped(self):
        self.assertOk(self.cli("daemon", "start"))
        pid = self.pid()
        self.upgrade()
        r = self.cli("daemon", "status")
        self.assertIn("running old code (pid %d" % pid, r.stdout)
        self.assertIn("gt_unlock.py daemon restart-if-stale", r.stdout)
        r = self.cli("verify")
        self.assertIn("authority-code", r.stdout)
        self.assertIn("old code", r.stdout)
        r = self.cli("daemon", "restart-if-stale")
        self.assertOk(r)
        self.assertIn("old code (pid %d); stopped it" % pid, r.stdout)
        self.assertIn("unlock again", r.stdout)
        deadline = time.monotonic() + 10
        while gt_ipc.alive_at(self.addr(), timeout=0.3) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(gt_ipc.alive_at(self.addr(), timeout=0.3))
        audit = (self.uhome / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn('"reason":"code_updated"', audit)

    def test_an_authority_from_before_the_check_is_stopped_too(self):
        # 0.20.0's authority has no code_status / stop_if_stale method
        m = self.inst / "gt_unlockd_methods.py"
        new = m.read_text(encoding="utf-8")
        old = new.replace("def code_status(", "def _gone_code_status(").replace(
            "def stop_if_stale(", "def _gone_stop_if_stale(")
        m.write_text(old, encoding="utf-8")
        self.assertOk(self.cli("daemon", "start"))
        pid = self.pid()
        m.write_text(new, encoding="utf-8")               # the upgrade lands
        r = self.cli("daemon", "restart-if-stale")
        self.assertOk(r)
        self.assertIn("old code (pid %d); stopped it" % pid, r.stdout)
        deadline = time.monotonic() + 10
        while gt_ipc.alive_at(self.addr(), timeout=0.3) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(gt_ipc.alive_at(self.addr(), timeout=0.3))

    def test_nothing_running_is_silent(self):
        r = self.cli("daemon", "restart-if-stale")
        self.assertOk(r)
        self.assertEqual(r.stdout, "")


class ConsentCooldown(AuthorityCase):
    """Review 2026-10-04: a refused platform consent set no cooldown, so a session could raise
    Touch ID / Hello prompts back to back. Denied once -> the next ask inside the cooldown is
    refused without a prompt; nothing is ever auto-approved."""

    OP = {"tool": "call_consent", "connection": "github@personal", "op": "merge_pull",
          "args_sha256": "ab" * 32}

    def test_a_denied_consent_cools_down_before_the_next_prompt(self):
        self.standard()
        self.set_policy(consent_requires_factor="platform")
        shim = self.child()
        self.assertIn("result", self.call(shim, "unlock", {"tty": True},
                                          answers=[self.code()]))
        sh = self.send(shim, {"pid": True})
        me = {"pid": os.getpid(), "start": gt_ipc.process_info(os.getpid())["start"]}
        consent = self.srv.methods["consent"]
        params = {"op": dict(self.OP), "subject": {"pid": sh["pid"], "start": sh["start"]}}
        self.platform.mode = "cancel"
        calls = self.platform.calls
        with self.assertRaises(D.Denied) as cm:
            consent(me, dict(params))
        self.assertEqual(cm.exception.code, "consent_denied")
        self.assertIn("wait", cm.exception.message)
        self.assertEqual(self.platform.calls, calls + 1)
        self.platform.mode = "ok"                     # even a willing finger is not asked
        with self.assertRaises(D.Denied) as cm:
            consent(me, dict(params))
        self.assertEqual(cm.exception.code, "cooldown")
        self.assertRegex(cm.exception.message, r"wait \d+ s")
        self.assertEqual(self.platform.calls, calls + 1, "a prompt was raised in the cooldown")
        self.auth.cooldown.clear()                    # past the cooldown: asks, and approves
        r = consent(me, dict(params))
        self.assertTrue(r["approved"])
        self.assertEqual(self.platform.calls, calls + 2)


class StopIfStaleNeverGrants(AuthorityCase):

    def test_current_code_is_not_stopped_and_stale_code_drops_grants(self):
        self.standard()
        shim = self.child()
        self.assertIn("result", self.call(shim, "unlock", {"tty": True},
                                          answers=[self.code()]))
        r = self.call(shim, "stop_if_stale")
        self.assertFalse(r["result"]["stopping"])
        self.assertFalse(self.stop.is_set())
        self.assertEqual(len(self.auth.grants), 1)
        self.auth.code = "0" * 64                         # as if the files changed under it
        r = self.call(shim, "stop_if_stale")
        self.assertTrue(r["result"]["stopping"])
        self.assertEqual(r["result"]["revoked"], 1)
        self.assertEqual(self.auth.grants, {})
        self.assertEqual(self.auth.last_revoked["why"], "code_updated")


# ============================================================================ MINOR

class UnlockWording(AuthorityCase):

    def test_screen_lock_revocation_shows_in_status(self):
        self.standard()
        shim = self.child()
        self.assertIn("result", self.call(shim, "unlock", {"tty": True},
                                          answers=[self.code()]))
        self.screen[0] = True
        self.auth.tick()
        st = self.auth.status()
        self.assertEqual(st["grants"], 0)
        self.assertEqual(st["last_revoked"]["why"], "screen_lock")
        line = _cli_module()._revoked_line(st)
        self.assertRegex(line, r"^revoked at \d\d:\d\d: screen locked$")

    def test_unlock_result_names_the_session_kind(self):
        self.standard()
        shim = self.child()
        r = self.call(shim, "unlock", {"tty": True}, answers=[self.code()])
        self.assertEqual(r["result"]["kind"], "claude")
        cli = _cli_module()
        cli._call = lambda *a, **k: {"grant": "ab" * 16, "factors": ["totp"], "ttl_s": 28800,
                                     "idle_s": 900, "session": 4242, "kind": "terminal"}
        out = io.StringIO()
        ns = cli.build_parser().parse_args(["unlock"])
        with redirect_stdout(out):
            cli.cmd_unlock(ns)
        self.assertIn("bound to this terminal's shell (pid 4242) only", out.getvalue())
        self.assertIn("Locking the screen", out.getvalue())

    def test_requester_reads_as_the_claude_code_session(self):
        shim = self.child()
        pid = self.send(shim, {"pid": True})["pid"]
        self.assertEqual(self.auth.describe(pid), "Claude Code session (pid %d)" % os.getpid())
        self.claude_pids.discard(os.getpid())               # outside a session: the script
        self.assertEqual(self.auth.describe(pid), "_unlock_child.py (pid %d)" % pid)

    def test_grandchild_is_named_as_a_command_in_the_session(self):
        shim = self.child()
        self.send(shim, {"spawn": True})
        gpid = self.send(shim, {"pid": True})["pid"]
        try:
            text = self.auth.describe(gpid)
        finally:
            self.send(shim, {"unspawn": True})
        self.assertIn("a command in Claude Code session (pid %d)" % os.getpid(), text)
        self.assertIn("_unlock_child.py (pid %d)" % gpid, text)


class NextStepAgrees(unittest.TestCase):
    """status and enrolment's "Next:" give one answer, never an unavailable factor."""

    def st(self, enrolled=(), avail=("totp",), recovery=False, required=1, one_of=(),
           enabled=False):
        f = {}
        for n in ("totp", "touchid", "hello", "sso"):
            f[n] = {"enrolled": n in enrolled, "available": n in avail, "why": "x"}
        f["recovery"] = {"enrolled": recovery, "available": True, "why": ""}
        return {"factors": f, "required": required, "require_one_of": list(one_of),
                "enabled": enabled}

    def test_sequence_on_a_totp_only_mac(self):
        cli = _cli_module()
        mac = dict(required=2, one_of=["touchid"])
        self.assertEqual(cli.next_step(self.st(**mac)), "gt_unlock.py enroll totp")
        self.assertIn("recovery", cli.next_step(self.st(enrolled=["totp"], **mac)))
        nxt = cli.next_step(self.st(enrolled=["totp"], recovery=True, **mac))
        self.assertIn("policy enable --factors totp", nxt)
        self.assertNotIn("touchid", nxt)

    def test_linux_and_a_platform_machine(self):
        cli = _cli_module()
        self.assertEqual(cli.next_step(self.st(enrolled=["totp"], recovery=True)),
                         "gt_unlock.py policy enable")
        self.assertIsNone(cli.next_step(self.st(enrolled=["totp"], recovery=True,
                                                enabled=True)))
        nxt = cli.next_step(self.st(avail=("totp", "hello"), required=2, one_of=["hello"]))
        self.assertTrue(nxt.startswith("gt_unlock.py enroll hello"))
        self.assertIn("--without-platform", nxt)

    def test_status_never_lists_a_factor_this_os_cannot_have(self):
        cli = _cli_module()
        st = self.st(enrolled=["totp"], recovery=True, enabled=True)
        st.update({"assurance": {"level": "L1", "why": "x"}, "problems": [], "door": "mcp_only",
                   "grants": 0, "sealed": [], "admin_floor": False})
        saved = (cli.C.enabled, cli.C.code_status)        # the shared client module
        cli.C.enabled = lambda h=None: True
        cli.C.code_status = lambda h=None: None
        cli._call = lambda *a, **k: st
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                cli.cmd_status(cli.build_parser().parse_args(["status"]))
        finally:
            cli.C.enabled, cli.C.code_status = saved
        self.assertNotIn("touchid", out.getvalue())
        self.assertNotIn("hello", out.getvalue())
        self.assertIn("K = 1: how many must agree to unlock", out.getvalue())


class EnrollAtEof(CliAgainstFixture):

    def test_enroll_totp_at_eof_says_one_line(self):
        self.no_platform()
        r = self.cli("enroll", "totp", input="")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("EOFError", r.stderr)
        self.assertIn("TOTP is NOT enrolled", r.stderr)
        self.assertFalse(os.path.exists(F.TotpFactor.seed_path(self.home)))


if __name__ == "__main__":
    unittest.main()
