#!/usr/bin/env python3
"""gt_daily.py -- put the day's FACTS into the daily note. Terse by design.

    gt_daily.py --vault V [--date YYYY-MM-DD] [--repo PATH ...] [--dry-run] [--check]

WHAT IT WRITES: tasks closed, commits per repo, event counts, wiki item COUNTS, and an active
span per project. Numbers and one-liners. It does not explain, summarise or interpret -- the
owner writes the meaning, and a generated block that editorialises would encode a reading of
the day that is not theirs. "Capture it all and don't be verbose about every detail" (owner,
2026-09-27) is the whole specification for the output format.

WHERE IT WRITES: one fenced, clearly-marked block in `Daily Notes/<date>.md`, replaced whole on
each run. It never touches a line outside that block, and specifically never touches `##
Noticed`, which is the owner's unfiled capture surface and the section `gt-review` sweeps.

GIT IS THE PRIMARY SOURCE, events are enrichment, and the order matters. Measured on
2026-09-27: `events.jsonl` held 5 events (all `capture`, one project) on a day with 10 commits
across three unrelated work streams, because task events are emitted by `gt_tasks.py` and it
had not run for two days. A job built on the event log alone would have reported a busy day as
almost empty -- confidently, and with no sign anything was missing.

SCHEDULING, and the one thing that provably does not work. Run from launchd at 22:00, with the
script living OUTSIDE CloudStorage (`~/.claude/golden-thread/hooks/`). Reading and writing known
paths inside a CloudStorage vault from launchd DOES work -- `independently verified`, the weekly
lint job has done it on 2026-09-08, 09-17 and 09-24. What does NOT work is listing
`~/.claude/projects/`: a scheduled job can `stat` it and gets True, then lists nothing and
reports a clean empty result. So this tool never reads Claude Code transcripts, and a daily
summary built on them was tried on 2026-09-23 and uninstalled the same hour. Everything here
comes from the vault and from git, which sessions write as they work.

Exit: 0 wrote (or would write) | 1 nothing to report | 2 usage | 3 could not run.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

WROTE, NOTHING, USAGE, CANNOT_RUN = 0, 1, 2, 3

BEGIN = "<!-- gt_daily:begin — GENERATED. Edits inside this block are replaced. -->"
END = "<!-- gt_daily:end -->"
# The heading the block lives under. Deliberately NOT `## Did`: that one is the owner's, and a
# generator writing into it would make their sentences and its counts indistinguishable.
HEADING = "## Captured automatically"

TASK_DONE = re.compile(r"^\+\s*-\s*\[x\]\s+(.*)$", re.I)
TASK_OPEN = re.compile(r"^\+\s*-\s*\[ \]\s+(.*)$")
BOLD = re.compile(r"\*\*(.+?)\*\*")
FIELDS = re.compile(r"\s*\[(?:p|waiting|due|since)::[^\]]*\]")


def sh(args, cwd=None):
    try:
        p = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True, text=True,
                           timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def short(text, limit=88):
    """A task or commit line, trimmed to something a human scans rather than reads."""
    # Use the bold span ONLY when it opens the line. A gt task is written
    # `- [x] **Title** — long explanation`, so the leading bold IS the title and taking it is
    # what makes these lines scannable. A commit subject that merely CONTAINS bold is not that
    # shape, and `search` reduced one to a single character on the first real run -- a summary
    # that silently replaces a line with a fragment of itself.
    m = BOLD.match(text.strip())
    if m:
        text = m.group(1)
    text = FIELDS.sub("", text)
    # Backticks and asterisks only. Stripping `_` as emphasis MANGLES IDENTIFIERS: the first
    # run on real data rendered `gt_daily.py` as `gtdaily.py`, turning a filename someone might
    # grep for into one that does not exist. Underscore emphasis is rare in commit subjects and
    # task titles; underscores in names are not.
    text = re.sub(r"[`*]", "", text).strip().rstrip(".")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def day_bounds(date: str):
    return "%sT00:00:00" % date, "%sT23:59:59" % date


def repo_commits(repo: Path, date: str):
    since, until = day_bounds(date)
    out = sh(["git", "-C", str(repo), "log", "--since", since, "--until", until,
              "--no-merges", "--pretty=%h\t%cI\t%s"], )
    if out is None:
        return None
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            rows.append({"sha": parts[0], "ts": parts[1], "subject": parts[2]})
    return rows


def tasks_closed(repo: Path, date: str):
    """-> {project: [title, ...]} for tasks that went from [ ] to [x] in today's commits.

    Read from the DIFF rather than from task.done events: those are emitted by gt_tasks.py, and
    on 2026-09-27 it had not run for two days, so the event log knew about none of the day's
    work. A closed checkbox in a commit is a fact that does not depend on a tool having been run.
    """
    since, until = day_bounds(date)
    out = sh(["git", "-C", str(repo), "log", "--since", since, "--until", until,
              "--no-merges", "-U0", "-p", "--", "Projects"])
    if not out:
        return {}
    closed = collections.defaultdict(list)
    project = None
    for line in out.splitlines():
        if line.startswith("+++ b/Projects/"):
            m = re.match(r"\+\+\+ b/Projects/([^/]+)/", line)
            project = m.group(1) if m else None
        elif line.startswith("+") and project:
            m = TASK_DONE.match(line)
            if m:
                t = short(m.group(1))
                if t and t not in closed[project]:
                    closed[project].append(t)
    return dict(closed)


def events_for(vault: Path, date: str):
    path = vault / "Projects" / "golden-thread" / "events.jsonl"
    if not path.is_file():
        return [], "events.jsonl is absent"
    rows = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if str(d.get("ts", "")).startswith(date):
                    rows.append(d)
    except OSError as exc:
        return [], "events.jsonl unreadable (%s)" % exc.__class__.__name__
    return rows, None


def wiki_counts(repo: Path, date: str):
    """COUNTS ONLY. The owner asked for "not details for the wiki items or ideas, but just
    time spent and an overview of the number of items" -- so page names stay out."""
    since, until = day_bounds(date)
    out = sh(["git", "-C", str(repo), "log", "--since", since, "--until", until,
              "--no-merges", "--name-status", "--pretty=%x00"])
    added = touched = 0
    if out:
        seen = set()
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or not parts[-1].startswith("Knowledge/"):
                continue
            name = parts[-1]
            if name in seen:
                continue
            seen.add(name)
            if parts[0].startswith("A"):
                added += 1
            touched += 1
    return {"added": added, "touched": touched}


