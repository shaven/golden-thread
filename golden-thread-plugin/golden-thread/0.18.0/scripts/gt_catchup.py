#!/usr/bin/env python3
"""gt_catchup.py -- a short, generated catch-up for a project you have been away from.

    gt_catchup.py --vault V --project SLUG [--brief | --no-brief] [--mark] [--json] [--dry-run]

Run by /gt:gt-open before its file-reading sequence. It prints ONE prose paragraph (150 words
at most), labelled as a generated summary, when either:

  * `--brief` is given -- always, whatever the absence; or
  * the project has not been opened on this machine for `brief_absence_days` days (setting,
    default 7; `off` never triggers on its own). With no record of a previous open, the age of
    the newest commit touching the project's files stands in for it.

and the project's files have at least one commit in the window (since the last open, or the
last 30 days when there is no record). Otherwise it prints nothing and exits 0 -- a project
nobody touched while you were away has nothing to catch up on.

The paragraph is ASSEMBLED, not written: it is built only from structured fields, never from a
summary of prose --
  1. git log of `Projects/<slug>/` in the window       what changed
  2. the LAST `## ` section of research.md only         what was last learned
  3. the oldest open `[p:: 1]` task in README ## Tasks  what is most urgent
  4. open `[waiting:: user]` tasks                      what needs your decision
It does not replace reading the project; gt-open still reads the files after it.

`--mark` records "opened now" for this project in ~/.claude/golden-thread/state/last-open.json
(machine-local state, not vault content). gt-open passes it, so the next open measures the
absence from this one. `--dry-run` never marks. `--no-brief` suppresses the paragraph (still
marks with --mark).

Exit: 0 always for a readable project (brief or not) | 2 usage | 3 the project cannot be read.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.expanduser("~/.claude/golden-thread/state/last-open.json")
SETTING = "brief_absence_days"
DEFAULT_DAYS = 7
NO_RECORD_WINDOW_DAYS = 30
MAX_WORDS = 150
LABEL = "Generated catch-up (assembled from git and the project files, not authoritative):"
FIELD = re.compile(r"\[([a-z_]+)::\s*([^\]]*)\]")
TASK = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+?)\s*$")


def absence_days():
    """-> int days, or None for `off`. Never raises."""
    try:
        sys.path.insert(0, HERE)
        import gt_settings                                        # noqa: PLC0415
        v = gt_settings.get(SETTING) or str(DEFAULT_DAYS)
    except Exception:
        v = str(DEFAULT_DAYS)
    if v == "off":
        return None
    try:
        return max(int(v), 1)
    except ValueError:
        return DEFAULT_DAYS


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _git(vault, *args):
    try:
        p = subprocess.run(["git", "-C", vault, *args], capture_output=True, text=True,
                           timeout=30)
        return p.stdout if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def load_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def mark(vault, slug, now):
    d = load_state()
    d["%s::%s" % (os.path.realpath(vault), slug)] = now.isoformat(timespec="seconds")
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2)
        os.replace(tmp, STATE)
    except OSError:
        pass


def last_open(vault, slug):
    v = load_state().get("%s::%s" % (os.path.realpath(vault), slug))
    try:
        return datetime.datetime.fromisoformat(v) if v else None
    except ValueError:
        return None


def commits(vault, slug, since):
    """-> [(datetime, subject)] newest first, for commits touching Projects/<slug>/."""
    args = ["log", "--format=%ct%x09%s", "--", "Projects/%s" % slug]
    if since:
        args.insert(1, "--since=%s" % since.isoformat(timespec="seconds"))
    out = _git(vault, *args)
    rows = []
    for line in (out or "").splitlines():
        if "\t" in line:
            ts, subj = line.split("\t", 1)
            try:
                rows.append((datetime.datetime.fromtimestamp(int(ts)).astimezone(), subj.strip()))
            except ValueError:
                continue
    return rows


def last_research(base):
    """The LAST `## ` section of research.md: (heading, first body line). Only that section."""
    text = _read(os.path.join(base, "research.md"))
    if not text:
        return None
    idx = [m.start() for m in re.finditer(r"^## ", text, re.M)]
    if not idx:
        return None
    sec = text[idx[-1]:].split("\n")
    head = sec[0][3:].strip()
    body = next((l.strip() for l in sec[1:] if l.strip() and not l.startswith("#")), "")
    return head, body


def tasks(base):
    """-> open tasks from README ## Tasks: [(text, fields)]."""
    text = _read(os.path.join(base, "README.md")) or ""
    out, inside = [], False
    for line in text.split("\n"):
        if re.match(r"^##\s+Tasks\b", line):
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        m = TASK.match(line) if inside else None
        if m and m.group(1) == " ":
            fields = {k: v.strip() for k, v in FIELD.findall(m.group(2))}
            label = FIELD.sub("", m.group(2)).strip()
            label = re.sub(r"\*\*", "", label).split(" — ")[0].strip()
            out.append((label, fields))
    return out


