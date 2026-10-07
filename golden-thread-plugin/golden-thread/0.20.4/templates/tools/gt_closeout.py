#!/usr/bin/env python3
"""Is a project ready to close? Signals, the question, and a record of the answers.

## Why

The vault's write-back habit is strong and its close-out habit is not. On
2026-09-05 the CYC26 talk had been delivered for two days while 25 rehearsal tasks
sat open and overdue, holding the project at PP1 through deadline escalation and
putting 24 dead rows at the top of TASKS.md above live trading work. Nothing asked
whether the project was finished, because nothing was looking.

This tool looks. It computes a few transparent signals per project, names the
ones that look ready to close, and -- the part that matters over time -- records
every time the question was asked and what the answer was, with the signal values
at that moment. The thresholds below are a first guess. `history` shows what the
answers actually looked like, so they can be tuned to how the user closes things
rather than to how a script imagined they would.

## Signals (per project whose stage is not already complete/archived)

  open           open tasks, excluding shelved ones (p >= 7)
  overdue_share  of the open tasks, the share whose due date has passed
  done_share     of all tasks ever listed, the share checked off
  urgent         open tasks at p <= 2
  newest_task    days since the newest open task was raised (its since::)
  last_work      days since the last log.md `[work]` line naming the project

## Rules -- any one firing makes the project a candidate

  R1 past-due   open >= 3 and overdue_share >= 0.8
  R2 done       tasks >= 5, done_share >= 0.8 and urgent <= 1
  R3 quiet      open > 0, newest_task >= 21 and last_work >= 21
  R4 empty      no open tasks at all, and there were tasks once

## Usage

  gt_closeout.py candidates [--json]          projects that look ready, with reasons
  gt_closeout.py signals <slug>               raw numbers for one project
  gt_closeout.py ask <slug> [source]          record that the question was put to the user
  gt_closeout.py answer <slug> yes|no|later [note]   (records the decision; emits no event)
  gt_closeout.py history [slug]               every ask/answer with its signals, for tuning

Records go to `Projects/golden-thread/closeout-signals.jsonl`, one JSON object per
line, append-only. The vault is inferred from this file's location; `--vault`
overrides it. `--dry-run` makes ask/answer say what they would record and write nothing.

A `yes` is the user's decision to close, not the move: it emits no event. The one
`archive` event comes from `vault_init.py archive-project`, which does the archiving.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time

SHELVED_P = 7
FIELD = re.compile(r"\[([a-z_]+)::\s*([^\]]*)\]")
TASK = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+?)\s*$")
RULES = {
    "R1": "past-due: %(open)d open, %(overdue)d of them past their due date",
    "R2": "done: %(done)d of %(total)d tasks checked, %(urgent)d still urgent",
    "R3": "quiet: newest task %(newest_task)dd old, last write-back %(last_work)dd ago",
    "R4": "empty: every task is checked off",
}
# `<date> [<time> <zone>] [work] <slug>[, <slug>...] — <summary>` (see gt-work). Time
# and zone are optional; every slug named before the dash is credited.
WORK_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\s+[^\s\[]+){0,2}\s+\[work\]\s+(.+?)(?:\s+(?:—|–|--?)\s|\s*$)")


def _frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([a-z_]+):\s*(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def _tasks(text):
    m = re.search(r"^## Tasks\s*$", text, re.M)
    if not m:
        return []
    rest = text[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    block = rest[: nxt.start()] if nxt else rest
    out = []
    for line in block.splitlines():
        t = TASK.match(line)
        if not t:
            continue
        f = dict(FIELD.findall(t.group(2)))
        try:
            p = int(f.get("p", 3) or 3)
        except ValueError:
            p = 3
        out.append({"done": t.group(1).lower() == "x", "p": p,
                    "due": f.get("due"), "since": f.get("since")})
    return out


def _days(datestr, today):
    try:
        return (today - dt.date.fromisoformat(datestr)).days
    except Exception:
        return None


def _last_work(vault, slug, today):
    """Days since the last log.md `[work]` line naming <slug>, or None."""
    log = vault / "log.md"
    if not log.exists():
        return None
    last = None
    try:
        for line in log.read_text(errors="replace").splitlines():
            m = WORK_LINE.match(line)
            # exact match: work on `alpha-beta` is not work on `alpha`
            if m and slug in (x.strip() for x in m.group(2).split(",")):
                last = max(last or "", m.group(1))
    except Exception:
        return None
    return _days(last, today) if last else None


def projects(vault):
    pdir = vault / "Projects"
    for readme in sorted(set(pdir.glob("*/README.md")) | set(pdir.glob("*/*/README.md"))):
        text = readme.read_text(errors="replace")
        fm = _frontmatter(text)
        if fm.get("type") != "project":
            continue
        slug = fm.get("slug", readme.parent.name)
        if fm.get("parent"):
            slug = fm["parent"] + "/" + slug
        yield slug, fm, _tasks(text)


def signals(vault, slug, fm, tasks, today=None):
    today = today or dt.date.today()
    live = [t for t in tasks if t["p"] < SHELVED_P]
    open_ = [t for t in live if not t["done"]]
    overdue = [t for t in open_ if t["due"] and (_days(t["due"], today) or 0) > 0]
    ages = [a for a in (_days(t["since"], today) for t in open_) if a is not None]
    lw = _last_work(vault, slug, today)
    return {
        "slug": slug,
        "stage": fm.get("stage", "?"),
        "total": len(tasks),
        "open": len(open_),
        "shelved": len([t for t in tasks if t["p"] >= SHELVED_P and not t["done"]]),
        "done": len([t for t in tasks if t["done"]]),
        "overdue": len(overdue),
        "overdue_share": round(len(overdue) / len(open_), 2) if open_ else 0.0,
        "done_share": round(len([t for t in tasks if t["done"]]) / len(tasks), 2) if tasks else 0.0,
        "urgent": len([t for t in open_ if t["p"] <= 2]),
        "newest_task": min(ages) if ages else None,
        "last_work": lw,
    }


def fired(s):
    out = []
    if s["stage"] in ("complete", "archived"):
        return out
    if s["open"] >= 3 and s["overdue_share"] >= 0.8:
        out.append("R1")
    if s["total"] >= 5 and s["done_share"] >= 0.8 and s["urgent"] <= 1:
        out.append("R2")
    if s["open"] > 0 and (s["newest_task"] or 0) >= 21 and (s["last_work"] or 0) >= 21:
        out.append("R3")
    if s["open"] == 0 and s["total"] > 0:
        out.append("R4")
    return out


def reasons(s, rules):
    return [RULES[r] % {**s, "newest_task": s["newest_task"] or 0, "last_work": s["last_work"] or 0}
            for r in rules]


def candidates(vault, today=None):
    out = []
    for slug, fm, tasks in projects(vault):
        s = signals(vault, slug, fm, tasks, today)
        rules = fired(s)
        if rules:
            out.append({**s, "rules": rules, "reasons": reasons(s, rules)})
    return out


# ------------------------------------------------------------------ record ----

def _record_path(vault):
    return vault / "Projects" / "golden-thread" / "closeout-signals.jsonl"


def _setting(name, default):
    cfg = pathlib.Path.home() / ".claude" / "vault-config.json"
    try:
        return json.loads(cfg.read_text(encoding="utf-8")).get(name, default)
    except Exception:                                           # noqa: BLE001
        return default


def _hard_linked_to_review(vault_real, real):
    """Review m1: an append writes THROUGH a hard link, and realpath cannot see one. A log with
    more than one link is checked against every global-memory/ file and every design.md; only
    the same file as one of those is refused -- any other hard link is the owner's business."""
    try:
        st = os.stat(real)
    except FileNotFoundError:
        return False
    if st.st_nlink < 2:
        return False
    roots = [os.path.join(vault_real, "global-memory")]
    for base in roots + [os.path.join(vault_real, "Projects")]:
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if base == roots[0] or f.lower() == "design.md":
                    try:
                        o = os.stat(os.path.join(dirpath, f))
                    except OSError:
                        continue
                    if (o.st_dev, o.st_ino) == (st.st_dev, st.st_ino):
                        return True
    return False


