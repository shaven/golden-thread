"""Errors as the MODEL sees them (gt-lotr 0.5.0, gt 0.21.4; ACS R-8.7-L1-002/003).

A downstream's error text is the likeliest place for the invocation layer to leak into the
planning layer: a provider's validation message quotes its schema, a network error names its host,
a credential hint names the secret reference. The engine keeps the full error (it goes to the
local audit log as `detail`, and the admin plane -- the CLI, /v1 -- still shows it); the two MCP
front doors pass every error through `public_error` before the model reads it.

Two rules, in this order:

  1. A code the GATEWAY wrote (policy, tier, caller, cursor, reference, token, consent, unlock)
     keeps its message and hints. They were written for the model. Only the shapes that must never
     reach it are taken out: a URL, a secret reference, a credential-shaped string.
  2. Every other code (anything a connector raised from what a downstream answered, a sign-in or a
     secret problem, and any code not known yet) becomes one fixed sentence per family. The
     downstream's words are dropped. The hints are dropped too, except a sign-in command
     (`run: lotr login ...`), which is a thing to do and carries no secret.

Unknown means generic: a code added tomorrow leaks nothing until it is listed here as safe.
Pure functions; no I/O.
"""
import math
import re

# Codes the gateway itself writes for the model. A new gateway-authored code goes here, with a test.
GATEWAY_CODES = frozenset((
    "bad_request", "bad_tool", "bad_op", "wrong_tool", "op_denied", "unknown_op", "unknown_connection",
    "unknown_client", "client_not_allowed", "tier_ceiling", "client_revoked", "unauthorized",
    "cursor_needs_read", "bad_cursor", "missing_param", "recipe_not_applicable", "oauth_unattended_refused", "audit_failed",
    "consent_denied", "consent_refused", "consent_unconfirmed",
    # gt action tokens and the references that share their refusals
    "token_required", "token_unavailable", "malformed", "unknown", "forged", "wrong_action",
    "wrong_target", "wrong_args", "wrong_session", "expired", "spent",
    # gt unlock, as the shim and the engine relay it
    "locked", "step_up", "mcp_only", "failed_closed", "peer_unidentified", "unlock_unavailable",
    "peer_refused", "not_registered", "subject_gone", "unattended",
    # the gateway's own doors
    "unknown_method", "too_large", "internal", "daemon_unreachable", "daemon_running", "zone_mismatch",
    # the shim's own setup failures (lotrlib/client.py), every one a fixed gateway-written message. Until
    # 0.5.1 these fell to the generic "details are in the audit log" text with no hint, so a Mac with no
    # gateway set up told the model nothing it could pass on (found 2026-10-19: `find` answered
    # not_initialized, hints empty). NOT here on purpose: bad_response and server_refused, whose messages
    # can carry the peer's own text (client._error_from, a hub's refusal) and so stay generic.
    "not_initialized", "config_invalid", "not_implemented", "no_front_door", "daemon_timeout",
    # the gateway's own optional per-connection limit (limits in the registry; lotrlib/upstream.py)
    "rate_limited_locally",
))

# What a provider's status becomes (lotrlib/upstream.py). The provider's own words are dropped, like
# any downstream's; the two numbers the structured error carries are kept, and only as numbers.
_UPSTREAM = {
    "upstream_rate_limited": ("The service is rate limiting this connection: it is busy, not down. "
                              "Wait (retry_after_s, when given) and try again; the gateway does not retry.",
                              ["wait, then retry later; do not retry in a tight loop"]),
    "upstream_error": ("The service failed while handling the request. Try again later; the details "
                       "are in the gateway's audit log.", []),
    "upstream_payment_required": ("The service refused the request because the account is out of credit "
                                  "or over its limit. Add credit or raise the limit with the provider; "
                                  "retrying will not help.", ["add credit with the provider, then retry"]),
    "upstream_timeout": ("The service timed out while handling the request. Try again, or with a "
                         "smaller request; the details are in the gateway's audit log.", []),
}
_NUMBERS = ("retry_after_s", "upstream_status")

_SIGN_IN_HINT = re.compile(r"^run: lotr (?:login|connect|status)\b[^\n]{0,120}$")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)
_REF = re.compile(r"\b(?:file|store|keychain|env|sealed|sops|op|bw|vault|wincred|brokered):[^\s,;)]+", re.I)

_FAMILIES = (
    # (code test, message, hints). The first that matches wins.
    (lambda c: c in ("http_401", "http_403"),
     "The service refused the credential this connection holds ({code}). The owner has to sign in "
     "or fix the credential; the details are in the gateway's audit log.",
     ["tell the user; do not retry"]),
    (lambda c: c.startswith("http_4"),
     "The service rejected the request ({code}). Check the inputs against find(connection, op); the "
     "service's own message is in the gateway's audit log.", []),
    (lambda c: c.startswith("http_5"),
     "The service failed while handling the request ({code}). Try again later; the details are in "
     "the gateway's audit log.", []),
    (lambda c: c in ("needs_login", "auth_invalid", "auth_failed", "insufficient_scope")
     or c.startswith(("oauth_", "secret_")),
     "This connection's sign-in or credential needs attention ({code}). The owner has to fix it; the "
     "details are in the gateway's audit log.", ["tell the user; do not retry"]),
    (lambda c: True,
     "The connection could not complete the request ({code}). The details are in the gateway's "
     "audit log.", []),
)


def scrub_text(text):
    """`text` with every URL and secret reference made a neutral word."""
    return _REF.sub("[reference]", _URL.sub("[address]", str(text)))


def _family(code):
    for test, message, hints in _FAMILIES:
        if test(code):
            return message.format(code=code), list(hints)
    return "The connection could not complete the request.", []


_SAFE_CODE = re.compile(r"[A-Za-z0-9_.-]{1,48}")


def safe_code(code):
    """A downstream's error code as the model may read it: a short plain token, else a neutral word.
    A code is the downstream's own text, so it can carry a URL or a secret reference (found 2026-10-15
    by a local-engine review); only the plain-token shape is passed through."""
    code = str(code or "internal")
    return code if _SAFE_CODE.fullmatch(code) else "downstream_error"


def _numbers(err):
    """retry_after_s and upstream_status, when they are plain finite numbers (upstream._number's rule: NaN and
    infinity are not numbers here); nothing else passes."""
    return {k: err[k] for k in _NUMBERS
            if not isinstance(err.get(k), bool) and isinstance(err.get(k), (int, float)) and math.isfinite(err[k])}


def public_error(err):
    """An error dict {code, message, hints} as the model may read it."""
    err = err if isinstance(err, dict) else {}
    code = safe_code(err.get("code"))
    if code in _UPSTREAM:
        message, hints = _UPSTREAM[code]
        return dict({"code": code, "message": message, "hints": list(hints)}, **_numbers(err))
    if code in GATEWAY_CODES:
        return dict({"code": code, "message": scrub_text(err.get("message") or code),
                     "hints": [scrub_text(h) for h in (err.get("hints") or []) if isinstance(h, str)]},
                    **_numbers(err))
    message, hints = _family(code)
    keep = [scrub_text(h) for h in (err.get("hints") or []) if isinstance(h, str) and _SIGN_IN_HINT.match(h)]
    return {"code": code, "message": message, "hints": keep or hints}
