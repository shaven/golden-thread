#!/usr/bin/env python3
"""gt_supersede.py -- supersession and expiry, applied when notes are READ (0.19.1).

    gt_supersede.py listing <dir> --vault V [--json]     # gt-open: newest of each chain only
    gt_supersede.py rank <path> [<path> ...] --vault V [--json]
                                                         # gt-query: current, expired, superseded
    gt_supersede.py dangling --vault V [--json]          # gt-lint: supersedes: pointing at nothing

A note declares that it replaces an earlier one in its frontmatter: `supersedes: <path>` (a
list, an inline [a, b], or one value). A path is relative to the note's folder or to the vault.
A note is EXPIRED when its frontmatter carries `expired: <date>` (its `expires_when` was
reported as met) or an `expires: YYYY-MM-DD` that has passed -- the same field names gt_adr.py
uses for ADRs.

Nothing is deleted or rewritten: superseded and expired notes stay on disk and stay readable.
This only decides what a session is shown first (request
2026-10-02-supersession-and-expiry-at-read-time). Read-only; stdlib only.
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOTS = ("Projects", "Knowledge", "global-memory")
FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def frontmatter(path):
    try:
        m = FM.match(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}
    if not m:
        return {}
    out, key = {}, None
    for line in m.group(1).splitlines():
        if re.match(r"^\s+-\s*", line) and key:
            out.setdefault(key, []).append(re.sub(r"^\s+-\s*", "", line).strip().strip("'\""))
            continue
        k, sep, v = line.partition(":")
        if not sep or line.startswith((" ", "\t")):
            continue
        key, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            out[key] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        elif v:
            out[key] = v.strip("'\"")
        else:
            out[key] = []
    return out


def _list(v):
    return v if isinstance(v, list) else ([v] if v else [])


def resolve(vault, note_rel, target):
    """-> vault-relative path of a supersedes target, or None when nothing is there."""
    for cand in (Path(note_rel).parent / target, Path(target)):
        if (vault / cand).is_file():
            return str(cand)
    return None


def notes(vault):
    out = []
    for top in ROOTS:
        base = vault / top
        if base.is_dir():
            out += sorted(str(p.relative_to(vault)) for p in base.rglob("*.md") if p.is_file())
    return out


def graph(vault):
    """-> ({note: [notes it supersedes]}, {note: the note that supersedes it}, dangling)."""
    sup, by, dangling = {}, {}, []
    for rel in notes(vault):
        for t in _list(frontmatter(vault / rel).get("supersedes")):
            r = resolve(vault, rel, t)
            if r is None:
                dangling.append({"path": rel, "target": t})
                continue
            sup.setdefault(rel, []).append(r)
            by.setdefault(r, rel)
    return sup, by, dangling


def current_of(rel, by):
    seen = set()
    while rel in by and rel not in seen:
        seen.add(rel)
        rel = by[rel]
    return rel


def older_of(rel, sup):
    out, stack, seen = [], list(sup.get(rel, [])), set()
    while stack:
        r = stack.pop(0)
        if r in seen:
            continue
        seen.add(r)
        out.append(r)
        stack += sup.get(r, [])
    return out


def expiry(vault, rel, today=None):
    fm = frontmatter(vault / rel)
    if fm.get("expired"):
        return "expired %s" % fm["expired"]
    when = fm.get("expires")
    if isinstance(when, str):
        try:
            due = dt.date.fromisoformat(when[:10])
        except ValueError:
            return None
        if due < (today or dt.date.today()):
            return "expired on %s" % due.isoformat()
    return None


def cmd_listing(a, vault):
    d = (vault / a.dir).resolve()
    sup, by, _ = graph(vault)
    rows = []
    for p in sorted(d.glob("*.md")):
        rel = str(p.relative_to(vault))
        if rel in by:
            continue                                    # superseded: shown via its successor
        rows.append({"path": rel, "older": older_of(rel, sup),
                     "expired": expiry(vault, rel)})
    if a.json:
        print(json.dumps({"dir": a.dir, "notes": rows}, indent=2))
    else:
        for r in rows:
            extra = "  (replaces %s)" % ", ".join(r["older"]) if r["older"] else ""
            print("%s%s%s" % (r["path"], "  [EXPIRED: %s]" % r["expired"] if r["expired"] else "",
                              extra))
    return 0


def cmd_rank(a, vault):
    _sup, by, _ = graph(vault)
    rows = []
    for i, rel in enumerate(a.paths):
        if rel in by:
            rows.append((2, i, {"path": rel, "state": "superseded",
                                "current": current_of(rel, by)}))
            continue
        why = expiry(vault, rel)
        rows.append((1 if why else 0, i, {"path": rel, "state": "expired" if why else "current",
                                          "why": why}))
    ranked = [r for _k, _i, r in sorted(rows, key=lambda x: (x[0], x[1]))]
    if a.json:
        print(json.dumps({"ranked": ranked}, indent=2))
    else:
        for r in ranked:
            tag = {"current": "", "expired": "  [EXPIRED: %s]" % r.get("why"),
                   "superseded": "  [SUPERSEDED by %s]" % r.get("current")}[r["state"]]
            print(r["path"] + tag)
    return 0


def cmd_dangling(a, vault):
    _s, _b, dangling = graph(vault)
    if a.json:
        print(json.dumps({"dangling": dangling}, indent=2))
    else:
        for x in dangling:
            print("%s: supersedes %s, which does not exist" % (x["path"], x["target"]))
    return 1 if dangling else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_supersede.py", description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", required=True, help="the vault (Core rule 2: always named)")
    common.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("listing", parents=[common])
    p.add_argument("dir")
    p.set_defaults(fn=cmd_listing)
    p = sub.add_parser("rank", parents=[common])
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=cmd_rank)
    sub.add_parser("dangling", parents=[common]).set_defaults(fn=cmd_dangling)
    a = ap.parse_args(argv)
    return a.fn(a, Path(a.vault).expanduser().resolve())


if __name__ == "__main__":
    sys.exit(main())
