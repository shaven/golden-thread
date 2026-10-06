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
from _harness import IS_WINDOWS, WIN_MODE_BITS, skip_on_windows


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
from lotrlib import engine as lotr_engine  # noqa: E402
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
        p.write_bytes((value + ("\n" if newline else "")).encode("utf-8"))
        os.chmod(p, mode)
        return p

    def fake_security(self, value=None, exit_code=0):
        """A stand-in for /usr/bin/security that records its argv and prints `value`."""
        log = self.tmp / "security.argv"
        script = self.tmp / "security"
        if IS_WINDOWS:
            # A #! script cannot be exec'd here: the same fake as Python behind a .cmd, which
            # subprocess runs directly (secrets.py calls SECURITY_BIN with an argv, no shell).
            py = self.tmp / "security_fake.py"
            py.write_bytes((
                "import sys\n"
                f"open({str(log)!r}, 'w', encoding='utf-8', newline='\\n')"
                ".write('\\n'.join(sys.argv[1:]) + '\\n')\n"
                "sys.stderr.write('security: some diagnostic\\n')\n"
                + (f"sys.stdout.buffer.write(({value!r} + '\\n').encode())\n"
                   if value is not None else "")
                + f"sys.exit({exit_code})\n").encode("utf-8"))
            cmd = self.tmp / "security.cmd"
            cmd.write_bytes(('@"%s" "%s" %%*\r\n' % (sys.executable, py)).encode("utf-8"))
            secrets.SECURITY_BIN = str(cmd)
            return log
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

    @skip_on_windows(WIN_MODE_BITS)
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
        p.write_bytes(b"abc\n\n")
        os.chmod(p, 0o600)
        self.assertEqual(secrets.resolve(f"file:{p}"), "abc\n")

    @skip_on_windows(WIN_MODE_BITS)
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
        (store / "jira").write_bytes(b"v1\n")
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
            secrets.resolve("nosuch:x")
        self.assertEqual(cm.exception.code, "secret_ref_invalid")
        # 0.3.0: vault: (and sealed: sops: op: bw: wincred:) became brokered schemes, resolved
        # only by gt core's unlock authority (tests/test_lotr_unlock.py).
        self.assertTrue(secrets.brokered("vault:x"))

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
        if not IS_WINDOWS:                                   # WIN_MODE_BITS
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
        for m in ("POST", "PUT", "PATCH"):
            self.assertEqual(policy.classify(c, {"method": m, "path": "/x"}), "write")
        # 0.20.1: DELETE is consent on every connection, not a plain write
        self.assertEqual(policy.classify(c, {"method": "DELETE", "path": "/x"}), "consent")
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

    def _gql(self, q):
        return policy.classify(self.conn(), {"name": "graphql", "method": "POST",
                                             "path": "/graphql", "graphql_query": q})

    def test_graphql_fails_closed_on_hidden_mutations(self):
        write = [
            "fragment F on X {id}\nmutation M { a }",           # fragment-first mutation
            "query A{x} mutation B{y}",                           # query then mutation
            "mutation B{y} query A{x}",
            "query A{x} query B{y}",                              # several operations
            "{ a } { b }",
            "query A{x} {y}",
            "subscription S { a }",
            "fragment F on X {id}",                               # no operation at all
            "# query\nmutation M { a }",                          # comment before it
            "query A{x}\n# }\nmutation M { a }",                  # comment holds a brace
            'query A{x(a:"}")}\nmutation M { a }',               # string holds a brace
            'query A($a:String="\\"") { x }\nmutation M { a }',   # escaped quote in string
            'query A{x(a:"""}\\"""}""")}\nmutation M { a }',      # escaped block-string end
            "query A{x}\r mutation M { a }",                     # bare CR separators
            "\ufeffmutation M { a }",                             # BOM / unicode whitespace
            "query\u00a0A{x}\u00a0mutation M{a}",
            "mut\u0430tion M { a }",                              # homoglyph: not valid GraphQL
            "query A{x} \u2028 mutation M{a}",
            "query A{x",                                          # unbalanced
            'query A{x(a:"oops)}',                                # unterminated string
            "query A{x}}",
            "", "   ", "# only a comment", "{",
            "query A { a } extend schema @x",
            "query A { a } schema { query: Q }",
        ]
        for q in write:
            self.assertEqual(self._gql(q), "write", q)
        self.assertEqual(policy._graphql_tier(None), "write")
        self.assertEqual(policy._graphql_tier(["mutation { a }"]), "write")

    def test_graphql_plain_queries_stay_read(self):
        read = [
            "query { viewer { login } }",
            "{ viewer { login } }",
            "query Q($n: Int = 3, $o: In = {a: [1, 2]}) @d(x: 1) { a(first: $n) { ...F } }\n"
            "fragment F on X { id }",
            "fragment F on X { id }\nquery Q { a { ...F } }",
            "fragment F on X { id } fragment G on X { ...F } { a { ...G } }",
            "query { search(query: \"mutation { x }\") { id } }",       # word in a string
            'query { r(q: "a # not a comment") { id } } # mutation M { a }',
            'query { r(q: """mutation M { a } } {""") { id } }',
            "# mutation M { a }\nquery A { x } # query B { y }",
            "query A {\n  x(a: \"mutation\")\n}\n",
            "query A { x } ,,, ",
        ]
        for q in read:
            self.assertEqual(self._gql(q), "read", q)

    def test_graphql_body_override_is_classified(self):
        """conn_http merges an explicit body over args, so the body's query is what is sent."""
        classify = lotr_engine.Engine._classify
        opd = {"name": "graphql", "method": "POST", "path": "/graphql", "graphql": True}
        c = self.conn()
        ro, mut = "query { viewer { login } }", "mutation { a }"
        self.assertEqual(classify(None, c, opd, {"query": ro}), "read")
        self.assertEqual(classify(None, c, opd, {"query": ro, "body": {"query": mut}}), "write")
        self.assertEqual(classify(None, c, opd, {"query": ro, "body": '{"query": "%s"}' % mut}),
                         "write")
        self.assertEqual(classify(None, c, opd, {"body": {"query": ro}}), "read")
        self.assertEqual(classify(None, c, opd, {"query": {"a": 1}}), "write")

    def _raw(self, method, path, profile="github", **over):
        return policy.classify(dict(self.conn(), profile=profile),
                               dict({"name": None, "method": method, "path": path}, **over))

    def test_consent_twins_of_send_and_merge(self):
        """Raw spellings of what a curated consent op does are consent too (review 0.20.1)."""
        rows = [
            ("graph", "POST", "/users/x@y.com/sendMail"),
            ("graph", "POST", "/me/sendmail/"),
            ("graph", "POST", "/me/messages/AAA/send"),
            ("graph", "POST", "/me/messages/AAA/forward"),
            ("graph", "POST", "/me/messages/AAA/reply"),
            ("graph", "POST", "/me/messages/AAA/replyAll"),
            ("graph", "POST", "/me/messages/AAA/createReply"),
            ("graph", "POST", "/me/messages/AAA/createForward/"),
            ("graph", "POST", "/me/mailFolders/inbox/messages/AAA/send"),
            ("graph", "POST", "/users/u@x.org/messages/AAA/SEND"),
            ("graph", "POST", "/$batch"),
            ("graph", "POST", "/%24batch"),
            ("graph", "POST", "/v1.0/$batch"),
            ("github", "PUT", "/repos/o/r/pulls/1/merge/"),
            ("github", "PUT", "/repos/o/r/pulls/1/MERGE"),
            ("github", "PUT", "/repos/o/r//pulls/1/merge"),
            ("github", "PUT", "/repositories/123/pulls/1/merge"),
            ("github", "PUT", "/repositories/123/pulls/1/merge/"),
            ("github", "PUT", "/repos/o/r/pulls/1/%6Derge"),
            ("github", "DELETE", "/repos/o/r/git/refs/heads/x"),
            ("graph", "DELETE", "/me/messages/AAA"),
            ("jira", "DELETE", "/rest/api/3/issue/X-1"),
            ("generic", "DELETE", "/anything"),
            ("generic", "PUT", "/repos/o/r/pulls/1/merge"),
        ]
        for prof, m, path in rows:
            self.assertEqual(self._raw(m, path, prof), "consent", f"{m} {path}")

    def test_consent_twins_negatives(self):
        for prof, m, path, want in [
            ("graph", "GET", "/me/sendMail", "read"),
            ("graph", "GET", "/me/messages/AAA/send", "read"),
            ("graph", "GET", "/$batch", "read"),
            ("github", "GET", "/repos/o/r/pulls/1/merge", "read"),   # "is it merged?" is a read
            ("github", "HEAD", "/repos/o/r/pulls/1/merge", "read"),
            ("github", "POST", "/repos/o/r/issues/1/comments", "write"),
            ("github", "POST", "/repos/o/r/pulls", "write"),
            ("graph", "POST", "/me/messages", "write"),
            ("graph", "POST", "/me/messages/AAA/move", "write"),
            ("graph", "PATCH", "/me/messages/AAA", "write"),
            ("generic", "POST", "/things", "write"),
            ("generic", "PUT", "/things/1", "write"),
        ]:
            self.assertEqual(self._raw(m, path, prof), want, f"{m} {path}")

    def test_consent_twins_keep_curated_tiers_and_owner_policy(self):
        # an explicit op tier is untouched (MCP ops, curated ops)
        self.assertEqual(self._raw("DELETE", "/x", tier="write"), "write")
        self.assertEqual(self._raw("POST", "/me/sendMail", "graph", tier="consent"), "consent")
        c = dict(self.conn(deny=["DELETE /x*"]), profile="github")
        self.assertEqual(policy.classify(c, {"method": "DELETE", "path": "/xy"}), "deny")
        c = dict(self.conn(), profile="generic")
        c["policy"] = {"consent": [], "write": ["DELETE /scratch/*"], "deny": [], "read": []}
        self.assertEqual(policy.classify(c, {"method": "DELETE", "path": "/scratch/1"}), "write")

    def test_graphql_merge_mutation_is_consent_other_mutations_write(self):
        for q in ["mutation { mergePullRequest(input: {pullRequestId: \"x\"}) { clientMutationId } }",
                  "mutation M { m: mergePullRequest(input: {}) { x } }",
                  "mutation { enablePullRequestAutoMerge(input: {}) { x } }",
                  "fragment F on X {id}\nmutation M { a mergePullRequest(input:{}) {x} }",
                  "mutation { mergePullRequest(input: {}"]:
            self.assertEqual(self._gql(q), "consent", q)
        for q in ["mutation { addComment(input: {body: \"mergePullRequest\"}) { x } }",
                  "mutation { closePullRequest(input: {}) { x } }",
                  "mutation { a } # mergePullRequest"]:
            self.assertEqual(self._gql(q), "write", q)
        self.assertEqual(self._gql("query { mergePullRequest }"), "read")

    def test_allowed_through(self):
        loose = {"call_read": {"read"}, "call_write": {"read", "write"},
                 "call_consent": {"read", "write", "consent"}}
        strict = {"call_read": {"read"}, "call_write": {"write"}, "call_consent": {"consent"}}
        for tool in loose:
            for tier in ("read", "write", "consent", "deny"):
                # strict is the default (0.4.0): a tool carries only its own tier
                self.assertEqual(policy.allowed_through(tool, tier), tier in strict[tool],
                                 (tool, tier))
                self.assertEqual(policy.allowed_through(tool, tier, True), tier in strict[tool])
                self.assertEqual(policy.allowed_through(tool, tier, False), tier in loose[tool],
                                 (tool, tier))
        for s in (True, False):
            self.assertFalse(policy.allowed_through("find", "read", s))
        self.assertTrue(policy.strict_tools({}))
        self.assertTrue(policy.strict_tools({"strict_tools": True}))
        self.assertTrue(policy.strict_tools({"strict_tools": "no"}))    # only exactly false
        self.assertFalse(policy.strict_tools({"strict_tools": False}))

    def test_read_only_posts_are_read_on_their_own_profile_only(self):
        def c(profile, method, path, kind="http"):
            return policy.classify(dict(self.conn(), profile=profile, kind=kind),
                                   {"name": None, "method": method, "path": path})
        for prof, path in [("jira-v3", "/rest/api/3/search"), ("jira-v3", "/rest/api/3/search/jql"),
                           ("jira-v3", "/rest/api/3/search/jql/"),
                           ("jira-v3", "/rest/api/3/search/approximate-count"),
                           ("jira-v2", "/rest/api/2/search"), ("jira-v2", "/rest/api/latest/search"),
                           ("jira-v2", "/REST/api/2/Search"),
                           ("graph", "/search/query"), ("graph", "/v1.0/search/query"),
                           ("graph", "/me/calendar/getSchedule"),
                           ("graph", "/users/a@b.c/findMeetingTimes"),
                           ("graph", "/me/getMemberGroups"), ("graph", "/me/checkMemberGroups")]:
            self.assertEqual(c(prof, "POST", path), "read", f"{prof} POST {path}")
        for prof, method, path, want in [
                ("generic", "POST", "/rest/api/3/search", "write"),     # unknown profile: write
                ("github", "POST", "/search/query", "write"),
                ("graph", "POST", "/rest/api/3/search", "write"),
                ("jira-v3", "POST", "/search/query", "write"),
                ("jira-v3", "POST", "/rest/api/3/issue", "write"),
                ("jira-v3", "POST", "/rest/api/3/search/x", "write"),
                ("jira-v3", "POST", "/rest/api/3/issue/K-1/comment", "write"),
                ("jira-v3", "PUT", "/rest/api/3/search", "write"),
                ("jira-v3", "DELETE", "/rest/api/3/search", "consent"),
                ("graph", "POST", "/me/sendMail", "consent"),
                ("graph", "POST", "/me/messages", "write"),
                ("graph", "POST", "/me/events", "write")]:
            self.assertEqual(c(prof, method, path), want, f"{prof} {method} {path}")
        self.assertEqual(c("jira-v3", "POST", "/rest/api/3/search", kind="mcp"), "write")
        # the owner's policy still wins
        conn = dict(self.conn(deny=["POST /rest/api/3/search"]), profile="jira-v3")
        self.assertEqual(policy.classify(conn, {"method": "POST", "path": "/rest/api/3/search"}),
                         "deny")

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
        if not IS_WINDOWS:                                   # WIN_MODE_BITS
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        first, second = (json.loads(l) for l in lines)
        self.assertEqual(list(first)[:len(audit.FIELDS)], list(audit.FIELDS))
        self.assertRegex(first["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(first["args_sha256"], "ab" * 32)
        self.assertIsNone(second["args_sha256"])
        self.assertEqual(second["duration_ms"], 12)

    @skip_on_windows(WIN_MODE_BITS)
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
