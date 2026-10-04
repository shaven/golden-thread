"""gt unlock, the SSO factor (0.20.1): Microsoft Entra ID, auth code + PKCE with a loopback
redirect, the ID token verified in Python. Runs against tests/_fake_idp.py on 127.0.0.1 -- no
network. The fake browser GETs the authorize URL and follows the redirect to gt's listener.
"""
import contextlib
import hashlib
import hmac
import io
import json
import os
import secrets
import sys
import tempfile
import time
import unittest

from _harness import SCRIPTS, rmtree
import _fake_idp as FI
import _unlock_keys as K

sys.path.insert(0, str(SCRIPTS))
import gt_unlock_crypto as C       # noqa: E402
import gt_unlock_factors as F      # noqa: E402
import gt_unlock_sso as S          # noqa: E402


def b64(obj):
    return C.b64url_encode(json.dumps(obj).encode())


class SsoCase(unittest.TestCase):
    tenant = "organizations"

    def setUp(self):
        self.idp = FI.FakeIdP()
        self.addCleanup(self.idp.close)
        self.tmp = tempfile.mkdtemp(prefix="gt-sso-")
        self.addCleanup(rmtree, self.tmp)
        self.mono = [1000.0]
        self.redirect_status = []
        self.policy = {"sso": {"provider": "entra", "client_id": FI.CLIENT_ID,
                               "tenant": self.tenant, "max_auth_age_s": 300,
                               "flow": "pkce_loopback", "allow_device_code": False}}
        self.fac = self.make()
        self.errors = []

    def make(self, **kw):
        args = dict(authority_host=self.idp.base, open_browser=FI.browser(self.redirect_status),
                    monotonic=lambda: self.mono[0], login_timeout_s=15,
                    policy_loader=lambda: self.policy, _insecure_loopback_idp_for_tests=True)
        args.update(kw)
        return S.SsoFactor(**args)

    def ctx(self):
        return F.Context(self.tmp, "test: unlock lotr", policy=self.policy)

    def enroll(self):
        return self.fac.enroll(self.ctx())

    def prove(self, record, challenge=None):
        return self.fac.prove(record, challenge or secrets.token_bytes(32), self.ctx())

    def refused(self, code, record=None, contains=None):
        record = record if record is not None else self.record
        with self.assertRaises(F.FactorError) as cm:
            self.prove(record)
        self.errors.append(cm.exception)
        self.assertEqual(cm.exception.code, code, cm.exception.message)
        if contains:
            self.assertIn(contains, cm.exception.message)
        return cm.exception


class HappyPath(SsoCase):
    def test_enroll_then_prove(self):
        rec = self.enroll()
        self.assertEqual(rec, {"issuer": self.idp.base + "/" + FI.TID + "/v2.0",
                               "tid": FI.TID, "oid": FI.OID, "sub": self.idp.sub,
                               "client_id": FI.CLIENT_ID, "tenant": "organizations",
                               "enrolled_upn": "owner@example.test"})
        ch = secrets.token_bytes(32)
        self.assertIs(self.fac.prove(rec, ch, self.ctx()), True)
        q = self.idp.last_authorize
        # every unlock is a fresh sign-in, bound to the challenge, minimal scope, PKCE S256
        self.assertEqual(q["prompt"], "login")
        self.assertEqual(q["max_age"], "0")
        self.assertEqual(q["scope"], "openid profile email")
        self.assertEqual(q["nonce"], C.b64url_encode(ch))
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["response_type"], "code")
        self.assertRegex(q["redirect_uri"], r"^http://localhost:\d+$")
        self.assertEqual(q["login_hint"], "owner@example.test")
        self.assertNotIn("client_secret", q)
        self.assertEqual(self.redirect_status[-1], 200)
        # the listener is closed after one answer
        import socket
        port = int(q["redirect_uri"].rsplit(":", 1)[1])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()

    def test_common_tenant_accepts_personal_account(self):
        self.policy["sso"]["tenant"] = "common"
        self.idp.tid = S.MSA_TENANT
        rec = self.enroll()
        self.assertEqual(rec["tid"], S.MSA_TENANT)

    def test_organizations_refuses_personal_account(self):
        self.idp.tid = S.MSA_TENANT
        with self.assertRaises(F.FactorError) as cm:
            self.enroll()
        self.assertEqual(cm.exception.code, "wrong")

    def test_pin_subject(self):
        self.policy["sso"]["pin_subject"] = FI.OID
        self.enroll()
        self.policy["sso"]["pin_subject"] = "someone-else"
        with self.assertRaises(F.FactorError) as cm:
            self.enroll()
        self.assertIn("pinned subject", cm.exception.message)


