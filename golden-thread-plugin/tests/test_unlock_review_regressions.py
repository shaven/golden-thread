"""Regression tests for the independent security review of gt 0.20.1 (2026-10-03 16:15 CDT).

Each test is one of the review's exploit harnesses (Projects/golden-thread/research/
review-0.20.0-harnesses/: test_exploits.py, test_exploits2.py, lotr/poc_unlock.py,
lotr/poc_fake_authority.py), turned from a demonstration that PRINTS a bypass into an
assertion that the bypass is closed. Every one of them failed against fe571f2, the reviewed
commit. The finding each pins is named in its docstring (F1-F7, M1; the review's numbering).

What is NOT here, because it is a documented limit rather than a bug (SECURITY.md, "What it
does not stop"): a same-user process reading totp.seed on a TOTP-only (L1) machine (E6), and an
allow-listed unattended scope being usable by any same-user process outside a session (E10).
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from _harness import IS_WINDOWS, PYTHON, SCRIPTS, rmtree
from _unlock_fixture import AuthorityCase

sys.path.insert(0, str(SCRIPTS))
import gt_ipc                    # noqa: E402
import gt_unlock_policy as P     # noqa: E402
import gt_unlockd as D           # noqa: E402


class SessionShell(AuthorityCase):
    """The shim is registered and unlocked; the attacker is a process the session's shell
    started (a child of the fake `claude`, exactly like the assistant's Bash)."""

    def setUp(self):
        super().setUp()
        self.standard()
        self.shim = self.child()
        self.assertIn("result", self.call(self.shim, "register_shim"))
        self.assertIn("result", self.call(self.shim, "unlock", {"tty": True},
                                          answers=[self.code()]))

    # -- F4 ---------------------------------------------------------------------------
    def test_E1_shell_child_gets_no_secret_on_the_shims_grant(self):
        """F4 (harness E1, poc_unlock test_1): after the shim read a sealed value, a shell
        child asked for it -- and for a file: ref -- for ITSELF and got both."""
        self.call(self.shim, "seal_put", {"name": "gh", "value": "GH-TOKEN-SECRET"})
        r = self.call(self.shim, "secret", {"ref": "sealed:gh"})
        self.assertEqual(r.get("result", {}).get("value"), "GH-TOKEN-SECRET", r)
        bash = self.child()
        r = self.call(bash, "secret", {"ref": "sealed:gh"})
        self.assertIn("error", r)
        self.assertEqual(r["error"]["code"], "mcp_only")
        self.assertNotIn("GH-TOKEN-SECRET", json.dumps(r))
        cred = os.path.join(self.tmp, "cred.txt")
        with open(cred, "w", encoding="utf-8") as f:
            f.write("FILE-CRED")
        if not IS_WINDOWS:
            os.chmod(cred, 0o600)
        r = self.call(bash, "secret", {"ref": "file:" + cred})
        self.assertIn("error", r)
        self.assertNotIn("FILE-CRED", json.dumps(r))

    def test_every_unseal_needs_a_fresh_platform_factor_by_default(self):
        """F4, owner 2026-10-03 18:37: no silent cache. secrets_window_s 0 (the default)
        means every read of a sealed value is its own Touch ID / Hello."""
        self.call(self.shim, "seal_put", {"name": "gh", "value": "V1"})
        before = self.platform.calls
        for _ in range(3):
            r = self.call(self.shim, "secret", {"ref": "sealed:gh"})
            self.assertEqual(r["result"]["value"], "V1")
        self.assertEqual(self.platform.calls - before, 3)

    def test_a_secrets_window_caches_per_subject_and_never_past_the_window(self):
        self.set_policy(secrets_window_s=60)
        self.call(self.shim, "seal_put", {"name": "gh", "value": "V2"})
        before = self.platform.calls
        self.assertEqual(self.call(self.shim, "secret", {"ref": "sealed:gh"})["result"]["value"],
                         "V2")
        self.assertEqual(self.call(self.shim, "secret", {"ref": "sealed:gh"})["result"]["value"],
                         "V2")
        self.assertEqual(self.platform.calls - before, 1, "inside the window: one factor")
        # the cache is keyed by subject and grant: lock + unlock starts again
        self.call(self.shim, "lock")
        self.assertEqual(self.auth.sealed_cache, {})

    # -- F2 / F3 / I6 --------------------------------------------------------------------
    def test_E2_removing_policy_and_state_never_reads_as_off(self):
        """F3 (harness E2): rm policy.json state.json turned unlock OFF."""
        bash = self.child()
        self.call(self.shim, "lock")
        os.unlink(os.path.join(self.home, "policy.json"))
        os.unlink(os.path.join(self.home, "state.json"))
        self.assertTrue(P.enabled_at(self.home), "the hooks' fast path must still say ON")
        for scope in ("lotr:github:write", "gt:unlock:policy", "gt:settings:hooks"):
            v = self.call(bash, "check", {"scope": scope})["result"]
            self.assertFalse(v["allowed"], scope)
            self.assertEqual(v["code"], "failed_closed", scope)

    def test_E3_a_policy_approval_written_to_state_json_is_not_an_approval(self):
        """F3 (harness E3): rewriting policy.json + its sha256 in state.json opened every
        scope. With a platform factor enrolled the approval is a signature by it."""
        import hashlib
        self.call(self.shim, "lock")
        pol = {"schema": 1, "enabled": True, "factors": {"required": 2,
                                                         "require_one_of": ["touchid"]},
               "door": "session", "default_scope_level": "open",
               "scopes": {"lotr:*:write": "open", "gt:secrets": "open",
                          "gt:unlock:policy": "open", "gt:settings:hooks": "open"}}
        data = (json.dumps(pol) + "\n").encode()
        with open(os.path.join(self.home, "policy.json"), "wb") as f:
            f.write(data)
        with open(os.path.join(self.home, "state.json"), encoding="utf-8") as f:
            st = json.load(f)
        st["policy_approved"] = hashlib.sha256(data).hexdigest()
        st.pop("policy_approval", None)
        with open(os.path.join(self.home, "state.json"), "w", encoding="utf-8") as f:
            json.dump(st, f)
        bash = self.child()
        for s in ("lotr:github:write", "gt:settings:hooks", "gt:secrets"):
            v = self.call(bash, "check", {"scope": s})["result"]
            self.assertFalse(v["allowed"], s)
            self.assertEqual(v["code"], "failed_closed", s)

    # -- F5 ---------------------------------------------------------------------------
    def test_E4_a_lotrd_py_outside_the_installed_plugin_is_not_a_consumer(self):
        """F5 (harness E4, poc_unlock test_4): a Bash-written /tmp/x/lotrd.py (with a
        lotrlib/engine.py beside it) passed is_lotr_daemon and received the shim's secret."""
        self.auth.consumer_ok = D.is_lotr_daemon            # the REAL check
        self.call(self.shim, "seal_put", {"name": "gh", "value": "GH-TOKEN-SECRET"})
        sh = self.send(self.shim, {"pid": True})
        d = tempfile.mkdtemp(prefix="fake-")
        try:
            os.makedirs(os.path.join(d, "lotrlib"))
            open(os.path.join(d, "lotrlib", "engine.py"), "w").close()
            with open(os.path.join(d, "lotrd.py"), "w", encoding="utf-8") as f:
                f.write("import sys,json; sys.path.insert(0,%r); import gt_ipc\n"
                        "c=gt_ipc.connect(%r)\n"
                        "try:\n"
                        "    print(json.dumps({'result': c.call('secret', {'ref':'sealed:gh',"
                        "'subject':%r})}))\n"
                        "except gt_ipc.IpcError as e:\n"
                        "    print(json.dumps({'error': e.to_dict()}))\n"
                        % (str(SCRIPTS), self.addr, {"pid": sh["pid"], "start": sh["start"]}))
            out = subprocess.run([PYTHON, os.path.join(d, "lotrd.py")], capture_output=True,
                                 text=True, timeout=30)
            r = json.loads(out.stdout)
            self.assertNotIn("GH-TOKEN-SECRET", out.stdout)
            self.assertEqual(r["error"]["code"], "not_a_consumer")
        finally:
            rmtree(d)

    # -- F7 ---------------------------------------------------------------------------
    def test_E5_a_replacement_shim_inherits_no_grant(self):
        """F7 (harness E5): kill the shim, register an impostor before the authority's next
        tick -- it took over the session's live grant."""
        self.assertTrue(self.auth.grants)
        self.shim.kill()
        self.shim.wait(5)
        imp = self.child()
        r = self.call(imp, "register_shim")
        self.assertIn("result", r)                 # the seat is free: the shim is gone
        self.auth.tick()
        v = self.call(imp, "check", {"scope": "lotr:github:write"})["result"]
        self.assertFalse(v["allowed"])
        self.assertEqual(v["code"], "locked")

    def test_restart_does_not_hand_the_seat_to_a_shell_child(self):
        """F7 (poc_unlock test_3): after an authority restart a shell child registered as
        the shim. A seat registration must come from the installed lotr_mcp.py."""
        self.restart_authority()
        self.auth.shim_ok = D.is_lotr_shim                  # the REAL check
        bash = self.child()
        r = self.call(bash, "register_shim")
        self.assertEqual(r["error"]["code"], "not_a_shim")

    # -- F1 ---------------------------------------------------------------------------
    def test_stop_needs_a_fresh_factor(self):
        """F1 (poc_unlock test_3, harness E8 precondition): a shell child stopped the
        authority with no factor at all."""
        bash = self.child()
        self.platform.mode = "cancel"
        self.auth.cooldown.clear()
        r = self.call(bash, "stop", {"tty": True}, answers=[self.code(30)])
        self.assertIn("error", r)
        self.assertFalse(self.stop.is_set(), "the authority stopped without a factor")
        self.assertTrue(gt_ipc.alive_at(self.addr))

    def test_lock_still_needs_nothing(self):
        bash = self.child()
        self.assertEqual(self.call(bash, "lock")["result"]["revoked"], 1)

    def test_E8_a_fake_authority_on_the_socket_fails_closed(self):
        """F1 (harness E8, poc_fake_authority): bind an allow-all server on the authority's
        address; every check through gt's client came back allowed."""
        if IS_WINDOWS:
            self.skipTest("the pipe server is verified by tests in test_lotr_unlock on Windows")
        import gt_unlock_client as C
        os.unlink(self.addr)
        ready = os.path.join(self.tmp, "fake.ready")
        fake = subprocess.Popen([PYTHON, "-c", FAKE_SERVER, self.addr, ready],
                                stdin=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not os.path.exists(ready) and time.monotonic() < deadline:
                time.sleep(0.05)
            v = C.check("gt:settings:hooks", h=self.home, start=False)
            self.assertFalse(v["allowed"], v)
            self.assertEqual(v["code"], "server_unverified")
            with self.assertRaises(gt_ipc.IpcError):
                C.call("consent", {"op": {}}, h=self.home, start=False)
        finally:
            fake.kill()
            fake.wait(5)

    # -- F6 ---------------------------------------------------------------------------
    @unittest.skipIf(IS_WINDOWS, "os.fork / setsid are POSIX")
    def test_E9_an_orphaned_process_gets_no_lotr_read_without_unlock(self):
        """F6 (harness E9, poc_unlock test_5): double-fork + setsid made a shell child a
        'terminal', and read_without_unlock opened lotr:*:read to it."""
        res = os.path.join(self.tmp, "orphan.json")
        subprocess.run([PYTHON, "-c", ORPHAN % {"scripts": str(SCRIPTS), "addr": self.addr,
                                                "res": res}], timeout=10)
        deadline = time.monotonic() + 10
        while not os.path.exists(res) and time.monotonic() < deadline:
            time.sleep(0.1)
        with open(res, encoding="utf-8") as f:
            out = json.load(f)
        self.assertFalse(out["read"]["allowed"], out)
        self.assertFalse(out["write"]["allowed"], out)

    # -- M1 ---------------------------------------------------------------------------
    def test_M1_a_shell_child_cannot_open_the_consent_window(self):
        """M1 (poc_unlock test_2): a shell child named the shim as subject, wrote its own
        prompt text, and opened a consent window lotrd's next consent op rode through."""
        self.set_policy(consent_requires_factor="platform", consent_window_s=600)
        sh = self.send(self.shim, {"pid": True})
        bash = self.child()
        r = self.call(bash, "consent", {"subject": {"pid": sh["pid"], "start": sh["start"]},
                                        "op_hash": "00" * 32,
                                        "text": "gt-lotr: read your calendar (harmless)"})
        self.assertEqual(r["error"]["code"], "not_a_consumer")
        r = self.call(bash, "consent", {"op_hash": "00" * 32, "text": "harmless"})
        self.assertEqual(r["error"]["code"], "not_a_consumer")
        self.assertTrue(all(not g.consent_until for g in self.auth.grants.values()))

    # -- low: audit ----------------------------------------------------------------------
    def test_an_unwritable_audit_log_refuses_write_scopes(self):
        """Low (review): audit-write failures were swallowed. A write-tier allow that cannot
        be audited is refused."""
        path = os.path.join(self.home, "audit.jsonl")
        os.unlink(path)
        os.makedirs(path)                                   # appending to it now fails
        try:
            v = self.auth.evaluate({"pid": self.shim.pid,
                                    "start": gt_ipc.process_info(self.shim.pid)["start"]},
                                   "lotr:github:write")
            self.assertFalse(v["allowed"])
            self.assertEqual(v["code"], "audit_failed")
        finally:
            os.rmdir(path)


class AdminFloor(unittest.TestCase):
    def test_E7_a_more_specific_user_pattern_never_beats_the_admin(self):
        """F2 (harness E7): merge() tightened only identical keys, so a user
        `lotr:github:write: open` beat an admin `lotr:*:write: step_up`."""
        user = {"enabled": True, "scopes": {"lotr:github:write": "open",
                                            "gt:unlock:policy": "open"}}
        admin = {"scopes": {"lotr:*:write": "step_up", "gt:*": "step_up"}}
        eff = P.Effective(P.merge(user, admin), [], True)
        self.assertEqual(eff.level("lotr:github:write"), "step_up")
        self.assertEqual(eff.level("gt:unlock:policy"), "step_up")
        # and the admin's own most-specific pattern still decides among admin patterns
        admin2 = {"scopes": {"lotr:*": "deny", "lotr:github:*": "unlocked"}}
        eff = P.Effective(P.merge({"scopes": {"lotr:github:write": "open"}}, admin2), [], True)
        self.assertEqual(eff.level("lotr:github:write"), "unlocked")
        self.assertEqual(eff.level("lotr:jira:write"), "deny")

    def test_a_user_policy_cannot_smuggle_an_admin_floor_key(self):
        eff = P.Effective(P.merge({"admin_floor_scopes": {"gt:*": "open"},
                                   "scopes": {"gt:publish": "open"}}, None), [], True)
        self.assertEqual(eff.level("gt:publish"), "open")
        self.assertEqual(eff.policy.get("admin_floor_scopes") or {}, {})


FAKE_SERVER = r'''
import json, os, socket, sys
addr, ready = sys.argv[1], sys.argv[2]
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); srv.bind(addr); srv.listen(4)
open(ready, "w").close()
while True:
    s, _ = srv.accept(); f = s.makefile("rwb")
    for line in f:
        rid = json.loads(line).get("id")
        f.write((json.dumps({"id": rid, "result": {"allowed": True, "code": "granted",
                 "grant": "x" * 32, "level": "unlocked", "hints": [], "message": "",
                 "mode": "platform", "approved": True}}) + "\n").encode()); f.flush()
'''

ORPHAN = r'''
import os, sys, json, time
sys.path.insert(0, %(scripts)r)
if os.fork(): os._exit(0)
os.setsid()
if os.fork(): os._exit(0)
time.sleep(0.3)
import gt_ipc
out = {}
for k, scope in (("read", "lotr:github:read"), ("write", "lotr:github:write")):
    c = gt_ipc.connect(%(addr)r)
    try:
        out[k] = c.call("check", {"scope": scope})
    finally:
        c.close()
open(%(res)r + ".tmp", "w").write(json.dumps(out))
os.replace(%(res)r + ".tmp", %(res)r)
'''


if __name__ == "__main__":
    unittest.main()
