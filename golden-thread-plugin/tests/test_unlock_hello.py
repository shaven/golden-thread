"""gt unlock, the Windows Hello factor (0.20.1): scripts/gt_unlock_hello.py + .ps1.

Everywhere: the factor is driven through a FAKE helper that answers the way
gt_unlock_hello.ps1 does, signing with a throwaway RSA key (tests/_unlock_keys.RSAKey), so
every verification path runs -- an honest signature passes; a wrong challenge, a forged "ok",
a tampered signature and an e=3 Bleichenbacher forgery fail; a randomised (PSS) signer
disables sealing; sealing round-trips through a stand-in DPAPI and fails under another key.
On Windows: the real .ps1 `available` op runs, and real DPAPI (crypt32) round-trips with
entropy and refuses the wrong entropy. A live Hello prompt is never raised here.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import unittest

from _harness import IS_WINDOWS, PYTHON, SCRIPTS
import _unlock_keys as K

sys.path.insert(0, str(SCRIPTS))
import gt_unlock_crypto as C   # noqa: E402
import gt_unlock_factors as F  # noqa: E402
import gt_unlock_hello as H    # noqa: E402


def _tlv(tag, body):
    n = len(body)
    ln = bytes([n]) if n < 0x80 else (lambda lb: bytes([0x80 | len(lb)]) + lb)(
        n.to_bytes((n.bit_length() + 7) // 8, "big"))
    return bytes([tag]) + ln + body


def _uint(v):
    b = v.to_bytes((v.bit_length() + 8) // 8, "big").lstrip(b"\0") or b"\0"
    return b"\0" + b if b[0] & 0x80 else b


def spki(n, e):
    rsapub = _tlv(0x30, _tlv(0x02, _uint(n)) + _tlv(0x02, _uint(e)))
    alg = _tlv(0x30, _tlv(0x06, C._OID_RSA) + b"\x05\x00")
    return _tlv(0x30, alg + _tlv(0x03, b"\x00" + rsapub))


def pss_sign(key, msg, salt_len=32):
    """RSASSA-PSS / SHA-256 / MGF1-SHA-256 signer (randomised salt) for the tests."""
    em_bits = key.n.bit_length() - 1
    em_len = (em_bits + 7) // 8
    salt = secrets.token_bytes(salt_len)
    h = hashlib.sha256(b"\0" * 8 + hashlib.sha256(msg).digest() + salt).digest()
    db = b"\0" * (em_len - salt_len - 32 - 2) + b"\x01" + salt
    masked = bytearray(a ^ b for a, b in zip(db, H._mgf1(h, len(db))))
    masked[0] &= 0xFF >> (8 * em_len - em_bits)
    em = bytes(masked) + h + b"\xbc"
    return pow(int.from_bytes(em, "big"), key.d, key.n).to_bytes(256, "big")


def icbrt(x):
    r = 1 << ((x.bit_length() + 2) // 3)
    while True:
        y = (2 * r + x // (r * r)) // 3
        if y >= r:
            return r
        r = y


def e3_forgery(message):
    """Bleichenbacher 2006: an e=3 key and a signature whose cube BEGINS with a valid
    00 01 FF 00 DigestInfo hash prefix followed by garbage instead of the full FF run."""
    prefix = b"\x00\x01\xff\x00" + C._SHA256_DIGESTINFO + hashlib.sha256(message).digest()
    garbage = 256 - len(prefix)
    target = int.from_bytes(prefix, "big") << (8 * garbage)
    s = icbrt(target) + 1
    assert (s ** 3) >> (8 * garbage) == int.from_bytes(prefix, "big")
    n = (1 << 2047) | secrets.randbits(2047) | 1          # never factored: only verified
    assert s ** 3 < n
    return spki(n, 3), s.to_bytes(256, "big")


class FakeDpapi:
    """DPAPI stand-in: opens only with the entropy it was sealed with."""

    def protect(self, data, entropy):
        pad = hashlib.sha256(b"pad" + entropy).digest()
        ct = bytes(b ^ pad[i % 32] for i, b in enumerate(data))
        return hmac.new(entropy, ct, hashlib.sha256).digest() + ct

    def unprotect(self, blob, entropy):
        mac, ct = blob[:32], blob[32:]
        if not hmac.compare_digest(mac, hmac.new(entropy, ct, hashlib.sha256).digest()):
            raise H.DpapiError("DPAPI failed (Windows error 13)")
        pad = hashlib.sha256(b"pad" + entropy).digest()
        return bytes(b ^ pad[i % 32] for i, b in enumerate(ct))


class FakeHelper:
    """Answers like gt_unlock_hello.ps1. mode: ok | ok_only | other_key | wrong_message |
    tamper | pss | cancel | garbage."""

    def __init__(self, key=None, mode="ok"):
        self.key = key or K.RSAKey("hello")
        self.mode = mode
        self.calls = []

    def __call__(self, op, payload):
        self.calls.append((op, dict(payload)))
        if op == "available":
            return {"supported": True}
        if op == "create":
            return {"public": base64.b64encode(self.key.spki()).decode()}
        if op != "sign":
            raise AssertionError(op)
        msg = base64.b64decode(payload["challenge"])
        if self.mode == "cancel":
            return H.parse_answer(1, json.dumps({"error": {"code": "cancelled",
                                                           "message": "sign: cancelled"}}).encode())
        if self.mode == "ok_only":
            return {"ok": True}
        if self.mode == "other_key":
            return {"ok": True, "signature": base64.b64encode(K.RSAKey("other").sign(msg)).decode()}
        if self.mode == "wrong_message":
            msg = secrets.token_bytes(32)
        if self.mode == "pss":
            sig = pss_sign(self.key, msg)
        elif self.mode == "garbage":
            sig = secrets.token_bytes(256)
        else:
            sig = self.key.sign(msg)
        if self.mode == "tamper":
            sig = sig[:-1] + bytes([sig[-1] ^ 1])
        return {"signature": base64.b64encode(sig).decode()}

    def signed(self):
        return [base64.b64decode(p["challenge"]) for op, p in self.calls if op == "sign"]


CTX = F.Context(home=None, reason="test: unlock gt:secrets for pid 1")


def enrolled(mode="ok"):
    helper = FakeHelper()
    fac = H.HelloFactor(runner=helper, dpapi=FakeDpapi())
    rec = fac.enroll(CTX)
    helper.mode = mode
    return fac, rec, helper


class HelloProve(unittest.TestCase):
    """INVARIANT I1: True only after a Python verification against the enrolled key."""

    def test_enrol_records_public_data_only(self):
        fac, rec, helper = enrolled()
        self.assertEqual(sorted(rec), ["key_name", "public", "scheme", "seal_capable",
                                       "seal_salt"])
        self.assertEqual(base64.b64decode(rec["public"]), helper.key.spki())
        self.assertEqual((rec["scheme"], rec["seal_capable"], rec["key_name"]),
                         ("pkcs1v15", True, "gt-unlock"))
        self.assertRegex(rec["seal_salt"], r"^[0-9a-f]{32}$")
        # the determinism check signed the seal message twice; neither signature is stored
        self.assertEqual(helper.signed(), [H.seal_message(rec["seal_salt"])] * 2)
        self.assertNotIn("signature", json.dumps(rec))

    def test_honest_signature_verifies(self):
        fac, rec, helper = enrolled()
        ch = secrets.token_bytes(32)
        sig = fac.prove(rec, ch, CTX)
        self.assertTrue(fac.verify(rec, ch, sig))        # the verified signature comes back
        self.assertFalse(fac.verify(rec, secrets.token_bytes(32), sig))
        self.assertEqual(helper.signed()[-1], ch)        # signed EXACTLY the challenge
        self.assertEqual(helper.calls[-1][1]["name"], "gt-unlock")

    def _fails(self, mode, code="wrong"):
        fac, rec, _ = enrolled(mode)
        with self.assertRaises(F.FactorError) as cm:
            fac.prove(rec, secrets.token_bytes(32), CTX)
        self.assertEqual(cm.exception.code, code, cm.exception.message)

    def test_signature_over_another_challenge_fails(self):
        self._fails("wrong_message")

    def test_forged_ok_without_signature_fails(self):
        self._fails("ok_only", "malformed")

    def test_forged_ok_with_another_keys_signature_fails(self):
        self._fails("other_key")

    def test_tampered_signature_fails(self):
        self._fails("tamper")

    def test_random_bytes_fail(self):
        self._fails("garbage")

    def test_cancel_is_reported_as_cancelled(self):
        self._fails("cancel", "cancelled")

    def test_e3_bleichenbacher_forgery_fails(self):
        ch = secrets.token_bytes(32)
        pub, forged = e3_forgery(ch)
        # the forgery is real: a verifier that parsed the padding would accept it
        em = pow(int.from_bytes(forged, "big"), 3, C.rsa_public(pub)[0]).to_bytes(256, "big")
        self.assertTrue(em.startswith(b"\x00\x01\xff\x00" + C._SHA256_DIGESTINFO
                                      + hashlib.sha256(ch).digest()))
        rec = {"public": base64.b64encode(pub).decode(), "key_name": "gt-unlock",
               "scheme": "pkcs1v15"}
        fac = H.HelloFactor(runner=lambda op, p: {"signature": base64.b64encode(forged).decode()})
        with self.assertRaises(F.FactorError) as cm:
            fac.prove(rec, ch, CTX)
        self.assertEqual(cm.exception.code, "wrong")
        rec["scheme"] = "pss"
        with self.assertRaises(F.FactorError):
            fac.prove(rec, ch, CTX)

    def test_challenge_must_be_32_bytes(self):
        fac, rec, helper = enrolled()
        n = len(helper.calls)
        for bad in (b"", secrets.token_bytes(31), secrets.token_bytes(33), "x" * 32):
            with self.assertRaises(F.FactorError) as cm:
                fac.prove(rec, bad, CTX)
            self.assertEqual(cm.exception.code, "malformed")
        self.assertEqual(len(helper.calls), n)            # refused before the helper ran

    def test_damaged_record_is_not_enrolled(self):
        fac, rec, _ = enrolled()
        for bad in ({}, dict(rec, public="!!"), dict(rec, key_name="a/b"),
                    dict(rec, public=base64.b64encode(b"\x30\x00").decode())):
            with self.assertRaises(F.FactorError) as cm:
                fac.prove(bad, secrets.token_bytes(32), CTX)
            self.assertEqual(cm.exception.code, "not_enrolled")

    def test_unknown_scheme_never_verifies(self):
        fac, rec, _ = enrolled()
        with self.assertRaises(F.FactorError):
            fac.prove(dict(rec, scheme="none"), secrets.token_bytes(32), CTX)

    def test_pkcs1_record_refuses_a_pss_signature(self):
        """The scheme is pinned at enrolment: a valid PSS signature is no proof for a
        PKCS#1 v1.5 record (no downgrade/upgrade by the helper)."""
        self._fails("pss")


