#!/usr/bin/env python3
"""gt_secrets — find credentials that are in the wrong place, and never become the thing
that leaks them.

THE OUTPUT CONTRACT, which is the reason this is a separate tool:

    Nothing in this process ever prints matched source text, and no other check runs
    beside it.

Every other Golden Thread scanner prints the text it found, because the text IS the
finding. Here the text is the thing being protected. Those are opposite output rules, and
two tools with opposite output rules in one process is exactly how the 0.16.0 attempt
leaked: its `secrets` check printed only a length while its `naming` check, same tool and
same run, printed raw source text, so a credential-shaped identifier reached stdout and
--json in full. That is why this is not a member of gt_scan, and why registering it as one
is a test failure.

A finding is `path:line`, the rule id, and a LENGTH. Never a redaction, never a prefix,
never a first-four/last-four, and never a hash -- a hash of a short or low-entropy secret
is crackable, and a hash still confirms a guess. Core rule 3 forbids the value entering a
session at all, and "redacted" output has a way of becoming un-redacted in a log.

Exit: 0 clean (nothing NEW against the baseline) | 1 found something | 2 could not run, or
could not scan part of what it was asked to scan. Silence is never clean: every successful
run ends with an affirmative naming what was covered.

Design: Projects/golden-thread/gt-secrets/design.md in the vault.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gt_registry                                        # noqa: E402

SLOT = "secrets"
CLEAN, FOUND, CANNOT_RUN = 0, 1, 2

# Files whose NAME alone means "look inside, whatever the other rules say". Validation
# finding #5 of 2026-09-16: skips are where secrets live. Lockfiles, CI artefacts and
# .env were skipped by default and that is precisely where credentials sit.
ALWAYS_SCAN = {
    ".env", ".envrc", ".npmrc", ".pypirc", ".netrc", ".dockercfg",
    "credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
ALWAYS_SCAN_SUFFIX = (".env", ".pem", ".key", ".p12", ".pfx", ".keystore")

# Skipped unless a name above overrides. Reported, never silent.
SKIP_DIRS = {".git", "__pycache__", "node_modules", "vendor", ".venv", "venv", ".tox"}
SKIP_SUFFIX = (".pyc", ".pyo", ".so", ".dylib", ".png", ".jpg", ".jpeg", ".gif",
               ".pdf", ".zip", ".gz", ".tar", ".whl", ".ico", ".woff", ".woff2")
SIZE_CAP = 2 * 1024 * 1024

# The generic rule, which is the one that matters: a .env full of real credentials
# produced ZERO findings in 0.16.0 because only vendor-prefixed shapes were detected.
# Vendor prefixes catch the secrets that are easiest to rotate; this catches the rest.
ASSIGN = re.compile(
    r"""(?ix)
    \b(?P<key>[A-Za-z0-9_.\-]*
        (?:pass(?:wd|word)?|pwd|secret|token|api[_.\-]?key|apikey|auth|credential|cred)
      [A-Za-z0-9_.\-]*)
    \s*[:=]\s*
    (?P<q>["']?)(?P<val>[^\s"'`,;<>\\]{8,})(?P=q)
    """)
MIN_ENTROPY = 3.0
MIN_LEN = 8

# A credential-shaped assignment whose VALUE is a regular expression is a DETECTOR, not a
# credential -- "the secret scanner flags the secret scanner". Measured 2026-09-27 on this
# repo: 10 of 10 live findings were gt's own detection patterns and test fixtures.
#
# Deliberately narrow. These are constructs that cannot occur in a real credential but are
# unavoidable in a pattern, so the rule removes detectors without removing secrets. It is a
# DETECTION improvement, not a suppression: a suppressed finding is invisible, whereas this
# never fires in the first place and the reason is one grep away.
LOOKS_LIKE_A_PATTERN = re.compile(
    r"""\\[bdswBDSWAZ]     # \b \d \s \w and friends
      | \(\?[:=!<P]        # (?: (?= (?! (?< (?P
      | \[[A-Za-z0-9]-[A-Za-z0-9] # a character range: [A-Za-z] [0-9]
      | \{\d+,\d*\}          # a repetition count: {8,} {16,32}
      | \.\*|\.\+            # .* .+
    """, re.X)


# Characters that appear in CODE and never in a credential. Measured 2026-09-27 on this
# repo: of ten live generic findings, seven contained `(` or `()` -- the rule was capturing
# function CALLS such as `secret = get_password(...)` -- and two contained `$`/`${}`, i.e.
# shell variable references. None was a credential.
#
# `/` and `+` are deliberately NOT here: both are base64 alphabet characters and excluding
# them would blind the rule to the most common encoded-secret shape.
#
# This replaced an earlier "require a digit or a symbol" rule that had NO measured effect,
# because `_`, `.` and `-` are symbols and appear in every identifier. That rule was
# removed rather than left in beside this one: a predicate that never fires reads like
# coverage and is not.
CODE_CHARS = set("(){}$")


def looks_like_code(v: str) -> bool:
    return any(c in CODE_CHARS for c in v)


def looks_like_a_path(v: str) -> bool:
    """An absolute filesystem path is not a credential.

    Found 2026-09-27: the one remaining finding on this repo was a SUDOERS line --
    `... NOPASSWD: /usr/sbin/service ...` -- where the key matched because NOPASSWD
    contains PASSWD and the captured value was the command path. `NOPASSWD` is in real
    sudoers files everywhere, so this would have fired constantly.

    The test is a leading `/` PLUS a second `/`, not merely the presence of `/`: base64
    uses `/` in its alphabet, and excluding it would blind the rule to encoded secrets.
    A base64 blob beginning with `/` and containing a second `/` is possible and would be
    missed -- a known, narrow gap, and filename awareness still covers such a value inside
    `.env` or `credentials`.
    """
    return v.startswith("/") and v.count("/") >= 2


def shannon(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n)
                for c in (s.count(ch) for ch in set(s)))


# ---------------------------------------------------------------------------
# The ONLY writer. Everything reportable goes through here, so there is exactly one
# place that could ever print a value -- and it cannot, because it takes no value.
# A test asserts there is one writer; see tests/test_gt_secrets.py.
# ---------------------------------------------------------------------------
class Out:
    def __init__(self, as_json: bool):
        self.as_json = as_json
        self.findings: list[dict] = []
        self.notes: list[str] = []

    def finding(self, path: str, line: int, rule: str, length: int, entropy=None):
        rec = {"path": path, "line": line, "rule": rule, "length": length}
        if entropy is not None:
            rec["entropy"] = round(entropy, 2)
        self.findings.append(rec)

    def note(self, text: str):
        self.notes.append(text)

    def fatal(self, text: str) -> int:
        """A could-not-run, through the same writer. Takes no value and cannot carry one."""
        print(text, file=sys.stderr)
        return CANNOT_RUN

    def said(self, text: str) -> None:
        """A one-line statement about the RUN, never about a finding."""
        print(text)

    def emit(self, summary: dict) -> None:
        if self.as_json:
            print(json.dumps({"findings": self.findings, "notes": self.notes,
                              "summary": summary}, indent=2))
            return
        for f in sorted(self.findings, key=lambda r: (r["path"], r["line"])):
            extra = "" if "entropy" not in f else " entropy=%.2f" % f["entropy"]
            print("%s:%d  %s  length=%d%s"
                  % (f["path"], f["line"], f["rule"], f["length"], extra))
        for n in self.notes:
            print(n)
        print(summary["line"])


def rules_from_slot(vault):
    """Load detectors from the `secrets` slot. Returns (rules, problems, retracted)."""
    effective, _shadowed, retracted, problems = gt_registry.resolve(SLOT, vault=vault)
    rules = []
    for rec in effective:
        e = rec["entry"]
        pat = e.get("pattern")
        if not pat:
            problems.append((rec["path"], "secrets entry %r has no pattern" % e.get("id")))
            continue
        try:
            rules.append({"id": e["id"], "re": re.compile(pat), "source": rec["source"]})
        except re.error as exc:
            problems.append((rec["path"], "secrets entry %r: bad pattern (%s)" % (e["id"], exc)))
    return rules, problems, retracted


def in_scope(p: Path, rel: str, excludes=()):
    """-> (scan?, reason_if_not). Name-based inclusion beats every skip -- including an
    explicit --exclude. A .env is never out of scope because a glob said so; that is
    validation finding #5 (skips are where secrets live) and an --exclude must not become
    a way to hide one.
    """
    name = p.name
    if name in ALWAYS_SCAN or name.endswith(ALWAYS_SCAN_SUFFIX):
        return True, None
    for g in excludes:
        if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.rstrip("/") + "/*"):
            return False, "excluded by %s" % g
    if p.suffix in SKIP_SUFFIX:
        return False, "binary/generated suffix"
    try:
        if p.stat().st_size > SIZE_CAP:
            return False, "over the size cap"
    except OSError as exc:
        return False, "unreadable (%s)" % exc.__class__.__name__
    return True, None


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            yield Path(dirpath) / fn


# Sentinels for scan_file, so the caller can tell the two apart. Conflating them meant a
# single .DS_Store made every run exit 2 "could not scan part of what it was asked to" --
# a permanent false failure that would have made this gate the first thing anyone disabled.
BINARY, UNREADABLE = -1, -2


def scan_file(p: Path, rel: str, rules, out: Out) -> int:
    """-> hit count, or BINARY (not text, nothing to scan) or UNREADABLE (a real failure).

    "I chose not to read this" and "I could not read this" are different facts. Binary is
    the first: a .DS_Store or a compiled object has no lines to match and skipping it costs
    nothing. Permission denied is the second, and it means the scan has a hole in it.
    """
    try:
        text = p.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return BINARY
    except OSError:
        return UNREADABLE
    hits = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        for r in rules:
            # finditer, not search: 0.16.0 reported ONE finding for five tokens on a
            # minified line, because it stopped at the first match per pattern per line.
            for m in r["re"].finditer(line):
                out.finding(rel, lineno, r["id"], len(m.group(0)))
                hits += 1
        for m in ASSIGN.finditer(line):
            val = m.group("val")
            if len(val) < MIN_LEN:
                continue
            if LOOKS_LIKE_A_PATTERN.search(val):
                continue
            if looks_like_code(val) or looks_like_a_path(val):
                continue
            ent = shannon(val)
            if ent < MIN_ENTROPY:
                continue
            out.finding(rel, lineno, "generic.assigned-credential", len(val), ent)
            hits += 1
    return hits


def load_baseline(path: Path):
    if not path or not path.is_file():
        return None
    try:
        return {tuple(x) for x in json.loads(path.read_text())["accepted"]}
    except Exception:
        return None


def key_of(f):
    # path + rule + length. NOT the line: a finding that only moved is the same finding.
    # NOT the value, by the output contract -- but the LENGTH is carried, so a rotated
    # credential of a different length resurfaces rather than staying accepted.
    return (f["path"], f["rule"], f["length"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="find credentials in the wrong place")
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--vault")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip paths matching GLOB (repeatable). Counted and reported; a "
                         "file whose NAME means credentials is scanned anyway.")
    ap.add_argument("--baseline", help="accepted findings; report only what is new")
    ap.add_argument("--write-baseline", metavar="FILE",
                    help="record today's findings as accepted and exit")
    a = ap.parse_args(argv)

    root = Path(a.path).resolve()
    out = Out(a.json)
    rules, problems, retracted = rules_from_slot(a.vault)

    for where, what in problems:
        out.note("PROBLEM %s: %s" % (where, what))
    for rec in retracted:
        out.note("retracted by %s: %s" % (rec.get("retracted_by"), rec["entry"].get("id")))

    # The emptiness guard sits on the SLOT, not the registry. 0.16.0's guard covered the
    # whole registry, so silencing one noisy pattern could disable credential scanning
    # entirely and still exit 0.
    if not rules:
        return out.fatal(
            "gt-secrets: CANNOT RUN — the `secrets` slot resolved to 0 pattern(s). The "
            "generic rule alone is not a credential scan. Check `gt_registry.py sources`.")

    scanned = unreadable = skipped = excluded = binary = 0
    for p in walk(root):
        rel = str(p.relative_to(root))
        ok, why = in_scope(p, rel, a.exclude)
        if not ok and why and why.startswith("excluded by"):
            excluded += 1
            continue
        if not ok:
            skipped += 1
            continue
        n = scan_file(p, rel, rules, out)
        if n == UNREADABLE:
            unreadable += 1
        elif n == BINARY:
            binary += 1
        else:
            scanned += 1

    base = load_baseline(Path(a.baseline)) if a.baseline else None
    if a.baseline and base is None:
        out.note("PROBLEM baseline %s could not be read — reporting every finding" % a.baseline)
    if base is not None:
        out.findings = [f for f in out.findings if key_of(f) not in base]

    if a.write_baseline:
        Path(a.write_baseline).write_text(json.dumps(
            {"accepted": sorted(key_of(f) for f in out.findings)}, indent=2) + "\n")
        out.said("gt-secrets: recorded %d finding(s) as accepted in %s"
                 % (len(out.findings), a.write_baseline))
        return CLEAN

    parts = ["%d file(s) scanned" % scanned, "%d rule(s)" % len(rules),
             "%d new finding(s)" % len(out.findings)]
    if skipped:
        parts.append("%d skipped" % skipped)
    if excluded:
        # Named, not silent: an --exclude that quietly removed half the tree is the
        # difference between a scan and the appearance of one.
        parts.append("%d excluded by %d glob(s)" % (excluded, len(a.exclude)))
    if binary:
        parts.append("%d binary" % binary)
    if unreadable:
        # Capitalised because it is the one that means the scan has a HOLE in it.
        parts.append("%d UNREADABLE" % unreadable)
    verdict = "clean" if not out.findings else "findings"
    summary = {"line": "gt-secrets: %s — %s" % (verdict, ", ".join(parts)),
               "scanned": scanned, "rules": len(rules), "skipped": skipped,
               "unreadable": unreadable, "binary": binary, "excluded": excluded,
               "excludes": list(a.exclude), "new": len(out.findings)}
    out.emit(summary)
    if unreadable or any(n.startswith("PROBLEM") for n in out.notes):
        return CANNOT_RUN
    return FOUND if out.findings else CLEAN


if __name__ == "__main__":
    sys.exit(main())