class Claims(SsoCase):
    def setUp(self):
        super().setUp()
        self.record = self.enroll()

    def hook(self, **changes):
        def h(c):
            c = dict(c)
            for k, v in changes.items():
                if v is None:
                    c.pop(k, None)
                else:
                    c[k] = v
            return c
        self.idp.claims_hook = h

    def test_wrong_nonce(self):
        self.hook(nonce=C.b64url_encode(secrets.token_bytes(32)))
        self.refused("replayed")

    def test_expired(self):
        now = int(time.time())
        self.hook(exp=now - 3600, iat=now - 200, nbf=now - 200)
        self.refused("wrong", contains="expired")

    def test_nbf_in_future(self):
        self.hook(nbf=int(time.time()) + 3600)
        self.refused("wrong", contains="not valid yet")

    def test_nan_time_is_malformed(self):
        self.idp.token_hook = lambda c: _jwt_raw(self.idp.key, "k1",
                                                 json.dumps(dict(c, exp=float("nan"))))
        self.refused("malformed")

    def test_wrong_aud(self):
        self.hook(aud="99999999-2222-3333-4444-555555555555")
        self.refused("wrong", contains="another application")

    def test_wrong_azp(self):
        self.hook(azp="99999999-2222-3333-4444-555555555555")
        self.refused("wrong", contains="authorized party")

    def test_matching_azp_is_fine(self):
        self.hook(azp=FI.CLIENT_ID)
        self.assertTrue(self.prove(self.record))

    def test_wrong_issuer(self):
        other = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.hook(iss=self.idp.base + "/" + other + "/v2.0")
        self.refused("wrong", contains="issuer")

    def test_issuer_on_another_host(self):
        self.hook(iss="https://login.example.test/" + FI.TID + "/v2.0")
        self.refused("wrong", contains="issuer")

    def test_stale_auth_time(self):
        self.hook(auth_time=int(time.time()) - 301 - 5)
        self.refused("wrong", contains="fresh")

    def test_missing_auth_time(self):
        self.hook(auth_time=None)
        self.refused("malformed")

    def test_different_oid_than_enrolled(self):
        self.idp.oid = "0e0e0e0e-1111-2222-3333-444444444444"
        self.refused("wrong", contains="another account")

    def test_different_sub_than_enrolled(self):
        self.idp.sub = "sub-other"
        self.refused("wrong", contains="another account")

    def test_require_mfa_without_amr(self):
        self.policy["sso"]["require_mfa"] = True
        self.refused("wrong", contains="Conditional Access")
        self.hook(amr=["pwd", "mfa"])
        self.assertTrue(self.prove(self.record))

    def test_client_id_changed_since_enrolment(self):
        self.policy["sso"]["client_id"] = "22222222-2222-3333-4444-555555555555"
        self.refused("not_enrolled")

    def test_not_enrolled(self):
        self.refused("not_enrolled", record={})


class PinnedTenant(SsoCase):
    tenant = FI.TID

    def test_happy_and_wrong_tid(self):
        rec = self.enroll()
        self.assertEqual(rec["tenant"], FI.TID)
        other = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"
        # issuer right for the pinned tenant, tid claim another tenant
        self.idp.claims_hook = lambda c: dict(c, tid=other)
        with self.assertRaises(F.FactorError) as cm:
            self.prove(rec)
        self.assertEqual(cm.exception.code, "wrong")
        self.assertIn("another tenant", cm.exception.message)
        # and a token honestly from the other tenant fails on its issuer
        self.idp.claims_hook = None
        self.idp.tid = other
        with self.assertRaises(F.FactorError) as cm:
            self.prove(rec)
        self.assertEqual(cm.exception.code, "wrong")


def _jwt_raw(key, kid, payload_json, alg="RS256"):
    h = b64({"alg": alg, "typ": "JWT", "kid": kid})
    p = C.b64url_encode(payload_json.encode())
    return h + "." + p + "." + C.b64url_encode(key.sign((h + "." + p).encode()))


