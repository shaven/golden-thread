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

## Blocked is not finished (0.21.0)

Quiet is not done. A project with an open `p:: 1` task, a task carrying a warning or alarm
marker, or a majority of its open tasks `waiting:: user` is BLOCKED, not finished: it is never
a close candidate, whatever the rules say, and `blocked` lists it with the counts ("these N items
are waiting on you"). Closing would demote its leftovers to `p:: 7`, out of the ranked rollup.

A recorded answer is read. The newest `answered` row per project suppresses the question:
  no      until the project's signals change (a task added or checked, or new write-back) --
          compared with the fingerprint stored on the answer, or for an older answer the signals
          of the `asked` row before it. `closeout_reask_days` in vault-config.json, when set,
          lets a `no` lapse after that many days regardless.
  later   until its re-ask date: `answer <slug> later [--until YYYY-MM-DD | YYYY-MM-DD] [note]`.
          With no date it defers `closeout_later_days` (vault-config.json; default 30; it was a
          fixed 14 before 0.21.0). A past or invalid date is refused and nothing is recorded.
          From the date on the project is listed again if a rule still fires.
  yes     suppresses nothing (the close itself is another tool's move).
`--include-answered` ignores the answers and lists everything.

## Usage

  gt_closeout.py candidates [--json] [--include-answered]   projects that look ready, with reasons
  gt_closeout.py blocked [--json] [--include-answered]      quiet projects that are blocked on you
  gt_closeout.py signals <slug>               raw numbers for one project
  gt_closeout.py ask <slug> [source]          record that the question was put to the user
  gt_closeout.py answer <slug> yes|no|later [--until YYYY-MM-DD | YYYY-MM-DD] [note]   (records the decision; emits no event)
  gt_closeout.py deferred [--json]            projects whose newest answer is a `later` still in force
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
MARKERS = ("\u26a0", "\U0001F6A8")     # warning sign, rotating light: a task flagged as a hazard
LATER_DAYS = 30                          # `answer later` with no date re-asks this far out (was 14)
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
    lines = block.splitlines()
    for i, line in enumerate(lines):
        t = TASK.match(line)
        if not t:
            continue
        # the task's block, as gt_tasks.block_fields reads it (0.21.0): continuation lines up to
        # the next checkbox line or heading, the checkbox line winning a clash
        f = {}
        for cont in lines[i + 1:]:
            if TASK.match(cont) or cont.startswith("#"):
                break
            f.update(FIELD.findall(cont))
        f.update(FIELD.findall(t.group(2)))
        try:
            p = int(f.get("p", 3) or 3)
        except ValueError:
            p = 3
        out.append({"done": t.group(1).lower() == "x", "p": p, "due": f.get("due"),
                    "since": f.get("since"), "waiting": (f.get("waiting") or "agent").strip().lower(),
                    "text": t.group(2)})
    return out


def _days(datestr, today):
    try:
        return (today - dt.date.fromisoformat(datestr)).days
    except Exception:
        return None


def _last_work_date(vault, slug):
    """The date (YYYY-MM-DD) of the last log.md `[work]` line naming <slug>, or None."""
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
    return last


def _last_work(vault, slug, today):
    """Days since the last log.md `[work]` line naming <slug>, or None."""
    last = _last_work_date(vault, slug)
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
    waiting_user = [t for t in open_ if t.get("waiting") == "user"]
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
        "last_work_date": _last_work_date(vault, slug),
        "p1_open": len([t for t in open_ if t["p"] <= 1]),
        "marked": len([t for t in open_ if any(m in t.get("text", "") for m in MARKERS)]),
        "waiting_user": len(waiting_user),
    }


def blocked_on_owner(s):
    """Why a project is blocked rather than finished, or [] -- see 'Blocked is not finished'."""
    why = []
    if s["p1_open"]:
        why.append("%d open p:: 1 task(s)" % s["p1_open"])
    if s["marked"]:
        why.append("%d task(s) flagged with a warning or alarm marker" % s["marked"])
    if s["open"] and s["waiting_user"] * 2 > s["open"]:
        why.append("%d of %d open tasks waiting on you" % (s["waiting_user"], s["open"]))
    return why


def fingerprint(s):
    """What 'the signals changed materially' means: a task added or checked, or new write-back."""
    return {"total": s["total"], "open": s["open"], "last_work_date": s.get("last_work_date")}


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


def _answers(vault):
    """-> {slug: (answered row, the `asked` row before it or None)}: the NEWEST answer per project."""
    out, asked = {}, {}
    for r in history(vault):
        slug = r.get("slug")
        if r.get("event") == "asked":
            asked[slug] = r
        elif r.get("event") == "answered":
            out[slug] = (r, asked.get(slug))
    return out


def _fingerprint_at(answer, asked):
    """The fingerprint an answer was given against: stored on it since 0.21.0, otherwise rebuilt
    from the `asked` row before it, otherwise None."""
    if isinstance(answer.get("fingerprint"), dict):
        return answer["fingerprint"]
    sig = (asked or {}).get("signals")
    if not isinstance(sig, dict) or "total" not in sig or "open" not in sig:
        return None
    last = sig.get("last_work_date")
    if last is None and sig.get("last_work") is not None:
        try:
            when = dt.date.fromisoformat(str(asked["ts"])[:10])
            last = (when - dt.timedelta(days=int(sig["last_work"]))).isoformat()
        except (KeyError, ValueError, TypeError):
            last = None
    return {"total": sig["total"], "open": sig["open"], "last_work_date": last}


def later_days():
    """`closeout_later_days` from vault-config.json: whole days, 1..365; anything else is the default."""
    v = _setting("closeout_later_days", LATER_DAYS)
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return LATER_DAYS
    return n if 1 <= n <= 365 else LATER_DAYS


def reask_date(row):
    """The date a `later` answer holds until: its own, else its record date plus the default."""
    reask = row.get("reask")
    if reask:
        return str(reask)
    try:
        return (dt.date.fromisoformat(str(row.get("ts"))[:10])
                + dt.timedelta(days=later_days())).isoformat()
    except ValueError:
        return None


def deferred(vault, today=None):
    """Projects whose NEWEST answer is a `later` that is still in force -> list of
    {slug, until, note}. A newer yes/no, or a date that has arrived, ends it."""
    today = today or dt.date.today()
    live = {slug for slug, fm, _t in projects(vault) if fm.get("stage") not in ("complete", "archived")}
    out = []
    for slug, (row, _asked) in sorted(_answers(vault).items()):
        until = reask_date(row) if row.get("answer") == "later" else None
        if slug in live and until and today.isoformat() < until:
            out.append({"slug": slug, "until": until, "note": row.get("note", "")})
    return out


def suppressed(answers, s, today):
    """True when a recorded answer says not to ask about this project yet."""
    hit = answers.get(s["slug"])
    if not hit:
        return False
    row, asked = hit
    answer = row.get("answer")
    if answer == "later":
        reask = reask_date(row)
        return bool(reask) and today.isoformat() < reask
    if answer != "no":
        return False                       # a yes is the close itself, which is another tool's move
    days = _setting("closeout_reask_days", 0)
    if isinstance(days, int) and days > 0:
        age = _days(str(row.get("ts"))[:10], today)
        if age is not None and age >= days:
            return False
    then = _fingerprint_at(row, asked)
    return then is None or then == fingerprint(s)


def _scan(vault, today, include_answered):
    answers = {} if include_answered else _answers(vault)
    for slug, fm, tasks in projects(vault):
        s = signals(vault, slug, fm, tasks, today)
        rules = fired(s)
        if rules and not suppressed(answers, s, today or dt.date.today()):
            yield s, rules


def candidates(vault, today=None, include_answered=False):
    """Projects that look finished: a rule fires, nothing blocks on the owner, and no recorded
    answer says not to ask yet."""
    out = []
    for s, rules in _scan(vault, today, include_answered):
        if not blocked_on_owner(s):
            out.append({**s, "rules": rules, "reasons": reasons(s, rules)})
    return out


def blocked(vault, today=None, include_answered=False):
    """Projects a rule WOULD have called finished, held back because they are waiting on the owner."""
    out = []
    for s, rules in _scan(vault, today, include_answered):
        why = blocked_on_owner(s)
        if why:
            out.append({**s, "rules": rules, "blocked_by": why})
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

def summary_line(rep):
    """The one line the report card prints while deferrals are active (never the list)."""
    until = rep["first"] if rep["first"] == rep["last"] else "%s to %s" % (rep["first"], rep["last"])
    return "Close-out: %d project(s) deferred until %s (gt_closeout.py history)" % (rep["count"], until)


def _print_blocked(b):
    if not b:
        return
    print("Quiet, but blocked on you -- not finished (%d):" % len(b))
    for s in b:
        print("  %s  these %d item(s) are waiting on you: %s" % (
            s["slug"], max(s["p1_open"], s["marked"], s["waiting_user"]), "; ".join(s["blocked_by"])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=str(pathlib.Path(__file__).resolve().parents[3]))
    ap.add_argument("cmd", nargs="?", default="candidates")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--include-answered", action="store_true",
                    help="candidates/blocked: ignore recorded answers and list everything")
    ap.add_argument("--dry-run", "-n", action="store_true",
                    help="ask/answer: say what would be recorded; write nothing")
    ap.add_argument("--until", metavar="YYYY-MM-DD",
                    help="answer ... later: hold the question until this date")
    a = ap.parse_intermixed_args()
    vault = pathlib.Path(a.vault)
    if not (vault / "Projects").is_dir():
        sys.exit("no Projects/ under %s" % vault)

    if a.cmd == "blocked":
        b = blocked(vault, include_answered=a.include_answered)
        if a.json:
            print(json.dumps(b, indent=1))
            return 0
        _print_blocked(b)
        return 0

    if a.cmd == "candidates":
        c = candidates(vault, include_answered=a.include_answered)
        if a.json:
            print(json.dumps(c, indent=1))
            return 0
        if not c:
            print("No project looks ready to close.")
        else:
            print("Projects that look ready to close (%d):" % len(c))
            for s in c:
                print("  %s  [%s]" % (s["slug"], ", ".join(s["rules"])))
                for r in s["reasons"]:
                    print("      %s" % r)
            print("Ask before acting: `gt_closeout.py ask <slug>` then `answer <slug> yes|no|later`.")
        _print_blocked(blocked(vault, include_answered=a.include_answered))
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
        slug, ans, rest = a.args[0], a.args[1], list(a.args[2:])
        reask = None
        if a.until and ans != "later":
            print("--until applies to a `later` answer only")
            return 2
        if ans == "later":
            given = a.until
            if given is None and rest and re.fullmatch(r"\d{4}-\d{2}-\d{2}", rest[0]):
                given = rest.pop(0)
            if given is not None:
                try:
                    when = dt.date.fromisoformat(given) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", given) \
                        else None
                except ValueError:
                    when = None
                if when is None:
                    print("not a valid date: %s (use YYYY-MM-DD). Nothing was recorded." % given[:40])
                    return 2
                if when < dt.date.today():
                    print("%s is in the past (today is %s): a deferral has to end in the future. "
                          "Nothing was recorded." % (given, dt.date.today().isoformat()))
                    return 2
                reask = given
            else:
                reask = (dt.date.today() + dt.timedelta(days=later_days())).isoformat()
        note = " ".join(rest)
        if a.dry_run:
            print("dry run: would record %s -> %s; nothing written" % (slug, ans))
            return 0
        extra = {}
        for s_, fm, tasks in projects(vault):
            if s_ == slug:
                extra["fingerprint"] = fingerprint(signals(vault, slug, fm, tasks))
        if reask:
            extra["reask"] = reask
        record(vault, "answered", slug, answer=ans, note=note, **extra)
        print("recorded: %s -> %s%s%s" % (slug, ans, (" until %s" % reask) if reask else "",
                                          (" (%s)" % note) if note else ""))
        return 0

    if a.cmd == "deferred":
        d = deferred(vault)
        rep = {"count": len(d), "first": min((x["until"] for x in d), default=None),
               "last": max((x["until"] for x in d), default=None), "projects": d}
        if a.json:
            print(json.dumps(rep, indent=1))
        elif not d:
            print("no close-out question is deferred")
        else:
            print(summary_line(rep))
            for x in d:
                print("  %s  until %s%s" % (x["slug"], x["until"], ("  " + x["note"]) if x["note"] else ""))
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
    print("usage: gt_closeout.py [candidates [--json] | blocked [--json] | signals <slug> | "
          "ask <slug> [source] | answer <slug> yes|no|later [--until YYYY-MM-DD] [note] | "
          "deferred [--json] | history [slug]]")
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
