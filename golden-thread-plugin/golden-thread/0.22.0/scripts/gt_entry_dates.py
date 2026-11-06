#!/usr/bin/env python3
"""gt_entry_dates.py -- dates on findings in research.md and decisions.md (0.22.0, phase 30).

A finding is a `## ` section. It is dated by its heading (`## 2026-10-19: ...`), by a capture marker in the
next three lines (`<!-- captured: 2026-10-19 05:41 -->`, with ` (git)` or ` (uncommitted)` after the time when
the date was DERIVED rather than recorded), or by a `Date:` line in the first six lines of its body (ADRs).

    marker(now, source=None, time=True)   the marker line
    needs_stamp(content)                  True when a block about to be appended carries no date at all
    stamp(content, now)                   the content with a marker first, unless it is already dated
    entry_dates(text)                     [{line, heading, date, source}] for every finding in a file
    newest(text)                          the latest entry date in a file (not the last entry's)
    terms(heading) / similar(a, b)        distinctive heading words, and whether two headings share enough
                                          of them for one to be a candidate for superseding the other

Reads and builds text; never edits a file. A marker is 36 bytes; stamping the vault's 177 undated entries
adds about 6 KB to 2.5 MB of logs.
"""
import datetime
import re

DATE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
MARKER = re.compile(r"<!--\s*captured:\s*(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}))?(?:\s+\((git|uncommitted)\))?\s*-->")
BODY_DATE = re.compile(r"\bdate\b[^\n]*?(20\d\d-\d\d-\d\d)", re.I)
HEADING = re.compile(r"^##\s+(.*\S)\s*$")
LOG_PAGES = ("research.md", "decisions.md")
STOP = set("""a an and are as at be by can did do does for from has have how if in is it its of on or our so that
the their them there these this to was we what when where which who why will with would you your not but into
than then they those were been after before about any more most only other under over also new now""".split())
MIN_SHARED = 3
MIN_OVERLAP = 0.6


def is_log_page(path):
    return str(path).replace("\\", "/").rsplit("/", 1)[-1] in LOG_PAGES


def marker(now, source=None, time=True):
    stamp = now.strftime("%Y-%m-%d %H:%M" if time else "%Y-%m-%d")
    return "<!-- captured: %s%s -->" % (stamp, " (%s)" % source if source else "")


def needs_stamp(content):
    lines = [l for l in str(content).splitlines() if l.strip()][:3]
    if not lines:
        return False
    return not any(DATE.search(l) or MARKER.search(l) for l in lines)


def stamp(content, now=None):
    if not needs_stamp(content):
        return content
    return marker(now or datetime.datetime.now()) + "\n" + content


def entry_dates(text):
    lines = str(text).splitlines()
    out, fenced = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else HEADING.match(line)
        if not m:
            continue
        heading = m.group(1)
        d, src = None, None
        hd = DATE.search(heading)
        if hd:
            d, src = hd.group(1), "heading"
        else:
            for nxt in lines[i + 1:i + 4]:
                mm = MARKER.search(nxt)
                if mm:
                    d, src = mm.group(1), mm.group(3) or "marker"
                    break
            if d is None:
                for nxt in lines[i + 1:i + 7]:
                    if HEADING.match(nxt):
                        break
                    bd = BODY_DATE.search(nxt)
                    if bd:
                        d, src = bd.group(1), "body"
                        break
        out.append({"line": i + 1, "heading": heading, "date": d, "source": src})
    return out


def newest(text):
    dates = [e["date"] for e in entry_dates(text) if e["date"]]
    return max(dates) if dates else None


def terms(heading):
    h = DATE.sub(" ", str(heading))
    h = re.sub(r"\b\d\d:\d\d\b", " ", h)
    h = re.sub(r"\badr-\d+\b", " ", h, flags=re.I)
    return {w for w in re.findall(r"[a-z][a-z0-9]{2,}", h.lower()) if w not in STOP}


def similar(a, b):
    ta, tb = terms(a), terms(b)
    shared = ta & tb
    if len(shared) < MIN_SHARED or not ta or not tb:
        return False
    return len(shared) / min(len(ta), len(tb)) >= MIN_OVERLAP
