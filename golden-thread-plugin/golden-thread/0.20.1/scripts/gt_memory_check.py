#!/usr/bin/env python3
"""gt_memory_check.py -- find memory notes that contradict each other, and say so. Never edits.

    gt_memory_check.py --vault V --project SLUG [--changed FILE ...] [--work] [--json] [--dry-run]

A project's memory/ folder accumulates claims, and two of them can assert opposite states --
"token rotation every 24h" in August, "token rotation: disabled" in September -- with nothing
flagging the disagreement. The next session reads both with equal weight. /gt:gt-work runs this
after it writes memory notes, so the pair is put in front of a person while the context that
could resolve it is still loaded.

SCOPE IS NARROW ON PURPOSE. Only the notes this session wrote or changed, plus their close
neighbours, are compared -- never the whole vault on every write:

  changed      --changed FILE ... (vault-relative or relative to the project's memory/); with
               none given, memory notes git reports as modified or untracked, else notes
               modified in the last 24 hours
  neighbours   other notes in the same memory/ whose file-name or `name:` words overlap the
               changed note's (two shared words, or half of the shorter name's)

WHAT COUNTS AS A CONTRADICTION -- keyword rules, no model, no embeddings:

  polarity     two sentences about the same subject (two or more shared content words, and
               at least 40% of the smaller sentence's) where one is negative and the other is
               not: enabled/disabled, required/optional, added/removed, active/deprecated,
               on/off, allowed/forbidden, supported/unsupported, a bare `not`/`no`/`never`
  value        two `key: value` lines (plain or **bold** key) with the same key and different
               values -- `token rotation: 24h` against `token rotation: disabled`
  unlinked     a contradicting pair where neither note links to the other: the newer one may
               be the update, and nothing tells a reader the older one was superseded

Each pair is printed with both paths, the conflicting sentences and a question. No file is
modified, under any flag. When nothing is found it prints nothing and exits 0, so the pass is
silent in gt-work unless it has something to say.

`--work` means "called by /gt:gt-work": the `memory_contradiction_check` setting is honoured
(off -> exit 0, silent). A person running it by hand gets the check regardless.

Exit: 0 nothing found (or switched off) | 1 contradictions reported | 2 usage
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

NEGATIVE = {"disabled", "disable", "removed", "remove", "deprecated", "off", "false", "no",
            "not", "never", "optional", "forbidden", "blocked", "unsupported", "dropped",
            "retired", "inactive", "unused", "denied", "none", "without", "cannot", "can't",
            "isn't", "doesn't", "don't", "won't", "unrequired", "deleted", "stopped"}
POSITIVE = {"enabled", "enable", "added", "add", "active", "on", "true", "yes", "required",
            "requires", "allowed", "supported", "kept", "used", "always", "must", "mandatory",
            "running", "started"}
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "was", "were", "be",
        "for", "on", "at", "by", "with", "it", "its", "this", "that", "as", "from", "but",
        "has", "have", "had", "now", "then", "so", "we", "i", "you", "they", "every", "each",
        "md", "into", "than", "also", "only", "all", "any", "after", "before", "when"}
UPDATE_WORDS = re.compile(r"\b(changed|now|updated|instead|no longer|replaced|moved|switched)\b",
                          re.I)
KEY_VALUE = re.compile(r"^\s*[-*]?\s*(?:\*\*)?([A-Za-z][\w /.-]{1,60}?)(?:\*\*)?\s*:\s*(.+?)\s*$")
WORD = re.compile(r"[a-z0-9][a-z0-9'_-]*")


def words(text):
    return WORD.findall(text.lower())


def content_words(text):
    return {w for w in words(text) if w not in STOP and w not in NEGATIVE
            and w not in POSITIVE and len(w) > 2}


def polarity(text):
    ws = set(words(text))
    if ws & NEGATIVE:
        return "negative"
    return "positive"


def strip_frontmatter(text):
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[:end], text[end + 4:]
    return "", text


def name_words(path, text):
    fm, _ = strip_frontmatter(text)
    m = re.search(r"^name:\s*(.+)$", fm, re.M)
    base = os.path.splitext(os.path.basename(path))[0]
    return content_words(base.replace("-", " ").replace("_", " ")
                         + " " + (m.group(1) if m else ""))


def sentences(text):
    _, body = strip_frontmatter(text)
    out, fence = [], False
    for line in body.split("\n"):
        if line.strip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence or not line.strip() or line.lstrip().startswith(("#", "|", "<!--")):
            continue
        for s in re.split(r"(?<=[.!?])\s+", line.strip()):
            s = s.strip(" -*")
            if len(s) >= 8:
                out.append(s)
    return out


def key_values(text):
    out = {}
    _, body = strip_frontmatter(text)
    for line in body.split("\n"):
        m = KEY_VALUE.match(line)
        if m and not line.strip().startswith(("http", "#")):
            key = " ".join(words(m.group(1)))
            if key and len(key) >= 3:
                out.setdefault(key, (line.strip(), m.group(2).strip().lower()))
    return out


def links_to(text, other_path):
    stem = os.path.splitext(os.path.basename(other_path))[0]
    return ("[[%s" % stem) in text or ("](%s)" % os.path.basename(other_path)) in text \
        or ("](%s" % os.path.basename(other_path)) in text


def neighbours_of(name_ws, candidates):
    out = []
    for path, ws in candidates:
        shared = name_ws & ws
        smaller = min(len(name_ws), len(ws)) or 1
        if len(shared) >= 2 or (shared and len(shared) / smaller >= 0.5):
            out.append(path)
    return out


def compare(a_path, a_text, b_path, b_text):
    """-> list of conflicts between two notes: {kind, a, b} sentence pairs."""
    found = []
    akv, bkv = key_values(a_text), key_values(b_text)
    for key in sorted(set(akv) & set(bkv)):
        (aline, aval), (bline, bval) = akv[key], bkv[key]
        if aval != bval:
            found.append({"kind": "value", "a": aline, "b": bline, "subject": key})
    seen = {(c["a"], c["b"]) for c in found}
    for sa in sentences(a_text):
        ca = content_words(sa)
        if len(ca) < 2:
            continue
        for sb in sentences(b_text):
            if (sa, sb) in seen:
                continue
            cb = content_words(sb)
            shared = ca & cb
            if len(shared) < 2 or len(shared) / (min(len(ca), len(cb)) or 1) < 0.4:
                continue
            if polarity(sa) != polarity(sb):
                found.append({"kind": "polarity", "a": sa, "b": sb,
                              "subject": " ".join(sorted(shared))})
                seen.add((sa, sb))
    return found


def git_changed(vault, mem_rel):
    try:
        p = subprocess.run(["git", "-C", vault, "status", "--porcelain", "--", mem_rel],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    out = []
    for line in p.stdout.splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.endswith(".md"):
            out.append(path)
    return out


def setting_off(name):
    try:
        import gt_settings                                       # noqa: PLC0415
        return gt_settings.get(name) == "off"
    except Exception:                                            # noqa: BLE001
        return False


def _spool():
    """The vault tools' gt_spool (templates/tools): the ONE slug -> Projects/<path>
    resolver (0.18.1), shared with gt_adr and vault_init."""
    tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates", "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import gt_spool
    return gt_spool


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", required=True, help="the vault (never inferred)")
    ap.add_argument("--project", required=True, help="project slug")
    ap.add_argument("--changed", nargs="*", default=None,
                    help="memory notes written or changed this session")
    ap.add_argument("--work", action="store_true",
                    help="called by /gt:gt-work: honour the memory_contradiction_check setting")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="accepted for the vault-tool contract; this tool never writes")
    args = ap.parse_args(argv)

    if args.work and setting_off("memory_contradiction_check"):
        return 0
    if ".." in args.project.split("/") or "\\" in args.project or args.project in (".",) \
            or os.path.isabs(args.project):
        print("--project takes a slug, not a path: %r" % args.project, file=sys.stderr)
        return 2
    vault = os.path.abspath(os.path.expanduser(args.vault))
    if os.path.isdir(vault):
        # The shared resolver (0.18.1): a bare sub-project slug reads
        # Projects/<parent>/<slug>/memory, not a top-level folder that is not there.
        S = _spool()
        try:
            args.project = S.resolve_project(vault, args.project,
            allow_unregistered=True)  # an existing folder, as before; never creates
        except S.ProjectNotFound as exc:
            print("gt_memory_check: %s" % exc, file=sys.stderr)
            return 2
    mem_rel = "Projects/%s/memory" % args.project
    mem = os.path.join(vault, mem_rel)
    if not os.path.isdir(vault):
        print("not a vault directory: %s" % vault, file=sys.stderr)
        return 2
    if not os.path.isdir(mem):
        return 0                                 # no memory folder: nothing to contradict

    notes = {}
    for name in sorted(os.listdir(mem)):
        if name.endswith(".md") and name.upper() != "MEMORY.MD":
            p = os.path.join(mem, name)
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    notes["%s/%s" % (mem_rel, name)] = fh.read()
            except OSError:
                continue

    if args.changed is not None:
        changed = []
        for c in args.changed:
            c = c.replace(os.sep, "/")
            rel = c if c.startswith("Projects/") else "%s/%s" % (mem_rel, os.path.basename(c))
            if rel in notes:
                changed.append(rel)
    else:
        g = git_changed(vault, mem_rel)
        if g is not None:
            changed = [r for r in g if r in notes]
        else:
            cutoff = time.time() - 24 * 3600
            changed = [r for r in notes
                       if os.path.getmtime(os.path.join(vault, r)) >= cutoff]

    names = {r: name_words(r, t) for r, t in notes.items()}
    pairs, done = [], set()
    for rel in changed:
        for other in neighbours_of(names[rel], [(r, w) for r, w in names.items() if r != rel]):
            key = tuple(sorted((rel, other)))
            if key in done:
                continue
            done.add(key)
            conflicts = compare(rel, notes[rel], other, notes[other])
            if not conflicts:
                continue
            linked = links_to(notes[rel], other) or links_to(notes[other], rel)
            pairs.append({"a": rel, "b": other, "conflicts": conflicts[:5],
                          "linked": linked,
                          "update_language": bool(UPDATE_WORDS.search(notes[rel]))})

    if not pairs:
        if args.json:
            print(json.dumps({"version": 1, "checked": sorted(changed), "pairs": []}))
        return 0
    if args.json:
        print(json.dumps({"version": 1, "checked": sorted(changed), "pairs": pairs}, indent=2))
        return 1
    print("Possible contradictions in %s (%d pair%s) -- nothing has been changed:"
          % (mem_rel, len(pairs), "" if len(pairs) == 1 else "s"))
    for p in pairs:
        print("\n  %s\n  %s" % (p["a"], p["b"]))
        for c in p["conflicts"]:
            print("    [%s: %s]" % (c["kind"], c["subject"]))
            print("      A: %s" % c["a"][:200])
            print("      B: %s" % c["b"][:200])
        if not p["linked"]:
            print("    neither note links to the other, so a reader cannot tell which is current")
        print("    Are these the same fact? Which is current? Should the older one be updated, "
              "or linked to the newer?")
    return 1


if __name__ == "__main__":
    sys.exit(main())
