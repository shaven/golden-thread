"""Regression tests for the independent RE-review of gt 0.20.x (2026-10-04 01:13 CDT), the
unlock side: H2, L1, M3, L4, L5 (the review's numbering).

Each test is one of the re-review's demonstrations (Projects/golden-thread/research/
review2-0.20.0-harnesses/: harnesses1/test_pty_orphan.py, ptytrick.py, sc/sitecustomize.py)
turned into an assertion that the bypass is closed. Every one of them fails against efa0620,
the re-reviewed commit, and passes after the fix. Nothing here raises real UI: the platform
factor is the fixture's software key, the Touch ID helper is a shell script.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _harness import IS_WINDOWS, PYTHON, REPO, SCRIPTS, rmtree
from _unlock_fixture import AuthorityCase

sys.path.insert(0, str(SCRIPTS))
import gt_ipc                    # noqa: E402
import gt_unlock_factors as F    # noqa: E402
import gt_unlockd as D           # noqa: E402


def _wait_for(path, secs=10):
    deadline = time.monotonic() + secs
    while not os.path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.05)
    return os.path.exists(path)


# ------------------------------------------------------------------------------------ H2
PTY_ORPHAN = r'''
import os, sys, json, time, fcntl, termios
sys.path.insert(0, %(scripts)r)
if os.fork(): os._exit(0)
os.setsid()
if os.fork(): os._exit(0)
time.sleep(0.3)
import gt_ipc
out = {}
def ask():
    r = {}
    for scope in ("lotr:github:read", "gt:vault:read"):
        c = gt_ipc.connect(%(addr)r)
        try:
            r[scope] = c.call("check", {"scope": scope})
        finally:
            c.close()
    return r
out["no_pty"] = ask()
# The re-review's trick: a new session, then a controlling terminal of its own.
os.setsid()
m, s = os.openpty()
try:
    fcntl.ioctl(s, termios.TIOCSCTTY, 0)
except OSError:
    pass
out["tty"] = (gt_ipc.process_info(os.getpid()) or {}).get("tty")
out["pty"] = ask()
open(%(res)r + ".tmp", "w").write(json.dumps(out))
os.replace(%(res)r + ".tmp", %(res)r)
'''


class PtyOrphan(AuthorityCase):
    """H2: the F6 fix asked only "has a controlling tty?". An orphan (double fork + setsid,
    re-parented to launchd / init) that gave ITSELF a tty with openpty + TIOCSCTTY got
    lotr:*:read and gt:vault:read through read_without_unlock."""

    def setUp(self):
        super().setUp()
        self.standard()

    @unittest.skipIf(IS_WINDOWS, "os.fork / setsid / openpty are POSIX")
    def test_H2_an_orphan_with_its_own_pty_gets_no_read_without_unlock(self):
        res = os.path.join(self.tmp, "pty-orphan.json")
        subprocess.run([PYTHON, "-c", PTY_ORPHAN % {"scripts": str(SCRIPTS), "addr": self.addr,
                                                    "res": res}],
                       timeout=20, stdin=subprocess.DEVNULL)
        self.assertTrue(_wait_for(res, 15), "the orphan did not report")
        with open(res, encoding="utf-8") as f:
            out = json.load(f)
        # The demonstration really ran: the orphan HAD a controlling terminal when it asked.
        self.assertTrue(out["tty"], out)
        for phase in ("no_pty", "pty"):
            for scope, v in out[phase].items():
                self.assertFalse(v["allowed"], (phase, scope, v))
                self.assertEqual(v["code"], "locked", (phase, scope, v))

    def test_a_terminal_with_a_login_shell_keeps_read_without_unlock(self):
        """The convenience is kept for a person: a tty AND a login shell above the process."""
        me = {"pid": os.getpid(), "start": gt_ipc.process_info(os.getpid())["start"]}
        self.auth.is_claude = lambda info: False                # this process is a terminal's
        self.auth._interactive = lambda ident: True
        D_has_tty = D._has_tty
        D._has_tty = lambda ident: True
        try:
            v = self.auth.evaluate(me, "lotr:github:read")
            self.assertTrue(v["allowed"], v)
            self.assertEqual(v["code"], "open")
            self.auth._interactive = lambda ident: False        # same process, now an orphan
            v = self.auth.evaluate(me, "lotr:github:read")
            self.assertFalse(v["allowed"], v)
        finally:
            D._has_tty = D_has_tty


class InteractiveChain(unittest.TestCase):
    """H2's decision, on synthetic process chains, so it runs on every platform."""

    @staticmethod
    def chain(*procs):
        return [{"pid": i + 100, "start": str(i), "comm": c} for i, (c, _a) in enumerate(procs)]

    def decide(self, procs, windows):
        args = {i + 100: a for i, (_c, a) in enumerate(procs)}
        return gt_ipc.interactive_chain(self.chain(*procs), args_of=lambda pid: args[pid],
                                        windows=windows)

    def test_posix(self):
        py = ("Python", ["python3", "x.py"])
        self.assertFalse(self.decide([py], False))                         # the orphan itself
        self.assertFalse(self.decide([py, ("zsh", ["/bin/zsh", "-c", "x"])], False))
        self.assertFalse(self.decide([py, ("Python", ["python3", "o.py"])], False))
        self.assertTrue(self.decide([py, ("zsh", ["-zsh"])], False))       # Terminal / login
        self.assertTrue(self.decide([py, ("zsh", ["/bin/zsh", "--login"])], False))
        self.assertTrue(self.decide([py, ("bash", ["bash", "-il"])], False))
        self.assertTrue(self.decide([py, ("tmux", ["tmux"]), ("zsh", ["-zsh"])], False))
        self.assertFalse(self.decide([], False))

    def test_windows(self):
        py = ("python.exe", ["python", "x.py"])
        self.assertFalse(self.decide([py], True))            # parent gone / reused: orphan
        self.assertFalse(self.decide([py, ("python.exe", ["python", "o.py"])], True))
        self.assertTrue(self.decide([py, ("pwsh.exe", ["pwsh"])], True))
        self.assertTrue(self.decide([py, ("cmd.exe", ["cmd"]), ("explorer.exe", [])], True))

    def test_login_shell_detection(self):
        self.assertTrue(gt_ipc.is_login_shell(["-bash"]))
        self.assertTrue(gt_ipc.is_login_shell(["/bin/zsh", "-l"]))
        self.assertFalse(gt_ipc.is_login_shell(["/bin/zsh", "-c", "echo -l"]))
        self.assertFalse(gt_ipc.is_login_shell(["-python3"]))
        self.assertFalse(gt_ipc.is_login_shell(["python3", "-l"]))
        self.assertFalse(gt_ipc.is_login_shell([]))


