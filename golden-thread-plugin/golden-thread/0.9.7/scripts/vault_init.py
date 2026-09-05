#!/usr/bin/env python3
"""
vault_init.py — Idempotent Golden Thread vault creator.

Modes:
  fresh           --vault <path> --domain <name>
  create-project  --vault <path> --name <slug> [--title <title>] [--tags a,b]
                  [--domain <grouping>]
                  [--parent <parent-slug>] [--project-dir <dir>]
                  [--topology local|remote|bastion-jump|bastion-direct]
                  [--repo-url <url>] [--fleet <page-name>]
  connect         --vault <path>
  install-core-rules --vault <path> [--no-hooks] [--settings <file>]
  rename-project  --vault <path> --from <old-slug> --to <new-slug>\n  merge-project   --vault <path> --from <slug> --into <slug>\n  archive-project --vault <path> --slug <slug> [--reason <text>]

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


ENFORCEMENT_CHECK_MARKER = "## First: is enforcement active?"


def _ensure_enforcement_check(vault: Path):
    """Put the 'is enforcement active?' section into the vault's own CLAUDE.md.

    Lifted verbatim from templates/wiki-CLAUDE.md so the two never drift. Idempotent:
    a vault that already carries the section is left alone.
    """
    claude = vault / "CLAUDE.md"
    tmpl = TEMPLATES_DIR / "wiki-CLAUDE.md"
    if not tmpl.exists():
        record("error", tmpl, "wiki-CLAUDE.md template missing; cannot add enforcement check")
        return
    src = tmpl.read_text(encoding="utf-8")
    if ENFORCEMENT_CHECK_MARKER not in src:
        record("error", tmpl, "template no longer contains the enforcement-check section")
        return
    start = src.index(ENFORCEMENT_CHECK_MARKER)
    end = src.index("## How to Read This", start)
    section = src[start:end]

    if not claude.exists():
        record("skipped", claude, "no vault CLAUDE.md to add the enforcement check to")
        return
    existing = claude.read_text(encoding="utf-8")
    if ENFORCEMENT_CHECK_MARKER in existing:
        record("skipped", claude, "enforcement check already present")
        return

    lines = existing.split("\n")
    # Insert before the first H2 so it is read early; otherwise append.
    idx = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
    lines[idx:idx] = section.rstrip("\n").split("\n") + [""]
    claude.write_text("\n".join(lines), encoding="utf-8")
    record("updated", claude, "added the enforcement-active check")


def install_core_rules(vault: Path, wire_hooks: bool = True, settings_path: Path = None):
    """Establish the Core tier: place the rules, record where they are, wire the hooks.

    The hooks are installed by install.sh to ~/.claude/golden-thread/hooks — outside
    the vault, so the settings.json path survives project renames and vault moves.
    Here we only ensure the RULES exist and that settings points at those stable
    scripts.
    """
    src = TEMPLATES_DIR / "core-rules"
    if not src.exists():
        record("error", src, "core-rules template missing from the plugin")
        return

    # Default location for a fresh vault; an existing one is found wherever it is.
    dest = _resolve_core_rules(vault) or (vault / "Projects" / "golden-thread" / "core-rules")
    ensure_dir(dest)
    for f in sorted(src.glob("*.md")):
        ensure_file(dest / f.name, f.read_text(encoding="utf-8"))

    # Retrofit the enforcement check into the vault's own CLAUDE.md. A vault that
    # predates the Core tier has no way to tell a session that enforcement exists but
    # may be absent — and that gap is silent, because a session without the hooks reads
    # the same rules and simply never has them re-asserted.
    _ensure_enforcement_check(vault)

    # Record the location so nothing has to guess next time. Relative to the vault, so
    # moving the whole vault does not invalidate it either.
    try:
        cfg_path = Path.home() / ".claude" / "vault-config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        rel = str(dest.relative_to(vault))
        if cfg.get("core_rules_path") != rel:
            cfg["core_rules_path"] = rel
            cfg.setdefault("vault_path", str(vault))
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            record("updated", cfg_path, f"core_rules_path = {rel}")
    except Exception as exc:
        record("error", "vault-config.json", f"could not record core_rules_path: {exc}")

    if not wire_hooks:
        return

    hooks_dir = Path.home() / ".claude" / "golden-thread" / "hooks"
    # Every Core/Validated rule with a mechanism needs its mechanism REGISTERED here,
    # not merely copied to disk by install.sh. A hook script that exists but is not
    # wired reads as installed and does nothing -- the same "present but not applied"
    # failure the Core tier exists to close, one level down. 0.9.4 shipped
    # guard_session_claims.sh with no PreToolUse entry and nothing reported it.
    wanted = {
        "UserPromptSubmit": hooks_dir / "inject_core_rules.sh",
        "Stop": hooks_dir / "validate_response.sh",
        "PreToolUse": hooks_dir / "guard_session_claims.sh",
    }
    missing = [str(v) for v in wanted.values() if not v.is_file()]
    if missing:
        record("error", hooks_dir,
               "hook scripts not installed — run install.sh (they ship with the plugin)")
        return

    settings = settings_path or (Path.home() / ".claude" / "settings.json")
    try:
        data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    except (json.JSONDecodeError, OSError) as exc:
        record("error", settings, f"could not read settings.json: {exc}")
        return

    data.setdefault("hooks", {})
    changed = False
    for event, script in wanted.items():
        blocks = data["hooks"].setdefault(event, [])
        # Drop any previous entry that pointed at a vault-internal copy of this hook.
        for b in blocks:
            if isinstance(b, dict):
                b["hooks"] = [h for h in b.get("hooks", [])
                              if not (h.get("command", "").endswith(script.name)
                                      and h.get("command") != str(script))]
        blocks[:] = [b for b in blocks if not (isinstance(b, dict) and not b.get("hooks"))]
        if any(h.get("command") == str(script)
               for b in blocks if isinstance(b, dict) for h in b.get("hooks", [])):
            record("skipped", settings, f"{event} hook already wired")
            continue
        blocks.append({"hooks": [{"type": "command", "command": str(script), "timeout": 10}]})
        changed = True

    if changed:
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        record("updated", settings, "wired Core-rule hooks at the stable path")


def _resolve_core_rules(vault: Path):
    """Find core-rules wherever it currently lives (config, then marker-file search)."""
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from gt_paths import find_core_rules
        return find_core_rules(vault, record=False)
    except Exception:
        for cand in sorted(vault.rglob("core-rules")):
            if (cand / "core_rule_priority_model.md").is_file():
                return cand
        return None


def cmd_rename_project(vault: Path, old: str, new: str):
    """Rename a project and update every reference to it.

    Renames happen — projects get redefined, merged, retired. This makes that a
    supported operation rather than a manual sweep that misses something.
    """
    vault = vault.resolve()
    projects = vault / "Projects"
    matches = [d for d in projects.rglob(old) if d.is_dir()] if projects.exists() else []
    if not matches:
        record("error", vault / "Projects" / old, "project not found")
        return
    src_dir = matches[0]
    dst_dir = src_dir.parent / new
    if dst_dir.exists():
        record("conflict", dst_dir, "destination already exists")
        return

    src_dir.rename(dst_dir)
    record("updated", dst_dir, f"renamed from {old}")

    # Update textual references across the vault.
    touched = 0
    for md in vault.rglob("*.md"):
        if ".git" in md.parts:
            continue
        try:
            text = original = md.read_text(encoding="utf-8")
        except Exception:
            continue
        text = text.replace(f"Projects/{old}/", f"Projects/{new}/")
        text = text.replace(f"({old}/", f"({new}/")
        text = text.replace(f"`{old}`", f"`{new}`")
        text = re.sub(rf"^(\s*slug:\s*){re.escape(old)}\s*$", rf"\g<1>{new}", text, flags=re.M)
        # `parent:` too. Missing it silently orphans every sub-project: the folders
        # move with the parent, but their frontmatter keeps naming a slug that no
        # longer exists, so any Dataview grouping by parent quietly drops them.
        text = re.sub(rf"^(\s*parent:\s*){re.escape(old)}\s*$", rf"\g<1>{new}", text, flags=re.M)
        if text != original:
            md.write_text(text, encoding="utf-8")
            touched += 1
    record("updated", vault, f"{touched} files re-pointed")

    # If core-rules lived under the renamed project, re-record its location.
    core = _resolve_core_rules(vault)
    if core:
        try:
            cfg_path = Path.home() / ".claude" / "vault-config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
            rel = str(core.relative_to(vault))
            if cfg.get("core_rules_path") != rel:
                cfg["core_rules_path"] = rel
                cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
                record("updated", cfg_path, f"core_rules_path = {rel}")
        except Exception:
            pass


def _proj_dir(vault: Path, slug: str):
    projects = vault / "Projects"
    if not projects.exists():
        return None
    for d in projects.rglob(slug):
        if d.is_dir():
            return d
    return None


def _frontmatter_set(path: Path, **kv):
    """Set keys in a file's YAML frontmatter, adding the block if absent."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        fm = "\n".join(f"{k}: {v}" for k, v in kv.items())
        path.write_text(f"---\n{fm}\n---\n\n" + text, encoding="utf-8")
        return
    body, fm = text[m.end():], m.group(1)
    for k, v in kv.items():
        if re.search(rf"^{k}\s*:", fm, re.M):
            fm = re.sub(rf"^{k}\s*:.*$", f"{k}: {v}", fm, count=1, flags=re.M)
        else:
            fm += f"\n{k}: {v}"
    if not body.startswith("\n"):
        body = "\n" + body
    path.write_text(f"---\n{fm}\n---\n" + body, encoding="utf-8")


