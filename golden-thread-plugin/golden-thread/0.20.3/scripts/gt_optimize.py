"""gt_optimize.py -- find content that costs context, and move it to where it costs less.

Memory files are read into every session. A duplicated fact is paid for on every turn, in every
project, forever. This finds that waste. It never REMOVES anything -- what it writes is a
demotion, an archive or a supersede mark, each of which moves or marks content and leaves a
pointer behind.

  gt_optimize.py --vault V [--project SLUG] [--only vault,session] [--cost] [--days N] [--json]
  gt_optimize.py --vault V --demote <path> [--to TIER] [--project SLUG] [--apply | --dry-run]
  gt_optimize.py --vault V --archive --project SLUG --before YYYY-MM-DD [--apply | --dry-run]
  gt_optimize.py --vault V --supersede --file REL --entry HEADING --by HEADING [--apply | --dry-run]
  gt_optimize.py --vault V --member vault [--project SLUG] [--cost] [--unused-days N] [--json]
  gt_optimize.py --vault V --only execution [--project SLUG] [--repo R] [--json]
  gt_optimize.py --execution --vault V [--project P] [--repo R] [--apply ID [--dry-run]]

AN AGGREGATOR OVER TWO MEMBERS (0.18.1), run through gt_aggregate like gt_scan and gt_allin:

    vault     what the vault STORES that a session pays for: duplicated facts, bloated memory,
              dead index rows, relative dates (as before), plus single-project globals,
              never-read Knowledge pages, and the open cost of every project. Paid every
              session, forever. It is this file, run as `--member vault`.
    session   what a session CARRIES: prompt-cache writes classified by cause, the avoidable
              share, context weight per session. Paid once per resume. gt_optimize_session.py.

    execution how work EXECUTES: timings, metrics, settings, ranked by measured cost.
              gt_optimize_exec.py. OPT-IN: reached with `--only execution` (or `--only
              vault,session,execution`), never part of a bare run, which still runs the two
              members above. `--execution ...` is an alias that hands every argument straight to
              gt_optimize_exec.py, so its `--apply ID` (one accepted finding) works through here.

A bare run runs vault and session and says `N of 2 member(s) ran`. A member that could not run -- no
transcripts on this machine, a crash -- is reported as such and sets exit 3; it is never read as
a clean result. `--only vault` is the report this tool printed before it became an aggregator,
with the new finding kinds added to it.

ARCHIVE AND SUPERSEDE (0.18.1). `research.md` is append-only and only ever grows, and a March
finding sits above the April entry that corrected it with equal weight. Two actions, both dry
runs unless `--apply`, both queued through gt_write_queue + gt_broker (Core rule 1), both refused
while another live session claims the file:

  --archive   moves every `## YYYY-MM-DD...` entry dated before --before out of
              Projects/<slug>/research.md into research-archive-<YYYY>.md (by the entry's year),
              and leaves one dated index line per moved entry under `## Archived entries`.
              The archive is written and READ BACK first; research.md shrinks only once every
              moved entry is verified on disk in its archive. A heading that is not a dated
              entry is never touched. Nothing is deleted.
  --supersede marks an entry superseded IN PLACE, with the `superseded_by:` idiom Sources/
              already uses, as a quoted line directly under its heading. The entry stays.

Summarising or rewriting memory is out of scope on purpose: a model compressing a long file
drops the line that mattered, and nothing downstream can detect that it did.

THE ACTION IS DEMOTION, NOT REMOVAL

`--demote` moves a note to where it costs less instead of deleting anything:

    global-memory/          read in EVERY session of every project     most expensive
    Projects/<slug>/memory/ read in every session of one project
    Knowledge/<page>.md     read when someone asks for it              cheapest

A fact in global-memory that only one project needs is charged to every session forever.
Moving it does not make it less true -- it makes it cost what it is worth.

The order is the safety property: WRITE the destination, VERIFY it by reading it back from
disk, and only THEN replace the source with a pointer. A failure at any step leaves
DUPLICATION, which a reader can see and resolve. The deletion-based --apply described below
failed by removing content, which no diff brings back. The mechanics live in gt_demote.py and
are tested there.

WHY THERE IS NO REMOVING --apply

There was one, for about an hour on 2026-09-16, restricted to a "SAFE" class of findings said to
be mechanical and decidable from the text alone. An independent validation took it apart:

  * `dead-index-row` deleted LIVE rows. The target was parsed with `[^)]+` and used raw, so
    `[Note](my%20file.md)`, `[Note](real.md "Tooltip")`, `[Note](<my file.md>)`, `mailto:` and
    `obsidian://` links, and any filename containing a bracket were all read as missing files.
    Eight rows in the fixture, seven of them live -- one survived. The deleted row carries the
    note's one-line description, so this is prose loss no diff brings back.
  * Blank-run collapsing was not fence-aware and rewrote the inside of code blocks.
  * Findings were computed from one read and applied to another by line index, so a file edited
    in between -- Obsidian autosaving, Dropbox syncing -- lost a line nobody had flagged.
  * CRLF files were rewritten wholesale to LF, and mode 0600 became 0644: a private memory file
    left world-readable by a tool the user ran to tidy up.
  * `--project` was joined to the vault unvalidated, so `--project ../../elsewhere` edited files
    outside the vault entirely, and a symlinked project root reached `core-rules/` and
    `global-memory/` straight past the refusals written to protect them.

Each of those was a case the author had not thought of, which is the point: the SAFE class was a
claim about the world, and the world kept producing exceptions. Ten findings on the real vault
happened to be harmless -- luck, not design. Removing the automation costs a little tidying;
keeping it risked notes that exist in one place.

The findings are still worth having, and the valuable ones were always the JUDGEMENT class
anyway: two facts that say the same thing, a memory file that has outgrown its budget. Those
needed a reader from the start.

Exit (aggregate): 0 every member clean | 1 findings | 2 usage | 3 a member could not run
Exit (--member vault): 0 nothing to report | 1 findings reported | 2 usage
Exit (--archive / --supersede): 0 done or previewed | 1 refused | 2 usage | 3 a write did not land
Exit (--demote): see gt_demote.py
"""

import argparse
import datetime
import json
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MEMORY_LINE_BUDGET = 30          # global-memory: the same budget gt_lint reports at
BLANK_RUN = 3                    # 3+ consecutive blank lines is a run worth collapsing

