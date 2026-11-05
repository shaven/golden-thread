#!/usr/bin/env python3
"""gt_daily.py -- put the day's FACTS into the daily note. Terse by design.

    gt_daily.py --vault V [--date YYYY-MM-DD] [--repo PATH ...] [--dry-run] [--check]
    gt_daily.py --vault V [--date YYYY-MM-DD] --sections-only [--dry-run]
    gt_daily.py --vault V --section-add NAME [--title T] [--instructions I]
    gt_daily.py --vault V --section-remove NAME | --sections-list
    gt_daily.py finalize --vault V (--date D | --before D | --all-past) [--dry-run [--diff]]

FINALIZE (0.21.4): once a day has ended its note is written in its final form -- every comment
(template guidance and this job's markers) removed with the generated content kept as plain text,
empty `-` / `- [ ]` placeholders and empty template headings dropped, marked `gt_daily: final` --
by the first run on a later day (the nightly job finalizes the last 7 days' notes; setting
`daily_finalize`, default on), or for a backlog by `finalize`. Today's note is never finalized,
and a final note is never written again. See "the finished day" below.

NAMED SECTIONS (0.17.9): the owner names a section ("comms"); gt_daily puts an empty, marked area
for it in `Daily Notes/<date>.md` and writes `Daily Notes/.handoff/<date>.md` saying where the note
is and which sections are waiting. Another tool reads the handoff and writes between its own
markers; gt_daily never overwrites a section. Sections are per user.

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

WHERE IT WRITES (owner, 2026-10-01): each fact under the heading it belongs to -- new projects,
tasks closed and commits under `## Did`; ADRs added that day under `## Decided`; tasks added and
open ones due today under `## Open at end of day` -- each in its own marked block after whatever
the owner wrote there, replaced whole on each run. Totals, event counts, the active span and any
NOTE stay in one footer block at the bottom. A heading the note lacks is added. It never touches a
line outside its blocks, and never writes into `## Noticed`, which is the owner's unfiled capture
surface and the section `gt-review` sweeps. Until then everything went into the one bottom block,
and the owner's headings stayed empty of the day's facts ("it just appended to the bottom").

HOW IT WRITES (0.17.11, Core rule 1 queue-first): never directly. The whole new note -- computed
from the file exactly as read -- goes to the write queue as ONE `replace-file` request carrying
the hash of what was read, and `gt_broker.py drain` runs at once, so the 22:00 job stays
synchronous. The broker writes it only if the note is unchanged since the read; an owner edit in
Obsidian in between is ESCALATED to them as a task, never overwritten, and a note a live session
has claimed is HELD (left queued; the next drain applies it if nothing changed). Requests already
queued for the note are drained FIRST, so a run never computes on top of a write still waiting.
Not routed, because the queue refuses dot-folders and non-Markdown: the handoff
(`Daily Notes/.handoff/<date>.md`, gt_daily's own file, written only after the note landed) and
the section config (`.gt/daily-sections.json`). Both scripts must sit beside this one -- the
installed copy in the hooks dir included -- and a run without them CANNOT RUN rather than fall
back to a direct write.

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

Exit: 0 wrote (or would write; or the broker escalated the write to the owner as a task, said in
one line) | 1 nothing to report | 2 usage | 3 could not run (a crash included: Python's own exit
1 would read as "nothing to report"; and a note HELD by the broker, claimed by a live session or
behind another drain -- NOT written yet).
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
# The heading the FOOTER lives under. The itemised facts go into the owner's headings (PARTS),
# each inside its own markers, so their sentences and its lines stay distinguishable.
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
        groups[dom.get(project.split("/")[0], UNCATEGORIZED)].append((project, items[project]))
    keys = sorted(k for k in groups if k != UNCATEGORIZED)
    if UNCATEGORIZED in groups:
        keys.append(UNCATEGORIZED)
    return [(k, groups[k]) for k in keys]


def _today(date: str) -> bool:
    return date == datetime.date.today().isoformat()


def _mtime_on(path: Path, date: str) -> bool:
    try:
        return datetime.date.fromtimestamp(path.stat().st_mtime).isoformat() == date
    except OSError:
        return False


def _uncommitted(repo: Path, date: str, pathspec):
    """-> [(status, rel)] for files under `pathspec` that differ from HEAD or are untracked,
    changed on `date` (by mtime). Only for TODAY: the working tree is the state now.

    0.20.1: the daily note read COMMITS only, but gt writes the vault through the write queue
    and never commits it -- so a task added with gt_task, or a project created that day, was
    missing until the owner happened to commit ("0 task(s) added" right after an add, and a
    real new project left out)."""
    if not _today(date):
        return []
    out = sh(["git", "-C", str(repo), "status", "--porcelain", "-z", "--untracked-files=all",
              "--", pathspec])
    if not out:
        return []
    rows, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        status, rel = entry[:2], entry[3:].replace("\\", "/")
        if status[0] in "RC":
            i += 1                      # -z: a rename carries its old path as the next field
        if _mtime_on(repo / rel, date):
            rows.append((status, rel))
    return rows


def task_diff(repo: Path, date: str):
    """The day's task-file diff: today's commits, then (for today) what is changed but not yet
    committed -- a modified README as `git diff HEAD`, an untracked one as an all-added file."""
    since, until = day_bounds(date)
    chunks = [sh(["git", "-C", str(repo), "log", "--since", since, "--until", until,
                  "--no-merges", "-U0", "-p", "--", TASK_FILES]) or ""]
    for status, rel in _uncommitted(repo, date, TASK_FILES):
        if not re.match(r"^Projects/.+/README\.md$", rel):
            continue
        if status == "??" or "A" in status:
            try:
                text = (repo / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            chunks.append("--- /dev/null\n+++ b/%s\n" % rel
                          + "".join("+%s\n" % l for l in text.splitlines()))
        else:
            chunks.append(sh(["git", "-C", str(repo), "diff", "HEAD", "-U0", "--", rel]) or "")
    return "\n".join(c for c in chunks if c)


def tasks_added(repo: Path, date: str):
    """-> {project: [title, ...]} for open tasks that appeared in today's commits.

    From the diff, like tasks_closed. An edited or moved task shows as a removed line and an
    added one; a title that was also removed that day is therefore not counted as new."""
    out = task_diff(repo, date)
    if not out:
        return {}
    added = collections.defaultdict(list)
    removed = collections.defaultdict(set)
    project = None
    for line in out.splitlines():
        line = line.rstrip("\r")
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


ADR_FILES = ":(glob)Projects/**/decisions.md"
ADR_ADDED = re.compile(r"^\+##\s+((ADR-\d+)\b.*)$")
ADR_REMOVED = re.compile(r"^-##\s+(ADR-\d+)\b")


def adrs_added(repo: Path, date: str):
    """-> {project: [heading, ...]} for `## ADR-n` headings added in today's commits.

    From the diff, like the tasks: an `adr` event exists only when a skill emitted one, and a
    decision written by hand would be missing. The key is the folder holding decisions.md, so a
    sub-project's ADR reads `golden-thread/mcp-gateway`. A heading also removed that day was
    reworded, not decided, and is not counted -- matched on its number, since a reword changes
    the title."""
    since, until = day_bounds(date)
    out = sh(["git", "-C", str(repo), "log", "--since", since, "--until", until,
              "--no-merges", "-U0", "-p", "--", ADR_FILES])
    if not out:
        return {}
    added = collections.defaultdict(list)
    removed = collections.defaultdict(set)
    project = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            m = re.match(r"\+\+\+ b/Projects/(.+)/decisions\.md$", line)
            project = m.group(1) if m else None
        elif line.startswith("--- ") or not project:
            continue
        else:
            m = ADR_REMOVED.match(line)
            if m:
                removed[project].add(m.group(1))
                continue
            m = ADR_ADDED.match(line)
            if m:
                t = (m.group(2), short(m.group(1)))
                if t[1] and t not in added[project]:
                    added[project].append(t)
    out = {}
    for project, titles in added.items():
        keep = [t for n, t in titles if n not in removed.get(project, ())]
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


# gt's own folder: vault_init scaffolds Projects/golden-thread/ (tools, spool, its README) in
# every vault, so the commit that creates a vault "added" it. It is not a project the owner
# started that day (0.20.1: the first daily note on a fresh vault listed it as one).
SCAFFOLD_PROJECTS = ("golden-thread",)


def _project_of_readme(rel: str):
    """'Projects/a/README.md' -> 'a', 'Projects/a/b/README.md' -> 'a/b'; None for anything else
    (the Projects index, a dot-folder, gt's own scaffold)."""
    m = re.match(r"^Projects/(.+)/README\.md$", rel.strip().replace("\\", "/"))
    if not m:
        return None
    slug = m.group(1)
    parts = slug.split("/")
    if parts[0] in SCAFFOLD_PROJECTS or any(p.startswith(".") for p in parts):
        return None
    return slug


def new_projects(repo: Path, date: str):
    """Projects whose README was ADDED that day: in today's commits, or -- for today -- created
    and not yet committed (the write queue never commits). Sub-projects read `parent/child`.
    gt's own scaffold is never one."""
    since, until = day_bounds(date)
    out = sh(["git", "-C", str(repo), "log", "--since", since, "--until", until,
              "--no-merges", "--diff-filter=A", "--name-only", "--pretty=format:",
              "--", "Projects"])
    rels = [l for l in (out or "").splitlines()]
    rels += [rel for status, rel in _uncommitted(repo, date, "Projects")
             if status == "??" or "A" in status]
    slugs = []
    for rel in rels:
        slug = _project_of_readme(rel)
        if slug and slug not in slugs:
            slugs.append(slug)
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
    out = task_diff(repo, date)
    if not out:
        return {}
    closed = collections.defaultdict(list)
    project = None
    for line in out.splitlines():
        line = line.rstrip("\r")
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
        L.append("- _%s_" % domain)
        for project, titles in projects:
            for t in titles:
                L.append("    - `%s` — %s" % (project, t))
    L.append("")


def render_parts(commits_by_repo, closed, added=None, due=None, fresh=None, dom=None,
                 vault_name=None, adrs=None):
    """-> {part key: [lines]} for the owner's sections. An empty list means nothing to place.

    Each fact goes where the owner's template has a heading for it (owner, 2026-10-01): what
    was done under `## Did`, what was decided under `## Decided`, what is still open under `##
    Open at end of day`. `## Noticed` gets nothing -- it is their unfiled capture surface and
    gt-review sweeps every open checkbox there."""
    added, due, fresh, dom, adrs = added or {}, due or {}, fresh or [], dom or {}, adrs or {}
    did = []
    if fresh:
        did += ["**New projects** — " + ", ".join(fresh), ""]
    task_section(did, "Tasks closed", closed, dom)
    if any(commits_by_repo.values()):
        did.append("**Commits**")
        grouped = commits_by_project(commits_by_repo, vault_name)
        for domain, projects in by_domain(grouped, dom):
            did.append("- _%s_" % domain)
            for label, rows in projects:
                did.append("    - **%s** (%d)" % (label, len(rows)))
                for r in rows:
                    did.append("        - `%s` %s" % (r["sha"], short(r["subject"])))
        did.append("")
    decided = []
    task_section(decided, "ADRs recorded", adrs, dom)
    still = []
    task_section(still, "Tasks added", added, dom)
    task_section(still, "Due today (open)", due, dom)
    return {"did": did, "decided": decided, "open": still}


def render(date, commits_by_repo, closed, events, wiki, span, notes, added=None, due=None,
           fresh=None, dom=None, vault_name=None, adrs=None):
    """The footer block: totals, event counts, active span and notes. The itemised facts are
    placed in the owner's sections by render_parts/place_parts, not here."""
    added, adrs = added or {}, adrs or {}
    L = [BEGIN, "", "%s — %s" % (HEADING, date), ""]

    total_commits = sum(len(v or []) for v in commits_by_repo.values())
    kinds = collections.Counter(d.get("kind") for d in events)
    n_closed = sum(len(v) for v in closed.values())
    n_added = sum(len(v) for v in added.values())
    n_adrs = sum(len(v) for v in adrs.values())

    L.append("**Totals** — %d commit(s) in %d repo(s) · %d task(s) closed · %d task(s) added · "
             "%d ADR(s) · %d event(s) · %d wiki page(s) touched (%d new)"
             % (total_commits, len([v for v in commits_by_repo.values() if v]), n_closed,
                n_added, n_adrs, len(events), wiki["touched"], wiki["added"]))
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

    L.append("_Facts only, itemised in marked blocks under `## Did`, `## Decided` and `## Open "
             "at end of day` above. Your own lines there, and all of `## Noticed`, are never "
             "touched._")
    L.append(END)
    return "\n".join(L) + "\n"


# -- the owner's sections: each generated part sits in its own marked block -------------------
#
# Under the heading it belongs to, after whatever the owner wrote there. Only the lines between
# a part's markers are ever replaced, so the owner can write above it at any time of day and a
# re-run leaves that alone. A heading the note lacks is added (before the footer) rather than
# the facts being dropped.
PARTS = (("did", "## Did"), ("decided", "## Decided"), ("open", "## Open at end of day"))


def part_begin(key):
    return "<!-- gt_daily:%s:begin — GENERATED. Edits inside this block are replaced; write " \
           "above it. -->" % key


def part_end(key):
    return "<!-- gt_daily:%s:end -->" % key


def _section_stop(line):
    """True for a line that ends the section above it."""
    t = line.strip()
    return line.startswith("## ") or t == "---" or t.startswith("<!-- gt_daily:")


def place_part(text, key, heading, body):
    """Put one part's block in the note. -> new text. Never alters a line outside its markers."""
    b, e = part_begin(key), part_end(key)
    block = [b] + [l for l in body] + [e]
    while len(block) > 2 and not block[-2].strip():
        block.pop(-2)
    lines = text.split("\n")
    if b in lines and e in lines[lines.index(b):]:
        i = lines.index(b)
        j = lines.index(e, i)
        if body:
            lines[i:j + 1] = block
        else:
            # Nothing to place any more: take the block out, and the blank line put before it.
            k = i - 1 if i > 0 and not lines[i - 1].strip() else i
            lines[k:j + 1] = []
        return "\n".join(lines)
    if not body:
        return text
    if heading in lines:
        h = lines.index(heading)
        stop = next((n for n in range(h + 1, len(lines)) if _section_stop(lines[n])), len(lines))
        last = stop
        while last > h + 1 and not lines[last - 1].strip():
            last -= 1
        lines[last:last] = [""] + block
        return "\n".join(lines)
    # No such heading in this note: add it, before the footer and any named section.
    at = next((n for n, l in enumerate(lines)
               if l == BEGIN or l.startswith("<!-- gt_daily:section:")), None)
    if at is None:
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines + ["", heading, ""] + block) + "\n"
    lines[at:at] = [heading, ""] + block + [""]
    return "\n".join(lines)


