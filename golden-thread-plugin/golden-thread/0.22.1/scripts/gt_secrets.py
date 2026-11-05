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

THE BASELINE (0.20.1) is the one place a hash is kept, and it is never printed: an accepted
finding is stored as its rule, its path with release-version segments as <ver>, and a
PBKDF2-HMAC-SHA256 of the normalised line (200,000 rounds, salted with rule and path;
gt_baseline.py) -- never the text, the value or a fast digest -- so a version cut does not
re-flag accepted lines. The lines this tool flags are long, high-entropy values by its own
rules, which is what makes a slow salted hash of the whole line an acceptable thing to keep.

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
import shutil
import subprocess
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gt_registry                                        # noqa: E402
import gt_baseline                                        # noqa: E402
import gt_staged                                          # noqa: E402

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
#
# Matching is anchored on the separator, not on the key (0.22.0). The earlier single pattern began
# its key at every word boundary and was quadratic on a long line of letters, dots and dashes (a
# minified file, 16 KB took 6 s, 100 KB never finished) in every hook that scans a paste or a
# result. Now: a cheap check that a keyword is on the line at all, then each `:` or `=` followed by
# a value, then a bounded walk to the left over the key. Linear in the line, a few hundred
# characters of walk per separator at most.
ASSIGN_HINT = re.compile(
    r"pass|pwd|secret|token|api[_.\-]?key|apikey|auth|credential|cred", re.I)     # (the keywords)
ASSIGN_SEP = re.compile(r"[:=]")
ASSIGN_VAL = re.compile(r"""\s*(?P<q>["']?)(?P<val>[^\s"'`,;<>\\]{8,})(?P=q)""")
ASSIGN_KEY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
ASSIGN_KEY_MAX = 200    # the walk left over the key stops here: `password` + 250 letters + `=value` is no longer flagged (the old pattern did flag it);
                        # a real key name is never this long, and the bound is what keeps the matcher linear
MIN_ENTROPY = 3.0
MIN_LEN = 8


def assign_values(line: str):
    """-> the values of every `<key containing a credential word> = <value>` on one line, in order.
    The key is the run of letters, digits, `_`, `.` and `-` just left of an optional quote and
    optional spaces before the `:` or `=`. A separator whose key has no credential word consumes
    nothing, so a later `apikey=...` inside the same run of text is still found, as it was before."""
    if not ASSIGN_HINT.search(line):
        return []
    out, cursor = [], 0
    for sep in ASSIGN_SEP.finditer(line):
        i = sep.start()
        if i < cursor:
            continue
        j = i
        while j > cursor and line[j - 1].isspace():
            j -= 1
        if j > cursor and line[j - 1] in "\"'":
            j -= 1
        k = j
        while k > cursor and j - k < ASSIGN_KEY_MAX and line[k - 1] in ASSIGN_KEY_CHARS:
            k -= 1
        if k == j or not ASSIGN_HINT.search(line, k, j):
            continue
        m = ASSIGN_VAL.match(line, i + 1)
        if m:
            out.append(m.group("val"))
            cursor = m.end()
    return out


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
        # The matched LINE of each finding, held in memory only to hash it for the baseline
        # (gt_baseline.slow_hash). Never in a finding record, so never printed or in --json.
        self._lines: dict[int, str] = {}
        self.notes: list[str] = []

    def finding(self, path: str, line: int, rule: str, length: int, entropy=None,
                text=None):
        rec = {"path": path, "line": line, "rule": rule, "length": length}
        if entropy is not None:
            rec["entropy"] = round(entropy, 2)
        self.findings.append(rec)
        if text is not None:
            self._lines[id(rec)] = text

    def line_hash(self, rec):
        """The baseline key's hash for a finding (PBKDF2, salted; never the text)."""
        return gt_baseline.slow_hash(rec["rule"], rec["path"], self._lines.get(id(rec), ""))

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
        for rule, length, ent in line_hits(line, rules):
            out.finding(rel, lineno, rule, length, ent, text=line)
            hits += 1
    return hits


# A line longer than LINE_WINDOW_OVER is read in windows of LINE_WINDOW characters that overlap by LINE_OVERLAP, so
# the work stays linear in the line however a pattern behaves on it (0.22.0: two shipped patterns were quadratic on
# a long run of letters and dashes). A match shorter than LINE_OVERLAP that starts in a window lies whole inside it.
LINE_WINDOW_OVER = 65536
LINE_WINDOW = 32768
LINE_OVERLAP = 8192