# GENERATED files are rewritten by a tool from a source of truth elsewhere. Editing one is
# pointless -- the next run puts it back -- and reporting one is worse than pointless, because
# it buries the findings that matter. First measurement against a real vault produced 1771
# findings, the large majority of them here (2026-09-16).
GENERATED = {"TASKS.md", "log.md", "review-queue.md", "events.jsonl", "MANIFEST.json"}

# Files whose content is LOADED INTO A SESSION, which is what "costs context" actually means.
# A duplicated sentence across two Knowledge pages is untidy; a duplicated sentence across two
# memory files is paid for on every turn of every project, forever. Only the second is this
# tool's business, and scoping to it is what makes the output short enough to act on.
CONTEXT_LOADED = (
    "global-memory/",                    # every session, every project
    "core-rules/",                       # re-asserted every turn
    "/memory/",                          # per-project session memory
    "CLAUDE.md",                         # instructions, read at load
)

# Durable prose, where a relative date silently rots. Deliberately NOT INBOX.md (a transient
# capture point where "today" is correct and about to be filed anyway).
# Where a relative date actually misleads: content injected into a session SILENTLY, months
# later, with no date in view. A research log or a decision record is opened deliberately, with
# its dates on the page, so "today" there reads correctly -- and flagging it produced 452
# findings nobody would work through, which buries the ones that matter (measured 2026-09-16).
DURABLE = ("/memory/", "global-memory/", "core-rules/", "CLAUDE.md")


def _matches(rel, needles):
    hay = "/" + rel
    return any(n in hay for n in needles)


def _is_generated(rel):
    return os.path.basename(rel) in GENERATED

# Retained as a CONFIDENCE label on the report, not as a licence to act. "mechanical" means the
# finding is decidable from the text; it does not mean acting on it is safe, which is exactly
# the inference that made --apply dangerous.
SAFE = "mechanical"
JUDGEMENT = "judgement"

# A fact worth deduplicating is a sentence, not a heading or a list bullet with one word.
MIN_FACT_CHARS = 25

# `- **Status**:` and friends: a template section label, which repeats by design.
BOILERPLATE = re.compile(r"^[-*]\s*\*\*[^*]+\*\*\s*:?\s*$")

# A line that is part of a TEMPLATE repeats by design, in every file made from that template.
# The MEMORY.md header comment was reported as a duplicate fact across 49 files -- true, and
# useless: it is the template working (measured 2026-09-16).
TEMPLATE_LINE = re.compile(r"^(<!--|\{\{|<%|:::)")


def _rel(vault, path):
    return os.path.relpath(path, vault).replace(os.sep, "/")


def _is_protected(rel):
    """-> 'core-rules' | 'global-memory' | None, by PATH SEGMENT, not substring.

    Segment-wise so a project honestly named `my-core-rules-notes` is not mistaken for the real
    thing, and `Projects/x/core-rules/` IS caught wherever it sits."""
    parts = rel.split("/")
    for i, part in enumerate(parts):
        low = part.casefold()
        if low == "core-rules":
            return "core-rules"
        if low == "global-memory" and i == 0:
            return "global-memory"
    return None


# An ALLOWLIST, not a skip list, and the direction is the point. First measurement against a
# real vault reported 187 duplicate lines inside recorded AI-response artifacts under
# packets/results/ -- and --apply would have rewritten them, destroying the record: exactly the
# mistake the Sources/ immutability rule exists to prevent. There will always be another folder
# of artifacts nobody thought to exclude, so this touches only the documents it is FOR -- the
# ones loaded into a session, plus the living project docs (2026-09-16).
OPTIMIZABLE = (
    "global-memory/", "core-rules/", "/memory/", "CLAUDE.md",
    "README.md", "research.md", "decisions.md", "design.md", "idea.md",
)


def markdown_files(vault, project=None):
    """Every .md this tool is ALLOWED to look at -- see OPTIMIZABLE."""
    skip_dirs = {".git", ".obsidian", "node_modules", "spool", "lint"}
    roots = [vault]
    if project:
        roots = [os.path.join(vault, "Projects", project)]
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in sorted(dirnames) if d not in skip_dirs]
            rel_dir = _rel(vault, dirpath)
            # Sources/ is immutable by convention; never propose changing one.
            if rel_dir.split("/")[0] == "Sources":
                continue
            for name in sorted(filenames):
                if not name.endswith(".md") or _is_generated(name):
                    continue
                full = os.path.join(dirpath, name)
                if _matches(_rel(vault, full), OPTIMIZABLE):
                    yield full


def _finding(kind, cls, rel, line, message, detail=None):
    f = {"kind": kind, "class": cls, "path": rel, "line": line, "message": message}
    if detail:
        f["detail"] = detail
    return f


def duplicate_lines_within(rel, lines):
    """The same non-trivial line twice in one file. JUDGEMENT, not safe -- this was SAFE until
    it was measured against a real vault (2026-09-16) and both examples it found were content
    that MUST repeat:

      `- **Rejected alternatives**:`  -- a required section in every ADR. Dropping the second
                                        copy silently destroys the second ADR's structure.
      `WHERE type = "project" ...`    -- a line inside two Dataview query blocks.

    A repeated line is waste only in prose; in a document built from repeated structure it IS
    the structure, and a script cannot tell which it is looking at. So it is reported with both
    line numbers and a human decides. Fenced blocks are skipped outright."""
    out, seen, fence = [], {}, False
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if line.startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        if len(line) < MIN_FACT_CHARS or line.startswith(("#", "|", "---", ">")):
            continue
        if line.endswith(":") or BOILERPLATE.match(line):
            continue                       # a section label, not a fact
        if line in seen:
            out.append(_finding("duplicate-line", JUDGEMENT, rel, n,
                                "identical to line %d; if it is prose the second copy is "
                                "waste, if it is structure it must stay -- your call"
                                % seen[line], detail={"first_line": seen[line]}))
        else:
            seen[line] = n
    return out


