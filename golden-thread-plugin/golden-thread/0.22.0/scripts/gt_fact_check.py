#!/usr/bin/env python3
"""gt_fact_check.py -- check the claims in a vault write that code can decide (0.22.0).

    check(text, scripts_dirs=, settings=, vault=, gt_version=, receipt_tests=) -> {"status", "counts", "claims"}

String matching only: no model, no network. A claim is something this machine can look up -- a script
name in backticks, a setting named with a "setting" cue, a [[wikilink]], a path the write itself says it
just created. Each is `verified`, `failed` or `unverified`. What the machine cannot decide (a path on
another host, a decision, an opinion) is never a claim, or is `unverified`, and is never `failed`: on the
vault's existing text 55% of quoted paths do not exist here, mostly because they live on other hosts. For
the same reason a script or setting name is `failed` only when it is a near miss of a real one (a typo);
the vault is shared between machines, so a name nowhere near any known one may belong to a plugin that is
not installed here.

The result holds kinds, line numbers and counts -- never the claimed text -- so a log line or a refusal
built from it cannot echo what was written. A check that cannot run is "not-checked", never "verified".
Code fences and inline code that only illustrates syntax are not claims.

Kinds: script, setting, link, path (0.22.0), and version, tests and quote (phase 28). The last three start
at WARN in the broker and never block: a rate measured on the vault's old text cannot promote a kind (it is
lint-clean and cannot show what writers emit); promotion needs the broker log's own counts over a week.
  version  "current/installed/latest (gt) version is X.Y.Z" -- a version mentioned in passing is no claim
  tests    "N tests" beside "latest/last receipt" or "last gate" -- N must be one of the ten newest passing
           receipts' counts (machines differ), unverified when there is no receipt
  quote    a quotation of 25 or more characters followed by its source (`(path)`, `per path`, `[[Page]]`):
           verified when the source resolves and holds it (whitespace and quote style ignored; `...` splits
           it into pieces that must each appear), failed when it resolves and does not, unverified when it
           cannot be resolved or is a URL
"""
import difflib
import os
import re
import tempfile

SCRIPT = re.compile(r"`(?:[^`\s]*/)?(gt_[A-Za-z0-9_]+\.(?:py|sh))\b[^`]*`")
SETTING_CUE_BEFORE = re.compile(r"\bsetting\s+`([a-z][a-z0-9_]*)`")
SETTING_CUE_AFTER = re.compile(r"`([a-z][a-z0-9_]*)`\s+setting\b")
WIKILINK = re.compile(r"\[\[([^\]|#\\]+?)\\?(?:[|#][^\]]*)?\]\]")
INLINE_CODE = re.compile(r"`[^`]*`")
CREATED_PATH = re.compile(
    r"\b(?:created|wrote|written|saved|generated|made|installed|added)\b[^`\n]*`((?:~|/|[A-Za-z]:[\\/])[^`\s]+)`", re.I)
QUOTED_PATH = re.compile(r"`((?:~|/|[A-Za-z]:[\\/])[A-Za-z0-9_.@+/\\:-]{3,})`")
VERSION_A = re.compile(r"\b(?:current|installed|latest)\s+(?:gt\s+)?version\s+(?:of\s+gt\s+)?(?:is\s+)?v?(\d+\.\d+\.\d+)\b", re.I)
VERSION_B = re.compile(r"\bgt\s+v?(\d+\.\d+\.\d+)\s+is\s+(?:the\s+)?(?:current|installed|latest)\b", re.I)
TESTS_A = re.compile(r"(\d[\d,]*)\s+tests?\b[^.\n]{0,60}?\b(?:latest|last)\s+(?:receipt|gate)", re.I)
TESTS_B = re.compile(r"\b(?:latest|last)\s+(?:receipt|gate)\b[^.\n]{0,60}?(\d[\d,]*)\s+tests?\b", re.I)
QUOTE = re.compile(r"[\"\u201c]([^\"\u201d\n]{25,})[\"\u201d]\s*(?:\(([^)\n]+)\)|[\u2014-]+\s*\[\[([^\]|#]+)|per\s+`([^`]+)`)")
SKIP_DIRS = {".git", ".obsidian", "node_modules"}


def _local_roots(vault):
    roots = [os.path.expanduser("~"), tempfile.gettempdir(), "/private/tmp", "/tmp"]
    if vault:
        roots.append(str(vault))
    out = set()
    for r in roots:
        out.add(os.path.normpath(r))
        out.add(os.path.normpath(os.path.realpath(r)))
    return out


def _is_local(path, roots):
    p = os.path.normpath(os.path.expanduser(path))
    return any(p == r or p.startswith(r + os.sep) for r in roots)


def _link_map(vault):
    """Every name a [[wikilink]] may resolve to, the way the linter reads them (project folders,
    path-form links and attachments included), as {name: Path}; None if that cannot be worked out."""
    try:
        import gt_lint
        from pathlib import Path
        return dict(gt_lint.link_targets(Path(str(vault))))
    except Exception:                                    # noqa: BLE001
        return None


def _targets(vault):
    m = _link_map(vault)
    return None if m is None else set(m)


