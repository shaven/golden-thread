#!/usr/bin/env python3
"""gt_spill_kinds.py -- what KIND of output is this, and how should its spill preview shorten it (0.22.0).

    shorten(lines, budget=None) -> None | (kind, items, note)
        lines  the output's lines, already masked for credentials (gt_spill masks before it calls this)
        budget the most items the caller will show (json fits itself to it); default JSON_BUDGET
        kind   one of KINDS
        items  [(first line, last line, text)], numbered as in the spill file; one item is one shown
               line, or a run of lines folded into one
        note   one sentence for the preview header: what was folded away

WHY. The spill preview used to show the first 25 lines, the last 40 and up to 20 lines that look like
errors. For a 2,000-line test run that is right only by luck: the passing lines fill the ends and the one
failure sits in the middle. Knowing the output is a test run lets the preview fold the passing lines away
and keep the failure and the verdict; knowing it is a log lets it fold a run of near-identical lines.
The full text is always in the spill file, so nothing is lost; this decides only what the model reads first.

THE IDEA is hystax/optscale-kompressor's (MIT; README read, no code read): an ordered chain of detection
rules, strictest first, because a loose rule placed early claims payloads it shortens badly; one
shortener per kind; and four rules every result must obey, pinned in tests/test_gt_spill.py and
tests/test_gt_spill_kinds.py:
    1. a result is never larger than its input (here: it must save MIN_SAVING of the lines, else None
       and the ends preview shows the output; gt_spill also drops any preview not smaller than the output),
    2. every shrink is accounted for (a folded run says `(xN)`, folded passing lines are counted in the note),
    3. the same input gives the same result (no clock, no randomness, no dict-order dependence),
    4. a failure falls back (`shorten` never raises: any error returns None, the ends preview shows).

DETECTION reads at most the first DETECT_BYTES of the output, plus its last TAIL_LINES lines (a test
runner's verdict is at the end). A kind that is detected but saves too little returns None; later kinds are
NOT tried, because the first kind that claims an output has read it more closely than the ones below.
Stdlib only; Python 3.8+.
"""
import json
import re

DETECT_BYTES = 1024 * 1024
TAIL_LINES = 60
MIN_SAVING = 0.20                      # a kind must remove this share of the lines, else the ends preview shows
MAX_JSON_BYTES = 4 * 1024 * 1024       # a larger output is not parsed
JSON_BUDGET = 60                       # items a json rendering may have when the caller gives no budget
# The order is the detection order, strictest first: a kind with a signature that cannot appear by
# accident (a parsed JSON document, `diff --git`) goes before one that is a majority vote over lines.
KINDS = ("json", "diff", "test-output", "grep", "log")


# ---- test output ---------------------------------------------------------------------------

# A line that says one test passed. Strict on purpose: a failure line must never match.
PASS_RES = tuple(re.compile(p) for p in (
    r"^\S+::\S+ PASSED\b",                                   # pytest -v
    r"^\S+\.py \.+\s*(\[\s*\d+%\])?$",                       # pytest progress, dots only
    r"^\S.* \.\.\. ok$",                                     # unittest -v, cargo test
    r"^\s*--- PASS: ",                                       # go test -v
    r"^=== (RUN|PAUSE|CONT)\s",                              # go test -v
    r"^PASS \S",                                             # jest
    r"^ok\s+\S+\s+[\d.]+s\b",                                # go test, one package
    r"^ok \S+",                                              # tests/run.sh
))
# A line a test runner prints once, at the start or the end.
SUMMARY_RES = tuple(re.compile(p) for p in (
    r"^=+ test session starts =+",
    r"^collected \d+ items?",
    r"^=+ .*\b(passed|failed|errors?|skipped|deselected)\b.* in [\d.]+s",
    r"^Ran \d+ tests? in\b",
    r"^(OK|FAILED) ?(\(|$)",
    r"^Test Suites?:",
    r"^Tests:\s",
    r"^test result: (ok|FAILED)\.",
    r"^running \d+ tests?",
    r"^(FAIL|ok)\s+\S+\s+[\d.]+s\b",
    r"^gt-gates:",
))


def _is_pass(line):
    return any(r.search(line) for r in PASS_RES)


def _is_summary(line):
    return any(r.search(line) for r in SUMMARY_RES)


def _claim_test_output(window, tail, lines):
    if any(_is_summary(x) for x in window[:200]) or any(_is_summary(x) for x in tail):
        return True
    return sum(1 for x in window if _is_pass(x)) >= 5


