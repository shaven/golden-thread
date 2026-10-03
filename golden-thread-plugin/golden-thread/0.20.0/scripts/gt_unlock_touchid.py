#!/usr/bin/env python3
"""gt_unlock_touchid -- the Touch ID factor: a Secure Enclave key behind the fingerprint (0.20.0).

    gt_unlock_touchid.py build --dest DIR [--source FILE]

`build` compiles gt-presence.swift (beside this file) with `swiftc -O` into DIR/gt-presence and
ad-hoc signs it. install.sh calls it; it needs the Xcode Command Line Tools and refuses cleanly
without them. A release machine installs the Developer ID signed helper instead
(dev/presence-release.sh).

How the factor works (the authority is the only caller):

  enroll   the helper creates TWO Secure Enclave P-256 keys, both [.privateKeyUsage,
           .biometryCurrentSet]: a signing key (presence) and a key-agreement key (sealing).
           The record keeps their public keys and the SE-wrapped blobs -- opaque handles only
           this Mac's Secure Enclave can use, not key material, but still never logged.
  prove    the helper signs the authority's 32-byte challenge after ONE Touch ID prompt whose
           text is ctx.reason (it names the requester and the scope); the signature is then
           VERIFIED HERE against the enrolled public key.
  seal     ECIES to the enrolled key-agreement public key (no prompt).
  unseal   the helper opens the box with the SE key after a Touch ID prompt.

A fingerprint added or removed kills both keys (biometryCurrentSet): re-enrol, by design.
"""
import argparse
import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_unlock_crypto as C               # noqa: E402
import gt_unlock_factors as F              # noqa: E402

HELPER_NAME = "gt-presence"
SOURCE = os.path.join(HERE, "gt-presence.swift")
MISSING = ("helper missing -- run install.sh with the Xcode Command Line Tools, or install the "
           "signed release helper")
QUICK_TIMEOUT_S = 30
CODES = ("cancelled", "timeout", "wrong", "locked_out", "replayed", "unavailable", "malformed",
         "helper_failed", "not_enrolled")
MAX_OUT = 1 << 20


def default_helper():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "bin", HELPER_NAME)


def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _unb64(s, what):
    try:
        return base64.b64decode(s, validate=True)
    except (TypeError, ValueError):
        raise F.FactorError("malformed", "%s is not base64" % what)