def _norm(text):
    """Quote style, dashes and whitespace ignored, so a quotation can be matched against its source."""
    for a, b in (("\u201c", '"'), ("\u201d", '"'), ("\u2018", "'"), ("\u2019", "'"), ("\u2013", "-"),
                 ("\u2014", "-")):
        text = text.replace(a, b)
    return " ".join(text.split())


def _quote_verdict(quote, local, page, vault):
    """verified / failed when the source resolves to a readable file; unverified otherwise."""
    src = None
    if local:
        local = local.strip().strip("`")
        if re.match(r"^[a-z][a-z0-9+.-]*://", local, re.I):
            return "unverified"
        local = re.sub(r":\d+(?:-\d+)?$", "", local)
        cand = os.path.expanduser(local)
        if os.path.isfile(cand):
            src = cand
    elif page and vault is not None:
        m = _link_map(vault) or {}
        hit = m.get(page.strip().lower())
        if hit is not None and os.path.isfile(str(hit)):
            src = str(hit)
    if src is None:
        return "unverified"
    try:
        with open(src, encoding="utf-8", errors="replace") as fh:
            body = _norm(fh.read(2_000_000))
    except OSError:
        return "unverified"
    pieces = [p.strip() for p in re.split(r"\.\.\.|\u2026", _norm(quote)) if len(p.strip()) >= 8]
    return "verified" if pieces and all(p in body for p in pieces) else "failed"


def _script_verdict(name, dirs):
    """A script that exists is verified. One that does not is `failed` only when it is a near miss of a
    script that does (a typo): the vault is shared between machines and a plugin installed on one need not
    be on another, so a name nowhere near any known script is unverified, not failed. Measured on the vault's
    own text: 0 of 512 claims fail at a 0.9 cutoff, with or without the farm plugin installed."""
    if any(os.path.isfile(os.path.join(d, name)) for d in dirs):
        return "verified"
    known = set()
    for d in dirs:
        try:
            known.update(f for f in os.listdir(d) if f.startswith("gt_"))
        except OSError:
            pass
    return "failed" if difflib.get_close_matches(name, known, n=1, cutoff=0.9) else "unverified"


def _setting_verdict(name, settings):
    """Another program's setting looks the same as one of gt's, so a name that is not in the registry
    fails only when it is a near miss of one that is (a typo); otherwise it is unverified."""
    if settings is None:
        return "unverified"
    if name in settings:
        return "verified"
    return "failed" if difflib.get_close_matches(name, settings, n=1, cutoff=0.8) else "unverified"


def _lines(text):
    """(number, line) for every line outside a code fence; a fenced line is skipped, not renumbered."""
    fenced = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield n, line


def check(text, scripts_dirs=(), settings=None, vault=None, gt_version=None, receipt_tests=None):
    if not isinstance(text, str):
        return {"status": "not-checked", "counts": None, "claims": []}
    claims = []
    stems = None
    roots = _local_roots(vault)
    dirs = [str(d) for d in scripts_dirs]
    for n, line in _lines(text):
        found = []
        for m in SCRIPT.finditer(line):
            found.append((m.start(), "script", _script_verdict(m.group(1), dirs)))
        for rx in (SETTING_CUE_BEFORE, SETTING_CUE_AFTER):
            for m in rx.finditer(line):
                found.append((m.start(), "setting", _setting_verdict(m.group(1), settings)))
        for m in WIKILINK.finditer(INLINE_CODE.sub("", line)):
            if vault is None:
                verdict = "unverified"
            else:
                if stems is None:
                    stems = _targets(vault) or False
                verdict = ("unverified" if stems is False
                           else "verified" if m.group(1).strip().lower() in stems else "failed")
            found.append((m.start(), "link", verdict))
        for rx in (VERSION_A, VERSION_B):
            for m in rx.finditer(line):
                found.append((m.start(), "version", "unverified" if not gt_version else
                              "verified" if m.group(1) == str(gt_version).lstrip("v") else "failed"))
        for rx in (TESTS_A, TESTS_B):
            for m in rx.finditer(line):
                try:
                    n = int(m.group(1).replace(",", ""))
                except ValueError:
                    continue
                found.append((m.start(), "tests", "unverified" if not receipt_tests else
                              "verified" if n in receipt_tests else "failed"))
        for m in QUOTE.finditer(line):
            found.append((m.start(), "quote", _quote_verdict(m.group(1), m.group(2) or m.group(4), m.group(3), vault)))
        made = {m.group(1) for m in CREATED_PATH.finditer(line)}
        for m in QUOTED_PATH.finditer(line):
            path = m.group(1)
            if path in made and _is_local(path, roots):
                verdict = "verified" if os.path.exists(os.path.expanduser(path)) else "failed"
            else:
                verdict = "unverified"              # the machine cannot tell "gone" from "elsewhere"
            found.append((m.start(), "path", verdict))
        for _, kind, verdict in sorted(found):
            claims.append({"kind": kind, "line": n, "verdict": verdict})
    counts = {"checked": 0, "verified": 0, "failed": 0, "unverified": 0}
    for c in claims:
        counts["checked" if c["verdict"] != "unverified" else "unverified"] += 1
        if c["verdict"] != "unverified":
            counts[c["verdict"]] += 1
    return {"status": "checked", "counts": counts, "claims": claims}
