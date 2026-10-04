"""gt-lotr 0.3.0 as a consumer of gt core's unlock authority (gt 0.20.1).

A real authority (tests/_unlock_fixture.AuthorityCase) serves a socket / pipe in a temp home;
this test process is the fake `claude`. A real lotrd front door (lotrlib.server) runs in this
process on its own temp home with a fake downstream, and the callers are REAL child
processes -- the MCP shim (lotr_mcp.py) and the lotr CLI -- because the authority and lotrd
identify callers by their kernel identity and ancestry, never by anything they say.

Pinned here:
  * unlock off: nothing changes (no authority traffic; audit lines carry grant null);
  * locked: a gated read is refused; the shim asks the authority to unlock FOR ITSELF and
    retries once; the audit line carries the grant id the authority issued;
  * door mcp_only: a process the session's shell started (the lotr CLI here) is refused even
    while the shim holds a grant -- calls and the catalog alike;
  * consent with consent_requires_factor "platform": the authority's platform signature is
    the confirmation (window honoured; a failed factor denies; never falls back to the dialog);
    with "none" the 0.2.0 dialog path runs; local.confirm "biometric" refuses without platform;
  * sealed: secrets resolve only through the authority, only under a grant, audited with it;
  * audit completeness: every op executed downstream, and every refusal, has a line with tool,
    tier and grant;
  * tier rules are untouched (read can never carry a write; an unknown verb is a write);
  * hub enrol / revoke need gt:hub:enroll with step-up;
  * gt's client unloadable while unlock is on: refused (unlock_unavailable), never served.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from _harness import IS_WINDOWS, PYTHON, REPO, SCRIPTS, latest_version_dir
from _unlock_fixture import AuthorityCase

GW = latest_version_dir(REPO / "golden-thread-lotr")
LOTR_SCRIPTS = GW / "scripts"
sys.path.insert(0, str(LOTR_SCRIPTS))

from lotrlib import server, unlock  # noqa: E402
from lotrlib.engine import Engine  # noqa: E402
from lotrlib.errors import GatewayError  # noqa: E402

import gt_unlock_seal as SEAL  # noqa: E402  (SCRIPTS is on sys.path via the fixture)

MERGE = {"owner": "a", "repo": "b", "pull_number": 1}
ISSUE = {"owner": "a", "repo": "b", "title": "t"}
SEALED_VALUE = "sealed-test-" + "Z" * 24


class FakeConn:
    calls = []
    resolved = []

    def __init__(self, conn, profile, **kw):
        self.conn = conn
        self.resolver = kw.get("secret_resolver")

    def call(self, op, args, *, cursor=None):
        ref = (self.conn.get("auth") or {}).get("token_ref") or ""
        if ref.startswith("sealed:"):
            if self.resolver is None:
                from lotrlib import secrets
                value = secrets.resolve(ref)
            else:
                value = self.resolver(ref)
            # Record only THAT the right value arrived, never the value itself.
            FakeConn.resolved.append(value == SEALED_VALUE)
        FakeConn.calls.append((self.conn["id"], op.get("name")))
        return {"status": 200, "data": {"ok": True}, "next_cursor": None}


def _lotr_home(confirm="dialog"):
    base = None if IS_WINDOWS else "/tmp"           # macOS caps a socket path at 104 bytes
    home = Path(tempfile.mkdtemp(prefix="glu", dir=base))
    conn = {"id": "github@personal", "identity": "me", "zone": "personal", "kind": "http",
            "profile": "github", "description": "GitHub", "base_url": "https://api.github.com",
            "network": {"hosts": ["api.github.com"]},
            "auth": {"scheme": "bearer", "token_ref": "keychain:gt-lotr/x"},
            "trust": "T0", "policy": {"deny": [], "consent": [], "write": [], "read": []}}
    sealed = dict(conn, id="vault@personal", auth={"scheme": "bearer", "token_ref": "sealed:gh"})
    g = {"schema": 1, "zone": "personal", "mode": "local",
         "local": {"allow": ["*"], "max_tier": "consent", "confirm": confirm}}
    (home / "gateway.json").write_text(json.dumps(g))
    (home / "registry.json").write_text(json.dumps(
        {"schema": 1, "zone": "personal", "connections": [conn, sealed], "clients": []}))
    if not IS_WINDOWS:
        os.chmod(home, 0o700)
        for f in ("gateway.json", "registry.json"):
            os.chmod(home / f, 0o600)
    return home


class LotrUnlockCase(AuthorityCase):
    """The authority (fixture) + lotrd's front door in-process + helpers for real callers."""
    CONFIRM = "dialog"

    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.get(k) for k in ("GT_HOOKS_DIR", "GT_UNLOCK_HOME")}
        os.environ["GT_HOOKS_DIR"] = str(SCRIPTS)       # gt core of the release under test
        os.environ["GT_UNLOCK_HOME"] = self.home        # the fixture's authority
        unlock.reset()
        FakeConn.calls, FakeConn.resolved = [], []
        self.dialogs = []
        self.dialog_answer = True
        self.lhome = _lotr_home(self.CONFIRM)
        self.shims = []
        self.lstop = threading.Event()
        ready = threading.Event()
        self.lerr = []

        def factory():
            return Engine(self.lhome, connection_factory=FakeConn, dialog=self._dialog)

        def run():
            try:
                if IS_WINDOWS:
                    server.serve_pipe(factory, self.lhome, stop_event=self.lstop,
                                      on_ready=lambda a: ready.set())
                else:
                    server.serve_unix(factory, str(self.lhome / "lotrd.sock"),
                                      stop_event=self.lstop, on_ready=lambda a: ready.set())
            except Exception as e:                       # noqa: BLE001
                self.lerr.append(e)
                ready.set()
        self.lthread = threading.Thread(target=run, daemon=True)
        self.lthread.start()
        self.assertTrue(ready.wait(10), "lotrd's front door did not start")
        self.assertEqual(self.lerr, [])

    def tearDown(self):
        for p in self.shims:
            try:
                p.stdin.close()
                p.wait(10)
            except Exception:                            # noqa: BLE001
                p.kill()
                p.wait(5)
            for s in (p.stdout, p.stderr):
                try:
                    s.close()
                except Exception:                        # noqa: BLE001
                    pass
        self.lstop.set()
        self.lthread.join(10)
        shutil.rmtree(self.lhome, True)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        unlock.reset()
        super().tearDown()

    def _dialog(self, text):
        self.dialogs.append(text)
        return self.dialog_answer

    # -- policy shortcuts -------------------------------------------------------------
    def unlock_on(self, **over):
        """Unlock on with ONE factor (the software platform key) so nothing ever prompts, and
        TOTP answered only at a terminal (never an OS dialog in a test)."""
        self.enrol_totp()
        self.enrol_platform()
        pol = {"factors": {"required": 1, "require_one_of": ["touchid"]},
               "totp": {"prompt": "tty"}, "read_without_unlock": False}
        pol.update(over)
        self.set_policy(**pol)

    # -- real callers -------------------------------------------------------------------
    def cli(self, *args):
        r = subprocess.run([PYTHON, str(LOTR_SCRIPTS / "lotr.py"), "--home", str(self.lhome),
                            *args], capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL)
        try:
            out = json.loads(r.stdout)
        except ValueError:
            raise AssertionError("lotr printed no JSON: %r %r" % (r.stdout, r.stderr))
        return r.returncode, out

    def shim(self):
        p = subprocess.Popen([PYTHON, str(LOTR_SCRIPTS / "lotr_mcp.py"), "--home",
                              str(self.lhome)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.shims.append(p)
        p._mid = 0
        self.rpc(p, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}})
        return p

    def rpc(self, p, method, params):
        p._mid += 1
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": p._mid, "method": method,
                                  "params": params}) + "\n")
        p.stdin.flush()
        line = p.stdout.readline()
        if not line:
            raise AssertionError("the shim died: " + p.stderr.read())
        return json.loads(line)

    def tool(self, p, name, connection, op, args=None):
        r = self.rpc(p, "tools/call", {"name": name, "arguments": {
            "connection": connection, "op": op, "args": args or {}}})
        return r["result"]["structuredContent"]

    # -- logs ------------------------------------------------------------------------------
    def lotr_audit(self):
        path = self.lhome / "state" / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]

    def auth_audit(self):
        try:
            with open(self.auth.path("audit.jsonl"), "r", encoding="utf-8") as f:
                return [json.loads(x) for x in f if x.strip()]
        except OSError:
            return []

    def grants_issued(self):
        return [e.get("grant") for e in self.auth_audit() if e.get("event") == "grant"]


