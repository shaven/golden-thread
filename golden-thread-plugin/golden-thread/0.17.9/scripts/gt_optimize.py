"""gt_optimize.py -- find content that costs context, and move it to where it costs less.

Memory files are read into every session. A duplicated fact is paid for on every turn, in every
project, forever. This finds that waste. It never REMOVES anything -- the only thing it writes
is a demotion, which moves a note somewhere cheaper and leaves a pointer behind.

  gt_optimize.py --vault V [--project SLUG] [--json]        report
  gt_optimize.py --vault V --demote <path>                  move one note down a tier
  gt_optimize.py --vault V --demote <path> --apply          ... and actually move it

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

Exit: 0 nothing to report | 1 findings reported | 2 usage
"""

import argparse
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


def analyse(vault, project=None):
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
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", required=True, help="the vault to analyse (never inferred)")
    ap.add_argument("--project", help="limit to one project slug")
    ap.add_argument("--demote", metavar="PATH",
                    help="move this note one tier cheaper (global-memory -> project memory "
                         "-> Knowledge) instead of deleting anything")
    ap.add_argument("--to", choices=("knowledge", "project-memory"),
                    help="with --demote: where to move it; default is one tier cheaper")
    ap.add_argument("--apply", action="store_true",
                    help="with --demote: actually move it. Reporting never writes.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

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
    if args.apply:
        print("--apply only applies to --demote: reporting never writes. See --help.",
              file=sys.stderr)
        return 2

    vault = os.path.abspath(os.path.expanduser(args.vault))
    if not os.path.isdir(vault):
        print("not a vault directory: %s" % vault, file=sys.stderr)
        return 2

    if args.project and ("/" in args.project or "\\" in args.project
                         or args.project in ("..", ".")):
        # `--project` is a SLUG, not a path. Joined unvalidated it walked out of the vault
        # entirely (validation 2026-09-16), and every protection here is expressed in terms of
        # a path relative to the vault root.
        print("--project takes a slug, not a path: %r" % args.project, file=sys.stderr)
        return 2
    if args.project:
        root = os.path.realpath(os.path.join(vault, "Projects", args.project))
        if not (root == os.path.realpath(vault)
                or root.startswith(os.path.realpath(vault) + os.sep)):
            print("project %r resolves outside the vault (%s); refusing"
                  % (args.project, root), file=sys.stderr)
            return 2

    findings = analyse(vault, args.project)
    safe = [f for f in findings if f["class"] == SAFE]
    judge = [f for f in findings if f["class"] == JUDGEMENT]

    if args.json:
        print(json.dumps({"version": 1, "vault": vault, "applied": False,
                          "findings": findings}, indent=2))
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

    if findings:
        print("\nNothing has been changed: this tool only reports. Edit what you agree with.")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
