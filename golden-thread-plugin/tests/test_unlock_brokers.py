"""gt_unlock_brokers -- bring-your-own secret stores behind the unlock grant (0.20.0).

The broker is called only by the unlock authority, in process, so it is tested in process. Every
store CLI is a FAKE Python program wired in through TOOL_OVERRIDE (an in-process seam with no
environment variable), so nothing here touches a real keychain, 1Password, Bitwarden or Vault.
The rule under test everywhere: an error names the REF, never the value.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from _harness import IS_WINDOWS, PYTHON, SCRIPTS, WIN_MODE_BITS, rmtree

sys.path.insert(0, str(SCRIPTS))
import gt_unlock_brokers as B     # noqa: E402

VALUE = "s3cr3t-VALUE-never-in-an-error"


class BrokerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gtb-")
        self.addCleanup(rmtree, self.tmp)
        self.addCleanup(B.TOOL_OVERRIDE.clear)
        B.TOOL_OVERRIDE.clear()
        env = mock.patch.dict(os.environ, {"LOTR_STORE_DIR": os.path.join(self.tmp, "store")})
        env.start()
        self.addCleanup(env.stop)
        for k in ("SOPS_AGE_KEY_FILE", "SOPS_AGE_KEY", "VAULT_TOKEN", "BW_SESSION"):
            os.environ.pop(k, None)

    def fake(self, name, stdout=VALUE, rc=0, stderr=None, sleep=0):
        """A fake store CLI: records its argv, prints `stdout`, exits `rc`."""
        log = os.path.join(self.tmp, name + ".argv")
        script = os.path.join(self.tmp, "fake_%s.py" % name)
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write("import json, sys, time\n"
                    "open(%r, 'w').write(json.dumps(sys.argv[1:]))\n"
                    "sys.stdout.write(%r)\n"
                    "sys.stdout.flush()\n"
                    "%s\n"
                    "time.sleep(%r)\n"
                    "sys.exit(%d)\n"
                    % (log, stdout, ("sys.stderr.write(%r)" % stderr) if stderr else "pass",
                       sleep, rc))
        B.TOOL_OVERRIDE[name] = [PYTHON, script]
        return log

    def argv(self, log):
        with open(log, encoding="utf-8") as f:
            return json.load(f)

    def assertRefusal(self, code, ref, fn=B.resolve, secret=VALUE):
        with self.assertRaises(B.BrokerError) as cm:
            fn(ref)
        e = cm.exception
        self.assertEqual(e.code, code, e.message)
        self.assertNotIn(secret, e.message)
        self.assertNotIn(secret, str(e))
        self.assertNotIn(secret, repr(e.args))
        return e

    def secret_file(self, name="tok", text=VALUE + "\n", mode=0o600):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(p, mode)
        return p


class FileAndStore(BrokerCase):
    def test_owner_only_file_resolves_and_the_newline_is_dropped(self):
        self.assertEqual(B.resolve("file:" + self.secret_file()), VALUE)

    @unittest.skipIf(IS_WINDOWS, WIN_MODE_BITS)
    def test_group_readable_file_is_refused_naming_the_ref_not_the_value(self):
        p = self.secret_file(mode=0o644)
        e = self.assertRefusal("perms", "file:" + p)
        self.assertIn(p, e.message)

    def test_json_field(self):
        p = self.secret_file("t.json", json.dumps({"access_token": VALUE, "other": "x"}))
        self.assertEqual(B.resolve("file:%s#access_token" % p), VALUE)
        self.assertRefusal("missing", "file:%s#nope" % p)

    def test_relative_file_and_store_traversal_are_refused(self):
        self.assertRefusal("ref_invalid", "file:relative/path")
        self.assertRefusal("ref_invalid", "store:../escape")

    def test_store_resolves_under_the_store_dir(self):
        os.makedirs(os.path.join(self.tmp, "store"))
        p = os.path.join(self.tmp, "store", "jira")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(VALUE)
        os.chmod(p, 0o600)
        self.assertEqual(B.resolve("store:jira"), VALUE)
        d = B.describe("store:jira")
        self.assertEqual((d["level"], d["present"]), ("L1", True))

    def test_missing_file_names_the_ref(self):
        e = self.assertRefusal("missing", "file:" + os.path.join(self.tmp, "absent"))
        self.assertIn("absent", e.message)

    def test_a_string_that_is_not_a_ref_is_never_quoted_back(self):
        for s in ("ghp_" + VALUE, VALUE + ":with-colon", "notascheme:" + VALUE):
            self.assertRefusal("ref_invalid", s, secret=VALUE)

    def test_remove_source_deletes_file_and_store_but_never_a_json_field_file(self):
        p = self.secret_file()
        B.remove_source("file:" + p)
        self.assertFalse(os.path.exists(p))
        j = self.secret_file("t.json", json.dumps({"k": VALUE}))
        self.assertRefusal("refused", "file:%s#k" % j, fn=B.remove_source)
        self.assertTrue(os.path.exists(j))

    def test_describe_is_a_stat_and_never_reads(self):
        p = self.secret_file()
        with mock.patch.object(B, "_read_owner_only", side_effect=AssertionError("read")):
            d = B.describe("file:" + p)
        self.assertEqual((d["scheme"], d["level"], d["present"]), ("file", "L1", True))
        self.assertNotIn(VALUE, json.dumps(d))


class Keychain(BrokerCase):
    def test_resolve_uses_dash_w_and_describe_does_not(self):
        log = self.fake("security")
        self.assertEqual(B.resolve("keychain:gt-lotr/github"), VALUE)
        self.assertEqual(self.argv(log), ["find-generic-password", "-s", "gt-lotr",
                                          "-a", "github", "-w"])
        d = B.describe("keychain:gt-lotr/github")
        self.assertNotIn("-w", self.argv(log))
        self.assertEqual((d["level"], d["present"]), ("L1", True))
        self.assertNotIn(VALUE, json.dumps(d))

    def test_remove_source_deletes_the_item(self):
        log = self.fake("security", stdout="")
        B.remove_source("keychain:svc/acct")
        self.assertEqual(self.argv(log), ["delete-generic-password", "-s", "svc", "-a", "acct"])

    def test_missing_security_binary_is_scheme_unavailable(self):
        with mock.patch.object(B, "SECURITY_BIN", os.path.join(self.tmp, "no-security")):
            e = self.assertRefusal("scheme_unavailable", "keychain:a/b")
        self.assertIn("security", e.message)

    def test_a_failing_lookup_never_echoes_what_the_tool_printed(self):
        self.fake("security", stdout=VALUE, rc=44, stderr=VALUE)
        e = self.assertRefusal("missing", "keychain:a/b")
        self.assertIn("keychain:a/b", e.message)


class ExternalStores(BrokerCase):
    def test_sops_extract_argv(self):
        log = self.fake("sops")
        f = os.path.join(self.tmp, "secrets.enc.yaml")
        self.assertEqual(B.resolve("sops:%s#api_key" % f), VALUE)
        self.assertEqual(self.argv(log), ["-d", "--extract", '["api_key"]', f])
        B.resolve("sops:" + f)
        self.assertEqual(self.argv(log), ["-d", f])

    def test_sops_key_must_be_plain(self):
        self.assertRefusal("ref_invalid", "sops:/x/y.yaml#a\"]; rm")

    def test_op_read(self):
        log = self.fake("op")
        self.assertEqual(B.resolve("op:op://Private/GitHub/token"), VALUE)
        self.assertEqual(self.argv(log), ["read", "--no-newline", "op://Private/GitHub/token"])
        self.assertRefusal("ref_invalid", "op:Private/GitHub/token")

    def test_bw_password_and_fields(self):
        log = self.fake("bw")
        self.assertEqual(B.resolve("bw:abc-123"), VALUE)
        self.assertEqual(self.argv(log), ["get", "password", "abc-123"])
        item = {"login": {"username": "me", "password": "p"}, "notes": "n",
                "fields": [{"name": "api_key", "value": VALUE}]}
        self.fake("bw", stdout=json.dumps(item))
        self.assertEqual(B.resolve("bw:abc-123#api_key"), VALUE)
        self.assertEqual(B.resolve("bw:abc-123#username"), "me")
        e = self.assertRefusal("missing", "bw:abc-123#nope")
        self.assertNotIn('"p"', e.message)

    def test_vault_field(self):
        log = self.fake("vault")
        self.assertEqual(B.resolve("vault:secret/data/ci#token"), VALUE)
        self.assertEqual(self.argv(log), ["kv", "get", "-field=token", "secret/data/ci"])
        self.assertRefusal("ref_invalid", "vault:secret/data/ci")

    def test_failure_and_timeout_never_echo_the_tools_output(self):
        f = "sops:%s" % os.path.join(self.tmp, "x.yaml")
        self.fake("sops", stdout=VALUE, rc=1, stderr=VALUE)
        e = self.assertRefusal("missing", f)
        self.assertIn(f, e.message)
        self.fake("sops", stdout=VALUE, sleep=5)
        with mock.patch.object(B, "TOOL_TIMEOUT_S", 0.5):
            self.assertRefusal("unreadable", f)

    def test_missing_tool_is_scheme_unavailable_naming_it(self):
        empty = os.path.join(self.tmp, "empty-bin")
        os.mkdir(empty)
        with mock.patch.dict(os.environ, {"PATH": empty}):
            for ref, tool in (("sops:/a/b.yaml", "sops"), ("op:op://v/i/f", "op"),
                              ("bw:item1", "bw"), ("vault:kv/x#f", "vault")):
                e = self.assertRefusal("scheme_unavailable", ref)
                self.assertIn("`%s`" % tool, e.message)

    @unittest.skipIf(IS_WINDOWS, "Windows has Credential Manager; tested on Windows below")
    def test_wincred_off_windows_is_scheme_unavailable(self):
        self.assertRefusal("scheme_unavailable", "wincred:gt/test")

    @unittest.skipUnless(IS_WINDOWS, "Credential Manager exists only on Windows")
    def test_wincred_missing_target_is_missing(self):
        self.assertRefusal("missing", "wincred:gt-test-no-such-target-%d" % os.getpid())

    def test_remove_source_refuses_brokered_stores(self):
        for ref in ("op:op://v/i/f", "bw:x", "vault:a#b", "sops:/a.yaml", "wincred:t",
                    "sealed:n"):
            e = self.assertRefusal("refused", ref, fn=B.remove_source)
            self.assertIn("remove it in that store", e.message)

    def test_sealed_is_never_resolved_here(self):
        self.assertRefusal("refused", "sealed:github")


class Levels(BrokerCase):
    KEY_LINE = "AGE-SECRET-KEY-1" + "Q" * 58

    def identity(self, *lines):
        p = os.path.join(self.tmp, "keys.txt")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("# created: 2026-10-03\n# public key: age1example\n" + "\n".join(lines) + "\n")
        os.environ["SOPS_AGE_KEY_FILE"] = p
        return p

    def level(self, ref="sops:/v/s.yaml#k"):
        d = B.describe(ref)
        blob = json.dumps(d)
        for secret in (self.KEY_LINE, "SEKEYMATERIAL", "YKMATERIAL"):
            self.assertNotIn(secret, blob)
        return d["level"]

    def test_plaintext_identity_is_l1(self):
        self.identity(self.KEY_LINE)
        self.assertEqual(self.level(), "L1")
        self.assertEqual(B.age_identity_kind(os.environ["SOPS_AGE_KEY_FILE"])["kind"], "plaintext")

    def test_secure_enclave_identity_is_l2(self):
        self.identity("AGE-PLUGIN-SE-1SEKEYMATERIAL")
        self.assertEqual(self.level(), "L2")

    def test_yubikey_identity_is_l2(self):
        self.identity("AGE-PLUGIN-YUBIKEY-1YKMATERIAL")
        self.assertEqual(self.level(), "L2")

    def test_hardware_beside_a_plaintext_key_is_l1(self):
        self.identity("AGE-PLUGIN-SE-1SEKEYMATERIAL", self.KEY_LINE)
        self.assertEqual(self.level(), "L1")

    def test_unknown_plugin_and_env_key_are_l1(self):
        self.identity("AGE-PLUGIN-OTHER-1XYZ")
        self.assertEqual(self.level(), "L1")
        os.environ["SOPS_AGE_KEY"] = self.KEY_LINE
        try:
            self.assertEqual(self.level(), "L1")
        finally:
            os.environ.pop("SOPS_AGE_KEY")

    def test_kind_never_carries_key_material(self):
        p = self.identity(self.KEY_LINE)
        self.assertNotIn(self.KEY_LINE, json.dumps(B.age_identity_kind(p)))

    def test_fixed_levels(self):
        self.assertEqual(self.level("op:op://v/i/f"), "L2")
        self.assertEqual(self.level("bw:item"), "L1")
        self.assertEqual(self.level("sealed:github"), "L2")
        os.environ["VAULT_TOKEN"] = "x"
        try:
            self.assertEqual(self.level("vault:kv/a#b"), "L1")
        finally:
            os.environ.pop("VAULT_TOKEN")
        self.fake("security", rc=44)
        d = B.describe("keychain:a/b")
        self.assertEqual((d["level"], d["present"]), ("L1", False))
        for scheme in ("file", "store", "keychain", "wincred", "bw"):
            self.assertEqual(B.LEVELS[scheme][0], "L1", scheme)


if __name__ == "__main__":
    unittest.main()
