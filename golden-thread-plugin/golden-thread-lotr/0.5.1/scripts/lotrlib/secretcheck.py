"""Does a plain (non-secret) value look like a secret? (0.5.0)

A template or a registry entry may carry plain, non-secret environment values (`env`): a region,
a log level, a base URL. A credential never belongs there; it is a REFERENCE (`env_refs`). This is
the one check that tells the two apart, used by the template validator and by the registry, so a
credential typed into the wrong box is refused by both.

It judges SHAPE, not meaning, so it can be wrong in both directions; it is a guard against the
common mistake (pasting a key where a plain value goes), not a scanner. The answer is a short
reason that never contains the value.
"""
import math
import re

_REF = re.compile(r"^(file|store|keychain|env|sealed|sops|op|bw|vault|wincred):\S")
_PREFIXES = re.compile(
    r"^(sk-|sk_live_|sk_test_|pk_live_|rk_live_|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|"
    r"xox[abprs]-|xapp-|AKIA[0-9A-Z]{8}|ASIA[0-9A-Z]{8}|AIza[0-9A-Za-z_-]{10}|ya29\.|"
    r"lin_api_|lin_oauth_|ntn_|secret_[A-Za-z0-9]{10}|sntrys_|sntryu_|npm_[A-Za-z0-9]{10}|"
    r"shpat_|dop_v1_|hf_[A-Za-z0-9]{10}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.)")
_BEARER = re.compile(r"^(bearer|basic|token)\s+\S{6,}", re.I)
_USERINFO = re.compile(r"^[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.I)
_SECRETISH_NAME = re.compile(r"(key|token|secret|passw|pwd|credential|private|cert|signature)", re.I)
_BENIGN = re.compile(r"^(true|false|yes|no|on|off|none|null|[0-9.]+|debug|info|warn|warning|error)$",
                     re.I)


def _entropy(s):
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _benign_shape(value):
    """A path or an https URL without credentials is a location, not a secret."""
    if value.startswith(("/", "~/", "./", "../")) or re.match(r"^[A-Za-z]:[\\/]", value):
        return True
    return bool(re.match(r"^https?://[^\s@]+$", value))


def looks_like_secret(name, value):
    """-> None when `value` is fine as a plain value, else a short reason (never the value)."""
    if not isinstance(value, str) or not value:
        return None
    if _REF.match(value):
        return "it is a secret reference; references go under env_refs, not env"
    if "-----BEGIN" in value:
        return "it contains a private key or certificate block"
    if _USERINFO.match(value):
        return "it is a URL with a user name and password in it"
    if _PREFIXES.match(value):
        return "it starts like a known API key or token format"
    if _BEARER.match(value):
        return "it looks like an Authorization header value"
    if _benign_shape(value) or _BENIGN.match(value):
        return None
    if _SECRETISH_NAME.search(name or "") and len(value) >= 8 and not re.search(r"\s", value):
        return f"the name {name} says it is a credential, so its value must be a reference"
    compact = len(value) >= 20 and not re.search(r"\s", value)
    if compact:
        classes = sum(bool(re.search(p, value)) for p in ("[a-z]", "[A-Z]", "[0-9]", r"[^A-Za-z0-9]"))
        if re.fullmatch(r"[0-9a-fA-F]{32,}", value):
            return "it is a long hexadecimal string, the shape of a key or digest"
        if classes >= 3 and _entropy(value) >= 3.8:
            return "it is a long random-looking string, the shape of a key or token"
    return None