def _clip(text, words):
    w = text.split()
    return text if len(w) <= words else " ".join(w[:words]) + "…"


def compose(slug, since, rows, research, p1, waiting):
    parts = [LABEL]
    when = ("since you last opened it on %s" % since.strftime("%Y-%m-%d")) if since else \
        "in the last %d days" % NO_RECORD_WINDOW_DAYS
    subj = "; ".join('"%s"' % _clip(s, 10) for _, s in rows[:3])
    more = (" and %d more" % (len(rows) - 3)) if len(rows) > 3 else ""
    parts.append("%s, %d commit(s) touched Projects/%s: %s%s." % (when[0].upper() + when[1:],
                                                                 len(rows), slug, subj, more))
    if research:
        parts.append('Most recent research: "%s"%s.' % (
            _clip(research[0], 14), (" — " + _clip(research[1], 18)) if research[1] else ""))
    if p1:
        parts.append("Oldest open p::1 task: %s." % _clip(p1, 20))
    if waiting:
        parts.append("Waiting on you (%d): %s." % (len(waiting), "; ".join(
            _clip(w, 12) for w in waiting[:3])))
    text = " ".join(parts)
    words = text.split()
    if len(words) > MAX_WORDS:
        text = " ".join(words[:MAX_WORDS - 1]) + " …"
    return text


def build(vault, slug, force, now):
    """-> (brief text or None, why) -- never raises for a readable project."""
    base = os.path.join(vault, "Projects", slug)
    seen = last_open(vault, slug)
    days = absence_days()
    if not force:
        if days is None:
            return None, "brief_absence_days is off"
        ref = seen
        if ref is None:
            newest = commits(vault, slug, None)[:1]
            ref = newest[0][0] if newest else None
        if ref is None:
            return None, "no record of a previous open and no commits to the project"
        away = (now - ref).total_seconds() / 86400
        if away < days:
            return None, "away %.1f day(s), under the %d-day threshold" % (away, days)
    since = seen or (now - datetime.timedelta(days=NO_RECORD_WINDOW_DAYS))
    rows = commits(vault, slug, since)
    if not rows:
        return None, "no commits to Projects/%s in the window" % slug
    open_tasks = tasks(base)
    p1 = [(f.get("since") or "9999", t) for t, f in open_tasks if f.get("p", "").strip() == "1"]
    p1 = min(p1)[1] if p1 else None
    waiting = [t for t, f in open_tasks if f.get("waiting", "").strip().lower() == "user"]
    return compose(slug, seen, rows, last_research(base), p1, waiting), "ok"


def main(argv=None):
    ap = argparse.ArgumentParser(description="a generated catch-up for a returning user")
    ap.add_argument("--vault", required=True, help="the vault (never inferred)")
    ap.add_argument("--project", required=True, help="project slug")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--brief", action="store_true", help="always brief, whatever the absence")
    g.add_argument("--no-brief", action="store_true", help="never brief")
    ap.add_argument("--mark", action="store_true", help="record this open (gt-open passes it)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="compute it but never --mark")
    a = ap.parse_args(argv)
    if "/" in a.project or a.project in (".", "..") or os.path.isabs(a.project):
        print("--project takes a slug, not a path: %r" % a.project, file=sys.stderr)
        return 2
    vault = os.path.abspath(os.path.expanduser(a.vault))
    if not os.path.isdir(os.path.join(vault, "Projects", a.project)):
        print("no project at Projects/%s" % a.project, file=sys.stderr)
        return 3
    now = datetime.datetime.now().astimezone()
    if a.no_brief:
        text, why = None, "--no-brief"
    else:
        text, why = build(vault, a.project, a.brief, now)
    if a.mark and not a.dry_run:
        mark(vault, a.project, now)
    if a.json:
        print(json.dumps({"brief": text, "why": why,
                          "words": len(text.split()) if text else 0}, indent=2))
    elif text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