def blank_runs(rel, lines):
    out, run_start, run = [], None, 0
    for n, raw in enumerate(lines, 1):
        if raw.strip() == "":
            run_start = run_start or n
            run += 1
        else:
            if run >= BLANK_RUN:
                out.append(_finding("blank-run", SAFE, rel, run_start,
                                    "%d consecutive blank lines" % run,
                                    detail={"count": run}))
            run_start, run = None, 0
    if run >= BLANK_RUN:
        out.append(_finding("blank-run", SAFE, rel, run_start,
                            "%d consecutive blank lines" % run, detail={"count": run}))
    return out


INDEX_ROW = re.compile(r"^\s*[-*]\s*\[[^\]]+\]\(([^)]+)\)")
# Anything of the shape `scheme:` -- mailto:, obsidian://, x-man-page: -- none of which is a
# path on disk, and every one of which this check used to read as a missing file.
_SCHEME = re.compile(r"\A[A-Za-z][A-Za-z0-9+.-]{1,31}:")


def _link_target(raw_target):
    """-> the on-disk path a markdown link names, or None when it names no file at all.

    Markdown does not put a bare path between the parentheses. `<my file.md>` wraps a name with
    spaces, `real.md "Tooltip"` carries a title, `my%20file.md` is percent-encoded, and a scheme
    means it is not a path. Each of those was read as a missing file by the raw `[^)]+` capture:
    in the hour this check had an --apply, seven of the eight fixture rows it deleted were live
    ones of exactly these shapes, which is why there is no --apply now."""
    t = raw_target.strip()
    if t.startswith("<") and t.endswith(">"):
        t = t[1:-1].strip()
    else:
        # A title after the destination: `path.md "Title"`, 'Title', or (Title).
        m = re.match(r"\A(\S+)\s+[\"'(]", t)
        if m:
            t = m.group(1)
    t = t.split("#")[0].strip()
    if not t or _SCHEME.match(t):
        return None
    return t


def dead_index_rows(vault, path, rel, lines):
    """A MEMORY.md row whose target did not resolve on disk. REPORTED, never applied.

    "Safe: it can never load" used to stand here, and it was a claim about a parse rather than
    about the world: every shape _link_target now handles was reported as dead while resolving
    perfectly well in Obsidian (corrected 2026-09-18). Handling them removes the false positives
    anyone can name; it does not make the check decidable from the text alone, which is why this
    is report-only and stays so. A row is prose -- it carries the note's one-line description --
    so a wrong deletion here is loss no diff brings back."""
    if os.path.basename(path).upper() != "MEMORY.MD":
        return []
    out = []
    for n, raw in enumerate(lines, 1):
        m = INDEX_ROW.match(raw)
        if not m:
            continue
        target = _link_target(m.group(1))
        if not target:
            continue
        here = os.path.dirname(path)
        # Either spelling may be the real name: a file can be literally called `a%20b.md`.
        if not any(os.path.exists(os.path.join(here, c))
                   for c in {target, urllib.parse.unquote(target)}):
            out.append(_finding("dead-index-row", SAFE, rel, n,
                                "points at %r, which does not exist" % target,
                                detail={"target": target}))
    return out


def oversized_global_memory(rel, lines):
    """JUDGEMENT: what to cut is a decision about meaning, so it is only ever reported."""
    if _is_protected(rel) != "global-memory" or os.path.basename(rel).upper() == "MEMORY.MD":
        return []
    body = [ln for ln in lines if ln.strip()]
    if len(body) <= MEMORY_LINE_BUDGET:
        return []
    return [_finding("memory-bloat", JUDGEMENT, rel, 1,
                     "%d non-blank lines against a budget of %d; global-memory is read in "
                     "EVERY session of every project, so detail here is paid for constantly. "
                     "Demote it:  --demote %s --project <slug>"
                     % (len(body), MEMORY_LINE_BUDGET, rel))]


RELATIVE_DATE = re.compile(
    r"\b(yesterday|today|tomorrow|last (week|month|night|year)|next (week|month|year)|"
    r"this (morning|afternoon|week|month)|recently|a few (days|weeks) ago)\b", re.I)


# `today's data`, `today's date`, `today's roster` -- a possessive describes what code does at
# RUNTIME, not a date the writer meant, and it rots no faster than the code does. 51 of 141
# findings on a real vault were this, and a check that is a third wrong gets ignored, which is
# worse than not having it (measured 2026-09-16).
POSSESSIVE = re.compile(r"\b(today|tomorrow|yesterday)'s\b", re.I)
CODE_SPAN = re.compile(r"`[^`]*`")


def relative_dates(rel, lines):
    """JUDGEMENT: 'last week' meant something when written and means something else now, but
    only a reader knows which absolute date was intended. Durable prose only -- in INBOX.md or
    a generated rollup, "today" is correct and about to be superseded anyway."""
    if not _matches(rel, DURABLE):
        return []
    out = []
    for n, raw in enumerate(lines, 1):
        # A word inside a code span is part of an expression, not prose that dates.
        text = CODE_SPAN.sub(" ", raw)
        if POSSESSIVE.search(text):
            text = POSSESSIVE.sub(" ", text)
        m = RELATIVE_DATE.search(text)
        if m:
            out.append(_finding("relative-date", JUDGEMENT, rel, n,
                                "%r is relative; in a file read months later it silently "
                                "means something else. Replace with the absolute date."
                                % m.group(0)))
    return out


def duplicate_facts_across(vault, files_lines):
    """JUDGEMENT: the same sentence in two files. Which copy is canonical is a decision."""
    where = {}
    for rel, lines in files_lines:
        if not _matches(rel, CONTEXT_LOADED):
            continue                       # untidy elsewhere; PAID FOR here, every turn
        fence = False
        for n, raw in enumerate(lines, 1):
            line = raw.strip()
            if line.startswith("```"):
                fence = not fence
                continue
            if fence or len(line) < MIN_FACT_CHARS:
                continue
            if line.startswith(("#", "|", "---", ">")) or line.endswith(":") \
                    or BOILERPLATE.match(line) or TEMPLATE_LINE.match(line):
                continue
            where.setdefault(line, []).append((rel, n))
    out = []
    for line, hits in sorted(where.items()):
        if len({r for r, _ in hits}) < 2:
            continue
        first_rel, first_n = hits[0]
        others = ", ".join("%s:%d" % (r, n) for r, n in hits[1:][:4])
        out.append(_finding("duplicate-fact", JUDGEMENT, first_rel, first_n,
                            "the same sentence appears in %d files (%s); decide which one "
                            "owns it and link to it from the others"
                            % (len({r for r, _ in hits}), others)))
    return out


