#!/usr/bin/env python3
"""gt_daily.py -- put the day's FACTS into the daily note. Terse by design.

    gt_daily.py --vault V [--date YYYY-MM-DD] [--repo PATH ...] [--dry-run] [--check]

WHAT IT WRITES: new projects, tasks closed, tasks added, tasks due today that are still open,
commits, event counts, wiki item COUNTS, and an active span per project. Numbers and one-liners.

GROUPED BY DOMAIN, then project (0.17.9): every task list and the commits are split under
`### <domain>` sub-headings read from each project README's `domain:` frontmatter, alphabetical,
with `uncategorized` last for projects that declare none. A commit belongs to a repo, not a
project, so the rule is: a VAULT commit is filed under the project whose files it changed most;
one that touched no project, and every commit in an extra `--repo`, is filed under
`uncategorized` by repo name. "Tasks added" is read from the diff like "Tasks closed"; an open
task whose title was also REMOVED that day was edited or moved, not added, and is not counted.
"Due today" reads the READMEs as they are when the job runs, so it is the day's remaining open
obligations, not what changed.

PUSH FIRST (0.17.9): before writing, `git -C <vault> push`, so what the note sits on is in the
remote. A failure -- or a vault with no remote -- adds a NOTE to the block and the write goes
ahead: the note is worth having even when the push is not. `--dry-run` never pushes; `--check`
reports an unreachable remote as a problem and a missing one as information. It does not explain, summarise or interpret -- the
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

Exit: 0 wrote (or would write) | 1 nothing to report | 2 usage | 3 could not run (a crash
included: Python's own exit 1 would read as "nothing to report").
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
TASK_OPEN_REMOVED = re.compile(r"^-\s*-\s*\[ \]\s+(.*)$")
OPEN_LINE = re.compile(r"^\s*-\s*\[ \]\s+(.*)$")
DOMAIN_LINE = re.compile(r"^domain:\s*([^\s#]+)\s*$", re.M)
UNCATEGORIZED = "uncategorized"
PUSH_TIMEOUT = 60
# Tasks live under `## Tasks` in a project README (CONVENTIONS). Reading every file under
# Projects/ counted handoff checklists ("Do the tests pass right now?") as tasks closed and
# added -- seen on the first real run of the added-tasks list, 2026-09-30.
TASK_FILES = ":(glob)Projects/**/README.md"
BOLD = re.compile(r"\*\*(.+?)\*\*")
FIELDS = re.compile(r"\s*\[(?:p|waiting|due|since)::[^\]]*\]")


def sh(args, cwd=None, timeout=120):
    try:
        p = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
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
    """-> [{sha, ts, subject, files}] for the day, or None when git cannot read the repo."""
    since, until = day_bounds(date)
    out = sh(["git", "-C", str(repo), "log", "--since", since, "--until", until,
              "--no-merges", "--name-only", "--pretty=%x00%h\t%cI\t%s"], )
    if out is None:
        return None
    rows = []
    for chunk in out.split("\0"):
        lines = [l for l in chunk.splitlines() if l.strip()]
        if not lines:
            continue
        parts = lines[0].split("\t", 2)
        if len(parts) == 3:
            rows.append({"sha": parts[0], "ts": parts[1], "subject": parts[2],
                         "files": lines[1:]})
    return rows


def project_of(path: str):
    m = re.match(r"Projects/([^/]+)/", path)
    return m.group(1) if m else None


def commit_project(files):
    """The project whose files a commit changed most, or None when it touched no project."""
    counts = collections.Counter(p for p in (project_of(f) for f in files) if p)
    if not counts:
        return None
    top = max(counts.values())
    return sorted(p for p, n in counts.items() if n == top)[0]


def domains(vault: Path):
    """-> {slug: domain} from each project README's frontmatter. A project with none is absent."""
    out = {}
    root = vault / "Projects"
    try:
        slugs = [d for d in os.listdir(root) if (root / d / "README.md").is_file()]
    except OSError:
        return out
    for slug in slugs:
        try:
            text = (root / slug / "README.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.startswith("---"):
            fm = text.split("---", 2)[1] if text.count("---") >= 2 else ""
            m = DOMAIN_LINE.search(fm)
            if m:
                out[slug] = m.group(1).strip().strip("'\"")
    return out


def by_domain(items, dom):
    """{project: [lines]} -> [(domain, [(project, [lines])])], alphabetical, uncategorized last."""
    groups = collections.defaultdict(list)
    for project in sorted(items):
        groups[dom.get(project, UNCATEGORIZED)].append((project, items[project]))
    keys = sorted(k for k in groups if k != UNCATEGORIZED)
    if UNCATEGORIZED in groups:
        keys.append(UNCATEGORIZED)
    return [(k, groups[k]) for k in keys]


def tasks_added(repo: Path, date: str):
    """-> {project: [title, ...]} for open tasks that appeared in today's commits.

    From the diff, like tasks_closed. An edited or moved task shows as a removed line and an
    added one; a title that was also removed that day is therefore not counted as new."""
    since, until = day_bounds(date)
    out = sh(["git", "-C", str(repo), "log", "--since", since, "--until", until,
              "--no-merges", "-U0", "-p", "--", TASK_FILES])
    if not out:
        return {}
    added = collections.defaultdict(list)
    removed = collections.defaultdict(set)
    project = None
    for line in out.splitlines():
        if line.startswith("+++ b/Projects/"):
            m = re.match(r"\+\+\+ b/Projects/([^/]+)/", line)
            project = m.group(1) if m else None
        elif line.startswith("--- "):
            continue
        elif project and line.startswith("-"):
            m = TASK_OPEN_REMOVED.match(line)
            if m:
                removed[project].add(short(m.group(1)))
        elif project and line.startswith("+"):
            m = TASK_OPEN.match(line)
            if m:
                t = short(m.group(1))
                if t and t not in added[project]:
                    added[project].append(t)
    out = {}
    for project, titles in added.items():
        keep = [t for t in titles if t not in removed.get(project, ())]
        if keep:
            out[project] = keep
    return out


def due_today(vault: Path, date: str):
    """-> {project: [title, ...]} for tasks marked `[due:: <date>]` that are still open NOW.

    Read from the files as they stand, not from a diff: at 22:00 this is what is left of the
    day's obligations, which is the useful reading."""
    root = vault / "Projects"
    mark = "[due:: %s]" % date
    out = collections.defaultdict(list)
    for readme in sorted(set(root.glob("*/**/README.md"))):   # ** also matches zero dirs
        slug = readme.relative_to(root).parts[0]
        try:
            text = readme.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = OPEN_LINE.match(line)
            if m and mark in line:
                t = short(m.group(1))
                if t and t not in out[slug]:
                    out[slug].append(t)
    return dict(out)


def new_projects(repo: Path, date: str):
    """Slugs whose top-level README was ADDED in today's commits."""
    since, until = day_bounds(date)
    out = sh(["git", "-C", str(repo), "log", "--since", since, "--until", until,
              "--no-merges", "--diff-filter=A", "--name-only", "--pretty=format:",
              "--", "Projects"])
    slugs = []
    for line in (out or "").splitlines():
        m = re.match(r"^Projects/([^/]+)/README\.md$", line.strip())
        if m and m.group(1) not in slugs:
            slugs.append(m.group(1))
    return sorted(slugs)


def push_vault(vault: Path):
    """Push the vault before writing. -> None on success, else a NOTE for the block."""
    if not (sh(["git", "-C", str(vault), "remote"]) or "").strip():
        return "the vault has no git remote, so nothing was pushed before this was written"
    try:
        p = subprocess.run(["git", "-C", str(vault), "push"], capture_output=True, text=True,
                           timeout=PUSH_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        p = None
    if p is None or p.returncode != 0:
        return "vault push failed before write — committed state may not be in remote"
    return None


def tasks_closed(repo: Path, date: str):
    """-> {project: [title, ...]} for tasks that went from [ ] to [x] in today's commits.

    Read from the DIFF rather than from task.done events: those are emitted by gt_tasks.py, and
    on 2026-09-27 it had not run for two days, so the event log knew about none of the day's
    work. A closed checkbox in a commit is a fact that does not depend on a tool having been run.
    """
    since, until = day_bounds(date)
    out = sh(["git", "-C", str(repo), "log", "--since", since, "--until", until,
              "--no-merges", "-U0", "-p", "--", TASK_FILES])
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


def commits_by_project(commits_by_repo, vault_name):
    """{repo: [row]} -> {label: [row]}: vault commits by the project they changed most (the
    vault's own name when they touched none); every other repo under its own name."""
    out = collections.defaultdict(list)
    for repo, rows in commits_by_repo.items():
        for r in rows or []:
            label = (commit_project(r.get("files", [])) or repo) if repo == vault_name else repo
            out[label].append(r)
    return dict(out)


def task_section(L, title, items, dom):
    """A task list grouped by domain then project; nothing at all when it is empty."""
    if not items:
        return
    L.append("**%s**" % title)
    for domain, projects in by_domain(items, dom):
        L.append("### %s" % domain)
        for project, titles in projects:
            for t in titles:
                L.append("- `%s` — %s" % (project, t))
    L.append("")


def render(date, commits_by_repo, closed, events, wiki, span, notes, added=None, due=None,
           fresh=None, dom=None, vault_name=None):
    added, due, fresh, dom = added or {}, due or {}, fresh or [], dom or {}
    L = [BEGIN, "", "%s — %s" % (HEADING, date), ""]

    total_commits = sum(len(v or []) for v in commits_by_repo.values())
    kinds = collections.Counter(d.get("kind") for d in events)
    n_closed = sum(len(v) for v in closed.values())
    n_added = sum(len(v) for v in added.values())

    L.append("**Totals** — %d commit(s) in %d repo(s) · %d task(s) closed · %d task(s) added · "
             "%d event(s) · %d wiki page(s) touched (%d new)"
             % (total_commits, len([v for v in commits_by_repo.values() if v]), n_closed,
                n_added, len(events), wiki["touched"], wiki["added"]))
    L.append("")

    if fresh:
        L.append("**New projects** — " + ", ".join(fresh))
        L.append("")

    task_section(L, "Tasks closed", closed, dom)
    task_section(L, "Tasks added", added, dom)
    task_section(L, "Due today (open)", due, dom)

    if total_commits:
        L.append("**Commits**")
        grouped = commits_by_project(commits_by_repo, vault_name)
        for domain, projects in by_domain(grouped, dom):
            L.append("### %s" % domain)
            for label, rows in projects:
                L.append("- **%s** (%d)" % (label, len(rows)))
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


def check_push(vault: Path):
    """-> (problem or None, info or None). An unreachable remote is a problem, because the
    nightly push would fail; a vault with NO remote is information, because the job still
    writes and says so in the note."""
    if not (sh(["git", "-C", str(vault), "remote"]) or "").strip():
        return None, "the vault has no git remote — the push step will be skipped and noted"
    if sh(["git", "-C", str(vault), "push", "--dry-run"], timeout=PUSH_TIMEOUT) is None:
        return "git push is not possible from the vault (remote unreachable or rejected)", None
    return None, None


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
        push_problem, push_info = check_push(vault)
        if push_problem:
            problems.append(push_problem)
        for p in problems:
            print("gt-daily: CANNOT RUN — %s" % p, file=sys.stderr)
        if push_info:
            print("gt-daily: note — %s" % push_info)
        if not problems:
            print("gt-daily: check passed — vault listable, Daily Notes/ listable, %d repo(s) "
                  "readable%s" % (len(repos), "" if push_info else ", vault push possible"))
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
    added = tasks_added(vault, date)
    due = due_today(vault, date)
    fresh = new_projects(vault, date)
    dom = domains(vault)
    events, why = events_for(vault, date)
    if why:
        notes.append(why + ", so event counts are MISSING")
    wiki = wiki_counts(vault, date)
    span = spans(commits_by_repo, events)
    vault_name = os.path.basename(str(vault).rstrip("/")) or str(vault)

    if not any(commits_by_repo.values()) and not closed and not added and not events:
        print("gt-daily: nothing recorded for %s" % date)
        return NOTHING

    if a.dry_run:
        # No push: a dry run changes nothing, the remote included.
        print(render(date, commits_by_repo, closed, events, wiki, span, notes, added, due,
                     fresh, dom, vault_name))
        return WROTE

    push_note = push_vault(vault)
    if push_note:
        notes.append(push_note)
    block = render(date, commits_by_repo, closed, events, wiki, span, notes, added, due,
                   fresh, dom, vault_name)

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
    # An uncaught exception exits 1 -- which is NOTHING, the code gt_schedule treats as a
    # normal night. The weekly lint's crashes hid behind exactly that for three weeks
    # (2026-09-28), so a crash here is reported as what it is: could not run.
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        print("gt-daily: COULD NOT RUN — see the traceback above", file=sys.stderr)
        sys.exit(CANNOT_RUN)