class TouchIdFactor(F.Factor):
    name = "touchid"
    platform = True

    def __init__(self, helper=None, insecure_test_keys=False):
        # SECURITY INVARIANT: production code constructs TouchIdFactor() with NO arguments
        # (gt_unlock_factors.registry). `insecure_test_keys` exists only so the test suite can
        # use NON-biometric Secure Enclave keys and never raise a Touch ID prompt. With it off,
        # enroll() creates only biometric keys and prove()/unseal() refuse a record that is not
        # marked biometric.
        self._helper = helper
        self._test = bool(insecure_test_keys)

    # ------------------------------------------------------------ the helper

    def helper_path(self, record=None):
        return self._helper or (record or {}).get("helper") or default_helper()

    def _check_helper(self, path):
        """The helper must be an absolute path to a regular executable file that only its owner
        (this user, or root) can write. SECURITY INVARIANT: this is hygiene, not the trust root
        -- a replaced helper still cannot forge a signature over a fresh challenge, because
        prove() verifies it here; but it would see what seal() is given, so a helper anybody
        else can rewrite is refused."""
        if not path or not os.path.isabs(path):
            raise F.FactorError("unavailable", "the helper path must be absolute")
        try:
            st = os.stat(path)
        except OSError:
            raise F.FactorError("unavailable", MISSING)
        if not stat.S_ISREG(st.st_mode) or not os.access(path, os.X_OK):
            raise F.FactorError("unavailable", MISSING)
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or st.st_uid not in (os.getuid(), 0):
            raise F.FactorError("unavailable", "the helper %s is writable by others or owned by "
                                "another user; reinstall it" % path)
        return path

    def _call(self, cmd, req, record=None, timeout=QUICK_TIMEOUT_S):
        # A biometric sign/unseal shows a Touch ID sheet: never under the test harness
        # (GT_UNLOCK_NO_UI=1). Non-biometric test keys cannot prompt and are exempt.
        bio = (record or {}).get("biometric", req.get("biometric", True)) if record \
            else req.get("biometric", True)
        if cmd in ("sign", "unseal") and bio is not False:
            F.require_ui("Touch ID")
        path = self._check_helper(self.helper_path(record))
        try:
            # The request (it may carry plaintext for `seal`) goes over a pipe, never argv/env.
            r = subprocess.run([path, cmd], input=json.dumps(req).encode("utf-8"),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            raise F.FactorError("timeout", "the Touch ID helper did not answer in time")
        except OSError as e:
            raise F.FactorError("unavailable", "%s (%s)" % (MISSING, type(e).__name__))
        if len(r.stdout) > MAX_OUT:
            raise F.FactorError("helper_failed", "the helper answered too much")
        try:
            out = json.loads(r.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise F.FactorError("helper_failed", "the helper's answer is not JSON (exit %d)"
                                % r.returncode)
        if not isinstance(out, dict):
            raise F.FactorError("helper_failed", "the helper's answer is not a JSON object")
        err = out.get("error")
        if r.returncode != 0 or err is not None:
            err = err if isinstance(err, dict) else {}
            code = err.get("code") if err.get("code") in CODES else "helper_failed"
            raise F.FactorError(code, str(err.get("message") or "the helper failed")[:300])
        return out

    def _biometric(self, record):
        bio = (record or {}).get("biometric", True)
        if bio is not True and not self._test:
            # A record that says its key needs no finger is not a Touch ID enrolment.
            raise F.FactorError("malformed", "the touchid enrolment is not biometric; re-enrol")
        return bool(bio)

    # ------------------------------------------------------------ Factor

    def available(self):
        if sys.platform != "darwin":
            return False, "Touch ID is macOS only"
        try:
            out = self._call("available", {})
        except F.FactorError as e:
            return False, e.message
        if out.get("secure_enclave") is not True:
            return False, str(out.get("reason") or "this Mac has no Secure Enclave")
        if out.get("biometry") is not True and not self._test:
            return False, str(out.get("reason") or "Touch ID cannot be used now")
        return True, str(out.get("reason") or "Secure Enclave and Touch ID are available")

    def _create(self, kind, ctx):
        out = self._call("create", {"kind": kind, "biometric": not self._test,
                                    "reason": ctx.reason})
        pub = _unb64(out.get("public"), "public")
        blob = _unb64(out.get("blob"), "blob")
        try:
            C.p256_point(pub)                  # 65-byte uncompressed point, on the curve
        except C.CryptoError:
            raise F.FactorError("helper_failed", "the helper returned an invalid public key")
        if len(pub) != 65 or not blob:
            raise F.FactorError("helper_failed", "the helper returned an invalid key")
        return pub, blob

    def enroll(self, ctx):
        pub, blob = self._create("sign", ctx)
        apub, ablob = self._create("agree", ctx)
        rec = {"public": _b64(pub), "blob": _b64(blob), "agree_public": _b64(apub),
               "agree_blob": _b64(ablob), "helper": self.helper_path(),
               "biometric": not self._test}
        return rec

    def prove(self, record, challenge, ctx):
        if not record or not record.get("public") or not record.get("blob"):
            raise F.FactorError("not_enrolled", "Touch ID is not enrolled")
        bio = self._biometric(record)
        if not isinstance(challenge, (bytes, bytearray)) or len(challenge) != 32:
            raise F.FactorError("malformed", "the challenge is 32 bytes")
        pub = _unb64(record["public"], "public")
        out = self._call("sign", {"blob": record["blob"], "challenge": _b64(bytes(challenge)),
                                  "reason": ctx.reason, "biometric": bio},
                         record, timeout=F.PROMPT_TIMEOUT_S + 15)
        sig = out.get("signature")
        if not isinstance(sig, str):
            raise F.FactorError("wrong", "the helper returned no signature")
        try:
            sig = base64.b64decode(sig, validate=True)
        except (TypeError, ValueError):
            raise F.FactorError("wrong", "the helper's signature is not base64")
        # SECURITY INVARIANT: presence is proven ONLY by an ES256 signature, checked here, under
        # the ENROLLED public key, over EXACTLY this challenge. Whatever else the helper says
        # ("ok", a signature over another message, another key's signature) is worthless.
        if not C.es256_verify(pub, bytes(challenge), sig):
            raise F.FactorError("wrong", "the Touch ID signature does not verify")
        return True

    def seal(self, record, plaintext):
        if not record or not record.get("agree_public"):
            raise F.FactorError("not_enrolled", "Touch ID sealing is not enrolled")
        apub = _unb64(record["agree_public"], "agree_public")
        try:
            C.p256_point(apub)
        except C.CryptoError:
            raise F.FactorError("malformed", "the enrolled key-agreement key is not P-256")
        out = self._call("seal", {"public": _b64(apub), "plaintext": _b64(bytes(plaintext))},
                         record)
        sealed = _unb64(out.get("sealed"), "sealed")
        if len(sealed) < 65 + 12 + 16 + len(plaintext) or sealed[0] != 4:
            raise F.FactorError("helper_failed", "the helper's sealed value has the wrong shape")
        return sealed

    def unseal(self, record, sealed, ctx):
        if not record or not record.get("agree_blob"):
            raise F.FactorError("not_enrolled", "Touch ID sealing is not enrolled")
        bio = self._biometric(record)
        out = self._call("unseal", {"blob": record["agree_blob"], "sealed": _b64(bytes(sealed)),
                                    "reason": ctx.reason, "biometric": bio},
                         record, timeout=F.PROMPT_TIMEOUT_S + 15)
        # AES-GCM authenticated the box inside the helper; the plaintext is returned to the
        # authority only and never logged.
        return _unb64(out.get("plaintext"), "plaintext")


# ---------------------------------------------------------------- building the helper

class BuildError(Exception):
    pass


def build(dest_dir, source=None):
    """Compile gt-presence.swift into dest_dir/gt-presence (swiftc -O) and ad-hoc sign it.
    -> the helper's path. Raises BuildError with a plain reason (no swiftc, not macOS, compile
    or codesign failure). The binary is built beside the target and renamed into place, so a
    failed build never leaves a half-written helper."""
    if sys.platform != "darwin":
        raise BuildError("the Touch ID helper is macOS only")
    source = source or SOURCE
    if not os.path.isfile(source):
        raise BuildError("helper source not found: %s" % source)
    swiftc = shutil.which("swiftc")
    if not swiftc or subprocess.run([swiftc, "--version"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL).returncode != 0:
        raise BuildError("swiftc is not available: install the Xcode Command Line Tools "
                         "(xcode-select --install), or install the signed release helper")
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    final = os.path.join(dest_dir, HELPER_NAME)
    tmpdir = tempfile.mkdtemp(prefix=".gt-presence-build.", dir=dest_dir)
    try:
        out = os.path.join(tmpdir, HELPER_NAME)
        r = subprocess.run([swiftc, "-O", "-o", out, source], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=600)
        if r.returncode != 0:
            raise BuildError("swiftc failed:\n" + r.stdout.decode("utf-8", "replace")[-2000:])
        r = subprocess.run(["/usr/bin/codesign", "-s", "-", "--force", out],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        if r.returncode != 0:
            raise BuildError("codesign failed:\n" + r.stdout.decode("utf-8", "replace")[-2000:])
        os.chmod(out, 0o755)
        os.replace(out, final)
    except subprocess.TimeoutExpired:
        raise BuildError("the helper build timed out")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return final


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_unlock_touchid.py",
                                 description="Build the gt-presence Touch ID helper.")
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True
    b = sub.add_parser("build", help="compile and ad-hoc sign gt-presence")
    b.add_argument("--dest", required=True, help="directory to put gt-presence in")
    b.add_argument("--source", default=None, help="the Swift source (default: beside this file)")
    ns = ap.parse_args(argv)
    try:
        path = build(ns.dest, ns.source)
    except BuildError as e:
        sys.stderr.write("gt_unlock_touchid: %s\n" % e)
        return 2
    sys.stdout.write("built %s\n" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
