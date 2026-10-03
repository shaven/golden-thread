#!/usr/bin/env python3
"""gt_unlock_totp -- RFC 6238 time-based one-time codes for the unlock authority (0.20.0).

Standard library only. SHA-1, 6 digits, 30-second steps: what every authenticator app reads
from an otpauth:// URI without asking.

WHAT A TOTP CODE PROVES HERE, honestly: "someone holding the phone typed a code". The seed
sits in a mode-600 file the authority reads, so a process running as the same user could read
it too -- at L1 and L2 TOTP is a presence factor, not a secret only the authority holds. It is
a real second factor only where the seed is out of the user's reach (L3, a service account).

INVARIANTS enforced in verify():
  * codes are compared with hmac.compare_digest;
  * only steps T-skew..T+skew are accepted (skew 1 by default);
  * a step at or below the last ACCEPTED step is refused, so a code -- or an older one seen
    over a shoulder or by a keylogger -- can be used once only (replay);
  * after `max_failures` wrong codes in a row, every code is refused for 1, then 5, then 15
    minutes; the counter and the lockout are persisted by the caller (state survives a
    restart), and a wall clock that went BACKWARDS during a lockout extends it rather than
    ending it.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

DIGITS = 6
STEP = 30
DEFAULT_SKEW = 1
DEFAULT_MAX_FAILURES = 5
DEFAULT_LOCKOUT_MINUTES = (1, 5, 15)


def code_at(key, t, *, digits=DIGITS, step=STEP, digest=hashlib.sha1):
    """The code for unix time `t` (RFC 6238 / RFC 4226 dynamic truncation)."""
    counter = struct.pack(">Q", int(t) // step)
    h = hmac.new(key, counter, digest).digest()
    o = h[-1] & 0x0F
    v = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(v).zfill(digits)


def new_secret():
    """160 random bits, the RFC 4226 recommended length."""
    return secrets.token_bytes(20)


def b32(key):
    return base64.b32encode(key).decode("ascii").rstrip("=")


def from_b32(text):
    t = "".join(text.split()).upper()
    return base64.b32decode(t + "=" * (-len(t) % 8))


def otpauth_uri(key, account, issuer="gt"):
    """The URI an authenticator app scans. It CONTAINS THE SEED: show it once, never log it."""
    label = urllib.parse.quote("%s:%s" % (issuer, account), safe=":@")
    q = urllib.parse.urlencode({"secret": b32(key), "issuer": issuer, "algorithm": "SHA1",
                                "digits": DIGITS, "period": STEP})
    return "otpauth://totp/%s?%s" % (label, q)


def lockout_seconds(level, minutes=DEFAULT_LOCKOUT_MINUTES):
    """Lockout length for the n-th lockout (1-based): 1, 5, 15, 15, ... minutes."""
    seq = list(minutes) or [15]
    return 60 * seq[min(max(level, 1), len(seq)) - 1]


def verify(key, code, state, *, now=None, skew=DEFAULT_SKEW, max_failures=DEFAULT_MAX_FAILURES,
           lockout_minutes=DEFAULT_LOCKOUT_MINUTES):
    """Check `code` against `key`, updating `state` IN PLACE (the caller persists it).

    state keys: last_step, failures, lockouts, locked_until, locked_at.
    -> (ok, reason) where reason is "ok" | "locked_out" | "malformed" | "replayed" | "wrong".
    """
    now = time.time() if now is None else now
    until = state.get("locked_until") or 0
    if until:
        locked_at = state.get("locked_at") or 0
        if now < locked_at:
            # The clock went backwards while locked out: never a way out early. Restart the
            # lockout from the new "now" instead.
            state["locked_until"] = now + (until - locked_at)
            state["locked_at"] = now
            return False, "locked_out"
        if now < until:
            return False, "locked_out"
        state["locked_until"] = 0
    code = "".join((code or "").split())
    if len(code) != DIGITS or not code.isdigit():
        return False, _fail(state, now, max_failures, lockout_minutes, "malformed")
    t_step = int(now) // STEP
    last = state.get("last_step", -1)
    matched = None
    for d in range(-skew, skew + 1):
        s = t_step + d
        if hmac.compare_digest(code_at(key, s * STEP), code):
            matched = s
            break
    if matched is None:
        return False, _fail(state, now, max_failures, lockout_minutes, "wrong")
    if matched <= last:
        return False, _fail(state, now, max_failures, lockout_minutes, "replayed")
    state["last_step"] = matched
    state["failures"] = 0
    return True, "ok"


def _fail(state, now, max_failures, lockout_minutes, reason):
    state["failures"] = int(state.get("failures") or 0) + 1
    if state["failures"] >= max_failures:
        state["lockouts"] = int(state.get("lockouts") or 0) + 1
        state["locked_at"] = now
        state["locked_until"] = now + lockout_seconds(state["lockouts"], lockout_minutes)
        state["failures"] = 0
    return reason
