#!/usr/bin/env python3
"""gt_unlock_seal -- credentials encrypted at rest under a hardware key: `sealed:<name>` refs
(0.20.0, level L2).

WHAT SEALED MEANS: the ciphertext lives in <unlock home>/sealed/<name>.blob and the key that
opens it never leaves the hardware --
  macOS    a Secure Enclave key-agreement key whose use needs Touch ID: the gt-presence helper
           seals to its public key (ephemeral ECDH, HKDF, AES-GCM in CryptoKit) and only the
           enclave, after a fingerprint, can open it;
  Windows  a key derived from a Windows Hello (TPM) signature over a fixed per-store salt,
           used as the DPAPI entropy: DPAPI alone is readable by any process of the user;
           with entropy only Hello can produce, it is not.
gt implements no cipher here (owner decision: no gt crypto store): the encryption is CryptoKit's
or DPAPI's; this module only files the blobs and asks the factor to seal and open them.

INVARIANTS
  * a sealed value is opened ONLY by the authority, ONLY for a caller holding a grant, and
    every open is audited with the grant id (the authority's job; this module has no policy);
  * opened values are cached in the authority's memory for the grant's life and dropped when
    any grant is revoked, so "unreadable while locked" holds for the cache too;
  * names are one plain path segment; values are never logged, printed or put in argv.
"""
import base64
import os
import re

import gt_unlock_factors as F

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SealError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _dir(home):
    return os.path.join(home, "sealed")


def _path(home, name):
    if not NAME.match(name or ""):
        raise SealError("bad_name", "a sealed name is one plain segment: letters, digits, . _ -")
    return os.path.join(_dir(home), name + ".blob")


def names(home):
    try:
        return sorted(n[:-5] for n in os.listdir(_dir(home)) if n.endswith(".blob"))
    except FileNotFoundError:
        return []


def platform_factor(enrolment, factors):
    """The enrolled platform factor that can seal on this machine, or (None, reason)."""
    for name in ("touchid", "hello"):
        rec = (enrolment.get("factors") or {}).get(name)
        f = factors.get(name)
        if rec and f is not None and hasattr(f, "seal"):
            ok, why = f.available()
            if ok:
                return name, f, rec
            return None, None, "%s is enrolled but unavailable: %s" % (name, why)
    return None, None, ("no platform factor (Touch ID / Windows Hello) is enrolled, so nothing "
                        "can be sealed on this machine (L2 needs one)")


def put(home, name, value, enrolment, factors, ctx=None):
    """Seal `value`. On Windows sealing itself needs Hello (the DPAPI entropy is a signature),
    so the factor gets `ctx` for its prompt; Touch ID seals to a public key with no prompt."""
    fname, f, rec = platform_factor(enrolment, factors)
    if not fname:
        raise SealError("no_platform_factor", rec)
    try:
        blob = f.seal(rec, value.encode("utf-8"), ctx) if ctx is not None else \
            f.seal(rec, value.encode("utf-8"))
    except TypeError:
        blob = f.seal(rec, value.encode("utf-8"))
    except F.FactorError as e:
        raise SealError(e.code, "sealed:%s could not be sealed: %s" % (name, e.message))
    F.write_json(_path(home, name), {"schema": 1, "factor": fname,
                                     "sealed": base64.b64encode(blob).decode("ascii")})
    return {"name": name, "factor": fname}


def get(home, name, enrolment, factors, ctx):
    data = F.read_json(_path(home, name))
    if not data:
        raise SealError("missing", "no sealed credential named %s" % name)
    fname = data.get("factor")
    rec = (enrolment.get("factors") or {}).get(fname)
    f = factors.get(fname)
    if not rec or f is None or not hasattr(f, "unseal"):
        raise SealError("no_platform_factor", "sealed:%s needs %s, which is not enrolled"
                        % (name, fname))
    try:
        return f.unseal(rec, base64.b64decode(data["sealed"]), ctx).decode("utf-8")
    except F.FactorError as e:
        raise SealError(e.code, "sealed:%s could not be opened: %s" % (name, e.message))
    except (KeyError, ValueError, UnicodeDecodeError):
        raise SealError("corrupt", "sealed:%s is damaged" % name)


def remove(home, name):
    try:
        os.unlink(_path(home, name))
        return True
    except FileNotFoundError:
        return False
