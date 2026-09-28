#!/usr/bin/env python3
"""gt_task.py -- create, list and work tasks through a tool instead of by hand.

    gt_task.py add TEXT --vault V (--project SLUG | --inbox) [--ref REF] [--p N]
                        [--waiting user|agent|external|parked] [--due YYYY-MM-DD] [--dry-run]
    gt_task.py list --vault V [FILTER ...] [--json]
    gt_task.py done  ID --vault V [--reason TEXT] [--dry-run]
    gt_task.py drop  ID --vault V --reason TEXT [--dry-run]
    gt_task.py defer ID --vault V --until YYYY-MM-DD --reason TEXT [--dry-run]
    gt_task.py count --vault V [--json]           # for gt_surface: p:: 1 waiting on the owner

FILTERS (any combination; a task must match all of them):
    <slug>        one project (sub-project as parent/child), or `inbox`
    p1 p2 p3      that priority
    mine          waiting:: user
    overdue       due:: before today
    stale         p:: 1 open for more than 7 days
    deferred      only tasks deferred to a later date (hidden otherwise)
    ref:<text>    whose ref:: contains <text>

WHY THIS EXISTS (owner, 2026-09-28). Tasks were hand-typed into `## Tasks` in a format only a
careful writer gets right, and read by nothing but the TASKS.md rollup when someone asked "what's
next". The owner called the task list a black hole. 0.17.2 gave handoffs a status and three verbs
-- surface, list, handle -- and this is the same shape for tasks: create one the way a developer
drops a TODO into code, see them without loading any project's context, and work through a
filtered set to get rid of them.

ONE STORE, ONE PARSER. Tasks stay in each project's README `## Tasks`; this is a writer and a
reader for it, not a second database, and it parses with gt_tasks.py's own `parse_tasks` rules
(imported from beside this file) so a task written here ranks in TASKS.md exactly as a
hand-written one does. A second parser is how the 2026-09-03 multi-line field bug would come back.

IDs are `slug:LINE:HASH` -- the README line number plus six hex of that line's text. Every write
re-reads the line and refuses if the hash no longer matches, so an ID from a list taken before
someone else edited the file can never close the wrong task.

NOTHING IS DELETED. done and drop check the box and append what settled it; drop requires a
reason. A deferral requires a date (`[defer:: D]`): the task is hidden from `list` until then
and comes back on its own. "Later, some time" is a drop with a reason, said out loud -- the same
rule as handoffs.

CORE RULE 1. Before writing a README this asks gt_session whether another LIVE session has claimed
it, and refuses if so. A tool that edits a claimed file because it is not the Write tool would
disarm the rule by the side door.

Exit: 0 ok | 1 refused (claimed by another session, stale ID) | 2 usage | 3 could not do it.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gt_tasks                                             # noqa: E402  the ONE parser

WAITING = ("user", "agent", "external", "parked")
STALE_DAYS = 7
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def today() -> datetime.date:
    pinned = os.environ.get("GT_TODAY")
    if pinned:
        try:
            return datetime.date.fromisoformat(pinned)
        except ValueError:
            pass
    return datetime.date.today()


def _date(s):
    try:
        return datetime.date.fromisoformat(s) if s and DATE.match(s) else None
    except ValueError:
        return None


# --------------------------------------------------------------------- where ----

def readmes(vault: Path):
    """-> [(slug, path)] for every project and sub-project README, plus the inbox."""
    out = []
    root = vault / "Projects"
    if root.is_dir():
        for p in sorted(root.iterdir()):
            if (p / "README.md").is_file():
                out.append((p.name, p / "README.md"))
            if p.is_dir():
                for sub in sorted(p.iterdir()):
                    if sub.is_dir() and (sub / "README.md").is_file():
                        out.append(("%s/%s" % (p.name, sub.name), sub / "README.md"))
    if (vault / "INBOX.md").is_file():
        out.append(("inbox", vault / "INBOX.md"))
    return out


def target(vault: Path, project: str | None, inbox: bool) -> Path | None:
    if inbox:
        return vault / "INBOX.md"
    if not project or ".." in project.split("/") or project.startswith("/"):
        return None
    p = vault / "Projects" / project / "README.md"
    return p if p.is_file() else None


def _h(line: str) -> str:
    return hashlib.sha1(line.strip().encode("utf-8")).hexdigest()[:6]


# ---------------------------------------------------------------------- read ----

def tasks_in(slug: str, path: Path):
    """Open task rows with their line numbers. The INBOX has no `## Tasks` heading, so every
    checkbox line there counts; a README's count only under `## Tasks`, as gt_tasks reads it."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    start, end = 0, len(lines)
    if slug != "inbox":
        hs = [i for i, l in enumerate(lines) if re.match(r"^## Tasks\s*$", l)]
        if not hs:
            return []
        start = hs[0] + 1
        nxt = [i for i in range(start, len(lines)) if lines[i].startswith("## ")]
        end = nxt[0] if nxt else len(lines)
    rows = []
    for i in range(start, end):
        line = lines[i]
        # parse through gt_tasks so the fields mean exactly what the rollup thinks they mean
        parsed = gt_tasks.parse_tasks("## Tasks\n" + line + "\n", where=slug)
        if not parsed or parsed[0]["done"]:
            continue
        t = parsed[0]
        fields = dict(gt_tasks.FIELD.findall(line))
        rows.append({"id": "%s:%d:%s" % (slug, i + 1, _h(line)), "project": slug,
                     "line": i + 1, "text": t["text"], "p": t["p"], "waiting": t["waiting"],
                     "due": t["due"], "since": t["since"], "ref": fields.get("ref"),
                     "defer": fields.get("defer")})
    return rows