def place_parts(text, parts):
    for key, heading in PARTS:
        text = place_part(text, key, heading, parts.get(key) or [])
    return text


# -- the template's pretext (0.21.0) -----------------------------------------------------------
#
# A note made from `Templates/Daily Note.md` carries the template's scaffolding: its guidance
# comments and its empty placeholder lines. Before the job places its blocks it removes exactly
# that -- a comment that is, character for character, one of the TEMPLATE's own comments (read at
# run time, never a copy here), and an empty `-` / `- [ ]` line the template defines. An edited
# comment, a bullet or checkbox with any text, and every heading stay: a line goes only if it is
# still the template's, so nothing the owner wrote can be deleted. Idempotent, and a note with no
# pretext is returned unchanged.
EMPTY_PLACEHOLDER = re.compile(r"^(-|- \[ \])[ \t]*$")
TEMPLATE_REL = ("Templates", "Daily Note.md")
_BLOCK_BEGIN = re.compile(r"^<!-- gt_daily:(?:[A-Za-z0-9_.:-]+:)?begin\b")
_BLOCK_END = re.compile(r"^<!-- gt_daily:(?:[A-Za-z0-9_.:-]+:)?end\b")


def template_text(vault: Path, date: str):
    """-> the daily-note template with its date placeholders filled, or None if it has none."""
    tmpl = vault.joinpath(*TEMPLATE_REL)
    if not tmpl.is_file():
        return None
    return tmpl.read_text(encoding="utf-8").replace("\r\n", "\n").replace(
        "{{date:YYYY-MM-DD}}", date).replace(
        "{{date:dddd}}", datetime.date.fromisoformat(date).strftime("%A"))


