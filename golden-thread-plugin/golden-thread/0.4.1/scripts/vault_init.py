#!/usr/bin/env python3
"""
vault_init.py — Idempotent Golden Thread vault creator.

Modes:
  fresh           --vault <path> --domain <name>
  create-project  --vault <path> --name <slug> [--title <title>] [--tags a,b]
                  [--domain <grouping>]
                  [--parent <parent-slug>] [--runbook] [--project-dir <dir>]
                  [--topology local|remote|bastion-jump|bastion-direct]
                  [--repo-url <url>] [--fleet <page-name>]
  connect         --vault <path>
  install-core-rules --vault <path> [--no-hooks] [--settings <file>]

Exit codes:
  0 = success
  1 = usage error
  3 = conflict (vault-config.json points to a different vault)

Output: JSON array of {"action": created|skipped|updated|conflict|error, "path": ..., "note": ...}
"""
import argparse
import json
import re
import os
import sys
from pathlib import Path

RESULTS = []

SCRIPT_DIR = Path(__file__).parent
TEMPLATES_DIR = SCRIPT_DIR.parent / "templates"

TOPOLOGIES = ("local", "remote", "bastion-jump", "bastion-direct")


def record(action, path, note=""):
    RESULTS.append({"action": action, "path": str(path), "note": note})


def ensure_dir(path: Path):
    path = Path(path)
    if path.exists():
        record("skipped", path, "directory already exists")
    else:
        path.mkdir(parents=True, exist_ok=True)
        record("created", path)


def ensure_file(path: Path, content: str):
    path = Path(path)
    if path.exists():
        record("skipped", path, "file already exists")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        record("created", path)


def seed_template(template_name: str, dest: Path, subs: dict):
    """Read template, apply substitutions, then ensure_file (skip if dest exists)."""
    tmpl_path = TEMPLATES_DIR / template_name
    if not tmpl_path.exists():
        record("error", dest, f"template not found: {tmpl_path}")
        return
    content = tmpl_path.read_text(encoding="utf-8")
    for key, val in subs.items():
        content = content.replace(f"{{{{{key}}}}}", val)
    ensure_file(dest, content)


def write_config(vault_path: Path):
    """Write ~/.claude/vault-config.json. Exit 3 if a different vault is already configured."""
    config_path = Path.home() / ".claude" / "vault-config.json"
    vault_str = str(vault_path.resolve())
    if config_path.exists():
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        if existing.get("vault_path") == vault_str:
            record("skipped", config_path, "already configured with same vault")
            return
        else:
            record("conflict", config_path,
                   f"already points to '{existing.get('vault_path')}' — run with 'connect' mode to switch")
            print(json.dumps(RESULTS, indent=2))
            sys.exit(3)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"vault_path": vault_str}, indent=2), encoding="utf-8")
    record("created", config_path)


def update_claude_md(claude_md_path: Path, section_content: str, section_marker: str = "## Golden Thread"):
    """Append a Golden Thread section to a CLAUDE.md if not already present."""
    claude_md_path = Path(claude_md_path)
    if claude_md_path.exists():
        existing = claude_md_path.read_text(encoding="utf-8")
        # Accept either the new or old section header as "already present"
        if section_marker in existing or "## Memory (Golden Thread)" in existing:
            record("skipped", claude_md_path, "Golden Thread section already present")
            return
        if "## See Also" in existing:
            updated = existing.replace("## See Also", section_content + "\n\n## See Also")
        else:
            updated = existing.rstrip() + "\n\n" + section_content + "\n"
        claude_md_path.write_text(updated, encoding="utf-8")
        record("updated", claude_md_path, "appended Golden Thread memory section")
    else:
        claude_md_path.parent.mkdir(parents=True, exist_ok=True)
        claude_md_path.write_text(section_content + "\n", encoding="utf-8")
        record("created", claude_md_path)


