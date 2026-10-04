"""Regression tests for the independent security review of gt 0.20.0 / gt-lotr 0.3.0
(2026-10-03 16:15 CDT): the gt-lotr side, the Touch ID helper, and the review's low findings.

Each test failed against fe571f2, the reviewed commit; the finding it pins is in its docstring.
The authority-side harnesses are in test_unlock_review_regressions.py.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from _harness import GT, IS_WINDOWS, PYTHON, SCRIPTS, rmtree
from test_lotr_unlock import (FakeConn, LOTR_SCRIPTS, MERGE, SEAL, SEALED_VALUE,  # noqa: F401
                              LotrUnlockCase)

from lotrlib import server, unlock          # noqa: E402
from lotrlib.engine import Engine           # noqa: E402
from lotrlib.errors import GatewayError     # noqa: E402

ISSUE = {"owner": "a", "repo": "b", "title": "t"}


def _client(cid, max_tier="read"):
    import hashlib
    return {"id": cid, "machine": "m", "zone": "personal", "allow": ["*"], "max_tier": max_tier,
            "secret_sha256": hashlib.sha256(b"s3cret").hexdigest(), "revoked": None}


class ShimAfterRestart(LotrUnlockCase):
    def test_the_real_shim_re_registers_after_an_authority_restart(self):
        """F7: after the authority restarted the shim never registered again, so every call
        came back mcp_only (and the seat stayed free for anyone)."""
        self.unlock_on()
        p = self.shim()
        r = self.tool(p, "call_read", "github@personal", "get_pull", MERGE)
        self.assertTrue(r["ok"], r)
        self.restart_authority()
        self.auth.cooldown.clear()
        r = self.tool(p, "call_read", "github@personal", "get_pull", MERGE)
        self.assertTrue(r["ok"], r)
        regs = [e for e in self.auth_audit() if e.get("event") == "register_shim"
                and e.get("verdict") == "ok"]
        self.assertGreaterEqual(len(regs), 2)


class HubClients(LotrUnlockCase):
    def _registry_with(self, *clients):
        reg = json.loads((self.lhome / "registry.json").read_text())
        reg["clients"] = list(clients)
        (self.lhome / "registry.json").write_text(json.dumps(reg))
        if not IS_WINDOWS:
            os.chmod(self.lhome / "registry.json", 0o600)

    def test_M2_an_http_client_named_local_is_never_the_local_caller(self):
        """M2: the hub HTTP path handed client_id "local" to the engine, which then applied
        gateway.json's `local` rules (any tier) instead of the client's max_tier."""
        self._registry_with(_client("local", "read"))
        from lotrlib.registry import Registry
        port = []
        ready = threading.Event()
        stop = threading.Event()

        def factory():
            return Engine(self.lhome, connection_factory=FakeConn, dialog=self._dialog)

        def getter():
            return Registry.load(self.lhome / "registry.json")

        t = threading.Thread(target=server.serve_http, args=(factory, "127.0.0.1", 0),
                             kwargs={"registry_getter": getter, "stop_event": stop,
                                     "on_ready": lambda h, pt: (port.append(pt), ready.set())},
                             daemon=True)
        t.start()
        self.assertTrue(ready.wait(10))
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/v1/call" % port[0], method="POST",
                data=json.dumps({"tool": "call_write", "connection": "github@personal",
                                 "op": "create_issue", "args": ISSUE}).encode(),
                headers={"Authorization": "Bearer s3cret", "X-GT-Client": "local",
                         "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                body = json.loads(e.read() or b"{}")
            self.assertIn("error", body if "error" in body else body.get("result", {}), body)
            self.assertEqual(FakeConn.calls, [])
        finally:
            stop.set()
            t.join(10)

    def test_a_registry_client_cannot_be_named_local(self):
        from lotrlib.registry import Registry
        self._registry_with(_client("local"))
        with self.assertRaises(GatewayError):
            Registry.load(self.lhome / "registry.json")

    def test_M3_a_hub_clients_brokered_ref_is_asked_under_the_hub_client(self):
        """M3: a hub client's brokered ref was resolved under lotrd's OWN identity. It is now
        asked as the unattended job lotr-hub:<client>, which only the allow-list opens."""
        self.unlock_on()
        self._registry_with(_client("lap", "read"))
        seen = []
        real = unlock.call

        def spy(method, params=None, **kw):
            seen.append((method, dict(params or {})))
            return real(method, params, **kw)
        unlock.call = spy
        try:
            e = Engine(self.lhome, connection_factory=FakeConn, dialog=self._dialog)
            r = e.call("call_read", "vault@personal", "get_pull", MERGE, client_id="lap")
        finally:
            unlock.call = real
        secrets_asked = [p for m, p in seen if m == "secret"]
        self.assertTrue(secrets_asked, seen)
        self.assertEqual(secrets_asked[0].get("job"), "lotr-hub:lap")
        self.assertNotIn("subject", secrets_asked[0])
        self.assertFalse(r["ok"])
        self.assertEqual(FakeConn.resolved, [])

    @unittest.skipIf(IS_WINDOWS, "POSIX mode bits")
    def test_a_world_readable_registry_is_refused(self):
        """M2: registry.json was reloaded by the hub without a permission check."""
        sys.path.insert(0, str(LOTR_SCRIPTS))
        import lotrd
        os.chmod(self.lhome / "registry.json", 0o644)
        with self.assertRaises(GatewayError) as c:
            lotrd.registry_getter_for(self.lhome / "registry.json")()
        self.assertEqual(c.exception.code, "insecure_perms")


class BrokeredWhileOff(LotrUnlockCase):
    def test_unlock_off_never_starts_the_authority_for_a_brokered_ref(self):
        """Low: with unlock OFF, resolving a brokered ref asked the authority with start=True,
        which spawns a daemon."""
        seen = []
        real = unlock.call

        def spy(method, params=None, **kw):
            seen.append((method, kw.get("start", True)))
            raise GatewayError("unreachable", "spy")
        unlock.call = spy
        try:
            from lotrlib import secrets
            with self.assertRaises(GatewayError) as c:
                secrets.resolve("sealed:gh")
        finally:
            unlock.call = real
        self.assertEqual(c.exception.code, "unlock_off")
        self.assertEqual(seen, [])


@unittest.skipUnless(sys.platform == "darwin", "Touch ID is macOS only")
class TouchIdHelperSwap(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        import gt_unlock_touchid as T
        self.T = T
        self.d = tempfile.mkdtemp(prefix="gt-helper-swap.")
        self.helper = os.path.join(self.d, "gt-presence")
        self._write('{"secure_enclave": true, "biometry": true, "reason": "fake"}')

    def tearDown(self):
        rmtree(self.d)

    def _write(self, answer):
        with open(self.helper, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/bin/sh\ncat >/dev/null\necho '%s'\n" % answer)
        os.chmod(self.helper, 0o755)

    def test_a_swapped_helper_is_unavailable(self):
        """Touch ID helper swap: install kept an existing gt-presence without hashing it, and
        nothing checked it before use."""
        T = self.T
        self.assertFalse(T.TouchIdFactor(helper=self.helper).available()[0],
                         "an unrecorded helper must not be used")
        T.record_helper(self.helper)
        self.assertTrue(T.TouchIdFactor(helper=self.helper).available()[0])
        self._write('{"secure_enclave": true, "biometry": true, "reason": "swapped"}')
        ok, why = T.TouchIdFactor(helper=self.helper).available()
        self.assertFalse(ok)
        self.assertIn("does not match", why)


class LowFindings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gt-review-low.")

    def tearDown(self):
        rmtree(self.tmp)

    @unittest.skipIf(IS_WINDOWS, "symlinks need privileges on Windows")
    def test_localize_mcp_never_writes_through_a_predictable_tmp(self):
        """Low: localize_mcp wrote <plugin.json>.tmp, a name anyone could plant first."""
        sys.path.insert(0, str(SCRIPTS))
        import gt_components as GC
        pj = os.path.join(self.tmp, "plugin.json")
        with open(pj, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"x": {"command": "python3", "args": ["s.py"]}}}, f)
        victim = os.path.join(self.tmp, "victim.txt")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("untouched")
        os.symlink(victim, pj + ".tmp")
        GC.localize_mcp([pj], sys.executable)
        with open(victim, encoding="utf-8") as f:
            self.assertEqual(f.read(), "untouched")
        with open(pj, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["mcpServers"]["x"]["command"], sys.executable)
        self.assertFalse(stat.S_ISLNK(os.lstat(pj).st_mode))

    def test_manifest_covers_agents_and_workflows(self):
        """Low: agents/ and workflows/ were not in MANIFEST.json, so drift checks could not
        see an edited agent definition or workflow."""
        sys.path.insert(0, str(SCRIPTS))
        import gt_components as GC
        files = GC.build_manifest(str(GT))["files"]
        with open(GT / "MANIFEST.json", encoding="utf-8") as f:
            shipped = json.load(f)["files"]
        for rel in ("agents/extract.md", "workflows/pipeline-stage.js"):
            self.assertTrue(rel in files, "build_manifest does not hash %s" % rel)
            self.assertTrue(rel in shipped, "the shipped MANIFEST.json does not carry %s" % rel)
        # the drift check maps the workflow to its installed copy (the plugin cache); installed
        # agent definitions are rewritten by gt_model_policy and judged by gt_agent_spec
        # resolve instead, so they are not hash-compared
        self.assertTrue(GC.hooks_and_packs_map(GT.name)("workflows/pipeline-stage.js"))
        self.assertIsNone(GC.hooks_and_packs_map(GT.name)("agents/extract.md"))

    def test_extract_prompt_forbids_shell_and_network_on_every_route(self):
        """Medium: the extract agent's Read/Grep/Glob limit held only on the gt:extract route;
        the fallback (Agent tool + model) spawns with every tool. The rendered prompt now says
        so to the agent on every route (an advisory limit there, and documented as one)."""
        r = subprocess.run([PYTHON, str(SCRIPTS / "gt_agent_spec.py"), "render", "extract-code",
                            "--template"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue("## Tools" in r.stdout, "the rendered extract prompt has no Tools section")
        self.assertIn("Read, Grep and Glob", r.stdout)
        self.assertIn("never run a shell command", r.stdout.lower())


if __name__ == "__main__":
    unittest.main()