# -- 0.18.1: what a project costs to open, and two vault-wide judgement findings ------------

# What /gt:gt-open reads, in its order (skills/gt-open/SKILL.md, Step 3-4). memory/ notes are
# NOT loaded -- only their index -- which is the whole point of that skill's lazy loading.
OPEN_FILES = ("README.md", "source.md", "idea.md", "research.md", "decisions.md",
              "design.md", "spec.md", "runbook.md", "memory/MEMORY.md")
# Read once per session whichever project is opened (gt-open Step 2).
OPEN_SHARED = ("Projects/CONVENTIONS.md", "Projects/PROTOCOL.md")
# gt-open: "If it exceeds ~200 lines, do not read it whole -- read its ## headings ... plus the
# most recent few." Costed the same way, so the number is what an open actually loads.
RESEARCH_SKIM_LINES = 200
RESEARCH_RECENT = 3
CHARS_PER_TOKEN = 4             # an approximation, labelled as one wherever it is printed


def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _research_as_opened(text):
    """-> (lines, chars, skimmed) for research.md the way gt-open reads it."""
    lines = text.split("\n")
    if len(lines) <= RESEARCH_SKIM_LINES:
        return len(lines), len(text), False
    heads = [ln for ln in lines if ln.startswith("## ")]
    starts = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    recent = lines[starts[-RESEARCH_RECENT]:] if len(starts) >= RESEARCH_RECENT else lines
    seen = heads + [ln for ln in recent if not ln.startswith("## ")]
    return len(seen), sum(len(ln) + 1 for ln in seen), True


def project_dirs(vault):
    """Top-level project folders (a README.md marks one). Sub-projects are opened on request
    only, so they are not part of what opening the parent costs."""
    root = os.path.join(vault, "Projects")
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [n for n in names if os.path.isfile(os.path.join(root, n, "README.md"))]


def open_cost(vault, project=None):
    """-> what /gt:gt-open loads per project, in lines and approximate tokens."""
    rows = []
    for slug in ([project] if project else project_dirs(vault)):
        base = os.path.join(vault, "Projects", slug)
        files, lines_n, chars_n, skimmed = {}, 0, 0, False
        for rel in OPEN_FILES:
            text = _read_text(os.path.join(base, rel))
            if text is None:
                continue
            if rel == "research.md":
                n, c, skimmed = _research_as_opened(text)
            else:
                n, c = len(text.split("\n")), len(text)
            files[rel] = n
            lines_n += n
            chars_n += c
        if files:
            rows.append({"project": slug, "lines": lines_n,
                         "tokens_approx": chars_n // CHARS_PER_TOKEN, "files": files,
                         "research_skimmed": skimmed})
    shared_lines = shared_chars = 0
    for rel in OPEN_SHARED:
        text = _read_text(os.path.join(vault, rel))
        if text is not None:
            shared_lines += len(text.split("\n"))
            shared_chars += len(text)
    rows.sort(key=lambda r: -r["tokens_approx"])
    return {"projects": rows,
            "shared": {"lines": shared_lines, "tokens_approx": shared_chars // CHARS_PER_TOKEN,
                       "files": list(OPEN_SHARED)},
            "total_lines": sum(r["lines"] for r in rows),
            "total_tokens_approx": sum(r["tokens_approx"] for r in rows),
            "basis": "what /gt:gt-open reads; tokens ~ characters / %d" % CHARS_PER_TOKEN}


def render_open_cost(cost):
    out = ["\nOPEN COST -- what /gt:gt-open loads per project (tokens approximate)"]
    for r in cost["projects"][:40]:
        out.append("  %-34s %6d lines  ~%7d tokens%s" % (
            r["project"], r["lines"], r["tokens_approx"],
            "  (research.md skimmed: headings + newest %d)" % RESEARCH_RECENT
            if r["research_skimmed"] else ""))
    if len(cost["projects"]) > 40:
        out.append("  ... and %d more" % (len(cost["projects"]) - 40))
    out.append("  %-34s %6d lines  ~%7d tokens" % ("all projects", cost["total_lines"],
                                                    cost["total_tokens_approx"]))
    out.append("  %-34s %6d lines  ~%7d tokens  (once per session)" % (
        "CONVENTIONS.md + PROTOCOL.md", cost["shared"]["lines"], cost["shared"]["tokens_approx"]))
    return "\n".join(out)


def _lint():
    import gt_lint                                               # noqa: PLC0415
    return gt_lint


def single_project_globals(vault):
    """JUDGEMENT: a global-memory note whose body names exactly ONE project.

    The same evidence gt_lint's global-scope-leak reads (project slugs in a global-memory note,
    honouring lint-declines.md), sharpened to the demotion case: a note naming several projects
    may be genuinely cross-project, a note naming one is that project's memory charged to every
    session of every other project. Whole-word matching, so `alpha` is not found in `alphabet`."""
    gm = os.path.join(vault, "global-memory")
    if not os.path.isdir(gm):
        return []
    try:
        from pathlib import Path                                  # noqa: PLC0415
        lint = _lint()
        suppressed = lint.read_suppress_list(Path(vault))
        is_suppressed = lint.is_suppressed
    except Exception:                                            # noqa: BLE001
        suppressed, is_suppressed = set(), (lambda *a, **k: False)
    slugs = set()
    for dirpath, dirnames, filenames in os.walk(os.path.join(vault, "Projects")):
        dirnames[:] = [d for d in dirnames if d not in ("memory", ".git")]
        if "README.md" in filenames and os.path.basename(dirpath) != "Projects":
            slugs.add(os.path.basename(dirpath).lower())
    out = []
    for name in sorted(os.listdir(gm)):
        if not name.endswith(".md") or name.upper() == "MEMORY.MD":
            continue
        rel = "global-memory/" + name
        if is_suppressed(suppressed, rel, name):
            continue
        text = (_read_text(os.path.join(gm, name)) or "").lower()
        hits = [s for s in sorted(slugs)
                if re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(s), text)
                and not is_suppressed(suppressed, rel, name, s)]
        if len(hits) == 1:
            out.append(_finding(
                "single-project-global", JUDGEMENT, rel, 1,
                "names only project '%s', yet loads into every session of every project. "
                "Demote it:  --demote %s --to project-memory --project %s"
                % (hits[0], rel, hits[0]), detail={"project": hits[0]}))
    return out


