"""gt-lotr core: secrets, registry, policy and audit.

The FIRST class here is the leak test, deliberately (same reasoning as test_gt_secrets): the
gateway exists to hold credentials on the model's behalf, so a value that reaches an
exception, a repr, stdout/stderr or the audit log is a release blocker however well the rest
works. Every distinctive value below is an obvious fake, built so a grep for it is exact.

Pure in-process imports: lotr reads no HOME-dependent config at import time. The keychain is
never touched -- `secrets.SECURITY_BIN` is pointed at a fake script per test.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import REPO, latest_version_dir


def _gateway_dir():
    root = REPO / "golden-thread-lotr"
    try:
        return latest_version_dir(root)
    except RuntimeError:
        # The module is being built and has no plugin.json yet: take the newest version dir.
        cands = [d for d in root.iterdir() if d.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+", d.name)]
        return max(cands, key=lambda d: tuple(int(x) for x in d.name.split(".")))


sys.path.insert(0, str(_gateway_dir() / "scripts"))

from lotrlib import audit, policy, registry, secrets  # noqa: E402
from lotrlib.errors import GatewayError  # noqa: E402

FAKE_VALUE = "lorFAKE-" + "Qz7pL2xV9kM4nR8tW1sY6"   # distinctive, never a real credential


def conn_entry(**over):
    e = {
        "id": "github@personal", "identity": "shaven @ github.com", "zone": "personal",
        "kind": "http", "profile": "github", "description": "GitHub as shaven",
        "base_url": "https://api.github.com",
        "auth": {"scheme": "bearer", "token_ref": "keychain:gt-lotr/github-personal",
                 "user": None, "header": None},
        "headers": {}, "network": {"hosts": ["api.github.com"]}, "trust": "T0",
        "policy": {"deny": [], "consent": [], "write": [], "read": []}, "enabled": True,
    }
    e.update(over)
    return e


class TmpBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self._env = dict(os.environ)
        self._bin = secrets.SECURITY_BIN

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        secrets.SECURITY_BIN = self._bin
        self._td.cleanup()

    def secret_file(self, name="tok", value=FAKE_VALUE, mode=0o600, newline=True):
        p = self.tmp / name
        p.write_text(value + ("\n" if newline else ""))
        os.chmod(p, mode)
        return p

    def fake_security(self, value=None, exit_code=0):
        """A stand-in for /usr/bin/security that records its argv and prints `value`."""
        log = self.tmp / "security.argv"
        script = self.tmp / "security"
        out = f"printf '%s\\n' '{value}'" if value is not None else ":"
        script.write_text("#!/bin/sh\n"
                          f"printf '%s\\n' \"$@\" > '{log}'\n"
                          "echo 'security: some diagnostic' >&2\n"
                          f"{out}\nexit {exit_code}\n")
        os.chmod(script, 0o700)
        secrets.SECURITY_BIN = str(script)
        return log


# --------------------------------------------------------------------------- the contract

class TestNoSecretLeaks(TmpBase):
    """A resolved value never appears in an error, a repr, captured output or the audit log."""

    def assertClean(self, *texts):
        for t in texts:
            self.assertNotIn(FAKE_VALUE, t, "secret value leaked")

    def run_capturing(self, fn):
        out, err = io.StringIO(), io.StringIO()
        exc = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                fn()
            except GatewayError as e:
                exc = e
        return exc, out.getvalue(), err.getvalue()

    def test_file_perms_error_names_ref_not_value(self):
        p = self.secret_file(mode=0o644)
        ref = f"file:{p}"
        exc, out, err = self.run_capturing(lambda: secrets.resolve(ref))
        self.assertEqual(exc.code, "secret_perms")
        self.assertIn(ref, str(exc))
        self.assertClean(str(exc), repr(exc), json.dumps(exc.to_dict()), out, err)

    def test_env_refused_error_does_not_leak(self):
        os.environ["LOTR_TEST_TOKEN"] = FAKE_VALUE
        os.environ.pop("LOTR_ALLOW_ENV_SECRETS", None)
        exc, out, err = self.run_capturing(lambda: secrets.resolve("env:LOTR_TEST_TOKEN"))
        self.assertEqual(exc.code, "secret_scheme_refused")
        self.assertClean(str(exc), repr(exc), out, err)

    def test_resolve_success_prints_nothing(self):
        os.environ["LOTR_ALLOW_ENV_SECRETS"] = "1"
        os.environ["LOTR_TEST_TOKEN"] = FAKE_VALUE
        p = self.secret_file()
        log = self.fake_security(FAKE_VALUE)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            vals = [secrets.resolve("env:LOTR_TEST_TOKEN"), secrets.resolve(f"file:{p}"),
                    secrets.resolve("keychain:svc/acct")]
            descs = [secrets.describe(f"file:{p}"), secrets.describe("keychain:svc/acct"),
                     secrets.describe("env:LOTR_TEST_TOKEN")]
        self.assertEqual(vals, [FAKE_VALUE] * 3)
        self.assertClean(out.getvalue(), err.getvalue(), json.dumps(descs))
        # argv carried the service/account only, never a value.
        self.assertClean(log.read_text())

    def test_literal_token_in_registry_is_not_echoed(self):
        e = conn_entry()
        e["auth"]["token_ref"] = FAKE_VALUE   # someone pasted the token itself
        with self.assertRaises(GatewayError) as cm:
            registry.validate_connection(e, "personal")
        self.assertEqual(cm.exception.code, "registry_invalid")
        self.assertIn("token_ref", str(cm.exception))
        self.assertClean(str(cm.exception))

    def test_literal_passed_to_resolve_is_not_echoed(self):
        for bogus in (FAKE_VALUE, "bearer:" + FAKE_VALUE):
            with self.assertRaises(GatewayError) as cm:
                secrets.resolve(bogus)
            self.assertEqual(cm.exception.code, "secret_ref_invalid")
            self.assertClean(str(cm.exception))

    def test_enrolled_secret_never_stored_or_in_repr(self):
        reg = registry.Registry("personal")
        secret = reg.enroll("mbp", "MacBook", ["*"], "write")
        path = self.tmp / "registry.json"
        reg.save(path)
        self.assertNotIn(secret, path.read_text())
        self.assertNotIn(secret, repr(reg))
        with self.assertRaises(GatewayError) as cm:
            reg.authenticate("mbp", secret + "x")
        self.assertNotIn(secret, str(cm.exception))

    def test_audit_never_writes_raw_args(self):
        home = self.tmp / "home"
        audit.record(home, client="local", connection="github@personal", op="list_pulls",
                     tier="read", tool="call_read", verdict="ok", status=200,
                     args={"token": FAKE_VALUE}, body=FAKE_VALUE, extra={"x": FAKE_VALUE})
        text = (home / "state" / "audit.jsonl").read_text()
        self.assertClean(text)
        rec = json.loads(text)
        self.assertNotIn("args", rec)
        self.assertNotIn("body", rec)
        self.assertNotIn("extra", rec)
        self.assertEqual(rec["args_sha256"], audit.args_sha256({"token": FAKE_VALUE}))


# ------------------------------------------------------------------------------- secrets

class TestSecrets(TmpBase):
    def test_file_strips_one_trailing_newline(self):
        p = self.tmp / "t"
        p.write_text("abc\n\n")
        os.chmod(p, 0o600)
        self.assertEqual(secrets.resolve(f"file:{p}"), "abc\n")

    def test_file_group_or_world_readable_refused(self):
        for mode in (0o640, 0o604, 0o660, 0o606):
            p = self.secret_file(name=f"t{mode:o}", mode=mode)
            with self.assertRaises(GatewayError) as cm:
                secrets.resolve(f"file:{p}")
            self.assertEqual(cm.exception.code, "secret_perms", oct(mode))

    def test_file_missing_and_relative(self):
        with self.assertRaises(GatewayError) as cm:
            secrets.resolve(f"file:{self.tmp}/nope")
        self.assertEqual(cm.exception.code, "secret_missing")
        with self.assertRaises(GatewayError) as cm:
            secrets.resolve("file:relative/path")
        self.assertEqual(cm.exception.code, "secret_ref_invalid")

    def test_store_uses_store_dir_and_env(self):
        store = self.tmp / "store"
        store.mkdir()
        (store / "jira").write_text("v1\n")
        os.chmod(store / "jira", 0o600)
        self.assertEqual(secrets.resolve("store:jira", store_dir=store), "v1")
        os.environ["LOTR_STORE_DIR"] = str(store)
        self.assertEqual(secrets.resolve("store:jira"), "v1")
        with self.assertRaises(GatewayError) as cm:
            secrets.resolve("store:../jira", store_dir=store)
        self.assertEqual(cm.exception.code, "secret_ref_invalid")

    def test_keychain_argv_and_value(self):
        log = self.fake_security("kc-value")
        self.assertEqual(secrets.resolve("keychain:gt-lotr/github-personal"), "kc-value")
        self.assertEqual(log.read_text().split("\n")[:6],
                         ["find-generic-password", "-s", "gt-lotr", "-a", "github-personal", "-w"])

    def test_keychain_not_found(self):
        self.fake_security(None, exit_code=44)
        with self.assertRaises(GatewayError) as cm:
            secrets.resolve("keychain:svc/acct")
        self.assertEqual(cm.exception.code, "secret_missing")
        self.assertIn("keychain:svc/acct", str(cm.exception))
        self.assertFalse(secrets.describe("keychain:svc/acct")["present"])

    def test_keychain_ref_shape(self):
        with self.assertRaises(GatewayError) as cm:
            secrets.resolve("keychain:noaccount")
        self.assertEqual(cm.exception.code, "secret_ref_invalid")

    def test_env_allowed_only_with_flag(self):
        os.environ["LOTR_X"] = "v"
        os.environ.pop("LOTR_ALLOW_ENV_SECRETS", None)
        with self.assertRaises(GatewayError) as cm:
            secrets.resolve("env:LOTR_X")
        self.assertEqual(cm.exception.code, "secret_scheme_refused")
        os.environ["LOTR_ALLOW_ENV_SECRETS"] = "1"
        self.assertEqual(secrets.resolve("env:LOTR_X"), "v")
        with self.assertRaises(GatewayError) as cm:
            secrets.resolve("env:LOTR_UNSET_VAR")
        self.assertEqual(cm.exception.code, "secret_missing")

    def test_unknown_scheme(self):
        with self.assertRaises(GatewayError) as cm:
            secrets.resolve("vault:x")
        self.assertEqual(cm.exception.code, "secret_ref_invalid")

    def test_describe_file_is_stat_only(self):
        p = self.secret_file()
        d = secrets.describe(f"file:{p}")
        self.assertEqual(d, {"scheme": "file", "target": str(p), "present": True})
        self.assertFalse(secrets.describe(f"file:{self.tmp}/none")["present"])


# ------------------------------------------------------------------------------ registry

class TestRegistry(TmpBase):
    def write_reg(self, conns=(), clients=(), zone="personal"):
        p = self.tmp / "registry.json"
        p.write_text(json.dumps({"schema": 1, "zone": zone, "connections": list(conns),
                                 "clients": list(clients)}))
        return p

    def assertInvalid(self, entry, field):
        with self.assertRaises(GatewayError) as cm:
            registry.validate_connection(entry, "personal")
        self.assertEqual(cm.exception.code, "registry_invalid")
        self.assertIn(field, str(cm.exception))

    def test_valid_entry_loads(self):
        reg = registry.Registry.load(self.write_reg([conn_entry()]))
        self.assertEqual(reg.zone, "personal")
        self.assertEqual([c["id"] for c in reg.active()], ["github@personal"])
        self.assertEqual(reg.connection("github@personal")["profile"], "github")

    def test_id_regex(self):
        for bad in ("GitHub@personal", "github", "@personal", "github@", "-x@personal", "a b@c"):
            self.assertInvalid(conn_entry(id=bad), "id")

    def test_zone_mismatch_refused(self):
        self.assertInvalid(conn_entry(zone="work"), "zone")
        with self.assertRaises(GatewayError):
            registry.Registry.load(self.write_reg([conn_entry()], zone="work"))

    def test_kind_and_trust(self):
        self.assertInvalid(conn_entry(kind="stdio"), "kind")
        self.assertInvalid(conn_entry(trust="T9"), "trust")

    def test_base_url_scheme(self):
        self.assertInvalid(conn_entry(base_url="http://api.github.com"), "base_url")
        self.assertInvalid(conn_entry(base_url="ftp://api.github.com"), "base_url")
        for local in ("127.0.0.1", "localhost"):
            registry.validate_connection(conn_entry(
                base_url=f"http://{local}:8080/api", network={"hosts": [local]}), "personal")

    def test_host_must_be_pinned(self):
        self.assertInvalid(conn_entry(network={"hosts": ["evil.example"]}), "network.hosts")
        self.assertInvalid(conn_entry(network={}), "network.hosts")

    def test_token_ref_required_and_shaped(self):
        e = conn_entry()
        e["auth"] = {"scheme": "bearer"}
        self.assertInvalid(e, "token_ref")
        for ok in ("file:/x", "store:x", "keychain:a/b", "env:X"):
            e["auth"] = {"scheme": "bearer", "token_ref": ok}
            registry.validate_connection(e, "personal")
        e["auth"] = {"scheme": "none"}
        registry.validate_connection(e, "personal")

    def test_unknown_connection_hints(self):
        reg = registry.Registry.load(self.write_reg([conn_entry()]))
        with self.assertRaises(GatewayError) as cm:
            reg.connection("githb@personal")
        self.assertEqual(cm.exception.code, "unknown_connection")
        self.assertIn("github@personal", cm.exception.hints)

    def test_disabled_excluded(self):
        reg = registry.Registry.load(self.write_reg([conn_entry(enabled=False)]))
        self.assertEqual(reg.active(), [])
        self.assertIn("github@personal", reg.connections)
        with self.assertRaises(GatewayError) as cm:
            reg.connection("github@personal")
        self.assertEqual(cm.exception.code, "connection_disabled")

    def test_add_connection(self):
        reg = registry.Registry("personal")
        reg.add_connection(conn_entry())
        with self.assertRaises(GatewayError) as cm:
            reg.add_connection(conn_entry())
        self.assertEqual(cm.exception.code, "duplicate_connection")
        with self.assertRaises(GatewayError) as cm:
            reg.add_connection(conn_entry(id="jira@work", zone="work"))
        self.assertEqual(cm.exception.code, "registry_invalid")

    def test_enroll_authenticate_revoke_roundtrip(self):
        reg = registry.Registry("personal")
        s = reg.enroll("mbp-shaven", "MacBook Pro", ["github@*"], "read")
        self.assertGreaterEqual(len(s), 40)
        c = reg.client("mbp-shaven")
        self.assertEqual(c["secret_sha256"], hashlib.sha256(s.encode()).hexdigest())
        self.assertNotIn("secret", {k for k in c if k != "secret_sha256"})
        path = self.tmp / "r.json"
        reg.save(path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        reg2 = registry.Registry.load(path)
        self.assertEqual(reg2.authenticate("mbp-shaven", s)["id"], "mbp-shaven")
        with self.assertRaises(GatewayError) as cm:
            reg2.authenticate("mbp-shaven", "wrong")
        self.assertEqual(cm.exception.code, "auth_failed")
        with self.assertRaises(GatewayError) as cm:
            reg2.authenticate("nobody", s)
        self.assertEqual(cm.exception.code, "unknown_client")
        reg2.revoke("mbp-shaven")
        with self.assertRaises(GatewayError) as cm:
            reg2.authenticate("mbp-shaven", s)
        self.assertEqual(cm.exception.code, "client_revoked")
        # Two enrolments never share a secret.
        self.assertNotEqual(s, reg.enroll("other", "x", ["*"], "write"))

    def test_enroll_duplicate_refused(self):
        reg = registry.Registry("personal")
        reg.enroll("a", "m", ["*"], "write")
        with self.assertRaises(GatewayError) as cm:
            reg.enroll("a", "m", ["*"], "write")
        self.assertEqual(cm.exception.code, "duplicate_client")
        with self.assertRaises(GatewayError):
            reg.enroll("b", "m", ["*"], "admin")

    def test_bad_json_and_schema(self):
        p = self.tmp / "registry.json"
        p.write_text("{nope")
        with self.assertRaises(GatewayError) as cm:
            registry.Registry.load(p)
        self.assertEqual(cm.exception.code, "bad_json")
        p.write_text(json.dumps({"schema": 2, "zone": "personal"}))
        with self.assertRaises(GatewayError) as cm:
            registry.Registry.load(p)
        self.assertEqual(cm.exception.code, "registry_invalid")


# -------------------------------------------------------------------------------- policy

class TestPolicy(unittest.TestCase):
    def conn(self, **pol):
        base = {"deny": [], "consent": [], "write": [], "read": []}
        base.update(pol)
        return {"policy": base}

    def test_method_default(self):
        c = self.conn()
        self.assertEqual(policy.classify(c, {"method": "GET", "path": "/x"}), "read")
        self.assertEqual(policy.classify(c, {"method": "head", "path": "/x"}), "read")
        for m in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(policy.classify(c, {"method": m, "path": "/x"}), "write")
        self.assertEqual(policy.classify(c, {}), "write")   # unknown -> write

    def test_order_deny_consent_write_read(self):
        op = {"name": "merge_pull", "method": "PUT", "path": "/repos/a/b/pulls/1/merge"}
        everything = dict(deny=["merge_*"], consent=["merge_pull"], write=["*"], read=["*"])
        self.assertEqual(policy.classify(self.conn(**everything), op), "deny")
        everything["deny"] = []
        self.assertEqual(policy.classify(self.conn(**everything), op), "consent")
        everything["consent"] = []
        self.assertEqual(policy.classify(self.conn(**everything), op), "write")
        everything["write"] = []
        self.assertEqual(policy.classify(self.conn(**everything), op), "read")

    def test_glob_matches_method_path_form(self):
        c = self.conn(deny=["DELETE /repos/*"])
        self.assertEqual(policy.classify(c, {"name": None, "method": "delete",
                                             "path": "/repos/a/b"}), "deny")
        self.assertEqual(policy.classify(c, {"name": None, "method": "GET",
                                             "path": "/repos/a/b"}), "read")

    def test_policy_beats_profile_tier_beats_method(self):
        op = {"name": "merge_pull", "method": "PUT", "path": "/x", "tier": "consent"}
        self.assertEqual(policy.classify(self.conn(), op), "consent")
        self.assertEqual(policy.classify(self.conn(write=["merge_pull"]), op), "write")
        self.assertEqual(policy.classify(self.conn(), {"method": "GET", "path": "/x",
                                                       "tier": "write"}), "write")

    def test_graphql(self):
        c = self.conn()
        g = {"name": "graphql", "method": "POST", "path": "/graphql"}
        self.assertEqual(policy.classify(c, dict(g, graphql_query="query { viewer { login } }")), "read")
        self.assertEqual(policy.classify(c, dict(g, graphql_query="{ viewer { login } }")), "read")
        self.assertEqual(policy.classify(c, dict(g, graphql_query="  # c\n mutation X { a }")), "write")
        self.assertEqual(policy.classify(self.conn(deny=["graphql"]),
                                         dict(g, graphql_query="query { a }")), "deny")

    def test_allowed_through(self):
        cases = {"call_read": {"read"}, "call_write": {"read", "write"},
                 "call_consent": {"read", "write", "consent"}}
        for tool, ok in cases.items():
            for tier in ("read", "write", "consent", "deny"):
                self.assertEqual(policy.allowed_through(tool, tier), tier in ok, (tool, tier))
        self.assertFalse(policy.allowed_through("find", "read"))

    def test_check_client(self):
        client = {"id": "mbp", "allow": ["github@*", "jira@personal"], "max_tier": "write",
                  "revoked": None}
        policy.check_client(client, "github@personal", "write")
        policy.check_client(client, "jira@personal", "read")
        with self.assertRaises(GatewayError) as cm:
            policy.check_client(client, "graph@personal", "read")
        self.assertEqual(cm.exception.code, "client_not_allowed")
        with self.assertRaises(GatewayError) as cm:
            policy.check_client(client, "github@personal", "consent")
        self.assertEqual(cm.exception.code, "tier_ceiling")
        policy.check_client(dict(client, allow=["*"], max_tier="consent"), "x@y", "consent")


# --------------------------------------------------------------------------------- audit

class TestAudit(TmpBase):
    def test_appends_jsonl_mode_600(self):
        home = self.tmp / "home"
        audit.record(home, client="local", connection="c@personal", op="list_pulls",
                     tier="read", tool="call_read", verdict="ok", status=200,
                     args_sha256="ab" * 32)
        audit.record(home, client="local", connection="c@personal", op="x",
                     tier="write", tool="call_write", verdict="error", status=500,
                     duration_ms=12)
        path = home / "state" / "audit.jsonl"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        first, second = (json.loads(l) for l in lines)
        self.assertEqual(list(first)[:len(audit.FIELDS)], list(audit.FIELDS))
        self.assertRegex(first["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(first["args_sha256"], "ab" * 32)
        self.assertIsNone(second["args_sha256"])
        self.assertEqual(second["duration_ms"], 12)

    def test_tightens_existing_loose_file(self):
        home = self.tmp / "home"
        (home / "state").mkdir(parents=True)
        p = home / "state" / "audit.jsonl"
        p.write_text("")
        os.chmod(p, 0o644)
        audit.record(home, op="x")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_op_dict_reduced_to_identity(self):
        home = self.tmp / "home"
        audit.record(home, op={"name": None, "method": "GET", "path": "/x",
                               "query": {"q": "secret-ish"}})
        rec = json.loads((home / "state" / "audit.jsonl").read_text())
        self.assertEqual(rec["op"], "GET /x")
        self.assertNotIn("secret-ish", json.dumps(rec))


if __name__ == "__main__":
    unittest.main()