def line_hits(line: str, rules):
    """-> [(rule id, matched length, entropy or None)] for ONE line: the single place the
    detectors are applied (scan_file, scan_text and the canary all come through here, so a
    detector that stops firing stops for all three and the canary sees it). Never the text.
    A line over LINE_WINDOW_OVER characters is scanned in overlapping windows; a hit seen twice in an overlap counts once."""
    if len(line) <= LINE_WINDOW_OVER:
        return _line_hits(line, rules, 0, None)
    found, seen = [], set()
    for start in range(0, len(line), LINE_WINDOW):
        found.extend(_line_hits(line[start:start + LINE_WINDOW + LINE_OVERLAP], rules, start, seen))
    return found


def _line_hits(line, rules, base, seen):
    """One window (or a whole short line) of line_hits. `seen` is None for a whole line; for a window it holds what
    earlier windows already reported, keyed by rule and absolute position (a value, for the assigned-credential rule)."""
    found = []
    for r in rules:
        # finditer, not search: 0.16.0 reported ONE finding for five tokens on a minified
        # line, because it stopped at the first match per pattern per line.
        for m in r["re"].finditer(line):
            if seen is not None:
                key = (r["id"], base + m.start())
                if key in seen:
                    continue
                seen.add(key)
            found.append((r["id"], len(m.group(0)), None))
    for val in assign_values(line):
        if seen is not None:
            if ("assign", val) in seen:
                continue
            seen.add(("assign", val))
        if len(val) < MIN_LEN:
            continue
        if LOOKS_LIKE_A_PATTERN.search(val):
            continue
        if looks_like_code(val) or looks_like_a_path(val):
            continue
        ent = shannon(val)
        if ent < MIN_ENTROPY:
            continue
        found.append(("generic.assigned-credential", len(val), ent))
    return found


PEM_RULE = "private-key.block"
PEM_RULE_CAPPED = "private-key.block-capped"      # the last line withheld when the key ran past PEM_MAX_LINES
PEM_BEGIN = re.compile(r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?)-----")      # group 1: the kind
PEM_END = re.compile(r"-----END ([A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?)-----")
PEM_B64 = re.compile(r"[A-Za-z0-9+/=_\-]+")                            # one line of key body
PEM_HEADER = re.compile(r"[A-Za-z0-9\-]+: .*")                         # any `Word: text` line inside a key that has an END
PEM_HEADER_KNOWN = re.compile(                                          # the ones a key carries: encryption and armor headers
    r"(?:Proc-Type|DEK-Info|Content-Domain|Key-Info|MIC-Info|Version|Comment|Hash|Charset|MessageID): .*")
PEM_PREFIX = re.compile(r"[>#;*\-|\s\"'`,\[\]]+")                     # quote, comment, bullet, table, JSON array
PEM_SUFFIX = "\"'`,;]|"
PEM_KEY_LINE = 40          # a body line is at least this long (real ones are 64 or 76) ...
PEM_MAX_LINES = 20000      # ... and no key is read past this many lines


def _pem_core(line):
    """The line without the prefix characters a quote, comment, bullet, table or JSON array puts in
    front of it and the quote, comma or bracket after it."""
    m = PEM_PREFIX.match(line)
    s = line[m.end():] if m else line
    return s.rstrip().rstrip(PEM_SUFFIX).rstrip()


def _pem_tail(s):
    """True for a short last line of key data: base64 with a digit, `+`, `/` or `=` in it, which an
    ordinary word is not."""
    return len(s) >= 4 and re.search(r"[0-9+/=]", s) is not None


PEM_RUN = re.compile(r"[A-Za-z0-9+/=_\-]{%d,}" % 40)      # a stretch that looks like key data, whatever surrounds it
PEM_WORD = re.compile(r"[A-Za-z][a-z]+[.,;:!?]?")          # an ordinary lowercase word
PEM_PROSE_WORDS = 4        # a line is prose when it holds this many tokens, most of them ordinary words, and no key-looking run


def _pem_is_prose(raw):
    """True only for a line that is clearly ordinary writing: no stretch of key-looking characters, and at least
    PEM_PROSE_WORDS space-separated tokens of which most are lowercase words (`(truncated)`, `...`, a label and a
    short base64 tail, a log prefix with a key row after it are all NOT prose). Conservative on purpose: a key body
    must always win over a guess that a line is a sentence."""
    if PEM_RUN.search(raw):
        return False
    tokens = raw.split()
    if len(tokens) < PEM_PROSE_WORDS:
        return False
    words = sum(1 for tok in tokens if PEM_WORD.fullmatch(tok))
    return words >= PEM_PROSE_WORDS and words * 3 >= len(tokens) * 2