USAGE_LOG = os.path.join("usage", "knowledge.jsonl")
UNUSED_DAYS = 90


def knowledge_reads(vault):
    """-> ({page rel: last read date}, first logged date) or (None, None) with no log."""
    path = os.path.join(vault, USAGE_LOG)
    if not os.path.isfile(path):
        return None, None
    last, first = {}, None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
                page, day = str(row["page"]), datetime.date.fromisoformat(str(row["date"]))
            except (ValueError, KeyError, TypeError):
                continue
            if page not in last or day > last[page]:
                last[page] = day
            first = day if first is None or day < first else first
    return last, first


def knowledge_unused(vault, days=UNUSED_DAYS, today=None):
    """JUDGEMENT: Knowledge pages with no read logged in `days` days, never-read first.

    Reads come from usage/knowledge.jsonl, written by the log_knowledge_read hook (PostToolUse
    on Read). With no log there is no evidence either way, so nothing is reported -- every page
    would otherwise read as "never read" on the day logging was switched on. Lists candidates for
    the owner; nothing is demoted or archived from here."""
    last, first = knowledge_reads(vault)
    if last is None:
        return []
    today = today or datetime.date.today()
    kroot = os.path.join(vault, "Knowledge")
    pages = []
    for dirpath, dirnames, filenames in os.walk(kroot):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.endswith(".md") and not name.startswith("_"):
                pages.append(_rel(vault, os.path.join(dirpath, name)))
    never, stale = [], []
    for rel in pages:
        when = last.get(rel)
        if when is None:
            never.append(_finding("knowledge-unused", JUDGEMENT, rel, 1,
                                  "never read since the access log began (%s)" % first,
                                  detail={"last_read": None}))
        elif (today - when).days > days:
            stale.append((when, _finding(
                "knowledge-unused", JUDGEMENT, rel, 1,
                "last read %s, %d days ago -- review whether it still earns its place"
                % (when, (today - when).days), detail={"last_read": str(when)})))
    return never + [f for _, f in sorted(stale, key=lambda t: t[0])]


def analyse(vault, project=None, unused_days=UNUSED_DAYS):
    findings, files_lines = [], []
    for path in markdown_files(vault, project):
        rel = _rel(vault, path)
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().split("\n")
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(_finding("unreadable", JUDGEMENT, rel, 1, str(exc)))
            continue
        files_lines.append((rel, lines))
        findings += duplicate_lines_within(rel, lines)
        findings += blank_runs(rel, lines)
        findings += dead_index_rows(vault, path, rel, lines)
        findings += oversized_global_memory(rel, lines)
        findings += relative_dates(rel, lines)
    findings += duplicate_facts_across(vault, files_lines)
    if not project:
        # Vault-wide by nature: a global note and a Knowledge page belong to no one project.
        findings += single_project_globals(vault)
        findings += knowledge_unused(vault, unused_days)
    return findings


# ------------------------------------------------------------------ archive / supersede ----

DATED = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\b[:\s-]*(.*?)\s*$")
ARCHIVE_SECTION = "Archived entries"
# The write queue refuses a request over 256 KiB; archive content goes in chunks under it.
CHUNK_BYTES = 200 * 1024


def split_blocks(text):
    """-> (preamble lines, [block]) where a block is {heading, lines}. Level-2 headings only,
    outside fenced code: a `## ` inside a fence is text, not an entry."""
    lines = text.split("\n")
    pre, blocks, fence = [], [], False
    for ln in lines:
        if re.match(r"^\s*(```|~~~)", ln):
            fence = not fence
        if not fence and ln.startswith("## "):
            blocks.append({"heading": ln, "lines": [ln]})
        elif blocks:
            blocks[-1]["lines"].append(ln)
        else:
            pre.append(ln)
    return pre, blocks


def _block_text(block):
    lines = list(block["lines"])
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _anchor(heading_line):
    # Obsidian heading links cannot carry these characters.
    return re.sub(r"[\[\]|#^]", "", heading_line[3:]).strip()


def _queue():
    import gt_write_queue                                        # noqa: PLC0415
    return gt_write_queue


def _claimed(vault, rels):
    try:
        import gt_demote                                         # noqa: PLC0415
    except Exception as exc:                                     # noqa: BLE001
        return "the session registry could not be checked (%s)" % exc.__class__.__name__
    for rel in rels:
        why = gt_demote.claim_refusal(vault, rel)
        if why:
            return why
    return None


def _submit(vault, writes):
    """-> (ok, message). Queue, drain at once, and accept only apply/deduplicate."""
    from pathlib import Path                                      # noqa: PLC0415
    wq = _queue()
    results, note = wq.submit(Path(vault), writes)
    bad = [r for r in results if r["decision"] not in ("apply", "deduplicate")]
    if bad:
        r = bad[0]
        return False, "%s %s: %s" % (r["path"], r["decision"],
                                     note or r.get("reason") or r.get("conflict") or "")
    return True, None


def plan_archive(vault, slug, before):
    rel = "Projects/%s/research.md" % slug
    text = _read_text(os.path.join(vault, rel))
    if text is None:
        return None, "no %s" % rel
    pre, blocks = split_blocks(text)
    moving, keep_at, existing_index = {}, None, None
    out_blocks = []
    for b in blocks:
        m = DATED.match(b["heading"])
        if b["heading"][3:].strip().lower() == ARCHIVE_SECTION.lower():
            existing_index = b
            keep_at = len(out_blocks) if keep_at is None else keep_at
            continue
        if m:
            try:
                day = datetime.date.fromisoformat(m.group(1))
            except ValueError:
                day = None                     # `## 2026-13-45` is not a date; never touched
            if day is not None and day < before:
                moving.setdefault(day.year, []).append((day, m.group(2), b))
                keep_at = len(out_blocks) if keep_at is None else keep_at
                continue
        out_blocks.append(b)
    return {"rel": rel, "text": text, "pre": pre, "out_blocks": out_blocks, "moving": moving,
            "keep_at": keep_at, "existing_index": existing_index}, None


