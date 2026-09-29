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
core-rules/ is never moved. Sources/ is never moved. A file another live session has claimed
is never moved (Core rule 1), and a session file that cannot be READ refuses too -- "could not
check" is not "clear".

That last refusal was WRITTEN HERE BEFORE IT EXISTED. From 0.16.1 until 0.16.2 this paragraph
promised it and no code performed it: there was no claim check anywhere in this file, and no
test for one. A claim outliving its implementation, in the docstring of the tool built to stop
exactly that -- found by a documentation sweep on 2026-09-17, not by anything mechanical. The
implementation and its tests now exist; the lesson is that prose in this file is not evidence
about this file, which is the whole argument for gt_validation.py.

Exit: 0 done (or a dry run that would proceed) | 1 refused, and why -- NOTHING was written |
2 usage | 3 an error MID-MOVE: one of the two ends may now hold something, and the message says
which, so 3 is the only code that asks a person to go and look
"""
import argparse
import datetime
import hashlib
import os
import re
import shutil
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Where a note may go, and what it costs there. Ordered most expensive first.
TIERS = ("global-memory", "project-memory", "knowledge")

NEVER_MOVE = ("core-rules", "sources")          # compared casefolded + NFC, never literally
SLUG_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")


def _inside(path, root):
    """True when path REALLY resolves inside root. Lexical containment is not containment:
    a symlinked source let the tool read and then overwrite a file outside the vault
    entirely (validation 2026-09-16)."""
    rp, rr = os.path.realpath(path), os.path.realpath(root)
    return rp == rr or rp.startswith(rr.rstrip(os.sep) + os.sep)


def _has_symlink(path, stop):
    """Any symlink on the path between `stop` and `path`. A symlinked PARENT is as good as a
    symlinked file for escaping, and a dangling destination symlink defeated the collision
    check while the write followed it out of the vault."""
    cur = os.path.abspath(path)
    stop = os.path.abspath(stop)
    while cur.startswith(stop) and cur != stop:
        if os.path.islink(cur):
            return cur
        cur = os.path.dirname(cur)
    return None


def _norm_seg(seg):
    return unicodedata.normalize("NFC", seg).casefold()


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


def refuse_reason(rel, vault=None):
    parts = rel.split("/")
    # Compared on the RESOLVED path, casefolded and NFC-normalised. Comparing the caller's
    # literal spelling meant `sources/` and `CORE-RULES/` walked straight past on a
    # case-insensitive volume, and a real Core rule was moved (validation 2026-09-16).
    if vault:
        real = os.path.realpath(os.path.join(vault, rel))
        try:
            parts = os.path.relpath(real, os.path.realpath(vault)).split(os.sep)
        except ValueError:
            return "this path does not resolve inside the vault"
    for seg in parts:
        if _norm_seg(seg) in NEVER_MOVE:
            return ("%s/ is never moved by a script: a Core rule is re-asserted into every turn "
                    "of every session, and a Source is immutable by convention" % seg)
    if classify(rel) is None:
        return ("this tool only moves notes between global-memory/, a project's memory/ and "
                "Knowledge/ -- %s is in none of those" % rel)
    return None


SESSIONS_DIR = "Projects/golden-thread/sessions"
CLAIM_STALE_MINUTES = 30        # matches gt_session.py's own --stale-after default


def _claim_rows(vault):
    """-> (claims, unreadable). claims is [(session_id, status, last_execution, [rel, ...])].

    Parsed here rather than by importing gt_session.py, because that tool lives in the VAULT
    (Projects/golden-thread/tools/) while this one ships with the plugin: a vault on an older
    release may not have it, and a demote must not depend on the thing it is protecting.
    """
    d = os.path.join(vault, SESSIONS_DIR)
    claims, unreadable = [], []
    if not os.path.isdir(d):
        return claims, unreadable
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md"):
            continue
        p = os.path.join(d, name)
        try:
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append("%s (%s)" % (name, exc.__class__.__name__))
            continue
        sid = status = last = None
        for line in text.split("\n"):
            m = re.match(r"^(session_id|status|last_execution):\s*(.+?)\s*$", line)
            if m:
                key, val = m.group(1), m.group(2)
                sid, status, last = (val if key == "session_id" else sid,
                                     val if key == "status" else status,
                                     val if key == "last_execution" else last)
        files = re.findall(r"^-\s+`([^`]+)`\s*$", text, re.M)
        claims.append((sid or name, status, last, [f.replace(os.sep, "/").lstrip("/")
                                                   for f in files]))
    return claims, unreadable


def _is_live(status, last, stale_after=CLAIM_STALE_MINUTES):
    """A claim only blocks while its session is plausibly still running."""
    if status and status.strip().lower() != "active":
        return False
    if not last:
        return True                 # no heartbeat recorded: treat as live, not as expired
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            when = datetime.datetime.strptime(last.strip(), fmt)
        except ValueError:
            continue
        now = datetime.datetime.now(when.tzinfo) if when.tzinfo else datetime.datetime.now()
        return (now - when) <= datetime.timedelta(minutes=stale_after)
    return True                     # unparseable timestamp: assume live rather than assume gone


def claim_refusal(vault, rel, me=None):
    """-> a reason string if another live session has claimed this note, else None.

    Core rule 1 says never write a file another live session has claimed, and until 0.16.2 this
    file's own docstring promised that refusal while no code implemented it -- a claim outliving
    its implementation, in the tool written to stop exactly that.

    AN UNREADABLE SESSION FILE REFUSES. "Could not check" is not "clear": the whole value of the
    registry is that it is complete, and skipping the row nobody could parse is how a guard
    reports safe for the case it cannot see.
    """
    me = me if me is not None else os.environ.get("CLAUDE_SESSION_ID", "")
    claims, unreadable = _claim_rows(vault)
    if unreadable:
        return ("%d session file(s) could not be read, so whether another session has this note "
                "open is UNKNOWN -- and unknown is not clear: %s" % (len(unreadable),
                                                                     ", ".join(unreadable)))
    holders = [sid for sid, status, last, files in claims
               if rel in files and sid != me and _is_live(status, last)]
    if holders:
        return ("%s is claimed by another live session (%s). Core rule 1: never write a file "
                "another live session has claimed -- moving it would be a write to both ends"
                % (rel, ", ".join(sorted(set(holders)))))
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
        if not SLUG_RE.match(project):
            # `--project ../../../ESCAPED` and an absolute path both wrote outside the vault
            # and left a pointer to a page that did not exist (validation 2026-09-16).
            return None, ("--project takes a slug, not a path: %r" % project)
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
    """MEMORY.md rows that point at this file.

    They are REPORTED, never rewritten. Nothing here retargets an index row, and it does not
    need to: the source path keeps existing, holding a pointer to where the note went, so a row
    naming it still resolves. The old wording here ("they must be retargeted, or they go dead")
    was the half of a 2026-09-16 fix that never got corrected -- it contradicted the preview
    printed twenty lines below, which says the rows "will still point at the pointer"
    (found 2026-09-18). Tidying them is worth doing; it is a person's edit, not this tool's."""
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

    why = refuse_reason(rel, vault)
    if why:
        print("REFUSING: %s" % why)
        return 1

    # Checked BEFORE --apply is considered, so a dry run reports the claim too: finding out at
    # apply time that someone else has the note open is finding out one step too late.
    why = claim_refusal(vault, rel)
    if why:
        print("REFUSING: %s" % why)
        return 1

    link = _has_symlink(src, vault)
    if link or os.path.islink(src):
        print("REFUSING: %s is or sits under a symlink (%s). Following it would read and then "
              "overwrite a file this tool was never pointed at."
              % (rel, os.path.relpath(link or src, vault)))
        return 1
    if not _inside(src, vault):
        print("REFUSING: %s resolves outside the vault" % rel)
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
    if not _inside(os.path.dirname(dst) or vault, vault):
        print("REFUSING: %s resolves outside the vault" % rel_dst)
        return 1
    if os.path.exists(dst) or os.path.islink(dst):
        print("REFUSING: %s already exists; merging two notes is a judgement a person makes"
              % rel_dst)
        return 1

    try:
        # newline="" so CRLF survives: the destination used to be rewritten wholesale to LF,
        # which is byte-level corruption of someone's note.
        with open(src, encoding="utf-8", newline="") as fh:
            body = fh.read()
    except UnicodeDecodeError as exc:
        # 1, not 3. This is a REFUSAL -- nothing has been written, nothing has been moved, and
        # the note is exactly where it was. Exit 3 means "an error mid-move", i.e. the source
        # and destination may both hold something now, which is the one outcome that needs a
        # person to look. Spending it on a refusal told every caller to go and inspect a vault
        # this run had not touched (found 2026-09-18).
        print("REFUSING: %s is not valid UTF-8 (%s); this tool will not rewrite bytes it "
              "cannot read" % (rel, exc.reason), file=sys.stderr)
        return 1
    body_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    when = datetime.date.today().isoformat()
    idx, rows = index_rows_for(vault, rel)

    print("move   %s" % rel)
    print("   ->  %s   (%s -> %s)" % (rel_dst, tier, to))
    if rows:
        # Says what is TRUE: nothing here rewrites the index. The preview used to promise a
        # retarget that never happened (validation 2026-09-16).
        print("index  %s: %d row(s) name this file and will still point at the pointer"
              % (_rel(vault, idx), len(rows)))
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
        # O_EXCL|O_NOFOLLOW is what actually prevents the two failures a separate exists()
        # check could not: a second run racing to the same destination (both passed the
        # check, one note ended up existing nowhere) and a dangling symlink that the check
        # called absent while the write followed it out of the vault.
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(want)
            fh.flush()
            os.fsync(fh.fileno())
        shutil.copymode(src, dst)          # a private note must not become world-readable
    except FileExistsError:
        print("ERROR: %s was created by something else while this ran -- nothing was removed"
              % rel_dst, file=sys.stderr)
        return 3
    except OSError as exc:
        print("ERROR writing %s: %s -- nothing was removed" % (rel_dst, exc), file=sys.stderr)
        return 3

    # 2. VERIFY it, by reading back what is actually on disk.
    try:
        # newline="" here TOO. The two move-path reads got it and this verify did not, so
        # universal newlines collapsed the CRLF that had just been written, the digest
        # comparison failed, and a CRLF note could not be demoted at all (found 2026-09-16 by
        # a test that was left red rather than weakened).
        with open(dst, encoding="utf-8", newline="") as fh:
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

    # 3. RE-CHECK BOTH SIDES, immediately before the source is touched. Verifying once after
    #    the write is not enough: between that verify and this removal another run overwrote
    #    the destination, and the note this run was moving ended up existing nowhere
    #    (validation 2026-09-16). The invariant is not "it was there", it is "it is there NOW".
    try:
        with open(dst, encoding="utf-8", newline="") as fh:
            still = fh.read()
    except OSError as exc:
        print("ERROR re-reading %s before removal: %s -- nothing was removed" % (rel_dst, exc),
              file=sys.stderr)
        return 3
    if body.strip() and body.strip() not in still:
        print("ERROR: %s no longer holds this note's text -- something else wrote it while "
              "this ran. Nothing was removed; the note is still at %s."
              % (rel_dst, rel), file=sys.stderr)
        return 3
    try:
        with open(src, encoding="utf-8", newline="") as fh:
            current = fh.read()
    except OSError as exc:
        print("ERROR re-reading %s: %s -- nothing was removed" % (rel, exc), file=sys.stderr)
        return 3
    if hashlib.sha256(current.encode("utf-8")).hexdigest() != body_digest:
        # The source changed after it was read, so the destination holds a stale copy and the
        # newer text would be destroyed by the pointer. Leave the duplicate.
        print("ERROR: %s changed while this ran, so %s holds an older version of it. Nothing "
              "was removed -- resolve the two by hand." % (rel, rel_dst), file=sys.stderr)
        return 3

    # 4. ONLY NOW replace the source with a pointer.
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
