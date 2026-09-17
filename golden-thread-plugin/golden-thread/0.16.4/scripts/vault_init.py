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
  3 = conflict (vault-config.json points to a different vault; fresh only —
      connect switches it)

Output: JSON array of {"action": created|skipped|updated|conflict|error, "path": ..., "note": ...}
"""
import argparse
import json
import subprocess
import shutil
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


# ---- the dry-run boundary ---------------------------------------------------
#
# Every write in this file goes through one of these three. That is the whole
# mechanism: a `--dry-run` that misses one write site is worse than none, because
# it is believed. Adding a new write means calling one of these, not Path.write_text
# -- dev/check_cli_contract.py fails the build if the flag disappears, and
# test_vault_init proves the flag writes nothing.
DRY_RUN = False


def dry(action, path, note=""):
    """True if this write is only to be described. Records it either way."""
    if DRY_RUN:
        record("would-" + action, path, note)
        return True
    return False


def w_write(path: Path, text: str, action="created", note="", **kw):
    """Write a file unless rehearsing. **kw swallows the encoding= the call sites
    pass; the text is always written as UTF-8."""
    path = Path(path)
    if dry(action, path, note):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def w_mkdir(path: Path, note="", **kw):
    path = Path(path)
    if dry("create-dir", path, note):
        return False
    kw.setdefault("parents", True)
    kw.setdefault("exist_ok", True)
    path.mkdir(**kw)
    return True


def w_unlink(path: Path, note=""):
    path = Path(path)
    if dry("delete", path, note):
        return False
    path.unlink()
    return True


def ensure_dir(path: Path):
    path = Path(path)
    if path.exists():
        record("skipped", path, "directory already exists")
    else:
        w_mkdir(path, parents=True, exist_ok=True)
        record("created", path)


def ensure_file(path: Path, content: str):
    path = Path(path)
    if path.exists():
        record("skipped", path, "file already exists")
    else:
        w_mkdir(path.parent, parents=True, exist_ok=True)
        w_write(path, content, encoding="utf-8")
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


def write_config(vault_path: Path, switch: bool = False):
    """Write ~/.claude/vault-config.json. Exit 3 if a different vault is already
    configured, unless switch=True (connect mode, the documented way to switch)."""
    config_path = Path.home() / ".claude" / "vault-config.json"
    vault_str = str(vault_path.resolve())
    if config_path.exists():
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        if existing.get("vault_path") == vault_str:
            record("skipped", config_path, "already configured with same vault")
            return
        elif switch:
            # Other settings carry over; core_rules_path is relative to the OLD vault,
            # so drop it and let gt_paths find the new vault's rules by marker file.
            old = existing.get("vault_path")
            existing.pop("core_rules_path", None)
            existing["vault_path"] = vault_str
            w_write(config_path, json.dumps(existing, indent=2) + "\n", encoding="utf-8")
            record("updated", config_path, f"switched from '{old}'")
            return
        else:
            record("conflict", config_path,
                   f"already points to '{existing.get('vault_path')}' — run with 'connect' mode to switch")
            print(json.dumps(RESULTS, indent=2))
            sys.exit(3)
    w_mkdir(config_path.parent, parents=True, exist_ok=True)
    w_write(config_path, json.dumps({"vault_path": vault_str}, indent=2), encoding="utf-8")
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
        w_write(claude_md_path, updated, encoding="utf-8")
        record("updated", claude_md_path, "appended Golden Thread memory section")
    else:
        w_mkdir(claude_md_path.parent, parents=True, exist_ok=True)
        w_write(claude_md_path, section_content + "\n", encoding="utf-8")
        record("created", claude_md_path)


def register_in_master_index(index_path: Path, slug: str, title: str, tags: list,
                             domain: str = None, stage: str = "idea"):
    """Insert a project row into the master index table.

    Appends into the markdown table rather than the end of the file, so the row
    lands in the list instead of after whatever section happens to be last.
    """
    row = f"| [{title}]({slug}/) | `{slug}` | {domain or 'TODO'} | {stage} | <!-- one-line description --> |"

    if not index_path.exists():
        w_mkdir(index_path.parent, parents=True, exist_ok=True)
        w_write(index_path, 
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

    def table(header):
        return next((i for i, l in enumerate(lines)
                     if re.match(r"^\|[\s-]*\|[\s-]*\|", l) and header in lines[i - 1]), None)

    sep = table("Slug")
    if sep is None:
        # A parent project's README: its list is the '| Sub-project |' Status table,
        # which has no Slug column, so the row takes that table's three columns.
        sep = table("Sub-project")
        if sep is not None:
            row = f"| [{title}]({slug}/) | {stage} | <!-- next action --> |"
    if sep is None:
        # No table found — fall back to appending, but keep it visible.
        lines.append(row)
    else:
        end = sep + 1
        while end < len(lines) and lines[end].startswith("|"):
            end += 1
        lines.insert(end, row)
    w_write(index_path, "\n".join(lines) + "\n", encoding="utf-8")
    record("updated", index_path, f"registered {slug}")


ENFORCEMENT_CHECK_MARKER = "## First: is enforcement active?"

# Things install-core-rules would otherwise put back on every install. One entry per line
# in <core-rules parent>/.gt-removed (by default Projects/golden-thread/.gt-removed): a
# core-rule file name, or the token below for CLAUDE.md's enforcement section. `#` starts
# a comment. Read only; gt never writes this file -- listing something is the owner's act.
REMOVED_FILE = ".gt-removed"
CLAUDE_MD_REMOVAL = "claude-md-enforcement-section"


def read_removed(folder: Path):
    """-> set of entries in <folder>/.gt-removed (empty when absent or unreadable)."""
    try:
        text = (Path(folder) / REMOVED_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    return {l.split("#", 1)[0].strip() for l in text.splitlines()} - {""}


def _ensure_enforcement_check(vault: Path, removed=None):
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
    if CLAUDE_MD_REMOVAL in (removed or set()):
        # The owner took the section out and said so in .gt-removed. Put back on every
        # install, it would be a deletion the owner cannot make stick (0.15.0).
        record("kept-removed", claude,
               "enforcement check not re-inserted — listed as '%s' in %s"
               % (CLAUDE_MD_REMOVAL, REMOVED_FILE))
        return

    lines = existing.split("\n")
    # Insert before the first H2 so it is read early; otherwise append.
    idx = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
    lines[idx:idx] = section.rstrip("\n").split("\n") + [""]
    w_write(claude, "\n".join(lines), encoding="utf-8")
    record("updated", claude, "added the enforcement-active check")


def install_core_rules(vault: Path, wire_hooks: bool = True, settings_path: Path = None,
                       record_config: bool = True):
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
    removed = read_removed(dest.parent)
    for f in sorted(src.glob("*.md")):
        if f.name in removed and not (dest / f.name).exists():
            # Deliberately deleted, and recorded as such: never re-created (0.15.0). A
            # rule a hook enforces is still enforced by that hook -- say so, loudly.
            text = f.read_text(encoding="utf-8")
            hooked = re.search(r"^\s*enforcement:\s*validated\b", text, re.M)
            record("kept-removed", dest / f.name,
                   "not re-created — listed in %s%s" % (REMOVED_FILE,
                   "; WARNING: its hook still enforces it (enforcement: validated) — "
                   "turn that off with gt_settings if that is the intent" if hooked else ""))
            continue
        ensure_file(dest / f.name, f.read_text(encoding="utf-8"))

    # Retrofit the enforcement check into the vault's own CLAUDE.md. A vault that
    # predates the Core tier has no way to tell a session that enforcement exists but
    # may be absent — and that gap is silent, because a session without the hooks reads
    # the same rules and simply never has them re-asserted.
    _ensure_enforcement_check(vault, removed)

    # Record the location so nothing has to guess next time. Relative to the vault, so
    # moving the whole vault does not invalidate it either.
    if not record_config:
        return
    try:
        cfg_path = Path.home() / ".claude" / "vault-config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        rel = str(dest.relative_to(vault))
        if cfg.get("core_rules_path") != rel:
            cfg["core_rules_path"] = rel
            cfg.setdefault("vault_path", str(vault))
            w_mkdir(cfg_path.parent, parents=True, exist_ok=True)
            w_write(cfg_path, json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
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
    # A LIST of pairs, not a dict keyed by event: PreToolUse now carries two hooks
    # (one inspects Write/Edit targets, one Bash command lines), and a dict would
    # have silently kept only the last of them — wiring one rule and dropping the
    # other while reporting success.
    #
    # READ from gt_components.HOOK_REGISTRATIONS rather than listed here. This list was
    # hand-maintained, which is how 0.9.5 shipped guard_session_claims.sh unwired: the
    # hook existed, install.sh copied it, the component check compared file contents
    # and reported clean, and nothing registered the event. A second declaration of
    # "what should be wired" is a second thing to forget.
    #
    # TRIPLES since 0.16.2: the third element is the settings.json `matcher`, or None for
    # "every source". inject_core_rules.sh is wired twice on purpose -- see the note beside
    # its second HOOK_REGISTRATIONS entry -- and the pair form could not have expressed the
    # difference between the two, which is exactly the "a dict would have kept only the last"
    # trap described above, one field further in.
    FALLBACK = [
        ("UserPromptSubmit", "inject_core_rules.sh", None),
        ("SessionStart", "inject_core_rules.sh", "compact"),
        ("Stop", "validate_response.sh", None),
        ("PreToolUse", "guard_session_claims.sh", None),
        ("PreToolUse", "guard_vault_writes.sh", None),
        ("PreToolUse", "guard_test_before_commit.sh", None),
    ]
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gt_components_for_wiring", str(Path(__file__).resolve().parent / "gt_components.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        pairs = [(r["event"], r["script"], r.get("matcher")) for r in mod.HOOK_REGISTRATIONS
                 if str(r.get("owner", "")).startswith("vault_init.py")]
        if not pairs:
            raise ValueError("no vault_init-owned registrations declared")
        order_blocks = lambda hooks: mod.order_hook_blocks(  # noqa: E731
            hooks, [(r["event"], r["script"]) for r in mod.HOOK_REGISTRATIONS],
            str(hooks_dir))
    except Exception:
        pairs = FALLBACK
        order_blocks = lambda hooks: False  # noqa: E731
    wanted = [(event, hooks_dir / script, matcher) for event, script, matcher in pairs]
    missing = [str(v) for _, v, _ in wanted if not v.is_file()]
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
    for event, script, matcher in wanted:
        blocks = data["hooks"].setdefault(event, [])
        # Drop any previous entry that pointed at a vault-internal copy of this hook.
        for b in blocks:
            if isinstance(b, dict):
                b["hooks"] = [h for h in b.get("hooks", [])
                              if not (h.get("command", "").endswith(script.name)
                                      and h.get("command") != str(script))]
        blocks[:] = [b for b in blocks if not (isinstance(b, dict) and not b.get("hooks"))]
        # Already-wired means wired WITH THE RIGHT MATCHER. A block carrying the correct
        # command under the wrong matcher fires on the wrong sources, which for the
        # compaction entry would mean re-injecting the Core rules on every session start
        # (harmless but wasteful) or on none (the hole this closes, still open while
        # reporting success). So an existing entry whose matcher disagrees is rewritten,
        # not skipped.
        existing = [b for b in blocks if isinstance(b, dict)
                    and any(h.get("command") == str(script) for h in b.get("hooks", []))]
        if existing and all(b.get("matcher") == matcher for b in existing):
            record("skipped", settings, f"{event} hook already wired")
            continue
        if existing:
            blocks[:] = [b for b in blocks if b not in existing]
            record("rewired", settings,
                   f"{event}/{script.name} matcher was {existing[0].get('matcher')!r}, "
                   f"now {matcher!r}")
        block = {"hooks": [{"type": "command", "command": str(script), "timeout": 10}]}
        if matcher is not None:
            block["matcher"] = matcher
        blocks.append(block)
        changed = True

    # The same canonical order install.sh applies (0.15.0, R1): appending here put a
    # newly wired enforcement hook after gt's other entries on an upgrade but not on a
    # fresh install. Module entries, not in HOOK_REGISTRATIONS, keep their place after.
    if order_blocks(data["hooks"]):
        changed = True

    if changed:
        w_mkdir(settings.parent, parents=True, exist_ok=True)
        w_write(settings, json.dumps(data, indent=2) + "\n", encoding="utf-8")
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


def _mentions_slug(md: Path, slug: str) -> bool:
    try:
        text = md.read_text(encoding="utf-8")
    except Exception:
        return False
    return (f"Projects/{slug}/" in text or f"({slug}/" in text or f"`{slug}`" in text
            or re.search(rf"^\s*(slug|parent):\s*{re.escape(slug)}\s*$", text, flags=re.M) is not None)


def cmd_rename_project(vault: Path, old: str, new: str):
    """Rename a project and update every reference to it.

    Renames happen — projects get redefined, merged, retired. This makes that a
    supported operation rather than a manual sweep that misses something.
    """
    vault = vault.resolve()
    projects = vault / "Projects"
    # never the spool: spool/decisions/<slug> shares the slug and is not the project
    matches = [d for d in projects.rglob(old) if d.is_dir() and not _in_spool(vault, d)] \
        if projects.exists() else []
    if not matches:
        record("error", vault / "Projects" / old, "project not found")
        return
    src_dir = matches[0]
    dst_dir = src_dir.parent / new
    if dst_dir.exists():
        record("conflict", dst_dir, "destination already exists")
        return
    spool_root = _decisions_spool_root(vault)
    new_spool = spool_root / str(dst_dir.relative_to(projects))
    if (spool_root / str(src_dir.relative_to(projects))).is_dir() and new_spool.exists():
        record("conflict", new_spool, "decisions spool already exists at the new name")
        return

    if DRY_RUN:
        # Path.rename below is outside the dry-run boundary (w_write): until 0.14.0 a
        # `--dry-run` rename really renamed the folder. Rehearse what would change, stop.
        refs = sum(1 for md in vault.rglob("*.md") if ".git" not in md.parts
                   and _mentions_slug(md, old))
        record("would-rename", src_dir, f"-> {dst_dir.name}; {refs} file(s) mention {old}")
        return

    src_dir.rename(dst_dir)
    record("updated", dst_dir, f"renamed from {old}")
    # The spool is keyed by the project's path: left behind, the renamed project reads as
    # unmigrated (`decisions-spool` pending) and its next ADR restarts at 1.
    _move_decisions_spool(vault, src_dir, dst_dir)

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
            w_write(md, text, encoding="utf-8")
            touched += 1
    record("updated", vault, f"{touched} files re-pointed")
    # the sweep edited spool files and rendered files alike; re-render so they agree
    _rerender_decisions(vault, dst_dir)

    # If core-rules lived under the renamed project, re-record its location.
    core = _resolve_core_rules(vault)
    if core:
        try:
            cfg_path = Path.home() / ".claude" / "vault-config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
            rel = str(core.relative_to(vault))
            if cfg.get("core_rules_path") != rel:
                cfg["core_rules_path"] = rel
                w_write(cfg_path, json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
                record("updated", cfg_path, f"core_rules_path = {rel}")
        except Exception:
            pass
    return src_dir, dst_dir


# ---- decisions spool (0.11.0) for rename-project / merge-project ------------------
#
# Since 0.11.0 a project's decisions.md is GENERATED by `gt_adr.py merge` from
# Projects/golden-thread/spool/decisions/<project>/. Until 0.14.0 rename and merge
# treated it as a plain file: merge appended the source's ADRs to the rendered
# decisions.md, and the next `gt_adr.py merge` re-rendered from the spool and deleted
# them without a word; rename left the spool under the old name. Found 2026-09-14.

def _adr_tool(vault: Path) -> Path:
    return vault / "Projects" / "golden-thread" / "tools" / "gt_adr.py"


def _load_adr(vault: Path):
    """The VAULT's own gt_adr module (one implementation of numbering and rendering)."""
    tool = _adr_tool(vault)
    if not tool.is_file():
        return None
    import importlib.util
    sys.dont_write_bytecode = True        # leave no __pycache__ in the vault's tools
    spec = importlib.util.spec_from_file_location("_vault_gt_adr", str(tool))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_adr(vault: Path, *args, tool_vault: Path = None):
    """Run the vault's gt_adr.py. `tool_vault` lets it act on a staging copy that
    holds no tools of its own."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    tool = _adr_tool(tool_vault or vault)
    p = subprocess.run([sys.executable, str(tool), "--vault", str(vault), *args],
                       capture_output=True, text=True, env=env)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    return p.returncode, (out.splitlines()[-1] if out else "rc=%s" % p.returncode)


def _primary_numbers(adr, text: str) -> list:
    return [int(m.group(1)) for m in adr.HEAD.finditer(text) if not m.group(2)]


def _adr_sections(adr, text: str):
    """Split rendered decisions text into (preamble, [[number, section-text], ...]).

    An `ADR-N amendment` section is not a new decision: it stays attached to the
    section before it, so the source's own order survives the move."""
    heads = list(adr.HEAD.finditer(text))
    if not heads:
        return text, []
    preamble, groups = text[:heads[0].start()], []
    for i, m in enumerate(heads):
        chunk = text[m.start():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        if m.group(2) and groups:
            groups[-1][1] += chunk
        elif m.group(2):
            preamble += chunk
        else:
            groups.append([int(m.group(1)), chunk])
    return preamble, groups


def _in_spool(vault: Path, d: Path) -> bool:
    spool = vault / "Projects" / "golden-thread" / "spool"
    return spool in d.parents or d == spool


def _decisions_spool_root(vault: Path) -> Path:
    return vault / "Projects" / "golden-thread" / "spool" / "decisions"


def _move_decisions_spool(vault: Path, old_dir: Path, new_dir: Path):
    """Move a project's decisions spool (sub-project spools nest inside and move too).

    `old_dir`/`new_dir` are project folders; only their path under Projects/ is used,
    so this works before or after the folder itself has moved."""
    root = _decisions_spool_root(vault)
    projects = vault / "Projects"
    old = root / str(old_dir.relative_to(projects))
    new = root / str(new_dir.relative_to(projects))
    if not old.is_dir():
        return True
    if new.exists():
        record("conflict", new, "decisions spool already exists at the new name")
        return False
    if dry("move", new, f"decisions spool moved from {old.relative_to(root)}"):
        return True
    new.parent.mkdir(parents=True, exist_ok=True)
    old.rename(new)
    record("updated", new, f"decisions spool moved from {old.relative_to(root)}")
    return True


def _rerender_decisions(vault: Path, proj: Path):
    """Re-render decisions.md for `proj` and every sub-project with a spool."""
    root = _decisions_spool_root(vault)
    base = root / str(proj.relative_to(vault / "Projects"))
    if not base.is_dir() or not _adr_tool(vault).is_file():
        return
    for d in [base] + sorted(x for x in base.rglob("*") if x.is_dir()):
        rel = str(d.relative_to(root))
        target = vault / "Projects" / rel / "decisions.md"
        if not (d / "0000-baseline.md").is_file() and not any(
                re.fullmatch(r"\d{4}\.md", f.name) for f in d.iterdir()):
            continue
        if target.exists() and "GENERATED" not in target.read_text(
                encoding="utf-8", errors="replace")[:600]:
            record("skipped", target, "not generated; left for gt_adr.py (would overwrite)")
            continue
        if dry("update", target, "would re-render from the spool"):
            continue
        rc, out = _run_adr(vault, "merge", rel)
        record("updated" if rc == 0 else "error", target, out)


def _merge_decisions_spool(vault: Path, src: Path, dst: Path, src_slug: str,
                           dst_slug: str, today: str):
    """Move src's ADRs into dst's spool, renumbered, then re-render dst. True = go on.

    All-or-nothing. Everything that can refuse is checked before the first write; the
    only writes to dst are exclusive creates of new slot files (the allocator's own
    O_CREAT|O_EXCL, so a concurrent `allocate` can never be overwritten); every one is
    removed again if verification fails; src's spool is deleted only after dst's
    re-rendered decisions.md is proven to hold every ADR.
    """
    import tempfile
    projects = vault / "Projects"
    srel, drel = str(src.relative_to(projects)), str(dst.relative_to(projects))
    adr = _load_adr(vault)
    sdec, ddec = src / "decisions.md", dst / "decisions.md"
    if adr is None:
        spool = vault / "Projects" / "golden-thread" / "spool" / "decisions"
        if (spool / srel).exists() or (spool / drel).exists():
            record("error", _adr_tool(vault), "decisions spool exists but gt_adr.py is missing; "
                   "merge refused, nothing changed")
            return False
        return None                       # pre-0.11 vault: the plain-file path still applies
    S = adr.S
    sspool, dspool = S.spool_dir(vault, adr.KIND, srel), S.spool_dir(vault, adr.KIND, drel)

    def refuse(path, why):
        record("error", path, why + " — merge refused, nothing changed")
        return False

    for dec, sp in ((sdec, sspool), (ddec, dspool)):
        if dec.exists() and not S.is_generated(dec) and (sp / S.BASELINE).is_file():
            return refuse(dec, "decisions.md is not generated but a spool baseline exists "
                               "(hand-edited generated file?); resolve it with gt_adr.py first")

    staging = Path(tempfile.mkdtemp(prefix="gt-merge-"))
    created, dst_migrated = [], False
    ddec_before = ddec.read_bytes() if ddec.exists() else None
    dspool_existed = dspool.is_dir() and any(dspool.iterdir())
    try:
        # -- read src's ADRs: from its spool, or (legacy) from a migration done in a copy
        read_root = vault
        if not (sspool / S.BASELINE).is_file() and sdec.exists():
            if S.is_generated(sdec):
                return refuse(sdec, "decisions.md is generated but has no spool baseline")
            (staging / "Projects" / srel).mkdir(parents=True)
            shutil.copy2(sdec, staging / "Projects" / srel / "decisions.md")
            if sspool.is_dir():
                shutil.copytree(sspool, S.spool_dir(staging, adr.KIND, srel), dirs_exist_ok=True)
            rc, out = _run_adr(staging, "migrate", srel, tool_vault=vault)
            if rc != 0:
                return refuse(sdec, "gt_adr.py migrate %s: %s" % (srel, out))
            read_root = staging
        body = adr.render(read_root, srel)[len(S.BANNER):]
        preamble, groups = _adr_sections(adr, body)
        preamble = _strip_heading(preamble.lstrip()).strip()
        if not groups and not preamble:
            return True                    # nothing but scaffolding to carry over
        if not groups:                     # prose without an ADR still must not be lost
            groups = [[0, f"## ADR-0: Notes from {src_slug}'s decisions.md\n"]]

        # -- dst must be migrated before it can take slots; rehearse first
        if not (dspool / S.BASELINE).is_file() and ddec.exists():
            if S.is_generated(ddec):
                return refuse(ddec, "decisions.md is generated but has no spool baseline")
            rc, out = _run_adr(vault, "migrate", drel, "--dry-run")
            if not dspool_existed and dspool.is_dir() and not any(dspool.iterdir()):
                dspool.rmdir()             # the rehearsal's mkdir, not ours to keep
            if rc != 0:
                return refuse(ddec, "gt_adr.py migrate %s: %s" % (drel, out))
        if DRY_RUN:
            record("would-update", dspool, f"{len(groups)} ADR(s) from {src_slug} as new spool slots")
            record("would-delete", sspool, f"{src_slug}'s decisions spool, once carried over")
            return True
        if not (dspool / S.BASELINE).is_file() and ddec.exists():
            rc, out = _run_adr(vault, "migrate", drel)
            if rc != 0:
                return refuse(ddec, "gt_adr.py migrate %s: %s" % (drel, out))
            dst_migrated = True
        dspool.mkdir(parents=True, exist_ok=True)
        before = _primary_numbers(adr, adr.render(vault, drel))

        # -- renumber onto slots, exactly as the allocator reserves a number
        sid = f"merge-project:{src_slug}->{dst_slug}:{today}"
        for _ in range(adr.MAX_RETRY):
            taken = set(adr.slots(vault, drel)) | adr.baseline_numbers(vault, drel)
            offset = max(max(taken) if taken else 0, adr._highwater(dspool))
            for p in created:
                p.unlink()
            created, clash = [], False
            for i, (n, chunk) in enumerate(groups):
                first, _, rest = chunk.partition("\n")
                hm = re.match(r"^##\s*ADR-\d+\s*:?\s*(.*)$", first)
                title = hm.group(1).strip() if hm else first
                new = n + offset if n else offset + 1
                text = (f"<!-- allocated by {sid} -->\n## ADR-{new}: {title} "
                        f"*(was {src_slug} ADR-{n})*\n" if n else
                        f"<!-- allocated by {sid} -->\n## ADR-{new}: {title}\n") + rest
                if i == 0 and preamble:
                    head, _, tail = text.partition("\n## ")
                    text = f"{head}\n*Merged from {src_slug} ({today}):*\n\n{preamble}\n\n## {tail}"
                p = dspool / ("%04d.md" % new)
                try:
                    fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                except FileExistsError:
                    clash = True           # someone allocated meanwhile: back out, recompute
                    break
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text.rstrip("\n") + "\n")
                created.append(p)
            if not clash:
                break
        else:
            raise RuntimeError("could not reserve ADR numbers after %d attempts" % adr.MAX_RETRY)
        adr._raise_highwater(dspool, max(int(p.name[:4]) for p in created))

        if os.environ.get("GT_TEST_FAULT") == "merge-decisions":
            raise RuntimeError("injected fault (GT_TEST_FAULT=merge-decisions)")

        # -- verify, render, verify again; only then is src's spool expendable
        after = _primary_numbers(adr, adr.render(vault, drel))
        want = set(before) | {int(p.name[:4]) for p in created}
        if len(after) != len(set(after)) or not want <= set(after) \
                or len(after) < len(before) + len(groups):
            raise RuntimeError("verification failed: dst would hold ADRs %s" % sorted(after))
        rc, out = _run_adr(vault, "merge", drel)
        if rc != 0 or ddec.read_text(encoding="utf-8") != adr.render(vault, drel):
            raise RuntimeError("gt_adr.py merge %s: %s" % (drel, out))
        record("updated", ddec, f"{len(groups)} ADR(s) merged from {src_slug} into the spool, "
               f"renumbered +{offset}; re-rendered")
    except Exception as exc:
        for p in created:
            try:
                p.unlink()
            except OSError:
                pass
        if dst_migrated:
            try:
                (dspool / S.BASELINE).unlink()
                if ddec_before is not None:
                    ddec.write_bytes(ddec_before)
            except OSError:
                pass
        if not dspool_existed:
            shutil.rmtree(dspool, ignore_errors=True)
        return refuse(ddec, f"decisions not merged ({exc}); both projects' decisions left as they were")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if sspool.is_dir():                    # sub-project spools move with their folders later
        for f in sspool.iterdir():
            if f.is_file():
                f.unlink()
        record("updated", sspool, f"{src_slug}'s decisions spool removed (carried into {dst_slug})")
    return True