def strip_pretext(text: str, template: str | None) -> str:
    """-> text without the template's pretext (see above). `template` None = nothing to strip."""
    if not template:
        return text
    blocks = sorted(_comment_blocks(template), key=lambda b: -len(b))
    placeholders = any(EMPTY_PLACEHOLDER.match(l) for l in template.split("\n"))
    lines = text.split("\n")
    i, changed, fenced, inside = 0, False, False, False
    while i < len(lines):
        ln = lines[i]
        if _BLOCK_BEGIN.match(ln.strip()):
            inside = True
        elif inside and _BLOCK_END.match(ln.strip()):
            inside = False
            i += 1
            continue
        if ln.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        if inside or fenced:
            i += 1
            continue
        n = 0
        for b in blocks:
            if [x.rstrip() for x in lines[i:i + len(b)]] == b:
                n = len(b)
                break
        if not n and placeholders and EMPTY_PLACEHOLDER.match(ln) \
                and (i == 0 or not lines[i - 1].strip() or lines[i - 1].startswith("#")):
            n = 1                   # (a bare `-` under a text line would be a heading underline)
        if not n:
            i += 1
            continue
        del lines[i:i + n]
        changed = True
        # the blank line before and the blank line after would now sit side by side: keep one
        if 0 < i < len(lines) and not lines[i - 1].strip() and not lines[i].strip():
            del lines[i]
    return "\n".join(lines) if changed else text


# -- the finished day: finalize (0.21.4) -------------------------------------------------------
#
# Owner, 2026-10-14: "I want the daily notes to get updated and remove the comments. It shouldn't
# have them once it is done." While a day is in use its note keeps the template's guidance
# comments and this job's marker comments: the markers are what let a re-run replace its blocks
# and never touch the owner's lines. Once the day has ended nothing replaces those blocks again,
# so the scaffolding is only noise, and the note is written in its FINAL form:
#
#   * every HTML comment that stands on lines of its own goes -- template guidance, gt_daily's
#     markers, a named section's markers -- and the generated content between markers stays as
#     plain text (a comment inside a line of the owner's text, or inside a code fence, is the
#     owner's text and is kept);
#   * empty placeholder bullets (a bare `-`, a bare `- [ ]`) go;
#   * a template heading left with nothing under it is DROPPED (a heading the owner added
#     themselves is theirs and stays, empty or not), and so is a trailing `---` rule left with
#     nothing after it; a named section nobody filled goes whole, its generated heading included;
#   * the blank lines around anything removed collapse to one, so no run of blank lines is left;
#   * every other line is kept byte for byte, its line ending included (a CRLF note stays CRLF).
#     A non-empty `- [ ]` in `## Noticed` -- what `/gt:gt-review` sweeps -- is such a line.
#
# The note is marked `gt_daily: final` in its frontmatter. A later run for that day sees the mark
# and does nothing, so no block is re-inserted and nothing is duplicated; finalizing twice is a
# no-op. Today's note is never finalized.
#
# NOT RECOMPUTED. The blocks are kept as the day's last run wrote them, not refreshed from git a
# day later: the write queue never commits, so the day's own run saw tasks and projects in the
# working tree that a later run, reading that past date from the log only, cannot see (the
# 2026-10-12 note: 9 tasks closed, 0 commits). A "last refresh" the next day would delete them.
FINAL_KEY, FINAL_VALUE = "gt_daily", "final"
FINALIZE_HINT = "gt_daily finalize: the day's note in its final form (comments removed)"
FINALIZE_LOOKBACK_DAYS = 7      # the nightly run finalizes this far back; older is the backlog's
NOTE_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
SHIPPED_HEADINGS = ("## Did", "## Decided", "## Noticed", "## Open at end of day")
_PLACEHOLDER_LINE = re.compile(r"^[ \t]*[-*+]([ \t]+\[ \])?[ \t]*$")
_CHECKBOX_PLACEHOLDER = re.compile(r"^[ \t]*[-*+][ \t]+\[ \][ \t]*$")
_LIST_ITEM = re.compile(r"^[ \t]*([-*+]|\d+[.)])([ \t]|$)")
_HR = re.compile(r"^[ \t]{0,3}([-*_])([ \t]*\1){2,}[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(```|~~~)")
_SECTION_MARK = re.compile(r"^<!-- gt_daily:section:([a-z0-9-]+):(begin|end)\b")
_FOOTER_META = re.compile(r"^_Facts only\b.*_\s*$")


def is_final(text) -> bool:
    """True when the note carries `gt_daily: final` in its frontmatter."""
    text = (text or "").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return False
    end = text.find("\n---", 3)
    if end == -1:
        return False
    for line in text[4:end].split("\n"):
        m = re.match(r"^%s\s*:\s*(.*?)\s*$" % re.escape(FINAL_KEY), line)
        if m:
            return m.group(1).strip("'\"").lower() == FINAL_VALUE
    return False


def finalize_enabled() -> bool:
    """The gt setting `daily_finalize` on THIS machine (default on). It governs the AUTOMATIC
    finalize only; `gt_daily.py finalize` is the owner asking, and runs either way."""
    try:
        v = json.loads(CONFIG.read_text(encoding="utf-8")).get("daily_finalize")
    except (OSError, ValueError, AttributeError):
        v = None
    return not (isinstance(v, str) and v.strip().lower() == "off")