class UnlockOff(LotrUnlockCase):
    def test_off_changes_nothing(self):
        p = self.shim()
        self.assertTrue(self.tool(p, "call_read", "github@personal", "get_pull", MERGE)["ok"])
        self.assertTrue(self.tool(p, "call_write", "github@personal", "create_issue",
                                  ISSUE)["ok"])
        rc, out = self.cli("read", "github@personal", "get_pull", "owner=a", "repo=b",
                           "pull_number=1")
        self.assertEqual(rc, 0, out)
        lines = self.lotr_audit()
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(x["grant"] is None for x in lines))
        self.assertTrue(all("pid" not in x for x in lines), "off: no identity looked up")
        # Not one question reached the authority: no check, no register_shim.
        events = {e.get("event") for e in self.auth_audit()}
        self.assertFalse(events & {"check", "register_shim", "consent"}, events)

    def test_off_consent_uses_the_dialog_as_before(self):
        p = self.shim()
        self.assertTrue(self.tool(p, "call_consent", "github@personal", "merge_pull",
                                  MERGE)["ok"])
        self.assertEqual(len(self.dialogs), 1)


class Locked(LotrUnlockCase):
    def test_locked_read_is_refused_then_allowed_after_the_shim_unlocks(self):
        self.unlock_on()
        p = self.shim()
        regs = [e for e in self.auth_audit() if e.get("event") == "register_shim"]
        self.assertEqual([e.get("verdict") for e in regs], ["ok"])
        r = self.tool(p, "call_read", "github@personal", "get_pull", MERGE)
        self.assertTrue(r["ok"], r)
        grants = self.grants_issued()
        self.assertEqual(len(grants), 1, "the shim asked for exactly one unlock")
        self.assertEqual(r.get("grant"), grants[0])
        lines = self.lotr_audit()
        # First the refusal (locked, no grant), then the retried call under the new grant.
        self.assertEqual([x["verdict"] for x in lines], ["error:locked", "ok"])
        self.assertIsNone(lines[0]["grant"])
        self.assertEqual(lines[1]["grant"], grants[0])
        self.assertEqual(lines[1]["pid"], p.pid)
        self.assertEqual(FakeConn.calls, [("github@personal", "get_pull")])

    def test_shim_retries_once_and_never_loops(self):
        self.unlock_on()
        p = self.shim()
        self.platform.mode = "cancel"          # the person declines: no grant
        r = self.tool(p, "call_read", "github@personal", "get_pull", MERGE)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "locked")
        self.assertTrue(any("not granted" in h for h in r["error"]["hints"]), r)
        self.assertEqual(self.platform.calls, 1, "one unlock attempt, no loop")
        self.assertEqual(FakeConn.calls, [])
        self.assertEqual([x["verdict"] for x in self.lotr_audit()], ["error:locked"])

    def test_direct_engine_call_without_identity_is_refused_while_on(self):
        self.unlock_on()
        e = Engine(self.lhome, connection_factory=FakeConn, dialog=self._dialog)
        r = e.call("call_read", "github@personal", "get_pull", MERGE)
        self.assertEqual(r["error"]["code"], "peer_unidentified")
        self.assertEqual(FakeConn.calls, [])

    def test_hub_clients_keep_their_bearer_model(self):
        # An enrolled HTTP client is not a local process: no unlock check is made for it.
        self.unlock_on()
        reg = json.loads((self.lhome / "registry.json").read_text())
        reg["clients"] = [{"id": "lap", "machine": "m", "zone": "personal", "allow": ["*"],
                           "max_tier": "read", "secret_sha256": "0" * 64, "revoked": None}]
        (self.lhome / "registry.json").write_text(json.dumps(reg))
        e = Engine(self.lhome, connection_factory=FakeConn, dialog=self._dialog)
        r = e.call("call_read", "github@personal", "get_pull", MERGE, client_id="lap")
        self.assertTrue(r["ok"], r)
        self.assertIsNone(self.lotr_audit()[-1]["grant"])