class HelloPss(unittest.TestCase):
    """A randomised (PSS) signer: proofs still verify (strict RFC 8017 9.1.2), sealing off."""

    def test_pss_signer_enrols_without_sealing(self):
        helper = FakeHelper(mode="pss")
        fac = H.HelloFactor(runner=helper, dpapi=FakeDpapi())
        rec = fac.enroll(CTX)
        self.assertEqual((rec["scheme"], rec["seal_capable"]), ("pss", False))
        self.assertIn("randomised", rec["seal_note"])
        ch = secrets.token_bytes(32)
        self.assertTrue(fac.verify(rec, ch, fac.prove(rec, ch, CTX)))
        with self.assertRaises(F.FactorError) as cm:
            fac.seal(rec, b"secret")
        self.assertEqual(cm.exception.code, "unavailable")
        self.assertIn("randomised", cm.exception.message)

    def test_pss_verify_is_strict(self):
        key = K.RSAKey("hello")
        pub = key.spki()
        msg = b"m" * 32
        sig = pss_sign(key, msg)
        self.assertTrue(H.pss_verify(pub, msg, sig))
        self.assertFalse(H.pss_verify(pub, b"n" * 32, sig))
        self.assertFalse(H.pss_verify(pub, msg, sig[:-1] + bytes([sig[-1] ^ 1])))
        self.assertFalse(H.pss_verify(pub, msg, pss_sign(key, msg, salt_len=20)))  # pinned
        self.assertFalse(H.pss_verify(pub, msg, sig[1:]))
        self.assertFalse(H.pss_verify(pub, msg, b"\xff" * 256))       # s >= n
        self.assertFalse(H.pss_verify(spki(key.n, key.e)[:-1], msg, sig))
        self.assertFalse(H.pss_verify(pub, msg, key.sign(msg)))       # PKCS#1 v1.5 is not PSS