class Signatures(SsoCase):
    def setUp(self):
        super().setUp()
        self.record = self.enroll()

    def test_alg_none(self):
        def t(c):
            return b64({"alg": "none", "typ": "JWT", "kid": "k1"}) + "." + b64(c) + "."
        self.idp.token_hook = t
        self.refused("wrong", contains="signature")

    def test_hs256_with_the_public_key_as_secret(self):
        def t(c):
            h, p = b64({"alg": "HS256", "typ": "JWT", "kid": "k1"}), b64(c)
            secret = json.dumps(self.idp.keys[0]).encode()
            sig = hmac.new(secret, (h + "." + p).encode(), hashlib.sha256).digest()
            return h + "." + p + "." + C.b64url_encode(sig)
        self.idp.token_hook = t
        self.refused("wrong", contains="signature")

    def test_es256_not_accepted_unless_policy_says(self):
        ec = K.P256Key()
        self.idp.keys.append(ec.jwk("e1"))
        self.mono[0] += 61
        self.idp.token_hook = lambda c: K.jwt(ec, c, kid="e1")
        self.refused("wrong", contains="signature")
        self.policy["sso"]["allow_es256"] = True
        self.assertTrue(self.prove(self.record))

    def test_tampered_signature(self):
        def t(c):
            tok = K.jwt(self.idp.key, c, kid="k1")
            h, p, s = tok.split(".")
            raw = bytearray(C.b64url_decode(s))
            raw[-1] ^= 1
            return h + "." + p + "." + C.b64url_encode(bytes(raw))
        self.idp.token_hook = t
        self.refused("wrong", contains="signature")

    def test_tampered_payload(self):
        def t(c):
            h, _p, s = K.jwt(self.idp.key, c, kid="k1").split(".")
            return h + "." + b64(dict(c, oid=FI.OID)) + "." + s
        self.idp.oid = "0e0e0e0e-1111-2222-3333-444444444444"
        self.idp.token_hook = t
        self.refused("wrong", contains="signature")

    def test_rotation_refetches_once_and_is_rate_limited(self):
        self.assertEqual(self.idp.jwks_fetches, 1)
        k2 = K.RSAKey("rotated")
        self.idp.keys = [k2.jwk("k2")]
        self.idp.key, self.idp.kid = k2, "k2"
        # within the minute: the unknown kid does NOT trigger a refetch
        self.mono[0] += 30
        self.refused("wrong")
        self.assertEqual(self.idp.jwks_fetches, 1)
        # a minute later: one refetch, and the rotated key verifies
        self.mono[0] += 31
        self.assertTrue(self.prove(self.record))
        self.assertEqual(self.idp.jwks_fetches, 2)
        # another unknown kid straight after: no second refetch
        self.idp.kid = "k3"
        self.refused("wrong")
        self.assertEqual(self.idp.jwks_fetches, 2)


class Loopback(SsoCase):
    def test_state_mismatch(self):
        self.idp.state_hook = lambda s: s[:-1] + ("A" if s[-1] != "A" else "B")
        with self.assertRaises(F.FactorError) as cm:
            self.enroll()
        self.assertEqual(cm.exception.code, "wrong")
        self.assertIn("state", cm.exception.message)
        for _ in range(50):                      # the browser thread records the answer
            if self.redirect_status:
                break
            time.sleep(0.1)
        self.assertEqual(self.redirect_status, [400])

    def test_timeout_when_nobody_comes_back(self):
        fac = self.make(open_browser=lambda url: True, login_timeout_s=1)
        t0 = time.monotonic()
        with self.assertRaises(F.FactorError) as cm:
            fac.enroll(self.ctx())
        self.assertEqual(cm.exception.code, "timeout")
        self.assertLess(time.monotonic() - t0, 10)

    def test_no_browser(self):
        fac = self.make(open_browser=lambda url: False)
        with self.assertRaises(F.FactorError) as cm:
            fac.enroll(self.ctx())
        self.assertEqual(cm.exception.code, "unavailable")

    def test_stray_requests_and_wrong_host_are_ignored(self):
        import threading
        import urllib.request
        import urllib.error
        seen = []

        def open_(url):
            port = int(dict(__import__("urllib.parse").parse.parse_qsl(
                url.split("?", 1)[1]))["redirect_uri"].rsplit(":", 1)[1])

            def run():
                for path, host in (("/favicon.ico", None), ("/?code=x&state=y", "evil.test")):
                    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
                    if host:
                        req.add_header("Host", host)
                    try:
                        urllib.request.urlopen(req, timeout=5)
                    except urllib.error.HTTPError as e:
                        seen.append(e.code)
                FI.browser(self.redirect_status)(url)
            threading.Thread(target=run, daemon=True).start()
            return True
        fac = self.make(open_browser=open_)
        rec = fac.enroll(self.ctx())
        self.assertEqual(rec["oid"], FI.OID)
        self.assertEqual(seen, [404, 404])