class McpOnlyDoor(LotrUnlockCase):
    def test_a_shell_child_is_refused_even_while_the_shim_holds_a_grant(self):
        self.unlock_on()
        p = self.shim()
        self.assertTrue(self.tool(p, "call_read", "github@personal", "get_pull", MERGE)["ok"])
        self.assertEqual(len(self.grants_issued()), 1)
        rc, out = self.cli("read", "github@personal", "get_pull", "owner=a", "repo=b",
                           "pull_number=1")
        self.assertEqual(rc, 1)
        self.assertEqual(out["error"]["code"], "mcp_only")
        rc, out = self.cli("find", "pull")
        self.assertEqual(out["error"]["code"], "mcp_only", "the catalog is behind the door too")
        self.assertEqual(FakeConn.calls, [("github@personal", "get_pull")])
        refused = [x for x in self.lotr_audit() if x["verdict"] == "error:mcp_only"]
        self.assertEqual(len(refused), 1)
        self.assertIsNone(refused[0]["grant"])
        self.assertNotEqual(refused[0]["pid"], p.pid)

    def test_door_session_lets_the_session_ride_the_grant(self):
        # The contrast that shows the door is what refused above.
        self.unlock_on(door="session")
        p = self.shim()
        self.assertTrue(self.tool(p, "call_read", "github@personal", "get_pull", MERGE)["ok"])
        rc, out = self.cli("read", "github@personal", "get_pull", "owner=a", "repo=b",
                           "pull_number=1")
        self.assertEqual(rc, 0, out)

    def test_unregistered_child_never_gets_lotr_even_unlocked_by_hand(self):
        # A child of claude that is not the registered shim asks the authority to unlock for
        # it; it gets a session grant, and the door still refuses it lotr:*.
        self.unlock_on()
        c = self.child()
        res = self.call(c, "unlock", {"reason": "bash"})
        self.assertIn("grant", res.get("result") or {}, res)
        rc, out = self.cli("read", "github@personal", "get_pull", "owner=a", "repo=b",
                           "pull_number=1")
        self.assertEqual(out["error"]["code"], "mcp_only")


