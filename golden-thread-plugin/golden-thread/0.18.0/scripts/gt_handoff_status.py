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

THROUGH THE WRITE QUEUE (0.17.11). `mark` does not write the handoff: `status:` and `until:` go
to gt_write_queue.py as set-property requests and the status-log line as an append, and the queue
is drained at once by gt_broker.py -- so another LIVE session's claim on the file leaves the mark
QUEUED (exit 3, said on stderr) instead of being written over. The queue cannot delete a key, so a
deferral that ends leaves `until: none`, which reads as no date. A hand-written handoff with no
frontmatter at all is marked with ONE replace-file (new frontmatter block, the original body, the
status-log line) whose base is the hash of the bytes read, so an edit made in between is escalated
by the broker, never overwritten. Nothing here writes the handoff directly.

Exit: 0 ok | 2 usage | 3 could not do it (file missing, outside the vault, bad date, claimed by
another live session -- the mark is then queued, not lost).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

LEGACY_DAYS = 7
NO_DATE = "none"     # what `until:` becomes when a deferral ends: the queue cannot delete a key
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
    if until.lower() == NO_DATE:
        until = ""
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
    raw = f.read_bytes()
    text = raw.decode("utf-8")
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
    if FM.match(text):
        return _mark_via_queue(vault, rel, text, a.status, until, entry)
    # No frontmatter at all (a handoff written by hand before 0.17.2): set-property needs a block
    # to set a key in, so the whole new file goes as ONE replace-file, based on the bytes read.
    return _mark_whole_file(vault, rel, raw, new, a.status, until)


def _wq():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import gt_write_queue                                         # noqa: PLC0415
    return gt_write_queue


def _mark_whole_file(vault: Path, rel, raw: bytes, new: str, status: str, until) -> int:
    """A handoff with no frontmatter: the whole new file (frontmatter block + original body +
    status-log line) as one replace-file, its base the sha256 of the raw bytes this tool READ --
    so an edit made after that read is escalated by the broker, never overwritten. Built from
    gt_write_queue's own build/validate/deposit because submit() records the base at queue time,
    not at read time."""
    import hashlib                                                # noqa: PLC0415
    wq = _wq()
    relp = str(rel).replace(os.sep, "/")
    req = wq.build(vault, relp, "replace-file", new, None, wq.session_id(None), "session", None)
    req["base_sha256"] = hashlib.sha256(raw).hexdigest()
    req["target_existed"] = True
    why = wq.validate(req, vault)
    if why:
        return _report([{"decision": "refused", "reason": why}], None, rel, status, until, wq,
                       vault)
    try:
        wq.deposit(vault, req)
    except OSError as exc:
        print("could not queue %s: %s" % (rel, exc), file=sys.stderr)
        return 3
    rows, note = wq.drain_now(vault)
    row = rows.get(req["id"]) or {"decision": "queued", "reason": "queued; not yet applied"}
    return _report([row], note, rel, status, until, wq, vault)


def _report(results, note, rel, status, until, wq, vault) -> int:
    """0 when every write was applied or already there; else one line on stderr, exit 3."""
    bad = [r for r in results if r["decision"] not in ("apply", "deduplicate")]
    if not bad:
        print("marked %s %s%s" % (rel, status, (" until " + until) if until else ""))
        return 0
    r = bad[0]
    if r["decision"] == "refused":
        print("refusing to mark %s: %s" % (rel, r["reason"]), file=sys.stderr)
    elif r["decision"] == "escalate":
        print("NOT marked: %s changed under the request; escalated to the owner as a #conflict "
              "task (%s)" % (rel, r.get("conflict", "spool/broker/conflicts/")), file=sys.stderr)
    else:
        print("queued, NOT marked yet: %s -- %s; %s" % (
            rel, note or r.get("reason") or r["decision"], wq.drain_hint(vault)), file=sys.stderr)
    return 3


def _mark_via_queue(vault: Path, rel, text: str, status: str, until, entry: str) -> int:
    """status (and until) as set-property, the status-log line as an append -- queued with
    gt_write_queue and drained at once by gt_broker (0.17.11), never written here: a script's
    write is invisible to the hooks, so writing directly would bypass Core rule 1."""
    wq = _wq()
    relp = str(rel).replace(os.sep, "/")
    writes = [{"path": relp, "op": "set-property", "key": "status", "content": status}]
    _has, cur_until = wq.frontmatter_get(text, "until")
    if until:
        writes.append({"path": relp, "op": "set-property", "key": "until", "content": until})
    elif cur_until is not None and cur_until.strip() not in ("", NO_DATE):
        writes.append({"path": relp, "op": "set-property", "key": "until", "content": NO_DATE})
    writes.append({"path": relp, "op": "append", "section": "Status log", "content": entry})
    try:
        results, note = wq.submit(vault, writes)
    except OSError as exc:
        print("could not queue %s: %s" % (rel, exc), file=sys.stderr)
        return 3
    return _report(results, note, rel, status, until, wq, vault)


def _spool():
    """The vault tools' gt_spool (templates/tools): the ONE slug -> Projects/<path>
    resolver (0.18.0), shared with gt_adr and vault_init."""
    tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates", "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import gt_spool
    return gt_spool


def do_list(a) -> int:
    vault = Path(a.vault).expanduser()
    if not vault.is_dir():
        print("no vault at %s" % vault, file=sys.stderr)
        return 3
    if a.project:
        # a bare sub-project slug filters on its parent/child path (0.18.0); unknown or
        # ambiguous is said, not shown as an empty list
        S = _spool()
        try:
            a.project = S.resolve_project(vault, a.project,
            allow_unregistered=True)  # an existing folder, as before; never creates
        except S.ProjectNotFound as exc:
            print("gt_handoff_status: %s" % exc, file=sys.stderr)
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