def all_tasks(vault: Path):
    out = []
    for slug, path in readmes(vault):
        out += tasks_in(slug, path)
    return out


def matches(r: dict, filters: list[str], now: datetime.date) -> bool:
    d = _date(r["defer"])
    deferred = d is not None and d > now
    if deferred and "deferred" not in filters:
        return False
    if r["p"] >= gt_tasks.SHELVED_P and not any(f.startswith("p") and f[1:].isdigit()
                                                  for f in filters):
        return False                       # shelved: kept as a record, never listed by default
    for f in filters:
        if f == "deferred":
            if not deferred:
                return False
        elif f == "mine":
            if r["waiting"] != "user":
                return False
        elif f == "overdue":
            due = _date(r["due"])
            if not due or due >= now:
                return False
        elif f == "stale":
            s = _date(r["since"])
            if r["p"] != 1 or not s or (now - s).days <= STALE_DAYS:
                return False
        elif re.fullmatch(r"p\d+", f):
            if r["p"] != int(f[1:]):
                return False
        elif f.startswith("ref:"):
            if f[4:].lower() not in (r["ref"] or "").lower():
                return False
        elif r["project"] != f and not r["project"].startswith(f + "/"):
            return False
    return True


def listed(vault: Path, filters: list[str]):
    now = today()
    rows = [r for r in all_tasks(vault) if matches(r, filters, now)]
    rows.sort(key=lambda r: (r["p"], r["since"] or "9999", r["project"], r["line"]))
    return rows


def fmt(r: dict) -> str:
    bits = ["P%d" % r["p"], r["waiting"]]
    if r["due"]:
        bits.append("due " + r["due"])
    if r["defer"]:
        bits.append("deferred to " + r["defer"])
    if r["since"]:
        bits.append("since " + r["since"])
    text = r["text"] if len(r["text"]) <= 110 else r["text"][:107] + "..."
    ref = ("  -> " + r["ref"]) if r["ref"] else ""
    return "%s  [%s]  %s%s" % (r["id"], ", ".join(bits), text, ref)


# ------------------------------------------------------------------ write ----

