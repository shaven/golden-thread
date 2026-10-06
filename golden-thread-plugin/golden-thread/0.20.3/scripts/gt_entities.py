#!/usr/bin/env python3
"""Entity view of a project's memory: which memory files are about a thing.

    gt_entities.py --vault V lookup <name> --project <slug>   # the files tagged <name>, in full
    gt_entities.py --vault V list --project <slug>            # every declared entity, with files

Read-only. Never writes, never tags anything: `entities:` is set by a person (gt-work
prompts for it when a memory file is created), and gt_lint's `memory-entity-orphan`
check only SUGGESTS it.

## Why

Memory is filed by when it was written -- one file per topic or session -- so "what do
we know about the auth service?" meant knowing which of twenty files to open, or opening
all of them. A memory file may declare what it is about:

    ---
    name: auth-token-rotation
    description: "Token rotation interval and mechanism"
    entities:
      - auth-service
      - TOKENSVC
    ---

and `lookup auth` loads exactly the files that declared it. Matching is case-insensitive
SUBSTRING on the declared names (`auth` finds `auth-service`), never on the body: a file
that merely mentions a name is not a file about it, and guessing that is what the
declaration exists to replace.

Each hit prints its MEMORY.md description line (what the index says the file is for)
and then the file itself. Files without `entities:` are never returned and never
affected. One project at a time: cross-project entity queries are out of scope.

`<slug>` resolves exactly as gt_adr resolves it (gt_spool.resolve_project), so a
sub-project's bare slug works.

Exit: 0 answered (including "no files tagged") · 2 usage, or no such project.
"""
import argparse
import re
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "templates" / "tools"


def _spool():
    sys.path.insert(0, str(TOOLS))
    import gt_spool
    return gt_spool


def declared_entities(text):
    """The `entities:` list of a memory file's frontmatter: inline `[a, b]` or a block."""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return []
    fm = m.group(1)
    inline = re.search(r"^entities:\s*\[([^\]]*)\]", fm, re.M)
    if inline:
        return [i.strip().strip("\"'") for i in inline.group(1).split(",") if i.strip()]
    block = re.search(r"^entities:\s*\n((?:[ \t]+-[^\n]*\n?)+)", fm, re.M)
    if block:
        return [re.sub(r"^[ \t]+-\s*", "", l).strip().strip("\"'")
                for l in block.group(1).splitlines() if l.strip().startswith("-")]
    return []


def memory_files(project_dir):
    d = Path(project_dir) / "memory"
    return sorted(p for p in d.glob("*.md") if p.name != "MEMORY.md") if d.is_dir() else []


def index_description(project_dir, name):
    """What the project's MEMORY.md says about `name`, or ''."""
    idx = Path(project_dir) / "memory" / "MEMORY.md"
    try:
        lines = idx.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        if "(%s)" % name in line or "[[%s]]" % name[:-3] in line:
            parts = re.split(r"\s+[—–-]\s+", line, maxsplit=1)
            return parts[1].strip() if len(parts) > 1 else line.strip()
    return ""


def lookup(project_dir, name):
    want = name.strip().lower()
    hits = []
    for f in memory_files(project_dir):
        text = f.read_text(encoding="utf-8", errors="replace")
        ents = declared_entities(text)
        if any(want in e.lower() for e in ents):
            hits.append((f, ents, text))
    return hits


def main(argv=None):
    p = argparse.ArgumentParser(description="entity view of a project's memory (read-only)")
    p.add_argument("--vault", default=None)
    p.add_argument("--dry-run", "-n", action="store_true",
                   help="accepted for the CLI contract; this tool never writes")
    sub = p.add_subparsers(dest="cmd", required=True)
    lk = sub.add_parser("lookup", help="the memory files that declare an entity")
    lk.add_argument("name")
    lk.add_argument("--project", required=True)
    ls = sub.add_parser("list", help="every declared entity and its files")
    ls.add_argument("--project", required=True)
    a = p.parse_args(argv)
    S = _spool()
    try:
        vault = S.vault_root(a.vault)
        rel = S.resolve_project(vault, a.project)
    except S.ProjectNotFound as exc:
        print("gt_entities: %s" % exc, file=sys.stderr)
        return 2
    proj = vault / "Projects" / rel
    if a.cmd == "list":
        seen = {}
        for f in memory_files(proj):
            for e in declared_entities(f.read_text(encoding="utf-8", errors="replace")):
                seen.setdefault(e, []).append(f.name)
        if not seen:
            print("No memory files in %s declare entities." % rel)
        for e in sorted(seen, key=str.lower):
            print("%s: %s" % (e, ", ".join(seen[e])))
        return 0
    hits = lookup(proj, a.name)
    if not hits:
        print("No memory files tagged with entity '%s'" % a.name)
        return 0
    print("%d memory file(s) in %s tagged with entity '%s':" % (len(hits), rel, a.name))
    for f, ents, text in hits:
        desc = index_description(proj, f.name)
        print("\n===== Projects/%s/memory/%s  [entities: %s]" % (rel, f.name, ", ".join(ents)))
        if desc:
            print("MEMORY.md: %s" % desc)
        print(text.rstrip("\n"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