class PlatformConsent(LotrUnlockCase):
    def _unlocked_shim(self, **pol):
        self.unlock_on(**pol)
        p = self.shim()
        self.assertTrue(self.tool(p, "call_read", "github@personal", "get_pull", MERGE)["ok"])
        self.platform.calls = 0
        return p

    def test_platform_consent_replaces_the_dialog(self):
        p = self._unlocked_shim(consent_requires_factor="platform")
        r = self.tool(p, "call_consent", "github@personal", "merge_pull", MERGE)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.platform.calls, 1)
        self.assertEqual(self.dialogs, [])
        self.assertIn(("github@personal", "merge_pull"), FakeConn.calls)
        # window 0: the next consent op asks again
        self.assertTrue(self.tool(p, "call_consent", "github@personal", "merge_pull",
                                  MERGE)["ok"])
        self.assertEqual(self.platform.calls, 2)

    def test_consent_window_is_honoured(self):
        p = self._unlocked_shim(consent_requires_factor="platform", consent_window_s=300)
        for _ in range(3):
            self.assertTrue(self.tool(p, "call_consent", "github@personal", "merge_pull",
                                      MERGE)["ok"])
        self.assertEqual(self.platform.calls, 1, "one touch approves for the window")

    def test_failed_platform_factor_denies_and_never_falls_back(self):
        p = self._unlocked_shim(consent_requires_factor="platform")
        self.platform.mode = "forge"
        r = self.tool(p, "call_consent", "github@personal", "merge_pull", MERGE)
        self.assertEqual(r["error"]["code"], "consent_denied")
        self.assertEqual(self.dialogs, [], "a failed signature is not a cue to ask a weaker way")
        self.assertNotIn(("github@personal", "merge_pull"), FakeConn.calls)

    def test_mode_none_falls_back_to_the_dialog_path(self):
        p = self._unlocked_shim()                      # consent_requires_factor: none
        self.assertTrue(self.tool(p, "call_consent", "github@personal", "merge_pull",
                                  MERGE)["ok"])
        self.assertEqual(len(self.dialogs), 1)
        self.assertEqual(self.platform.calls, 0)
        self.dialog_answer = False
        r = self.tool(p, "call_consent", "github@personal", "merge_pull", MERGE)
        self.assertEqual(r["error"]["code"], "consent_denied")


class BiometricConfirm(LotrUnlockCase):
    CONFIRM = "biometric"

    def test_biometric_refuses_without_platform_consent(self):
        p = self.shim()                               # unlock off
        r = self.tool(p, "call_consent", "github@personal", "merge_pull", MERGE)
        self.assertEqual(r["error"]["code"], "consent_refused")
        self.assertEqual(self.dialogs, [])
        self.unlock_on()                              # on, but consent mode "none"
        p2 = self.shim()
        r = self.tool(p2, "call_consent", "github@personal", "merge_pull", MERGE)
        self.assertEqual(r["error"]["code"], "consent_refused")
        self.assertEqual(FakeConn.calls, [])

    def test_biometric_with_platform_consent_runs(self):
        self.unlock_on(consent_requires_factor="platform")
        p = self.shim()
        r = self.tool(p, "call_consent", "github@personal", "merge_pull", MERGE)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.dialogs, [])