def _whole_line_comment_end(lines, i):
    """-> index of the last line of an HTML comment that starts line i and ends a line, or None.
    `<!-- a --> <!-- b -->` on one line counts; text before or after a comment does not."""
    s = lines[i].strip()
    if not s.startswith("<!--"):
        return None
    j, rest = i, s
    while True:
        k = rest.find("-->")
        while k < 0:
            j += 1
            if j >= len(lines):
                return None                         # never closed: not ours to cut to the end
            rest = lines[j]
            k = rest.find("-->")
        rest = rest[k + 3:].strip()
        if not rest:
            return j
        if not rest.startswith("<!--"):
            return None                             # text after the comment: the owner's line


def finalize_text(text: str, template=None):
    """-> (final text, summary dict). `text` may be LF or CRLF; the result keeps its ending.
    Pure: no I/O. A note already final is returned unchanged."""
    crlf = "\r\n" in text and text.count("\r\n") >= text.count("\n") - text.count("\r\n")
    body_all = text.replace("\r\n", "\n")
    stats = {"comments": 0, "markers": 0, "placeholders": 0, "sections": [], "unfilled": [],
             "already": False}
    if is_final(body_all):
        stats["already"] = True
        return text, stats

    front, body = "", body_all
    if body_all.startswith("---\n"):
        end = body_all.find("\n---", 3)
        if end != -1:
            nl = body_all.find("\n", end + 1)
            cut = len(body_all) if nl == -1 else nl + 1
            front, body = body_all[:cut], body_all[cut:]
    lines = body.split("\n")
    trailing_nl = body.endswith("\n")
    if trailing_nl:
        lines.pop()
    gone = [False] * len(lines)

    # 1. named sections nobody filled go whole; a filled one loses only its markers
    i = 0
    while i < len(lines):
        m = _SECTION_MARK.match(lines[i].strip())
        if m and m.group(2) == "begin":
            name = m.group(1)
            j = next((n for n in range(i + 1, len(lines))
                      if lines[n].strip().startswith(sec_end(name))), None)
            if j is not None:
                if not filled("\n".join(lines[i + 1:j])):
                    for n in range(i, j + 1):
                        gone[n] = True
                    stats["unfilled"].append(name)
                i = (j if j is not None else i) + 1
                continue
        i += 1

    # 2. comments and placeholders, outside code fences
    fenced, footer = False, False
    for i, ln in enumerate(lines):
        if gone[i]:
            continue
        if _FENCE.match(ln):
            fenced = not fenced
            continue
        if fenced:
            continue
        s = ln.strip()
        if s == BEGIN:
            footer = True
        elif s == END:
            footer = False
        if footer and _FOOTER_META.match(s):
            gone[i] = True                          # "itemised in marked blocks..." -- not any more
            continue
        e = _whole_line_comment_end(lines, i)
        if e is not None:
            stats["markers" if s.startswith("<!-- gt_daily:") else "comments"] += 1
            for n in range(i, e + 1):
                gone[n] = True
            continue
        if _PLACEHOLDER_LINE.match(ln):
            prev = i - 1 if i > 0 else None
            under_text = (prev is not None and not gone[prev] and lines[prev].strip()
                          and not lines[prev].lstrip().startswith("#")
                          and not _LIST_ITEM.match(lines[prev]))
            if _CHECKBOX_PLACEHOLDER.match(ln) or not under_text:
                gone[i] = True                      # (a bare `-` under text is a heading underline)
                stats["placeholders"] += 1

    # 3. template headings left empty, then a trailing rule with nothing after it
    tmpl_lines = set((template or "").replace("\r\n", "\n").split("\n"))
    droppable = set(SHIPPED_HEADINGS) | {l.rstrip() for l in tmpl_lines if l.startswith("## ")}
    for i, ln in enumerate(lines):
        if gone[i] or ln.rstrip() not in droppable:
            continue
        n = i + 1
        empty = True
        while n < len(lines):
            t = lines[n]
            if not gone[n] and (t.startswith("# ") or t.startswith("## ") or _HR.match(t)):
                break
            if not gone[n] and t.strip():
                empty = False
                break
            n += 1
        if empty:
            gone[i] = True
            stats["sections"].append(ln.rstrip()[3:])
    # a template rule that now separates nothing: last in the note, or straight under the title
    kept = [n for n in range(len(lines)) if not gone[n] and lines[n].strip()]
    for pos, n in enumerate(kept):
        if not (_HR.match(lines[n]) and lines[n].strip() in (tmpl_lines | {"---"})):
            continue
        before = kept[pos - 1] if pos else None
        if pos == len(kept) - 1 or before is None or lines[before].startswith("# "):
            gone[n] = True

    # 4. rebuild: a run of blank and removed lines that removed anything becomes one blank line
    #    (none at the start or the end); every other line, blank runs included, is as it was
    out, i = [], 0
    while i < len(lines):
        if lines[i].strip() and not gone[i]:
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and (gone[j] or not lines[j].strip()):
            j += 1
        run = range(i, j)
        if not any(gone[n] for n in run):
            out.extend(lines[i:j])
        elif out and j < len(lines) and any(not gone[n] for n in run):
            out.append("")
        i = j
    while out and not out[-1].strip():
        out.pop()

    mark = "%s: %s" % (FINAL_KEY, FINAL_VALUE)
    if front:
        fm = front.rstrip("\n").split("\n")        # ["---", ...owner's lines..., "---"]
        at = next((n for n in range(1, len(fm) - 1)
                   if re.match(r"^%s\s*:" % re.escape(FINAL_KEY), fm[n])), None)
        if at is None:
            fm.insert(len(fm) - 1, mark)
        else:
            fm[at] = mark
        front = "\n".join(fm) + "\n"
    else:
        front = "---\n%s\n---\n" % mark
    result = front + "\n".join(out) + ("\n" if out or trailing_nl else "")
    if crlf:
        result = result.replace("\n", "\r\n")
    return result, stats


def summary_line(name, before, after, stats):
    bits = []
    if stats["comments"]:
        bits.append("%d comment(s)" % stats["comments"])
    if stats["markers"]:
        bits.append("%d marker(s)" % stats["markers"])
    if stats["placeholders"]:
        bits.append("%d empty placeholder(s)" % stats["placeholders"])
    if stats["sections"]:
        bits.append("empty section(s) dropped: %s" % ", ".join(stats["sections"]))
    if stats["unfilled"]:
        bits.append("unfilled named section(s) dropped: %s" % ", ".join(stats["unfilled"]))
    nb, na = before.count("\n"), after.count("\n")
    return "%s: %s; %d -> %d lines; marked gt_daily: final" % (
        name, "removes " + ", ".join(bits) if bits else "nothing to remove", nb, na)


def past_notes(vault: Path, today: str, since=None, before=None, only=None):
    """-> [(date, path)] for `Daily Notes/YYYY-MM-DD.md` dated before today (and the bounds)."""
    root = vault / "Daily Notes"
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        m = NOTE_NAME.match(name)
        if not m or not (root / name).is_file():
            continue
        d = m.group(1)
        if d >= today or (since and d < since) or (before and d >= before) or (only and d != only):
            continue
        out.append((d, root / name))
    return out


