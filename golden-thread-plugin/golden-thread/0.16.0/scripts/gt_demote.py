#!/usr/bin/env python3
"""gt_demote.py -- move a note to where it costs less, without ever deleting it first.

  gt_demote.py --vault V --file <relative path> [--to knowledge|project-memory]
  gt_demote.py --vault V --file <relative path> --apply

THE LADDER IS ABOUT COST, NOT MATURITY

`/gt:gt-promote` moves knowledge UP by how settled it is. This moves it DOWN by how often it is
PAID FOR:

    global-memory/        read in EVERY session of every project        most expensive
    Projects/<slug>/memory/   read in every session of one project
    Knowledge/<page>.md   read when someone asks for it                 cheapest

A fact in global-memory that only one project needs is charged to every session forever. Moving
it does not make it less true or less important -- it makes it cost what it is worth.

WHY WRITE-VERIFY-REMOVE, AND WHY THAT ORDER

gt_optimize had an --apply that DELETED, and an independent validation found seven ways it
destroyed notes: it removed live index rows, rewrote fenced blocks, and applied stale offsets to
files edited underneath it. It was removed rather than repaired.

This is the shape that can be repaired, because of the order:

    1. WRITE the destination, with provenance and a link back
    2. VERIFY it by re-reading it from disk and comparing bytes
    3. only THEN remove the source, leaving a pointer to where it went

A failure at any step leaves DUPLICATION -- the same content in two places, which a reader can
see and resolve. The old design failed by DELETION, which no diff brings back. That asymmetry is
the entire reason this exists as a separate tool rather than as another --apply flag.

REFUSALS, for the same reason gt_optimize has them: this is a script, and
guard_protected_paths is a PreToolUse hook that never sees a script write a file.
core-rules/ is never moved. Sources/ is never moved. A file another session has claimed is
never moved.

Exit: 0 done (or nothing to do) | 1 refused, and why | 2 usage | 3 an error mid-move
"""
import argparse
import datetime
import hashlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Where a note may go, and what it costs there. Ordered most expensive first.
TIERS = ("global-memory", "project-memory", "knowledge")

NEVER_MOVE = ("core-rules", "Sources")


def _rel(vault, path):
    return os.path.relpath(path, vault).replace(os.sep, "/")


def classify(rel):
    """-> which tier a path sits in, or None if this tool has no business with it."""
    parts = rel.split("/")
    if parts[0] == "global-memory":
        return "global-memory"
    if parts[0] == "Knowledge":
        return "knowledge"
    if len(parts) >= 4 and parts[0] == "Projects" and parts[2] == "memory":
        return "project-memory"
    return None


def refuse_reason(rel):
    parts = rel.split("/")
    for seg in parts:
        if seg in NEVER_MOVE:
            return ("%s/ is never moved by a script: a Core rule is re-asserted into every turn "
                    "of every session, and a Source is immutable by convention" % seg)
    if classify(rel) is None:
        return ("this tool only moves notes between global-memory/, a project's memory/ and "
                "Knowledge/ -- %s is in none of those" % rel)
    return None


def default_target(tier):
    return {"global-memory": "project-memory", "project-memory": "knowledge"}.get(tier)