class SealedSecrets(LotrUnlockCase):
    def setUp(self):
        super().setUp()
        self.unlock_on(door="session")
        SEAL.put(self.home, "gh", SEALED_VALUE, self.auth.enrolment(), self.factors)

    def test_sealed_resolves_only_under_a_grant(self):
        p = self.shim()
        self.platform.mode = "cancel"     # every unlock (and the sealed open) is declined
        r = self.tool(p, "call_read", "vault@personal", "get_pull", MERGE)
        self.assertFalse(r["ok"])
        self.assertEqual(FakeConn.resolved, [])
        self.platform.mode = "ok"
        self.auth.cooldown.clear()        # the authority's anti-carpet-prompt pause
        r = self.tool(p, "call_read", "vault@personal", "get_pull", MERGE)
        self.assertTrue(r["ok"], r)
        self.assertEqual(FakeConn.resolved, [True])
        grant = self.grants_issued()[-1]
        resolves = [e for e in self.auth_audit() if e.get("event") == "secret_resolve"]
        self.assertEqual(len(resolves), 1)
        self.assertEqual(resolves[0].get("grant"), grant)
        self.assertEqual(resolves[0].get("ref"), "sealed:gh")
        # The value never reaches either audit log.
        for path in (self.lhome / "state" / "audit.jsonl", Path(self.auth.path("audit.jsonl"))):
            self.assertNotIn(SEALED_VALUE, path.read_text(encoding="utf-8"))

    def test_lotr_never_reads_a_brokered_store_itself(self):
        # gt's client unloadable -> the ref is refused, not read some other way.
        os.environ["GT_HOOKS_DIR"] = tempfile.mkdtemp(prefix="nohooks")
        self.addCleanup(shutil.rmtree, os.environ["GT_HOOKS_DIR"], True)
        unlock.reset()
        from lotrlib import secrets
        with self.assertRaises(GatewayError) as c:
            secrets.resolve("sealed:gh")
        self.assertEqual(c.exception.code, "unlock_unavailable")
        self.assertNotIn(SEALED_VALUE, str(c.exception))

    def test_status_labels_levels(self):
        st = Engine(self.lhome, connection_factory=FakeConn).status()
        creds = {c["id"]: c["credential"] for c in st["connections"]}
        self.assertEqual(creds["github@personal"]["scheme"], "keychain")
        self.assertEqual(creds["github@personal"]["level"], "L1")
        self.assertEqual(creds["vault@personal"]["scheme"], "sealed")


class AuditAndTiers(LotrUnlockCase):
    def test_every_executed_op_and_every_refusal_is_audited_with_its_grant(self):
        self.unlock_on(consent_requires_factor="platform")
        p = self.shim()
        plan = [("call_read", "get_pull", MERGE), ("call_write", "create_issue", ISSUE),
                ("call_consent", "merge_pull", MERGE), ("call_read", "create_issue", ISSUE)]
        results = [self.tool(p, t, "github@personal", op, a) for t, op, a in plan]
        lines = self.lotr_audit()
        executed = [x for x in lines if x["verdict"] == "ok"]
        self.assertEqual(len(executed), len(FakeConn.calls))
        self.assertEqual(len(executed), 3)
        grant = self.grants_issued()[0]
        for x in executed:
            self.assertIn(x["tool"], ("call_read", "call_write", "call_consent"))
            self.assertIn(x["tier"], ("read", "write", "consent"))
            self.assertEqual(x["grant"], grant)
        # Every call made produced a line: 4 calls + the first refusal before the unlock.
        self.assertEqual(len(lines), len(plan) + 1)
        # Tier rules untouched: read can never carry a write; an unknown verb is a write.
        self.assertEqual(results[3]["error"]["code"], "wrong_tool")
        self.assertEqual(lines[-1]["tier"], "write")
        from lotrlib import policy
        self.assertEqual(policy.classify({}, {"method": "PURGE", "path": "/x"}), "write")
        self.assertFalse(policy.allowed_through("call_read", "write"))

    def test_consent_always_confirms(self):
        # Unlocked and granted, a consent op still needs its own confirmation (dialog here).
        self.unlock_on()
        p = self.shim()
        self.assertTrue(self.tool(p, "call_read", "github@personal", "get_pull", MERGE)["ok"])
        self.dialog_answer = False
        r = self.tool(p, "call_consent", "github@personal", "merge_pull", MERGE)
        self.assertEqual(r["error"]["code"], "consent_denied")
        self.assertEqual(len(self.dialogs), 1)