def finalize_notes(vault: Path, notes, dry_run=False, diff=False, quiet_final=True):
    """Finalize each (date, path). -> (finalized, already, problems) counts; prints a line each."""
    import difflib
    done = already = problems = 0
    for date, path in notes:
        rel = os.path.relpath(path, vault).replace(os.sep, "/")
        try:
            raw = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print("gt-daily: %s — could not read (%s)" % (rel, exc.__class__.__name__),
                  file=sys.stderr)
            problems += 1
            continue
        if is_final(raw):
            already += 1
            if not quiet_final:
                print("gt-daily: %s — already final" % rel)
            continue
        new, stats = finalize_text(raw, template_text(vault, date))
        if dry_run:
            print("gt-daily: would finalize " + summary_line(rel, raw, new, stats))
            if diff:
                sys.stdout.writelines(difflib.unified_diff(
                    raw.replace("\r\n", "\n").splitlines(True),
                    new.replace("\r\n", "\n").splitlines(True), rel, rel + " (final)", n=1))
            done += 1
            continue
        if pending_for(vault, rel):
            drain(vault)
            if pending_for(vault, rel):
                print("gt-daily: %s still has queued writes the broker is holding — not finalized "
                      "yet; the next run tries again" % rel, file=sys.stderr)
                continue
            raw = path.read_bytes().decode("utf-8")
            if is_final(raw):
                already += 1
                continue
            new, stats = finalize_text(raw, template_text(vault, date))
        decision, reason = submit(vault, rel, new, _wq().sha(raw), FINALIZE_HINT)
        if decision in ("apply", "deduplicate"):
            print("gt-daily: finalized " + summary_line(rel, raw, new, stats))
            done += 1
            handoff = vault / "Daily Notes" / HANDOFF_DIR / ("%s.md" % date)
            if handoff.is_file():
                handoff.unlink()                    # the day is over: nothing is open for filling
        elif decision == "escalate":
            print("gt-daily: %s changed while finalizing — NOT written; escalated to you as a "
                  "#conflict task (%s)" % (rel, reason))
        elif decision == "held":
            print("gt-daily: %s — finalize held by the broker (%s); the next run tries again"
                  % (rel, reason), file=sys.stderr)
        else:
            print("gt-daily: %s — NOT finalized (broker: %s, %s)" % (rel, decision, reason),
                  file=sys.stderr)
            problems += 1
    return done, already, problems


def finalize_cli(argv) -> int:
    ap = argparse.ArgumentParser(
        prog="gt_daily.py finalize",
        description="write finished days' notes in their final form: comments, markers and "
                    "empty placeholders removed, marked gt_daily: final. Today is never touched.")
    ap.add_argument("--vault", required=True)
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--date", help="one day, YYYY-MM-DD (must be before today)")
    which.add_argument("--before", help="every note dated before this day (and before today)")
    which.add_argument("--all-past", action="store_true", help="every note dated before today")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what each note would lose, per file; write nothing")
    ap.add_argument("--diff", action="store_true", help="with --dry-run: the unified diff as well")
    a = ap.parse_args(argv)
    vault = Path(a.vault).expanduser()
    today = datetime.date.today().isoformat()
    for d in (a.date, a.before):
        if d:
            try:
                datetime.date.fromisoformat(d)
            except ValueError:
                print("gt-daily: %r is not a YYYY-MM-DD date" % d, file=sys.stderr)
                return USAGE
    if a.date and a.date >= today:
        print("gt-daily: %s is not over yet — today's note is never finalized" % a.date,
              file=sys.stderr)
        return USAGE
    try:
        os.listdir(vault / "Daily Notes")
    except OSError as exc:
        print("gt-daily: CANNOT RUN — cannot LIST %s (%s)" % (vault / "Daily Notes",
                                                              exc.__class__.__name__),
              file=sys.stderr)
        return CANNOT_RUN
    if not a.dry_run and queue_missing():
        print("gt-daily: CANNOT RUN — the write queue is not installed beside this script "
              "(missing: %s); reinstall gt" % ", ".join(queue_missing()), file=sys.stderr)
        return CANNOT_RUN
    notes = past_notes(vault, today, before=a.before, only=a.date)
    if a.date and not notes:
        print("gt-daily: no note for %s" % a.date, file=sys.stderr)
        return NOTHING
    if not finalize_enabled():
        print("gt-daily: note — the setting daily_finalize is off, so the nightly run does not "
              "finalize; this command does, because you asked")
    done, already, problems = finalize_notes(vault, notes, a.dry_run, a.diff,
                                             quiet_final=not a.date)
    print("gt-daily: %s %d note(s); %d already final%s" % (
        "would finalize" if a.dry_run else "finalized", done, already,
        "; %d problem(s)" % problems if problems else ""))
    if problems:
        return CANNOT_RUN
    return WROTE if done else NOTHING


def finalize_due(vault: Path, today: str):
    """The nightly run's share: the past LOOKBACK days' notes not yet final. -> problems."""
    since = (datetime.date.fromisoformat(today)
             - datetime.timedelta(days=FINALIZE_LOOKBACK_DAYS)).isoformat()
    notes = [(d, p) for d, p in past_notes(vault, today, since=since)]
    if not notes:
        return 0
    _done, _already, problems = finalize_notes(vault, notes)
    return problems


def first_write(current) -> bool:
    """True for a note that exists and carries no gt_daily marker: this job has not written it."""
    return current is not None and "<!-- gt_daily:" not in current


def _comment_blocks(template: str):
    """The template's HTML comments, each as its list of right-trimmed lines, when the comment
    stands on lines of its own (starts a line, ends a line)."""
    out = []
    for m in re.finditer(r"<!--.*?-->", template, re.S):
        a = template.rfind("\n", 0, m.start()) + 1
        e = template.find("\n", m.end())
        e = len(template) if e < 0 else e
        if template[a:m.start()].strip() or template[m.end():e].strip():
            continue
        if a != m.start():
            continue                # indented in the template: not matched in a note either
        out.append([l.rstrip() for l in template[a:e].split("\n")])
    return out


# -- the write path: the queue and the broker (Core rule 1, queue-first, 0.17.11) --------------
#
# The claim check that used to live here (a `status: active` grep of sessions/) is the broker's
# job now: it asks the vault's gt_session.py what LIVE means and HOLDS a claimed target.
HERE = Path(__file__).resolve().parent
QUEUE_SCRIPTS = ("gt_write_queue.py", "gt_broker.py")
HINT = "gt_daily: the day's generated blocks for this note; re-run gt_daily once it is resolved"
SESSION = "gt-daily"


def queue_missing():
    """-> the queue scripts absent beside this one (the hooks dir included), or []."""
    return [s for s in QUEUE_SCRIPTS if not (HERE / s).is_file()]


def _wq():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import gt_write_queue                                      # noqa: PLC0415
    return gt_write_queue


def read_note(target: Path):
    """-> (text with LF line ends, the broker's hash of the exact bytes) or (None, None).

    The base hash is taken from the bytes gt_daily computes from, not from a second read, so an
    edit landing between the read and the queueing is caught by the broker as a change."""
    if not target.exists():
        return None, None
    raw = target.read_bytes().decode("utf-8")      # a directory here raises: could not run
    return raw.replace("\r\n", "\n"), _wq().sha(raw)


