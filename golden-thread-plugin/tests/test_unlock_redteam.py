"""Red team: every bypass route in design-unlock.md §0 and §7, attempted, and each must fail
(0.20.0, owner requirement 2026-10-03 11:07).

Each test is one attack, named for what the attacker tries. Routes that live in another module
are listed at the bottom with where they are pinned, so this file is the one catalogue.
The attacker here is "a process running as you, started from the session's shell" -- the
assistant's Bash -- unless a test says otherwise. Same-user MALWARE after you unlock is out of
scope by design (SECURITY.md); those rows are pinned as documented limits, not as passes.
"""
import json
import os
import socket
import subprocess
import sys
import time
import unittest

from _harness import IS_WINDOWS, PYTHON, SCRIPTS, Sandbox
from _unlock_fixture import AuthorityCase

sys.path.insert(0, str(SCRIPTS))
import gt_ipc                  # noqa: E402
import gt_unlock_factors as F  # noqa: E402


class FromTheSessionShell(AuthorityCase):

    def setUp(self):
        super().setUp()
        self.standard()
        self.shim = self.child()
        self.assertIn("result", self.call(self.shim, "register_shim"))
        self.assertIn("result", self.call(self.shim, "unlock", {"tty": True},
                                          answers=[self.code()]))

    def test_raw_socket_client_under_mcp_only_gets_no_lotr(self):
        """No client library, just bytes on the socket, from a shell child: refused."""
        code = ("import json, sys; sys.path.insert(0, %r); import gt_ipc; "
                "c = gt_ipc.connect(%r); print(json.dumps(c.call('check', "
                "{'scope': 'lotr:github:write'})))" % (str(SCRIPTS), self.addr))
        out = subprocess.run([PYTHON, "-c", code], capture_output=True, text=True, timeout=30)
        v = json.loads(out.stdout)
        self.assertEqual((v["allowed"], v["code"]), (False, "mcp_only"))

    def test_naming_the_shim_as_subject_grants_the_asker_nothing(self):
        """A shell child asks 'is the SHIM allowed?' -- the answer is about the shim; nothing
        the asker can use comes back (no credential, no token; the grant id is an audit
        handle that no method accepts)."""
        sh = self.send(self.shim, {"pid": True})
        bash = self.child()
        v = self.call(bash, "check", {"scope": "lotr:github:write",
                                      "subject": {"pid": sh["pid"], "start": sh["start"]}})
        self.assertTrue(v["result"]["allowed"])
        mine = self.call(bash, "check", {"scope": "lotr:github:write",
                                         "grant": v["result"]["grant"]})["result"]
        self.assertEqual(mine["code"], "mcp_only")
        r = self.call(bash, "secret", {"ref": "sealed:none", "request": False,
                                       "grant": v["result"]["grant"]})
        self.assertIn("error", r)

    def test_a_shell_child_naming_the_shim_gets_no_secret_value(self):
        """The shim's session holds a grant; a shell child asks for a sealed value ON BEHALF
        of the shim. Only gt-lotr's daemon may do that -- the value never reaches it."""
        self.call(self.shim, "seal_put", {"name": "gh3", "value": "SHOULD-NOT-LEAK"})
        sh = self.send(self.shim, {"pid": True})
        bash = self.child()
        r = self.call(bash, "secret", {"ref": "sealed:gh3",
                                       "subject": {"pid": sh["pid"], "start": sh["start"]}})
        self.assertEqual(r["error"]["code"], "not_a_consumer")
        self.assertNotIn("SHOULD-NOT-LEAK", json.dumps(r))

    def test_only_a_real_lotrd_main_script_is_a_consumer(self):
        import gt_unlockd as D
        info = gt_ipc.process_info(os.getpid())
        self.assertFalse(D.is_lotr_daemon({"pid": os.getpid(), "start": info["start"]}))
        for argv in (["-c", "print(1)", "lotrd.py"], ["-", "lotrd.py"], ["-m", "lotrd"]):
            p = subprocess.Popen([PYTHON] + argv, stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(0.3)
                pi = gt_ipc.process_info(p.pid)
                if pi:
                    self.assertFalse(D.is_lotr_daemon({"pid": p.pid, "start": pi["start"]}),
                                     argv)
            finally:
                p.kill()
                p.wait(5)
                p.stdin.close()

    def test_only_the_installed_lotrd_is_a_consumer(self):
        """Review F5 (2026-10-03): this test used to assert that a temp-dir lotrd.py with a
        lotrlib/engine.py beside it PASSED -- a file-name check any shell could satisfy. Now a
        consumer runs <installPath>/scripts/lotrd.py of gt-lotr as installed_plugins.json
        records it, by realpath; the same file anywhere else is refused."""
        import gt_unlockd as D
        import shutil
        import tempfile
        d = tempfile.mkdtemp(prefix="gtlotrd-")
        try:
            plugin = os.path.join(d, "cache", "gt-lotr", "0.3.0")
            os.makedirs(os.path.join(plugin, "scripts", "lotrlib"))
            fake = os.path.join(d, "fake")
            os.makedirs(os.path.join(fake, "lotrlib"))
            for root in (os.path.join(plugin, "scripts"), fake):
                open(os.path.join(root, "lotrlib", "engine.py"), "w").close()
                with open(os.path.join(root, "lotrd.py"), "w", encoding="utf-8",
                          newline="\n") as f:
                    f.write("import time\ntime.sleep(30)\n")
            plugins = os.path.join(d, "installed_plugins.json")
            with open(plugins, "w", encoding="utf-8") as f:
                json.dump({"version": 2, "plugins": {"gt-lotr@golden-thread-plugin": [
                    {"scope": "user", "installPath": plugin, "version": "0.3.0"}]}}, f)
            verdicts = {}
            for name, script in (("installed", os.path.join(plugin, "scripts", "lotrd.py")),
                                 ("look-alike", os.path.join(fake, "lotrd.py"))):
                p = subprocess.Popen([PYTHON, "-I", "-B", script])   # installed AND isolated (L1)
                try:
                    deadline = time.monotonic() + 5
                    ok = False
                    while time.monotonic() < deadline and not ok:
                        pi = gt_ipc.process_info(p.pid)
                        ok = bool(pi) and D.is_lotr_daemon({"pid": p.pid, "start": pi["start"]},
                                                           plugins_file=plugins)
                        time.sleep(0.1)
                    verdicts[name] = ok
                finally:
                    p.kill()
                    p.wait(5)
            self.assertEqual(verdicts, {"installed": True, "look-alike": False})
        finally:
            shutil.rmtree(d, True)

    def test_forged_subject_is_refused(self):
        bash = self.child()
        sh = self.send(self.shim, {"pid": True})
        for subj in ({"pid": sh["pid"], "start": "0"}, {"pid": 999999, "start": sh["start"]}):
            v = self.call(bash, "check", {"scope": "lotr:github:write", "subject": subj})
            self.assertEqual(v["result"]["code"], "subject_gone")

    def test_a_client_cannot_assert_that_factors_passed(self):
        """I2: there is no 'approved' parameter. Sending one changes nothing: the authority
        still runs the factors itself -- and here the platform factor refuses."""
        bash = self.child()
        self.call(bash, "lock")
        self.auth.cooldown.clear()
        self.platform.mode = "cancel"
        r = self.call(bash, "unlock", {"approved": True, "factors": ["touchid", "totp"],
                                       "grant": "x" * 32, "tty": True}, answers=[self.code(30)])
        self.assertIn("error", r)
        self.assertEqual(self.auth.grants, {})

    def test_job_claim_from_inside_the_session_is_not_a_job(self):
        """An agent passing job=<an allow-listed job> is still the session's shell child."""
        self.set_policy(unattended={"allowed": [{"job": "sync", "scope": "lotr:github:read"}]})
        bash = self.child()
        v = self.call(bash, "check", {"scope": "lotr:github:read", "job": "sync"})["result"]
        self.assertEqual(v["code"], "mcp_only")

    def test_impostor_shim_cannot_take_the_seat(self):
        bash = self.child()
        self.assertEqual(self.call(bash, "register_shim")["error"]["code"], "seat_taken")

    def test_grant_use_after_shim_exit(self):
        self.shim.kill()
        self.shim.wait(5)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.auth.grants:
            self.auth.tick()
            time.sleep(0.1)
        self.assertEqual(self.auth.grants, {})

    def test_policy_downgrade_by_editing_the_user_file(self):
        path = os.path.join(self.home, "policy.json")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"enabled": False}, f)            # "switch it off" behind its back
        bash = self.child()
        v = self.call(bash, "check", {"scope": "gt:publish"})["result"]
        self.assertFalse(v["allowed"])
        self.assertEqual(v["code"], "failed_closed")

    def test_policy_downgrade_through_the_api_needs_fresh_factors(self):
        bash = self.child()
        self.platform.mode = "cancel"
        r = self.call(bash, "policy_set", {"policy": {"enabled": False}, "tty": True},
                      answers=[self.code(30)])
        self.assertIn("error", r)

    def test_enrolling_an_attacker_factor_needs_fresh_factors(self):
        bash = self.child()
        self.platform.mode = "cancel"
        r = self.call(bash, "enroll", {"factor": "totp", "phase": "begin", "tty": True})
        self.assertIn("error", r)
        self.assertFalse(os.path.exists(os.path.join(self.home, "totp.pending")))

    def test_sealed_blob_on_disk_is_ciphertext_and_locked_reads_refused(self):
        self.call(self.shim, "seal_put", {"name": "gh", "value": "tok-xyz"})
        with open(os.path.join(self.home, "sealed", "gh.blob"), encoding="utf-8") as f:
            self.assertNotIn("tok-xyz", f.read())
        self.call(self.shim, "lock")
        r = self.call(self.shim, "secret", {"ref": "sealed:gh", "request": False})
        self.assertEqual(r["error"]["code"], "locked")
        # and a shell child never gets as far as "locked": the broker is the shim's (F4)
        bash = self.child()
        r = self.call(bash, "secret", {"ref": "sealed:gh", "request": False})
        self.assertEqual(r["error"]["code"], "mcp_only")

    def test_prompt_flood_is_throttled(self):
        bash = self.child()
        self.call(bash, "lock")
        self.auth.cooldown.clear()
        self.platform.mode = "cancel"
        first = self.call(bash, "unlock", {"tty": True}, answers=[self.code(30)])
        second = self.call(bash, "unlock", {"tty": True}, answers=[self.code(30)])
        self.assertEqual(first["error"]["code"], "factor_cancelled")
        self.assertEqual(second["error"]["code"], "cooldown")

    def test_the_audit_log_never_holds_codes_or_secret_values(self):
        self.call(self.shim, "seal_put", {"name": "gh2", "value": "VALUE-NEVER-LOGGED"})
        self.call(self.shim, "secret", {"ref": "sealed:gh2"})
        with open(os.path.join(self.home, "audit.jsonl"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("VALUE-NEVER-LOGGED", text)
        self.assertIn('"event":"secret_resolve"', text)


class RelocatingTheHome(Sandbox):
    """GT_UNLOCK_HOME pointed at an empty directory must not turn a gate in that process into
    'disabled' while the real home's unlock is on."""

    def test_env_relocation_is_ignored_when_the_real_unlock_is_on(self):
        real = self.home / ".claude" / "golden-thread" / "unlock"
        real.mkdir(parents=True)
        (real / "policy.json").write_text('{"enabled": true}\n', encoding="utf-8")
        empty = self.tmp / "empty-unlock"
        empty.mkdir()
        code = ("import sys, json; sys.path.insert(0, %r); import gt_unlock_client as C; "
                "print(json.dumps(C.check('gt:publish', start=False)))" % str(SCRIPTS))
        r = self.run_cmd([PYTHON, "-c", code], env={"GT_UNLOCK_HOME": str(empty)})
        v = json.loads(r.stdout)
        self.assertFalse(v["allowed"], r.stdout)
        self.assertNotEqual(v["code"], "disabled")


class EditingThePolicyFileOff(Sandbox):
    """policy.json edited to "enabled": false behind the authority's back does not switch the
    hooks' fast path off: the authority's unlock_on marker keeps it ON (and the authority then
    fails closed on the unapproved edit -- FromTheSessionShell.test_policy_downgrade...)."""

    def test_fast_path_stays_on(self):
        real = self.home / ".claude" / "golden-thread" / "unlock"
        real.mkdir(parents=True)
        (real / "policy.json").write_text('{"enabled": false}\n', encoding="utf-8")
        (real / "state.json").write_text('{"unlock_on": true}\n', encoding="utf-8")
        code = ("import sys, json; sys.path.insert(0, %r); import gt_unlock_client as C; "
                "print(json.dumps(C.check('gt:publish', start=False)))" % str(SCRIPTS))
        v = json.loads(self.run_cmd([PYTHON, "-c", code]).stdout)
        self.assertFalse(v["allowed"])
        self.assertNotEqual(v["code"], "disabled")


class GatedHookWithTheAuthorityDown(Sandbox):
    """Hooks fail CLOSED for gated scopes (A6) -- and only when unlock is on."""

    def payload(self, target):
        return json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(target)},
                           "cwd": str(self.tmp)})

    def hook(self, target):
        return self.py(SCRIPTS.parent / "hooks" / "guard_protected_paths.py",
                       str(SCRIPTS), input=self.payload(target))

    def test_settings_edit_denied_when_on_and_authority_down(self):
        real = self.home / ".claude" / "golden-thread" / "unlock"
        real.mkdir(parents=True)
        (real / "policy.json").write_text('{"enabled": true}\n', encoding="utf-8")
        r = self.hook(self.home / ".claude" / "settings.json")
        self.assertOk(r)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny", r.stdout)
        self.assertIn("gt unlock", out["permissionDecisionReason"])

    def test_turning_protected_paths_off_first_does_not_help(self):
        real = self.home / ".claude" / "golden-thread" / "unlock"
        real.mkdir(parents=True)
        (real / "policy.json").write_text('{"enabled": true}\n', encoding="utf-8")
        (self.home / ".claude" / "vault-config.json").write_text(
            json.dumps({"vault_path": str(self.tmp), "protected_paths": "off"}),
            encoding="utf-8")
        r = self.hook(self.home / ".claude" / "golden-thread" / "hooks" / "x.py")
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"],
                         "deny")

    def test_gt_settings_cannot_switch_a_guard_off_when_on(self):
        real = self.home / ".claude" / "golden-thread" / "unlock"
        real.mkdir(parents=True)
        (real / "policy.json").write_text('{"enabled": true}\n', encoding="utf-8")
        (self.home / ".claude" / "vault-config.json").write_text(
            json.dumps({"vault_path": str(self.tmp)}), encoding="utf-8")
        r = self.py(SCRIPTS / "gt_settings.py", "set", "protected_paths", "off",
                    env={"GT_UNLOCK_NO_START": "1"}, timeout=60)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("refused", r.stdout)
        cfg = json.loads((self.home / ".claude" / "vault-config.json").read_text())
        self.assertNotIn("protected_paths", cfg)

    def test_off_means_unchanged(self):
        r = self.hook(self.home / ".claude" / "settings.json")
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "ask")


# Routes pinned elsewhere (the catalogue continues):
#   pipe squatting, foreign SID, server of another user ... test_gt_ipc
#   TOTP replay, lockout across restart .................. test_unlock_core.TotpVerify / Grants
#   forged / stale Touch ID signature .................... test_unlock_core.Signatures,
#                                                          test_unlock_touchid (real SE)
#   K above usable factors, unreadable/foreign admin file  test_unlock_core.FailClosed /
#                                                          AdminFloorOwnership
#   lotr CLI from Bash, tiers, allow-lists, audit grant ids test_lotr_unlock
#   ID token forgery (alg none, wrong aud/iss/nonce...) ... test_unlock_sso
#   RS256 e=3 forgery, forged "ok" from Hello ............ test_unlock_hello

if __name__ == "__main__":
    unittest.main()
