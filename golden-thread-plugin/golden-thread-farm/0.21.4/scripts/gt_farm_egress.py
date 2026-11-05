#!/usr/bin/env python3
"""gt_farm_egress.py -- what an agent engine may read, and what may leave with it (gt-farm agent mode, phase C).

Everything an agent reads from the workspace, or a command prints, goes to a third-party model. This module
is the one choke point for that traffic. Standard library only; gt_farm_send is imported lazily so the module
imports under a fake HOME.

    gt_farm_egress.py scan FILE [--engine NAME-or-local] [--workdir DIR] [--json]
    gt_farm_egress.py deny PATH
    gt_farm_egress.py policy BASE_URL [TOKEN ...]

Public functions:
    scrub_tool_result(text, engine, workdir=None) -> (clean_text, notes)
    deny_read(rel_path, extra_names=()) -> "" when readable, else a plain-language reason
    provider_family(base_url) -> "openrouter" | "unknown"
    provider_extras(engine) -> dict of extra request-body fields (data_policy tokens, enforced per family)
    data_policy_summary(engine) -> list of plain sentences (who sees the code, what is enforced)

scan prints which lines WOULD be withheld (line number and rule, never a value). `--engine local` shows the
local-engine exemption; the default is a hosted engine.

Provider fields (PROVIDER_FIELDS). openrouter: provider.data_collection="deny" (documented provider
preference, "allow"|"deny") is VERIFIED from memory of OpenRouter's provider-routing docs; provider.zdr=true
(restrict to Zero Data Retention endpoints) is UNVERIFIED against live docs in this build (no network) --
check https://openrouter.ai/docs/features/provider-routing before relying on it. Unknown families get {}
and a plain warning that the policy is NOT enforced.

Exit codes: 0 nothing would be withheld (or deny: readable); 1 usage/IO error; 2 something would be
withheld (or deny: refused); 6 the secret scanner could not run (everything would be withheld, fail closed).
"""
import argparse
import fnmatch
import json
import os
import re
import sys
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

GT_VERSION = "0.21.4"

WITHHELD = "[withheld: looks like a secret]"
WITHHELD_ALL = "[withheld: the secret scan could not run, so this result was not released]"
INTERNAL_PH = "[internal-host]"
PRIVATE_PH = "[private-term]"

