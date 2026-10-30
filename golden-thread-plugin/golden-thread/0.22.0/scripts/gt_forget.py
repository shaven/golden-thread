#!/usr/bin/env python3
"""gt_forget.py -- forget a wrong fact by RETRACTING the notes that state it; nothing is deleted (0.21.0).

    gt_forget.py search  "<the wrong statement>" --vault V [--json]
    gt_forget.py retract <path> --reason R [--section HEADING] [--by REPLACEMENT]
                         --vault V [--session ID] [--no-drain] [--dry-run] [--json]

When a fact turns out to be wrong -- a host moved, a figure was mismeasured, a decision was
reversed -- the notes that repeat it go on being read as current. `search` finds every note
that states it (Knowledge/, Projects/, global-memory/), each with its file, its section and the
line, and changes nothing. The owner chooses; `retract` then marks ONE note (or one `## `
section of it) as retracted, with the reason, the date, and -- when there is one -- the note
that now holds the right fact. Request 2026-10-03-forget-a-fact-by-retraction.

THE MARKER. A retraction is supersession with a reason (gt_supersede.py reads both):
  * a note with frontmatter gets one key, written with the queue's set-property:
        retracted: "2026-10-04 | reason: the host moved | replaced_by: Knowledge/hosts.md"
  * a note without frontmatter, or one `## ` section of a long page, gets one line -- the
    first line of the file, or the line right under the section heading -- written with
    replace-file, so every other byte of the note stays as it was:
        > retracted: 2026-10-04 | reason: the host moved | replaced_by: Knowledge/hosts.md
A retracted note stays on disk and readable. gt-open's listing leaves it out; gt-query ranks it
after current, expired and superseded notes with `[RETRACTED: <reason>]`; gt-lint reports a
`replaced_by` that names nothing.

EVERY WRITE GOES THROUGH THE WRITE QUEUE (gt_write_queue.py, Core rule 1), one request per
note, drained at once unless --no-drain. A path outside the vault, or one the queue refuses
(Sources/, the Core rules, generated files), is refused with exit 2 before anything is queued.

Out of scope, on purpose: deleting anything; guessing which other facts depend on this one;
retracting anything the owner did not choose.

Exit: 0 found / retracted (or would be, with --dry-run) | 1 nothing found, or the broker did
not apply it | 2 usage, or a path refused.
"""
import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gt_supersede  # noqa: E402

OK, NONE, USAGE = 0, 1, 2
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
WORD = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on", "at", "and", "or",
        "it", "its", "be", "by", "for", "with", "that", "this", "as", "from"}


def norm(text):
    return " ".join(WORD.findall(text.lower()))


def words(text):
    return [w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 1]


def _rel(vault, path):
    return Path(path).resolve().relative_to(vault).as_posix()


