#!/usr/bin/env python3
"""gt_secret_shapes -- the credential shapes the Stop validator looks for, in one place (0.22.0).

Before this the Stop hook carried its own inline list and the scanner pack another, and they
drifted: neither knew `sk-ant-`, `sk-proj-`, `github_pat_`, `AIza` or `xoxe-`, and the validator
let a value that merely STARTED with `test`, `my-`, `example` or `xxx` through however long and
random it was (`password=testSuperRealPass123`). The review (phase 109) found both.

Two rules keep this honest:
  * Talking ABOUT credentials stays possible, so the documented placeholders still pass:
    the AWS docs key, <token>, ${VAR}, $VAR, changeme, a run of x or * or ., a bare word like
    `example`, and a short or digit-free value after a placeholder word.
  * A finding is a label. This module never returns, logs or formats a matched value.

The scanner pack (packs/core/secrets.common.pack.json) holds the same families as data; a test
checks every family here is also caught there. Stdlib only; the validator must fail open, so an
import problem is the caller's to swallow.
"""
import re

HIGH_CONFIDENCE = [
    ("Google OAuth client secret", re.compile(r'GOCSPX-[A-Za-z0-9_-]{20,}')),
    ("Google access token",        re.compile(r'\bya29\.[A-Za-z0-9_-]{30,}')),
    ("Google refresh token",       re.compile(r'\b1//0[A-Za-z0-9_-]{30,}')),
    ("Google API key",             re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b')),
    ("AWS access key id",          re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ("GitHub token",               re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}')),
    ("GitHub fine-grained token",  re.compile(r'\bgithub_pat_[A-Za-z0-9_]{22,}')),
    ("Anthropic API key",          re.compile(r'\bsk-ant-[A-Za-z0-9_-]{20,}')),
    ("OpenAI API key",             re.compile(r'\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}')),
    ("Slack token",                re.compile(r'\bxox[abprse]-[A-Za-z0-9-]{12,}')),
    ("Stripe secret key",          re.compile(r'\b[sr]k_live_[A-Za-z0-9]{16,}')),
    ("npm token",                  re.compile(r'\bnpm_[A-Za-z0-9]{36}\b')),
    ("JSON web token",             re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')),
    ("private key block",          re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
]

# keyword = literalvalue   /   -u user:literalpassword   /   Bearer <token>
#
# The keyword may carry an identifier PREFIX (aws_secret_access_key, NEST_CLIENT_SECRET,
# CAM_HTTP_PASS: real credentials are namespaced), but the SUFFIX is deliberately not open: a
# trailing tail would fire on `token_count = 123456` and `cache_key = ...`. Do NOT "fix" the prefix
# with a word boundary: underscore is a word character and \b would stop matching CAM_HTTP_PASS=.
KEYWORD_LITERAL = re.compile(
    r'([\w-]*(?:pass(?:word|wd)?|pwd|secret|token|credential|'
    r'(?:api|access|private|auth|signing|encryption|client|secret)[_-]?key))'
    r'["\']?\s*[=:]\s*(["\']?)([^\s"\'&<>]{6,})\2',     # the optional quote lets JSON keys match ("password": "...")
    re.I)

# `NOPASSWD:` is the sudoers tag for *no password required*; its operand is a command path.
SAFE_KEYWORD = re.compile(r'^no[_-]?passwd$', re.I)

BASIC_AUTH = re.compile(r'-u\s+[A-Za-z0-9_.-]{2,}:([^\s"\']{4,})')
URL_CREDENTIALS = re.compile(r'\b[a-z][a-z0-9+.-]*://[^\s:/@"\']+:([^\s@/"\']{4,})@')
BEARER = re.compile(
    r'\b(?:bearer|authorization\s*:\s*bearer)\s+([A-Za-z0-9._~+/=-]{16,})', re.I)

_REF = re.compile(r'^[\s"\'`]*[$<{]')                         # $VAR  ${VAR}  <placeholder>  {{X}}
_REDACTION = re.compile(r'^[\s"\'`]*(?:x+|X+|\.+|\*+|_+)[\s"\'`]*$')   # the WHOLE value is the mask
_WORD = re.compile(
    r'^[\s"\'`]*(?:redacted|change-?me|example|placeholder|dummy|fake|sample|none|null|n/a|'
    r'test|todo|tbd|secret|password|token)[\s"\'`]*$', re.I)
_LEADING_WORD = re.compile(r'^[\s"\'`]*(?:your[-_]|my[-_]|test|example|dummy|fake|sample)', re.I)
_SHORT = 10

# The access keys and secret key AWS prints in its own documentation: public, never a credential.
DOCUMENTED = frozenset(("AKIA" + "IOSFODNN7" + "EXAMPLE", "AKIA" + "I44QH8DHB" + "EXAMPLE",
                        "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"))


def is_placeholder(val):
    """True when `val` is a documented stand-in, not a credential.

    A value that only BEGINS with a placeholder word is excused when what follows is short or has
    no digit (`your-password-here`, `my-api-key`, `test123`); a long value with digits (a random
    string) is not, whatever it starts with. Only the keys AWS itself documents are excused by name;
    a value that merely CONTAINS the word EXAMPLE is not (the demo's planted key must still be caught)."""
    if not val:
        return True
    if _REF.match(val) or _REDACTION.match(val) or _WORD.match(val):
        return True
    if val in DOCUMENTED:
        return True
    if _LEADING_WORD.match(val):
        return len(val) <= _SHORT or not any(c.isdigit() for c in val)
    return False


def secret_findings(text):
    """-> sorted labels of the credential shapes in `text`. Never the matched text."""
    hits = []
    for name, rx in HIGH_CONFIDENCE:
        for m in rx.finditer(text):
            if m.group(0) not in DOCUMENTED:
                hits.append(name)
                break

    for m in KEYWORD_LITERAL.finditer(text):
        keyword, val = m.group(1), m.group(3)
        if SAFE_KEYWORD.match(keyword):
            continue
        if val and not is_placeholder(val):
            hits.append("credential assigned a literal value")
            break

    for rx, label in ((BASIC_AUTH, "basic-auth pair with a literal password"),
                      (URL_CREDENTIALS, "URL with a literal password"),
                      (BEARER, "bearer token with a literal value")):
        for m in rx.finditer(text):
            val = m.group(m.lastindex or 1)
            if val and not is_placeholder(val):
                hits.append(label)
                break
    return sorted(set(hits))