def _append_path(vault, p):
    """Where record() may append (0.20.2, review M-A). The log may be a link the owner made on
    purpose -- shared with another folder or machine -- so a link is not refused as such; what
    it really points at decides:
      inside the vault, not a review target  -> written through
      design.md or global-memory/             -> refused, always (owner-only, link or not)
      outside the vault                       -> refused unless symlink_writes_outside_vault is
                                                 "allow" (the text is model-chosen: a planted
                                                 link to a shell profile would make it code)
    Refusals exit non-zero and write nothing."""
    vault_real = os.path.realpath(vault)
    real = os.path.realpath(p)
    shown = os.path.relpath(os.path.abspath(p), os.path.abspath(vault)).replace(os.sep, "/")
    if real == vault_real or real.startswith(vault_real + os.sep):
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import gt_review_target as RT                            # noqa: PLC0415
        rel = os.path.relpath(real, vault_real).replace(os.sep, "/")
        if RT.review_target(vault_real, rel) or _hard_linked_to_review(vault_real, real):
            raise SystemExit("gt_closeout: refused: %s resolves to %s, which only the owner edits "
                             "(design.md / global-memory/). Nothing was written." % (shown, rel))
        return real, False
    if _setting("symlink_writes_outside_vault", "refuse") == "allow":
        return real, True
    raise SystemExit("gt_closeout: refused: %s is a link to %s, outside the vault. If that link is "
                     "yours, allow it once from a terminal: gt_settings.py set "
                     "symlink_writes_outside_vault allow. Nothing was written." % (shown, real))