class HelloSeal(unittest.TestCase):
    """Sealing: DPAPI entropy = HKDF(verified Hello signature over the record's salt)."""

    def test_round_trip(self):
        fac, rec, helper = enrolled()
        blob = fac.seal(rec, b"ghp_example")
        self.assertTrue(blob.startswith(H.SEAL_MAGIC))
        self.assertNotIn(b"ghp_example", blob)
        self.assertEqual(fac.unseal(rec, blob, CTX), b"ghp_example")
        # the seal message is never a 32-byte prove() challenge (I3)
        self.assertNotEqual(len(H.seal_message(rec["seal_salt"])), 32)

    def test_another_key_cannot_open(self):
        fac, rec, _ = enrolled()
        blob = fac.seal(rec, b"value")
        other = FakeHelper(key=K.RSAKey("other"))
        fac2 = H.HelloFactor(runner=other, dpapi=FakeDpapi())
        rec2 = fac2.enroll(CTX)
        with self.assertRaises(F.FactorError) as cm:
            fac2.unseal(dict(rec2, seal_salt=rec["seal_salt"]), blob, CTX)
        self.assertEqual(cm.exception.code, "wrong")

    def test_another_salt_cannot_open(self):
        fac, rec, _ = enrolled()
        blob = fac.seal(rec, b"value")
        with self.assertRaises(F.FactorError) as cm:
            fac.unseal(dict(rec, seal_salt="0" * 32), blob, CTX)
        self.assertEqual(cm.exception.code, "wrong")

    def test_unverified_sealing_signature_is_refused(self):
        """I4: a helper that hands back bytes it chose cannot make gt seal under a key the
        attacker knows."""
        for mode in ("garbage", "other_key", "tamper", "pss"):
            fac, rec, _ = enrolled(mode)
            with self.assertRaises(F.FactorError) as cm:
                fac.seal(rec, b"value")
            self.assertIn(cm.exception.code, ("wrong", "malformed"), mode)

    def test_not_a_hello_blob(self):
        fac, rec, helper = enrolled()
        n = len(helper.calls)
        with self.assertRaises(F.FactorError) as cm:
            fac.unseal(rec, b"something else", CTX)
        self.assertEqual(cm.exception.code, "malformed")
        self.assertEqual(len(helper.calls), n)

    def test_cancelled_unseal(self):
        fac, rec, helper = enrolled()
        blob = fac.seal(rec, b"value")
        helper.mode = "cancel"
        with self.assertRaises(F.FactorError) as cm:
            fac.unseal(rec, blob, CTX)
        self.assertEqual(cm.exception.code, "cancelled")

    @unittest.skipIf(IS_WINDOWS, "off-Windows path: real DPAPI exists on Windows")
    def test_no_dpapi_off_windows(self):
        helper = FakeHelper()
        fac = H.HelloFactor(runner=helper)
        rec = fac.enroll(CTX)
        with self.assertRaises(F.FactorError) as cm:
            fac.seal(rec, b"value")
        self.assertEqual(cm.exception.code, "unavailable")


