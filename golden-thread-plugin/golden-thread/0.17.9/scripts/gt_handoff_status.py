#!/usr/bin/env python3
"""gt_handoff_status.py -- is a handoff still waiting on someone, and until when?

    gt_handoff_status.py list --vault V [--project SLUG] [--all] [--json]
    gt_handoff_status.py mark FILE --vault V --status handled|open|deferred
                              [--until YYYY-MM-DD] [--reason TEXT] [--dry-run]

A handoff exists because a session could NOT capture everything. Until 0.17.2 nothing recorded
whether anyone had dealt with it: `gt_surface` showed a new one once and then went quiet, so a
handoff scrolled past on a busy morning was as good as never written. This gives every handoff
a STATUS, and the status -- not a "shown once" ledger -- decides whether it keeps coming back.

THE FOUR STATES, and what decides each:

  open      written by gt_handoff, or re-opened; keeps surfacing until handled or deferred
  deferred  put off TO A DATE (`until:`). Hidden until then; on that date it is open again.
            There is no deferral without a date -- "later, some time" is a decision to drop it,
            and that is `handled` with a reason, said out loud.
  handled   marked so by a person, OR every task citing it has been checked off. The tasks
            gt-work raises are the record; when the last one closes, the handoff is done
            without anyone having to remember a second step.
  history   a handoff written before 0.17.2 (no `status:`) that is over a week old with no open
            task citing it. The upgrade must not resurface every old handoff in the vault.

A handoff with no `status:` field is treated as `open` while it is under a week old or any
open task cites it -- the same rule the first 0.17.2 surfacer used, so upgrading changes
nothing about what is shown on day one.

This never loads a handoff's BODY into anything. `list` reads the frontmatter and counts the
task lines that cite the file, which is the whole point: surfacing a handoff must not cost the
context of handling it (owner, 2026-09-28).

Exit: 0 ok | 2 usage | 3 could not do it (file missing, outside the vault, bad date).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import tempfile
from pathlib import Path

LEGACY_DAYS = 7
STATES = ("open", "deferred", "handled")
TASK = re.compile(r"^\s*- \[( |x|X)\]\s+(.*)$")
FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def today() -> datetime.date:
    pinned = os.environ.get("GT_TODAY")               # tests pin the clock
    if pinned:
        try:
            return datetime.date.fromisoformat(pinned)
        except ValueError:
            pass
    return datetime.date.today()


def read_frontmatter(text: str) -> dict:
    m = FM.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _projects(vault: Path):
    root = vault / "Projects"
    if not root.is_dir():
        return
    for p in sorted(root.iterdir()):
        if p.is_dir():
            yield p
            for sub in sorted(p.iterdir()):
                if sub.is_dir() and (sub / "README.md").is_file():
                    yield sub


def handoff_files(proj: Path):
    """gt_handoff writes `handoff/*.md`; people wrote `handoff*.md` beside the README before the
    tool was trusted, and both are real handoffs."""
    for f in sorted(list(proj.glob("handoff/*.md")) + list(proj.glob("handoff*.md"))):
        if f.is_file() and f.name.lower() != "readme.md":
            yield f


def citing_tasks(proj: Path, name: str):
    """-> (open, closed) counts of task lines in the project README naming this handoff file."""
    try:
        text = (proj / "README.md").read_text(encoding="utf-8")
    except OSError:
        return 0, 0
    o = c = 0
    for line in text.splitlines():
        m = TASK.match(line)
        if m and name in m.group(2):
            if m.group(1) == " ":
                o += 1
            else:
                c += 1
    return o, c


def slug_of(vault: Path, proj: Path) -> str:
    return str(proj.relative_to(vault / "Projects"))


def record(vault: Path, proj: Path, f: Path) -> dict:
    try:
        head = f.read_text(encoding="utf-8")[:2000]
    except OSError:
        head = ""
    fm = read_frontmatter(head)
    status = fm.get("status", "").lower()
    until = fm.get("until", "")
    open_n, closed_n = citing_tasks(proj, f.name)
    age = int((datetime.datetime.now().timestamp() - f.stat().st_mtime) // 86400)
    now = today()
    note = ""
    if status == "handled":
        eff = "handled"
    elif status == "deferred":
        try:
            due = datetime.date.fromisoformat(until)
        except ValueError:
            due = None
        if due is None:
            eff, note = "open", "deferred with no readable date -- treated as open"
        elif due > now:
            eff, note = "deferred", "until %s" % until
        else:
            eff, note = "open", "deferral ended %s" % until
    elif open_n == 0 and closed_n > 0:
        eff, note = "handled", "every task citing it is closed"
    elif status == "open":
        eff = "open"
    elif age <= LEGACY_DAYS or open_n:
        eff, note = "open", "no status recorded (written before 0.17.2)"
    else:
        eff = "history"
    return {"path": str(f.relative_to(vault)), "project": slug_of(vault, proj),
            "status": eff, "recorded": status or None, "until": until or None,
            "open_items": open_n, "closed_items": closed_n, "age_days": age, "note": note}


def all_handoffs(vault: Path, project: str | None = None):
    out = []
    for proj in _projects(vault):
        slug = slug_of(vault, proj)
        if project and slug != project:
            continue
        for f in handoff_files(proj):
            try:
                out.append(record(vault, proj, f))
            except OSError:
                continue
    return out


def waiting(vault: Path, project: str | None = None):
    """Handoffs that want attention now, oldest first."""
    return sorted((r for r in all_handoffs(vault, project) if r["status"] == "open"),
                  key=lambda r: -r["age_days"])


def line(r: dict) -> str:
    items = ("%d open item%s" % (r["open_items"], "" if r["open_items"] == 1 else "s")
             if r["open_items"] else "no task cites it yet")
    extra = ("; " + r["note"]) if r["note"] else ""
    return "%s  (%s, %dd old, %s%s)" % (r["path"], r["project"], r["age_days"], items, extra)


# ------------------------------------------------------------------------ mark ----

def set_frontmatter(text: str, updates: dict) -> str:
    m = FM.match(text)
    lines = m.group(1).splitlines() if m else []
    body = text[m.end():] if m else text
    keys = {l.split(":", 1)[0].strip(): i for i, l in enumerate(lines) if ":" in l}
    for k, v in updates.items():
        if v is None:
            if k in keys:
                lines[keys[k]] = None
            continue
        if k in keys:
            lines[keys[k]] = "%s: %s" % (k, v)
        else:
            lines.append("%s: %s" % (k, v))
    lines = [l for l in lines if l is not None]
    return "---\n%s\n---\n%s" % ("\n".join(lines), body)


def do_mark(a) -> int:
    vault = Path(a.vault).expanduser().resolve()
    f = Path(a.file)
    f = (f if f.is_absolute() else vault / f).resolve()
    if not str(f).startswith(str(vault) + os.sep):
        print("refusing: %s is not inside the vault %s" % (f, vault), file=sys.stderr)
        return 3
    if not f.is_file():
        print("no such handoff: %s" % f, file=sys.stderr)
        return 3
    until = None
    if a.status == "deferred":
        if not a.until:
            print("a deferral needs --until YYYY-MM-DD. 'Later, some time' is a decision to "
                  "drop it: use --status handled --reason '...'", file=sys.stderr)
            return 2
        try:
            d = datetime.date.fromisoformat(a.until)
        except ValueError:
            print("--until must be YYYY-MM-DD, got %r" % a.until, file=sys.stderr)
            return 3
        if d <= today():
            print("--until %s is not in the future; a deferral to today is no deferral"
                  % a.until, file=sys.stderr)
            return 3
        until = a.until
    elif a.until:
        print("--until applies only to --status deferred", file=sys.stderr)
        return 2
    text = f.read_text(encoding="utf-8")
    new = set_frontmatter(text, {"status": a.status, "until": until})
    stamp = today().isoformat()
    entry = "- %s %s%s%s" % (stamp, a.status, (" until " + until) if until else "",
                             (": " + a.reason) if a.reason else "")
    if "\n## Status log\n" not in new:
        new = new.rstrip("\n") + "\n\n## Status log\n\n"
    new = new.rstrip("\n") + "\n" + entry + "\n"
    rel = f.relative_to(vault)
    if a.dry_run:
        print("would mark %s %s%s" % (rel, a.status, (" until " + until) if until else ""))
        return 0
    fd, tmp = tempfile.mkstemp(prefix=".handoff.", dir=str(f.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.replace(tmp, f)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print("could not write %s: %s" % (rel, exc), file=sys.stderr)
        return 3
    print("marked %s %s%s" % (rel, a.status, (" until " + until) if until else ""))
    return 0


def do_list(a) -> int:
    vault = Path(a.vault).expanduser()
    if not vault.is_dir():
        print("no vault at %s" % vault, file=sys.stderr)
        return 3
    rows = all_handoffs(vault, a.project) if a.all else waiting(vault, a.project)
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    if not rows:
        print("no handoff waiting%s" % ((" in " + a.project) if a.project else ""))
        return 0
    for r in rows:
        print(("[%s] " % r["status"] if a.all else "") + line(r))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gt_handoff_status.py",
                                 description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("list", help="handoffs waiting on someone (open, or deferral ended)")
    l.add_argument("--vault", required=True)
    l.add_argument("--project", help="one project slug (sub-projects as parent/child)")
    l.add_argument("--all", action="store_true", help="every handoff, whatever its state")
    l.add_argument("--json", action="store_true")
    l.add_argument("--dry-run", action="store_true", help="accepted for symmetry; list writes nothing")
    m = sub.add_parser("mark", help="set a handoff's status")
    m.add_argument("file", help="the handoff, relative to the vault or absolute")
    m.add_argument("--vault", required=True)
    m.add_argument("--status", required=True, choices=STATES)
    m.add_argument("--until", help="YYYY-MM-DD, required with --status deferred")
    m.add_argument("--reason", help="one line, recorded in the handoff's status log")
    m.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    return do_list(a) if a.cmd == "list" else do_mark(a)


if __name__ == "__main__":
    sys.exit(main())