def _pem_end_line(lines, begin, stop, kind, marks):
    """-> the 1-based number of the END line of the same kind within `stop`, or None when the key is not closed
    there (another BEGIN comes first, or no END before the cap). `marks` is the sorted list of line numbers that
    contain `-----` (built once per scan_text), so a long text with many headers is not walked line by line."""
    if marks is None:
        marks = [n for n in range(begin + 1, stop + 1) if "-----" in lines[n - 1]]
    from bisect import bisect_right
    for n in marks[bisect_right(marks, begin):]:
        if n > stop:
            return None
        raw = lines[n - 1]
        if PEM_BEGIN.search(raw):
            return None
        m = PEM_END.search(raw)
        if m and (kind is None or m.group(1) == kind):
            return n
    return None


def pem_block(lines, begin, marks=None):
    """-> (the 1-based number of the last line of the PEM private key that begins on line `begin`,
    whether the PEM_MAX_LINES cap cut it short).

    With an END line of the same kind (and no other BEGIN before it) every line through it is part of the key,
    whatever each line carries in front of or behind its data (`> `, `// `, `% `, `REM `, a log or syslog prefix,
    a number, a string literal, a box-drawing bar): the prefixes are not listed, only clearly ordinary writing is
    left out (see _pem_is_prose) and a line such as `(truncated)` is not writing. If a sentence sits between BEGIN
    and END, the END is taken as someone else's and the key is read as below. Without an END (a paste cut off, or
    a header quoted in prose) the key is the header and the unbroken run of lines after it that look like key data: base64 of
    PEM_KEY_LINE characters or more, a known key header (`Proc-Type:`, `DEK-Info:`, `Version:` ...), and one short last line. The run
    stops at the first blank line once body has started, and at the first line of ordinary words."""
    last, run_open, seen_body = begin, True, False
    stop = min(len(lines), begin + PEM_MAX_LINES)
    m0 = PEM_BEGIN.search(lines[begin - 1])
    end = _pem_end_line(lines, begin, stop, m0.group(1) if m0 else None, marks)
    if end is not None and not any(_pem_is_prose(lines[n - 1]) for n in range(begin + 1, end)):
        return end, False       # closed key, no sentence inside it: everything through END, whatever each line is prefixed with
    for n in range(begin + 1, stop + 1):
        raw = lines[n - 1]
        if PEM_END.search(raw):
            return n, False
        s = _pem_core(raw)
        if not s:
            if seen_body:
                run_open = False
            continue
        if PEM_HEADER.fullmatch(s):
            if run_open and PEM_HEADER_KNOWN.fullmatch(s):
                last = n
            continue
        if PEM_B64.fullmatch(s):
            if len(s) >= PEM_KEY_LINE:
                seen_body = True
                if run_open:
                    last = n
            else:
                if run_open and seen_body and _pem_tail(s):
                    last = n
                run_open = False
            continue
        break                                   # ordinary words: the key, if it was one, is over
    else:
        return last, stop - begin >= PEM_MAX_LINES
    return last, False


def pem_block_end(lines, begin):
    """-> the 1-based number of the last line of the PEM private key that begins on line `begin`."""
    return pem_block(lines, begin)[0]


def _shortest_hit(rules):
    """-> the length below which no detector can match a line: the shorter of the generic rule's
    least match (a 3-letter keyword, a separator, an 8-character value: 12) and the narrowest
    pattern in `rules`. 0 when a width cannot be read, which skips nothing."""
    floor = 12
    try:
        try:
            from re import _parser as sp
        except ImportError:                                 # Python before 3.11
            import sre_parse as sp
        for r in rules:
            floor = min(floor, sp.parse(r["re"].pattern, r["re"].flags).getwidth()[0])
    except Exception:                                       # noqa: BLE001
        return 0
    return floor


def scan_text(text: str, rules):
    """-> [(line number, rule id)] for credential-shaped text held in memory (0.21.0).

    The same detectors scan_file applies -- the `secrets` slot's patterns and the generic
    assigned-credential rule -- for a caller that has a string and not a file: the I/O brokers
    (gt_io_broker) scan what disk_read and net_fetch are about to return and withhold it. It
    returns where and which, never the matched text, so nothing downstream can print a value.

    A PEM private key spans many lines and only its first line names it, so a hit on that line
    (or on any `-----BEGIN ... PRIVATE KEY-----` line, including ENCRYPTED) reports every line
    through the matching END line as well (0.22.0): the callers mask each reported line, and the
    body of a key must not survive the header. See pem_block.
    """
    hits = []
    lines = text.splitlines()
    floor = _shortest_hit(rules)           # a line shorter than any detector's least match costs nothing
    marks = None                           # the lines containing `-----`, built on the first key header
    lineno = 0
    while lineno < len(lines):
        lineno += 1
        line = lines[lineno - 1]
        if len(line) < floor:
            continue
        found = line_hits(line, rules)
        if PEM_BEGIN.search(line) or any(rule == PEM_RULE for rule, _l, _e in found):
            if marks is None:
                marks = [i + 1 for i, ln in enumerate(lines) if "-----" in ln]
            last, capped = pem_block(lines, lineno, marks)
            for n in range(lineno, last + 1):
                hits.append((n, PEM_RULE_CAPPED if capped and n == last else PEM_RULE))
            lineno = last
            continue
        for rule, _length, _ent in found:
            hits.append((lineno, rule))
    return hits