# Shapes the shared scanner may not cover (a header or an assignment in command output). Line-level, like the scanner.
_EXTRA_SECRET_RES = (
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I)),
    ("credential-assignment", re.compile(
        r"(?i)\b[\w.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|auth)[\w.-]*\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{16,}")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")

# ---- deny_read -------------------------------------------------------------------------------

_DENY_NAMES = (".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "id_rsa*", "id_dsa*",
               "id_ecdsa*", "id_ed25519*", ".netrc", ".npmrc", ".pypirc", ".pgpass", "credentials", "credentials.*",
               "*credentials*.json", "service-account*.json", "secrets.*", "*.apikey", "*api_key*", "*apikey*",
               "farm-key*", "*farm*.key", "*farm*key*")
_DENY_DIRS = (".ssh", ".aws", ".gnupg", ".kube", ".docker")


def deny_read(rel_path, extra_names=()):
    """-> "" when the agent may read this path, else the plain-language reason it may not. The workspace layer
    calls this before it opens anything. `extra_names` are more fnmatch names (an engine's key_file basename)."""
    raw = str(rel_path or "")
    if not raw.strip():
        return "empty path"
    p = os.path.expanduser(raw).replace("\\", "/")
    norm = os.path.normpath(p).replace("\\", "/")
    parts = [x for x in norm.split("/") if x not in ("", ".")]
    low = [x.lower() for x in parts]
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    if home and len(home) > 1 and (norm == home + "/.claude" or norm.startswith(home + "/.claude/")):
        return "anything under ~/.claude is off limits (gt state, keys, settings)"
    if ".claude" in low and os.path.isabs(norm):
        return "anything under a .claude folder is off limits (gt state, keys, settings)"
    for d in _DENY_DIRS:
        if d in low[:-1] or (low and low[-1] == d):
            return "%s/ holds credentials" % d
    for i in range(len(low) - 1):
        if low[i] == ".git" and low[i + 1] == "config":
            return ".git/config can hold remote credentials"
    if len(low) >= 2 and low[-2] == ".git" and low[-1] == "config":
        return ".git/config can hold remote credentials"
    name = low[-1] if low else ""
    for pat in tuple(_DENY_NAMES) + tuple(str(x).lower() for x in extra_names or ()):
        if fnmatch.fnmatch(name, pat):
            return "%s looks like a credential or key file" % parts[-1]
    return ""


# ---- provider data policy --------------------------------------------------------------------

# family -> {policy token -> fields merged into the request body}. UNVERIFIED entries are marked in the module
# docstring; a family is inferred from the base_url host. Add a family here, never in code paths.
PROVIDER_FIELDS = {
    "openrouter": {
        "no-train": {"provider": {"data_collection": "deny"}},        # documented provider preference
        "zero-retention": {"provider": {"zdr": True}},                # UNVERIFIED field name
    },
}
FAMILY_HOSTS = {"openrouter.ai": "openrouter"}
# `block:<slug>+<slug>` token: providers the request must never be routed to. family -> body fields for a slug list.
# openrouter: provider.ignore (documented provider preference; takes provider slugs from GET /api/v1/providers).
BLOCK_FIELDS = {"openrouter": lambda slugs: {"provider": {"ignore": slugs}}}
BLOCK_PREFIX = "block:"
TOKEN_TEXT = {"no-train": "your code is not used for training", "zero-retention": "the provider keeps no copy (zero data retention)"}


def provider_family(base_url):
    host = (urlparse(str(base_url or "")).hostname or "").lower()
    for h, fam in FAMILY_HOSTS.items():
        if host == h or host.endswith("." + h):
            return fam
    return "unknown"


def _tokens(engine):
    dp = (engine or {}).get("data_policy") or ""
    if isinstance(dp, (list, tuple, set)):
        items = [str(x) for x in dp]
    else:
        items = re.split(r"[,;\s|]+", str(dp))
    return [t.strip().lower() for t in items if t.strip()]


def _block_slugs(token):
    return [x for x in token[len(BLOCK_PREFIX):].split("+") if x]


def provider_extras(engine):
    """Extra request-body fields for the engine's data_policy tokens. {} for a local engine, no tokens, or an
    unknown family (data_policy_summary says so). Tokens merge with a one-level deep merge."""
    if not engine or engine.get("local"):
        return {}
    fam = provider_family(engine.get("base_url"))
    table = PROVIDER_FIELDS.get(fam, {})
    out = {}
    for t in _tokens(engine):
        fields = table.get(t)
        if t.startswith(BLOCK_PREFIX) and fam in BLOCK_FIELDS and _block_slugs(t):
            fields = BLOCK_FIELDS[fam](_block_slugs(t))
        for k, v in (fields or {}).items():
            if isinstance(v, dict):
                out.setdefault(k, {}).update(v)
            else:
                out[k] = v
    return out


def data_policy_summary(engine):
    """-> list of plain sentences: who sees the code, what the row asks for, what is enforced."""
    engine = engine or {}
    host = urlparse(str(engine.get("base_url") or "")).hostname or "an unknown host"
    if engine.get("local"):
        return ["Your code stays on your network: %s is a local engine (%s)." % (engine.get("name") or "this engine", host)]
    out = ["Your code will be sent to %s." % host]
    toks = _tokens(engine)
    if not toks:
        out.append("This engine row asks for no data policy, so the provider's own terms apply.")
        return out
    fam = provider_family(engine.get("base_url"))
    table = PROVIDER_FIELDS.get(fam, {})
    asked = ", ".join(toks)
    blocking = fam in BLOCK_FIELDS
    enforced = [t for t in toks if t in table or (blocking and t.startswith(BLOCK_PREFIX) and _block_slugs(t))]
    missing = [t for t in toks if t not in enforced]
    if fam == "unknown" or not enforced:
        out.append("This engine row asks for: %s. It is NOT enforced for this provider; only its own terms protect your code." % asked)
        return out
    out.append("This engine row asks for: %s. gt adds these to every request (a request to the provider; gt cannot check that the provider honours it): %s." % (
        asked, "; ".join("%s (%s)" % (t, TOKEN_TEXT.get(t) or "never routed to: %s" % ", ".join(_block_slugs(t))) for t in enforced)))
    if missing:
        out.append("Not enforced for this provider: %s." % ", ".join(missing))
    return out


# ---- scrubbing tool results ------------------------------------------------------------------

def _send():
    import gt_farm_send as S      # lazy: needs HOME-dependent config only when called
    return S


def _vault(S):
    try:
        return S.vault_path() or ""
    except (SystemExit, Exception):       # noqa: BLE001 -- no vault configured is a state, not an error
        return ""


def secret_lines(text, vault=""):
    """-> (set of 1-based line numbers that look like secrets, ok). ok False = the scanner could not run."""
    hits = set()
    ok = True
    try:
        S = _send()
        found, _why = S.secret_scanner(vault)
        if not found:
            ok = False
        else:
            gt_secrets, rules = found
            hits |= {h[0] for h in gt_secrets.scan_text(text, rules)}
    except Exception:                      # noqa: BLE001 -- a scan that fails is no scan
        ok = False
    lines = text.split("\n")
    in_pem = False
    for i, ln in enumerate(lines, 1):
        if in_pem:
            hits.add(i)
            if _PEM_END.search(ln):
                in_pem = False
            continue
        if _PEM_BEGIN.search(ln):
            hits.add(i)
            in_pem = not _PEM_END.search(ln)
            continue
        if any(rx.search(ln) for _n, rx in _EXTRA_SECRET_RES):
            hits.add(i)
    return hits, ok


def _rel_paths(text, workdir):
    if not workdir:
        return text, 0
    wd = os.path.abspath(workdir).rstrip("/\\")
    n = 0
    for base in {wd, os.path.realpath(wd)}:
        if len(base) > 1:
            text, k = re.subn(re.escape(base) + r"(?:[/\\]|(?=$|[^\w]))", "", text)
            n += k
    return text, n


def scrub_tool_result(text, engine, workdir=None):
    """-> (clean_text, notes). Hosted engine: secret lines withheld, workdir paths made relative, the release()
    path/project/loopback scrub applied, internal hostnames/IPs and forbidden terms replaced with placeholders
    (release() would REFUSE a packet; a tool result cannot be refused, so the offending text is withheld).
    Local engine (engine["local"]): exempt from the path/host scrub exactly as release() is, but secrets are
    still withheld (secret_refusal applies to every packet). Notes never contain a value."""
    notes = []
    text = "" if text is None else str(text)
    if not text:
        return text, notes
    S = None
    try:
        S = _send()
    except Exception:                      # noqa: BLE001
        pass
    vault = _vault(S) if S else ""
    hits, ok = secret_lines(text, vault)
    if not ok:
        return WITHHELD_ALL, ["secret scan could not run: whole result withheld"]
    if hits:
        lines = text.split("\n")
        out, prev = [], False
        for i, ln in enumerate(lines, 1):
            if i in hits:
                if not prev:
                    out.append(WITHHELD)
                prev = True
            else:
                out.append(ln)
                prev = False
        text = "\n".join(out)
        notes.append("withheld %d line(s) that looked like secrets" % len(hits))
    if (engine or {}).get("local"):
        return text, notes
    text, n = _rel_paths(text, workdir)
    if n:
        notes.append("made %d workspace path(s) relative" % n)
    if S is None:
        return WITHHELD_ALL, notes + ["farm scrub rules could not load: whole result withheld"]
    try:
        text, counts, _restore = S.scrub_text(text, vault)
        if any(counts.values()):
            notes.append("scrubbed " + ", ".join("%d %s" % (v, k) for k, v in counts.items() if v))
        names = S.internal_names(text)
        for nm in sorted(names, key=len, reverse=True):
            text = re.sub(re.escape(nm), INTERNAL_PH, text, flags=re.I)
        if names:
            notes.append("replaced %d internal host name(s)/address(es)" % len(names))
        left = [t for t in S.forbidden_terms(vault) if t in text.lower()]
        for t in left:
            text = re.sub(re.escape(t), PRIVATE_PH, text, flags=re.I)
        if left:
            notes.append("replaced %d private term(s)" % len(left))
    except Exception:                      # noqa: BLE001 -- fail closed: a scrub that breaks releases nothing
        return WITHHELD_ALL, notes + ["farm scrub failed: whole result withheld"]
    return text, notes


# ---- CLI -------------------------------------------------------------------------------------

def _cmd_scan(a):
    try:
        with open(a.file, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        print("cannot read %s: %s" % (a.file, e.strerror or type(e).__name__), file=sys.stderr)
        return 1
    engine = {"name": a.engine, "local": a.engine == "local"}
    S = _send()
    hits, ok = secret_lines(text, _vault(S))
    clean, notes = scrub_tool_result(text, engine, a.workdir)
    changed = clean != text
    if a.json:
        print(json.dumps({"scanner_ran": ok, "secret_lines": sorted(hits), "notes": notes, "changed": changed}))
    else:
        if not ok:
            print("secret scan could not run: the whole result would be withheld")
        for n in sorted(hits):
            print("line %d: would be withheld (looks like a secret)" % n)
        for n in notes:
            print("note: " + n)
        if not changed:
            print("nothing would be withheld")
    return 6 if not ok else (2 if changed else 0)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_farm_egress.py", description="egress checks for farm agent mode")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("file")
    s.add_argument("--engine", default="hosted")
    s.add_argument("--workdir")
    s.add_argument("--json", action="store_true")
    d = sub.add_parser("deny")
    d.add_argument("path")
    p = sub.add_parser("policy")
    p.add_argument("base_url")
    p.add_argument("tokens", nargs="*")
    a = ap.parse_args(argv)
    if a.cmd == "scan":
        return _cmd_scan(a)
    if a.cmd == "deny":
        why = deny_read(a.path)
        print(("refused: " + why) if why else "readable")
        return 2 if why else 0
    eng = {"name": "engine", "base_url": a.base_url, "data_policy": ",".join(a.tokens)}
    print("\n".join(data_policy_summary(eng)))
    print("extra request fields: " + json.dumps(provider_extras(eng)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