class HubAdmin(LotrUnlockCase):
    def test_enroll_needs_step_up(self):
        # fresh_s 0: the unlock's own Touch ID does not count as the step-up, so the step-up
        # itself is visible in the authority's audit.
        self.unlock_on(step_up={"factors": ["touchid", "hello"], "fresh_s": 0})
        out = Path(tempfile.mkdtemp(prefix="sec")) / "lap.secret"
        self.addCleanup(shutil.rmtree, out.parent, True)
        # The session is unlocked first (an ordinary grant), so what enrol needs on top of it
        # is the step-up alone. The pause outlasts a coarse monotonic clock (Windows: ~16 ms).
        c = self.child()
        self.assertIn("grant", self.call(c, "unlock", {"reason": "session"}).get("result") or {})
        time.sleep(0.1)
        self.platform.mode = "cancel"
        rc, res = self.cli("enroll", "lap", "--machine", "m", "--secret-out", str(out))
        self.assertEqual(rc, 1, res)
        self.assertFalse(out.exists(), "nothing written before the authority answers")
        self.assertNotIn("lap", (self.lhome / "registry.json").read_text())
        self.platform.mode = "ok"
        self.auth.cooldown.clear()
        time.sleep(0.1)
        rc, res = self.cli("enroll", "lap", "--machine", "m", "--secret-out", str(out))
        self.assertEqual(rc, 0, res)
        self.assertTrue(out.exists())
        self.assertTrue(any(e.get("event") == "step_up" for e in self.auth_audit()))
        rc, res = self.cli("revoke", "lap")
        self.assertEqual(rc, 0, res)

    def test_enroll_unchanged_when_off(self):
        out = Path(tempfile.mkdtemp(prefix="sec")) / "lap.secret"
        self.addCleanup(shutil.rmtree, out.parent, True)
        rc, res = self.cli("enroll", "lap", "--machine", "m", "--secret-out", str(out))
        self.assertEqual(rc, 0, res)
        self.assertFalse(any(e.get("event") == "check" for e in self.auth_audit()))


class FailClosed(LotrUnlockCase):
    def test_client_unloadable_while_on_refuses(self):
        self.unlock_on()
        empty = tempfile.mkdtemp(prefix="nohooks")
        self.addCleanup(shutil.rmtree, empty, True)
        os.environ["GT_HOOKS_DIR"] = empty
        unlock.reset()
        self.assertIsNone(unlock.client())
        self.assertTrue(unlock.enabled(), "the policy file alone says on")
        e = Engine(self.lhome, connection_factory=FakeConn, dialog=self._dialog)
        r = e.call("call_read", "github@personal", "get_pull", MERGE,
                   subject={"pid": os.getpid(), "start": 0})
        self.assertEqual(r["error"]["code"], "unlock_unavailable")
        self.assertEqual(FakeConn.calls, [])

    def test_client_unloadable_while_off_is_0_2_0(self):
        empty = tempfile.mkdtemp(prefix="nohooks")
        self.addCleanup(shutil.rmtree, empty, True)
        os.environ["GT_HOOKS_DIR"] = empty
        unlock.reset()
        self.assertFalse(unlock.enabled())
        e = Engine(self.lhome, connection_factory=FakeConn, dialog=self._dialog)
        self.assertTrue(e.call("call_read", "github@personal", "get_pull", MERGE)["ok"])


@unittest.skipUnless(IS_WINDOWS, "the named-pipe front door is the native-Windows door")
class WindowsPipe(LotrUnlockCase):
    def test_cli_through_the_pipe(self):
        rc, out = self.cli("find", "pull")
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("status")
        self.assertEqual(rc, 0, out)

    def test_pipe_acl_and_first_instance(self):
        import gt_ipc
        self.assertEqual(gt_ipc.pipe_sddl(), "D:P(A;;GA;;;%s)" % gt_ipc.own_sid())
        with self.assertRaises(GatewayError) as c:
            server.serve_pipe(lambda: None, self.lhome, stop_event=threading.Event())
        self.assertEqual(c.exception.code, "daemon_running")
        with self.assertRaises(gt_ipc.IpcError) as c2:
            gt_ipc.create_pipe_instance(server.pipe_address(self.lhome), first=True)
        self.assertEqual(c2.exception.code, "pipe_squatted")


if __name__ == "__main__":
    unittest.main()