def render_research(plan, index_lines):
    idx = list(plan["existing_index"]["lines"]) if plan["existing_index"] else \
        ["## " + ARCHIVE_SECTION, "",
         "Entries moved to a dated archive by `gt_optimize.py --archive`. Nothing was deleted: "
         "each line names where its entry now lives, unedited."]
    while idx and not idx[-1].strip():
        idx.pop()
    idx += [""] + index_lines if idx[-1].startswith(("Entries", "##")) else index_lines
    idx.append("")
    blocks = list(plan["out_blocks"])
    blocks.insert(plan["keep_at"] if plan["keep_at"] is not None else len(blocks),
                  {"heading": idx[0], "lines": idx})
    body = list(plan["pre"])
    for b in blocks:
        body += b["lines"]
    return "\n".join(body).rstrip("\n") + "\n"


def do_archive(vault, slug, before_s, apply):
    try:
        before = datetime.date.fromisoformat(before_s or "")
    except ValueError:
        print("--archive needs --before YYYY-MM-DD", file=sys.stderr)
        return 2
    plan, err = plan_archive(vault, slug, before)
    if err:
        print("nothing to archive: %s" % err, file=sys.stderr)
        return 1
    if not plan["moving"]:
        print("nothing to archive: no dated entry in %s is older than %s" % (plan["rel"], before))
        return 0
    targets = {y: "Projects/%s/research-archive-%d.md" % (slug, y) for y in plan["moving"]}
    why = _claimed(vault, [plan["rel"]] + sorted(targets.values()))
    if why:
        print("refusing to archive: %s" % why, file=sys.stderr)
        return 1

    index_lines, n = [], 0
    for year in sorted(plan["moving"]):
        stem = os.path.basename(targets[year])[:-3]
        for day, title, b in plan["moving"][year]:
            n += 1
            index_lines.append("- %s — %s → [[%s#%s]]" % (day, title or "(untitled)", stem,
                                                       _anchor(b["heading"])))
    new_research = render_research(plan, index_lines)

    print("%s %d entr%s dated before %s out of %s:" % (
        "archiving" if apply else "would archive (dry run, nothing written)", n,
        "y" if n == 1 else "ies", before, plan["rel"]))
    for year in sorted(plan["moving"]):
        exists = os.path.isfile(os.path.join(vault, targets[year]))
        print("  -> %s (%s): %d entr%s" % (targets[year], "append" if exists else "new file",
                                           len(plan["moving"][year]),
                                           "y" if len(plan["moving"][year]) == 1 else "ies"))
        for day, title, _b in plan["moving"][year]:
            print("       %s  %s" % (day, title))
    print("  %s keeps one index line per moved entry under `## %s` (%d -> %d lines)" % (
        plan["rel"], ARCHIVE_SECTION, len(plan["text"].split("\n")),
        len(new_research.split("\n"))))
    if not apply:
        print("Re-run with --apply to move them. Nothing is deleted either way.")
        return 0

    # 1. WRITE THE ARCHIVE, in chunks the queue accepts, and READ IT BACK.
    today = datetime.date.today().isoformat()
    for year in sorted(plan["moving"]):
        trel = targets[year]
        texts = [_block_text(b) for _d, _t, b in plan["moving"][year]]
        chunks, cur = [], []
        for t in texts:
            if cur and len("\n\n".join(cur + [t]).encode("utf-8")) > CHUNK_BYTES:
                chunks.append(cur)
                cur = []
            cur.append(t)
        chunks.append(cur)
        for i, chunk in enumerate(chunks):
            body = "\n\n".join(chunk) + "\n"
            if i == 0 and not os.path.isfile(os.path.join(vault, trel)):
                body = ("# Research archive %d — %s\n\nEntries moved out of [[research]] by "
                        "`gt_optimize.py --archive` on %s. Each entry is exactly as it stood; "
                        "research.md keeps a dated line pointing at each one.\n\n%s"
                        % (year, slug, today, body))
                op = "create"
            else:
                op = "append"
            ok, msg = _submit(vault, [{"path": trel, "op": op, "content": body,
                                       "hint": "gt-optimize --archive: research archive"}])
            if not ok:
                print("archive write did not land (%s); research.md was NOT changed" % msg,
                      file=sys.stderr)
                return 3
        landed = _read_text(os.path.join(vault, trel)) or ""
        missing = [t.split("\n")[0] for t in texts if t not in landed]
        if missing:
            print("%s does not hold %d of the entries after writing (first: %s); research.md "
                  "was NOT changed" % (trel, len(missing), missing[0]), file=sys.stderr)
            return 3

    # 2. ONLY NOW SHRINK research.md -- and only if nobody changed it while we worked.
    if _read_text(os.path.join(vault, plan["rel"])) != plan["text"]:
        print("%s changed while archiving; the archive holds copies, research.md was NOT "
              "changed. Re-run to finish." % plan["rel"], file=sys.stderr)
        return 3
    ok, msg = _submit(vault, [{"path": plan["rel"], "op": "replace-file",
                               "content": new_research,
                               "hint": "gt-optimize --archive: research.md index"}])
    if not ok:
        print("research.md was not rewritten (%s); the entries are now in BOTH places, which "
              "is visible and safe -- resolve by hand or re-run" % msg, file=sys.stderr)
        return 3
    print("done: %d entr%s archived; nothing deleted." % (n, "y" if n == 1 else "ies"))
    return 0


SUPERSEDED = re.compile(r"^>\s*superseded_by:", re.I)


