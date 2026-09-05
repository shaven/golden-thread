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
  last_work      days since the last `[work] <slug>` line in log.md

## Rules -- any one firing makes the project a candidate

  R1 past-due   open >= 3 and overdue_share >= 0.8
  R2 done       tasks >= 5, done_share >= 0.8 and urgent <= 1
  R3 quiet      open > 0, newest_task >= 21 and last_work >= 21
  R4 empty      no open tasks at all, and there were tasks once

## Usage

  gt_closeout.py candidates [--json]          projects that look ready, with reasons
  gt_closeout.py signals <slug>               raw numbers for one project
  gt_closeout.py ask <slug> [source]          record that the question was put to the user
  gt_closeout.py answer <slug> yes|no|later [note]
  gt_closeout.py history [slug]               every ask/answer with its signals, for tuning

Records go to `Projects/golden-thread/closeout-signals.jsonl`, one JSON object per
line, append-only. The vault is inferred from this file's location; `--vault`
overrides it.
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
    """Days since the last `[work] <slug>` line in log.md, or None."""
    log = vault / "log.md"
    if not log.exists():
        return None
    pat = re.compile(r"^(\d{4}-\d{2}-\d{2}) \[work\] %s\b" % re.escape(slug))
    last = None
    try:
        for line in log.read_text(errors="replace").splitlines():
            m = pat.match(line)
            if m:
                last = m.group(1)
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


def record(vault, event, slug, **extra):
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, "slug": slug, **extra}
    p = _record_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


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
                record(vault, "asked", slug, source=src, signals=sig, rules=fired(sig))
                print("recorded: asked %s (%s)" % (slug, ", ".join(fired(sig)) or "no rule fired"))
                return 0
        print("no such project: %s" % slug)
        return 2

    if a.cmd == "answer" and len(a.args) >= 2 and a.args[1] in ("yes", "no", "later"):
        note = " ".join(a.args[2:])
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
    raise SystemExit(main())
