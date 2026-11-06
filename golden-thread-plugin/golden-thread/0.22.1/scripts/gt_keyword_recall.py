#!/usr/bin/env python3
"""gt_keyword_recall.py -- find vault pages for a question by keyword, the way gt-query does (0.19.1).

    gt_keyword_recall.py "<question>" --vault V [--k 10] [--json]

gt-query reads index.md first, follows the pages it names, and falls back to searching the
vault. This is that path made deterministic, so it can be measured (gt_bench.py recall) and
reused (the optional prompt-hints hook): every markdown page under Knowledge/, Projects/ and
global-memory/ is scored on the question's words -- a hit in the page's name or its index.md
line counts triple, a hit in the body once -- and the best come first.

`retrieve()` returns {"results": [vault-relative paths, best first], "scores": {...},
"read": [what a session would open: index.md and the top three]} -- the read list is what the
bench counts as files read and tokens spent. Stdlib only.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

LEVELS = ("Knowledge", "Projects", "global-memory")
STOP = set("""a an and are as at be by can did do does for from has have how i if in is it its
many may of on or our should so that the their them there these this to was we what when where
which who why will with would you your about after any been but could into more most much not
only other than then they those through under very were""".split())
WORD = re.compile(r"[a-z0-9][a-z0-9_-]*")


def words(text):
    return [w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 1]


def _stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


LOCK_STUB = ".gt-locked"


def pages(vault, locked=None):
    """Every .md page under LEVELS. A folder holding gt_lock.py's `.gt-locked` stub (0.20.1) is
    absent: never descended into, never surfaced -- the stub is seen in the directory listing
    the walk already makes, so it costs no extra stat. Its vault-relative path is appended to
    `locked` when a list is given (one notice is the caller's)."""
    vault = Path(vault)
    out = []
    for top in LEVELS:
        base = vault / top
        if not base.is_dir():
            continue
        found = []
        for root, dirs, files in os.walk(str(base)):
            if LOCK_STUB in files:
                dirs[:] = []
                if locked is not None:
                    locked.append(Path(root).relative_to(vault).as_posix())
                continue
            found += [Path(root) / f for f in files if f.endswith(".md")]
        out += sorted(p for p in found if p.is_file())
    return out


def index_lines(vault):
    """-> {page stem (lowercase): the index.md line that names it}."""
    f = Path(vault) / "index.md"
    out = {}
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            for m in re.finditer(r"\[\[([^\]|#]+)", line):
                out[m.group(1).strip().lower()] = line
    except OSError:
        pass
    return out


def score_pages(question, vault, locked=None):
    q = {_stem(w) for w in words(question)}
    if not q:
        return {}
    idx = index_lines(vault)
    scores = {}
    for p in pages(vault, locked):
        rel = p.relative_to(vault).as_posix()
        name = {_stem(w) for w in words(rel.replace("/", " ").replace(".md", ""))}
        name |= {_stem(w) for w in words(idx.get(p.stem.lower(), ""))}
        try:
            body = [_stem(w) for w in words(p.read_text(encoding="utf-8", errors="replace"))]
        except OSError:
            continue
        s = 3 * len(q & name) + sum(1 for w in body if w in q)
        if s:
            scores[rel] = s
    return scores


def _tie_breaker(vault, scores):
    """Equal scores: the most recently USED page first, then the most recently READ (0.21.0,
    gt_use.py), then the path -- the path alone when there is no read or use log, which is
    exactly the order before. The logs only, never the derived uses: this also serves the
    prompt-hints hook, whose budget is 0.8 s, and deriving reads the whole vault."""
    try:
        import gt_use
        sig, _first, has = gt_use.signals(vault, list(scores), derive=False)
    except Exception:                                            # noqa: BLE001
        has = False
    if not has:
        return lambda r: (-scores[r], r)
    import datetime as _dt
    lo = _dt.date.min

    def key(r):
        v = sig.get(r) or {}
        return (-scores[r], -(v.get("last_use") or lo).toordinal(),
                -(v.get("last_read") or lo).toordinal(), r)
    return key


def _log_dates(vault, ranked):
    """0.22.0: for each ranked research.md or decisions.md, the date of its newest entry, how many entries it has
    and how many carry no date. Such a page is appended to, so its file time is always recent and says nothing
    about the finding a question is after. Other pages get no entry. Reads only the ranked log pages."""
    out = {}
    try:
        import gt_entry_dates
    except Exception:                                            # noqa: BLE001
        return out
    for rel in ranked:
        if not gt_entry_dates.is_log_page(rel):
            continue
        try:
            entries = gt_entry_dates.entry_dates((Path(vault) / rel).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        dates = [e["date"] for e in entries if e["date"]]
        out[rel] = {"newest_entry": max(dates) if dates else None, "entries": len(entries),
                    "undated": sum(1 for e in entries if not e["date"])}
    return out


def retrieve(question, vault, k=10):
    locked = []
    scores = score_pages(question, vault, locked)
    ranked = sorted(scores, key=_tie_breaker(vault, scores))[:k]
    read = (["index.md"] if (Path(vault) / "index.md").is_file() else []) + ranked[:3]
    return {"results": ranked, "scores": {r: scores[r] for r in ranked}, "read": read,
            "locked": sorted(locked), "dates": _log_dates(vault, ranked)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("question")
    ap.add_argument("--vault", required=True, help="the vault to search (Core rule 2: named)")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = retrieve(a.question, a.vault, a.k)
    if r["locked"]:
        sys.stderr.write("note: %d locked folder(s) not searched (%s)\n"
                         % (len(r["locked"]), ", ".join(r["locked"])))
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        for rel in r["results"]:
            d = r["dates"].get(rel)
            print("%4d  %s%s" % (r["scores"][rel], rel,
                                 "  (newest entry %s)" % d["newest_entry"] if d and d["newest_entry"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