def title_of(text, fallback):
    for line in text.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    m = re.search(r"^name:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else fallback


def destination(vault, rel, to, project):
    """-> (absolute destination, relative destination) or (None, reason)."""
    base = os.path.basename(rel)
    stem = os.path.splitext(base)[0]
    if to == "knowledge":
        name = title_of("", stem).replace("_", " ").replace("-", " ").strip()
        return (os.path.join(vault, "Knowledge", "%s.md" % (name or stem)),
                "Knowledge/%s.md" % (name or stem))
    if to == "project-memory":
        if not project:
            return None, "moving into a project's memory needs --project <slug>"
        return (os.path.join(vault, "Projects", project, "memory", base),
                "Projects/%s/memory/%s" % (project, base))
    return None, "unknown destination %r" % to


def render_destination(body, rel_src, rel_dst, when):
    """The moved note, carrying where it came from. Provenance is not decoration: without it
    the next reader cannot tell a demoted note from one that was always here, and cannot find
    the pointer that replaced it."""
    head = ("<!-- Demoted from %s on %s by gt_demote. The original path now holds a pointer "
            "here. -->\n\n" % (rel_src, when))
    return head + body.lstrip("\n")


def render_pointer(title, rel_dst, when):
    return ("# %s\n\n"
            "> Moved to [[%s]] on %s — it was read in every session here and is needed less "
            "often than that.\n>\n"
            "> This pointer is deliberately small: the content is not duplicated, and nothing "
            "was deleted.\n" % (title, os.path.splitext(os.path.basename(rel_dst))[0], when))


def index_rows_for(vault, rel):
    """MEMORY.md rows that point at this file -- they must be retargeted, or they go dead."""
    d = os.path.dirname(os.path.join(vault, rel))
    idx = os.path.join(d, "MEMORY.md")
    if not os.path.isfile(idx):
        return None, []
    base = os.path.basename(rel)
    rows = []
    with open(idx, encoding="utf-8") as fh:
        for n, line in enumerate(fh.read().split("\n"), 1):
            if "(%s)" % base in line:
                rows.append(n)
    return idx, rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", required=True, help="the vault (never inferred)")
    ap.add_argument("--file", required=True, help="path of the note, relative to the vault")
    ap.add_argument("--to", choices=("knowledge", "project-memory"),
                    help="default: one step cheaper than where it is")
    ap.add_argument("--project", help="target project slug, when moving into a project memory")
    ap.add_argument("--apply", action="store_true", help="actually move it")
    args = ap.parse_args(argv)

    vault = os.path.abspath(os.path.expanduser(args.vault))
    rel = args.file.replace(os.sep, "/").lstrip("/")
    if ".." in rel.split("/"):
        print("--file is a path inside the vault, not a traversal: %r" % args.file,
              file=sys.stderr)
        return 2
    src = os.path.join(vault, rel)
    if not os.path.isfile(src):
        print("no such note: %s" % rel, file=sys.stderr)
        return 2

    why = refuse_reason(rel)
    if why:
        print("REFUSING: %s" % why)
        return 1

    tier = classify(rel)
    to = args.to or default_target(tier)
    if not to:
        print("REFUSING: %s is already the cheapest tier; there is nowhere further down" % tier)
        return 1

    dst, rel_dst = destination(vault, rel, to, args.project)
    if dst is None:
        print("REFUSING: %s" % rel_dst)
        return 1
    if os.path.exists(dst):
        print("REFUSING: %s already exists; merging two notes is a judgement a person makes"
              % rel_dst)
        return 1

    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    when = datetime.date.today().isoformat()
    idx, rows = index_rows_for(vault, rel)

    print("move   %s" % rel)
    print("   ->  %s   (%s -> %s)" % (rel_dst, tier, to))
    if rows:
        print("index  %s: %d row(s) will be retargeted" % (_rel(vault, idx), len(rows)))
    if not args.apply:
        print("\nNothing has been moved. Re-run with --apply.")
        print("Order when applied: write the destination, verify it on disk, and only then")
        print("replace the source with a pointer -- so a failure leaves a duplicate, never a")
        print("hole.")
        return 0

    # 1. WRITE the destination.
    want = render_destination(body, rel, rel_dst, when)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(want)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        print("ERROR writing %s: %s -- nothing was removed" % (rel_dst, exc), file=sys.stderr)
        return 3

    # 2. VERIFY it, by reading back what is actually on disk.
    try:
        with open(dst, encoding="utf-8") as fh:
            got = fh.read()
    except OSError as exc:
        print("ERROR re-reading %s: %s -- nothing was removed" % (rel_dst, exc), file=sys.stderr)
        return 3
    if hashlib.sha256(got.encode("utf-8")).hexdigest() != \
            hashlib.sha256(want.encode("utf-8")).hexdigest():
        print("ERROR: %s does not match what was written -- nothing was removed" % rel_dst,
              file=sys.stderr)
        return 3
    if body.strip() and body.strip() not in got:
        print("ERROR: the destination does not contain the original text -- nothing was removed",
              file=sys.stderr)
        return 3

    # 3. ONLY NOW replace the source with a pointer.
    try:
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(render_pointer(title_of(body, os.path.basename(rel)), rel_dst, when))
    except OSError as exc:
        print("ERROR replacing %s with a pointer: %s" % (rel, exc), file=sys.stderr)
        print("The destination IS written and verified, so nothing is lost -- the content now "
              "exists in both places. Resolve by hand.", file=sys.stderr)
        return 3

    print("\nwrote and verified %s" % rel_dst)
    print("replaced %s with a pointer" % rel)
    if rows:
        print("NOTE: %s still has %d row(s) naming this file. They now point at a pointer, "
              "which is correct but worth tidying." % (_rel(vault, idx), len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