def pending_for(vault: Path, rel: str):
    """Queued requests for this note, from anyone. Unreadable ones count: the drain decides."""
    out = []
    for p in _wq().pending(vault):
        try:
            if json.loads(p.read_text(encoding="utf-8")).get("path") != rel:
                continue
        except (OSError, ValueError, AttributeError):
            pass
        out.append(p)
    return out


def drain(vault: Path):
    """Run the sibling broker once. -> (rows, error). Synchronous on purpose: the 22:00 job
    reports what happened to its own write, not that it asked for one."""
    try:
        p = subprocess.run([sys.executable, str(HERE / "gt_broker.py"), "drain", "--vault",
                            str(vault), "--json"], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], "the broker could not run (%s)" % exc.__class__.__name__
    if not p.stdout.strip():
        if p.returncode == 0:
            return [], None                        # an empty queue drains silently
        return [], (p.stderr.strip().splitlines() or ["broker exited %d" % p.returncode])[-1]
    try:
        return json.loads(p.stdout).get("results", []), None
    except ValueError:
        return [], "the broker's output was not JSON (exit %d)" % p.returncode


def submit(vault: Path, rel: str, text: str, base, hint=HINT):
    """Queue the whole new note as one replace-file and drain. -> (decision, reason).

    decision: apply | deduplicate | escalate | held | reject, as the broker logged it, or
    `error` when the request could not be queued or the broker could not be asked."""
    wq = _wq()
    req = wq.build(vault, rel, "replace-file", text, None, os.environ.get(
        "CLAUDE_CODE_SESSION_ID") or SESSION, "session", hint)
    req["base_sha256"] = base                       # what THIS text was computed from
    why = wq.validate(req, vault)
    if why:
        return "error", "the queue refused the note: %s" % why
    try:
        wq.deposit(vault, req)
    except OSError as exc:
        return "error", "could not queue the note (%s)" % exc.__class__.__name__
    rows, err = drain(vault)
    row = next((r for r in rows if r.get("request") == req["id"]), None)
    if row is None:
        return "held", err or "the broker did not decide it (another drain is running?)"
    if row.get("conflict"):
        return row["decision"], "%s -> %s" % (row.get("reason", ""), row["conflict"])
    return row["decision"], row.get("reason", "")


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


# -- named sections: other tools fill them, gt_daily makes room and says where ------------
#
# Owner design, 2026-09-30, replacing the comms request's fixed `none`/`m365`/`<local app>` setting:
# the owner NAMES a section ("comms"), gt_daily puts an empty, marked area for it in the daily
# note -- which always lives in `Daily Notes/<date>.md` -- and writes a HANDOFF file saying where
# the note is, which sections exist and which are still waiting. Another tool (<local app>, a Claude
# session with M365 tools, anything) reads the handoff and writes its content between its own
# markers. gt_daily never overwrites a section's content, so any number of writers share the
# note without colliding, and gt_daily itself never reaches for mail, Teams or a network.
# In the SHARED vault, so every machine one user works from agrees on the sections and on the
# comms policy (owner, 2026-09-30: one user, several machines, one vault, one source tree).
SECTIONS_REL = os.path.join(".gt", "daily-sections.json")
HANDOFF_DIR = ".handoff"                    # inside Daily Notes/; Obsidian hides dot-folders
SECTION_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
PLACEHOLDER = "_Nothing handed off yet._"
CONFIG = Path(os.path.expanduser("~/.claude/vault-config.json"))