# ------------------------------------------------------------------------------------ L1
SITECUSTOMIZE = "open(%r, 'w').write('injected')\n"


class IsolatedInterpreter(unittest.TestCase):
    """L1: realpath identity ("this process runs the INSTALLED lotr_mcp.py") was defeated by
    PYTHONPATH pointing at a sitecustomize.py: arbitrary code ran inside the real file. A
    trusted python peer must now have been started with -I (or -E and -s)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gtl1-")
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait(5)
        rmtree(self.tmp)

    def spawn(self, script, flags=(), env_extra=None):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.update(env_extra or {})
        p = subprocess.Popen([PYTHON] + list(flags) + [script], env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        self.procs.append(p)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            info = gt_ipc.process_info(p.pid)
            if info and gt_ipc.main_script(p.pid):
                return {"pid": p.pid, "start": info["start"]}
            time.sleep(0.05)
        self.fail("the child did not start")

    def installed_lotrd(self):
        plugin = os.path.join(self.tmp, "cache", "gt-lotr", "0.3.0")
        os.makedirs(os.path.join(plugin, "scripts"))
        script = os.path.join(plugin, "scripts", "lotrd.py")
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write("import time\ntime.sleep(30)\n")
        plugins = os.path.join(self.tmp, "installed_plugins.json")
        with open(plugins, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "plugins": {"gt-lotr@golden-thread-plugin": [
                {"scope": "user", "installPath": plugin, "version": "0.3.0"}]}}, f)
        return script, plugins

    def test_L1_sitecustomize_injection_into_the_installed_file_is_refused(self):
        script, plugins = self.installed_lotrd()
        sc = os.path.join(self.tmp, "sc")
        os.makedirs(sc)
        mark = os.path.join(self.tmp, "mark")
        with open(os.path.join(sc, "sitecustomize.py"), "w", encoding="utf-8") as f:
            f.write(SITECUSTOMIZE % mark)
        injected = self.spawn(script, env_extra={"PYTHONPATH": sc})
        self.assertTrue(_wait_for(mark, 5), "the injection did not run -- the test proves "
                                            "nothing")
        self.assertFalse(D.is_lotr_daemon(injected, plugins_file=plugins))
        self.assertFalse(D.is_lotr_shim(injected, plugins_file=plugins))
        # The same installed file, started isolated, is the consumer -- even with the same
        # PYTHONPATH in its environment (-I ignores it).
        os.unlink(mark)
        clean = self.spawn(script, flags=["-I"], env_extra={"PYTHONPATH": sc})
        self.assertTrue(D.is_lotr_daemon(clean, plugins_file=plugins))
        self.assertFalse(os.path.exists(mark))

    def test_isolation_problem_names_variables_never_values(self):
        script = os.path.join(self.tmp, "s.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import time\ntime.sleep(30)\n")
        self.assertIsNone(gt_ipc.isolation_problem(self.spawn(script, ["-I"])["pid"]))
        self.assertIsNone(gt_ipc.isolation_problem(self.spawn(script, ["-E", "-s"])["pid"]))
        self.assertIsNone(gt_ipc.isolation_problem(self.spawn(script, ["-IB"])["pid"]))
        self.assertIsNotNone(gt_ipc.isolation_problem(self.spawn(script, ["-E"])["pid"]))
        why = gt_ipc.isolation_problem(self.spawn(script)["pid"])
        self.assertIn("-I", why)
        value = os.path.join(self.tmp, "VALUE-NEVER-SHOWN")
        why = gt_ipc.isolation_problem(self.spawn(script, env_extra={"PYTHONPATH": value})["pid"])
        if not IS_WINDOWS:                    # Windows: environments are not read (documented)
            self.assertIn("PYTHONPATH", why)
        self.assertNotIn("VALUE-NEVER-SHOWN", why)

    def test_python_flags(self):
        self.assertEqual(gt_ipc.python_flags(["python3", "-IB", "x.py"]), {"I", "B"})
        self.assertEqual(gt_ipc.python_flags(["python3", "-X", "dev", "-I", "x.py"]), {"I"})
        self.assertEqual(gt_ipc.python_flags(["python3", "-Xutf8", "-E", "-s", "x.py"]),
                         {"E", "s"})
        self.assertEqual(gt_ipc.python_flags(["python3", "x.py", "-I"]), set())

    def test_windows_localized_servers_keep_utf8_under_isolation(self):
        """-I ignores PYTHONUTF8, which localize_mcp sets for Windows servers: it adds the
        option form (-X utf8) after -I, and the server still reads as isolated."""
        import gt_components as GC
        man = os.path.join(self.tmp, "p", ".claude-plugin", "plugin.json")
        os.makedirs(os.path.dirname(man))
        with open(man, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"s": {"command": "python3",
                                            "args": ["-I", "${CLAUDE_PLUGIN_ROOT}/s.py"]}}}, f)
        GC.localize_mcp([man], os.path.realpath(sys.executable))
        with open(man, encoding="utf-8") as f:
            args = json.load(f)["mcpServers"]["s"]["args"]
        self.assertEqual(args, ["-I", "-X", "utf8", "${CLAUDE_PLUGIN_ROOT}/s.py"])
        self.assertEqual(gt_ipc.python_flags(["python"] + args), {"I"})

    def test_gt_starts_its_own_daemons_and_servers_isolated(self):
        for man, server in ((REPO / "golden-thread" / SCRIPTS.parent.name / ".claude-plugin"
                             / "plugin.json", "gt-vault"),
                            (sorted((REPO / "golden-thread-lotr").glob("*/.claude-plugin/"
                                                                     "plugin.json"))[-1],
                             "gt-lotr")):
            args = json.loads(man.read_text(encoding="utf-8"))["mcpServers"][server]["args"]
            self.assertEqual(args[0], "-I", man)
        import gt_unlock_client as C
        seen = []

        class Fake:
            def __init__(self, cmd, **kw):
                seen.append(cmd)
        saved = (C.subprocess.Popen, C.gt_ipc.alive_at, os.environ.get("GT_UNLOCK_NO_START"))
        C.subprocess.Popen = Fake
        C.gt_ipc.alive_at = lambda addr, timeout=1.0: False
        os.environ.pop("GT_UNLOCK_NO_START", None)
        try:
            C.start_daemon(h=os.path.join(self.tmp, "uh"), wait=0)
        finally:
            C.subprocess.Popen, C.gt_ipc.alive_at = saved[0], saved[1]
            if saved[2] is not None:
                os.environ["GT_UNLOCK_NO_START"] = saved[2]
        self.assertEqual(len(seen), 1)
        self.assertIn("-I", seen[0][1:seen[0].index(next(a for a in seen[0]
                                                         if a.endswith("gt_unlockd.py")))])


# ------------------------------------------------------------------------------------ M3
class PolicyReplay(AuthorityCase):
    """M3: the F3 approval (a platform signature over the policy hash) had no serial or time,
    so an OLDER signed policy -- looser, once approved -- restored with its state.json was
    accepted again."""

    POL_A = {"schema": 1, "enabled": True,
             "factors": {"required": 2, "require_one_of": ["touchid"]}}
    POL_B = dict(POL_A, read_without_unlock=False)

    def setUp(self):
        super().setUp()
        self.standard()
        self.admin = self.child()

    def policy_set(self, pol, offset):
        self.auth.cooldown.clear()
        r = self.call(self.admin, "policy_set", {"policy": pol, "tty": True},
                      answers=[self.code(offset)])
        self.assertIn("result", r, r)

    def files(self):
        out = {}
        for n in ("policy.json", "state.json"):
            with open(os.path.join(self.home, n), "rb") as f:
                out[n] = f.read()
        return out

    def restore(self, files):
        for n, data in files.items():
            F.write_private(os.path.join(self.home, n), data)
        return self.auth.reload()

    def test_M3_an_older_signed_approval_restored_is_refused(self):
        self.policy_set(self.POL_A, 0)
        old = self.files()
        self.policy_set(self.POL_B, 30)
        new = self.files()
        self.assertFalse(self.auth.reload().failed_closed)
        eff = self.restore(old)                     # the replay: A's bytes and A's approval
        self.assertTrue(eff.failed_closed, eff.problems)
        self.assertIn("older than one already accepted", "; ".join(eff.problems))
        a = json.loads(old["state.json"])["policy_approval"]
        b = json.loads(new["state.json"])["policy_approval"]
        self.assertEqual((a["serial"], b["serial"]), (1, 2))
        self.assertIsInstance(a["ts"], int)
        # ... after a restart too (the floor is persisted) ...
        self.restart_authority()
        self.assertTrue(self.auth.reload().failed_closed)
        # ... and with the floor file deleted while the daemon runs (it is kept in memory).
        os.unlink(os.path.join(self.home, D.FLOOR_FILE))
        self.assertTrue(self.restore(old).failed_closed)

    def test_the_serial_is_inside_the_signature(self):
        self.policy_set(self.POL_A, 0)
        st = json.loads(self.files()["state.json"])
        st["policy_approval"]["serial"] = 99                  # bump it to beat the floor
        F.write_private(os.path.join(self.home, "state.json"),
                        (json.dumps(st) + "\n").encode())
        eff = self.auth.reload()
        self.assertTrue(eff.failed_closed)
        self.assertIn("does not verify", "; ".join(eff.problems))

    def test_a_serial_less_approval_counts_until_the_first_serial(self):
        """Migration: an approval written before M3 is serial 0 -- still accepted (the fixture's
        own set_policy writes one) until a serial'd approval has been accepted."""
        self.assertFalse(self.auth.reload().failed_closed)    # standard(): serial-less
        self.policy_set(self.POL_A, 0)
        self.set_policy(read_without_unlock=False)            # serial-less, validly signed
        eff = self.auth.reload()
        self.assertTrue(eff.failed_closed, eff.problems)