class Endpoints(SsoCase):
    def test_production_refuses_http(self):
        fac = S.SsoFactor(authority_host=self.idp.base, open_browser=FI.browser(),
                          policy_loader=lambda: self.policy)
        ok, why = fac.available()
        self.assertFalse(ok)
        self.assertIn("https", why)
        with self.assertRaises(F.FactorError) as cm:
            fac.enroll(self.ctx())
        self.assertEqual(cm.exception.code, "unavailable")

    def test_test_flag_still_refuses_non_loopback_http(self):
        fac = S.SsoFactor(authority_host="http://idp.example.test",
                          policy_loader=lambda: self.policy,
                          _insecure_loopback_idp_for_tests=True)
        self.assertFalse(fac.available()[0])

    def test_default_host_is_entra_https(self):
        self.assertEqual(S.SsoFactor().host, "https://login.microsoftonline.com")

    def test_discovery_cannot_move_the_token_endpoint(self):
        self.idp.discovery_hook = lambda d: dict(
            d, token_endpoint="http://127.0.0.2:1/organizations/oauth2/v2.0/token")
        with self.assertRaises(F.FactorError) as cm:
            self.enroll()
        self.assertEqual(cm.exception.code, "helper_failed")

    def test_discovery_with_unexpected_issuer(self):
        self.idp.discovery_hook = lambda d: dict(d, issuer=self.idp.base + "/x/v2.0")
        with self.assertRaises(F.FactorError) as cm:
            self.enroll()
        self.assertEqual(cm.exception.code, "helper_failed")

    def test_available_needs_client_id(self):
        self.policy["sso"]["client_id"] = None
        ok, why = self.make().available()
        self.assertFalse(ok)
        self.assertEqual(why, "no Entra app registration configured (sso.client_id)")
        with self.assertRaises(F.FactorError) as cm:
            self.make().enroll(self.ctx())
        self.assertEqual(cm.exception.code, "unavailable")

    def test_available_with_client_id(self):
        self.assertEqual(self.make().available()[0], True)

    def test_bad_tenant_value(self):
        self.policy["sso"]["tenant"] = "contoso.onmicrosoft.com"
        self.assertFalse(self.make().available()[0])

    def test_device_code_is_not_silently_honoured(self):
        self.policy["sso"]["allow_device_code"] = True
        with self.assertRaises(F.FactorError) as cm:
            self.enroll()
        self.assertEqual(cm.exception.code, "unavailable")

    def test_registry_loads_it(self):
        reg = F.registry()
        self.assertEqual(type(reg["sso"]).__name__, "SsoFactor")


class NoLeaks(SsoCase):
    """Token values never appear in output, logs or exception text; the module writes no
    files and has no print/logging calls."""

    def test_failures_never_carry_token_values(self):
        out, err = io.StringIO(), io.StringIO()
        msgs = []
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rec = self.enroll()
            for hook in (lambda c: dict(c, nonce="x"), lambda c: dict(c, aud="y"),
                         lambda c: dict(c, auth_time=1)):
                self.idp.claims_hook = hook
                try:
                    self.prove(rec)
                except F.FactorError as e:
                    msgs.append("%s %s %r %r" % (e, e.message, e, e.args))
            self.idp.claims_hook = None
            self.idp.token_hook = lambda c: "not.a.jwt" + json.dumps(c)
            try:
                self.prove(rec)
            except F.FactorError as e:
                msgs.append("%s %s %r" % (e, e.message, e.args))
        self.assertEqual(len(msgs), 4)
        blob = "\n".join(msgs) + out.getvalue() + err.getvalue()
        self.assertTrue(self.idp.issued)
        for secret in self.idp.issued:
            for part in secret.split("."):
                if len(part) >= 8:
                    self.assertNotIn(part, blob)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_source_has_no_print_logging_or_file_writes(self):
        src = (SCRIPTS / "gt_unlock_sso.py").read_text(encoding="utf-8")
        import re
        for bad in (r"\bprint\(", r"import logging", r"(?<![.\w])open\(", r"sys\.std(err|out)",
                    r"write_private", r"write_json", r"\.write\("):
            self.assertIsNone(re.search(bad, src), bad)


if __name__ == "__main__":
    unittest.main()