# ---------------------------------------------------------------------------
# THE POSITIVE-DETECTION CANARY (0.21.0, request 2026-10-08-secrets-scan-positive-detection-
# canary). "The patterns loaded" (the zero-patterns check) is weaker than "a planted secret is
# caught": a scanner can load patterns and still never fire, and the one found dead on a real
# machine said nothing. The canary builds a fake token shaped like a shipped rule, scans it with
# the SAME line detectors as every other scan, and must see it flagged.
#
# The token is assembled from fragments at RUN TIME and held only in memory: it is never written
# to a file, a log or the output, and the tool prints only the rule id it was shaped like. So no
# scanned file, and no scan of this file, ever contains it whole. Nothing is staged in a
# repository or the vault -- there is no file to stage.
# ---------------------------------------------------------------------------
CANARY_PASS, CANARY_CANNOT_RUN, CANARY_MISSED = 0, 2, 3


def canary_candidates():
    """-> [(shipped rule id, token builder)], most reliable first. Each token is assembled from
    fragments and matches ONLY its own rule's shape, so removing that rule leaves it uncaught
    (the generic assigned-credential rule needs an assignment, which a bare token is not)."""
    pad = "GTCANARY"
    return [
        ("aws.access-key-id", lambda: "".join(["AK", "IA", pad, "0" * 8])),
        ("github.token", lambda: "".join(["gh", "p_", pad.lower() * 5])),
        ("gitlab.token", lambda: "".join(["gl", "pat", "-", pad.lower() * 3])),
        ("slack.token", lambda: "".join(["xo", "xb", "-", "0123456789", pad])),
        ("npm.token", lambda: "".join(["np", "m_", pad.lower() * 5])),
    ]


def run_canary(rules):
    """-> (status, rule id the token was shaped like or None, number of rules).

    CANARY_PASS the token was flagged | CANARY_CANNOT_RUN no rules at all (the zero-patterns
    condition, reported by the caller as that) | CANARY_MISSED rules loaded and the planted
    token was NOT flagged: the scanner is not protecting anything."""
    if not rules:
        return CANARY_CANNOT_RUN, None, 0
    ids = {r["id"] for r in rules}
    for rid, build in canary_candidates():
        if rid in ids:
            token = build()
            hit = any(rule == rid for _l, rule in scan_text("canary = 1\n" + token + "\n", rules))
            return (CANARY_PASS if hit else CANARY_MISSED), rid, len(rules)
    # No vendor rule the canary knows is in the set (a vault retracted them all): fall back to
    # the generic assigned-credential rule, which needs an assignment and a high-entropy value.
    token = "".join(["Zk9q", "P2wX", "7bV4", "nM8t", "R1sL", "6yH3"])
    hit = any(rule == "generic.assigned-credential"
              for _l, rule in scan_text("api_" + "token" + " = " + token + "\n", []))
    return (CANARY_PASS if hit else CANARY_MISSED), "generic.assigned-credential", len(rules)


def load_baseline(path: Path):
    """-> gt_baseline.Baseline (content entries, plus 0.19-era path-keyed tuples), or None."""
    if not path or not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not ("accepted" in doc or "entries" in doc):
            return None
        return gt_baseline.Baseline(doc)
    except Exception:
        return None


def key_of(f):
    # The 0.19-era key, still honoured from an old baseline: path + rule + length. Since 0.20.1
    # a baseline is CONTENT-keyed (gt_baseline): rule, the path with its release-version
    # segment normalised, and a salted PBKDF2 of the line -- so a version cut that copies a
    # file to golden-thread/<new>/ does not re-flag lines the owner already accepted, while an
    # edited line, a rotated credential, or the same text in another file still does.
    return (f["path"], f["rule"], f["length"])


