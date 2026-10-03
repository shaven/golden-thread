#!/usr/bin/env python3
"""gt_unlock_hello -- the Windows Hello factor for gt unlock, and its sealed store (0.20.0).

HOW IT PROVES PRESENCE
  gt_unlock_hello.ps1 (Windows PowerShell 5.1, WinRT KeyCredentialManager) holds nothing: it
  asks Windows to sign with a per-user RSA-2048 key that the TPM keeps and that Windows Hello
  (PIN, face, finger) must unlock for every use. This module chooses the challenge, receives
  the signature, and VERIFIES it here against the public key recorded at enrolment.

  The signature scheme is pinned at enrolment in the record ("scheme"): RSASSA-PKCS1-v1_5 /
  SHA-256 (what KeyCredential.RequestSignAsync is expected to produce) is verified by
  gt_unlock_crypto.rs256_verify; RSASSA-PSS / SHA-256 / MGF1-SHA-256 / 32-byte salt is
  verified by pss_verify below, a strict RFC 8017 9.1.2 check, in case a platform signs PSS.

HOW IT SEALS (design: "Windows: a key derived from a Windows Hello (TPM) signature over a fixed
per-store salt, used as the DPAPI entropy")
  key   = HKDF-SHA256(ikm = Hello signature over b"gt-seal-v1|" + <record seal_salt>)
  blob  = DPAPI CryptProtectData(plaintext, pOptionalEntropy = key, CRYPTPROTECT_UI_FORBIDDEN)
  DPAPI alone opens for any process of the user; with entropy that only a Hello signature
  yields, it does not. This needs a DETERMINISTIC signature (PKCS#1 v1.5 is; PSS is not), so
  enrolment signs the salt twice and records seal_capable=False if the two differ.
  NOTE: unlike Touch ID (which seals to a public key), sealing here ALSO needs the Hello
  prompt, because the entropy is the signature itself.

SECURITY INVARIANTS (each is restated where it is enforced)
  I1  prove() returns True ONLY after a signature over EXACTLY the 32-byte challenge verifies
      in Python against the ENROLLED public key; the helper's exit code or any "ok" is never
      read as proof.
  I2  verification compares full encodings; padding is never parsed (no Bleichenbacher e=3).
  I3  the seal message is never 32 bytes and carries its own prefix, so a prove() challenge
      can never be made to equal it: the unlock flow cannot leak the sealing signature.
  I4  the sealing signature is verified before use: a helper cannot substitute bytes it
      knows and so make gt seal under a key the attacker holds.
  I5  the sealing signature and the derived key never leave this process: not logged, not
      put in argv or env, not written to disk (the signature crosses only the helper's
      stdout pipe).
  I6  powershell.exe is run by absolute path from %SystemRoot%, never resolved through PATH.

usage: gt_unlock_hello.py available
       gt_unlock_hello.py selftest [--no-seal] [--keep]
"""
import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_unlock_crypto as C               # noqa: E402
import gt_unlock_factors as F              # noqa: E402

IS_WINDOWS = os.name == "nt"
PS1 = os.path.join(HERE, "gt_unlock_hello.ps1")
KEY_NAME = "gt-unlock"
SELFTEST_KEY = "gt-unlock-selftest"
KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SEAL_PREFIX = b"gt-seal-v1|"
SEAL_MAGIC = b"gt-hello-seal-v1\0"
CODES = ("cancelled", "timeout", "wrong", "locked_out", "replayed", "unavailable", "malformed",
         "helper_failed", "not_enrolled")
CRYPTPROTECT_UI_FORBIDDEN = 0x1
AVAILABLE_TTL_S = 300


# ---------------------------------------------------------------- the helper

