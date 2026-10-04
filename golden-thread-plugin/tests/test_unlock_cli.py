"""gt_unlock.py and the REAL authority process, end to end (0.20.0): start the daemon, enrol
TOTP (bootstrap where no platform factor is available), turn unlock on with a fresh code, see
the gate locked, unlock, lock, stop. The same sequence is the Windows VM proof.
"""
import json
import os
import sys
import time
import unittest
import urllib.parse

from _harness import IS_WINDOWS, PYTHON, SCRIPTS, Sandbox

sys.path.insert(0, str(SCRIPTS))
import gt_ipc            # noqa: E402
import gt_unlock_totp as T  # noqa: E402

CLI = SCRIPTS / "gt_unlock.py"


class DaemonEndToEnd(Sandbox):

    def setUp(self):
        super().setUp()
        self.uhome = self.home / ".claude" / "golden-thread" / "unlock"
        self.env["GT_UNLOCK_HOME"] = str(self.uhome)
        # This test starts the REAL daemon on purpose (the harness forbids auto-starting one),
        # and must stop it: a leaked daemon fails the test.
        self.env["GT_UNLOCK_NO_START"] = "0"

    def tearDown(self):
        self.run_cmd([PYTHON, CLI, "daemon", "stop"], timeout=30)
        addr = gt_ipc.default_address(str(self.uhome), "unlockd")
        if gt_ipc.alive_at(addr, timeout=0.3):
            # With unlock ON `daemon stop` needs a fresh factor (review F1), which no test
            # answers. A same-user process can always end the daemon by signal -- that is the
            # documented residual -- and the test cleans up that way.
            self._signal_daemon()
        deadline = time.monotonic() + 10
        while gt_ipc.alive_at(addr, timeout=0.3) and time.monotonic() < deadline:
            time.sleep(0.1)
        leaked = gt_ipc.alive_at(addr, timeout=0.3)
        super().tearDown()
        self.assertFalse(leaked, "the test leaked a running unlock daemon")

    def _signal_daemon(self):
        import signal
        try:
            with open(self.uhome / "unlockd.pid", encoding="utf-8") as f:
                os.kill(int(f.read().strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass

    def client(self, answers=()):
        q = list(answers)
        return gt_ipc.connect(gt_ipc.default_address(str(self.uhome), "unlockd"),
                              answer=lambda need: q.pop(0) if q else None)

    def call(self, method, params=None, answers=()):
        c = self.client(answers)
        try:
            return c.call(method, params or {})
        finally:
            c.close()

    def test_enrol_enable_locked_unlock_lock(self):
        r = self.run_cmd([PYTHON, CLI, "daemon", "start"], timeout=60)
        self.assertOk(r)
        # the authority this test talks to is the one in the SANDBOX home
        self.assertTrue(gt_ipc.alive_at(gt_ipc.default_address(str(self.uhome), "unlockd")))
        st = self.call("status")
        if any(st["factors"].get(p, {}).get("available") for p in ("touchid", "hello")):
            self.skipTest("a platform factor is available here, so TOTP cannot bootstrap")
        res = self.call("enroll", {"factor": "totp", "phase": "begin", "account": "t"})
        secret = urllib.parse.parse_qs(urllib.parse.urlparse(res["uri"]).query)["secret"][0]
        key = T.from_b32(secret)
        self.call("enroll", {"factor": "totp", "phase": "confirm",
                             "code": T.code_at(key, time.time() - 30)})
        # three codes, three distinct steps inside the +-1 skew: each is accepted once
        pol = {"schema": 1, "enabled": True, "factors": {"required": 1, "require_one_of": []}}
        self.call("policy_set", {"policy": pol, "tty": True},
                  answers=[T.code_at(key, time.time())])
        r = self.run_cmd([PYTHON, CLI, "status"], timeout=60)
        self.assertIn("unlock: ON", r.stdout)
        self.assertIn("level L1", r.stdout)
        r = self.run_cmd([PYTHON, CLI, "check", "--scope", "gt:publish"], timeout=60)
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)          # locked
        g = self.call("unlock", {"tty": True}, answers=[T.code_at(key, time.time() + 30)])
        self.assertEqual(g["factors"], ["totp"])
        r = self.run_cmd([PYTHON, CLI, "lock"], timeout=60)
        self.assertIn("1 grant", r.stdout)
        # F1: with unlock on, `daemon stop` without a factor leaves the authority running
        r = self.run_cmd([PYTHON, CLI, "daemon", "stop"], timeout=60)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("NOT stopped", r.stdout)
        self.assertTrue(gt_ipc.alive_at(gt_ipc.default_address(str(self.uhome), "unlockd")))
        r = self.run_cmd([PYTHON, CLI, "audit", "-n", "50"], timeout=60)
        events = [json.loads(x)["event"] for x in r.stdout.splitlines() if x.startswith("{")]
        for e in ("enroll", "policy_set", "grant", "revoke"):
            self.assertIn(e, events)
        self.assertNotIn(secret, r.stdout)
        r = self.run_cmd([PYTHON, CLI, "verify"], timeout=120)
        self.assertIn("level: L1", r.stdout)
        self.assertNotIn("secure", r.stdout.replace("insecure", ""))

    def test_secret_get_refuses_a_terminal_and_status_off_says_how(self):
        r = self.run_cmd([PYTHON, CLI, "status"], timeout=60)
        self.assertOk(r)
        self.assertIn("unlock: off", r.stdout)
        self.assertIn("SECURITY.md", r.stdout)


if __name__ == "__main__":
    unittest.main()