def main(argv=None):
    """Wrapper whose only job is to guarantee the temp dir dies.

    --staged writes STAGED BLOB CONTENT to disk, and that content is exactly what this
    tool exists to find. Leaving it in /tmp for the OS to reap eventually would mean the
    credential scanner is itself a way credentials end up in a world-readable place --
    the same class of defect that cut 0.16.0. mkdtemp gives 0700; this makes it temporary
    in fact and not merely in name, on every exit path including a fatal one.
    """
    tmps = []
    try:
        return _main(argv, tmps)
    finally:
        for t in tmps:
            shutil.rmtree(t, ignore_errors=True)


def _main(argv, tmps):
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
    ap.add_argument("--staged", action="store_true",
                    help="scan what is STAGED for commit in the git repo at `path`, not the "
                         "working tree. Exit 1 blocks the commit.")
    ap.add_argument("--canary", action="store_true",
                    help="plant a fake token (built at run time, held in memory only) shaped "
                         "like a shipped rule and prove the scan flags it. Exit 0 caught | 2 no "
                         "patterns resolved (the zero-patterns condition) | 3 patterns loaded "
                         "but the planted token was NOT caught")
    ap.add_argument("--count-rules", action="store_true",
                    help="print how many credential patterns the `secrets` slot resolves and "
                         "exit: 0 when there is at least one, 2 when there are none (the "
                         "check gt_doctor runs against the installed copy)")
    a = ap.parse_args(argv)

    root = Path(a.path).resolve()
    out = Out(a.json)
    if a.count_rules:
        rules, _problems, _retracted = rules_from_slot(a.vault)
        out.said(str(len(rules)))
        return CLEAN if rules else CANNOT_RUN
    if a.canary:
        rules, _problems, _retracted = rules_from_slot(a.vault)
        status, rid, n = run_canary(rules)
        if status == CANARY_CANNOT_RUN:
            return out.fatal("gt-secrets canary: CANNOT RUN — the `secrets` slot resolved to 0 "
                             "pattern(s), so there is nothing to test (the zero-patterns "
                             "condition, not a missed canary).")
        if status == CANARY_MISSED:
            out.fatal("gt-secrets canary: NOT CAUGHT — %d pattern(s) loaded, but a "
                             "planted token shaped like %s was NOT flagged: the scanner "
                             "is not protecting anything even though its patterns resolved "
                             "(a dead detector, not the zero-patterns condition)." % (n, rid))
            return CANARY_MISSED
        out.said("gt-secrets canary: caught — a token built at run time, shaped like %s, was "
                 "flagged by the %d loaded rule(s); it was held in memory only" % (rid, n))
        return CANARY_PASS
    if a.staged:
        rels = gt_staged.staged_paths(root)
        if rels is None:
            return out.fatal("gt-secrets: CANNOT RUN — `%s` is not a git repository, so there "
                             "is no index to scan." % root)
        if not rels:
            # Nothing staged is not a pass to celebrate, but it IS clean: there is no
            # content to carry a credential. Said out loud so an empty run is never
            # mistaken for a scan that happened.
            out.said("gt-secrets: clean — nothing staged for commit, 0 file(s) scanned")
            return CLEAN
        tmp = tempfile.mkdtemp(prefix="gt-secrets-staged-")
        tmps.append(tmp)
        root = Path(tmp)
        gt_staged.materialise(Path(a.path).resolve(), rels, root)
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
            "gt-secrets: CANNOT RUN — the `secrets` slot resolved to 0 pattern(s), so the "
            "scan did NOT run and NOTHING was checked: this is not a clean result (exit 2). "
            "The generic rule alone is not a credential scan. Check `gt_registry.py sources`.")

    scanned = unreadable = skipped = excluded = binary = 0
    for p in walk(root):
        rel = p.relative_to(root).as_posix()
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
        out.findings = [f for f in out.findings
                        if not base.accepts(key_of(f), f["rule"], f["path"], out.line_hash(f))]

    if a.write_baseline:
        # The content-keyed format (0.20.1). It holds rule, normalised path, a salted PBKDF2 of
        # the line and a count -- never the line, the value, or a fast hash of either.
        for f in out.findings:
            f["_hash"] = out.line_hash(f)
        doc = {"format": gt_baseline.FORMAT, "entries": gt_baseline.entries_for(out.findings),
               "_note": "Content-keyed (gt_baseline.py): rule, the path with release-version "
                        "segments as <ver>, and a salted PBKDF2 of the normalised line. No "
                        "matched text is stored."}
        for f in out.findings:
            f.pop("_hash", None)
        Path(a.write_baseline).write_bytes((json.dumps(doc, indent=2) + "\n").encode("utf-8"))
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
