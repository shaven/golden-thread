#!/usr/bin/env python3
"""
gt_lint.py — Audit a Golden Thread vault for structural issues.

Usage:
  python3 gt_lint.py <vault-path> [--queue <output-file>]

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
  core-unenforced      Core rule whose enforcement hook is not actually wired

Suppression: reads <vault>/lint-declines.md — lines starting with "suppress:" are matched
against finding paths.

Exit codes:
  0 = clean
  1 = findings found
"""
import argparse
import json
import re
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


def extract_wikilinks(text: str) -> list:
    return re.findall(r'\[\[([^\]|#]+?)(?:[|#][^\]]*)?\]\]', text)


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


def check_index_gap(vault: Path, findings: list, suppressed: set):
    index = vault / "index.md"
    if not index.exists():
        return
    index_text = index.read_text(encoding="utf-8")
    for page in all_knowledge_pages(vault):
        title = page_title(page)
        rel = f"Knowledge/{page.name}"
        if rel.lower() in suppressed or title.lower() in suppressed:
            continue
        if title not in index_text and page.name not in index_text:
            findings.append({
                "check": "index-gap",
                "path": rel,
                "message": f"Knowledge/{page.name} exists but has no entry in index.md",
                "proposed_fix": f"Add to index.md: `[[{title}]]` — <one-line description>",
            })