def register_in_master_index(index_path: Path, slug: str, title: str, tags: list,
                             domain: str = None, stage: str = "idea"):
    """Insert a project row into the master index table.

    Appends into the markdown table rather than the end of the file, so the row
    lands in the list instead of after whatever section happens to be last.
    """
    row = f"| [{title}]({slug}/) | `{slug}` | {domain or 'TODO'} | {stage} | <!-- one-line description --> |"

    if not index_path.exists():
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            "# Projects\n\n## All projects\n\n"
            "| Project | Slug | Domain | Stage | What it is |\n|---|---|---|---|---|\n"
            f"{row}\n", encoding="utf-8")
        record("created", index_path)
        return

    existing = index_path.read_text(encoding="utf-8")
    if f"]({slug}/)" in existing or f"`{slug}`" in existing:
        record("skipped", index_path, f"{slug} already listed")
        return

    lines = existing.splitlines()
    sep = next((i for i, l in enumerate(lines)
                if re.match(r"^\|[\s-]*\|[\s-]*\|", l) and "Slug" in lines[i - 1]), None)
    if sep is None:
        # No table found — fall back to appending, but keep it visible.
        lines.append(row)
    else:
        end = sep + 1
        while end < len(lines) and lines[end].startswith("|"):
            end += 1
        lines.insert(end, row)
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    record("updated", index_path, f"registered {slug}")


def install_core_rules(vault: Path, wire_hooks: bool = True, settings_path: Path = None):
    """Establish the Core-rule tier: copy the module in, then WIRE THE HOOKS.

    Copying the files is not enough. Per core_rule_priority_model.md, a Core rule is
    only as durable as the mechanism that re-asserts it — an unwired module is exactly
    the failure mode this tier exists to prevent. So the wiring is part of init, not a
    follow-up step.
    """
    src = TEMPLATES_DIR / "core-rules"
    if not src.exists():
        record("error", src, "core-rules template missing from the plugin")
        return

    dest = vault / "Projects" / "golden-thread" / "core-rules"
    ensure_dir(dest)
    ensure_dir(dest / "hooks")
    for f in sorted(src.glob("*.md")):
        ensure_file(dest / f.name, f.read_text(encoding="utf-8"))
    for f in sorted((src / "hooks").glob("*.sh")):
        target = dest / "hooks" / f.name
        ensure_file(target, f.read_text(encoding="utf-8"))
        try:
            target.chmod(0o755)
        except OSError:
            pass

    if not wire_hooks:
        return

    # Core = user-global scope. Wiring per-project would scope these to one project,
    # which is the Context tier, not Core.
    settings = settings_path or (Path.home() / ".claude" / "settings.json")
    hooks_dir = dest / "hooks"
    wanted = {
        "UserPromptSubmit": str(hooks_dir / "inject_core_rules.sh"),
        "Stop": str(hooks_dir / "validate_response.sh"),
    }

    try:
        data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    except (json.JSONDecodeError, OSError) as exc:
        record("error", settings, f"could not read settings.json: {exc}")
        return

    data.setdefault("hooks", {})
    changed = False
    for event, cmd in wanted.items():
        blocks = data["hooks"].setdefault(event, [])
        already = any(
            h.get("command") == cmd
            for b in blocks if isinstance(b, dict)
            for h in b.get("hooks", [])
        )
        if already:
            record("skipped", settings, f"{event} hook already wired")
            continue
        blocks.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
        changed = True

    if changed:
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        record("updated", settings, "wired Core-rule enforcement hooks (UserPromptSubmit + Stop)")


def cmd_fresh(vault: Path, domain: str):
    vault = vault.resolve()
    write_config(vault)

    ensure_dir(vault)
    ensure_dir(vault / "Knowledge")
    ensure_dir(vault / "Sources")
    ensure_dir(vault / "global-memory")
    ensure_dir(vault / "Projects")

    seed_template("wiki-CLAUDE.md", vault / "CLAUDE.md", {"DOMAIN": domain})

    ensure_file(vault / "index.md", f"# {domain} Knowledge Index\n\n<!-- Add entries: [[Page Title]] — one-line description -->\n")
    ensure_file(vault / "log.md", "# Golden Thread Activity Log\n\n<!-- Format: YYYY-MM-DD [verb] description -->\n")
    ensure_file(vault / "review-queue.md", "# Review Queue\n\n<!-- Items flagged for owner review -->\n")
    ensure_file(vault / "global-memory" / "MEMORY.md", "# Global Memory Index\n\n<!-- One entry per file: - [Title](filename.md) — description -->\n")

    seed_template("CONVENTIONS.md", vault / "Projects" / "CONVENTIONS.md", {"DOMAIN": domain})
    seed_template("PROTOCOL.md", vault / "Projects" / "PROTOCOL.md", {"DOMAIN": domain})
    seed_template("INFRASTRUCTURE.md", vault / "Projects" / "INFRASTRUCTURE.md", {"DOMAIN": domain})
    seed_template("PROJECTS-README.md", vault / "Projects" / "README.md", {"DOMAIN": domain})
    seed_template("lint-declines.md", vault / "lint-declines.md", {"DOMAIN": domain})

    install_core_rules(vault)

    global_claude = Path.home() / ".claude" / "CLAUDE.md"
    gt_section = f"""## Golden Thread

Timestamps: Begin every response with the current date and time from the system context (injected as "Current date and time: ..." at the start of each prompt).

Scope rule: `global-memory/` contains only facts needed in ALL projects. Project-specific facts belong in `Projects/<slug>/memory/`, not here.

Load the memory index only when explicitly asked, or when a `/gt:*` skill is invoked:
`{vault}/global-memory/MEMORY.md`

Platform wiki:
`{vault}/index.md` → follow links into `Knowledge/`
"""
    update_claude_md(global_claude, gt_section)