def _shorten_test_output(lines, budget, claimed):
    items, passed = [], 0
    for i, x in enumerate(lines):
        if _is_pass(x):
            passed += 1
        else:
            items.append((i + 1, i + 1, x))
    if passed < len(lines) * MIN_SAVING:
        return None
    if not items:                                            # every line passed (an output cut before its verdict)
        keep = sorted(set([0, 1, len(lines) - 2, len(lines) - 1]) & set(range(len(lines))))
        return "test-output", [(i + 1, i + 1, lines[i]) for i in keep], (
            "all %s lines are passing results; only the first and last two are listed" % format(len(lines), ","))
    return "test-output", items, "%s passing line%s folded away" % (format(passed, ","), "" if passed == 1 else "s")


# ---- logs ----------------------------------------------------------------------------------

_TS = r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?"
LOG_START = re.compile(
    r"^\s*\[?(?:" + _TS + r"|\d\d:\d\d:\d\d|\d\d?/\w{3}/\d{4}|\w{3} [ \d]\d \d\d:\d\d:\d\d"
    r"|(?:TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERROR|FATAL|CRITICAL)\b)")
_MASKS = (
    (re.compile(_TS), "T"),
    (re.compile(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{6,}\b"), "H"),
    (re.compile(r"\d+"), "#"),
)


def _normal(line):
    for rx, sub in _MASKS:
        line = rx.sub(sub, line)
    return line


def _claim_log(window, tail, lines):
    return len(window) >= 10 and sum(1 for x in window if LOG_START.search(x)) * 2 >= len(window)


def _shorten_log(lines, budget, claimed):
    items, prev, start, count, first = [], None, 0, 0, ""

    def close():
        if count:
            text = first if count == 1 else "%s  (x%s)" % (first, format(count, ","))
            items.append((start, start + count - 1, text))

    for i, x in enumerate(lines):
        n = _normal(x)
        if n == prev and count:
            count += 1
            continue
        close()
        prev, start, count, first = n, i + 1, 1, x
    close()
    if len(items) > len(lines) * (1 - MIN_SAVING):
        return None
    return "log", items, "%s lines folded into %s runs (a run is lines equal once numbers and times are masked)" % (
        format(len(lines), ","), format(len(items), ","))


# ---- json ----------------------------------------------------------------------------------

def _claim_json(window, tail, lines):
    """-> the parsed document, or None. Strict: the WHOLE output must parse (a log that merely starts
    with a bracket does not)."""
    first = next((x for x in lines if x.strip()), "")
    last = next((x for x in reversed(lines) if x.strip()), "")
    if first.lstrip()[:1] not in ("{", "[") or last.rstrip()[-1:] not in ("}", "]"):
        return None
    text = "\n".join(lines)
    if len(text) > MAX_JSON_BYTES:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    return (doc,) if isinstance(doc, (dict, list)) else None


def _common_keys(items):
    keys = None
    for x in items:
        if not isinstance(x, dict):
            return None
        keys = list(x) if keys is None else [k for k in keys if k in x]
    return keys


def _reduce(v, k, depth):
    if isinstance(v, dict):
        if depth <= 0:
            return "{... %s key%s}" % (format(len(v), ","), "" if len(v) == 1 else "s")
        return {key: _reduce(val, k, depth - 1) for key, val in v.items()}
    if isinstance(v, list):
        if depth <= 0:
            return "[... %s item%s]" % (format(len(v), ","), "" if len(v) == 1 else "s")
        kept = [_reduce(x, k, depth - 1) for x in v[:k]]
        if len(v) > k:
            keys = _common_keys(v[:200])
            what = "objects with keys " + ", ".join(keys[:8]) if keys else "items"
            kept.append("... %s more %s" % (format(len(v) - k, ","), what))
        return kept
    return v


def _shorten_json(lines, budget, claimed):
    doc = claimed[0]
    budget = budget or JSON_BUDGET
    size = sum(len(x) + 1 for x in lines)
    for k in (3, 2, 1, 0):
        for depth in (8, 6, 4, 3, 2, 1):
            shown = json.dumps(_reduce(doc, k, depth), indent=2, ensure_ascii=False).split("\n")
            if len(shown) <= budget:
                if sum(len(x) + 1 for x in shown) > size * (1 - MIN_SAVING):
                    return None
                total = len(lines)
                return "json", [(1, total, x) for x in shown], (
                    "JSON sampled: each array cut to its first %d item%s with a count, nested to depth %d"
                    % (k, "" if k == 1 else "s", depth))
    return None


# ---- diff ----------------------------------------------------------------------------------

def _claim_diff(window, tail, lines):
    if any(x.startswith("diff --git ") for x in window):
        return True
    head = window[:500]
    return (any(x.startswith("--- ") for x in head) and any(x.startswith("+++ ") for x in head)
            and any(re.match(r"@@ -\d", x) for x in head))


def _shorten_diff(lines, budget, claimed):
    items, hunks, files = [], 0, 0
    hunk = None                                              # [start, adds, dels, header]

    def close(end):
        nonlocal hunk
        if hunk:
            start, adds, dels, header = hunk
            items.append((start, end, "%s  (+%d -%d)" % (header, adds, dels)))
            hunk = None

    for i, x in enumerate(lines):
        n = i + 1
        if x.startswith("diff --git "):
            close(n - 1)
            files += 1
            items.append((n, n, x))
        elif x.startswith("@@"):
            close(n - 1)
            hunks += 1
            hunk = [n, 0, 0, x]
        elif hunk:
            if x.startswith("+"):
                hunk[1] += 1
            elif x.startswith("-"):
                hunk[2] += 1
        elif x.startswith("index ") or not x.strip():
            continue                                          # header noise, folded
        else:
            items.append((n, n, x))                           # ---, +++, mode, rename, Binary files ...
    close(len(lines))
    if not hunks or len(items) > len(lines) * (1 - MIN_SAVING):
        return None
    return "diff", items, "%d hunk%s in %d file%s listed with their +/- counts" % (
        hunks, "" if hunks == 1 else "s", files or 1, "" if files <= 1 else "s")


# ---- grep ----------------------------------------------------------------------------------

# path:line: or path:line:col:, where the path has a dot or a slash (so 12:00:01 is not one)
GREP_RE = re.compile(r"^(?=[^:\n]*[./\\])([^\s:][^:\n]*):(\d+):(?:\d+:)?(.*)$")


def _claim_grep(window, tail, lines):
    body = [x for x in window if x.strip()]
    return len(body) >= 5 and sum(1 for x in body if GREP_RE.match(x)) * 5 >= len(body) * 4


def _shorten_grep(lines, budget, claimed):
    items, run = [], None                                     # run: [path, start, end, count, first, last]

    def close():
        nonlocal run
        if run:
            path, start, end, count, first, last = run
            text = lines[start - 1] if count == 1 else "%s  (%s matches; first %s; last %s)" % (
                path, format(count, ","), first, last)
            items.append((start, end, text))
            run = None

    for i, x in enumerate(lines):
        m = GREP_RE.match(x)
        if not m:
            close()
            items.append((i + 1, i + 1, x))
            continue
        path, ln, rest = m.group(1), m.group(2), m.group(3).strip()[:80]
        tag = "%s:%s" % (ln, rest)
        if run and run[0] == path:
            run[2], run[3], run[5] = i + 1, run[3] + 1, tag
        else:
            close()
            run = [path, i + 1, i + 1, 1, tag, tag]
    close()
    if len(items) > len(lines) * (1 - MIN_SAVING):
        return None
    return "grep", items, "%s matches grouped into %s runs by file" % (format(len(lines), ","), format(len(items), ","))


_CLAIM = {"json": _claim_json, "diff": _claim_diff, "test-output": _claim_test_output,
          "grep": _claim_grep, "log": _claim_log}
_SHORTEN = {"json": _shorten_json, "diff": _shorten_diff, "test-output": _shorten_test_output,
            "grep": _shorten_grep, "log": _shorten_log}


# ---- entry point ---------------------------------------------------------------------------

def _window(lines):
    out, size = [], 0
    for x in lines:
        out.append(x)
        size += len(x) + 1
        if size >= DETECT_BYTES:
            break
    return out


def _shorten(lines, budget=None):
    if not isinstance(lines, list) or not lines or not all(isinstance(x, str) for x in lines):
        return None
    window, tail = _window(lines), lines[-TAIL_LINES:]
    for kind in KINDS:
        claimed = _CLAIM[kind](window, tail, lines)
        if claimed:
            got = _SHORTEN[kind](lines, budget, claimed)
            return got if got and got[1] else None            # a result with nothing to show is no result
    return None


def shorten(lines, budget=None):
    """-> None (leave it to the ends preview) or (kind, items, note). Never raises."""
    try:
        return _shorten(lines, budget)
    except Exception:                                         # noqa: BLE001
        return None