def record(vault, event, slug, **extra):
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, "slug": slug, **extra}
    p = _record_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    target, outside = _append_path(vault, p)
    line = json.dumps(row) + "\n"
    log = _outside_log(vault) if outside else None              # checked BEFORE the outside write
    _append(target, line)
    if log:
        _append(log, json.dumps({
            "ts": row["ts"], "tool": "gt_closeout",
            "link": os.path.relpath(os.path.abspath(p), os.path.abspath(vault)).replace(os.sep, "/"),
            "target": target, "lines": line.count("\n"), "bytes": len(line.encode("utf-8")),
            "text": line}) + "\n")
    return row


OUTSIDE_LOG = "Projects/golden-thread/outside-vault-writes.jsonl"


def _outside_log(vault):
    """The vault's record of every write symlink_writes_outside_vault=allow let through (owner,
    2026-10-06: a blanket allow needs a change log of files and lines, kept in the vault). The
    log itself is never a link, or it could be pointed away; then nothing is written at all."""
    log = os.path.join(os.path.abspath(vault), *OUTSIDE_LOG.split("/"))
    os.makedirs(os.path.dirname(log), exist_ok=True)
    if os.path.islink(log) or os.path.realpath(log) != os.path.join(
            os.path.realpath(vault), *OUTSIDE_LOG.split("/")):
        raise SystemExit("gt_closeout: refused: %s is a link, so an outside write could not be "
                         "logged. Nothing was written." % OUTSIDE_LOG)
    return log


def _append(path, text):
    # O_NOFOLLOW: the path was resolved before, so a link swapped in afterwards is refused, not followed
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_BINARY", 0), 0o644)
    with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def history(vault, slug=None):
    p = _record_path(vault)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if slug is None or r.get("slug") == slug:
            rows.append(r)
    return rows