def do_supersede(vault, rel, entry, by, apply):
    if not rel or not entry or not by:
        print("--supersede needs --file, --entry and --by", file=sys.stderr)
        return 2
    rel = rel.replace(os.sep, "/").lstrip("/")
    full = os.path.realpath(os.path.join(vault, rel))
    if not full.startswith(os.path.realpath(vault) + os.sep) or ".." in rel.split("/"):
        print("--file must be a path inside the vault: %r" % rel, file=sys.stderr)
        return 2
    text = _read_text(full)
    if text is None:
        print("no such file: %s" % rel, file=sys.stderr)
        return 2
    _pre, blocks = split_blocks(text)

    def matches(name):
        want = name.strip().lstrip("#").strip().lower()
        return [b for b in blocks if b["heading"][3:].strip().lower() == want]
    hit = matches(entry)
    if len(hit) != 1:
        print("--entry must name exactly one `## ` heading in %s; %d match %r"
              % (rel, len(hit), entry), file=sys.stderr)
        return 1
    if "#" in by or "]]" in by:
        link = by.strip().strip("[]")
    else:
        if len(matches(by)) != 1:
            print("--by must name exactly one `## ` heading in %s (or `file#heading`); %d "
                  "match %r" % (rel, len(matches(by)), by), file=sys.stderr)
            return 1
        link = "%s#%s" % (os.path.basename(rel)[:-3], _anchor(matches(by)[0]["heading"]))
    block = hit[0]
    body = block["lines"][1:]
    first = next((ln for ln in body if ln.strip()), "")
    if SUPERSEDED.match(first):
        print("already marked: %s" % first.strip())
        return 0
    marker = "> superseded_by: [[%s]] — marked %s by `gt_optimize.py --supersede`; this entry is " \
             "kept as it stood." % (link, datetime.date.today().isoformat())
    why = _claimed(vault, [rel])
    if why:
        print("refusing to mark: %s" % why, file=sys.stderr)
        return 1
    print("%s in %s:\n  %s\n  %s" % ("marking" if apply else "would mark (dry run)",
                                      rel, block["heading"], marker))
    if not apply:
        return 0
    old_body = "\n".join(body).strip("\n")
    content = marker + ("\n\n" + old_body if old_body else "")
    ok, msg = _submit(vault, [{"path": rel, "op": "replace-section",
                               "section": block["heading"][3:].strip(), "content": content,
                               "hint": "gt-optimize --supersede"}])
    if not ok:
        print("not marked: %s" % msg, file=sys.stderr)
        return 3
    after = _read_text(full) or ""
    if marker not in after or (old_body and old_body not in after):
        print("the mark did not land as expected in %s; check it by hand" % rel, file=sys.stderr)
        return 3
    print("done: marked, and the entry is unchanged beneath the mark.")
    return 0


# ------------------------------------------------------------------------ the aggregator ----

MEMBERS = {
    "vault": ("gt_optimize.py",
              "what the vault stores that every session pays for (report only)"),
    "session": ("gt_optimize_session.py",
                "prompt-cache writes by cause, from the Claude Code transcripts (report only)"),
}
MEMBER_ORDER = ("vault", "session")
# Members a bare run does not include; `--only` names them (0.18.1, g7).
OPT_IN_MEMBERS = {
    "execution": ("gt_optimize_exec.py",
                  "how work executes, ranked by measured cost (report only)"),
}


def vault_member(args, vault):
    """The report this tool printed before 0.18.1, plus the new finding kinds and --cost."""
    findings = analyse(vault, args.project, args.unused_days)
    safe = [f for f in findings if f["class"] == SAFE]
    judge = [f for f in findings if f["class"] == JUDGEMENT]
    cost = open_cost(vault, args.project)

    if args.json:
        last, first = knowledge_reads(vault)
        print(json.dumps({"version": 1, "vault": vault, "applied": False,
                          "findings": findings, "open_cost": cost,
                          "knowledge_log": {"present": last is not None,
                                            "since": str(first) if first else None}},
                         indent=2))
        return 1 if findings else 0

    print("vault: %s" % vault)
    print("\nMECHANICAL -- decidable from the text (%d)" % len(safe))
    for f in safe[:40]:
        print("  %-16s %s:%d  %s" % (f["kind"], f["path"], f["line"], f["message"]))
    if len(safe) > 40:
        print("  ... and %d more" % (len(safe) - 40))
    print("\nJUDGEMENT -- reported, never applied (%d)" % len(judge))
    for f in judge[:40]:
        print("  %-16s %s:%d  %s" % (f["kind"], f["path"], f["line"], f["message"]))
    if len(judge) > 40:
        print("  ... and %d more" % (len(judge) - 40))
    if args.cost:
        print(render_open_cost(cost))

    if findings:
        print("\nNothing has been changed: this tool only reports. Edit what you agree with.")
    return 1 if findings else 0


def member_cmd(name, args, vault):
    if name == "vault":
        cmd = [sys.executable, os.path.abspath(__file__), "--vault", vault, "--member", "vault",
               "--unused-days", str(args.unused_days)]
        if args.project:
            cmd += ["--project", args.project]
        if args.cost:
            cmd.append("--cost")
    elif name == "execution":
        script = os.path.join(HERE, OPT_IN_MEMBERS[name][0])
        if not os.path.isfile(script):
            return None
        cmd = [sys.executable, script, "--vault", vault]
        if args.project:
            cmd += ["--project", args.project]
        if args.repo:
            cmd += ["--repo", args.repo]
    else:
        script = os.path.join(HERE, MEMBERS[name][0])
        if not os.path.isfile(script):
            return None
        cmd = [sys.executable, script]
        if args.days:
            cmd += ["--days", str(args.days)]
    if args.json:
        cmd.append("--json")
    return cmd


def aggregate(args, vault):
    import gt_aggregate                                          # noqa: PLC0415
    declared = dict(MEMBERS, **OPT_IN_MEMBERS) if args.only is not None else MEMBERS
    wanted, err = gt_aggregate.resolve_wanted(declared, None, args.only)
    if err:
        print(err, file=sys.stderr)
        return 2
    wanted = [m for m in MEMBER_ORDER + tuple(OPT_IN_MEMBERS) if m in wanted]
    if not args.json:
        print("running %d optimize member(s): %s -- each reports only"
              % (len(wanted), ", ".join(wanted)))
    results = [gt_aggregate.run_member(n, member_cmd(n, args, vault), args.timeout,
                                       keep_raw=True) for n in wanted]
    ran = [r for r in results if r["ran"]]
    failed = [r for r in results if not r["ran"]]
    found = [r for r in ran if r["status"] == "findings"]
    code = 3 if failed else (1 if found else 0)

    if args.json:
        data = {}
        for r in ran:
            try:
                data[r["member"]] = json.loads(r.get("raw") or "")
            except ValueError:
                data[r["member"]] = None
        vault_data = data.get("vault") or {}
        print(json.dumps({
            "version": 1, "vault": vault, "applied": False,
            "headline": "%d of %d member(s) ran; %d reported findings"
                        % (len(ran), len(wanted), len(found)),
            "asked": wanted, "ran": [r["member"] for r in ran],
            "could_not_run": [{"member": r["member"], "exit": r["exit"], "detail": r["detail"]}
                              for r in failed],
            # The vault member's own keys, kept at the top level so a caller of the pre-0.18
            # report (`findings`) reads the same shape.
            "findings": vault_data.get("findings", []),
            "open_cost": vault_data.get("open_cost"),
            "members": data,
        }, indent=2))
        return code
    for r in results:
        if r["output"]:
            print("\n--- %s ---" % r["member"])
            print(r["output"])
    print()
    return gt_aggregate.summarise(results, wanted)