def _proj_dir(vault: Path, slug: str):
    projects = vault / "Projects"
    if not projects.exists():
        return None
    for d in projects.rglob(slug):
        if d.is_dir() and not _in_spool(vault, d):
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
        w_write(path, f"---\n{fm}\n---\n\n" + text, encoding="utf-8")
        return
    body, fm = text[m.end():], m.group(1)
    for k, v in kv.items():
        if re.search(rf"^{k}\s*:", fm, re.M):
            fm = re.sub(rf"^{k}\s*:.*$", f"{k}: {v}", fm, count=1, flags=re.M)
        else:
            fm += f"\n{k}: {v}"
    if not body.startswith("\n"):
        body = "\n" + body
    w_write(path, f"---\n{fm}\n---\n" + body, encoding="utf-8")


def _append(path: Path, text: str):
    prior = path.read_text(encoding="utf-8").rstrip() + "\n" if path.exists() else ""
    w_write(path, prior + text, encoding="utf-8")


def _section(text: str, heading: str) -> list:
    """Lines under `## <heading>`, up to the next `## ` heading."""
    m = re.search(rf"^## {re.escape(heading)}\s*$", text, re.M)
    if not m:
        return []
    rest = text[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    return (rest[:nxt.start()] if nxt else rest).splitlines()


def _add_tasks(readme: Path, tasks: list):
    """Add task lines at the end of a README's ## Tasks section, creating it if absent."""
    text = readme.read_text(encoding="utf-8") if readme.exists() else ""
    m = re.search(r"^## Tasks\s*$", text, re.M)
    if not m:
        _append(readme, "\n## Tasks\n\n" + "\n".join(tasks) + "\n")
        return
    nxt = re.search(r"^## ", text[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    head = text[:end].rstrip("\n")
    tail = text[end:]
    w_write(readme, head + "\n" + "\n".join(tasks) + "\n" + ("\n" + tail if tail else ""),
                      encoding="utf-8")


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

    # decisions FIRST: it is the step that can refuse, and a refusal must find both
    # projects exactly as they were -- nothing else has moved yet.
    spooled = _merge_decisions_spool(vault, src, dst, src_slug, dst_slug, today)
    if spooled is False:
        return
    if DRY_RUN:
        # The steps below move and delete with Path.rename/unlink, outside the dry-run
        # boundary: until 0.14.0 a `--dry-run` merge really moved memory notes, deleted
        # the source's decisions.md and research.md, then crashed. Rehearse, stop.
        record("would-merge", src, f"fold {src_slug} into {dst_slug}; {src_slug} becomes a tombstone")
        return

    review = [f"\n### Merge {src_slug} -> {dst_slug} ({today})", ""]

    # memory notes keep their filenames so existing [[wikilinks]] still resolve
    src_mem, dst_mem = src / "memory", dst / "memory"
    moved = 0
    if src_mem.exists():
        w_mkdir(dst_mem, parents=True, exist_ok=True)
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
        w_mkdir(dst_mem, parents=True, exist_ok=True)
        keep = dst_mem / f"idea_{src_slug.replace('-', '_')}.md"
        w_write(keep, 
            f"> Origin story of `{src_slug}`, merged into `{dst_slug}` on {today}.\n"
            f"> Preserved verbatim — idea.md is immutable.\n\n"
            + src_idea.read_text(encoding="utf-8"), encoding="utf-8")
        w_unlink(src_idea, )
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
    # (spool vaults were handled above; this plain-file path is for pre-0.11 vaults only)
    src_dec = src / "decisions.md"
    if spooled is None and src_dec.exists():
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
            _move_decisions_spool(vault, child, dst / child.name)

    sroot = vault / "Projects" / "golden-thread" / "spool" / "decisions" / str(
        src.relative_to(vault / "Projects"))
    if spooled and sroot.is_dir() and not DRY_RUN:
        try:
            sroot.rmdir()
        except OSError:
            record("skipped", sroot, "left in place: holds spools of folders merge did not move")

    # open tasks stay live: moved into the destination's ## Tasks, where gt_tasks reads them
    src_readme = src / "README.md"
    tasks = []
    if src_readme.exists():
        tasks = [l for l in _section(src_readme.read_text(encoding="utf-8"), "Tasks")
                 if re.match(r"^\s*- \[ \]", l)]
    if tasks:
        _add_tasks(dst / "README.md", tasks)
        record("updated", dst / "README.md", f"{len(tasks)} open task(s) moved from {src_slug}")
        review.append(f"- [ ] {len(tasks)} open task(s) from {src_slug} added to `{dst_slug}/README.md` — re-prioritise")

    # the source becomes a tombstone, not a hole. Only files whose content was carried
    # over above (or that held nothing but scaffolding) are removed; everything else --
    # the old README, CLAUDE.md, files merge does not recognise -- moves, untouched,
    # into an archive folder in the destination.
    for name in ("research.md", "decisions.md", "runbook.md", "spec.md", "design.md", "source.md"):
        if (src / name).is_file():
            (src / name).unlink()
    for d in sorted((x for x in src.rglob("*") if x.is_dir()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    leftovers = sorted(src.iterdir())
    archive = None
    if leftovers:
        archive, n = dst / f"merged-{src_slug}", 1
        while archive.exists():
            n += 1
            archive = dst / f"merged-{src_slug}-{n}"
        w_mkdir(archive, )
        for item in leftovers:
            # README.md renamed so the archive is not read as a second live project
            # by gt_tasks, gt_lint and the Dataview views.
            item.rename(archive / ("README.pre-merge.md" if item.name == "README.md" else item.name))
        record("updated", archive, f"{len(leftovers)} item(s) from {src_slug} kept verbatim")
        review.append(f"- [ ] Look through `{dst_slug}/{archive.name}/` — {src_slug}'s files merge did not fold in")
    (src / "README.md").write_text(
        f"---\ntype: project\nslug: {src_slug}\ndomain: merged\nstage: merged\n"
        f"merged_into: {dst_slug}\nmerged: {today}\ntags: [merged]\n---\n\n"
        f"# {src_slug} — merged into {dst_slug}\n\n"
        f"> Merged into [{dst_slug}](../{dst_slug}/) on {today}. This tombstone stays so that\n"
        f"> notes and links written before the merge still lead somewhere.\n\n"
        f"Its memory notes kept their filenames and moved to `{dst_slug}/memory/`, so existing\n"
        f"`[[wikilinks]]` still resolve. Its `idea.md` is preserved verbatim at\n"
        f"`{dst_slug}/memory/idea_{src_slug.replace('-', '_')}.md`.\n"
        + (f"Its open tasks moved to `{dst_slug}/README.md`.\n" if tasks else "")
        + (f"Everything else it held — its old README, CLAUDE.md and any other files — is kept\n"
           f"verbatim at `{dst_slug}/{archive.name}/`.\n" if archive else ""), encoding="utf-8")
    record("updated", src / "README.md", "replaced with a merge tombstone")

    idx = vault / "Projects" / "README.md"
    if idx.exists():
        lines = [l for l in idx.read_text(encoding="utf-8").splitlines()
                 if not re.search(rf"\|\s*`{re.escape(src_slug)}`\s*\|", l)]
        w_write(idx, "\n".join(lines) + "\n", encoding="utf-8")
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
                w_write(cfg_path, json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
                record("updated", cfg_path, f"core_rules_path = {rel}")
        except Exception:
            pass
    return src, dst


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
                w_write(readme, head + body, encoding="utf-8")
            else:
                w_write(readme, banner + text, encoding="utf-8")
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
        w_write(idx, "\n".join(lines) + "\n", encoding="utf-8")
        record("updated", idx, f"{slug} marked archived")
    return proj



def seed_vault_workspace(vault: Path, seeded: bool = True):
    """Everything a vault needs beyond the rules and the conventions, seeded only when
    absent: the vault tools, the inbox, the git hooks and the rollup.

    Before 0.9.9 this happened only inside install.sh, and only when vault-config.json
    already pointed at a vault that was already a git repo -- so a vault created by
    `fresh` had no gt_tasks.py, no gt_closeout.py, no INBOX.md and no TASKS.md, and
    the manual told its owner to run four files that did not exist. Found by
    scaffolding a vault from the plugin alone into an empty HOME.
    """
    tools_src = TEMPLATES_DIR / "tools"
    tools_dst = vault / "Projects" / "golden-thread" / "tools"
    if tools_src.is_dir():
        ensure_dir(tools_dst)
        for f in sorted(tools_src.glob("*.py")):
            ensure_file(tools_dst / f.name, f.read_text(encoding="utf-8"))
            try:
                (tools_dst / f.name).chmod(0o755)
            except Exception:
                pass
    ensure_file(vault / "Projects" / "golden-thread" / "README.md",
                "---\ntype: project\nslug: golden-thread\ndomain: infrastructure\nstage: active\n"
                "pp: 3\ntopology: local\ntags: [infrastructure, memory, tooling]\n---\n\n"
                "# Golden Thread\n\n> The memory system itself: its Core rules, tools, sessions "
                "and pending edits live under this folder.\n\n## Tasks\n\n"
                "<!-- Format: - [ ] text [p:: 1|2|3|7+] [waiting:: user|agent|external|parked] "
                "[due:: YYYY-MM-DD] [since:: YYYY-MM-DD]\n     See CONVENTIONS.md > Priority. "
                "Rolled up into /TASKS.md -- do not edit that file by hand. -->\n")
    inbox = TEMPLATES_DIR / "INBOX.md"
    if inbox.exists():
        ensure_file(vault / "INBOX.md", inbox.read_text(encoding="utf-8"))

    # How to open this folder in Obsidian, and which plugins the vault's own
    # conventions actually rely on. Seeded INTO the vault rather than left in the
    # plugin's docs because the question ("how do I look at this?") is asked while
    # looking at the folder, often on a machine that never cloned the plugin.
    obs = TEMPLATES_DIR / "OPEN-IN-OBSIDIAN.md"
    if obs.exists():
        ensure_file(vault / "OPEN-IN-OBSIDIAN.md",
                    obs.read_text(encoding="utf-8").replace("{{VAULT_PATH}}", str(vault)))

    # The merge BASE for the documents an owner edits, and the version stamp that says
    # which release they came from. Without these, gt_upgrade.py has no way to take a
    # release's changes into an edited PROTOCOL.md without either overwriting the
    # owner's work or ignoring the update -- it is the difference between a merge and a
    # guess. The base lives in the vault because old release directories are pruned.
    #
    # `fresh` seeded the documents from these templates a moment ago, so the template IS
    # the base. `connect` (seeded=False) did not: an existing vault's document may carry
    # edits nobody has reviewed against the release, and recording the template as its
    # base would make the next unattended upgrade treat that review as done. So connect
    # records a base only when the document is byte-identical to the shipped template --
    # nothing to review -- and otherwise leaves it for gt_upgrade to report as
    # "needs a person: no merge base" until `gt_upgrade.py run --record-base <doc>`.
    base_dir = vault / "Projects" / "golden-thread" / ".templates"
    for name in ("PROTOCOL.md", "CONVENTIONS.md"):
        src = TEMPLATES_DIR / name
        if not src.is_file():
            continue
        if not seeded and not (base_dir / name).exists():
            doc = vault / "Projects" / name
            if not (doc.is_file() and doc.read_bytes() == src.read_bytes()):
                record("skipped", base_dir / name,
                       "no merge base recorded: Projects/%s %s the shipped template -- review "
                       "it, then gt_upgrade.py run --record-base %s"
                       % (name, "differs from" if doc.is_file() else "is absent, not", name))
                continue
        ensure_file(base_dir / name, src.read_text(encoding="utf-8"))
    stamp = vault / "Projects" / "golden-thread" / ".vault-version.json"
    if not stamp.exists():
        try:
            version = json.loads((SCRIPT_DIR.parent / ".claude-plugin" / "plugin.json")
                                 .read_text(encoding="utf-8"))["version"]
        except Exception:
            version = SCRIPT_DIR.parent.name
        ensure_file(stamp, json.dumps(
            {"gt": version,
             "updated": __import__("datetime").datetime.now().astimezone().isoformat(),
             "history": [{"to": version, "at": "seeded", "applied": ["fresh"]}]},
            indent=2) + "\n")

    # The vault is a git repo so its truth survives one disk; per-edit attribution
    # rides on git hooks that only work once core.hooksPath points at .githooks.
    if shutil.which("git"):
        inside = subprocess.run(["git", "-C", str(vault), "rev-parse", "--git-dir"],
                                capture_output=True, text=True)
        if inside.returncode != 0:
            subprocess.run(["git", "-C", str(vault), "init", "-q"], capture_output=True)
            record("created", vault / ".git", "git init")
        hooks_src = TEMPLATES_DIR / "githooks"
        if hooks_src.is_dir():
            ensure_dir(vault / ".githooks")
            for f in sorted(hooks_src.iterdir()):
                if f.is_file():
                    ensure_file(vault / ".githooks" / f.name, f.read_text(encoding="utf-8"))
                    try:
                        (vault / ".githooks" / f.name).chmod(0o755)
                    except Exception:
                        pass
            # Set only when unset -- the rule vault_refresh.py applies on every install. An
            # owner's own hooks directory is kept; vault_refresh prints the chaining hint.
            cur = subprocess.run(["git", "-C", str(vault), "config", "--get", "core.hooksPath"],
                                 capture_output=True, text=True).stdout.strip()
            if not cur:
                subprocess.run(["git", "-C", str(vault), "config", "core.hooksPath", ".githooks"],
                               capture_output=True)
                record("updated", vault / ".git/config", "core.hooksPath = .githooks")
            elif cur.rstrip("/") in (".githooks", str(vault / ".githooks")):
                record("skipped", vault / ".git/config", "core.hooksPath already .githooks")
            else:
                record("skipped", vault / ".git/config",
                       "core.hooksPath is %r (yours) -- kept" % cur)
    else:
        record("skipped", vault / ".git", "git not found; attribution hooks not wired")

    rollup = tools_dst / "gt_tasks.py"
    if rollup.exists():
        r = subprocess.run([sys.executable, str(rollup), "--vault", str(vault)],
                           capture_output=True, text=True)
        record("created" if r.returncode == 0 else "error", vault / "TASKS.md",
               "generated by gt_tasks.py" if r.returncode == 0 else (r.stderr or "").strip()[-200:])


def _generate_fresh_log(vault: Path):
    """Make the log.md `fresh` just wrote a GENERATED file, as 0.11.0 defines it.

    Until 0.14.0 `fresh` wrote a plain log.md with no spool baseline, so every new vault
    was born one migration behind: `gt_upgrade.py status` reported `log-spool` pending,
    and install.sh -- which applies upgrades unattended -- migrated the vault it had just
    created and left it "uncommitted for your review". Found by regression 2026-09-14.

    The vault's own `gt_log.py migrate` does the work rather than a copy of it here: the
    baseline and the rendered file must be byte-identical to what that migration produces
    (its round-trip check is the gate), and a second implementation is a second thing to
    drift. Only a log.md created in THIS run is migrated -- an existing file is the
    owner's history and belongs to gt_upgrade, which backs the vault up first.
    """
    tool = vault / "Projects" / "golden-thread" / "tools" / "gt_log.py"
    if dry("migrate", vault / "log.md", "would generate log.md from the spool baseline"):
        return
    if not tool.is_file():
        record("error", vault / "log.md", "gt_log.py not seeded; log.md left ungenerated")
        return
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, str(tool), "--vault", str(vault), "migrate"],
                       capture_output=True, text=True, env=env)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    record("updated" if p.returncode == 0 else "error", vault / "log.md",
           out.splitlines()[-1] if out else "gt_log.py migrate rc=%s" % p.returncode)


def _generate_project_decisions(vault: Path, proj: Path):
    """Make the decisions.md `create-project` just wrote a GENERATED file (0.11.0).

    Same bug as `_generate_fresh_log`, one level down: a new project's decisions.md had
    no spool baseline, so `gt_upgrade.py status` reported `decisions-spool` pending the
    moment the project existed, and install.sh migrated it unattended. Found by
    regression 2026-09-14. The vault's own `gt_adr.py migrate` does the work, for the
    same reason: one implementation, gated by its own round-trip check. The caller
    passes only a decisions.md created in THIS run -- an existing one is the owner's
    ADR history and belongs to gt_upgrade.
    """
    rel = str(proj.relative_to(vault / "Projects"))
    target = proj / "decisions.md"
    tool = vault / "Projects" / "golden-thread" / "tools" / "gt_adr.py"
    if dry("migrate", target, "would generate decisions.md from the spool baseline"):
        return
    if not tool.is_file():
        record("error", target, "gt_adr.py not seeded; decisions.md left ungenerated")
        return
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, str(tool), "--vault", str(vault), "migrate", rel],
                       capture_output=True, text=True, env=env)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    record("updated" if p.returncode == 0 else "error", target,
           out.splitlines()[-1] if out else "gt_adr.py migrate rc=%s" % p.returncode)


def cmd_fresh(vault: Path, domain: str, no_config: bool = False):
    """Scaffold a vault. With no_config, touch nothing outside it — not vault-config.json,
    not settings.json, not ~/.claude/CLAUDE.md. That is how gt-demo builds a throwaway
    vault beside the real one without the real one's configuration moving at all."""
    vault = vault.resolve()
    if not no_config:
        write_config(vault)

    ensure_dir(vault)
    ensure_dir(vault / "Knowledge")
    ensure_dir(vault / "Sources")
    ensure_dir(vault / "global-memory")
    ensure_dir(vault / "Projects")

    seed_template("wiki-CLAUDE.md", vault / "CLAUDE.md", {"DOMAIN": domain})

    ensure_file(vault / "index.md", f"# {domain} Knowledge Index\n\n<!-- Add entries: [[Page Title]] — one-line description -->\n")
    log_is_new = not (vault / "log.md").exists()
    ensure_file(vault / "log.md", "# Golden Thread Activity Log\n\n<!-- Format: YYYY-MM-DD [verb] description -->\n")
    ensure_file(vault / "review-queue.md", "# Review Queue\n\n<!-- Items flagged for owner review -->\n")
    ensure_file(vault / "global-memory" / "MEMORY.md", "# Global Memory Index\n\n<!-- One entry per file: - [Title](filename.md) — description -->\n")

    seed_template("CONVENTIONS.md", vault / "Projects" / "CONVENTIONS.md", {"DOMAIN": domain})
    seed_template("PROTOCOL.md", vault / "Projects" / "PROTOCOL.md", {"DOMAIN": domain})
    seed_template("INFRASTRUCTURE.md", vault / "Projects" / "INFRASTRUCTURE.md", {"DOMAIN": domain})
    seed_template("PROJECTS-README.md", vault / "Projects" / "README.md", {"DOMAIN": domain})
    seed_template("lint-declines.md", vault / "lint-declines.md", {"DOMAIN": domain})

    install_core_rules(vault, wire_hooks=not no_config, record_config=not no_config)
    seed_vault_workspace(vault)
    if log_is_new:
        _generate_fresh_log(vault)
    if no_config:
        return

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
    decisions_is_new = not (proj / "decisions.md").exists()
    ensure_file(proj / "decisions.md",
                f"# {display_title} Decisions\n\n<!-- Append-only ADRs. Format: ## ADR-N: Title -->\n")
    if decisions_is_new:
        _generate_project_decisions(vault, proj)
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
    readme_is_new = not (proj / "README.md").exists()
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
    return proj if readme_is_new else None


def cmd_connect(vault: Path):
    vault = vault.resolve()
    if not vault.exists():
        record("error", vault, "vault directory does not exist")
        print(json.dumps(RESULTS, indent=2))
        sys.exit(1)
    write_config(vault, switch=True)
    # An existing vault gets the same tools, inbox and hooks a fresh one does --
    # only ever the files it lacks. seeded=False: its documents were not written from
    # the templates here, so a merge base is recorded only for an untouched copy.
    seed_vault_workspace(vault, seeded=False)


# ---- structured events (0.15.0) ---------------------------------------------------
#
# Lifecycle operations emit one gt_events.py event each, through the PLUGIN's copy of
# the tool (the vault's own copy may predate safe_emit). Never under --dry-run, never
# for an operation that did not happen, and never able to fail the operation: a
# failure is one stderr line, and the JSON on stdout is unchanged.

def _emit_event(vault: Path, kind: str, item: str, **kw):
    if DRY_RUN:
        return None
    try:
        import importlib.util
        sys.dont_write_bytecode = True
        tool = TEMPLATES_DIR / "tools" / "gt_events.py"
        spec = importlib.util.spec_from_file_location("_plugin_gt_events", str(tool))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:
        print(f"vault_init: {kind} event NOT recorded (gt_events.py unavailable: {exc})",
              file=sys.stderr)
        return None
    return mod.safe_emit(vault, kind, item, **kw)


def _rel_project(vault: Path, d: Path) -> str:
    """Project path under Projects/. Falls back to the folder name rather than raise:
    working out an event's label must never fail the operation it labels."""
    try:
        return d.resolve().relative_to(vault.resolve() / "Projects").as_posix()
    except (ValueError, OSError):
        return Path(d).name


def main():
    # Rehearsable on every mode -- core_explicit_vault_target. On a parent parser so
    # it parses AFTER the verb, which is where it gets typed; SUPPRESS so a subparser
    # cannot reset a value given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", "-n", action="store_true", default=argparse.SUPPRESS,
                        help="report every action as would-* and write nothing")
    parser = argparse.ArgumentParser(description="Golden Thread vault initializer",
                                     parents=[common])
    sub = parser.add_subparsers(dest="mode", required=True)

    p_fresh = sub.add_parser("fresh", parents=[common], help="Create a new vault scaffold")
    p_fresh.add_argument("--vault", required=True, type=Path)
    p_fresh.add_argument("--domain", required=True)
    p_fresh.add_argument("--no-config", action="store_true",
                         help="Build the vault without touching vault-config.json, settings.json "
                              "or ~/.claude/CLAUDE.md (used for the throwaway demo vault)")

    p_proj = sub.add_parser("create-project", parents=[common], help="Scaffold a new project in an existing vault")
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

    p_conn = sub.add_parser("connect", parents=[common], help="Point vault-config.json at an existing vault")
    p_conn.add_argument("--vault", required=True, type=Path)

    p_ren = sub.add_parser("rename-project", parents=[common], help="Rename a project and update every reference")
    p_ren.add_argument("--vault", required=True, type=Path)
    p_ren.add_argument("--from", dest="old", required=True)
    p_ren.add_argument("--to", dest="new", required=True)

    p_mrg = sub.add_parser("merge-project", parents=[common], help="Fold one project into another (nothing deleted)")
    p_mrg.add_argument("--vault", required=True, type=Path)
    p_mrg.add_argument("--from", dest="src", required=True)
    p_mrg.add_argument("--into", dest="dst", required=True)
    p_mrg.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")

    p_ret = sub.add_parser("archive-project", parents=[common],
                       help="Archive a project (stage: archived). Nothing is deleted.")
    p_ret.add_argument("--vault", required=True, type=Path)
    p_ret.add_argument("--slug", required=True)
    p_ret.add_argument("--reason", default="Superseded.")
    p_ret.add_argument("--date", default=None)

    p_core = sub.add_parser("install-core-rules", parents=[common],
                            help="Establish the Core-rule tier in an existing vault and wire the hooks")
    p_core.add_argument("--vault", required=True, type=Path)
    p_core.add_argument("--no-hooks", action="store_true",
                        help="Copy the module but do not touch settings.json")
    p_core.add_argument("--settings", type=Path, default=None,
                        help="Settings file to wire (default: ~/.claude/settings.json)")

    args = parser.parse_args()
    global DRY_RUN
    DRY_RUN = getattr(args, "dry_run", False)

    if args.mode == "fresh":
        cmd_fresh(args.vault, args.domain, no_config=args.no_config)
    elif args.mode == "create-project":
        tags = [t.strip() for t in args.tags.split(",")] if args.tags else []
        made = cmd_create_project(args.vault, args.name, args.title, tags, args.parent,
                                  args.runbook, args.project_dir, args.topology,
                                  args.repo_url, args.fleet, args.domain)
        if made is not None:
            rel = _rel_project(args.vault, made)
            _emit_event(args.vault.resolve(), "create", "Projects/" + rel,
                        to="Projects/" + rel, project=rel,
                        note=(f"create-project {rel}" + (f": {' '.join(args.title.split())}"
                                                       if args.title else ""))[:120])
    elif args.mode == "connect":
        cmd_connect(args.vault)
    elif args.mode == "merge-project":
        done = cmd_merge_project(args.vault, args.src, args.dst,
                                 args.date or __import__("datetime").date.today().isoformat())
        if done:
            src, dst = (_rel_project(args.vault, d) for d in done)
            _emit_event(args.vault.resolve(), "merge", "Projects/" + src,
                        frm="Projects/" + src, to="Projects/" + dst, project=dst,
                        note=f"merge-project {src} into {dst}")
    elif args.mode == "archive-project":
        done = cmd_archive_project(args.vault, args.slug, args.reason,
                                   args.date or __import__("datetime").date.today().isoformat())
        if done is not None:
            rel = _rel_project(args.vault, done)
            _emit_event(args.vault.resolve(), "archive", "Projects/" + rel, project=rel,
                        note=f"archive-project: {' '.join(args.reason.split())}"[:120])
    elif args.mode == "rename-project":
        done = cmd_rename_project(args.vault, args.old, args.new)
        if done:
            old, new = (_rel_project(args.vault, d) for d in done)
            _emit_event(args.vault.resolve(), "rename", "Projects/" + new,
                        frm="Projects/" + old, to="Projects/" + new, project=new,
                        note=f"rename-project {old} -> {new}")
    elif args.mode == "install-core-rules":
        install_core_rules(args.vault.resolve(), wire_hooks=not args.no_hooks,
                           settings_path=args.settings)

    print(json.dumps(RESULTS, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