class HelloHelperPlumbing(unittest.TestCase):

    def test_error_answers_map_to_factor_codes(self):
        for code in ("cancelled", "not_enrolled", "locked_out", "timeout", "unavailable"):
            out = json.dumps({"error": {"code": code, "message": "m"}}).encode()
            with self.assertRaises(F.FactorError) as cm:
                H.parse_answer(1, out)
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(F.FactorError) as cm:
            H.parse_answer(1, b'{"error":{"code":"ok","message":"m"}}')
        self.assertEqual(cm.exception.code, "helper_failed")   # unknown code is never "ok"
        for rc, out in ((1, b""), (0, b"not json"), (0, b"[1]"), (3, b'{"signature":"AA=="}')):
            with self.assertRaises(F.FactorError) as cm:
                H.parse_answer(rc, out, b"boom")
            self.assertEqual(cm.exception.code, "helper_failed")
        self.assertEqual(H.parse_answer(0, b'{"supported": true}\r\n'), {"supported": True})

    def test_unsupported_means_unavailable(self):
        fac = H.HelloFactor(runner=lambda op, p: {"supported": False})
        ok, why = fac.available()
        self.assertFalse(ok)
        self.assertIn("not set up", why)
        self.assertTrue(H.HelloFactor(runner=FakeHelper()).available()[0])

    def test_powershell_by_absolute_path(self):
        p = H.powershell_exe()
        self.assertTrue(p.endswith(os.path.join("System32", "WindowsPowerShell", "v1.0",
                                                "powershell.exe")), p)
        self.assertTrue(os.path.isabs(p) or not IS_WINDOWS)

    def test_ps1_is_ascii_lf(self):
        raw = (SCRIPTS / "gt_unlock_hello.ps1").read_bytes()
        raw.decode("ascii")
        self.assertNotIn(b"\r\n", raw)
        for op in ("'available'", "'create'", "'sign'", "'delete'", "ReplaceExisting",
                   "X509SubjectPublicKeyInfo", "RequestSignAsync"):
            self.assertIn(op.encode(), raw)

    def test_ps1_declares_every_winrt_type_it_uses(self):
        """Live finding 2026-10-03 (gt-win11, owner's PIN): enrolment failed with
        MethodArgumentConversionInvalidCastArgument right after the prompt. Every [Windows.*]
        type the helper names must be loaded explicitly with ContentType = WindowsRuntime, and
        the calls that pass WinRT objects go through reflection (Invoke-WinRT)."""
        import re
        text = (SCRIPTS / "gt_unlock_hello.ps1").read_text(encoding="ascii")
        declared = set(re.findall(r"\[(Windows\.[A-Za-z0-9_.]+), [A-Za-z0-9_.]+, "
                                  r"ContentType = WindowsRuntime\]", text))
        used = set(re.findall(r"\[(Windows\.[A-Za-z0-9_.]+)\]", text))
        self.assertTrue(declared, "no WinRT type is loaded explicitly")
        self.assertEqual(used - declared, set(), "used but never loaded")
        for name in ("KeyCredential", "IBuffer", "CryptographicPublicKeyBlobType"):
            self.assertTrue(any(d.endswith("." + name) for d in declared), name)
        # no WinRT object goes through PowerShell's method binder after the first await
        for call in ("$r.Credential.RetrievePublicKey(", "$r.Credential.RequestSignAsync(",
                     "$Buf::EncodeToBase64String(", "$Buf::DecodeFromBase64String("):
            self.assertNotIn(call, text)
        self.assertIn("'typecheck'", text)
        self.assertIn("trap {", text)
        # PowerShell variables are case-insensitive: assigning $op anywhere would hit the
        # ValidateSet on the -Op parameter (found on gt-win11 by the typecheck run, 2026-10-03)
        self.assertEqual(re.findall(r"(?i)\$op\b(?!\w)(?=\s*=)", text), [])
        self.assertEqual([m for m in re.findall(r"(?i)\$op\b", text) if m != "$Op"], [])

    def test_typecheck_cli_is_offered(self):
        r = subprocess.run([PYTHON, str(SCRIPTS / "gt_unlock_hello.py"), "--help"],
                           capture_output=True, text=True, timeout=60)
        self.assertIn("typecheck", r.stdout)

    @unittest.skipIf(IS_WINDOWS, "POSIX path of the real factor")
    def test_real_factor_is_unavailable_off_windows(self):
        self.assertEqual(H.HelloFactor().available(), (False, "Windows Hello is Windows only"))
        r = subprocess.run([PYTHON, str(SCRIPTS / "gt_unlock_hello.py"), "available"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(json.loads(r.stdout)["ok"], False)


@unittest.skipUnless(IS_WINDOWS, "needs Windows: real powershell.exe / crypt32")
class HelloWindows(unittest.TestCase):

    def test_ps1_available_op_really_runs(self):
        ans = H.run_powershell("available", {}, timeout=90)
        self.assertIsInstance(ans.get("supported"), bool, ans)

    def test_ps1_typecheck_binds_every_type_and_method_without_hello(self):
        """The non-prompting self-check: types, enums, overloads, the AsTask bridges and an
        IBuffer round trip through the reflection path -- no Windows Hello call."""
        ans = H.run_powershell("typecheck", {}, timeout=120)
        self.assertEqual(ans.get("typecheck"), "ok", ans)
        self.assertIn("KeyCredential.RequestSignAsync", ans["methods"])
        self.assertIn("KeyCredential.RetrievePublicKey", ans["methods"])

    def test_ps1_refuses_a_non_base64_challenge_before_hello(self):
        old = os.environ.get("GT_UNLOCK_NO_UI")
        os.environ["GT_UNLOCK_NO_UI"] = "0"
        try:
            with self.assertRaises(F.FactorError) as cm:
                H.run_powershell("sign", {"name": "gt-unlock-no-such-key-test",
                                          "challenge": "!!"}, timeout=90)
        finally:
            os.environ["GT_UNLOCK_NO_UI"] = old if old is not None else "1"
        self.assertEqual(cm.exception.code, "malformed", cm.exception.message)

    def test_ps1_rejects_a_bad_key_name(self):
        # The ps1 refuses the name before any Windows Hello call, so no prompt can follow; the
        # harness's GT_UNLOCK_NO_UI guard is lifted for this one call only to reach the ps1.
        old = os.environ.get("GT_UNLOCK_NO_UI")
        os.environ["GT_UNLOCK_NO_UI"] = "0"
        try:
            with self.assertRaises(F.FactorError) as cm:
                H.run_powershell("sign", {"name": "..\\x y", "challenge": "AA=="}, timeout=90)
        finally:
            os.environ["GT_UNLOCK_NO_UI"] = old if old is not None else "1"
        self.assertEqual(cm.exception.code, "malformed", cm.exception.message)

    def _user_dpapi_or_skip(self):
        d = H.Dpapi()
        try:
            d.protect(b"probe", b"probe")
        except H.DpapiError as e:
            if getattr(e, "winerror", None) == H.ERROR_ACCESS_DENIED:
                self.skipTest("user-scope DPAPI is denied in this logon (an OpenSSH key-auth / "
                              "S4U session never held the password that opens the user's "
                              "master key); run from a console or password logon")
            raise
        return d

    def test_dpapi_plumbing_with_entropy(self):
        """The ctypes CryptProtectData / CryptUnprotectData plumbing, entropy honoured and
        enforced -- machine scope, which every logon can use (test only: gt seals USER-scope)."""
        d = H.Dpapi()
        m = H.CRYPTPROTECT_UI_FORBIDDEN | 0x4                 # CRYPTPROTECT_LOCAL_MACHINE
        ent = secrets.token_bytes(32)
        blob = d._call(d._crypt32.CryptProtectData, b"value \x00\xff", ent, "t", m)
        self.assertNotIn(b"value", blob)
        self.assertEqual(d._call(d._crypt32.CryptUnprotectData, blob, ent, None, m),
                         b"value \x00\xff")
        for wrong in (secrets.token_bytes(32), b""):
            with self.assertRaises(H.DpapiError):
                d._call(d._crypt32.CryptUnprotectData, blob, wrong, None, m)

    def test_dpapi_user_scope_round_trip_and_wrong_entropy(self):
        d = self._user_dpapi_or_skip()
        ent = secrets.token_bytes(32)
        blob = d.protect(b"value \x00\xff", ent)
        self.assertNotIn(b"value", blob)
        self.assertEqual(d.unprotect(blob, ent), b"value \x00\xff")
        with self.assertRaises(H.DpapiError):
            d.unprotect(blob, secrets.token_bytes(32))
        with self.assertRaises(H.DpapiError):
            d.unprotect(blob, b"")
        self.assertEqual(d.unprotect(d.protect(b"", ent), ent), b"")

    def test_seal_through_real_dpapi_with_fake_hello(self):
        helper = FakeHelper()
        fac = H.HelloFactor(runner=helper)
        rec = fac.enroll(CTX)
        try:
            blob = fac.seal(rec, b"value")
        except F.FactorError as e:
            if e.code == "unavailable" and "logon" in e.message:
                self.skipTest("user-scope DPAPI is denied in this logon: " + e.message)
            raise
        self.assertEqual(fac.unseal(rec, blob, CTX), b"value")
        with self.assertRaises(F.FactorError) as cm:
            fac.unseal(dict(rec, seal_salt="1" * 32), blob, CTX)
        self.assertEqual(cm.exception.code, "wrong")

if __name__ == "__main__":
    unittest.main()