def check_broken_links(vault: Path, findings: list, suppressed: set):
    all_pages = {p.stem.lower(): p for p in vault.rglob("*.md")}

    for md_file in vault.rglob("*.md"):
        # Skip lint-declines itself
        if md_file.name == "lint-declines.md":
            continue
        rel = str(md_file.relative_to(vault))
        # A whole file can be suppressed, same as in every other check. Immutable
        # Sources/ files rely on this: their links point at the world as it was.
        # read_suppress_list() lowercases its entries, so compare lowercased.
        if rel.lower() in suppressed or md_file.name.lower() in suppressed:
            continue
        text = md_file.read_text(encoding="utf-8", errors="replace")
        links = extract_wikilinks(text)
        for link in links:
            target = link.strip()
            suppress_key = f"{rel}:[[{target}]]"
            if suppress_key.lower() in suppressed or target.lower() in suppressed:
                continue
            if target.lower() not in all_pages:
                findings.append({
                    "check": "broken-link",
                    "path": rel,
                    "message": f"`[[{target}]]` in {rel} doesn't resolve to any vault page",
                    "proposed_fix": f"Remove the link or create `Knowledge/{target}.md`",
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
        if rel.lower() in suppressed or title.lower() in suppressed:
            continue
        in_index = title in index_text or page.name in index_text
        in_links = title.lower() in all_linked_titles
        if not in_index and not in_links:
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
            if rel.lower() in suppressed or f.name.lower() in suppressed:
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
        if rel.lower() in suppressed or f.name.lower() in suppressed:
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
            suppress_key = f"{rel}:{src_name}"
            if suppress_key.lower() in suppressed or rel.lower() in suppressed:
                continue
            findings.append({
                "check": "superseded-cited",
                "path": rel,
                "message": f"`{rel}` cites `{src}` which has been superseded by `Sources/{new_name}`",
                "proposed_fix": f"Review changes between old and new source, update page content if needed, update sources: to `Sources/{new_name}`",
            })


def check_memory_bloat(vault: Path, findings: list, suppressed: set):
    gm = vault / "global-memory"
    if not gm.exists():
        return
    for f in sorted(gm.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        rel = f"global-memory/{f.name}"
        if rel.lower() in suppressed or f.name.lower() in suppressed:
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
        if rel.lower() in suppressed or f.name.lower() in suppressed:
            continue
        text = f.read_text(encoding="utf-8", errors="replace").lower()
        for slug in sorted(project_slugs):
            suppress_key = f"{rel}:{slug}"
            if suppress_key.lower() in suppressed:
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
        if rel.lower() in suppressed:
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
        if rel.lower() in suppressed:
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
        if rel.lower() in suppressed:
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


def wired_hook_events(settings_path: Path = None) -> set:
    """Which hook events are actually wired. Enforcement lives here, not in the vault."""
    settings = settings_path or (Path.home() / ".claude" / "settings.json")
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except Exception:
        return set()
    events = set()
    for event, blocks in (data.get("hooks") or {}).items():
        for b in blocks or []:
            if isinstance(b, dict) and b.get("hooks"):
                events.add(event)
    return events


def check_core_rules(vault: Path, findings: list, suppressed: set):
    """Audit that Core rules are ENFORCED, not merely stored.

    The whole point of the Core tier is that storing a rule is not enough — the
    2026-08-16 incident had the timestamp rule sitting in global-memory while
    silently not being applied. So this checks the mechanism, not the file.
    """
    core_dir = vault / "Projects" / "golden-thread" / "core-rules"
    events = wired_hook_events()

    for md in sorted(vault.rglob("*.md")):
        if ".git" in md.parts or "templates" in md.parts:
            continue
        rel = str(md.relative_to(vault))
        if rel.lower() in suppressed or md.name.lower() in suppressed:
            continue
        try:
            fm = parse_frontmatter_map(md.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if fm.get("level") != "core":
            continue

        # (i) must live in the canonical folder
        if core_dir not in md.parents:
            findings.append({
                "check": "core-misplaced",
                "path": rel,
                "message": f"{md.name} declares level: core but lives outside core-rules/",
                "proposed_fix": "Move it to Projects/golden-thread/core-rules/, or lower its level",
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

        # (iii) the declared mechanism must actually be wired
        needed = "Stop" if enf == "validated" else "UserPromptSubmit"
        if needed not in events:
            findings.append({
                "check": "core-unenforced",
                "path": rel,
                "message": (f"{md.name} declares enforcement: {enf} but no {needed} hook is wired — "
                            f"the rule is stored, never re-asserted"),
                "proposed_fix": (f"Wire {needed} in ~/.claude/settings.json to "
                                 f"core-rules/hooks/"
                                 + ("validate_response.sh" if enf == "validated" else "inject_core_rules.sh")
                                 + " (or run vault_init.py install-core-rules)"),
            })

    # A core-rules folder with no wiring at all is the headline failure.
    if core_dir.exists() and not ({"UserPromptSubmit", "Stop"} & events):
        rel = "Projects/golden-thread/core-rules"
        if rel.lower() not in suppressed:
            findings.append({
                "check": "core-unenforced",
                "path": rel,
                "message": "core-rules/ exists but neither enforcement hook is wired — the Core tier is inert",
                "proposed_fix": "Run: vault_init.py install-core-rules --vault <vault>",
            })


def write_queue(vault: Path, findings: list, queue_path: Path):
    lines = ["# Vault Review Queue\n\nGenerated by gt_lint.py\n"]
    by_check = {}
    for f in findings:
        by_check.setdefault(f["check"], []).append(f)
    for check, items in sorted(by_check.items()):
        lines.append(f"\n## {check} ({len(items)} items)\n")
        for item in items:
            lines.append(f"- [ ] `{item['path']}` — {item['message']}")
            lines.append(f"  - Fix: {item['proposed_fix']}\n")
    queue_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Golden Thread vault health checker")
    parser.add_argument("vault", type=Path, help="Path to the vault root")
    parser.add_argument("--queue", type=Path, default=None, help="Write review checklist to this file")
    args = parser.parse_args()

    vault = args.vault.resolve()
    if not vault.exists():
        print(f"Error: vault not found at {vault}", file=sys.stderr)
        sys.exit(1)

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
    check_superseded_cited(vault, findings, suppressed)
    check_stale(vault, findings, suppressed)

    if args.queue and findings:
        write_queue(vault, findings, args.queue)

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