def claimed_elsewhere(vault: Path, path: Path):
    """-> the holder line if another LIVE session claims this file, else None. gt_session
    missing or failing is not a claim: the write proceeds, as the guard hook fails open."""
    tool = HERE / "gt_session.py"
    if not tool.is_file():
        return None
    try:
        p = subprocess.run([sys.executable, str(tool), "--vault", str(vault), "check",
                            str(path.relative_to(vault))],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 1 else None


def write_lines(path: Path, lines: list[str]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".gt_task.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def resolve_ref(vault: Path, ref: str) -> str | None:
    """-> None if the ref resolves, else what was looked for. `[[Page]]` resolves against any
    markdown file of that name (as Obsidian does); anything else is a vault-relative path."""
    m = re.fullmatch(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]", ref.strip())
    if m:
        name = m.group(1).strip()
        stem = Path(name).name
        if (vault / (name + ".md")).is_file() or any(vault.rglob(stem + ".md")):
            return None
        return "no page named %r anywhere in the vault" % name
    p = (vault / ref.strip()).resolve()
    if str(p).startswith(str(vault.resolve())) and p.exists():
        return None
    return "no file at %s (paths are relative to the vault)" % ref


def cmd_add(a, vault: Path) -> int:
    text = " ".join(a.text).strip()
    if not text or "\n" in text or gt_tasks.FIELD.search(text):
        print("the task text must be one line with no [field:: value] in it -- pass fields as "
              "flags", file=sys.stderr)
        return 2
    path = target(vault, a.project, a.inbox)
    if path is None:
        print("no such project %r (pass --project <slug>, or --inbox)" % a.project,
              file=sys.stderr)
        return 3
    if a.ref:
        why = resolve_ref(vault, a.ref)
        if why:
            print("refusing the ref: %s" % why, file=sys.stderr)
            return 3
    if a.due and not _date(a.due):
        print("--due must be YYYY-MM-DD", file=sys.stderr)
        return 3
    fields = []
    if a.ref:
        fields.append("[ref:: %s]" % a.ref.strip())
    if not a.inbox:
        fields += ["[p:: %d]" % a.p, "[waiting:: %s]" % a.waiting]
        if a.due:
            fields.append("[due:: %s]" % a.due)
    else:
        fields.append("[project:: %s]" % a.project) if a.project else None
    fields.append("[since:: %s]" % today().isoformat())
    line = "- [ ] %s %s" % (text, " ".join(fields))
    lines = path.read_text(encoding="utf-8").splitlines()
    if a.inbox:
        at = len(lines)
    else:
        hs = [i for i, l in enumerate(lines) if re.match(r"^## Tasks\s*$", l)]
        if not hs:
            lines += ["", "## Tasks", ""]
            hs = [len(lines) - 2]
        at = hs[0] + 1
        while at < len(lines) and not lines[at].strip():
            at += 1                          # newest first, directly under the heading
    rel = path.relative_to(vault)
    if a.dry_run:
        print("would add to %s:\n%s" % (rel, line))
        return 0
    holder = claimed_elsewhere(vault, path)
    if holder:
        print("refused -- another live session holds %s:\n  %s" % (rel, holder), file=sys.stderr)
        return 1
    lines.insert(at, line)
    try:
        write_lines(path, lines)
    except OSError as exc:
        print("could not write %s: %s" % (rel, exc), file=sys.stderr)
        return 3
    slug = "inbox" if a.inbox else a.project
    print("added %s:%d:%s\n%s" % (slug, at + 1, _h(line), line))
    return 0


def locate(vault: Path, tid: str):
    """-> (path, index, line) or (None, error)."""
    m = re.fullmatch(r"(.+):(\d+):([0-9a-f]{6})", tid or "")
    if not m:
        return None, "an ID looks like slug:LINE:HASH -- take it from `list`"
    slug, n, h = m.group(1), int(m.group(2)), m.group(3)
    path = (vault / "INBOX.md") if slug == "inbox" else target(vault, slug, False)
    if path is None:
        return None, "no project %r" % slug
    lines = path.read_text(encoding="utf-8").splitlines()
    if n < 1 or n > len(lines) or _h(lines[n - 1]) != h:
        return None, ("the task at %s changed since it was listed (someone edited the file) -- "
                      "list again and use the new ID" % tid)
    if not gt_tasks.TASK.match(lines[n - 1]) or re.match(r"^\s*-\s*\[[xX]\]", lines[n - 1]):
        return None, "%s is not an open task" % tid
    return (path, n - 1, lines), None


def _settle(vault: Path, a, verb: str) -> int:
    found, err = locate(vault, a.id)
    if err:
        print(err, file=sys.stderr)
        return 1
    path, i, lines = found
    old = lines[i]
    stamp = today().isoformat()
    if verb == "defer":
        until = _date(a.until)
        if not until:
            print("--until must be YYYY-MM-DD", file=sys.stderr)
            return 3
        if until <= today():
            print("--until %s is not in the future; a deferral to today is no deferral" % a.until,
                  file=sys.stderr)
            return 3
        new = re.sub(r"\s*\[defer::[^\]]*\]", "", old)
        new = new.rstrip() + " [defer:: %s] — deferred %s: %s" % (a.until, stamp, a.reason)
    else:
        new = re.sub(r"^(\s*-\s*)\[ \]", r"\1[x]", old, count=1)
        word = "done" if verb == "done" else "dropped"
        new = new.rstrip() + " — %s %s%s" % (word, stamp, (": " + a.reason) if a.reason else "")
    rel = path.relative_to(vault)
    if a.dry_run:
        print("would change %s line %d:\n- %s\n+ %s" % (rel, i + 1, old.strip(), new.strip()))
        return 0
    holder = claimed_elsewhere(vault, path)
    if holder:
        print("refused -- another live session holds %s:\n  %s" % (rel, holder), file=sys.stderr)
        return 1
    lines[i] = new
    try:
        write_lines(path, lines)
    except OSError as exc:
        print("could not write %s: %s" % (rel, exc), file=sys.stderr)
        return 3
    print("%s %s" % ({"done": "closed", "drop": "dropped", "defer": "deferred"}[verb], a.id))
    return 0


def cmd_count(a, vault: Path) -> int:
    now = today()
    mine = [r for r in all_tasks(vault) if matches(r, ["p1", "mine"], now)]
    overdue = [r for r in mine if _date(r["due"]) and _date(r["due"]) < now]
    stale = [r for r in mine if matches(r, ["stale"], now)]
    d = {"p1_waiting_on_user": len(mine), "overdue": len(overdue), "stale": len(stale)}
    print(json.dumps(d) if a.json else
          "%d p:: 1 task(s) wait on you (%d overdue, %d open over %dd)"
          % (len(mine), len(overdue), len(stale), STALE_DAYS))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gt_task.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ad = sub.add_parser("add", help="create a task")
    ad.add_argument("text", nargs="+")
    ad.add_argument("--vault", required=True)
    g = ad.add_mutually_exclusive_group(required=True)
    g.add_argument("--project", help="project slug (sub-project as parent/child)")
    g.add_argument("--inbox", action="store_true", help="no project yet: INBOX.md")
    ad.add_argument("--ref", help="[[wiki page]], or a vault path (Sources/..., Projects/...)")
    ad.add_argument("--p", type=int, default=2, choices=(1, 2, 3), help="priority (default 2)")
    ad.add_argument("--waiting", default="user", choices=WAITING)
    ad.add_argument("--due", help="YYYY-MM-DD")
    ad.add_argument("--dry-run", action="store_true")
    ls = sub.add_parser("list", help="open tasks matching every FILTER (read-only)")
    ls.add_argument("filters", nargs="*")
    ls.add_argument("--vault", required=True)
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--dry-run", action="store_true", help="accepted for symmetry; list writes nothing")
    for verb in ("done", "drop", "defer"):
        s = sub.add_parser(verb, help="%s a task by ID" % verb)
        s.add_argument("id")
        s.add_argument("--vault", required=True)
        s.add_argument("--reason", required=(verb != "done"))
        if verb == "defer":
            s.add_argument("--until", required=True, help="YYYY-MM-DD, in the future")
        s.add_argument("--dry-run", action="store_true")
    ct = sub.add_parser("count", help="p:: 1 tasks waiting on the owner (for gt_surface)")
    ct.add_argument("--vault", required=True)
    ct.add_argument("--json", action="store_true")
    ct.add_argument("--dry-run", action="store_true", help="accepted for symmetry; count writes nothing")
    a = ap.parse_args(argv)
    vault = Path(a.vault).expanduser().resolve()
    if not vault.is_dir():
        print("no vault at %s" % vault, file=sys.stderr)
        return 3
    if a.cmd == "add":
        return cmd_add(a, vault)
    if a.cmd == "list":
        rows = listed(vault, a.filters)
        if a.json:
            print(json.dumps(rows, indent=1))
        elif not rows:
            print("no open task matches %s" % (" ".join(a.filters) or "(all)"))
        else:
            for r in rows:
                print(fmt(r))
            print("\n%d task(s)" % len(rows))
        return 0
    if a.cmd == "count":
        return cmd_count(a, vault)
    return _settle(vault, a, a.cmd)


if __name__ == "__main__":
    sys.exit(main())