# ------------------------------------------------------------------------------------ L4
class SeatGrants(AuthorityCase):
    """L4: the vault MCP server's seat and LOTR's shim shared ONE session grant, so approving
    "gt vault MCP: read X" also unlocked every lotr:* call of the session."""

    def setUp(self):
        super().setUp()
        self.standard()
        self.set_policy(read_without_unlock=False)
        self.auth.vault_shim_ok = lambda peer: True
        self.lotr = self.child()
        self.vault = self.child()
        self.assertIn("result", self.call(self.lotr, "register_shim", {"role": "lotr"}))
        self.assertIn("result", self.call(self.vault, "register_shim", {"role": "vault"}))

    def check(self, proc, scope):
        return self.call(proc, "check", {"scope": scope})["result"]

    def test_L4_a_vault_unlock_does_not_unlock_lotr_and_back(self):
        self.assertEqual(self.check(self.vault, "gt:vault:read")["code"], "locked")
        r = self.call(self.vault, "unlock", {"tty": True, "scope": "gt:vault:read"},
                      answers=[self.code()])
        self.assertIn("result", r, r)
        self.assertTrue(self.check(self.vault, "gt:vault:read")["allowed"])
        v = self.check(self.lotr, "lotr:github:read")
        self.assertFalse(v["allowed"], v)
        self.assertEqual(v["code"], "locked")
        self.auth.cooldown.clear()
        r = self.call(self.lotr, "unlock", {"tty": True}, answers=[self.code(30)])
        self.assertIn("result", r, r)
        self.assertTrue(self.check(self.lotr, "lotr:github:read")["allowed"])
        self.assertEqual(len(self.auth.grants), 2)
        # a session-wide revocation still takes both
        self.call(self.lotr, "lock")
        self.assertEqual(self.auth.grants, {})