def spans(commits_by_repo, events):
    """First -> last recorded activity per project, as a wall-clock SPAN.

    NOT time worked, and labelled as such wherever it is printed. gt tracks no timers; this is
    the distance between the first and last thing recorded. Presenting it as effort would be
    inventing a number, which is worse than having none.
    """
    seen = collections.defaultdict(list)
    for d in events:
        if d.get("project") and d.get("ts"):
            seen[d["project"]].append(d["ts"])
    for repo, rows in commits_by_repo.items():
        for r in rows or []:
            seen[repo].append(r["ts"])
    out = {}
    for key, stamps in seen.items():
        ts = sorted(s for s in stamps if s)
        if len(ts) < 2:
            out[key] = None
            continue
        try:
            a = datetime.datetime.fromisoformat(ts[0])
            b = datetime.datetime.fromisoformat(ts[-1])
        except ValueError:
            out[key] = None
            continue
        mins = int((b - a).total_seconds() // 60)
        out[key] = "%dh%02dm" % (mins // 60, mins % 60) if mins >= 60 else "%dm" % mins
    return out


def render(date, commits_by_repo, closed, events, wiki, span, notes):
    L = [BEGIN, "", "%s — %s" % (HEADING, date), ""]

    total_commits = sum(len(v or []) for v in commits_by_repo.values())
    kinds = collections.Counter(d.get("kind") for d in events)
    n_closed = sum(len(v) for v in closed.values())

    L.append("**Totals** — %d commit(s) in %d repo(s) · %d task(s) closed · %d event(s) · "
             "%d wiki page(s) touched (%d new)"
             % (total_commits, len([v for v in commits_by_repo.values() if v]), n_closed,
                len(events), wiki["touched"], wiki["added"]))
    L.append("")

    if closed:
        L.append("**Tasks closed**")
        for project in sorted(closed):
            for t in closed[project]:
                L.append("- `%s` — %s" % (project, t))
        L.append("")

    if total_commits:
        L.append("**Commits**")
        for repo in sorted(commits_by_repo):
            rows = commits_by_repo[repo] or []
            if not rows:
                continue
            L.append("- **%s** (%d)" % (repo, len(rows)))
            for r in rows:
                L.append("    - `%s` %s" % (r["sha"], short(r["subject"])))
        L.append("")

    if kinds:
        L.append("**Events** — " + " · ".join("%s %d" % (k, n)
                                              for k, n in sorted(kinds.items())))
        L.append("")

    if span:
        L.append("**Active span** (first→last recorded activity; NOT time worked)")
        for key in sorted(span):
            if span[key]:
                L.append("- %s — %s" % (key, span[key]))
        L.append("")

    for n in notes:
        L.append("> NOTE %s" % n)
    if notes:
        L.append("")

    L.append("_Facts only. The meaning is yours — `## Did`, `## Decided` and `## Noticed` "
             "above are not touched by this._")
    L.append(END)
    return "\n".join(L) + "\n"


def claim_holder(vault: Path, rel: str):
    sessions = vault / "Projects" / "golden-thread" / "sessions"
    if not sessions.is_dir():
        return None
    for f in sorted(sessions.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "status: active" in text and rel in text:
            return f.stem.split("_")[0]
    return None


def splice(existing: str, block: str) -> str:
    """Replace the generated block, or append it under its own heading. Never touch the rest."""
    if BEGIN in existing and END in existing:
        head = existing.split(BEGIN, 1)[0]
        tail = existing.split(END, 1)[1]
        return head + block.rstrip("\n") + tail
    sep = "" if existing.endswith("\n\n") or not existing else "\n"
    return existing + sep + "\n" + block


def check(vault: Path, repos):
    """Fail LOUDLY on each dependency, and use listdir rather than isdir.

    The documented failure mode for a scheduled job on this Mac is not "cannot reach it" but
    "reached it and found nothing": ~/.claude/projects can be stat'd successfully and not
    listed. An existence check cannot see that, so every directory here is LISTED.
    """
    problems = []
    try:
        os.listdir(vault)
    except OSError as exc:
        problems.append("cannot LIST the vault %s (%s)" % (vault, exc.__class__.__name__))
    notes_dir = vault / "Daily Notes"
    if not notes_dir.is_dir():
        problems.append("no 'Daily Notes/' in the vault — run the installer to seed it")
    else:
        try:
            os.listdir(notes_dir)
        except OSError as exc:
            problems.append("cannot LIST Daily Notes/ (%s)" % exc.__class__.__name__)
    if sh(["git", "--version"]) is None:
        problems.append("git is not runnable")
    for r in repos:
        # ASK GIT, do not look for a `.git` directory. A repo subdirectory has no `.git` of its
        # own, so the naive test called the plugin tree "not a git repo" while `git log` in it
        # worked perfectly -- a --check step crying wolf, which is worse than no check because
        # it trains you to ignore the one time it is right.
        if sh(["git", "-C", str(r), "rev-parse", "--git-dir"]) is None:
            problems.append("git cannot read %s (not a repo, or TCC denied)" % r)
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="write the day's facts into the daily note")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--date", default=None, help="default: today, local time")
    ap.add_argument("--repo", action="append", default=[],
                    help="a repo to summarise (repeatable). The vault is always included.")
    ap.add_argument("--dry-run", action="store_true", help="print the block, write nothing")
    ap.add_argument("--check", action="store_true", help="verify every dependency and exit")
    a = ap.parse_args(argv)

    vault = Path(a.vault).expanduser()
    date = a.date or datetime.date.today().isoformat()
    repos = [str(vault)] + [str(Path(r).expanduser()) for r in a.repo]

    if a.check:
        problems = check(vault, repos)
        for p in problems:
            print("gt-daily: CANNOT RUN — %s" % p, file=sys.stderr)
        if not problems:
            print("gt-daily: check passed — vault listable, Daily Notes/ listable, %d repo(s) "
                  "readable" % len(repos))
        return CANNOT_RUN if problems else WROTE

    problems = check(vault, repos)
    if any("vault" in p or "Daily Notes" in p for p in problems):
        for p in problems:
            print("gt-daily: CANNOT RUN — %s" % p, file=sys.stderr)
        return CANNOT_RUN

    notes = [p for p in problems]
    commits_by_repo = {}
    for r in repos:
        rows = repo_commits(Path(r), date)
        if rows is None:
            notes.append("could not read git in %s, so its commits are MISSING from these "
                         "counts" % os.path.basename(r.rstrip("/")))
            continue
        commits_by_repo[os.path.basename(r.rstrip("/")) or r] = rows

    closed = tasks_closed(vault, date)
    events, why = events_for(vault, date)
    if why:
        notes.append(why + ", so event counts are MISSING")
    wiki = wiki_counts(vault, date)
    span = spans(commits_by_repo, events)

    if not any(commits_by_repo.values()) and not closed and not events:
        print("gt-daily: nothing recorded for %s" % date)
        return NOTHING

    block = render(date, commits_by_repo, closed, events, wiki, span, notes)
    if a.dry_run:
        print(block)
        return WROTE

    target = vault / "Daily Notes" / ("%s.md" % date)
    rel = os.path.relpath(target, vault)
    holder = claim_holder(vault, rel)
    if holder:
        print("gt-daily: %s is claimed by session %s — NOT written" % (rel, holder),
              file=sys.stderr)
        return CANNOT_RUN

    existing = ""
    if target.is_file():
        existing = target.read_text(encoding="utf-8")
    else:
        tmpl = vault / "Templates" / "Daily Note.md"
        if tmpl.is_file():
            existing = tmpl.read_text(encoding="utf-8").replace(
                "{{date:YYYY-MM-DD}}", date).replace(
                "{{date:dddd}}", datetime.date.fromisoformat(date).strftime("%A"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(splice(existing, block), encoding="utf-8")
    print("gt-daily: wrote %s — %d commit(s), %d task(s) closed, %d event(s)"
          % (rel, sum(len(v or []) for v in commits_by_repo.values()),
             sum(len(v) for v in closed.values()), len(events)))
    return WROTE


if __name__ == "__main__":
    sys.exit(main())
