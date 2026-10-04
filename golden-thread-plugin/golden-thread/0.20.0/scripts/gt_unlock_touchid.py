#!/usr/bin/env python3
"""gt_unlock_touchid -- the Touch ID factor: a Secure Enclave key behind the fingerprint (0.20.0).

    gt_unlock_touchid.py build --dest DIR [--source FILE]
    gt_unlock_touchid.py record --helper PATH [--release]
    gt_unlock_touchid.py verify --helper PATH

`build` compiles gt-presence.swift (beside this file) with `swiftc -O` into DIR/gt-presence and
ad-hoc signs it. install.sh calls it; it needs the Xcode Command Line Tools and refuses cleanly
without them. A release machine installs the Developer ID signed helper instead
(dev/presence-release.sh).

THE INSTALL RECORD (review, 2026-10-03: "Touch ID helper swap"). `build` -- and `record` for a
binary installed another way -- writes <helper>.install.json: the BUILT binary's sha256 and, for
a Developer ID signed release binary, its code-signing requirement. Every use of the helper
(_check_helper) re-hashes it and, when a requirement is recorded, asks codesign to verify it; a
mismatch, or no record at all, makes Touch ID UNAVAILABLE (never "passed"). `record --release`
refuses a binary that is not signed by gt's release team. Honest limit: the record sits beside
the helper in your own directory, so a same-user process that rewrites BOTH defeats the hash
(not the code-signing requirement of a release binary) -- friction at L1/L2.

HASH, THEN EXEC THE SAME BYTES (review L5, 2026-10-04). The helper used to be hashed by path and
then executed by path, so a swap between the two ran a binary nobody had hashed. Now every use
reads the helper ONCE through a file descriptor (O_NOFOLLOW), hashes exactly those bytes against
the install record, writes them to a fresh private directory (mkdtemp, 0700, created beside the
helper -- in ~/.claude/golden-thread/bin, which gt sandbox mode write-protects), checks the
recorded code-signing requirement on THAT copy, executes the copy and removes it. Residual: the
record itself is the user's own file (above), and a same-user process outside the sandbox could
in principle race the private copy inside its random 0700 directory.

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
RECORD_SUFFIX = ".install.json"
# The Developer ID team that signs gt's release helper (owner, 2026-10-03 10:55).
RELEASE_TEAM = "CTM8ZW9QJD"
RELEASE_REQUIREMENT = ('anchor apple generic and certificate leaf[subject.OU] = "%s"'
                       % RELEASE_TEAM)
CODESIGN = "/usr/bin/codesign"
SOURCE = os.path.join(HERE, "gt-presence.swift")
MISSING = ("helper missing -- run install.sh with the Xcode Command Line Tools, or install the "
           "signed release helper")
QUICK_TIMEOUT_S = 30
CODES = ("cancelled", "timeout", "wrong", "locked_out", "replayed", "unavailable", "malformed",
         "helper_failed", "not_enrolled")
MAX_OUT = 1 << 20
MAX_HELPER = 64 << 20          # a helper larger than this is refused, never read whole


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
        (this user, or root) can write, AND match its install record (sha256, and the recorded
        code-signing requirement if any). SECURITY INVARIANT: this is not the trust root for
        PRESENCE -- a replaced helper still cannot forge a signature over a fresh challenge,
        because prove() verifies it here; but it would see what seal() and unseal() handle, so
        a helper that is not the one install built (or recorded) is refused."""
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
        why = helper_problem(path)
        if why:
            raise F.FactorError("unavailable", why)
        return path

    def _call(self, cmd, req, record=None, timeout=QUICK_TIMEOUT_S):
        # A biometric sign/unseal shows a Touch ID sheet: never under the test harness
        # (GT_UNLOCK_NO_UI=1). Non-biometric test keys cannot prompt and are exempt.
        bio = (record or {}).get("biometric", req.get("biometric", True)) if record \
            else req.get("biometric", True)
        if cmd in ("sign", "unseal") and bio is not False:
            F.require_ui("Touch ID")
        path = self._check_helper(self.helper_path(record))
        rundir, copy = private_copy(path)                  # L5: exec what was hashed
        try:
            # The request (it may carry plaintext for `seal`) goes over a pipe, never argv/env.
            r = subprocess.run([copy, cmd], input=json.dumps(req).encode("utf-8"),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            raise F.FactorError("timeout", "the Touch ID helper did not answer in time")
        except OSError as e:
            raise F.FactorError("unavailable", "%s (%s)" % (MISSING, type(e).__name__))
        finally:
            shutil.rmtree(rundir, ignore_errors=True)
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
        return sig          # truthy; the authority keeps it where an approval must be re-checked

    def verify(self, record, challenge, signature):
        """Re-check a signature prove() returned, under the enrolled public key (the policy
        approval, gt_unlockd._approval_problem). No prompt, no helper."""
        try:
            pub = _unb64((record or {}).get("public"), "public")
        except F.FactorError:
            return False
        return bool(C.es256_verify(pub, bytes(challenge), bytes(signature)))

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


# ---------------------------------------------------------------- the install record

def _sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _codesign_ok(path, requirement):
    try:
        r = subprocess.run([CODESIGN, "--verify", "--strict", "-R=" + requirement, path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _read_helper_bytes(path):
    """The helper's bytes, read ONCE through one descriptor that refuses a symlink and anything
    but a regular file, capped at MAX_HELPER. Raises FactorError("unavailable")."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) \
        | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise F.FactorError("unavailable", MISSING)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_HELPER:
            raise F.FactorError("unavailable", MISSING)
        chunks, total = [], 0
        while True:
            b = os.read(fd, 1 << 16)
            if not b:
                break
            total += len(b)
            if total > MAX_HELPER:
                raise F.FactorError("unavailable", MISSING)
            chunks.append(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


def private_copy(helper):
    """-> (rundir, copy): the helper's bytes, hashed against its install record and written to
    a fresh 0700 directory as a 0700 file -- the file that is then executed (L5). The caller
    removes rundir. Raises FactorError("unavailable") on any mismatch."""
    import hashlib
    try:
        with open(record_path(helper), "r", encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        raise F.FactorError("unavailable", "the helper %s has no install record; reinstall gt "
                            "(install.sh) so the helper is built and recorded" % helper)
    if not isinstance(rec, dict) or not isinstance(rec.get("sha256"), str):
        raise F.FactorError("unavailable", "the helper's install record is damaged; reinstall gt")
    data = _read_helper_bytes(helper)
    if hashlib.sha256(data).hexdigest() != rec["sha256"]:
        raise F.FactorError("unavailable", "the helper %s does not match the binary install "
                            "recorded; it may have been replaced -- reinstall gt" % helper)
    try:
        rundir = tempfile.mkdtemp(prefix=".gt-presence-run-", dir=os.path.dirname(helper))
    except OSError:
        rundir = tempfile.mkdtemp(prefix="gt-presence-run-")
    try:
        os.chmod(rundir, 0o700)
        copy = os.path.join(rundir, HELPER_NAME)
        fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o700)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        req = rec.get("requirement")
        if req and not _codesign_ok(copy, req):
            raise F.FactorError("unavailable", "the helper %s no longer satisfies its "
                                "code-signing requirement" % helper)
    except BaseException:
        shutil.rmtree(rundir, ignore_errors=True)
        raise
    return rundir, copy


def record_path(helper):
    return helper + RECORD_SUFFIX


def record_helper(helper, requirement=None):
    """Write <helper>.install.json for the binary as it is NOW: its sha256 and, optionally,
    the code-signing requirement it must keep satisfying. -> the record."""
    rec = {"schema": 1, "sha256": _sha256_file(helper), "requirement": requirement}
    if requirement and not _codesign_ok(helper, requirement):
        raise BuildError("%s does not satisfy the requirement %s" % (helper, requirement))
    F.write_json(record_path(helper), rec)
    return rec


def helper_problem(helper):
    """None when `helper` matches its install record, else why not (so Touch ID is
    unavailable). Re-hashed on every call: a swap between uses is caught at the next use."""
    try:
        with open(record_path(helper), "r", encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return ("the helper %s has no install record; reinstall gt (install.sh) so the "
                "helper is built and recorded" % helper)
    if not isinstance(rec, dict) or not isinstance(rec.get("sha256"), str):
        return "the helper's install record is damaged; reinstall gt"
    try:
        if _sha256_file(helper) != rec["sha256"]:
            return ("the helper %s does not match the binary install recorded; it may have "
                    "been replaced -- reinstall gt" % helper)
    except OSError:
        return MISSING
    req = rec.get("requirement")
    if req and not _codesign_ok(helper, req):
        return "the helper %s no longer satisfies its code-signing requirement" % helper
    return None


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
        record_helper(final)              # the BUILT binary's hash: _check_helper verifies it
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
    r = sub.add_parser("record", help="record an installed helper's sha256 (and signature)")
    r.add_argument("--helper", required=True)
    r.add_argument("--release", action="store_true",
                   help="require (and record) gt's Developer ID release signature")
    v = sub.add_parser("verify", help="exit 0 when the helper matches its install record")
    v.add_argument("--helper", required=True)
    ns = ap.parse_args(argv)
    if ns.cmd == "verify":
        why = helper_problem(ns.helper)
        if why:
            sys.stderr.write("gt_unlock_touchid: %s\n" % why)
            return 1
        sys.stdout.write("helper matches its install record\n")
        return 0
    if ns.cmd == "record":
        try:
            record_helper(ns.helper, RELEASE_REQUIREMENT if ns.release else None)
        except (BuildError, OSError) as e:
            sys.stderr.write("gt_unlock_touchid: %s\n" % e)
            return 2
        sys.stdout.write("recorded %s\n" % ns.helper)
        return 0
    try:
        path = build(ns.dest, ns.source)
    except BuildError as e:
        sys.stderr.write("gt_unlock_touchid: %s\n" % e)
        return 2
    sys.stdout.write("built %s\n" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