def main(argv=None):
    # 0.18.1 (g7): `--execution` is an alias for the execution member, handed every argument
    # unchanged so gt_optimize_exec.py's own options (`--apply ID`, `--repo`) keep working.
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--execution" in argv:
        import gt_optimize_exec                                  # noqa: PLC0415
        return gt_optimize_exec.main([a for a in argv if a != "--execution"])
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", required=True, help="the vault to analyse (never inferred)")
    ap.add_argument("--project", help="limit to one project slug")
    ap.add_argument("--only", help="comma-separated members: vault, session (default: both), "
                                   "and the opt-in execution")
    ap.add_argument("--execution", action="store_true",
                    help="alias: run the execution member (gt_optimize_exec.py) with these "
                         "arguments, including its --apply ID")
    ap.add_argument("--repo", help="execution member: also look at this repo's scripts")
    ap.add_argument("--member", choices=("vault",),
                    help="run the vault member directly, as the aggregator does")
    ap.add_argument("--cost", action="store_true",
                    help="vault member: print what /gt:gt-open loads per project")
    ap.add_argument("--unused-days", type=int, default=UNUSED_DAYS,
                    help="vault member: a Knowledge page unread this long is reported "
                         "(default %d)" % UNUSED_DAYS)
    ap.add_argument("--days", type=int, default=None,
                    help="session member: window in days (default: setting "
                         "optimize_session_days)")
    ap.add_argument("--timeout", type=int, default=300, help="per-member timeout, seconds")
    ap.add_argument("--demote", metavar="PATH",
                    help="move this note one tier cheaper (global-memory -> project memory "
                         "-> Knowledge) instead of deleting anything")
    ap.add_argument("--to", choices=("knowledge", "project-memory"),
                    help="with --demote: where to move it; default is one tier cheaper")
    ap.add_argument("--archive", action="store_true",
                    help="move research.md entries dated before --before into "
                         "research-archive-<YYYY>.md, leaving an index line for each")
    ap.add_argument("--before", metavar="YYYY-MM-DD", help="with --archive: the cutoff")
    ap.add_argument("--supersede", action="store_true",
                    help="mark --entry in --file superseded by --by, in place")
    ap.add_argument("--file", metavar="REL", help="with --supersede: the vault-relative file")
    ap.add_argument("--entry", metavar="HEADING", help="with --supersede: the superseded entry")
    ap.add_argument("--by", metavar="HEADING", help="with --supersede: the entry that replaces it")
    ap.add_argument("--apply", action="store_true",
                    help="with --demote, --archive or --supersede: actually write. "
                         "Reporting never writes.")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what --demote, --archive or --supersede would do (the default)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    actions = [a for a in ("demote", "archive", "supersede") if getattr(args, a)]
    if len(actions) > 1:
        print("--demote, --archive and --supersede are separate actions; pick one",
              file=sys.stderr)
        return 2
    if args.apply and args.dry_run:
        print("--apply and --dry-run contradict each other", file=sys.stderr)
        return 2

    if args.demote:
        # Delegated so the write-verify-remove order has ONE implementation and one set of
        # tests; a second copy of that order is a second chance to get it wrong.
        import gt_demote
        argv2 = ["--vault", args.vault, "--file", args.demote]
        if args.to:
            argv2 += ["--to", args.to]
        if args.project:
            argv2 += ["--project", args.project]
        if args.apply:
            argv2 += ["--apply"]
        return gt_demote.main(argv2)
    if args.apply and not actions:
        print("--apply only applies to --demote, --archive or --supersede: reporting never "
              "writes. See --help.", file=sys.stderr)
        return 2

    vault = os.path.abspath(os.path.expanduser(args.vault))
    if not os.path.isdir(vault):
        print("not a vault directory: %s" % vault, file=sys.stderr)
        return 2

    if args.project and (".." in args.project.split("/") or "\\" in args.project
                         or args.project in (".",) or os.path.isabs(args.project)):
        # `--project` is a SLUG, not a path. Joined unvalidated it walked out of the vault
        # entirely (validation 2026-09-16), and every protection here is expressed in terms of
        # a path relative to the vault root.
        print("--project takes a slug, not a path: %r" % args.project, file=sys.stderr)
        return 2
    if args.project:
        # The ONE shared resolver (gt_spool.resolve_project, 0.18.1): a bare sub-project slug
        # means Projects/<parent>/<slug>; unknown or ambiguous is an error, not a guess.
        tools = os.path.join(os.path.dirname(HERE), "templates", "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import gt_spool
        try:
            args.project = gt_spool.resolve_project(vault, args.project,
            allow_unregistered=True)  # an existing folder, as before; never creates
        except gt_spool.ProjectNotFound as exc:
            print("gt_optimize: %s" % exc, file=sys.stderr)
            return 2
        root = os.path.realpath(os.path.join(vault, "Projects", args.project))
        if not (root == os.path.realpath(vault)
                or root.startswith(os.path.realpath(vault) + os.sep)):
            print("project %r resolves outside the vault (%s); refusing"
                  % (args.project, root), file=sys.stderr)
            return 2

    if args.archive:
        if not args.project:
            print("--archive needs --project SLUG", file=sys.stderr)
            return 2
        return do_archive(vault, args.project, args.before, args.apply)
    if args.supersede:
        return do_supersede(vault, args.file, args.entry, args.by, args.apply)
    if args.member == "vault":
        return vault_member(args, vault)
    return aggregate(args, vault)


if __name__ == "__main__":
    sys.exit(main())