def cmd_create_project(vault: Path, slug: str, title: str = None, tags: list = None,
                       parent: str = None, runbook: bool = False, project_dir=None,
                       topology: str = None, repo_url: str = None, fleet: str = None,
                       domain: str = None):
    vault = vault.resolve()
    display_title = title or slug
    tags = tags or []

    if parent:
        proj = vault / "Projects" / parent / slug
        master_index = vault / "Projects" / parent / "README.md"
    else:
        proj = vault / "Projects" / slug
        master_index = vault / "Projects" / "README.md"

    ensure_dir(proj)
    ensure_dir(proj / "memory")
    ensure_file(proj / "memory" / "MEMORY.md",
                f"# {display_title} Memory Index\n\n<!-- One entry per file: - [Title](filename.md) — description -->\n")
    ensure_file(proj / "decisions.md",
                f"# {display_title} Decisions\n\n<!-- Append-only ADRs. Format: ## ADR-N: Title -->\n")
    ensure_file(proj / "research.md",
                f"# {display_title} Research\n\n<!-- Append-only findings. Format: ## YYYY-MM-DD: Title -->\n")
    ensure_file(proj / "design.md",
                f"# {display_title} Design\n\n<!-- Iteratively updated. Keep current — put history in research.md -->\n")
    ensure_file(proj / "idea.md",
                f"# {display_title}\n\n<!-- Original brain dump — immutable after creation -->\n")

    # source.md — where the code lives and how it is deployed.
    topology = topology or "TODO"
    if fleet:
        fleet_str = fleet if fleet.startswith("[[") else f"[[{fleet}]]"
    elif topology.startswith("bastion"):
        fleet_str = "[[INFRASTRUCTURE]]"
    else:
        fleet_str = "n/a"
    seed_template("source.md", proj / "source.md", {
        "TITLE": display_title,
        "TOPOLOGY": topology,
        "REPO_URL": repo_url or "TODO",
        "FLEET": fleet_str,
    })

    tags_str = ", ".join(tags) if tags else ""
    # Frontmatter drives Obsidian's tag pane / properties and the Dataview views in
    # Projects/README.md. Categorisation lives here rather than in folder nesting,
    # because a second level under Projects/ silently disables gt_lint's
    # memory-unlisted check (single-level iterdir).
    fm_tags = list(tags)
    if domain and domain not in fm_tags:
        fm_tags.insert(0, domain)
    frontmatter = (
        "---\n"
        "type: project\n"
        f"slug: {slug}\n"
        f"domain: {domain or 'TODO'}\n"
        "stage: idea\n"
        f"topology: {topology}\n"
        f"tags: [{', '.join(fm_tags)}]\n"
        + (f"parent: {parent}\n" if parent else "")
        + "---\n\n"
    )
    readme_content = frontmatter + f"""# {display_title}

> <!-- one-line vision -->

## Status

| Sub-project | Phase | Next action |
|---|---|---|

## Stage
idea

## Source
{topology} — see [source.md](source.md)

## Tags
{tags_str}

## Related
"""
    ensure_file(proj / "README.md", readme_content)

    if runbook:
        ensure_file(proj / "runbook.md",
                    f"# {display_title} Runbook\n\n<!-- Operational procedures. Project-specific facts only — process rules go in PROTOCOL.md -->\n")

    register_in_master_index(master_index, slug, display_title, tags, domain)

    global_claude = Path.home() / ".claude" / "CLAUDE.md"
    gt_section = f"""## Golden Thread

Timestamps: Begin every response with the current date and time from the system context (injected as "Current date and time: ..." at the start of each prompt).

Scope rule: `global-memory/` contains only facts needed in ALL projects. Project-specific facts belong in `Projects/<slug>/memory/`, not here.

Load the memory index only when explicitly asked, or when a `/gt:*` skill is invoked:
`{vault}/global-memory/MEMORY.md`

Platform wiki:
`{vault}/index.md` → follow links into `Knowledge/`
"""
    update_claude_md(global_claude, gt_section)

    if project_dir:
        project_dir = Path(project_dir).resolve()
        proj_claude = project_dir / "CLAUDE.md"
        proj_section = f"""## Golden Thread — {slug}

Scope rule: write project-specific facts to `Projects/{slug}/` only. For cross-project or platform facts, use `/gt:gt-promote`.

Load the project memory index only when explicitly asked, or when a `/gt:*` skill is invoked:
`{vault}/Projects/{slug}/memory/MEMORY.md`

Platform wiki:
`{vault}/index.md` → follow links into `Knowledge/`
"""
        update_claude_md(proj_claude, proj_section)


