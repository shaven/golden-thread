#!/usr/bin/env python3
"""gt_use.py -- how recently a note was READ, and how recently it was USED (0.21.0).

    gt_use.py show   --vault V [--page P ...] [--json]
    gt_use.py record --vault V --page P --kind answered-from|enforced|cited|acted-on
                     [--by WHAT] [--session ID] [--dry-run]

Request 2026-10-03-rank-notes-by-read-recency, with the owner's "use, not just reads" (2026-10-04):
"If a rule is read and never used, wouldn't that make it irrelevant?" Reads alone over-rank the
pages every session loads and nothing ever changes because of. So there are two signals.

READ  <vault>/usage/knowledge.jsonl, written by the log_knowledge_read hook since 0.18.1: one line
      per Read of a Knowledge page. Nothing new is hooked for it.
USE   four kinds of evidence that a note changed what happened:
        cited          a write-back written ON OR AFTER a read of the page links or names it -- a
                       research entry (its `## YYYY-MM-DD` date), a handoff, an ADR, a design
                       note, another Knowledge page. DERIVED from the vault each time, never
                       guessed: the citing file and its date are the evidence.
        acted-on       the session that read the page then edited a file the page names, on or
                       after the read -- from the vault's `Session-Edit:` commit trailers.
                       DERIVED, like cited.
        answered-from  gt-query answered from the page and cited it (the skill records it).
        enforced       a guard or hook decision named the rule (deny / ask); the guards record it.
      Recorded kinds go to <vault>/usage/use.jsonl -- a local log beside the read log, under the
      same `usage/.gitignore`, JSON Lines and not vault content.

WHAT THE SIGNALS DO, AND NEVER DO. gt-query (gt_supersede.py rank) orders equally ranked matches by
last USE, then last READ, and labels a Knowledge page unread past `knowledge_unread_days` as
`[unread since <date>]`. gt-optimize lists pages unread past it, and pages READ OFTEN BUT NEVER
USED (`knowledge_read_often` reads in 30 days, no use) as review candidates. Nothing expires,
moves or is hidden because of either signal, and neither moves a note across the current /
expired / superseded / retracted bands. With no read log and no use log every caller behaves
exactly as before.

PREVENTIVE RULES. A rule that is obeyed never fires, so "never enforced" is not "irrelevant":
the Core rules (core-rules/) and any page whose frontmatter says `preventive: true` are never
listed as unused; gt-optimize shows them apart, with that caveat.

Exit: 0 ok | 2 usage.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gt_use_record import (USAGE_DIR, USE_LOG, KINDS, record_use, rule_page,  # noqa: E402,F401
                           record_enforced)

READ_LOG = "knowledge.jsonl"
RECORDED_KINDS = ("answered-from", "enforced", "cited", "acted-on")
READ_OFTEN_WINDOW = 30
DATED = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\b")
DATE_IN = re.compile(r"(\d{4}-\d{2}-\d{2})")
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)
TRAILER = re.compile(r"^Session-Edit:\s*(.+?)\s+·\s+([0-9A-Za-z_-]{1,64})(?:\s+·.*)?$", re.M)
# Files a page "naming" proves nothing about: generated, logs, gt's own spool.
NOT_GOVERNED = ("log.md", "TASKS.md", "INBOX.md", "index.md")


def _date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        pass
    return out


def reads(vault):
    """-> ({page: [(date, session)]}, first logged date) or ({}, None) with no read log."""
    rows = _jsonl(Path(vault) / USAGE_DIR / READ_LOG)
    out, first = {}, None
    for r in rows:
        d = _date(r.get("date"))
        page = r.get("page")
        if d is None or not isinstance(page, str):
            continue
        out.setdefault(page, []).append((d, str(r.get("session") or "")))
        first = d if first is None or d < first else first
    return out, first


def recorded_uses(vault):
    """-> {page: [{date, kind, by, session}]} from use.jsonl."""
    out = {}
    for r in _jsonl(Path(vault) / USAGE_DIR / USE_LOG):
        d, page, kind = _date(r.get("date")), r.get("page"), r.get("kind")
        if d is None or not isinstance(page, str) or kind not in KINDS:
            continue
        out.setdefault(page, []).append({"date": d, "kind": kind, "by": r.get("by"),
                                         "session": r.get("session")})
    return out


# record_use, rule_page and record_enforced live in gt_use_record.py (imported above).


# -- derived uses --------------------------------------------------------------------------------
def _frontmatter(text):
    m = FM.match(text)
    out = {}
    if m:
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if sep and not line.startswith((" ", "\t")):
                out[k.strip()] = v.strip().strip("'\"")
    return out


def _citing_units(vault):
    """Yield (rel, anchor, date, text): research entries by their dated heading, every other
    write-back file whole, dated by frontmatter `updated`/`created`, a date in its name, or its
    modification time."""
    vault = Path(vault)
    for base in ("Projects", "Knowledge", "global-memory"):
        root = vault / base
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.md")):
            rel = p.relative_to(vault).as_posix()
            if "/spool/" in rel or "/tools/" in rel:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if p.name == "research.md":
                cur, buf, anchor = None, [], None
                for line in text.splitlines():
                    m = DATED.match(line)
                    if m:
                        if cur:
                            yield rel, anchor, cur, "\n".join(buf)
                        cur, buf, anchor = _date(m.group(1)), [], line[3:].strip()
                    elif cur:
                        buf.append(line)
                if cur:
                    yield rel, anchor, cur, "\n".join(buf)
                continue
            fm = _frontmatter(text)
            d = _date(fm.get("updated")) or _date(fm.get("created"))
            if d is None:
                m = DATE_IN.search(p.name)
                d = _date(m.group(1)) if m else None
            if d is None:
                try:
                    d = dt.date.fromtimestamp(p.stat().st_mtime)
                except OSError:
                    continue
            yield rel, None, d, text


def derived_cited(vault, read_map, pages=None):
    """-> {page: [use events]}: each citation dated on or after a read of the page."""
    want = set(pages) if pages is not None else set(read_map)
    want &= set(read_map)
    if not want:
        return {}
    by_stem = {}
    for page in want:
        by_stem.setdefault(Path(page).stem.lower(), set()).add(page)
    out = {}
    for rel, anchor, d, text in _citing_units(vault):
        named = set()
        for m in WIKILINK.finditer(text):
            target = m.group(1).strip()
            named |= by_stem.get(Path(target).stem.lower(), set())
        for page in want:
            if page in text:
                named.add(page)
        for page in named:
            if page == rel:
                continue
            if any(rd <= d for rd, _s in read_map.get(page, ())):
                out.setdefault(page, []).append({
                    "date": d, "kind": "cited",
                    "by": rel + ("#" + anchor if anchor else ""), "session": None})
    return out


def _edits(vault, since):
    """-> [(date, path, session8)] from the vault's Session-Edit commit trailers since `since`."""
    try:
        p = subprocess.run(["git", "-C", str(vault), "log", "--since=%s" % since.isoformat(),
                            "--format=%x1e%cs%n%B"], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    out = []
    for chunk in p.stdout.split("\x1e"):
        lines = chunk.strip("\n").split("\n", 1)
        d = _date(lines[0]) if lines else None
        if d is None or len(lines) < 2:
            continue
        for m in TRAILER.finditer(lines[1]):
            out.append((d, m.group(1).strip(), m.group(2)[:8]))
    return out


def derived_acted_on(vault, read_map, pages=None):
    """-> {page: [use events]}: an edit by the reading session, on or after the read, to a file
    the page names (its vault path, or a file name of six or more characters)."""
    want = set(pages) if pages is not None else set(read_map)
    want &= set(read_map)
    if not want:
        return {}
    first = min(d for p in want for d, _s in read_map[p])
    edits = _edits(vault, first)
    if not edits:
        return {}
    out = {}
    for page in want:
        try:
            text = (Path(vault) / page).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sessions = {}
        for d, s in read_map[page]:
            if s and (s not in sessions or d < sessions[s]):
                sessions[s] = d
        for d, path, s in edits:
            if s not in sessions or d < sessions[s] or path == page:
                continue
            name = path.rsplit("/", 1)[-1]
            if name in NOT_GOVERNED or "/spool/" in path:
                continue
            if path in text or (len(name) >= 6 and "." in name and name in text):
                out.setdefault(page, []).append({"date": d, "kind": "acted-on", "by": path,
                                                 "session": s})
    return out


# -- the signals ---------------------------------------------------------------------------------
def preventive(vault, page):
    """Core rules, and pages whose frontmatter says `preventive: true`."""
    if page.startswith("core-rules/") or "/core-rules/" in page:
        return True
    try:
        fm = _frontmatter((Path(vault) / page).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False
    return str(fm.get("preventive", "")).lower() in ("true", "yes")


def signals(vault, pages=None, derive=True, today=None):
    """-> ({page: {reads, last_read, reads_window, uses: {kind: n}, last_use, last_use_kind,
    evidence: [...]}}, read log start or None, has_logs). `pages` limits the work."""
    today = today or dt.date.today()
    read_map, first = reads(vault)
    uses = recorded_uses(vault)
    if derive and read_map:
        for extra in (derived_cited(vault, read_map, pages),
                      derived_acted_on(vault, read_map, pages)):
            for page, evs in extra.items():
                uses.setdefault(page, []).extend(evs)
    has_logs = bool(read_map) or bool(uses)
    keys = set(read_map) | set(uses)
    if pages is not None:
        keys &= set(pages)
    out = {}
    for page in keys:
        rs = read_map.get(page, [])
        us = sorted(uses.get(page, []), key=lambda e: e["date"])
        counts = {}
        for e in us:
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        out[page] = {
            "reads": len(rs),
            "last_read": max((d for d, _s in rs), default=None),
            "reads_window": sum(1 for d, _s in rs if (today - d).days <= READ_OFTEN_WINDOW),
            "uses": counts,
            "last_use": us[-1]["date"] if us else None,
            "last_use_kind": us[-1]["kind"] if us else None,
            "evidence": [{"date": e["date"].isoformat(), "kind": e["kind"], "by": e.get("by")}
                         for e in us[-5:]],
        }
    return out, first, has_logs


def setting(name, default):
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import gt_settings                                       # noqa: PLC0415
        return int(gt_settings.get(name) or default)
    except Exception:                                            # noqa: BLE001
        return int(default)


def unread_days():
    return setting("knowledge_unread_days", 90)


def read_often():
    return setting("knowledge_read_often", 10)


# -- commands ------------------------------------------------------------------------------------
def cmd_show(a, vault):
    sig, first, has = signals(vault, a.page or None)
    if a.json:
        print(json.dumps({"read_log_since": first.isoformat() if first else None,
                          "pages": {p: dict(v, last_read=v["last_read"] and v["last_read"]
                                            .isoformat(), last_use=v["last_use"] and
                                            v["last_use"].isoformat())
                                    for p, v in sorted(sig.items())}}, indent=2))
        return 0
    if not has:
        print("no read log and no use log in %s/usage/ -- nothing to show" % vault)
        return 0
    for p, v in sorted(sig.items(), key=lambda kv: (kv[1]["last_use"] or dt.date.min,
                                                     kv[1]["last_read"] or dt.date.min),
                       reverse=True):
        uses = ", ".join("%s %d" % kv for kv in sorted(v["uses"].items())) or "never used"
        print("%-50s read %d (last %s)  %s%s" % (p, v["reads"], v["last_read"] or "-", uses,
                                                  " (last %s)" % v["last_use"] if v["last_use"]
                                                  else ""))
    return 0


def cmd_record(a, vault):
    page = a.page
    if os.path.isabs(page):
        try:
            page = Path(page).resolve().relative_to(vault).as_posix()
        except ValueError:
            print("refused: %s is outside the vault" % a.page, file=sys.stderr)
            return 2
    if ".." in Path(page).parts:
        print("refused: %s is outside the vault" % a.page, file=sys.stderr)
        return 2
    if a.dry_run:
        print("would record %s use of %s" % (a.kind, page))
        return 0
    ok = record_use(vault, page, a.kind, a.by, a.session or
                    os.environ.get("CLAUDE_CODE_SESSION_ID"))
    print("recorded %s use of %s" % (a.kind, page) if ok else "could not record (usage/ not "
                                                               "writable)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_use.py", description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("show", parents=[common])
    p.add_argument("--page", action="append")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("record", parents=[common])
    p.add_argument("--page", required=True)
    p.add_argument("--kind", required=True, choices=RECORDED_KINDS)
    p.add_argument("--by")
    p.add_argument("--session")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_record)
    a = ap.parse_args(argv)
    return a.fn(a, Path(a.vault).expanduser().resolve())


if __name__ == "__main__":
    sys.exit(main())