def powershell_exe():
    # I6: absolute path to Windows PowerShell 5.1 (WinRT needs 5.1; PATH could name pwsh or a
    # planted powershell.exe).
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def run_powershell(op, payload, timeout=F.PROMPT_TIMEOUT_S + 15):
    """Run one op of gt_unlock_hello.ps1 -> its JSON answer; a helper error -> FactorError.
    The request travels on stdin (I5: nothing in argv beyond the op name)."""
    if op in ("create", "sign"):
        F.require_ui("Windows Hello")          # never a real Hello prompt under the harness
    argv = [powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", PS1, "-Op", op]
    flags = 0x08000000 if IS_WINDOWS else 0            # CREATE_NO_WINDOW: no console flash
    try:
        r = subprocess.run(argv, input=json.dumps(payload).encode("utf-8"),
                           capture_output=True, timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired:
        raise F.FactorError("timeout", "Windows Hello was not answered in time")
    except OSError as e:
        raise F.FactorError("unavailable", "Windows PowerShell could not be started (%s)"
                            % type(e).__name__)
    return parse_answer(r.returncode, r.stdout, r.stderr)


def parse_answer(returncode, stdout, stderr=b""):
    out = (stdout or b"").decode("utf-8", "replace").strip()
    try:
        ans = json.loads(out.splitlines()[-1]) if out else None
    except ValueError:
        ans = None
    if isinstance(ans, dict) and isinstance(ans.get("error"), dict):
        err = ans["error"]
        code = err.get("code") if err.get("code") in CODES else "helper_failed"
        raise F.FactorError(code, str(err.get("message") or "Windows Hello failed")[:300])
    if returncode != 0 or not isinstance(ans, dict):
        tail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        raise F.FactorError("helper_failed", "the Windows Hello helper failed (exit %s) %s"
                            % (returncode, tail[0][:200]))
    return ans


def _b64(s, what):
    if not isinstance(s, str):
        raise F.FactorError("malformed", "the helper returned no %s" % what)
    try:
        return base64.b64decode(s.encode("ascii"), validate=True)
    except (ValueError, binascii.Error, UnicodeEncodeError):
        raise F.FactorError("malformed", "the helper's %s is not base64" % what)


# ---------------------------------------------------------------- RSASSA-PSS (strict)

def _mgf1(seed, length):
    out, i = b"", 0
    while len(out) < length:
        out += hashlib.sha256(seed + i.to_bytes(4, "big")).digest()
        i += 1
    return out[:length]


def pss_verify(pub, message, signature, salt_len=32):
    """True iff `signature` is RSASSA-PSS (SHA-256, MGF1-SHA-256, salt_len-byte salt) over
    `message`. RFC 8017 9.1.2 in full: every check is made, the salt length is pinned (never
    inferred from the block), and malformed input is False, never an exception."""
    try:
        n, e = C.rsa_public(pub)
    except C.CryptoError:
        return False
    k = (n.bit_length() + 7) // 8
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    m = pow(s, e, n)
    em_bits = n.bit_length() - 1
    em_len = (em_bits + 7) // 8
    if m.bit_length() > em_bits:
        return False
    em = m.to_bytes(em_len, "big")
    h_len = 32
    if em_len < h_len + salt_len + 2 or em[-1] != 0xBC:
        return False
    masked_db, h = em[:em_len - h_len - 1], em[em_len - h_len - 1:-1]
    top = 8 * em_len - em_bits
    if top and masked_db[0] >> (8 - top):
        return False
    db = bytearray(a ^ b for a, b in zip(masked_db, _mgf1(h, em_len - h_len - 1)))
    if top:
        db[0] &= 0xFF >> top
    ps_len = em_len - h_len - salt_len - 2
    if any(db[:ps_len]) or db[ps_len] != 0x01:
        return False
    salt = bytes(db[ps_len + 1:])
    h2 = hashlib.sha256(b"\0" * 8 + hashlib.sha256(message).digest() + salt).digest()
    return hmac.compare_digest(h, h2)


def verify(scheme, pub, message, signature):
    # I2: both paths build/check full encodings; neither parses PKCS#1 v1.5 padding.
    if scheme == "pkcs1v15":
        return C.rs256_verify(pub, message, signature)
    if scheme == "pss":
        return pss_verify(pub, message, signature)
    return False


# ---------------------------------------------------------------- DPAPI

def hkdf_sha256(ikm, salt, info, length=32):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, t, i = b"", b"", 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
        i += 1
    return okm[:length]


class DpapiError(Exception):
    pass


ERROR_ACCESS_DENIED = 5


def _logon_denied(e):
    """User-scope DPAPI refuses (ERROR_ACCESS_DENIED) in a logon that never held the user's
    password -- an OpenSSH key-auth (S4U) session, a task set to "do not store password".
    That is not a wrong key; say what it is."""
    if getattr(e, "winerror", None) == ERROR_ACCESS_DENIED:
        raise F.FactorError("unavailable", "this logon cannot use the user's DPAPI keys (e.g. an "
                                           "SSH key-auth session); seal and unseal from a "
                                           "console or password logon")


class Dpapi:
    """CryptProtectData / CryptUnprotectData (crypt32) with caller entropy, no UI."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ct = ctypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        self._BLOB = BLOB
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        P = ctypes.POINTER(BLOB)
        for fn in (self._crypt32.CryptProtectData, self._crypt32.CryptUnprotectData):
            fn.restype = wintypes.BOOL
        self._crypt32.CryptProtectData.argtypes = [P, wintypes.LPCWSTR, P, ctypes.c_void_p,
                                                   ctypes.c_void_p, wintypes.DWORD, P]
        self._crypt32.CryptUnprotectData.argtypes = [P, ctypes.POINTER(wintypes.LPWSTR), P,
                                                     ctypes.c_void_p, ctypes.c_void_p,
                                                     wintypes.DWORD, P]
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    def _blob(self, data):
        buf = self._ct.create_string_buffer(bytes(data), max(1, len(data)))
        return self._BLOB(len(data), self._ct.cast(buf, self._ct.POINTER(self._ct.c_char))), buf

    def _call(self, fn, data, entropy, middle, flags=CRYPTPROTECT_UI_FORBIDDEN):
        # INVARIANT: protect()/unprotect() never pass flags, so sealing is always USER-scope
        # DPAPI (the machine-scope flag exists only for a plumbing test where an SSH key-auth
        # logon cannot open the user's master key).
        ct = self._ct
        din, _k1 = self._blob(data)
        ent, _k2 = self._blob(entropy)
        out = self._BLOB()
        ok = fn(ct.byref(din), middle, ct.byref(ent), None, None, flags, ct.byref(out))
        if not ok:
            err = ct.get_last_error()
            e = DpapiError("DPAPI failed (Windows error %d)" % err)
            e.winerror = err
            raise e
        try:
            return ct.string_at(out.pbData, out.cbData)
        finally:
            self._kernel32.LocalFree(ct.cast(out.pbData, ct.c_void_p))

    def protect(self, data, entropy):
        return self._call(self._crypt32.CryptProtectData, data, entropy, "gt sealed credential")

    def unprotect(self, blob, entropy):
        return self._call(self._crypt32.CryptUnprotectData, blob, entropy, None)


# ---------------------------------------------------------------- the factor

def seal_message(seal_salt):
    # I3: 11-byte prefix + 32 hex chars = 43 bytes, never a 32-byte challenge.
    return SEAL_PREFIX + seal_salt.encode("ascii")


class HelloFactor(F.Factor):
    name = "hello"
    platform = True

    def __init__(self, runner=None, dpapi=None, key_name=KEY_NAME):
        self._real = runner is None
        self._run = runner or run_powershell
        self._dpapi = dpapi
        self.key_name = key_name
        self._avail = None

    # -- availability
    def available(self):
        if self._real and not IS_WINDOWS:
            return False, "Windows Hello is Windows only"
        if self._real and not os.path.isfile(PS1):
            return False, "gt_unlock_hello.ps1 is not installed beside this module"
        if self._avail and time.monotonic() - self._avail[0] < AVAILABLE_TTL_S:
            return self._avail[1]
        try:
            ans = self._run("available", {})
            res = ((True, "Windows Hello (KeyCredentialManager)") if ans.get("supported") is True
                   else (False, "Windows Hello is not set up for this user (no PIN, face or "
                                "fingerprint), or KeyCredentialManager is unsupported here"))
        except F.FactorError as e:
            res = (False, e.message)
        self._avail = (time.monotonic(), res)
        return res

    # -- signing (helper) + verification (here)
    def _sign(self, key_name, message, reason):
        ans = self._run("sign", {"name": key_name, "reason": reason or "",
                                 "challenge": base64.b64encode(message).decode("ascii")})
        return _b64(ans.get("signature"), "signature")

    @staticmethod
    def _record_key(record):
        try:
            pub = base64.b64decode(record["public"], validate=True)
            C.rsa_public(pub)
        except (KeyError, TypeError, ValueError, binascii.Error, C.CryptoError):
            raise F.FactorError("not_enrolled", "the Windows Hello enrolment record is damaged")
        name = record.get("key_name") or KEY_NAME
        if not KEY_RE.match(name):
            raise F.FactorError("not_enrolled", "the Windows Hello key name is invalid")
        return pub, name

    def enroll(self, ctx):
        """Create (or replace) the Hello key -> the PUBLIC record. Prompts: create, then two
        signatures over the seal salt (the determinism check)."""
        if not KEY_RE.match(self.key_name):
            raise F.FactorError("malformed", "invalid Windows Hello key name")
        ans = self._run("create", {"name": self.key_name, "reason": ctx.reason})
        pub = _b64(ans.get("public"), "public key")
        try:
            C.rsa_public(pub)                 # RSA >= 2048 bits, sane exponent, strict DER
        except C.CryptoError as e:
            raise F.FactorError("malformed", "the Windows Hello public key is unusable: %s" % e)
        seal_salt = secrets.token_hex(16)
        msg = seal_message(seal_salt)
        s1 = self._sign(self.key_name, msg, ctx.reason)
        # The scheme is decided once, here, and pinned: prove() accepts only this scheme.
        if C.rs256_verify(pub, msg, s1):
            scheme = "pkcs1v15"
        elif pss_verify(pub, msg, s1):
            scheme = "pss"
        else:
            raise F.FactorError("wrong", "the new Windows Hello key's signature does not verify")
        s2 = self._sign(self.key_name, msg, ctx.reason)
        if not verify(scheme, pub, msg, s2):
            raise F.FactorError("wrong", "the new Windows Hello key's signature does not verify")
        rec = {"public": base64.b64encode(pub).decode("ascii"), "key_name": self.key_name,
               "scheme": scheme, "seal_salt": seal_salt,
               "seal_capable": hmac.compare_digest(s1, s2)}
        if not rec["seal_capable"]:
            rec["seal_note"] = ("this machine's Windows Hello signatures are randomised (%s), "
                                "so no stable sealing key can be derived from them" % scheme)
        return rec

    def prove(self, record, challenge, ctx):
        if not isinstance(challenge, (bytes, bytearray)) or len(challenge) != 32:
            raise F.FactorError("malformed", "a challenge is 32 bytes")
        pub, name = self._record_key(record)
        sig = self._sign(name, bytes(challenge), getattr(ctx, "reason", ""))
        # I1: True only if the signature verifies over exactly `challenge` under the enrolled
        # key, with the scheme pinned at enrolment. Nothing the helper says is read as proof.
        if not verify(record.get("scheme") or "pkcs1v15", pub, bytes(challenge), sig):
            raise F.FactorError("wrong", "the Windows Hello signature does not verify against "
                                         "the enrolled key")
        return True

    # -- sealed store
    def _dp(self):
        if self._dpapi is None:
            if not IS_WINDOWS:
                raise F.FactorError("unavailable", "DPAPI is Windows only")
            self._dpapi = Dpapi()
        return self._dpapi

    def _seal_key(self, record, reason):
        if not record.get("seal_capable"):
            raise F.FactorError("unavailable", record.get("seal_note")
                                or "this Windows Hello enrolment cannot seal (re-enrol hello)")
        pub, name = self._record_key(record)
        salt = record.get("seal_salt")
        if not isinstance(salt, str) or not re.match(r"^[0-9a-f]{32}$", salt):
            raise F.FactorError("not_enrolled", "the Windows Hello enrolment has no seal salt")
        msg = seal_message(salt)
        sig = self._sign(name, msg, reason)
        # I4: only a signature the enrolled key really made may become the sealing key.
        if not C.rs256_verify(pub, msg, sig):
            raise F.FactorError("wrong", "the Windows Hello sealing signature does not verify")
        # I5: the signature and the key stay in this frame and the DPAPI call.
        return hkdf_sha256(sig, b"gt-hello-seal-v1",
                           b"dpapi-entropy|" + name.encode("ascii") + b"|" + msg + b"|"
                           + hashlib.sha256(pub).digest())

    def seal(self, record, plaintext, ctx=None):
        """-> sealed bytes. Needs a Hello prompt (the entropy IS a signature)."""
        key = self._seal_key(record, getattr(ctx, "reason", "seal a gt credential"))
        try:
            return SEAL_MAGIC + self._dp().protect(bytes(plaintext), key)
        except DpapiError as e:
            _logon_denied(e)
            raise F.FactorError("helper_failed", str(e))

    def unseal(self, record, sealed, ctx):
        if not isinstance(sealed, (bytes, bytearray)) or not bytes(sealed).startswith(SEAL_MAGIC):
            raise F.FactorError("malformed", "not a Windows Hello sealed blob")
        key = self._seal_key(record, getattr(ctx, "reason", ""))
        try:
            return self._dp().unprotect(bytes(sealed[len(SEAL_MAGIC):]), key)
        except DpapiError as e:
            _logon_denied(e)
            raise F.FactorError("wrong", "the sealed blob does not open with this Windows Hello "
                                         "key (re-enrolled, another user, or damaged)")


# ---------------------------------------------------------------- CLI (owner's live proof)

def _emit(step, **kw):
    print(json.dumps(dict(step=step, **kw), sort_keys=True), flush=True)


def selftest(seal=True, keep=False):
    """Live proof at the console: enrol a THROWAWAY key (never the real gt-unlock key), prove a
    fresh challenge, check a tampered signature fails, seal and unseal, delete the key."""
    fac = HelloFactor(key_name=SELFTEST_KEY)
    ok, why = fac.available()
    _emit("available", ok=ok, detail=why)
    if not ok:
        return 2
    ctx = F.Context(home=None, reason="gt unlock selftest")
    rc = 0
    try:
        rec = fac.enroll(ctx)
        _emit("enroll", ok=True, scheme=rec["scheme"], seal_capable=rec["seal_capable"],
              modulus_bits=C.rsa_public(base64.b64decode(rec["public"]))[0].bit_length())
        ch = secrets.token_bytes(32)
        _emit("prove", ok=fac.prove(rec, ch, ctx))
        bad = HelloFactor(runner=lambda op, p: {"signature": base64.b64encode(
            bytes(256)).decode()}, key_name=SELFTEST_KEY)
        try:
            bad.prove(rec, ch, ctx)
            _emit("forged_rejected", ok=False)
            rc = 1
        except F.FactorError as e:
            _emit("forged_rejected", ok=e.code == "wrong")
        if seal and rec["seal_capable"]:
            blob = fac.seal(rec, b"gt selftest value", ctx)
            _emit("seal", ok=True, bytes=len(blob))
            _emit("unseal", ok=fac.unseal(rec, blob, ctx) == b"gt selftest value")
        elif seal:
            _emit("seal", ok=False, detail=rec.get("seal_note"))
            rc = 1
    except F.FactorError as e:
        _emit("error", ok=False, code=e.code, detail=e.message)
        rc = 1
    finally:
        if not keep:
            try:
                run_powershell("delete", {"name": SELFTEST_KEY})
                _emit("delete", ok=True)
            except F.FactorError as e:
                _emit("delete", ok=False, code=e.code, detail=e.message)
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_unlock_hello.py",
                                 description="Windows Hello factor for gt unlock")
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True
    sub.add_parser("available", help="is Windows Hello usable for this user?")
    st = sub.add_parser("selftest", help="live proof with a throwaway key (Hello prompts)")
    st.add_argument("--no-seal", action="store_true", help="skip the seal/unseal round trip")
    st.add_argument("--keep", action="store_true", help="keep the throwaway key afterwards")
    a = ap.parse_args(argv)
    if a.cmd == "available":
        ok, why = HelloFactor().available()
        _emit("available", ok=ok, detail=why)
        return 0 if ok else 2
    return selftest(seal=not a.no_seal, keep=a.keep)


if __name__ == "__main__":
    sys.exit(main())