def search(vault, statement):
    """-> [{path, section, subsection, line, text, how}]: each note stating `statement`.

    A line states it when the normalised statement occurs in the normalised line ("exact"), or
    when every significant word of it occurs in one paragraph ("words"). The section is the
    nearest `## ` heading above -- the unit `retract --section` marks."""
    want, keys = norm(statement), set(words(statement))
    if not want:
        return []
    hits = []
    for rel in gt_supersede.notes(vault):
        try:
            lines = (vault / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        section = sub = None
        fenced = False
        para_start, para = 0, []
        seen_paras = set()

        def flush(start, para_lines, section, sub):
            if not para_lines or not keys:
                return
            text = " ".join(para_lines)
            if want in norm(text) or start in seen_paras:
                return
            if keys <= set(words(text)):
                seen_paras.add(start)
                hits.append({"path": rel, "section": section, "subsection": sub,
                             "line": start + 1, "text": para_lines[0].strip()[:200],
                             "how": "words"})
        for i, line in enumerate(lines):
            if FENCE.match(line):
                fenced = not fenced
            m = HEADING.match(line) if not fenced else None
            if m:
                flush(para_start, para, section, sub)
                para, para_start = [], i + 1
                if len(m.group(1)) == 2:
                    section, sub = m.group(2), None
                elif len(m.group(1)) > 2:
                    sub = m.group(2)
                continue
            if not line.strip():
                flush(para_start, para, section, sub)
                para, para_start = [], i + 1
                continue
            if not para:
                para_start = i
            para.append(line)
            if want in norm(line):
                seen_paras.add(para_start)
                hits.append({"path": rel, "section": section, "subsection": sub,
                             "line": i + 1, "text": line.strip()[:200], "how": "exact"})
        flush(para_start, para, section, sub)
    return hits


def marker_value(date, reason, by):
    parts = [date, "reason: " + reason.replace("|", "/").replace('"', "'").strip()]
    if by:
        parts.append("replaced_by: " + by)
    return " | ".join(parts)


def plan_retraction(vault, rel, reason, section=None, by=None, today=None):
    """-> (write dict for gt_write_queue.submit, None) or (None, why). Reads; writes nothing."""
    text = (vault / rel).read_text(encoding="utf-8")
    date = (today or datetime.date.today()).isoformat()
    value = marker_value(date, reason, by)
    if section is None and text.startswith("---"):
        has_fm, cur = __import__("gt_write_queue").frontmatter_get(text, "retracted")
        if has_fm:
            if cur:
                return None, "%s is already retracted (%s)" % (rel, cur.strip('"'))
            return {"path": rel, "op": "set-property", "key": "retracted",
                    "content": '"%s"' % value,
                    "hint": "gt-forget: retract %s" % rel}, None
    lines = text.split("\n")
    marker = "> retracted: " + value
    if section is None:
        if lines and lines[0].startswith("> retracted:"):
            return None, "%s is already retracted" % rel
        new = marker + "\n\n" + text
    else:
        want = section.strip().lower()
        idx, fenced = None, False
        for i, line in enumerate(lines):
            if FENCE.match(line):
                fenced = not fenced
            m = HEADING.match(line) if not fenced else None
            if m and len(m.group(1)) == 2 and m.group(2).strip().lower() == want:
                idx = i
                break
        if idx is None:
            return None, "%s has no `## %s` section" % (rel, section)
        if idx + 1 < len(lines) and lines[idx + 1].startswith("> retracted:"):
            return None, "`## %s` in %s is already retracted" % (section, rel)
        new = "\n".join(lines[:idx + 1] + [marker] + lines[idx + 1:])
    return {"path": rel, "op": "replace-file", "content": new,
            "hint": "gt-forget: retract %s%s" % (rel, " ## " + section if section else "")}, None


def inside_vault(vault, path):
    """-> the vault-relative "/" path of `path`, or None when it is not inside the vault."""
    p = Path(path)
    if not p.is_absolute():
        p = vault / p
    try:
        real = p.resolve()
        real.relative_to(vault)
    except (ValueError, OSError):
        return None
    return real.relative_to(vault).as_posix()


def cmd_search(a, vault):
    hits = search(vault, a.statement)
    if a.json:
        print(json.dumps({"statement": a.statement, "matches": hits}, indent=2))
    else:
        if not hits:
            print("no note states it")
        for n, h in enumerate(hits, 1):
            where = "## %s" % h["section"] if h["section"] else "(top of the note)"
            if h["subsection"]:
                where += " > %s" % h["subsection"]
            print("%2d. %s:%d  %s\n      %s" % (n, h["path"], h["line"], where, h["text"]))
        if hits:
            print("\nNothing was changed. Retract the ones you choose with "
                  "gt_forget.py retract <path> [--section H] --reason R [--by PATH].")
    return OK if hits else NONE


def cmd_retract(a, vault):
    rel = inside_vault(vault, a.path)
    if rel is None:
        print("refused: %s is outside the vault %s" % (a.path, vault), file=sys.stderr)
        return USAGE
    import gt_write_queue as wq
    why = wq.path_refusal(vault, rel)
    if why:
        print("refused: %s -- %s" % (rel, why), file=sys.stderr)
        return USAGE
    if not (vault / rel).is_file():
        print("refused: %s does not exist" % rel, file=sys.stderr)
        return USAGE
    if not a.reason.strip():
        print("refused: a retraction needs a reason (--reason)", file=sys.stderr)
        return USAGE
    by = None
    if a.by:
        by = inside_vault(vault, a.by)
        if by is None:
            print("refused: the replacement %s is outside the vault" % a.by, file=sys.stderr)
            return USAGE
    write, why = plan_retraction(vault, rel, a.reason, a.section, by)
    if write is None:
        print(why)
        return OK
    if a.dry_run:
        print(json.dumps({"would_queue": write}, indent=2) if a.json
              else "would queue %s on %s (%s)" % (write["op"], rel, write.get("hint")))
        return OK
    results, note = wq.submit(vault, [write], session=a.session, drain=not a.no_drain)
    r = results[0]
    if a.json:
        print(json.dumps({"result": r, "note": note}, indent=2))
    else:
        print("%s %s: %s%s" % (r["decision"], rel, write["op"],
                                (" -- " + (note or r.get("reason") or "")) if r["decision"]
                                not in ("apply", "deduplicate") else ""))
    if r["decision"] == "refused":
        return USAGE
    return OK if r["decision"] in ("apply", "deduplicate", "queued") else NONE


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_forget.py", description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", required=True, help="the vault (Core rule 2: always named)")
    common.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("search", parents=[common])
    p.add_argument("statement")
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser("retract", parents=[common])
    p.add_argument("path")
    p.add_argument("--reason", required=True)
    p.add_argument("--section", help="retract one `## ` section instead of the whole note")
    p.add_argument("--by", help="the note that now holds the right fact")
    p.add_argument("--session")
    p.add_argument("--no-drain", action="store_true", help="queue only; the next drain applies it")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_retract)
    a = ap.parse_args(argv)
    return a.fn(a, Path(a.vault).expanduser().resolve())


if __name__ == "__main__":
    sys.exit(main())