def cmd_connect(vault: Path):
    vault = vault.resolve()
    if not vault.exists():
        record("error", vault, "vault directory does not exist")
        print(json.dumps(RESULTS, indent=2))
        sys.exit(1)
    write_config(vault)


def main():
    parser = argparse.ArgumentParser(description="Golden Thread vault initializer")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_fresh = sub.add_parser("fresh", help="Create a new vault scaffold")
    p_fresh.add_argument("--vault", required=True, type=Path)
    p_fresh.add_argument("--domain", required=True)

    p_proj = sub.add_parser("create-project", help="Scaffold a new project in an existing vault")
    p_proj.add_argument("--vault", required=True, type=Path)
    p_proj.add_argument("--name", required=True)
    p_proj.add_argument("--title", default=None, help="Human-readable project title")
    p_proj.add_argument("--tags", default=None, help="Comma-separated tags, e.g. platform,infra")
    p_proj.add_argument("--parent", default=None, help="Parent project slug (creates sub-project)")
    p_proj.add_argument("--runbook", action="store_true", help="Create runbook.md skeleton")
    p_proj.add_argument("--project-dir", type=Path, default=None)
    p_proj.add_argument("--topology", choices=TOPOLOGIES, default=None,
                        help="Where the code lives: local, remote, bastion-jump, bastion-direct")
    p_proj.add_argument("--repo-url", default=None, help="Git remote URL for this project")
    p_proj.add_argument("--domain", default=None,
                        help="Top-level grouping for the project (see CONVENTIONS.md taxonomy)")
    p_proj.add_argument("--fleet", default=None,
                        help="Page name of the shared fleet definition (default: INFRASTRUCTURE "
                             "for bastion topologies)")

    p_conn = sub.add_parser("connect", help="Point vault-config.json at an existing vault")
    p_conn.add_argument("--vault", required=True, type=Path)

    p_core = sub.add_parser("install-core-rules",
                            help="Establish the Core-rule tier in an existing vault and wire the hooks")
    p_core.add_argument("--vault", required=True, type=Path)
    p_core.add_argument("--no-hooks", action="store_true",
                        help="Copy the module but do not touch settings.json")
    p_core.add_argument("--settings", type=Path, default=None,
                        help="Settings file to wire (default: ~/.claude/settings.json)")

    args = parser.parse_args()

    if args.mode == "fresh":
        cmd_fresh(args.vault, args.domain)
    elif args.mode == "create-project":
        tags = [t.strip() for t in args.tags.split(",")] if args.tags else []
        cmd_create_project(args.vault, args.name, args.title, tags, args.parent, args.runbook,
                           args.project_dir, args.topology, args.repo_url, args.fleet,
                           args.domain)
    elif args.mode == "connect":
        cmd_connect(args.vault)
    elif args.mode == "install-core-rules":
        install_core_rules(args.vault.resolve(), wire_hooks=not args.no_hooks,
                           settings_path=args.settings)

    print(json.dumps(RESULTS, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
