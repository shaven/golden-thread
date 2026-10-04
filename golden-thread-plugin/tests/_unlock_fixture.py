"""Shared fixture for the unlock tests (0.20.1): an authority on a real socket / pipe in a
throwaway home, test factors, and real client processes (tests/_unlock_child.py).

The platform factor here is a SOFTWARE P-256 key standing in for the Secure Enclave / TPM:
the authority still verifies a real ES256 signature over its own fresh challenge, so a forged
or replayed signature fails exactly as it would against the hardware. Nothing here prompts.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from _harness import IS_WINDOWS, PYTHON, SCRIPTS, rmtree

sys.path.insert(0, str(SCRIPTS))
import gt_ipc                     # noqa: E402
import gt_unlock_crypto as CR     # noqa: E402
import gt_unlock_factors as F     # noqa: E402
import gt_unlock_totp as T        # noqa: E402
import gt_unlockd as D            # noqa: E402
import _unlock_keys as K          # noqa: E402

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_unlock_child.py")


class SoftPlatform(F.Factor):
    """Touch ID stand-in. `mode`: ok | forge (sign with another key) | replay (return the
    first signature it ever made) | cancel."""
    platform = True

    def __init__(self, name="touchid"):
        self.name = name
        self.key = K.P256Key()
        self.mode = "ok"
        self.calls = 0
        self._first = None
        self.pad = os.urandom(32)

    def available(self):
        return True, "test key"

    def enroll(self, ctx):
        return {"public": base64.b64encode(self.key.public_bytes()).decode()}

    def _signature(self, challenge):
        if self.mode == "forge":
            return K.P256Key().sign_der(challenge)
        if self.mode == "replay" and self._first is not None:
            return self._first
        sig = self.key.sign_der(challenge)
        if self._first is None:
            self._first = sig
        return sig

    def prove(self, record, challenge, ctx):
        self.calls += 1
        if self.mode == "cancel":
            raise F.FactorError("cancelled", "cancelled")
        sig = self._signature(challenge)
        if not CR.es256_verify(base64.b64decode(record["public"]), challenge, sig):
            raise F.FactorError("wrong", "the signature does not verify")
        return sig                    # like the real factors: the verified signature

    def verify(self, record, challenge, signature):
        return CR.es256_verify(base64.b64decode(record["public"]), challenge, signature)

    def seal(self, record, plaintext):
        return bytes(b ^ self.pad[i % 32] for i, b in enumerate(plaintext))

    def unseal(self, record, sealed, ctx):
        self.prove(record, os.urandom(32), ctx)
        return bytes(b ^ self.pad[i % 32] for i, b in enumerate(sealed))


class AuthorityCase(unittest.TestCase):
    """An authority in this process, serving a socket in a temp home; THIS test process is
    the fake `claude`, so its children are its session's processes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gtu-")
        self.home = os.path.join(self.tmp, "unlock")
        os.makedirs(self.home, mode=0o700)
        self.platform = SoftPlatform()
        self.factors = {"totp": F.TotpFactor(), "touchid": self.platform,
                        "hello": SoftPlatform("hello"), "recovery": F.RecoveryFactor()}
        self.factors["hello"].available = lambda: (False, "test: no hello")
        self.claude_pids = {os.getpid()}
        self.consumer_pids = {os.getpid()}
        self.screen = [False]
        self.clock_skew = [0.0]
        self.admin_paths = [os.path.join(self.tmp, "no-admin", "unlock-policy.json")]
        self.children = []
        # The authority runs IN this test process, not as gt_unlockd.py, so gt's client would
        # refuse it as a server it cannot verify (review F1). This names the test authority;
        # gt_unlock_client honours it only while the unlock home is relocated (GT_UNLOCK_HOME,
        # itself honoured only while the real home's unlock is off). Children inherit it.
        self._prev_test_pid = os.environ.get("GT_UNLOCK_TEST_SERVER_PID")
        os.environ["GT_UNLOCK_TEST_SERVER_PID"] = str(os.getpid())
        self.start_authority()

    def start_authority(self):
        self.stop = threading.Event()
        ready = threading.Event()
        self.auth = D.Authority(
            self.home, factors=self.factors, admin_paths=self.admin_paths,
            is_claude=lambda info: info["pid"] in self.claude_pids,
            screen_locked=lambda: self.screen[0],
            clock=lambda: (time.time() + self.clock_skew[0], time.monotonic()),
            # In-process gt-lotr engines in tests run in THIS process: it stands in for lotrd
            # as the one recognised secret consumer. The real check is tested in test_unlock_redteam.
            consumer_ok=lambda peer: peer.get("pid") in self.consumer_pids,
            # The shims here are tests/_unlock_child.py and the repo's lotr_mcp.py, not the
            # installed gt-lotr: the real seat check (is_lotr_shim) is tested on its own.
            shim_ok=lambda peer: True)
        self.srv = D.Server(self.auth)
        self.auth.stop = self.stop
        self.addr = gt_ipc.default_address(self.home, "unlockd")
        self.thread = threading.Thread(
            target=gt_ipc.serve, args=(self.addr, self.srv.handle),
            kwargs={"stop_event": self.stop, "on_ready": lambda a: ready.set()}, daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(5), "the authority did not start")

    def restart_authority(self):
        self.stop.set()
        self.thread.join(5)
        self.start_authority()

    def tearDown(self):
        for c in self.children:
            try:
                c.stdin.write(json.dumps({"exit": True}) + "\n")
                c.stdin.flush()
                c.wait(5)
            except Exception:                    # noqa: BLE001
                c.kill()
                c.wait(5)
            for stream in (c.stdin, c.stdout):
                try:
                    stream.close()
                except Exception:                # noqa: BLE001
                    pass
        self.stop.set()
        self.thread.join(5)
        if self._prev_test_pid is None:
            os.environ.pop("GT_UNLOCK_TEST_SERVER_PID", None)
        else:
            os.environ["GT_UNLOCK_TEST_SERVER_PID"] = self._prev_test_pid
        rmtree(self.tmp)

    # -- processes ------------------------------------------------------------------
    def child(self):
        p = subprocess.Popen([PYTHON, CHILD, str(SCRIPTS), self.addr], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
        self.children.append(p)
        return p

    def send(self, proc, cmd, timeout=30):
        proc.stdin.write(json.dumps(cmd) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise AssertionError("the child process died")
        return json.loads(line)

    def call(self, proc, method, params=None, answers=None):
        return self.send(proc, {"call": method, "params": params or {}, "answers": answers or []})

    # -- enrolment shortcuts ---------------------------------------------------------
    def enrol_totp(self):
        """TOTP seed + enrolment record written directly (enrolment itself is tested
        separately); returns the key."""
        key = T.new_secret()
        F.write_private(F.TotpFactor.seed_path(self.home), (T.b32(key) + "\n").encode())
        enr = self.auth.enrolment()
        enr.setdefault("factors", {})["totp"] = {"account": "test"}
        self.auth.save_enrolment(enr)
        self.totp_key = key
        return key

    def enrol_platform(self):
        enr = self.auth.enrolment()
        enr.setdefault("factors", {})["touchid"] = self.platform.enroll(None)
        self.auth.save_enrolment(enr)

    def code(self, offset=0):
        return T.code_at(self.totp_key, time.time() + offset)

    def set_policy(self, **over):
        import gt_unlock_policy as P
        pol = {"schema": 1, "enabled": True,
               "factors": {"required": 2, "require_one_of": ["touchid"]}}
        for k, v in over.items():
            pol[k] = v
        data = (json.dumps(pol, indent=2, sort_keys=True) + "\n").encode()
        F.write_private(os.path.join(self.home, "policy.json"), data)
        st = self.auth.state()
        h = D._sha(data)
        st["policy_approved"] = h
        st.pop("policy_approval", None)
        if "touchid" in (self.auth.enrolment().get("factors") or {}):
            # What policy_set records after a step-up: the platform factor's signature over
            # the policy hash (review F3). Here the software key signs as Touch ID would.
            nonce = os.urandom(32)
            ch = self.auth.approval_challenge(h, nonce, "test", "touchid")
            st["policy_approval"] = {"hash": h, "factor": "touchid", "requester": "test",
                                     "nonce": base64.b64encode(nonce).decode(),
                                     "sig": base64.b64encode(self.platform.key.sign_der(ch))
                                     .decode()}
        st["unlock_on"] = bool(pol.get("enabled"))
        self.auth.save_state(st)
        P.set_marker(self.home, bool(pol.get("enabled")))
        if not pol.get("enabled"):
            self.auth._off_by_policy = True
        self.auth.reload()
        del P

    def standard(self):
        self.enrol_totp()
        self.enrol_platform()
        self.set_policy()