# -------------------------------------------------------------------- main ----

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=str(pathlib.Path(__file__).resolve().parents[3]))
    ap.add_argument("cmd", nargs="?", default="candidates")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", "-n", action="store_true",
                    help="ask/answer: say what would be recorded; write nothing")
    a = ap.parse_args()
    vault = pathlib.Path(a.vault)
    if not (vault / "Projects").is_dir():
        sys.exit("no Projects/ under %s" % vault)

    if a.cmd == "candidates":
        c = candidates(vault)
        if a.json:
            print(json.dumps(c, indent=1))
            return 0
        if not c:
            print("No project looks ready to close.")
            return 0
        print("Projects that look ready to close (%d):" % len(c))
        for s in c:
            print("  %s  [%s]" % (s["slug"], ", ".join(s["rules"])))
            for r in s["reasons"]:
                print("      %s" % r)
        print("Ask before acting: `gt_closeout.py ask <slug>` then `answer <slug> yes|no|later`.")
        return 0

    if a.cmd == "signals" and a.args:
        for slug, fm, tasks in projects(vault):
            if slug == a.args[0]:
                s = signals(vault, slug, fm, tasks)
                print(json.dumps({**s, "rules": fired(s)}, indent=1))
                return 0
        print("no such project: %s" % a.args[0])
        return 2

    if a.cmd == "ask" and a.args:
        slug = a.args[0]
        src = a.args[1] if len(a.args) > 1 else "manual"
        for s, fm, tasks in projects(vault):
            if s == slug:
                sig = signals(vault, slug, fm, tasks)
                if a.dry_run:
                    print("dry run: would record asked %s; nothing written" % slug)
                    return 0
                record(vault, "asked", slug, source=src, signals=sig, rules=fired(sig))
                print("recorded: asked %s (%s)" % (slug, ", ".join(fired(sig)) or "no rule fired"))
                return 0
        print("no such project: %s" % slug)
        return 2

    if a.cmd == "answer" and len(a.args) >= 2 and a.args[1] in ("yes", "no", "later"):
        note = " ".join(a.args[2:])
        if a.dry_run:
            print("dry run: would record %s -> %s; nothing written" % (a.args[0], a.args[1]))
            return 0
        record(vault, "answered", a.args[0], answer=a.args[1], note=note)
        print("recorded: %s -> %s%s" % (a.args[0], a.args[1], (" (%s)" % note) if note else ""))
        return 0

    if a.cmd == "history":
        rows = history(vault, a.args[0] if a.args else None)
        if not rows:
            print("no close-out history yet")
            return 0
        for r in rows:
            if r["event"] == "asked":
                s = r.get("signals", {})
                print("%s  asked     %-28s via %-11s open=%s overdue=%s done_share=%s last_work=%s rules=%s"
                      % (r["ts"][:16], r["slug"], r.get("source", "?"), s.get("open"), s.get("overdue"),
                         s.get("done_share"), s.get("last_work"), ",".join(r.get("rules", [])) or "-"))
            else:
                print("%s  answered  %-28s %s  %s" % (r["ts"][:16], r["slug"], r.get("answer"), r.get("note", "")))
        yes = [r for r in rows if r["event"] == "answered" and r["answer"] == "yes"]
        no = [r for r in rows if r["event"] == "answered" and r["answer"] != "yes"]
        print("\n%d asked, %d closed, %d declined or deferred. Tune the rules in this file "
              "against what the 'yes' rows looked like." % (
                  len([r for r in rows if r["event"] == "asked"]), len(yes), len(no)))
        return 0

    print(__doc__.strip().splitlines()[0])
    print("usage: gt_closeout.py [candidates [--json] | signals <slug> | ask <slug> [source] | "
          "answer <slug> yes|no|later [note] | history [slug]]")
    return 2


if __name__ == "__main__":
    # A refusal from the OS -- gt sandbox mode, or a macOS interpreter refusal -- is one line
    # naming the next step, not a traceback (0.20.1, B4). gt_errors sits beside this file.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import gt_errors as _gte
    except ImportError:
        _gte = None
    raise SystemExit(_gte.run(main, "gt_closeout") if _gte else main())
