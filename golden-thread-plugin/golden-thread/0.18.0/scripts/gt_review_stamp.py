#!/usr/bin/env python3
"""gt_review_stamp.py -- record that a Knowledge page was READ, so review-due measures use.

    gt_review_stamp.py --vault V PAGE [PAGE ...] [--date YYYY-MM-DD] [--dry-run] [--json]

PAGE is a vault-relative path under `Knowledge/` (`Knowledge/Foo.md`), or a bare page name
(`Foo`, `Foo.md`), which is resolved to `Knowledge/<name>.md`.

WHY THIS EXISTS (0.18.0, request 2026-09-24-srs-review-scheduling). The wiki lint's
`review-due` check aged a page from its `updated:` date -- the last time it was WRITTEN. A page
written once and never consulted again looked exactly like one reread last week, and a page cited
in every session but never edited (a platform constant, an auth pattern) aged into review-due
while being the page most often checked against reality, or never got reread while silently going
wrong. Reading is the event that matters, so `/gt:gt-query` and `/gt:gt-open` call this after
they have used a Knowledge page, and `wiki_lint.py` ages the page from the NEWER of
`last_reviewed:` and `updated:`.

`last_reviewed` is access, not content: it never touches `updated:`.

HOW IT WRITES. Through the write queue only (Core rule 1, queue-first): one `set-property`
request per page (`--key last_reviewed --value <date>`), submitted all-or-nothing, then one
broker drain. Nothing here opens a page for writing. `review_stamp: off` (gt setting) turns it
off; the lint then falls back to `updated:` for every page not already stamped. A page already
stamped with the same date is skipped, so a page read ten times in a day is one write, not ten.

WHAT IT REFUSES: a page outside `Knowledge/`, a page that does not exist, `_template.md`, and a
malformed date. A refusal names the page and why; the others still go through.

Exit: 0 stamped / nothing to stamp (or would, with --dry-run) | 1 a page refused, or the queue
refused the write | 2 usage or no vault.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEY = "last_reviewed"


def _queue():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import gt_write_queue                                         # noqa: PLC0415
    return gt_write_queue


def setting(name: str, fallback: str) -> str:
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import gt_settings                                        # noqa: PLC0415
        return gt_settings.get(name) or fallback
    except Exception:
        return fallback


def today() -> datetime.date:
    pinned = os.environ.get("GT_TODAY")
    if pinned:
        try:
            return datetime.date.fromisoformat(pinned)
        except ValueError:
            pass
    return datetime.date.today()


def resolve(vault: Path, page: str) -> tuple[str | None, str | None]:
    """-> (vault-relative path, None) or (None, why refused)."""
    p = page.strip().replace("\\", "/")
    if p.startswith("[[") and p.endswith("]]"):
        p = p[2:-2].split("|")[0].split("#")[0]
    if os.path.isabs(p):
        try:
            p = str(Path(p).resolve().relative_to(vault.resolve())).replace(os.sep, "/")
        except ValueError:
            return None, "%s is outside the vault" % page
    if "/" not in p:
        p = "Knowledge/" + p
    if not p.endswith(".md"):
        p += ".md"
    if not p.startswith("Knowledge/") or ".." in p.split("/"):
        return None, "%s is not a Knowledge page (only Knowledge/ pages carry last_reviewed)" % page
    if p.split("/")[-1] == "_template.md":
        return None, "_template.md is the page template, not a page"
    if not (vault / p).is_file():
        return None, "%s does not exist" % p
    return p, None


def current(vault: Path, rel: str) -> str | None:
    q = _queue()
    try:
        text = (vault / rel).read_text(encoding="utf-8")
    except OSError:
        return None
    _has, v = q.frontmatter_get(text, KEY)
    return str(v).strip().strip("'\"") if v is not None else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gt_review_stamp.py",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("pages", nargs="+", metavar="PAGE")
    ap.add_argument("--vault", help="the vault (Core rule 2: name it)")
    ap.add_argument("--date", help="YYYY-MM-DD (default today)")
    ap.add_argument("--dry-run", action="store_true", help="say what would be stamped")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if setting("review_stamp", "on") == "off":
        print("gt_review_stamp: review stamping is off (gt setting review_stamp) -- nothing written")
        return 0
    q = _queue()
    vault = q.find_vault(a.vault)
    if vault is None:
        print("gt_review_stamp: no vault -- pass --vault", file=sys.stderr)
        return 2
    if a.date:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", a.date):
            print("gt_review_stamp: --date must be YYYY-MM-DD", file=sys.stderr)
            return 2
        try:
            datetime.date.fromisoformat(a.date)
        except ValueError:
            print("gt_review_stamp: --date %s is not a real date" % a.date, file=sys.stderr)
            return 2
    date = a.date or today().isoformat()

    refused, skipped, writes, seen = [], [], [], set()
    for page in a.pages:
        rel, why = resolve(vault, page)
        if why:
            refused.append({"page": page, "reason": why})
            continue
        if rel in seen:
            continue
        seen.add(rel)
        if current(vault, rel) == date:
            skipped.append(rel)
            continue
        writes.append({"path": rel, "op": "set-property", "key": KEY, "content": date,
                       "hint": "review stamp: page read on %s" % date})

    results, note = [], None
    if writes and not a.dry_run:
        results, note = q.submit(vault, writes)
    elif writes:
        results = [{"path": w["path"], "op": "set-property", "decision": "dry-run"}
                   for w in writes]
    bad = [r for r in results if r.get("decision") in ("refused", "reject")]

    if a.json:
        print(json.dumps({"date": date, "results": results, "skipped": skipped,
                          "refused": refused, "note": note}, indent=1))
    else:
        for r in results:
            print("gt_review_stamp: %s %s=%s -> %s%s"
                  % (r["path"], KEY, date, r.get("decision"),
                     (" (%s)" % r["reason"]) if r.get("reason") else ""))
        for s in skipped:
            print("gt_review_stamp: %s already %s=%s -- nothing to write" % (s, KEY, date))
        for r in refused:
            print("gt_review_stamp: REFUSED %s -- %s" % (r["page"], r["reason"]), file=sys.stderr)
        if note:
            print("gt_review_stamp: %s; %s" % (note, q.drain_hint(vault)))
    return 1 if (refused or bad) else 0


if __name__ == "__main__":
    raise SystemExit(main())
