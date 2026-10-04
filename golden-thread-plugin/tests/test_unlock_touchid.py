"""gt unlock, the Touch ID factor (0.20.0): the gt-presence helper and gt_unlock_touchid.py.

The helper is BUILT from the shipped Swift source into a temp dir, then exercised with
NON-BIOMETRIC Secure Enclave keys (TouchIdFactor(insecure_test_keys=True)), so no test ever
raises a Touch ID prompt -- the helper also marks the LAContext interactionNotAllowed on that
path, so a biometric key slipped in by mistake fails instead of prompting. No test creates or
uses a biometric key.

Pins the design's Touch ID acceptance items (design-unlock.md §2 Touch ID, Phase 2):
  * presence is a signature verified in Python under the enrolled key over exactly the
    challenge; a forged "ok" helper, or a signature over anything else, fails;
  * a REAL Secure Enclave DER signature verifies with the pure-Python es256_verify (the first
    cross-check of the verifier against hardware output);
  * sealing round-trips; a damaged box fails; a missing helper is unavailable, not passed.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from _harness import PYTHON, SCRIPTS, rmtree

sys.path.insert(0, str(SCRIPTS))
import gt_unlock_crypto as C   # noqa: E402
import gt_unlock_factors as F  # noqa: E402

IS_MAC = sys.platform == "darwin"
if IS_MAC:
    import gt_unlock_touchid as T  # noqa: E402

_STATE = {}


def _skip_reason():
    if not IS_MAC:
        return "Touch ID is macOS only"
    if not shutil.which("swiftc"):
        return "swiftc is not installed (Xcode Command Line Tools), so the helper cannot be built"
    return None


def setUpModule():
    if _skip_reason():
        return
    d = tempfile.mkdtemp(prefix="gt-touchid-test.")
    _STATE["dir"] = d
    try:
        _STATE["helper"] = T.build(os.path.join(d, "bin"))
    except T.BuildError as e:
        _STATE["build_error"] = str(e)
        return
    r = subprocess.run([_STATE["helper"], "available"], input=b"{}", stdout=subprocess.PIPE,
                       timeout=30)
    try:
        _STATE["se"] = json.loads(r.stdout.decode()).get("secure_enclave") is True
    except ValueError:
        _STATE["se"] = False


def tearDownModule():
    if _STATE.get("dir"):
        rmtree(_STATE["dir"])


def ctx(reason="gt test: no prompt is raised"):
    return F.Context(home=_STATE["dir"], reason=reason)


def write_fake(name, body):
    """A stand-in helper: a shell script that answers whatever it is told to."""
    path = os.path.join(_STATE["dir"], name)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/bin/sh\ncat >/dev/null\n" + body + "\n")
    os.chmod(path, 0o755)
    # Recorded, as install records a helper it built (0.20.0 review): these tests are about
    # what the helper SAYS, so it must get past the install-record check first.
    T.record_helper(path)
    return path


class HelperCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        why = _skip_reason()
        if why:
            raise unittest.SkipTest(why)
        if "build_error" in _STATE:
            raise unittest.SkipTest("the helper did not build here: " + _STATE["build_error"])
        if not _STATE.get("se"):
            raise unittest.SkipTest("this Mac has no Secure Enclave")
        cls.fac = T.TouchIdFactor(helper=_STATE["helper"], insecure_test_keys=True)
        cls.rec = cls.fac.enroll(ctx())


class SignAndVerify(HelperCase):
    def test_enrolment_is_public_handles_and_two_distinct_keys(self):
        r = self.rec
        for k in ("public", "blob", "agree_public", "agree_blob", "helper"):
            self.assertIn(k, r)
        self.assertEqual(len(base64.b64decode(r["public"])), 65)
        self.assertEqual(len(base64.b64decode(r["agree_public"])), 65)
        self.assertNotEqual(r["public"], r["agree_public"])
        self.assertFalse(r["biometric"], "the test factor must mark its keys non-biometric")
        self.assertEqual(r["helper"], _STATE["helper"])

    def test_prove_verifies_a_real_secure_enclave_signature(self):
        ch = os.urandom(32)
        sig = self.fac.prove(self.rec, ch, ctx())
        # prove() returns the verified signature (kept for the policy approval) ...
        self.assertTrue(self.fac.verify(self.rec, ch, sig))
        # ... which verifies over exactly that challenge and nothing else
        self.assertFalse(self.fac.verify(self.rec, os.urandom(32), sig))

    def test_es256_verify_accepts_real_se_der_and_rejects_other_messages(self):
        """First cross-check of the pure-Python verifier against hardware output."""
        ch = os.urandom(32)
        out = self.fac._call("sign", {"blob": self.rec["blob"], "challenge": T._b64(ch),
                                      "reason": "t", "biometric": False})
        sig = base64.b64decode(out["signature"])
        self.assertEqual(sig[0], 0x30, "the helper must return DER")
        pub = base64.b64decode(self.rec["public"])
        self.assertTrue(C.es256_verify(pub, ch, sig))
        self.assertFalse(C.es256_verify(pub, os.urandom(32), sig))
        bad = bytearray(sig)
        bad[-1] ^= 1
        self.assertFalse(C.es256_verify(pub, ch, bytes(bad)))
        other = base64.b64decode(self.rec["agree_public"])
        self.assertFalse(C.es256_verify(other, ch, sig))

    def test_signature_over_another_challenge_fails(self):
        ch = os.urandom(32)
        out = self.fac._call("sign", {"blob": self.rec["blob"], "challenge": T._b64(ch),
                                      "reason": "t", "biometric": False})
        replay = write_fake("replay", "printf '%s\\n' '" + json.dumps(out) + "'")
        fake = T.TouchIdFactor(helper=replay, insecure_test_keys=True)
        with self.assertRaises(F.FactorError) as cm:
            fake.prove(self.rec, os.urandom(32), ctx())
        self.assertEqual(cm.exception.code, "wrong")

    def test_forged_ok_helper_fails(self):
        for body, code in (("echo '{\"ok\": true}'", "wrong"),
                           ("echo '{\"signature\": \"MEUCIQ==\"}'", "wrong"),
                           ("echo 'yes'", "helper_failed")):
            fake = T.TouchIdFactor(helper=write_fake("forged", body), insecure_test_keys=True)
            with self.assertRaises(F.FactorError) as cm:
                fake.prove(self.rec, os.urandom(32), ctx())
            self.assertEqual(cm.exception.code, code, body)

    def test_valid_looking_signature_from_another_key_fails(self):
        other = self.fac.enroll(ctx())
        ch = os.urandom(32)
        out = self.fac._call("sign", {"blob": other["blob"], "challenge": T._b64(ch),
                                      "reason": "t", "biometric": False})
        fake = T.TouchIdFactor(helper=write_fake("otherkey", "printf '%s\\n' '"
                                                 + json.dumps(out) + "'"),
                               insecure_test_keys=True)
        with self.assertRaises(F.FactorError) as cm:
            fake.prove(self.rec, ch, ctx())
        self.assertEqual(cm.exception.code, "wrong")

    def test_helper_error_codes_pass_through_and_unknown_codes_do_not(self):
        f = T.TouchIdFactor(helper=write_fake("cancel", "echo '{\"error\":{\"code\":\"cancelled\""
                                              ",\"message\":\"x\"}}'; exit 1"),
                            insecure_test_keys=True)
        with self.assertRaises(F.FactorError) as cm:
            f.prove(self.rec, os.urandom(32), ctx())
        self.assertEqual(cm.exception.code, "cancelled")
        f = T.TouchIdFactor(helper=write_fake("weird", "echo '{\"error\":{\"code\":\"granted\"}}'"
                                              "; exit 1"), insecure_test_keys=True)
        with self.assertRaises(F.FactorError) as cm:
            f.prove(self.rec, os.urandom(32), ctx())
        self.assertEqual(cm.exception.code, "helper_failed")

    def test_challenge_must_be_32_bytes(self):
        with self.assertRaises(F.FactorError) as cm:
            self.fac.prove(self.rec, b"short", ctx())
        self.assertEqual(cm.exception.code, "malformed")


class Sealing(HelperCase):
    def test_round_trip(self):
        secret = b"gt-test-value \x00\xff" + os.urandom(40)
        box = self.fac.seal(self.rec, secret)
        self.assertNotIn(secret, box)
        self.assertEqual(self.fac.unseal(self.rec, box, ctx()), secret)
        self.assertNotEqual(box, self.fac.seal(self.rec, secret), "each seal is fresh (ECIES)")

    def test_corrupt_box_fails(self):
        box = bytearray(self.fac.seal(self.rec, b"value"))
        for i in (len(box) - 1, 70, 1):
            bad = bytearray(box)
            bad[i] ^= 1
            with self.assertRaises(F.FactorError) as cm:
                self.fac.unseal(self.rec, bytes(bad), ctx())
            self.assertIn(cm.exception.code, ("malformed", "helper_failed"), i)
        with self.assertRaises(F.FactorError):
            self.fac.unseal(self.rec, bytes(box[:40]), ctx())

    def test_box_for_another_key_does_not_open(self):
        other = self.fac.enroll(ctx())
        box = self.fac.seal(other, b"value")
        with self.assertRaises(F.FactorError) as cm:
            self.fac.unseal(self.rec, box, ctx())
        self.assertEqual(cm.exception.code, "malformed")


@unittest.skipUnless(IS_MAC, "Touch ID is macOS only")
class Availability(unittest.TestCase):
    def test_missing_helper_is_unavailable(self):
        f = T.TouchIdFactor(helper="/nonexistent/gt-presence")
        ok, why = f.available()
        self.assertFalse(ok)
        self.assertIn("helper missing", why)
        with self.assertRaises(F.FactorError) as cm:
            f.prove({"public": T._b64(b"\x04" * 65), "blob": "AAAA"}, os.urandom(32), ctx())
        self.assertEqual(cm.exception.code, "unavailable")

    def test_relative_or_group_writable_helper_is_refused(self):
        d = tempfile.mkdtemp(prefix="gt-touchid-perm.")
        try:
            p = os.path.join(d, "gt-presence")
            with open(p, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("#!/bin/sh\necho '{}'\n")
            os.chmod(p, 0o775)
            self.assertFalse(T.TouchIdFactor(helper=p).available()[0])
            self.assertFalse(T.TouchIdFactor(helper="gt-presence").available()[0])
        finally:
            rmtree(d)

    def test_production_factor_refuses_a_non_biometric_record(self):
        """No helper is called: the refusal comes first, so this can never prompt."""
        f = T.TouchIdFactor(helper="/nonexistent/gt-presence")
        rec = {"public": "x", "blob": "y", "agree_blob": "z", "biometric": False}
        for call in (lambda: f.prove(rec, os.urandom(32), ctx()),
                     lambda: f.unseal(rec, b"\x04" * 100, ctx())):
            with self.assertRaises(F.FactorError) as cm:
                call()
            self.assertEqual(cm.exception.code, "malformed")

    def test_registry_builds_the_production_factor(self):
        fac = F.registry()["touchid"]
        self.assertIsInstance(fac, T.TouchIdFactor)
        self.assertFalse(fac._test, "registry must never construct a test-key factor")


@unittest.skipUnless(IS_MAC, "Touch ID is macOS only")
class BuildCli(unittest.TestCase):
    def test_refuses_cleanly_without_swiftc(self):
        d = tempfile.mkdtemp(prefix="gt-touchid-build.")
        try:
            env = dict(os.environ, PATH=d)
            r = subprocess.run([PYTHON, str(SCRIPTS / "gt_unlock_touchid.py"), "build",
                                "--dest", os.path.join(d, "bin")], capture_output=True,
                               text=True, env=env, timeout=60)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("Xcode Command Line Tools", r.stderr)
            self.assertFalse(os.path.exists(os.path.join(d, "bin", "gt-presence")))
        finally:
            rmtree(d)

    def test_build_records_the_built_binary(self):
        why = _skip_reason()
        if why:
            self.skipTest(why)
        if "build_error" in _STATE:
            self.skipTest("the helper did not build here: " + _STATE["build_error"])
        self.assertIsNone(T.helper_problem(_STATE["helper"]))
        r = subprocess.run([PYTHON, str(SCRIPTS / "gt_unlock_touchid.py"), "verify", "--helper",
                            _STATE["helper"]], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_build_cli_produces_a_signed_helper(self):
        why = _skip_reason()
        if why:
            self.skipTest(why)
        if "build_error" in _STATE:
            self.skipTest("the helper did not build here: " + _STATE["build_error"])
        # setUpModule already built through build(); check the result is signed and answers.
        r = subprocess.run(["/usr/bin/codesign", "-v", _STATE["helper"]], capture_output=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([_STATE["helper"], "nonsense"], input=b"", capture_output=True,
                           timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout.decode())["error"]["code"], "malformed")
        self.assertEqual(r.stderr, b"")


if __name__ == "__main__":
    unittest.main()