def _vault_cfg(vault):
    try:
        d = json.loads((Path(vault) / SECTIONS_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _save_vault_cfg(vault, d):
    path = Path(vault) / SECTIONS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes((json.dumps(d, indent=2) + "\n").encode("utf-8"))
    os.replace(tmp, path)


def machine_forces_off():
    """The gt setting `daily_comms_content` on THIS machine: `follow` the vault (default) or
    `off`. A machine can only tighten the vault's policy, never loosen it: the vault is shared,
    so content written on one machine is readable from every other."""
    try:
        v = json.loads(CONFIG.read_text(encoding="utf-8")).get("daily_comms_content")
    except (OSError, ValueError, AttributeError):
        v = None
    return isinstance(v, str) and v.strip().lower() == "off"


def comms_state(vault):
    """-> (allowed, why). Allowed only when the vault says on AND this machine does not say off."""
    vault_on = str(_vault_cfg(vault).get("comms_content", "off")).lower() == "on"
    if not vault_on:
        return False, "off in the vault (the default)"
    if machine_forces_off():
        return False, "on in the vault, but forced off on this machine (daily_comms_content off)"
    return True, "on in the vault"


def sec_begin(name):
    return "<!-- gt_daily:section:%s:begin — filled by another tool; gt_daily never " \
           "overwrites this -->" % name


def sec_end(name):
    return "<!-- gt_daily:section:%s:end -->" % name


def load_sections(vault):
    """-> [{name, title, instructions, comms}] from the vault, shared by every machine."""
    d = _vault_cfg(vault)
    out = []
    for s in d.get("sections", []):
        if isinstance(s, dict) and SECTION_NAME.match(str(s.get("name", ""))):
            out.append({"name": s["name"], "title": s.get("title") or s["name"].title(),
                        "instructions": s.get("instructions", ""),
                        "comms": bool(s.get("comms"))})
    return out


def save_sections(vault, sections):
    d = _vault_cfg(vault)
    d["sections"] = sections
    _save_vault_cfg(vault, d)


def section_content(text, name):
    """-> what sits between a section's markers, or None when the section is not in the note."""
    b, e = sec_begin(name), sec_end(name)
    if b not in text or e not in text:
        return None
    return text.split(b, 1)[1].split(e, 1)[0]


def filled(content):
    body = (content or "").strip()
    lines = [l for l in body.splitlines() if l.strip() and not l.startswith("## ")]
    return bool(lines) and lines != [PLACEHOLDER]


def ensure_sections(text, sections):
    """Append a marked, empty area for each section the note lacks. Never alters one it has."""
    for s in sections:
        if section_content(text, s["name"]) is not None:
            continue
        sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        text += "%s%s\n## %s\n\n%s\n%s\n" % (sep, sec_begin(s["name"]), s["title"], PLACEHOLDER,
                                            sec_end(s["name"]))
    return text


def handoff_text(date, rel_note, sections, note_text, comms_on=False):
    """The handoff: where the note is, which sections wait, and the rules for filling them."""
    L = ["---", "type: daily-handoff", "date: %s" % date, "note: %s" % rel_note,
         "comms_content: %s" % ("on" if comms_on else "off"), "sections:"]
    for s in sections:
        L.append("  - name: %s" % s["name"])
        L.append("    title: %s" % json.dumps(s["title"]))
        L.append("    status: %s" % ("filled" if filled(section_content(note_text, s["name"]))
                                     else "waiting"))
        L.append("    begin: %s" % json.dumps(sec_begin(s["name"])))
        L.append("    end: %s" % json.dumps(sec_end(s["name"])))
        if s["instructions"]:
            L.append("    instructions: %s" % json.dumps(s["instructions"]))
    L += ["---", "", "# Daily note handoff — %s" % date, "",
          "Today's note is `%s`. Each section below has an area in it, between its `begin` and "
          "`end` markers." % rel_note, "",
          "**To contribute:** replace the text between YOUR section's markers — keep the markers, "
          "and the `## Title` line under the begin marker. Touch nothing else in the note: the "
          "generated `gt_daily` blocks are replaced on every run, and the owner's own headings are "
          "theirs. gt_daily never overwrites a section, and its next run marks it `filled` here.",
          ""]
    if not comms_on:
        L += ["**Email and Teams content is OFF for this note** (vault policy or this machine). Do not "
              "write any mail or chat information — subjects, senders, bodies, counts — into any "
              "section of this note.", ""]
    for s in sections:
        L.append("- **%s** (`%s`)%s%s" % (s["title"], s["name"],
                                          " [comms]" if s.get("comms") else "",
                                        " — " + s["instructions"] if s["instructions"] else ""))
    return "\n".join(L) + "\n"


def credential_note(note_text, sections):
    """A NOTE when a filled section holds credential-shaped text -- found, never quoted."""
    body = "\n".join(section_content(note_text, s["name"]) or "" for s in sections)
    if not body.strip():
        return None
    here = Path(__file__).resolve().parent
    scanner = here / "gt_secrets.py"
    if not scanner.is_file():
        return None
    import tempfile
    import shutil
    d = tempfile.mkdtemp(prefix="gt-daily-sections-")
    try:
        (Path(d) / "sections.md").write_bytes(body.encode("utf-8"))
        p = subprocess.run([sys.executable, str(scanner), d], capture_output=True, text=True,
                           timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if p.returncode == 1:
        return ("a handed-off section contains credential-shaped text — review the sections "
                "below (run gt_secrets.py on this note to see where)")
    return None


def sections_cli(a, vault):
    if a.comms_content:
        d = _vault_cfg(vault)
        d["comms_content"] = a.comms_content
        _save_vault_cfg(vault, d)
        allowed, why = comms_state(vault)
        print("gt-daily: comms content is now %s in the vault — effective here: %s (%s)"
              % (a.comms_content, "ON" if allowed else "off", why))
        return WROTE
    sections = load_sections(vault)     # every configured section, comms or not
    if a.sections_list:
        if not sections:
            print("gt-daily: no named sections in this vault (%s)" % SECTIONS_REL)
        for s in sections:
            print("%-16s %s%s%s" % (s["name"], s["title"], "  [comms]" if s["comms"] else "",
                                    "  — " + s["instructions"] if s["instructions"] else ""))
        allowed, why = comms_state(vault)
        if any(s["comms"] for s in sections) and not allowed:
            print("gt-daily: comms content is %s, so [comms] sections are not placed" % why)
        return WROTE
    if a.section_add:
        name = a.section_add.strip().lower()
        if not SECTION_NAME.match(name):
            print("gt-daily: a section name is lowercase letters, digits and -, up to 31",
                  file=sys.stderr)
            return USAGE
        sections = [s for s in sections if s["name"] != name]
        sections.append({"name": name, "title": (a.title or name.replace("-", " ").title()),
                         "instructions": a.instructions or "", "comms": bool(a.comms)})
        save_sections(vault, sections)
        allowed, why = comms_state(vault)
        if a.comms and not allowed:
            print("gt-daily: section %r added as a comms section, but comms content is OFF (%s) — "
                  "it will not appear in the note or the handoff until "
                  "`gt_daily.py --vault V --comms-content on`" % (name, why))
        else:
            print("gt-daily: section %r added — the next run makes room for it and lists it in "
                  "the handoff" % name)
        return WROTE
    name = a.section_remove.strip().lower()
    if not any(s["name"] == name for s in sections):
        print("gt-daily: no section named %r" % name, file=sys.stderr)
        return USAGE
    save_sections(vault, [s for s in sections if s["name"] != name])
    print("gt-daily: section %r removed — existing notes keep what was written in it" % name)
    return WROTE


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "finalize":
        return finalize_cli(argv[1:])
    ap = argparse.ArgumentParser(
        description="write the day's facts into the daily note",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="finished days, in their final form (comments removed):\n"
               "  gt_daily.py finalize --vault V (--date D | --before D | --all-past)\n"
               "                       [--dry-run [--diff]]\n"
               "  gt_daily.py finalize --help")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--date", default=None, help="default: today, local time")
    ap.add_argument("--repo", action="append", default=[],
                    help="a repo to summarise (repeatable). The vault is always included.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the note as it would be written, write nothing")
    ap.add_argument("--check", action="store_true", help="verify every dependency and exit")
    ap.add_argument("--sections-only", action="store_true",
                    help="make room for the named sections and write the handoff; no facts block")
    ap.add_argument("--sections-list", action="store_true", help="list the vault's named sections")
    ap.add_argument("--section-add", metavar="NAME", help="add a named section")
    ap.add_argument("--section-remove", metavar="NAME", help="remove a named section")
    ap.add_argument("--title", help="with --section-add: the heading shown in the note")
    ap.add_argument("--instructions", help="with --section-add: what the filling tool must follow")
    ap.add_argument("--comms-content", choices=("on", "off"),
                    help="the VAULT-WIDE policy for email/Teams content (default off); a machine "
                         "can force it off with the gt setting daily_comms_content")
    ap.add_argument("--comms", action="store_true",
                    help="with --section-add: this section holds email/Teams information, placed "
                         "only while daily_comms_content is on")
    a = ap.parse_args(argv)

    vault = Path(a.vault).expanduser()
    if a.sections_list or a.section_add or a.section_remove or a.comms_content:
        return sections_cli(a, vault)

    date = a.date or datetime.date.today().isoformat()
    repos = [str(vault)] + [str(Path(r).expanduser()) for r in a.repo]
    comms_on, comms_why = comms_state(vault)
    configured = load_sections(vault)
    # A comms section is placed only while the vault allows comms AND this machine does not force
    # it off (daily_comms_content).
    sections = [s for s in configured if comms_on or not s["comms"]]
    held_back = [s for s in configured if s["comms"] and not comms_on]

    if a.check:
        problems = check(vault, repos)
        push_problem, push_info = check_push(vault)
        if push_problem:
            problems.append(push_problem)
        if queue_missing():
            problems.append("the write queue is not installed beside %s (missing: %s), so the "
                            "note cannot be written" % (HERE, ", ".join(queue_missing())))
        for p in problems:
            print("gt-daily: CANNOT RUN — %s" % p, file=sys.stderr)
        if push_info:
            print("gt-daily: note — %s" % push_info)
        if not problems:
            print("gt-daily: check passed — vault listable, Daily Notes/ listable, %d repo(s) "
                  "readable%s" % (len(repos), "" if push_info else ", vault push possible"))
        print("gt-daily: email/Teams content in the daily note is %s — %s"
              % ("ON" if comms_on else "off", comms_why))
        if sections:
            note = vault / "Daily Notes" / ("%s.md" % date)
            text = note.read_text(encoding="utf-8") if note.is_file() else ""
            for s in sections:
                c = section_content(text, s["name"])
                print("gt-daily: section %s — %s" % (s["name"], "not in today's note yet"
                                                    if c is None else ("filled" if filled(c)
                                                                       else "waiting")))
        return CANNOT_RUN if problems else WROTE

    problems = check(vault, repos)
    if any("vault" in p or "Daily Notes" in p for p in problems):
        for p in problems:
            print("gt-daily: CANNOT RUN — %s" % p, file=sys.stderr)
        return CANNOT_RUN

    today = datetime.date.today().isoformat()
    target = vault / "Daily Notes" / ("%s.md" % date)
    rel = os.path.relpath(target, vault).replace(os.sep, "/")
    if target.is_file() and is_final(target.read_bytes().decode("utf-8", errors="replace")):
        # Finalized: its markers are gone, so a run now would re-insert every block below the
        # owner's lines. The day is over and its note is what it is.
        print("gt-daily: %s is final (gt_daily: final) — nothing to do" % rel)
        return NOTHING
    finalize_trouble = 0
    if date == today and not a.dry_run and not a.sections_only and finalize_enabled():
        # The first run on a later day finishes the days before it (the nightly job, normally).
        if queue_missing():
            finalize_trouble = 1
        else:
            finalize_trouble = finalize_due(vault, today)

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
    adrs = adrs_added(vault, date)
    due = due_today(vault, date)
    fresh = new_projects(vault, date)
    dom = domains(vault)
    events, why = events_for(vault, date)
    if why:
        notes.append(why + ", so event counts are MISSING")
    wiki = wiki_counts(vault, date)
    span = spans(commits_by_repo, events)
    vault_name = os.path.basename(str(vault).rstrip("/")) or str(vault)

    handoff = vault / "Daily Notes" / HANDOFF_DIR / ("%s.md" % date)
    quiet = not any(commits_by_repo.values()) and not closed and not added and not events
    facts = not quiet and not a.sections_only
    if held_back:
        print("gt-daily: %d comms section(s) held back — daily_comms_content is off on this "
              "machine" % len(held_back))
    if not facts and not configured:
        print("gt-daily: nothing recorded for %s" % date if quiet else
              "gt-daily: --sections-only, but no named sections in this vault")
        return CANNOT_RUN if finalize_trouble else NOTHING

    def existing_note(text=None):
        if text is not None:
            return text
        if target.is_file():
            return target.read_text(encoding="utf-8")
        tmpl = vault / "Templates" / "Daily Note.md"
        if tmpl.is_file():
            return tmpl.read_text(encoding="utf-8").replace(
                "{{date:YYYY-MM-DD}}", date).replace(
                "{{date:dddd}}", datetime.date.fromisoformat(date).strftime("%A"))
        return ""

    if a.dry_run:
        # No push and no write: a dry run changes nothing, the remote included.
        text = existing_note()
        if first_write(read_note(target)[0]):
            text = strip_pretext(text, template_text(vault, date))
        if not facts and not sections:
            print("gt-daily: nothing to place — every named section is held back (%s)" % comms_why)
        if facts:
            # The whole note as it would be written, so the placement is visible, not just the
            # facts.
            text = splice(place_parts(text, render_parts(commits_by_repo, closed, added, due,
                                                         fresh, dom, vault_name, adrs)),
                          render(date, commits_by_repo, closed, events, wiki, span, notes,
                                 added, due, fresh, dom, vault_name, adrs))
            print(text)
        if sections:
            text = ensure_sections(text, sections)
            for s in sections:
                print("%s\n%s%s" % (sec_begin(s["name"]), section_content(text, s["name"]),
                                    sec_end(s["name"])))
            print("--- handoff: %s ---" % os.path.relpath(handoff, vault).replace(os.sep, "/"))
            print(handoff_text(date, rel, sections, text, comms_on))
        return WROTE

    missing = queue_missing()
    if missing:
        # Never a direct write instead: queue-first is the rule, and a silent fallback is how a
        # rule stops being one.
        print("gt-daily: CANNOT RUN — the write queue is not installed beside this script "
              "(missing: %s); reinstall gt" % ", ".join(missing), file=sys.stderr)
        return CANNOT_RUN
    if pending_for(vault, rel):
        # Land what is already waiting for this note first, so this run computes on top of it
        # rather than racing it -- the broker would escalate a replace-file whose base moved.
        drain(vault)
        if pending_for(vault, rel):
            print("gt-daily: %s still has queued writes the broker is holding (claimed by a live "
                  "session, or another drain is running) — NOT written; see "
                  "spool/broker/log-*.jsonl" % rel, file=sys.stderr)
            return CANNOT_RUN

    current, base = read_note(target)
    # A note that already exists and has none of this job's blocks yet (Obsidian made it from the
    # template) is cleaned of the template's pretext before the first blocks go in. A note this
    # run creates from the template is the template as it always was, and a note the job has
    # written to before is never cleaned again: a re-run changes only the blocks, and the owner's
    # later empty bullet is the owner's (existing tests: create-from-template, dedupe on re-run).
    text = existing_note(current)
    if first_write(current):
        text = strip_pretext(text, template_text(vault, date))
    if sections:
        warn = credential_note(text, sections)
        if warn:
            notes.append(warn)
    for s in held_back:
        # Never deleted -- it is another tool's writing -- but never silent either.
        if filled(section_content(text, s["name"])):
            notes.append("comms section %r holds content although comms content is %s — review "
                         "it and remove it if it should not be here" % (s["name"], comms_why))
    if facts:
        push_note = push_vault(vault)
        if push_note:
            notes.append(push_note)
        text = place_parts(text, render_parts(commits_by_repo, closed, added, due, fresh, dom,
                                              vault_name, adrs))
        text = splice(text, render(date, commits_by_repo, closed, events, wiki, span, notes,
                                   added, due, fresh, dom, vault_name, adrs))
    if sections:
        # After the facts block, so a new note reads: the owner's headings, the facts, then the
        # sections other tools fill. A section already in the note is never moved or altered.
        text = ensure_sections(text, sections)
    decision, reason = submit(vault, rel, text, base)
    if decision == "escalate":
        # Not written: the note changed between this run's read and the write (the owner, in
        # Obsidian). Their edit stands; the broker made them a #conflict task with both versions.
        print("gt-daily: %s changed while this ran — NOT written; the broker escalated it to you "
              "as a #conflict task (%s)" % (rel, reason))
        return WROTE
    if decision not in ("apply", "deduplicate"):
        print("gt-daily: %s — NOT written (broker: %s, %s)%s"
              % (rel, decision, reason, "; it stays queued and the next drain applies it if the "
                 "note is unchanged" if decision == "held" else ""), file=sys.stderr)
        return CANNOT_RUN
    if sections:
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_bytes(handoff_text(date, rel, sections, text, comms_on).encode("utf-8"))
    elif handoff.is_file():
        # Nothing is open for filling (owner, 2026-09-30: no sections, no handoff). A handoff left
        # from earlier in the day -- when comms was on -- would go on inviting tools to write mail
        # into the note, so it is removed rather than left standing.
        handoff.unlink()
    waiting = [s["name"] for s in sections if not filled(section_content(text, s["name"]))]
    print("gt-daily: wrote %s — %d commit(s), %d task(s) closed, %d event(s)%s"
          % (rel, sum(len(v or []) for v in commits_by_repo.values()),
             sum(len(v) for v in closed.values()), len(events),
             "; %d section(s), waiting: %s" % (len(sections), ", ".join(waiting) or "none")
             if sections else ""))
    return CANNOT_RUN if finalize_trouble else WROTE


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
