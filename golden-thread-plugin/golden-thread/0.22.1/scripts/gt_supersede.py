#!/usr/bin/env python3
"""gt_supersede.py -- supersession and expiry, applied when notes are READ (0.19.1).

    gt_supersede.py listing <dir> --vault V [--json]     # gt-open: newest of each chain only
    gt_supersede.py rank <path> [<path> ...] --vault V [--json]
                                                         # gt-query: current, expired, superseded
    gt_supersede.py dangling --vault V [--json]          # gt-lint: supersedes: pointing at nothing

RETRACTION (0.21.0, gt_forget.py) is supersession with a reason: `retracted: "<date> | reason:
<why> | replaced_by: <path>"` in a note's frontmatter, or the same after `> retracted: ` as the
first line of a note without frontmatter -- the WHOLE note is retracted -- or as the line right
under a `## ` heading -- that section is. A retracted note ranks after current, expired and
superseded ones with its reason; gt-open's listing leaves it out; a `replaced_by` naming nothing
is dangling. A note with only a section retracted keeps its band, tagged PART RETRACTED.

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
            return cand.as_posix()           # vault paths are "/" on every platform (0.20.1)
    return None


def notes(vault):
    out = []
    for top in ROOTS:
        base = vault / top
        if base.is_dir():
            out += sorted(p.relative_to(vault).as_posix() for p in base.rglob("*.md") if p.is_file())
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
        ret = retraction(vault, rel)
        for r in ([ret["note"]] if ret["note"] else []) + ret["sections"]:
            if r.get("by") and resolve(vault, rel, r["by"]) is None:
                dangling.append({"path": rel, "target": r["by"], "kind": "retracted_by"})
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


MARKER = re.compile(r"^>\s*retracted:\s*(.+?)\s*$")
H2 = re.compile(r"^##\s+(.*?)\s*#*\s*$")


def parse_retraction(value):
    """'<date> | reason: R | replaced_by: P' -> {date, reason, by}; None for nothing usable."""
    if not isinstance(value, str) or not value.strip():
        return None
    parts = [x.strip() for x in value.strip().strip('"').split("|")]
    out = {"date": parts[0], "reason": "", "by": None}
    for p in parts[1:]:
        k, _, v = p.partition(":")
        if k.strip() == "reason":
            out["reason"] = v.strip()
        elif k.strip() == "replaced_by":
            out["by"] = v.strip() or None
    return out


def retraction(vault, rel):
    """-> {note: {...} or None, sections: [{section, ...}]} -- what of `rel` is retracted."""
    note, sections = None, []
    fm = frontmatter(vault / rel)
    if fm.get("retracted"):
        note = parse_retraction(fm["retracted"] if isinstance(fm["retracted"], str)
                                else " ".join(fm["retracted"]))
    try:
        text = (vault / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"note": note, "sections": sections}
    m = FM.match(text)
    lines = text[m.end():].split("\n") if m else text.split("\n")
    first = next((ln for ln in lines if ln.strip()), "")
    if note is None and MARKER.match(first):
        note = parse_retraction(MARKER.match(first).group(1))
    for i, ln in enumerate(lines[:-1]):
        h = H2.match(ln)
        if h and MARKER.match(lines[i + 1]):
            r = parse_retraction(MARKER.match(lines[i + 1]).group(1))
            if r:
                sections.append(dict(r, section=h.group(1)))
    return {"note": note, "sections": sections}


def cmd_listing(a, vault):
    d = (vault / a.dir).resolve()
    sup, by, _ = graph(vault)
    rows = []
    for p in sorted(d.glob("*.md")):
        rel = p.relative_to(vault).as_posix()
        if rel in by:
            continue                                    # superseded: shown via its successor
        if retraction(vault, rel)["note"]:
            continue                                    # retracted (0.21.0): not listed
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
        ret = retraction(vault, rel)
        if ret["note"]:
            rows.append((3, i, {"path": rel, "state": "retracted",
                                "why": ret["note"]["reason"] or "no reason given",
                                "date": ret["note"]["date"], "by": ret["note"]["by"]}))
            continue
        part = [{"section": r["section"], "why": r["reason"]} for r in ret["sections"]]
        if rel in by:
            row = {"path": rel, "state": "superseded", "current": current_of(rel, by)}
            if part:
                row["retracted_sections"] = part
            rows.append((2, i, row))
            continue
        why = expiry(vault, rel)
        row = {"path": rel, "state": "expired" if why else "current", "why": why}
        if part:
            row["retracted_sections"] = part
        rows.append((1 if why else 0, i, row))
    ranked = [r for _k, _i, r in sorted(rows, key=lambda x: (x[0], x[1]))]
    ranked = recency_order(vault, rows) or ranked
    if a.json:
        print(json.dumps({"ranked": ranked}, indent=2))
    else:
        for r in ranked:
            tag = {"current": "", "expired": "  [EXPIRED: %s]" % r.get("why"),
                   "superseded": "  [SUPERSEDED by %s]" % r.get("current"),
                   "retracted": "  [RETRACTED: %s%s]" % (
                       r.get("why"), "; see %s" % r["by"] if r.get("by") else "")}[r["state"]]
            for p in r.get("retracted_sections") or []:
                tag += "  [PART RETRACTED: ## %s: %s]" % (p["section"], p["why"])
            if r.get("unread_since"):
                tag += "  [unread since %s]" % r["unread_since"]
            print(r["path"] + tag)
    return 0


def recency_order(vault, rows, today=None):
    """READ and USE recency (0.21.0, gt_use.py) inside each band -- never across one. -> the
    re-ordered rows, labelled, or None when there is no read log and no use log (so the output
    is exactly what 0.19.1 printed).

    Inside a band: the most recently USED first, then the most recently READ, then the input
    order. A Knowledge page with no read for `knowledge_unread_days` (or never, since the log
    began) is labelled unread since that date. Nothing is hidden or moved out of its band."""
    try:
        import gt_use
    except ImportError:
        return None
    paths = [r["path"] for _b, _i, r in rows]
    try:
        sig, first, has = gt_use.signals(vault, paths, today=today)
    except Exception:                                           # noqa: BLE001 - as before
        return None
    if not has:
        return None
    today = today or dt.date.today()
    limit = gt_use.unread_days()
    lo = dt.date.min

    def key(x):
        band, i, r = x
        v = sig.get(r["path"]) or {}
        return (band, -(v.get("last_use") or lo).toordinal(),
                -(v.get("last_read") or lo).toordinal(), i)
    out = []
    for _b, _i, r in sorted(rows, key=key):
        v = sig.get(r["path"]) or {}
        r = dict(r, last_read=v["last_read"].isoformat() if v.get("last_read") else None,
                 last_use=v["last_use"].isoformat() if v.get("last_use") else None)
        if v.get("last_use_kind"):
            r["last_use_kind"] = v["last_use_kind"]
        if r["path"].startswith("Knowledge/") and first is not None:
            last = v.get("last_read")
            if last is None:
                r["unread_since"] = first.isoformat()
            elif (today - last).days > limit:
                r["unread_since"] = last.isoformat()
        out.append(r)
    return out


def cmd_dangling(a, vault):
    _s, _b, dangling = graph(vault)
    if a.json:
        print(json.dumps({"dangling": dangling}, indent=2))
    else:
        for x in dangling:
            print("%s: %s %s, which does not exist" % (
                x["path"], "is retracted in favour of" if x.get("kind") == "retracted_by"
                else "supersedes", x["target"]))
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
