#!/usr/bin/env python3
"""
gt_lint.py — Audit a Golden Thread vault for structural issues.

Usage:
  python3 gt_lint.py <vault-path> [--queue <output-file>] [--json]
  python3 gt_lint.py --vault <vault-path> --runbooks [--json]

  --json      findings as {"version":1,"vault","findings":[...],"counts"}.
              A default-mode finding is {kind, path, line, message, proposed_fix}:
              `proposed_fix` is ALWAYS present, and `line` is null for every
              default-mode check except decision-candidate (0.18.0), the one check
              whose finding IS a line. Under --runbooks a finding is
              {kind, path, line, message, text, locations} with a real `line`.
  --runbooks  read-only: only report lines duplicated (identical or difflib ratio >= 0.9)
              across two or more Projects/**/runbook.md, kind runbook-duplicate. Fewer
              than two runbooks prints "nothing to compare" and exits 0. Read-only means
              read-only: --queue is rejected with it rather than quietly ignored.

Checks:
  index-gap            Knowledge page exists but has no entry in index.md
  broken-link          [[wikilink]] that doesn't resolve to any file
  orphan               Knowledge page not linked from index or any other page
  memory-unlisted      File in Projects/*/memory/ not in that project's MEMORY.md
  global-gap           File in global-memory/ not in global-memory/MEMORY.md
  memory-bloat         global-memory file exceeds 30 lines (detail belongs in Knowledge/)
  global-scope-leak    global-memory file references a project slug (project-specific bleed-over)
  superseded-cited     Knowledge page cites a source that has been superseded
  stale                Knowledge page with status: stale in frontmatter
  source-todo          Project source.md with no topology or deployment targets
  frontmatter          Project README missing/incorrect property frontmatter
  core-misplaced       level: core rule living outside core-rules/
  core-no-enforcement  level: core rule with no enforcement declared
  core-unenforced      Core rule whose enforcement SCRIPT is not wired to its event
  adr-collision        two ADRs share a number in one project
  generated-hand-edited  log.md/decisions.md edited by hand instead of merged
  attribution-unwired  git's core.hooksPath does not reach the attribution hooks, so
                       per-edit attribution never reaches a commit message
  secrets-gate-unwired  .githooks/pre-commit exists but core.hooksPath does not reach
                       it, so the credential gate is shipped and never runs
  project-missing      A link points at a project folder that no longer exists
  adr-expires          ADR declaring `Expires when: <condition>` (always) or
                       `Expires: YYYY-MM-DD` (once past), with the condition text (0.18.0)
  bundled-concept      Knowledge page with 4+ `## ` headings of which at most 2 share a
                       keyword with its title/tags -- likely two topics (0.18.0)
  decision-candidate   design.md/research.md line with a decision-signal phrase ("we
                       chose", "by design", ...): a possible undocumented ADR (0.18.0).
                       Records a real `line`. Phrases: gt setting `decision_signals`
  memory-entity-orphan memory file naming an entity 3+ times without listing it in
                       `entities:` frontmatter (0.18.0)

Suppression: reads <vault>/lint-declines.md — lines starting with "suppress:". One rule,
used by every check (see is_suppressed):

  suppress: Projects/alpha/decisions.md      the finding's vault-relative path
  suppress: decisions.md                     that BARE FILE NAME, anywhere in the vault
  suppress: Knowledge/Page.md:[[Gone]]       a sub-key, always scoped by path or name
  suppress: Projects/a/decisions.md:ADR-7    adr-expires: one ADR
  suppress: Projects/a/design.md:#1a2b3c4d   decision-candidate: one LINE, by the hash of
                                             its text, so it survives the line moving
                                             (`:L12`, by line number, also works)

Matching is case-insensitive. A sub-key (a single wikilink, a cited source, one project
slug) is NEVER matched on its own: `suppress: alpha` would otherwise silence every
`[[alpha]]` in the vault, which is not what a one-word line looks like it does.

Exit codes:
  0 = clean
  1 = findings found
  2 = the tool could not run: bad usage, or a vault path that does not exist. A caller
      must distinguish this from 1 — "no findings reported" and "nothing was read" are
      not the same answer.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path


Finding = dict  # {check, path, message, proposed_fix}


def read_suppress_list(vault: Path) -> set:
    declines = vault / "lint-declines.md"
    suppressed = set()
    if declines.exists():
        for line in declines.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("suppress:"):
                suppressed.add(line[len("suppress:"):].strip().lower())
    return suppressed


def is_suppressed(suppressed: set, rel: str, name: str = None, detail: str = None) -> bool:
    """One suppression rule for every check. See the module docstring.

    A finding is suppressed by its vault-relative path, or by its bare file name
    (documented in templates/lint-declines.md, and what makes `suppress: index.md`
    work for a file that could move). A `detail` — one wikilink, one cited source,
    one project slug — is only ever honoured SCOPED: `<rel>:<detail>` or
    `<name>:<detail>`.

    Before this, scope was per-check: check_broken_links also matched a bare link
    target globally, so `suppress: alpha` silenced `[[alpha]]` in every file in the
    vault, while check_stale honoured only the relative path. Same file, same word,
    three different meanings.
    """
    keys = {rel.lower()}
    if name:
        keys.add(name.lower())
    if detail:
        keys |= {f"{k}:{detail}".lower() for k in list(keys)}
    return bool(keys & suppressed)


def strip_code(text: str) -> str:
    """Blank out fenced blocks and inline code spans.

    A wikilink written inside backticks is documentation ABOUT link syntax, not a
    link. CONVENTIONS.md and the runbooks do this routinely; counting those as
    real links produced broken-link findings for `[[wikilink]]` itself.
    Replaced with spaces rather than removed so offsets stay usable.
    """
    def blank(m):
        return re.sub(r'\S', ' ', m.group(0))
    text = re.sub(r'```.*?```', blank, text, flags=re.DOTALL)
    text = re.sub(r'~~~.*?~~~', blank, text, flags=re.DOTALL)
    text = re.sub(r'`[^`\n]*`', blank, text)
    return text


def extract_wikilinks(text: str) -> list:
    # `\\?` drops the backslash Obsidian requires before an alias pipe inside a
    # table (`[[Target\|alias]]`); without it the target read as `Target\`.
    return re.findall(r'\[\[([^\]|#]+?)\\?(?:[|#][^\]]*)?\]\]', strip_code(text))


def parse_frontmatter_status(text: str) -> str:
    m = re.search(r'^---\s*\n(.*?)\n---', text, re.DOTALL | re.MULTILINE)
    if not m:
        return ""
    for line in m.group(1).splitlines():
        if line.strip().startswith("status:"):
            return line.split(":", 1)[1].strip().strip('"\'')
    return ""


def all_knowledge_pages(vault: Path) -> list:
    k = vault / "Knowledge"
    if not k.exists():
        return []
    return list(k.glob("*.md"))


def page_title(path: Path) -> str:
    """Return stem as the effective wikilink target."""
    return path.stem


def in_index(index_text: str, page: Path) -> bool:
    """Match the actual link, not a bare substring: 'Quote API' must not count as
    indexed merely because 'Schwab Quote API' is listed."""
    title = page_title(page)
    return (f"[[{title}]]" in index_text
            or f"[[{title}|" in index_text
            or f"[[{title}\\|" in index_text
            or f"[[{title}#" in index_text
            or f"({page.name})" in index_text
            or f"Knowledge/{page.name}" in index_text)


def check_index_gap(vault: Path, findings: list, suppressed: set):
    index = vault / "index.md"
    if not index.exists():
        return
    index_text = index.read_text(encoding="utf-8")
    for page in all_knowledge_pages(vault):
        title = page_title(page)
        rel = f"Knowledge/{page.name}"
        if is_suppressed(suppressed, rel, page.name):
            continue
        if not in_index(index_text, page):
            findings.append({
                "check": "index-gap",
                "path": rel,
                "message": f"Knowledge/{page.name} exists but has no entry in index.md",
                "proposed_fix": f"Add to index.md: `[[{title}]]` — <one-line description>",
            })


def link_targets(vault: Path) -> dict:
    """Every name a [[wikilink]] may legitimately resolve to.

    Two kinds of target exist and only one used to be registered:

      * a PAGE — any .md file, addressed by its stem (`[[INFRASTRUCTURE]]`)
      * a PROJECT — a folder holding README.md, addressed by its folder name
        (`[[shome-security]]`), or for a sub-project by `parent/child`
        (`[[golden-thread/validation-agents]]`)

    Keying only on file stem made every project folder register as "readme", so
    no project slug could ever resolve. Measured against the reference vault on
    2026-08-22: 77 of 87 broken-link findings were this false positive, which
    buried the 2 real ones and made the check unusable.
    """
    targets = {}
    for p in vault.rglob("*.md"):
        targets.setdefault(p.stem.lower(), p)
        # 2026-09-08: Obsidian also resolves path-form links -- `[[chrome-extension/decisions]]`,
        # `[[historical-minute-backfill/design]]`, `[[chrome-extension/CLAUDE]]` -- by matching a
        # trailing run of path segments. Keying on the stem alone flagged 21 valid links as broken
        # in the reference vault. Register every trailing suffix of the vault-relative path
        # (without .md), so `parent/child/decisions`, `child/decisions` and `decisions` all resolve.
        parts = p.relative_to(vault).with_suffix("").parts
        if parts and parts[0] == "Projects":
            parts = parts[1:]
        for i in range(len(parts)):
            targets.setdefault("/".join(parts[i:]).lower(), p)
    # Attachments (`![[flow.png]]`) are addressed by their full file name, extension
    # included. Hidden folders (.git, .obsidian) hold no link targets.
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            p = Path(root) / name
            if p.suffix == ".md" or name.startswith("."):
                continue
            parts = p.relative_to(vault).parts
            if parts[0] == "Projects":
                parts = parts[1:]
            for i in range(len(parts)):
                targets.setdefault("/".join(parts[i:]).lower(), p)
    projects = vault / "Projects"
    if projects.is_dir():
        for readme in projects.rglob("README.md"):
            folder = readme.parent
            if folder == projects:
                continue
            targets.setdefault(folder.name.lower(), folder)
            rel = folder.relative_to(projects)
            if len(rel.parts) > 1:
                targets.setdefault(str(rel).lower(), folder)
    return targets


def check_broken_links(vault: Path, findings: list, suppressed: set):
    all_pages = link_targets(vault)

    for md_file in vault.rglob("*.md"):
        # Skip lint-declines itself
        if md_file.name == "lint-declines.md":
            continue
        rel = str(md_file.relative_to(vault))
        # A whole file can be suppressed, same as in every other check. Immutable
        # Sources/ files rely on this: their links point at the world as it was.
        # read_suppress_list() lowercases its entries, so compare lowercased.
        if is_suppressed(suppressed, rel, md_file.name):
            continue
        text = md_file.read_text(encoding="utf-8", errors="replace")
        links = extract_wikilinks(text)
        for link in links:
            target = link.strip()
            if is_suppressed(suppressed, rel, md_file.name, f"[[{target}]]"):
                continue
            if target.lower() not in all_pages:
                findings.append({
                    "check": "broken-link",
                    "path": rel,
                    "message": f"`[[{target}]]` in {rel} doesn't resolve to any vault page",
                    "proposed_fix": (
                        f"Remove the link, create `Knowledge/{target}.md`, "
                        f"or scaffold a project with slug `{target}`"
                    ),
                })


def check_orphans(vault: Path, findings: list, suppressed: set):
    index = vault / "index.md"
    index_text = index.read_text(encoding="utf-8") if index.exists() else ""

    # Build all links across vault
    all_linked_titles = set()
    for md_file in vault.rglob("*.md"):
        text = md_file.read_text(encoding="utf-8", errors="replace")
        for link in extract_wikilinks(text):
            all_linked_titles.add(link.strip().lower())

    for page in all_knowledge_pages(vault):
        title = page_title(page)
        rel = f"Knowledge/{page.name}"
        if is_suppressed(suppressed, rel, page.name):
            continue
        in_links = title.lower() in all_linked_titles
        if not in_index(index_text, page) and not in_links:
            findings.append({
                "check": "orphan",
                "path": rel,
                "message": f"Knowledge/{page.name} is not linked from index.md or any other page",
                "proposed_fix": f"Add to index.md or link from a related Knowledge page",
            })


def check_memory_unlisted(vault: Path, findings: list, suppressed: set):
    projects_dir = vault / "Projects"
    if not projects_dir.exists():
        return
    # rglob, not iterdir: sub-projects created with --parent live one level deeper,
    # and a single-level walk silently skips them.
    for memory_dir in sorted(projects_dir.rglob("memory")):
        if not memory_dir.is_dir():
            continue
        proj = memory_dir.parent
        memory_index = memory_dir / "MEMORY.md"
        index_text = memory_index.read_text(encoding="utf-8") if memory_index.exists() else ""
        for f in sorted(memory_dir.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            rel = str(f.relative_to(vault))
            if is_suppressed(suppressed, rel, f.name):
                continue
            if f.name not in index_text and f.stem not in index_text:
                findings.append({
                    "check": "memory-unlisted",
                    "path": rel,
                    "message": f"{rel} is not listed in {proj.name}/memory/MEMORY.md",
                    "proposed_fix": f"Add to MEMORY.md: `- [{f.stem}]({f.name}) — <description>`",
                })


def check_global_gap(vault: Path, findings: list, suppressed: set):
    gm = vault / "global-memory"
    if not gm.exists():
        return
    gm_index = gm / "MEMORY.md"
    index_text = gm_index.read_text(encoding="utf-8") if gm_index.exists() else ""
    for f in sorted(gm.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        rel = f"global-memory/{f.name}"
        if is_suppressed(suppressed, rel, f.name):
            continue
        if f.name not in index_text and f.stem not in index_text:
            findings.append({
                "check": "global-gap",
                "path": rel,
                "message": f"{rel} is not reachable from global-memory/MEMORY.md",
                "proposed_fix": f"Add to global-memory/MEMORY.md: `- [{f.stem}]({f.name}) — <description>`",
            })


def parse_frontmatter_field(text: str, field: str) -> list:
    """Parse a frontmatter list field. Returns a list of strings."""
    m = re.search(r'^---\s*\n(.*?)\n---', text, re.DOTALL | re.MULTILINE)
    if not m:
        return []
    fm = m.group(1)
    inline = re.search(rf'^{re.escape(field)}:\s*\[([^\]]*)\]', fm, re.MULTILINE)
    if inline:
        return [i.strip().strip('"\'') for i in inline.group(1).split(',') if i.strip()]
    block = re.search(rf'^{re.escape(field)}:\s*\n((?:[ \t]+-[^\n]*\n?)+)', fm, re.MULTILINE)
    if block:
        return [re.sub(r'^[ \t]+-\s*', '', l).strip().strip('"\'')
                for l in block.group(1).splitlines() if l.strip().startswith('-')]
    return []


def build_superseded_map(vault: Path) -> dict:
    """Map old source filename → new source filename by scanning Sources/ supersedes: fields."""
    sources_dir = vault / "Sources"
    superseded = {}
    if not sources_dir.exists():
        return superseded
    for src in sources_dir.glob("*.md"):
        text = src.read_text(encoding="utf-8", errors="replace")
        for old in parse_frontmatter_field(text, "supersedes"):
            superseded[Path(old).name] = src.name
    return superseded


def check_superseded_cited(vault: Path, findings: list, suppressed: set):
    superseded_map = build_superseded_map(vault)
    if not superseded_map:
        return
    for page in all_knowledge_pages(vault):
        text = page.read_text(encoding="utf-8", errors="replace")
        rel = f"Knowledge/{page.name}"
        for src in parse_frontmatter_field(text, "sources"):
            src_name = Path(src).name
            if src_name not in superseded_map:
                continue
            new_name = superseded_map[src_name]
            if is_suppressed(suppressed, rel, page.name, src_name):
                continue
            findings.append({
                "check": "superseded-cited",
                "path": rel,
                "message": f"`{rel}` cites `{src}` which has been superseded by `Sources/{new_name}`",
                "proposed_fix": f"Review changes between old and new source, update page content if needed, update sources: to `Sources/{new_name}`",
            })


def decisions_files(vault: Path) -> list:
    """Every project's decisions.md, at ANY depth under Projects/.

    `Projects/*/decisions.md` is one level and sub-projects live deeper —
    `Projects/automated-trading-system/trading-tsla/decisions.md`. The spool's own
    working files are skipped: they are inputs to the generated file, not copies of it.
    """
    projects = vault / "Projects"
    if not projects.is_dir():
        return []
    return sorted(p for p in projects.rglob("decisions.md")
                  if p.is_file() and "spool" not in p.relative_to(projects).parts)


def project_key(vault: Path, decisions: Path) -> str:
    """The project id gt_adr uses: the path under Projects/, so a sub-project reads
    `parent/child` rather than just `child` (gt_adr.target() joins it straight back on)."""
    return str(decisions.parent.relative_to(vault / "Projects"))


def check_adr_collision(vault: Path, findings: list, suppressed: set):
    """Two ADRs sharing a number in one project.

    `Projects/cyc26-talk/decisions.md` carried two different ADR-6 headings for weeks
    with nothing reporting it -- two unrelated decisions answering to one name, in the
    file whose whole purpose is to be the stable record.

    `## ADR-6 amendment:` is DELIBERATE usage (external-ai-tools) and is not a second
    allocation, so it is excluded. Flagging it would train people to ignore the check.
    The exclusion is CASE-INSENSITIVE, and tolerates the bracketed form: the promise
    above was written against `re.M` alone, so `Amendment:`, `AMENDMENT:` and
    `(amendment):` were all reported as collisions — the check training people to
    ignore it in exactly the way this docstring says it must not.

    rglob, not glob: a single level skipped every sub-project. 19 decisions.md files
    sit at depth >= 3 in the reference vault, and not one of them was ever examined.
    """
    head = re.compile(r"^##\s*ADR-(\d+)\s*(?:[(\[]\s*)?(amendment)?", re.M | re.I)
    for dec in sorted(decisions_files(vault)):
        rel = str(dec.relative_to(vault))
        if is_suppressed(suppressed, rel, dec.name):
            continue
        try:
            text = dec.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        nums = [int(m.group(1)) for m in head.finditer(text) if not m.group(2)]
        dupes = sorted({n for n in nums if nums.count(n) > 1})
        for n in dupes:
            findings.append({
                "check": "adr-collision",
                "path": rel,
                "message": f"ADR-{n} is used by more than one decision in {project_key(vault, dec)}",
                "proposed_fix": ("Two sessions took the same number. Decide which keeps it; "
                                 "renumbering the other invalidates inbound references, so "
                                 "check for them first. Allocate with gt_adr.py in future."),
            })


def check_generated_hand_edited(vault: Path, findings: list, suppressed: set):
    """A generated file that no longer matches its spool -- i.e. someone typed in it.

    Only checks files that ALREADY carry the generated banner: a vault that has not
    migrated is not in breach, it simply has not adopted this yet.
    """
    tools = vault / "Projects" / "golden-thread" / "tools"
    spool_mod = tools / "gt_spool.py"
    if not spool_mod.is_file():
        return
    try:
        sys.path.insert(0, str(tools))
        import importlib
        S = importlib.import_module("gt_spool")
        importlib.reload(S)
    except Exception:
        return
    targets = [(vault / "log.md", "log", None)]
    # rglob via decisions_files(): the single-level glob here examined only top-level
    # projects, so a hand edit to any sub-project's generated decisions.md -- lost at
    # the next merge, which is the whole finding -- was never reported.
    for dec in decisions_files(vault):
        targets.append((dec, "decisions", project_key(vault, dec)))
    for path, kind, project in targets:
        if not path.is_file() or not S.is_generated(path):
            continue
        rel = str(path.relative_to(vault))
        if is_suppressed(suppressed, rel, path.name):
            continue
        try:
            if kind == "log":
                body = "\n".join(S.lines(vault, "log"))
                expected = S.BANNER + "\n\n" + body + ("\n" if body else "")
            else:
                sys.path.insert(0, str(tools))
                A = importlib.import_module("gt_adr")
                importlib.reload(A)
                expected = A.render(vault, project)
        except Exception:
            continue
        if path.read_text(encoding="utf-8", errors="replace") != expected:
            findings.append({
                "check": "generated-hand-edited",
                "path": rel,
                "message": f"{path.name} is generated but does not match its spool — "
                           "a hand edit here is lost at the next merge",
                "proposed_fix": ("Move the change into your own spool file under "
                                 "Projects/golden-thread/spool/, then re-run the merge."),
            })


def check_memory_bloat(vault: Path, findings: list, suppressed: set):
    gm = vault / "global-memory"
    if not gm.exists():
        return
    for f in sorted(gm.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        rel = f"global-memory/{f.name}"
        if is_suppressed(suppressed, rel, f.name):
            continue
        lines = len(f.read_text(encoding="utf-8", errors="replace").splitlines())
        if lines > 30:
            findings.append({
                "check": "memory-bloat",
                "path": rel,
                "message": f"{rel} has {lines} lines — global-memory files should stay under 30 lines",
                "proposed_fix": "Move detailed tables/sections to a Knowledge/ page; keep only the 3-5 most essential facts here",
            })


def check_global_scope_leak(vault: Path, findings: list, suppressed: set):
    gm = vault / "global-memory"
    projects_dir = vault / "Projects"
    if not gm.exists() or not projects_dir.exists():
        return
    project_slugs = {d.name.lower() for d in projects_dir.rglob("*")
                     if d.is_dir() and (d / "README.md").exists() and d.name != "memory"}
    for f in sorted(gm.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        rel = f"global-memory/{f.name}"
        if is_suppressed(suppressed, rel, f.name):
            continue
        text = f.read_text(encoding="utf-8", errors="replace").lower()
        for slug in sorted(project_slugs):
            if is_suppressed(suppressed, rel, f.name, slug):
                continue
            if slug in text:
                findings.append({
                    "check": "global-scope-leak",
                    "path": rel,
                    "message": f"{rel} references project slug '{slug}' — global-memory should contain only cross-project facts",
                    "proposed_fix": f"Move '{slug}'-specific content to Projects/{slug}/memory/ or Projects/{slug}/decisions.md",
                })
                break


def check_stale(vault: Path, findings: list, suppressed: set):
    for page in all_knowledge_pages(vault):
        text = page.read_text(encoding="utf-8", errors="replace")
        status = parse_frontmatter_status(text)
        rel = f"Knowledge/{page.name}"
        if is_suppressed(suppressed, rel, page.name):
            continue
        if status == "stale":
            findings.append({
                "check": "stale",
                "path": rel,
                "message": f"Knowledge/{page.name} is marked status: stale",
                "proposed_fix": "Update the page and change status, or run /gt-promote → retire",
            })


def has_table_data(text: str) -> bool:
    """True if any markdown table in `text` has at least one row with real content.

    A row counts only if it follows a |---|---| separator, so header rows are not
    mistaken for data. Rows whose cells are all blank don't count either — the
    scaffold ships with empty placeholder rows.
    """
    seen_separator = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            seen_separator = False
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and all(set(c) <= {"-", ":"} and c for c in cells):
            seen_separator = True
            continue
        if seen_separator and any(cells):
            return True
    return False


def check_source_todo(vault: Path, findings: list, suppressed: set):
    """Flag source.md files whose topology or deployment targets were never filled in.

    An unfilled source.md is worse than none at all: it looks authoritative while
    telling you nothing about which host to touch.
    """
    projects_dir = vault / "Projects"
    if not projects_dir.exists():
        return
    for src in sorted(projects_dir.rglob("source.md")):
        rel = str(src.relative_to(vault))
        if is_suppressed(suppressed, rel, src.name):
            continue
        text = src.read_text(encoding="utf-8", errors="replace")
        proj = src.parent.name

        topology = None
        for line in text.splitlines():
            if line.startswith("**Topology:**"):
                topology = line.split("**Topology:**", 1)[1].strip()
                break

        if not topology or topology == "TODO":
            findings.append({
                "check": "source-todo",
                "path": rel,
                "message": f"{proj} has no topology recorded in source.md",
                "proposed_fix": "Set **Topology:** to local, remote, bastion-jump, or bastion-direct",
            })

        # A targets table with no data rows means nobody knows where this deploys.
        if not has_table_data(text):
            findings.append({
                "check": "source-todo",
                "path": rel,
                "message": f"{proj}/source.md has no deployment targets filled in",
                "proposed_fix": "Add at least one role x env row, or set topology to 'local' if there is nothing to deploy",
            })

        if topology and topology.startswith("bastion") and "[[" not in text:
            findings.append({
                "check": "source-todo",
                "path": rel,
                "message": f"{proj} is {topology} but links no fleet definition",
                "proposed_fix": "Set **Fleet:** to [[INFRASTRUCTURE]] so the host table is not duplicated here",
            })


def check_frontmatter(vault: Path, findings: list, suppressed: set):
    """Project READMEs must carry the property frontmatter the Dataview views read.

    A project missing these is invisible to the vault's own index views, and a
    slug that disagrees with its folder makes every generated link wrong.
    """
    projects_dir = vault / "Projects"
    if not projects_dir.exists():
        return
    required = ("type", "slug", "domain", "stage")
    for readme in sorted(projects_dir.rglob("README.md")):
        proj = readme.parent
        if proj == projects_dir:
            continue  # the master index is not a project
        if not (proj / "idea.md").exists():
            continue  # not a project folder
        rel = str(readme.relative_to(vault))
        if is_suppressed(suppressed, rel, readme.name):
            continue
        text = readme.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
        if not m:
            findings.append({
                "check": "frontmatter",
                "path": rel,
                "message": f"{proj.name}/README.md has no property frontmatter",
                "proposed_fix": "Add type/slug/domain/stage/topology/tags — see CONVENTIONS.md",
            })
            continue
        fm = dict(re.findall(r"^([a-z_]+):\s*(.+)$", m.group(1), re.M))
        missing = [k for k in required if k not in fm]
        if missing:
            findings.append({
                "check": "frontmatter",
                "path": rel,
                "message": f"{proj.name}/README.md frontmatter missing: {', '.join(missing)}",
                "proposed_fix": "Add the missing keys — see CONVENTIONS.md",
            })
        if fm.get("slug") and fm["slug"] != proj.name:
            findings.append({
                "check": "frontmatter",
                "path": rel,
                "message": f"{proj.name}/README.md declares slug '{fm['slug']}' but the folder is '{proj.name}'",
                "proposed_fix": f"Set slug: {proj.name}",
            })
        if fm.get("domain", "").upper() == "TODO":
            findings.append({
                "check": "frontmatter",
                "path": rel,
                "message": f"{proj.name} has no domain set",
                "proposed_fix": "Set domain: to a value from the CONVENTIONS.md taxonomy",
            })


def parse_frontmatter_map(text: str) -> dict:
    """Flat key->value of the YAML frontmatter (nested keys included, un-nested)."""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        km = re.match(r"^\s*([a-zA-Z_][a-zA-Z0-9_-]*)\s*:\s*(.*)$", line)
        if km:
            out[km.group(1)] = km.group(2).strip().strip('"\'')
    return out


# The script that implements each declared enforcement mechanism, and the event it has
# to be registered on. vault_init.py install-core-rules writes exactly these pairs (it
# reads them from gt_components.HOOK_REGISTRATIONS); this is the same statement read
# back, so "declared" and "wired" are compared against the same thing.
ENFORCEMENT_MECHANISM = {
    "validated": ("Stop", "validate_response.sh"),
    "reminder": ("UserPromptSubmit", "inject_core_rules.sh"),
}
DEFAULT_MECHANISM = ENFORCEMENT_MECHANISM["reminder"]


def wired_hook_commands(settings_path: Path = None) -> dict:
    """{event: [command string, ...]} for every hook registered in settings.json.

    COMMANDS, not merely which events are occupied. The predecessor
    (wired_hook_events) recorded an event as wired as soon as any block under it
    held a non-empty `hooks` list, and never looked at `command` — so a vault with
    somebody else's UserPromptSubmit hook and inject_core_rules.sh nowhere in
    settings.json was reported as enforcing the whole Core tier.

    That is this project's own 0.9.5 failure restated: the hook existed, install.sh
    copied it, the component check compared file contents and reported clean, and
    nothing had registered the event. Occupancy is not enforcement. vault_init.py
    has always compared `h.get("command") == str(script)`; this reads the same field.
    """
    settings = settings_path or (Path.home() / ".claude" / "settings.json")
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for event, blocks in (data.get("hooks") or {}).items():
        for b in blocks or []:
            if not isinstance(b, dict):
                continue
            for h in b.get("hooks") or []:
                if isinstance(h, dict) and h.get("command"):
                    out.setdefault(event, []).append(str(h["command"]))
    return out


def mechanism_wired(commands: dict, event: str, script: str) -> bool:
    """Is `script` the command of some hook registered on `event`?

    Matched on the command's basename rather than on one absolute path: the
    canonical wiring is ~/.claude/golden-thread/hooks/<script>, but a command may
    legitimately be `bash /elsewhere/<script>` or carry arguments, and refusing
    those would report an installed mechanism as missing. Anything that does not
    name the script at all — the third-party hook, the empty block, `"command": "x"`
    — is not this mechanism, which is the whole point of reading the field.
    """
    for cmd in commands.get(event) or []:
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            tokens = cmd.split()
        for t in tokens + [cmd]:
            if os.path.basename(t.strip('"\'')) == script:
                return True
    return False


def check_core_rules(vault: Path, findings: list, suppressed: set):
    """Audit that Core rules are ENFORCED, not merely stored.

    The whole point of the Core tier is that storing a rule is not enough — the
    2026-08-16 incident had the timestamp rule sitting in global-memory while
    silently not being applied. So this checks the mechanism, not the file.
    """
    core_dir = None
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from gt_paths import find_core_rules
        core_dir = find_core_rules(vault, record=False)
    except Exception:
        pass
    if core_dir is None:
        for cand in sorted(vault.rglob("core-rules")):
            if (cand / "core_rule_priority_model.md").is_file():
                core_dir = cand
                break
    commands = wired_hook_commands()

    for md in sorted(vault.rglob("*.md")):
        if ".git" in md.parts or "templates" in md.parts:
            continue
        rel = str(md.relative_to(vault))
        if is_suppressed(suppressed, rel, md.name):
            continue
        try:
            fm = parse_frontmatter_map(md.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if fm.get("level") != "core":
            continue

        # (i) must live in the canonical folder
        if core_dir is None or core_dir not in md.parents:
            findings.append({
                "check": "core-misplaced",
                "path": rel,
                "message": f"{md.name} declares level: core but lives outside core-rules/",
                "proposed_fix": f"Move it to {core_dir.relative_to(vault) if core_dir else 'core-rules/'}/, or lower its level",
            })

        # (ii) must declare an enforcement mechanism
        enf = fm.get("enforcement")
        if not enf:
            findings.append({
                "check": "core-no-enforcement",
                "path": rel,
                "message": f"{md.name} is level: core with no enforcement declared",
                "proposed_fix": "Add enforcement: reminder (re-injected) or validated (output-checked)",
            })
            continue

        # (iii) the declared mechanism must actually be wired — the SCRIPT that
        # implements it, on the event it implements it on. An event occupied by
        # somebody else's hook enforces nothing here.
        needed, script = ENFORCEMENT_MECHANISM.get(enf, DEFAULT_MECHANISM)
        if not mechanism_wired(commands, needed, script):
            occupied = " (the event is wired, but to something else)" \
                if commands.get(needed) else ""
            findings.append({
                "check": "core-unenforced",
                "path": rel,
                "message": (f"{md.name} declares enforcement: {enf} but no {needed} hook runs "
                            f"{script}{occupied} — the rule is stored, never re-asserted"),
                "proposed_fix": (f"Wire {needed} in ~/.claude/settings.json to "
                                 f"~/.claude/golden-thread/hooks/{script}"
                                 " (or run vault_init.py install-core-rules)"),
            })

    # A core-rules folder with no wiring at all is the headline failure.
    if core_dir and core_dir.exists() and not any(
            mechanism_wired(commands, ev, sc) for ev, sc in ENFORCEMENT_MECHANISM.values()):
        rel = str(core_dir.relative_to(vault))
        if not is_suppressed(suppressed, rel, core_dir.name):
            findings.append({
                "check": "core-unenforced",
                "path": rel,
                "message": "core-rules/ exists but neither enforcement hook is wired — the Core tier is inert",
                "proposed_fix": "Run: vault_init.py install-core-rules --vault <vault>",
            })


def check_project_refs(vault: Path, findings: list, suppressed: set):
    """Catch links to a project folder that no longer exists.

    Renames, merges and archives all move folders. A merge leaves a tombstone so links
    still land somewhere, but a hand-edited path or a half-finished rename does not —
    and a markdown link to a missing folder fails silently in Obsidian.
    """
    projects = vault / "Projects"
    if not projects.exists():
        return
    known = {d.name for d in projects.rglob("*") if d.is_dir() and (d / "README.md").exists()}
    known |= {d.name for d in projects.iterdir() if d.is_dir()}

    for md in sorted(vault.rglob("*.md")):
        if ".git" in md.parts or "templates" in md.parts:
            continue
        rel = str(md.relative_to(vault))
        if is_suppressed(suppressed, rel, md.name):
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in re.finditer(r"\]\(([A-Za-z0-9._/-]*?Projects/)?([a-z0-9][a-z0-9-]*)/\)", text):
            slug = m.group(2)
            if slug in known or slug in {"hooks", "memory", "core-rules"}:
                continue
            findings.append({
                "check": "project-missing",
                "path": rel,
                "message": f"{md.name} links to project '{slug}/' which does not exist",
                "proposed_fix": (f"Point it at the renamed/merged project, or leave a tombstone "
                                 f"README at Projects/{slug}/"),
            })


# --------------------------------------------------------------------------- review queue
# Where the digest of the LAST queue this tool generated is kept. A generation receipt,
# exactly as gt_tasks.py keeps one for TASKS.md -- but a SEPARATE file, because this is a
# different tool writing a different target, and because --queue names an arbitrary path:
# the receipt is a {resolved queue path: sha256} map, so two vault worklists cannot
# invalidate each other's receipt.
QUEUE_DIGESTS = "Projects/golden-thread/.lint-queue-digests"
QUEUE_BACKUPS = "Projects/golden-thread/backups"
# How many copies of one queue file to keep. Nothing pruned this folder, and because a
# failed receipt makes EVERY later run see a mismatch, "occasionally" was really "every
# run, for ever". Ten is enough to recover a note typed a few runs ago and small enough
# that the folder never becomes the reason someone stops reading the warning.
QUEUE_BACKUPS_KEEP = 10

# `- [ ] `path` — message`, the item lines write_queue itself emits.
QUEUE_ITEM = re.compile(r"^- \[([ xX])\] `(.*?)` — (.*)$")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_queue_digests(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def untick(text: str) -> str:
    """The queue with every tick cleared, i.e. the form this tool generates.

    The receipt is compared against THIS, not against the raw file. write_queue
    always emits `- [ ]` and carries ticks across faithfully, so a file that differs
    only by ticks is a file this tool did generate, worked through exactly as
    intended. Comparing raw text made the "not what this tool last generated"
    warning and its backup fire on every run after anybody ticked anything —
    routine, always benign, and therefore training people to ignore the one case
    (a hand-typed note, a gt-promote REVIEW item) where the warning is real.
    """
    # re.M over the whole string, not a splitlines()/join round trip: that would drop the
    # trailing newline and make every untouched queue look edited.
    return re.sub(r"^- \[[xX]\] ", "- [ ] ", text or "", flags=re.M)


def _backup_queue(bdir: Path, queue_path: Path) -> Path:
    """Copy the queue aside under a name nothing else can take, then prune.

    The stamp is second-resolution and shutil.copy2 overwrites, so two runs inside
    one second destroyed the first copy -- a backup that silently replaces a backup.
    A counter is appended until the name is free.
    """
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    kept = bdir / f"{queue_path.name}.{stamp}"
    n = 2
    while kept.exists():
        kept = bdir / f"{queue_path.name}.{stamp}-{n}"
        n += 1
    shutil.copy2(queue_path, kept)
    existing = sorted(bdir.glob(f"{queue_path.name}.*"), key=lambda p: (p.stat().st_mtime, p.name))
    for old in existing[:-QUEUE_BACKUPS_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return kept


def ticked_items(text: str) -> set:
    """{(path, message)} for every item a person has checked off in an existing queue."""
    done = set()
    for line in (text or "").splitlines():
        m = QUEUE_ITEM.match(line.rstrip())
        if m and m.group(1).lower() == "x":
            done.add((m.group(2), m.group(3)))
    return done


def write_queue(vault: Path, findings: list, queue_path: Path):
    """Replace the review queue without destroying the human work recorded in it.

    UNLIKE TASKS.md, this file is NOT a pure projection, so ticks are PRESERVED, not
    merely backed up. Three things in the code decide that:

      * nothing regenerates a tick. TASKS.md ticks live upstream in each project's
        README.md `## Tasks`, so gt_tasks can honestly say "make the change there".
        A lint finding has no upstream checkbox at all -- findings are recomputed from
        vault state on every run and this function hard-codes `- [ ]`. The tick exists
        in this file and nowhere else, so a truncating write is the only copy going.
      * an unfixed finding comes BACK. If a tick meant only "fixed", it would be
        redundant -- the finding would simply vanish from the next queue. A tick that
        survives regeneration is therefore precisely the human judgement ("triaged",
        "accepting this one", "not now") that the recomputation cannot reach.
        `lint-declines.md` records a permanent decline; the tick is the lighter,
        in-flight state, and nothing else holds it.
      * the queue is READ by other things. skills/gt-lint points --queue at
        <vault>/review-queue.md, gt-open reports its "pending items" count, and
        vault_init.py:_append writes gt-promote's parked REVIEW items into that same
        file. So this file carries content this tool did not generate and cannot
        regenerate.

    Hence, in order:
      1. carry every still-open finding's tick across from the file on disk;
      2. compare the file, WITH ITS TICKS CLEARED (untick), against the digest of what
         we generated last time, and if it differs copy it aside BEFORE touching it and
         say so -- ticks come across, but anything else a person or gt-promote put in
         the file does not, and a backup nobody is told about is not a backup;
      3. write atomically (tmp + fsync + os.replace) so a crash or a Dropbox sync
         mid-write cannot leave a torn worklist. Prevention, not recovery;
      4. record the receipt, reporting rather than raising if that fails: by then the
         queue is already replaced, and dying there loses the message naming the copy.
    An untouched queue -- and a queue whose only change is the ticks this function
    itself carries across -- takes neither a note nor a backup, so the warning keeps
    meaning. Copies are capped at QUEUE_BACKUPS_KEEP per queue file and cannot collide.

    Called for EVERY --queue run, findings or none: a clean vault used to leave the
    previous run's file in place, still listing findings that had been fixed, which
    gt-open then reported as pending items.
    """
    notes = []
    queue_path = Path(queue_path)
    current = None
    if queue_path.exists():
        try:
            current = queue_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = None          # unreadable: back it up rather than reason about it
    done = ticked_items(current)

    lines = ["# Vault Review Queue\n\nGenerated by gt_lint.py\n"]
    by_check = {}
    for f in findings:
        by_check.setdefault(f["check"], []).append(f)
    carried = 0
    for check, items in sorted(by_check.items()):
        lines.append(f"\n## {check} ({len(items)} items)\n")
        for item in items:
            ticked = (item["path"], item["message"]) in done
            carried += 1 if ticked else 0
            lines.append(f"- [{'x' if ticked else ' '}] `{item['path']}` — {item['message']}")
            lines.append(f"  - Fix: {item['proposed_fix']}\n")
    if not findings:
        lines.append("\n_No open findings._\n")
    out = "\n".join(lines)

    digest_path = vault / QUEUE_DIGESTS
    digests = _read_queue_digests(digest_path)
    key = str(queue_path.resolve())
    recorded = digests.get(key)
    # An absent receipt is UNKNOWN, not permission. On the first run after this landed
    # there genuinely is none, and refusing would wedge every vault -- so back up and
    # continue, which is neither refusing nor overwriting in silence.
    if queue_path.exists() and (current is None or _sha(untick(current)) != recorded):
        kept = _backup_queue(vault / QUEUE_BACKUPS, queue_path)
        try:
            shown = kept.relative_to(vault)
        except ValueError:
            shown = kept
        notes.append(f"{queue_path.name} on disk is not what this tool last generated")
        notes.append(f"  kept a copy: {shown}")
        if not recorded:
            notes.append("  no previous digest recorded — first run since this check existed, "
                         "so this is probably just the pre-existing file")
        notes.append(f"  ticked items were carried into the regenerated queue ({carried} kept); "
                     "anything else added by hand — notes, or gt-promote's parked REVIEW "
                     "items — is only in that copy")

    queue_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = queue_path.with_name(f"{queue_path.name}.gt-tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(out)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, queue_path)
    finally:
        if tmp.exists():
            tmp.unlink()            # a failed write leaves no litter beside the worklist

    # The receipt is written LAST but may not take the caller down with it. It used to
    # run unguarded after os.replace: an unwritable receipt (a directory in its place, a
    # read-only vault, a full disk) raised AFTER the queue had been replaced and copied
    # aside, so `return notes` never ran and the "kept a copy: ..." line the user needs
    # in order to find that copy was never printed -- a traceback instead, about a file
    # they had not asked about. The queue itself is already safely written by here; a
    # missing receipt only costs one spurious backup on the next run, so it is reported
    # and the notes are returned.
    digests[key] = _sha(out)
    try:
        digest_path.parent.mkdir(parents=True, exist_ok=True)
        digest_path.write_text(json.dumps(digests, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
    except OSError as exc:
        notes.append(f"could not record the generation receipt {QUEUE_DIGESTS}: {exc}")
        notes.append("  the queue itself was written; the next run will copy it aside "
                     "again because it cannot tell it generated this one")
    return notes


def check_attribution_wired(vault: Path, findings: list, suppressed: set):
    """Does git actually reach the attribution hooks? (check: attribution-unwired)

    WHAT THIS CHECKS, because the module docstring used to claim something else
    entirely ("a session wrote vault files with no registered task" — a different
    check, on a different subject, that this function has never performed). In a
    vault that is a git repo and has adopted gt_edits.py, it reads
    `git config --get core.hooksPath` and reports:

      * core.hooksPath unset            -> the commit hooks never run at all
      * <hooksPath>/prepare-commit-msg or /post-commit missing
      * either of those present but not executable (git skips it in silence)

    Per-edit attribution is INSTALLED but is it POINTED AT?

    Same shape as check_core_rules above, one layer down. `.githooks/` is tracked
    so the hooks travel with the repo, but `core.hooksPath` is per-clone LOCAL
    config -- it does not travel, and nothing about a fresh clone announces its
    absence. In that state safe_write keeps recording attribution to
    .git/gt-edits.jsonl and the ledger simply never reaches a commit message: no
    error, no warning, silently no permanent record.

    That is exactly the "stored but never re-asserted" failure the Core tier
    exists to catch. This check was itself missed on 2026-08-29 -- the task
    listing it was marked done with this third item unbuilt, which is the same
    mistake in miniature.
    """
    if not (vault / ".git").exists():
        return                      # not a repo: attribution does not apply
    tools = vault / "Projects" / "golden-thread" / "tools" / "gt_edits.py"
    if not tools.exists():
        return                      # attribution not adopted here at all

    hookspath = None
    try:
        import subprocess
        out = subprocess.run(["git", "-C", str(vault), "config", "--get", "core.hooksPath"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            hookspath = out.stdout.strip() or None
    except Exception:
        return                      # cannot tell: stay silent rather than cry wolf

    if not hookspath:
        findings.append({
            "check": "attribution-unwired",
            "path": ".git/config",
            "message": ("gt_edits.py is present but core.hooksPath is unset, so the "
                        "commit hooks never run and per-edit attribution never reaches "
                        "a commit — silently"),
            "proposed_fix": "git -C <vault> config core.hooksPath .githooks",
        })
        return

    for hook in ("prepare-commit-msg", "post-commit"):
        f = vault / hookspath / hook
        if not f.exists():
            findings.append({
                "check": "attribution-unwired",
                "path": f"{hookspath}/{hook}",
                "message": f"core.hooksPath is {hookspath} but {hook} is missing there",
                "proposed_fix": "re-run the plugin install.sh to restore the git hooks",
            })
        elif not (f.stat().st_mode & 0o111):
            findings.append({
                "check": "attribution-unwired",
                "path": f"{hookspath}/{hook}",
                "message": f"{hook} is present but not executable, so git silently skips it",
                "proposed_fix": f"chmod +x {hookspath}/{hook}",
            })


def check_secrets_gate_wired(vault: Path, findings: list, suppressed: set):
    """Is the pre-commit credential gate REACHABLE? (check: secrets-gate-unwired)

    Deliberately a separate check from check_attribution_wired above, rather than one more
    hook name in its loop, because the two have different subjects and different stakes.
    That function reports `attribution-unwired` and is gated on gt_edits.py being adopted;
    filing a credential gate under that name would describe a security exposure as a
    bookkeeping gap, and whoever read the finding would triage it accordingly.

    The failure this catches is the one this release keeps meeting: `.githooks/pre-commit`
    is TRACKED, so it travels with every clone, but `core.hooksPath` is per-clone LOCAL
    config that does not travel and whose absence nothing announces. In that state the gate
    sits in the repo looking installed and never runs once -- a shipped-but-inert control,
    which is worse than none, because its presence is taken for coverage.
    """
    if not (vault / ".git").exists():
        return
    if not (vault / ".githooks" / "pre-commit").exists():
        return                      # this vault predates the gate; nothing to be wired

    hookspath = None
    try:
        import subprocess
        out = subprocess.run(["git", "-C", str(vault), "config", "--get", "core.hooksPath"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            hookspath = out.stdout.strip() or None
    except Exception:
        return                      # cannot tell: stay silent rather than cry wolf

    if not hookspath:
        findings.append({
            "check": "secrets-gate-unwired",
            "path": ".git/config",
            "message": (".githooks/pre-commit exists but core.hooksPath is unset, so the "
                        "credential gate never runs — a commit carrying a credential would "
                        "not be stopped, and nothing would say so"),
            "proposed_fix": "git -C <vault> config core.hooksPath .githooks",
        })
        return

    live = vault / hookspath / "pre-commit"
    if not live.exists():
        findings.append({
            "check": "secrets-gate-unwired",
            "path": f"{hookspath}/pre-commit",
            "message": (f"core.hooksPath is {hookspath} but the credential gate is not there, "
                        f"so it never runs"),
            "proposed_fix": "re-run the plugin install.sh to restore the git hooks",
        })
    elif not (live.stat().st_mode & 0o111):
        findings.append({
            "check": "secrets-gate-unwired",
            "path": f"{hookspath}/pre-commit",
            "message": ("the credential gate is present but not executable, so git skips it "
                        "in silence"),
            "proposed_fix": f"chmod +x {hookspath}/pre-commit",
        })


# ---------------------------------------------------------------------------- runbooks
RUNBOOK_MIN_CHARS = 25
RUNBOOK_NEAR_RATIO = 0.9
_BULLET = re.compile(r"^(?:>\s*)*(?:[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)")


def normalise_runbook_line(raw: str):
    """The comparable form of a runbook line, or None when it should be ignored.

    Headings, code-fence markers and short lines carry no promotable content; bullets,
    numbering and blockquote markers are formatting, not meaning, so they are stripped
    before comparison. Case is kept -- commands and env vars are case-sensitive.
    """
    line = raw.strip()
    if not line or line.startswith("#") or line.startswith("```") or line.startswith("~~~"):
        return None
    line = _BULLET.sub("", line)
    line = re.sub(r"\s+", " ", line).strip()
    if len(line) < RUNBOOK_MIN_CHARS or set(line) <= set("|-: "):
        return None
    return line


def find_runbooks(vault: Path) -> list:
    projects = vault / "Projects"
    if not projects.is_dir():
        return []
    return sorted(p for p in projects.rglob("runbook.md") if p.is_file())


def runbook_clusters(vault: Path, runbooks: list) -> list:
    """Clusters of identical or near-identical lines found in >= 2 different runbooks.

    Read-only. Each cluster: {"text", "locations": [{"path", "line"}]}, sorted by
    first location. Identical normalised lines group first; distinct texts are then
    joined when difflib's ratio reaches RUNBOOK_NEAR_RATIO (union-find, so a chain of
    near-matches forms one cluster and is reported once).
    """
    import difflib
    by_text = {}                                     # normalised text -> [(rel, lineno)]
    for rb in runbooks:
        rel = str(rb.relative_to(vault))
        try:
            lines = rb.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for n, raw in enumerate(lines, 1):
            norm = normalise_runbook_line(raw)
            if norm is not None:
                by_text.setdefault(norm, []).append((rel, n))

    texts = sorted(by_text)
    parent = list(range(len(texts)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # Near-duplicates: compare only across different runbooks, and only texts whose
    # lengths could reach the ratio (ratio <= 2*min/(a+b)), so the pass stays cheap.
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    files = [{p for p, _ in by_text[t]} for t in texts]
    for a_pos, i in enumerate(order):
        a = texts[i]
        sm = difflib.SequenceMatcher(None, autojunk=False)
        sm.set_seq2(a)
        for j in order[a_pos + 1:]:
            b = texts[j]
            if 2 * len(a) / (len(a) + len(b)) < RUNBOOK_NEAR_RATIO:
                break
            if files[i] == files[j] and len(files[i]) == 1:
                continue                             # same single runbook: not drift
            sm.set_seq1(b)
            if (sm.real_quick_ratio() >= RUNBOOK_NEAR_RATIO
                    and sm.quick_ratio() >= RUNBOOK_NEAR_RATIO
                    and sm.ratio() >= RUNBOOK_NEAR_RATIO):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    groups = {}
    for i, t in enumerate(texts):
        groups.setdefault(find(i), []).append(t)
    clusters = []
    for members in groups.values():
        locs = sorted({loc for t in members for loc in by_text[t]})
        if len({p for p, _ in locs}) < 2:
            continue
        rep = max(members, key=lambda t: len(by_text[t]))
        clusters.append({"text": rep,
                         "locations": [{"path": p, "line": n} for p, n in locs]})
    clusters.sort(key=lambda c: (c["locations"][0]["path"], c["locations"][0]["line"]))
    return clusters


def run_runbooks(vault: Path, as_json: bool):
    runbooks = find_runbooks(vault)
    findings = []
    if len(runbooks) >= 2:
        for c in runbook_clusters(vault, runbooks):
            first = c["locations"][0]
            where = ", ".join(f"{l['path']}:{l['line']}" for l in c["locations"])
            findings.append({
                "kind": "runbook-duplicate",
                "path": first["path"],
                "line": first["line"],
                "message": f"repeated in {len({l['path'] for l in c['locations']})} runbooks: {where}",
                "text": c["text"],
                "locations": c["locations"],
            })
    if as_json:
        print(json.dumps({
            "version": 1, "vault": str(vault), "runbooks": len(runbooks),
            **({"note": "nothing to compare"} if len(runbooks) < 2 else {}),
            "findings": findings,
            "counts": {"runbook-duplicate": len(findings)} if findings else {},
        }, indent=2))
        sys.exit(1 if findings else 0)
    if len(runbooks) < 2:
        print(f"Fewer than 2 runbooks found ({len(runbooks)}) — nothing to compare.")
        sys.exit(0)
    if not findings:
        print(f"✓ No content duplicated across {len(runbooks)} runbooks.")
        sys.exit(0)
    print(f"Found {len(findings)} duplicated-content cluster(s) across {len(runbooks)} runbooks:\n")
    for f in findings:
        print(f"[runbook-duplicate] {f['path']}:{f['line']}")
        print(f"  {f['text']}")
        for l in f["locations"]:
            print(f"    - {l['path']}:{l['line']}")
        print()
    sys.exit(1)


# ---------------------------------------------------------------------------------------
# 0.18.0 -- ADR expiry, bundled concepts, decision candidates, memory entities.
# All four file into the review queue (--queue) like every other check; none writes
# anything else. Their finding shape is the shared one, so write_queue, --json, the
# tick carry-over and lint-declines.md suppression need nothing new.
# ---------------------------------------------------------------------------------------

_ADR_MOD = []


def _plugin_adr():
    """The RELEASE's own gt_adr (templates/tools), for its ADR parser -- or None.

    The plugin copy, not the vault's: the vault's tools may predate the parser, and two
    parsers would drift (adr-expires, gt_brief and `gt_adr.py lineage` must read the same
    fields the same way). Loaded under a private name with sys.modules restored, because
    check_generated_hand_edited imports the VAULT's gt_spool/gt_adr by their real names
    and must keep getting those.
    """
    if _ADR_MOD:
        return _ADR_MOD[0]
    import importlib.util
    tools = Path(__file__).resolve().parent.parent / "templates" / "tools"
    saved = {k: sys.modules.pop(k) for k in ("gt_spool", "gt_adr") if k in sys.modules}
    sys.path.insert(0, str(tools))
    mod = None
    try:
        spec = importlib.util.spec_from_file_location("_gt_lint_adr", str(tools / "gt_adr.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        mod = None
    finally:
        try:
            sys.path.remove(str(tools))
        except ValueError:
            pass
        sys.modules.pop("gt_spool", None)
        sys.modules.pop("gt_adr", None)
        sys.modules.update(saved)
    _ADR_MOD.append(mod)
    return mod


def check_adr_expires(vault: Path, findings: list, suppressed: set, today: dt.date = None):
    """ADRs whose truth has a declared end (check: adr-expires).

    The ADR twin of the wiki's `expiry-declared`, with the same rule: a prose condition
    (`Expires when:`) is surfaced at ANY age, because no tool can tell whether "until the
    SDK is upgraded" has happened -- only a person can; a date (`Expires:`) is surfaced
    only once it has passed. The condition text goes in the message, so the queue entry
    can be judged without opening the file.

    An ADR another ADR already supersedes is skipped: the replacement IS the answer to
    "has this expired", and listing it would ask a question already settled.
    """
    A = _plugin_adr()
    if A is None:
        return
    today = today or dt.date.today()
    for dec in decisions_files(vault):
        rel = str(dec.relative_to(vault))
        if is_suppressed(suppressed, rel, dec.name):
            continue
        try:
            adrs = A.parse_adrs(dec.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        replaced = A.superseded_by(adrs)
        for adr in adrs:
            f, n = adr["fields"], adr["n"]
            if n in replaced or is_suppressed(suppressed, rel, dec.name, f"ADR-{n}"):
                continue
            cond, when = f.get("expires when"), f.get("expires")
            if cond:
                msg = f"ADR-{n} ({adr['title']}) expires when: {cond}"
            elif when:
                try:
                    due = dt.date.fromisoformat(when.strip()[:10])
                except ValueError:
                    msg = f"ADR-{n} ({adr['title']}) has an unreadable Expires date: {when}"
                else:
                    if due >= today:
                        continue
                    msg = f"ADR-{n} ({adr['title']}) expired on {due.isoformat()}"
            else:
                continue
            findings.append({
                "check": "adr-expires",
                "path": rel,
                "message": msg,
                "proposed_fix": (f"Decide whether it still holds. If not, write the replacement "
                                 f"with `gt_adr.py --vault \"<vault>\" allocate {project_key(vault, dec)} --supersedes "
                                 f"{n}`; if it does, add `suppress: {rel}:ADR-{n}` to "
                                 "lint-declines.md (or move the date on in a superseding ADR)."),
            })


_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "and", "for", "with", "what", "how", "this", "that", "from", "into", "are",
         "its", "why", "when", "where", "not", "one", "two", "use", "using", "about", "page",
         "notes", "note", "overview", "see", "also", "more", "other", "our", "your"}


def _keywords(text: str) -> set:
    return {w for w in _WORD.findall(text.lower()) if len(w) >= 3 and w not in _STOP}


def _shares_keyword(heading: str, keys: set) -> bool:
    """`Running migrations` shares `migration`: a prefix match either way, on words of at
    least four letters, so plurals and -ing forms count without a stemmer."""
    for w in _keywords(heading):
        for k in keys:
            if w == k or (min(len(w), len(k)) >= 4 and (w.startswith(k) or k.startswith(w))):
                return True
    return False


BUNDLED_MIN_HEADINGS = 4
# A page is coherent when at least this many of its `## ` sections are on its title/tags.
# The request fixed "fewer than 2 share -> bundled" and "3 or more share -> coherent" and
# left exactly two undecided. Two is BUNDLED: a page about two topics, each named once in
# its title, has exactly two on-topic sections and the rest wandering -- which is the
# request's own worked example ("Running migrations" and "Testing migrations" both match
# "migration", and it must fire).
BUNDLED_COHERENT_AT = 3


def check_bundled_concept(vault: Path, findings: list, suppressed: set):
    """A Knowledge page that has grown into several topics (check: bundled-concept).

    CONVENTIONS: one concept per page. Fires when the page has 4+ distinct `## `
    headings and at most 2 of them share a keyword with its title or tags. `decision`
    pages are exempt -- an ADR-shaped page legitimately has many headings. The check
    never suggests the split; that is a human judgement.
    """
    for page in all_knowledge_pages(vault):
        if page.name.startswith("_"):
            continue
        rel = f"Knowledge/{page.name}"
        if is_suppressed(suppressed, rel, page.name):
            continue
        try:
            text = page.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = parse_frontmatter_map(text)
        if fm.get("category", "").strip().lower() == "decision":
            continue
        heads = []
        for line in strip_code(text).splitlines():
            m = re.match(r"^##\s+(.+?)\s*#*\s*$", line)
            if m and m.group(1) not in heads:
                heads.append(m.group(1))
        if len(heads) < BUNDLED_MIN_HEADINGS:
            continue
        keys = _keywords(fm.get("title") or page.stem)
        for tag in parse_frontmatter_field(text, "tags"):
            keys |= _keywords(tag)
        on_topic = sum(1 for h in heads if _shares_keyword(h, keys))
        if on_topic >= BUNDLED_COHERENT_AT:
            continue
        findings.append({
            "check": "bundled-concept",
            "path": rel,
            "message": (f"This page covers multiple topics. Consider splitting. {len(heads)} "
                        f"sections, {on_topic} on its title/tags: " + " · ".join(heads)),
            "proposed_fix": ("Split the off-topic sections into their own pages and link them; "
                             f"if it really is one concept, add `suppress: {rel}` to "
                             "lint-declines.md."),
        })


DEFAULT_DECISION_SIGNALS = (
    "we chose", "we decided", "this is intentional", "don't change this", "do not change this",
    "deliberately", "by design", "we use ... instead of", "this workaround",
    "existing behavior is correct", "existing behaviour is correct", "trade-off we accepted",
)


def decision_signals() -> list:
    """The phrase list in effect: the defaults, edited by the gt setting `decision_signals`.

    `default` (the setting's default) = the list above. `off` = no phrases, the check is
    silent. Anything else is `;`-separated edits applied to the defaults: `+phrase` (or a
    bare `phrase`) adds, `-phrase` removes -- e.g. `+we went with;-deliberately`. `...` in
    a phrase matches up to 60 characters of anything (`we use ... instead of`)."""
    raw = "default"
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gt_settings
        raw = gt_settings.get("decision_signals") or "default"
    except Exception:
        pass
    raw = raw.strip().lower()
    if raw == "off":
        return []
    phrases = list(DEFAULT_DECISION_SIGNALS)
    for part in raw.split(";"):
        part = part.strip()
        if not part or part == "default":
            continue
        if part.startswith("-"):
            phrases = [p for p in phrases if p != part[1:].strip()]
        else:
            add = part.lstrip("+").strip()
            if add and add not in phrases:
                phrases.append(add)
    return phrases


def _signal_regex(phrase: str):
    return re.compile(r"\s*".join(r".{1,60}?" if w == "..." else re.escape(w)
                                 for w in phrase.split()), re.I)


def line_hash(line: str) -> str:
    """The id a decision-candidate suppression uses: stable while the text is."""
    return hashlib.sha256(" ".join(line.lower().split()).encode("utf-8")).hexdigest()[:8]


def _sentence(line: str, start: int) -> str:
    """The sentence of `line` that contains offset `start`, markdown stripped."""
    lo = max(line.rfind(". ", 0, start), line.rfind("! ", 0, start), line.rfind("? ", 0, start))
    lo = 0 if lo < 0 else lo + 2
    hi = len(line)
    for mark in (". ", "! ", "? "):
        i = line.find(mark, start)
        if i >= 0:
            hi = min(hi, i + 1)
    s = re.sub(r"^\s*(?:[-*]\s+|\d+\.\s+|>\s*)", "", line[lo:hi])
    return re.sub(r"[*_`]", "", s).strip()


def check_decision_candidates(vault: Path, findings: list, suppressed: set):
    """Decisions made in prose and never written as an ADR (check: decision-candidate).

    Scans every project's design.md and research.md, line by line, for a decision-signal
    phrase (decision_signals()). Headings, fenced code and HTML comments are skipped: a
    heading names a topic, the prose under it is where the choice is stated. Each hit is
    a CANDIDATE in the review queue -- with the line number, the phrase, the sentence and
    a proposed ADR title -- and nothing more: converting it is `gt_adr.py allocate`,
    declining it is a `suppress:` line. No ADR is ever written by this check.
    """
    phrases = decision_signals()
    if not phrases:
        return
    pats = [(p, _signal_regex(p)) for p in phrases]
    projects = vault / "Projects"
    if not projects.is_dir():
        return
    files = sorted(p for p in projects.rglob("*.md")
                   if p.name in ("design.md", "research.md")
                   and "spool" not in p.relative_to(projects).parts)
    for path in files:
        rel = str(path.relative_to(vault))
        if is_suppressed(suppressed, rel, path.name):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        fenced = in_comment = False
        for no, raw in enumerate(lines, 1):
            s = raw.strip()
            if s.startswith(("```", "~~~")):
                fenced = not fenced
                continue
            if fenced:
                continue
            if "<!--" in s and "-->" not in s.split("<!--", 1)[1]:
                in_comment = True
                continue
            if in_comment:
                in_comment = "-->" not in s
                continue
            if not s or s.startswith("#") or s.startswith("<!--"):
                continue
            text = raw.replace("’", "'")
            for phrase, rx in pats:
                m = rx.search(text)
                if not m:
                    continue
                if is_suppressed(suppressed, rel, path.name, f"L{no}") or \
                        is_suppressed(suppressed, rel, path.name, f"#{line_hash(raw)}"):
                    break
                sent = _sentence(text, m.start())
                title = sent[:1].upper() + sent[1:]
                title = title.rstrip(".!? ")
                if len(title) > 72:
                    title = title[:71].rstrip() + "…"
                clip = sent if len(sent) <= 200 else sent[:199] + "…"
                findings.append({
                    "check": "decision-candidate",
                    "path": rel,
                    "line": no,
                    "message": f"line {no}: \"{phrase}\" — {clip}",
                    "proposed_fix": (f"If it is a decision, record it: `gt_adr.py --vault \"<vault>\" allocate "
                                     f"{project_key(vault, path)} --title \"{title}\"`. If not, "
                                     f"add `suppress: {rel}:#{line_hash(raw)}` to "
                                     "lint-declines.md."),
                })
                break                       # one finding per line, whatever else matches


# ALLCAPS words that are vocabulary, not things: counting them would ask every memory file
# to declare "TODO" as an entity.
_CAPS_STOP = {"TODO", "NOTE", "README", "MEMORY", "CLAUDE", "FIXME", "WARNING", "IMPORTANT",
              "NEVER", "ALWAYS", "MUST", "AND", "THE", "NOT", "FOR", "YES", "UTC", "ADR",
              "URL", "JSON", "YAML", "HTML", "HTTP", "HTTPS", "PDF", "CSS", "CLI", "API",
              "SSH", "DONE", "WHY", "HOW", "WHAT", "ONE", "TWO", "OFF", "NEW", "OLD", "ALL",
              # emphasis, measured on a real vault: shouted words, not things
              "WRONG", "RIGHT", "ONLY", "KEPT", "BEFORE", "AFTER", "DONT", "STOP", "BOTH",
              "EVERY", "ANY", "NONE", "SAME", "EXACTLY", "NOW", "THEN", "THIS", "THAT",
              "BUT", "WITH", "WITHOUT", "FAIL", "FAILED", "PASS", "PASSED", "TRUE", "FALSE",
              "LIVE", "REAL", "STILL", "ALSO", "MORE", "LESS", "NEXT", "LAST", "FIRST"}
# A backticked token counts only when it is shaped like a NAME -- a hyphen, an underscore
# or a digit inside, or CamelCase -- and not a file or a path. `accounts` is a word and
# `eval_regen.py` is a file; both were most of the noise on a real vault.
_IDENT = re.compile(r"^(?=.*(?:[-_0-9]|[a-z][A-Z]))[A-Za-z][A-Za-z0-9_-]{2,39}$")


def check_memory_entity_orphan(vault: Path, findings: list, suppressed: set,
                               threshold: int = 3):
    """A memory file about something it does not declare (check: memory-entity-orphan).

    Memory files may list `entities:` in frontmatter so `/gt:gt-query --entity <name>`
    (gt_entities.py) loads only the files about that thing. This suggests the field where
    a name appears `threshold`+ times in the body and is not in the file's `entities:`:
    a name some memory file in the vault already declares, an ALLCAPS identifier, or a
    backticked identifier. A suggestion only -- nothing is tagged without the user.
    Suppress one name with `suppress: <path>:<name>`.

    ADOPTION GATE. The shape-based guesses (ALLCAPS, backticked) run only in a project
    where at least one memory file already declares `entities:`. Measured on the owner's
    vault on 2026-10-01, before anything declared the field: 129 of its memory files
    would each have been a queue entry on the first run -- a flood that buries every
    other finding the day the check ships. Declared names are suggested everywhere,
    because a name someone has declared is evidence, not a guess.
    """
    projects = vault / "Projects"
    if not projects.is_dir():
        return
    files = sorted(p for p in projects.rglob("*.md")
                   if p.parent.name == "memory" and p.name != "MEMORY.md"
                   and "spool" not in p.relative_to(projects).parts)
    texts, known, adopted = {}, {}, set()
    for f in files:
        try:
            texts[f] = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for e in parse_frontmatter_field(texts[f], "entities"):
            known.setdefault(e.lower(), e)
            adopted.add(f.parent.parent)
    for f, text in texts.items():
        rel = str(f.relative_to(vault))
        if is_suppressed(suppressed, rel, f.name):
            continue
        declared = {e.lower() for e in parse_frontmatter_field(text, "entities")}
        body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S)
        body = re.sub(r"```.*?```", " ", body, flags=re.S)
        counts = {}
        for low, name in known.items():
            n = len(re.findall(r"(?<![\w-])" + re.escape(low) + r"(?![\w-])", body.lower()))
            if n:
                counts[name] = n
        guess = f.parent.parent in adopted
        for tok in (re.findall(r"\b[A-Z][A-Z0-9]{2,}\b", body) if guess else ()):
            if tok not in _CAPS_STOP and tok.lower() not in known:
                counts[tok] = counts.get(tok, 0) + 1
        for tok in (re.findall(r"`([^`\s]{3,40})`", body) if guess else ()):
            if _IDENT.match(tok) and tok.lower() not in known and not tok.isupper():
                counts[tok] = counts.get(tok, 0) + 1
        hits = sorted(((n, name) for name, n in counts.items()
                       if n >= threshold and name.lower() not in declared
                       and not is_suppressed(suppressed, rel, f.name, name)),
                      key=lambda x: (-x[0], x[1].lower()))[:5]
        if not hits:
            continue
        names = [name for _, name in hits]
        findings.append({
            "check": "memory-entity-orphan",
            "path": rel,
            "message": "mentions " + ", ".join(f"{name} ({n}×)" for n, name in hits)
                       + (" but declares no `entities:`" if not declared
                          else " not listed in its `entities:`"),
            "proposed_fix": (f"Add `entities: [{', '.join(names)}]` to its frontmatter so "
                             "`/gt:gt-query --entity` finds it, or suppress a name with "
                             f"`suppress: {rel}:<name>`."),
        })


def main():
    parser = argparse.ArgumentParser(description="Golden Thread vault health checker")
    parser.add_argument("vault", type=Path, nargs="?", default=None, help="Path to the vault root")
    parser.add_argument("--vault", dest="vault_opt", type=Path, default=None,
                        help="Path to the vault root (same as the positional argument)")
    parser.add_argument("--queue", type=Path, default=None,
                        help="Write review checklist to this file (rewritten on every run, "
                             "including a clean one; not allowed with --runbooks)")
    parser.add_argument("--json", action="store_true",
                        help="Emit findings as JSON instead of text (exit codes unchanged)")
    parser.add_argument("--runbooks", action="store_true",
                        help="Only detect content duplicated across Projects/**/runbook.md "
                             "(read-only, so it cannot be combined with --queue)")
    args = parser.parse_args()

    if args.vault is None and args.vault_opt is None:
        parser.error("a vault path is required (positional or --vault)")
    if args.vault is not None and args.vault_opt is not None \
            and args.vault.resolve() != args.vault_opt.resolve():
        parser.error("positional vault and --vault disagree")
    # --queue used to be read only AFTER the --runbooks branch had already exited: no
    # file, no warning, exit 0, and neither flag's help said they were exclusive. A
    # caller that asked for a worklist and got a silent success is worse off than one
    # that got an error.
    if args.runbooks and args.queue:
        parser.error("--queue cannot be used with --runbooks; --runbooks is read-only "
                     "and writes nothing")
    vault = (args.vault or args.vault_opt).resolve()
    if not vault.exists():
        # Exit 2, not 1: 1 means "the vault was read and these are its findings", so a
        # missing vault exiting 1 told every caller a clean bill of health on a vault
        # nothing had opened.
        print(f"Error: vault not found at {vault}", file=sys.stderr)
        sys.exit(2)

    if args.runbooks:
        run_runbooks(vault, args.json)

    suppressed = read_suppress_list(vault)
    findings = []

    check_index_gap(vault, findings, suppressed)
    check_broken_links(vault, findings, suppressed)
    check_orphans(vault, findings, suppressed)
    check_memory_unlisted(vault, findings, suppressed)
    check_global_gap(vault, findings, suppressed)
    check_memory_bloat(vault, findings, suppressed)
    check_global_scope_leak(vault, findings, suppressed)
    check_source_todo(vault, findings, suppressed)
    check_frontmatter(vault, findings, suppressed)
    check_core_rules(vault, findings, suppressed)
    check_project_refs(vault, findings, suppressed)
    check_superseded_cited(vault, findings, suppressed)
    check_stale(vault, findings, suppressed)
    check_attribution_wired(vault, findings, suppressed)
    check_secrets_gate_wired(vault, findings, suppressed)
    check_adr_collision(vault, findings, suppressed)
    check_generated_hand_edited(vault, findings, suppressed)
    # 0.18.0 (lint + ADR group): each files into the review queue like every other check.
    check_adr_expires(vault, findings, suppressed)
    check_bundled_concept(vault, findings, suppressed)
    check_decision_candidates(vault, findings, suppressed)
    check_memory_entity_orphan(vault, findings, suppressed)

    if args.queue:
        for note in write_queue(vault, findings, args.queue):
            print(note, file=sys.stderr)

    if args.json:
        counts = {}
        for f in findings:
            counts[f["check"]] = counts.get(f["check"], 0) + 1
        print(json.dumps({
            "version": 1,
            "vault": str(vault),
            "findings": [{"kind": f["check"], "path": f["path"], "line": f.get("line"),
                          "message": f["message"], "proposed_fix": f.get("proposed_fix")}
                         for f in findings],
            "counts": counts,
        }, indent=2))
        sys.exit(1 if findings else 0)

    if not findings:
        print("✓ Vault is healthy — no issues found.")
        sys.exit(0)

    # Count by type
    by_check = {}
    for f in findings:
        by_check.setdefault(f["check"], []).append(f)

    sources_count = len(list((vault / "Sources").glob("*.md"))) if (vault / "Sources").exists() else 0
    print(f"Found {len(findings)} issue(s) — checked {len(all_knowledge_pages(vault))} Knowledge pages, {sources_count} sources:\n")
    for check, items in sorted(by_check.items()):
        print(f"  {check}: {len(items)}")
    print()
    for f in findings:
        print(f"[{f['check']}] {f['path']}")
        print(f"  {f['message']}")
        print(f"  Fix: {f['proposed_fix']}")
        print()

    if args.queue:
        print(f"Review queue written to: {args.queue}")

    sys.exit(1)


if __name__ == "__main__":
    main()