# ------------------------------------------------------------------------------------ L5
@unittest.skipIf(IS_WINDOWS, "the Touch ID helper is a POSIX executable")
class HelperSwap(unittest.TestCase):
    """L5: the helper was hashed by PATH and then executed by PATH, so a swap between the hash
    and the exec ran a binary nobody had hashed. The swap is injected deterministically right
    after the install-record check passes."""

    GOOD = '#!/bin/sh\ncat >/dev/null\necho \'{"ok": true, "who": "good"}\'\n'
    EVIL = '#!/bin/sh\ncat >/dev/null\ntouch %s\necho \'{"ok": true, "who": "evil"}\'\n'

    def setUp(self):
        import gt_unlock_touchid as T
        self.T = T
        self.tmp = tempfile.mkdtemp(prefix="gtl5-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin, mode=0o700)
        self.helper = os.path.join(self.bin, "gt-presence")
        with open(self.helper, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.GOOD)
        os.chmod(self.helper, 0o755)
        T.record_helper(self.helper)
        self.mark = os.path.join(self.tmp, "evil-ran")
        self.fac = T.TouchIdFactor(helper=self.helper, insecure_test_keys=True)

    def tearDown(self):
        rmtree(self.tmp)

    def test_L5_a_swap_after_the_hash_never_runs(self):
        T = self.T
        real = T.helper_problem
        helper, evil = self.helper, self.EVIL % self.mark

        def check_then_swap(path):
            why = real(path)                          # the check passes on the GOOD binary...
            tmp = path + ".swap"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(evil)                         # ...then the binary is replaced
            os.chmod(tmp, 0o755)
            os.replace(tmp, helper)
            return why
        T.helper_problem = check_then_swap
        try:
            try:
                out = self.fac._call("available", {})
            except F.FactorError as e:
                out = {"refused": e.code}
        finally:
            T.helper_problem = real
        self.assertFalse(os.path.exists(self.mark), "the swapped-in helper ran")
        self.assertNotEqual(out.get("who"), "evil", out)
        self.assertEqual(out.get("refused"), "unavailable", out)

    def test_the_recorded_helper_runs_from_a_private_copy_that_is_removed(self):
        out = self.fac._call("available", {})
        self.assertEqual(out.get("who"), "good")
        self.assertEqual([n for n in os.listdir(self.bin) if n.startswith(".gt-presence-run")],
                         [])

    def test_a_symlinked_helper_is_not_followed(self):
        T = self.T
        link = os.path.join(self.bin, "link")
        os.symlink(self.helper, link)
        shutil.copy(T.record_path(self.helper), T.record_path(link))
        with self.assertRaises(F.FactorError) as cm:
            T.private_copy(link)
        self.assertEqual(cm.exception.code, "unavailable")


if __name__ == "__main__":
    unittest.main()