def _append(path: Path, text: str):
    prior = path.read_text(encoding="utf-8").rstrip() + "\n" if path.exists() else ""
    path.write_text(prior + text, encoding="utf-8")


def _strip_heading(text: str) -> str:
    """Drop frontmatter, a leading '# Title' and comment scaffolding from a merged file."""
    text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.S)
    text = re.sub(r"\A#\s+.*\n", "", text)
    text = re.sub(r"\A\s*<!--.*?-->\s*\n", "", text, flags=re.S)
    return text.strip() + "\n"


def cmd_merge_project(vault: Path, src_slug: str, dst_slug: str, today: str):
    """Fold one project into another.

    Deliberately split. Content that combines without judgement is moved and appended.
    Content encoding a CURRENT state (design.md, source.md) or a single choice
    (domain, tags) is appended under a REVIEW banner and parked in review-queue.md —
    silently concatenating two architectures yields a design.md describing neither.

    Nothing is deleted: the source becomes a tombstone so older notes and links that
    reference it still lead somewhere.
    """
    vault = vault.resolve()
    src, dst = _proj_dir(vault, src_slug), _proj_dir(vault, dst_slug)
    if src is None or dst is None:
        record("error", vault, f"project not found: {src_slug if src is None else dst_slug}")
        return
    if src == dst:
        record("error", src, "cannot merge a project into itself")
        return
    if str(dst).startswith(str(src) + "/"):
        record("error", dst, "destination is inside the source — move it out first")
        return

    review = [f"\n### Merge {src_slug} -> {dst_slug} ({today})", ""]

    # memory notes keep their filenames so existing [[wikilinks]] still resolve
    src_mem, dst_mem = src / "memory", dst / "memory"
    moved = 0
    if src_mem.exists():
        dst_mem.mkdir(parents=True, exist_ok=True)
        for f in sorted(src_mem.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            target = dst_mem / f.name
            if target.exists():
                target = dst_mem / f"{f.stem}__from_{src_slug}{f.suffix}"
                review.append(f"- [ ] Name clash: `{f.name}` kept as `{target.name}` — reconcile or keep both")
            f.rename(target)
            moved += 1
        src_idx = src_mem / "MEMORY.md"
        if src_idx.exists() and moved:
            entries = [l for l in src_idx.read_text(encoding="utf-8").splitlines() if l.startswith("- [")]
            if entries:
                _append(dst_mem / "MEMORY.md",
                        f"\n## Merged from {src_slug} ({today})\n\n" + "\n".join(entries) + "\n")
        if moved:
            record("updated", dst_mem, f"{moved} memory notes moved from {src_slug}")

    # idea.md is IMMUTABLE — preserved whole, never concatenated
    src_idea = src / "idea.md"
    if src_idea.exists():
        dst_mem.mkdir(parents=True, exist_ok=True)
        keep = dst_mem / f"idea_{src_slug.replace('-', '_')}.md"
        keep.write_text(
            f"> Origin story of `{src_slug}`, merged into `{dst_slug}` on {today}.\n"
            f"> Preserved verbatim — idea.md is immutable.\n\n"
            + src_idea.read_text(encoding="utf-8"), encoding="utf-8")
        src_idea.unlink()
        _append(dst_mem / "MEMORY.md",
                f"- [{keep.stem}]({keep.name}) — original brain dump of the merged {src_slug} project\n")
        record("updated", keep, "source idea.md preserved verbatim")

    # research.md — append-only and dated, safe to interleave
    src_res = src / "research.md"
    if src_res.exists() and _strip_heading(src_res.read_text(encoding="utf-8")).strip():
        _append(dst / "research.md",
                f"\n---\n\n# Merged from {src_slug} ({today})\n\n"
                + _strip_heading(src_res.read_text(encoding="utf-8")))
        record("updated", dst / "research.md", f"research merged from {src_slug}")

    # decisions.md — ADR ids collide, so renumber and keep the original id visible
    src_dec = src / "decisions.md"
    if src_dec.exists():
        body = _strip_heading(src_dec.read_text(encoding="utf-8"))
        if body.strip():
            dst_dec = dst / "decisions.md"
            existing = dst_dec.read_text(encoding="utf-8") if dst_dec.exists() else ""
            nums = [int(n) for n in re.findall(r"^## ADR-(\d+)", existing, re.M)]
            offset = max(nums) if nums else 0

            def _renum(m):
                old_n = int(m.group(1))
                return f"## ADR-{old_n + offset}: {m.group(2)} *(was {src_slug} ADR-{old_n})*"

            body = re.sub(r"^## ADR-(\d+):\s*(.+)$", _renum, body, flags=re.M)
            _append(dst_dec, f"\n---\n\n# Merged from {src_slug} ({today})\n\n" + body)
            record("updated", dst_dec, f"ADRs merged from {src_slug}, renumbered +{offset}")

    for name in ("runbook.md", "spec.md"):
        s = src / name
        if s.exists() and _strip_heading(s.read_text(encoding="utf-8")).strip():
            _append(dst / name, f"\n---\n\n# Merged from {src_slug} ({today})\n\n"
                    + _strip_heading(s.read_text(encoding="utf-8")))
            record("updated", dst / name, f"merged from {src_slug}")

    # design.md / source.md describe a CURRENT state — never auto-merged
    for name, why in (("design.md", "two architectures"),
                      ("source.md", "two topologies / file plans")):
        s = src / name
        if s.exists() and _strip_heading(s.read_text(encoding="utf-8")).strip():
            _append(dst / name,
                    f"\n---\n\n# MERGED FROM {src_slug} ({today}) — NEEDS REVIEW\n\n"
                    f"Appended verbatim, not reconciled: {why} cannot be combined\n"
                    f"mechanically. Rewrite this file to describe the single current system,\n"
                    f"then delete this banner.\n\n"
                    + _strip_heading(s.read_text(encoding="utf-8")))
            record("updated", dst / name, f"{name} appended UNREVIEWED from {src_slug}")
            review.append(f"- [ ] Reconcile `{dst_slug}/{name}` — {src_slug}'s section is appended, not merged")

    for child in sorted(src.iterdir()):
        if child.is_dir() and child.name != "memory" and (child / "idea.md").exists():
            child.rename(dst / child.name)
            record("updated", dst / child.name, f"sub-project moved from {src_slug}")

    # the source becomes a tombstone, not a hole
    for leftover in sorted(src.rglob("*")):
        if leftover.is_file() and leftover.name != "README.md":
            leftover.unlink()
    for d in sorted((x for x in src.rglob("*") if x.is_dir()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    (src / "README.md").write_text(
        f"---\ntype: project\nslug: {src_slug}\ndomain: merged\nstage: merged\n"
        f"merged_into: {dst_slug}\nmerged: {today}\ntags: [merged]\n---\n\n"
        f"# {src_slug} — merged into {dst_slug}\n\n"
        f"> Merged into [{dst_slug}](../{dst_slug}/) on {today}. This tombstone stays so that\n"
        f"> notes and links written before the merge still lead somewhere.\n\n"
        f"Its memory notes kept their filenames and moved to `{dst_slug}/memory/`, so existing\n"
        f"`[[wikilinks]]` still resolve. Its `idea.md` is preserved verbatim at\n"
        f"`{dst_slug}/memory/idea_{src_slug.replace('-', '_')}.md`.\n", encoding="utf-8")
    record("updated", src / "README.md", "replaced with a merge tombstone")

    idx = vault / "Projects" / "README.md"
    if idx.exists():
        lines = [l for l in idx.read_text(encoding="utf-8").splitlines()
                 if not re.search(rf"\|\s*`{re.escape(src_slug)}`\s*\|", l)]
        idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
        record("updated", idx, f"{src_slug} row removed")

    review += [
        f"- [ ] Confirm `{dst_slug}` frontmatter (domain/stage/tags) still fits the combined project",
        f"- [ ] Re-run /gt:gt-lint — expect memory-unlisted findings until MEMORY.md is tidied",
    ]
    _append(vault / "review-queue.md", "\n" + "\n".join(review) + "\n")
    record("updated", vault / "review-queue.md", "merge items needing review")

    core = _resolve_core_rules(vault)
    if core:
        try:
            cfg_path = Path.home() / ".claude" / "vault-config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
            rel = str(core.relative_to(vault))
            if cfg.get("core_rules_path") != rel:
                cfg["core_rules_path"] = rel
                cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
                record("updated", cfg_path, f"core_rules_path = {rel}")
        except Exception:
            pass


def cmd_archive_project(vault: Path, slug: str, reason: str, today: str):
    """Archive a project. Nothing is deleted.

    Follows the vault's own vocabulary: `archived` is the STAGE (CONVENTIONS.md
    defines it as "Retired or replaced"); `retire` is the LOG VERB for log.md.
    """
    vault = vault.resolve()
    proj = _proj_dir(vault, slug)
    if proj is None:
        record("error", vault, f"project not found: {slug}")
        return

    readme = proj / "README.md"
    _frontmatter_set(readme, stage="archived", archived=today)
    if readme.exists():
        text = readme.read_text(encoding="utf-8")
        banner = (f"\n> **Archived {today}.** {reason}\n>\n"
                  f"> Kept in full: its notes, decisions and research remain readable and its\n"
                  f"> `[[wikilinks]]` still resolve. Archiving is not deleting.\n")
        if "**Archived" not in text:
            m = re.match(r"^(---\s*\n.*?\n---\s*\n)(.*)$", text, re.S)
            if m:
                head, body = m.group(1), m.group(2)
                bm = re.match(r"^(\s*#[^\n]*\n)(.*)$", body, re.S)
                body = (bm.group(1) + banner + bm.group(2)) if bm else banner + body
                readme.write_text(head + body, encoding="utf-8")
            else:
                readme.write_text(banner + text, encoding="utf-8")
        record("updated", readme, f"archived: {reason}")

    idx = vault / "Projects" / "README.md"
    if idx.exists():
        lines = idx.read_text(encoding="utf-8").splitlines()
        for i, l in enumerate(lines):
            if re.search(rf"\|\s*`{re.escape(slug)}`\s*\|", l):
                parts = l.split("|")
                if len(parts) >= 5:
                    parts[4] = " archived "
                    lines[i] = "|".join(parts)
        idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
        record("updated", idx, f"{slug} marked archived")



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

    # CLAUDE.md — the outward axis. Unlike every other file here, this one is
    # written for a reader who has never seen the vault, and is committed to the
    # project's repo root so any session working in that code picks it up.
    seed_template("project-CLAUDE.md", proj / "CLAUDE.md", {
        "TITLE": display_title,
        "SLUG": slug,
    })

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

## Tasks

<!-- Format: - [ ] text [p:: 1|2|3] [waiting:: user|agent|external|parked] [due:: YYYY-MM-DD] [since:: YYYY-MM-DD]
     See [[CONVENTIONS]] > Priority. Rolled up into /TASKS.md — do not edit that file by hand. -->

## Stage
idea

## Source
{topology} — see [source.md](source.md)

## Tags
{tags_str}

## Related
"""
    ensure_file(proj / "README.md", readme_content)

    # runbook.md — the incubator for the outward axis. Created ALWAYS, not behind a
    # flag: it was opt-in until 0.6.1 and produced zero instances across eleven
    # projects. A capture surface that must be requested is one that never gets used.
    # The `runbook` parameter is retained for call compatibility and is a no-op.
    seed_template("runbook.md", proj / "runbook.md", {"TITLE": display_title})

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
    p_proj.add_argument("--runbook", action="store_true",
                    help="No-op; runbook.md is always created (kept for compatibility)")
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

    p_ren = sub.add_parser("rename-project", help="Rename a project and update every reference")
    p_ren.add_argument("--vault", required=True, type=Path)
    p_ren.add_argument("--from", dest="old", required=True)
    p_ren.add_argument("--to", dest="new", required=True)

    p_mrg = sub.add_parser("merge-project", help="Fold one project into another (nothing deleted)")
    p_mrg.add_argument("--vault", required=True, type=Path)
    p_mrg.add_argument("--from", dest="src", required=True)
    p_mrg.add_argument("--into", dest="dst", required=True)
    p_mrg.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")

    p_ret = sub.add_parser("archive-project",
                       help="Archive a project (stage: archived). Nothing is deleted.")
    p_ret.add_argument("--vault", required=True, type=Path)
    p_ret.add_argument("--slug", required=True)
    p_ret.add_argument("--reason", default="Superseded.")
    p_ret.add_argument("--date", default=None)

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
    elif args.mode == "merge-project":
        cmd_merge_project(args.vault, args.src, args.dst,
                          args.date or __import__("datetime").date.today().isoformat())
    elif args.mode == "archive-project":
        cmd_archive_project(args.vault, args.slug, args.reason,
                           args.date or __import__("datetime").date.today().isoformat())
    elif args.mode == "rename-project":
        cmd_rename_project(args.vault, args.old, args.new)
    elif args.mode == "install-core-rules":
        install_core_rules(args.vault.resolve(), wire_hooks=not args.no_hooks,
                           settings_path=args.settings)

    print(json.dumps(RESULTS, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
