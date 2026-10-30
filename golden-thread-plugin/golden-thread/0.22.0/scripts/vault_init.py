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
  finish          --vault <path> [--project <slug>] [--doctor-json <file>]   (prints text; writes nothing)
  install-core-rules --vault <path> [--no-hooks] [--settings <file>]
  rename-project  --vault <path> --from <old-slug> --to <new-slug>\n  merge-project   --vault <path> --from <slug> --into <slug>\n  archive-project --vault <path> --slug <slug> [--reason <text>] [--move] [--force]

Exit codes:
  0 = success: every recorded row is created / skipped / updated / would-*
  1 = THE RUN DID NOT DO ALL IT WAS ASKED: at least one row in the JSON output is an
      `error` or a `conflict` — a project that does not exist, a rename whose
      destination is taken, a template missing from the plugin, `connect` at a path
      with no vault. The row says which; the code only says "read the output".
  2 = usage error (argparse: an unknown mode, a missing --vault)
  3 = vault-config.json already points at a different vault (fresh only —
      connect switches it)

Through 0.16.4 the first two were wrong in the same direction. "1 = usage error" named
something the code never did (argparse exits 2), and main() ended in a bare sys.exit(0),
so `rename-project` on a project that does not exist printed {"action": "error", ...} and
exited 0 — telling install.sh, the skills and every other caller that it had succeeded
(corrected 2026-09-18).

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


def w_write(path: Path, text: str, action="created", note="", exclusive=False, **kw):
    """Write a file unless rehearsing. **kw swallows the encoding= the call sites
    pass; the text is always written as UTF-8. exclusive=True creates the file only if nothing
    -- not even a dangling symlink -- is there (open mode "x", O_EXCL): it never follows a link."""
    path = Path(path)
    if dry(action, path, note):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n": text mode on Windows writes every "\n" as CRLF, so a fresh vault built
    # there differed byte for byte from the templates it was built from -- every tool and
    # Core rule read as "modified locally" and the doc merges were held. (Not write_text's
    # newline=: that arrived in Python 3.10 and gt supports 3.8.)
    with open(path, "x" if exclusive else "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return True


def w_write_atomic(path: Path, text: str, action="updated", note=""):
    """w_write for a file the user owns and other programs read (settings.json): the text goes to a
    temp file beside it and replaces it in one step, so a failure mid-write leaves the original. The
    file's mode is kept, and a symlinked file (a dotfiles repo) is replaced at its target, not turned
    into a plain file."""
    path = Path(path)
    if dry(action, path, note):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    import stat
    import tempfile
    target = os.path.realpath(str(path))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".gt-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        try:
            os.chmod(tmp, stat.S_IMODE(os.stat(target).st_mode))
        except OSError:
            pass                                             # a new file keeps mkstemp's 0600
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
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
    # lexists, not exists: a DANGLING symlink "does not exist" to exists(), and open(..., "w")
    # would then follow it and create its target wherever it points (0.20.2 re-review M2). The
    # create itself is exclusive, so it cannot follow a link planted between the check and it.
    if os.path.lexists(path):
        record("skipped", path, "file already exists")
    else:
        w_mkdir(path.parent, parents=True, exist_ok=True)
        try:
            w_write(path, content, encoding="utf-8", exclusive=True)
        except FileExistsError:
            record("skipped", path, "file already exists")
            return
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


def hook_command(script):
    """The settings.json command for the hook script `script`.

    Windows (0.19.2): Claude Code runs it through Git Bash, where an unquoted C:\\Users\\...
    loses every backslash ("C:Users.claude...: command not found") -- the Core-rule hooks were
    all dead while the wiring check said PASS. So "/" separators, quoted when the path needs
    it ("C:/Users/John Smith/..."). Everywhere else: the plain path, as before."""
    if os.name != "nt":
        return str(script)
    import shlex
    return shlex.quote(script.as_posix())


def _is_old_gt_copy(command, script, vault):
    """True for a settings.json command that runs an OLD copy of gt's hook `script`: the same file name
    inside the vault or under a core-rules folder (where hooks lived before they moved to
    ~/.claude/golden-thread/hooks). The name must match whole: a user's my_guard_vault_writes.sh, or a
    script of the same name anywhere else, is theirs and stays."""
    if not isinstance(command, str):
        return False
    import shlex
    # shlex.split (POSIX mode) treats "\" as an escape character and corrupts a native Windows
    # path; normalised first, same as _is_own_command and gt_uninstall.py: gt's own wiring always
    # writes forward slashes, so this only bites a stale backslash-spelled old copy.
    text = command.replace("\\", "/")
    try:
        toks = shlex.split(text)
    except ValueError:
        toks = [text.strip("'\"")]
    if len(toks) != 1:
        return False                                         # a command with arguments is not gt's wiring
    p = toks[0]
    if p.rsplit("/", 1)[-1] != script.name:
        return False
    parts = p.split("/")
    if "core-rules" in parts[:-1]:
        return True
    try:
        root = os.path.realpath(str(vault)).replace("\\", "/").rstrip("/") + "/"
        return os.path.realpath(p).replace("\\", "/").startswith(root)
    except (OSError, ValueError):
        return False


def _own_path_token(tok, script):
    """True when `tok` is an absolute path to `script`, by basename and by its parent folder's
    realpath (so a symlinked parent, or a lookalike folder of the same name, is told apart)."""
    p = os.path.expanduser(tok)
    if not os.path.isabs(p) or os.path.basename(p.replace("\\", "/")) != script.name:
        return False
    try:
        return (os.path.normcase(os.path.realpath(os.path.dirname(p)))
                == os.path.normcase(os.path.realpath(str(script.parent))))
    except (OSError, ValueError):
        return False


def _is_own_command(command, script):
    """True when a settings.json command runs gt's own hook `script` (a path inside gt's hooks folder), in any
    spelling of that same path: ~/..., $HOME/..., quoted, with surrounding space, or through a symlinked
    parent folder. A same-named script in any other folder, or the same folder name reached by a lookalike,
    is the user's. Two shapes count as gt's own wiring, and nothing else does:
      - a bare path and nothing else (`.sh` hooks, and `.py` hooks everywhere but native Windows)
      - gt's own Windows `.py` invocation (`gt_components.py: hook_python`): an interpreter, then an
        optional `-X utf8` pair and an optional `-I`, then `-B`, then the script path, then any args.
    Used to take out only gt's handler from a block and leave the user's handlers beside it."""
    if not isinstance(command, str):
        return False
    import shlex
    home = str(Path.home()).replace("\\", "/")
    # shlex.split (POSIX mode, below) treats "\" as an escape character and corrupts a native
    # Windows path; gt's own wiring always writes forward slashes, so this only bites a stale or
    # foreign-looking entry someone wrote with native backslashes -- which must still be matched.
    text = command.strip().replace("\\", "/").replace("${HOME}", home).replace("$HOME", home)
    try:
        toks = shlex.split(text)
    except ValueError:
        return False
    if len(toks) == 1:
        return _own_path_token(toks[0], script)
    i = 1
    while i < len(toks) and toks[i] in ("-I", "-X"):
        i += 2 if toks[i] == "-X" else 1                     # "-X" takes a value ("utf8"); "-I" does not
    if i < len(toks) and toks[i] == "-B":
        i += 1
    return i < len(toks) and _own_path_token(toks[i], script)


class _SettingsUnreadable(Exception):
    """settings.json could not be read the way hook wiring needs; the message is one line and quotes no content."""


def _no_constant(name):
    raise _SettingsUnreadable("it holds %s, which is not valid JSON and cannot be written back safely" % name)


def _finite_float(text):
    value = float(text)
    if value in (float("inf"), float("-inf")):
        raise _SettingsUnreadable("it holds a number too large to keep (%s...), which would be written back as "
                                  "Infinity, invalid JSON" % text[:12])
    return value


def _load_settings(path):
    """-> the parsed settings.json, {} when absent. Refuses (_SettingsUnreadable) what it could not write back
    as valid JSON (NaN, Infinity, 1e999) and turns an unreadable, non-UTF-8 or absurdly nested file into one
    short line; the file is never touched."""
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=_no_constant, parse_float=_finite_float)
    except _SettingsUnreadable:
        raise
    except json.JSONDecodeError as exc:
        raise _SettingsUnreadable("it is not valid JSON (%s at line %d)" % (exc.msg, exc.lineno))
    except UnicodeDecodeError:
        raise _SettingsUnreadable("it is not UTF-8 text")
    except RecursionError:
        raise _SettingsUnreadable("it is nested too deeply to read")
    except (ValueError, OverflowError) as exc:
        raise _SettingsUnreadable("a value in it could not be read (%s)" % type(exc).__name__)
    except OSError as exc:
        raise _SettingsUnreadable("it could not be opened (%s)" % (exc.strerror or type(exc).__name__))


def _write_settings(settings, data):
    """Write settings.json atomically as strict JSON. -> True when written, False when it could not be (an error
    row says why and the file is as it was)."""
    try:
        text = json.dumps(data, indent=2, allow_nan=False) + "\n"
    except (ValueError, RecursionError):
        record("error", settings, "settings.json was not written: the result would not be valid JSON; left unchanged")
        return False
    try:
        w_mkdir(settings.parent, parents=True, exist_ok=True)
        w_write_atomic(settings, text)
    except OSError as exc:
        record("error", settings, "could not write settings.json (%s); left unchanged -- fix the permissions, "
                                  "then run this again" % (exc.strerror or type(exc).__name__))
        return False
    return True


def _settings_shape_problem(data):
    """A short description of how settings.json is shaped wrongly for hook wiring, or None. Never
    quotes the file: it may hold tokens in env or headers."""
    if not isinstance(data, dict):
        return "the top level is not an object"
    if "hooks" not in data:
        return None
    hooks = data["hooks"]
    if not isinstance(hooks, dict):
        return "'hooks' is not an object"
    for event, blocks in hooks.items():
        if not isinstance(blocks, list):
            return "hooks.%s is not a list" % event
        for b in blocks:
            if not isinstance(b, dict):
                return "an entry under hooks.%s is not an object" % event
            hs = b.get("hooks", [])
            if not isinstance(hs, list):
                return "'hooks' inside an entry of hooks.%s is not a list" % event
            for h in hs:
                if not isinstance(h, dict):
                    return "a handler under hooks.%s is not an object" % event
                if "command" in h and not isinstance(h["command"], str):
                    return "a handler command under hooks.%s is not text" % event
    return None


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
    #
    # 0.17.0: the default moved to the vault ROOT. Core rules govern every project, so
    # filing them under one project asserted an ownership that was never real. Existing
    # vaults are unaffected here — _resolve_core_rules finds them wherever they are, and
    # the `core-rules-root` migration in gt_upgrade relocates them deliberately rather
    # than this function relocating them as a side effect of an install.
    dest = _resolve_core_rules(vault) or _default_core_rules(vault)
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
        rel = dest.relative_to(vault).as_posix()
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
        ("PreToolUse", "guard_session_claims.sh", "Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell"),
        ("PreToolUse", "guard_vault_writes.sh", "Bash|PowerShell"),
        ("PreToolUse", "guard_test_before_commit.sh", "Bash"),
    ]
    # The handler fields beside each command (timeout, `if`, onFailure), read from the same registrations.
    # A copy of this table is the fallback, kept equal to HOOK_REGISTRATIONS by a test. The settings route
    # used to write only {"timeout": 10}, so a guard that hung failed open here and blocked on the plugin
    # route (obsidian-36, 2026-10-27); both routes now carry what the registration says.
    FALLBACK_EXTRAS = {
        ("UserPromptSubmit", "inject_core_rules.sh", None): {"timeout": 10},
        ("SessionStart", "inject_core_rules.sh", "compact"): {"timeout": 10},
        ("Stop", "validate_response.sh", None): {"timeout": 10},
        ("PreToolUse", "guard_session_claims.sh", "Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell"):
            {"timeout": 10, "onFailure": "block"},
        ("PreToolUse", "guard_vault_writes.sh", "Bash|PowerShell"): {"timeout": 10, "onFailure": "block"},
        ("PreToolUse", "guard_test_before_commit.sh", "Bash"):
            {"timeout": 10, "if": "Bash(*git*)", "onFailure": "block"},
    }
    extras = FALLBACK_EXTRAS
    mod = None
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
        extras = {(r["event"], r["script"], r.get("matcher")):
                  {k: r[k] for k in ("timeout", "if", "onFailure") if r.get(k) is not None}
                  for r in mod.HOOK_REGISTRATIONS if str(r.get("owner", "")).startswith("vault_init.py")}
        order_blocks = lambda hooks: mod.order_hook_blocks(  # noqa: E731
            hooks, [(r["event"], r["script"]) for r in mod.HOOK_REGISTRATIONS],
            str(hooks_dir))
    except Exception:
        pairs = FALLBACK
        extras = FALLBACK_EXTRAS
        order_blocks = lambda hooks: False  # noqa: E731
    wanted = [(event, hooks_dir / script, matcher) for event, script, matcher in pairs]
    command = hook_command
    missing = [str(v) for _, v, _ in wanted if not v.is_file()]
    if missing:
        record("error", hooks_dir,
               "hook scripts not installed — run install.sh (they ship with the plugin)")
        return

    settings = settings_path or (Path.home() / ".claude" / "settings.json")
    try:
        data = _load_settings(settings)
    except _SettingsUnreadable as exc:
        record("error", settings, f"could not read settings.json: {exc}; left unchanged -- fix it by hand, "
                                  "then run this again")
        return
    problem = _settings_shape_problem(data)
    if problem:
        record("error", settings, f"settings.json is not shaped the way hook wiring needs ({problem}); "
                                  "left unchanged -- fix it by hand, then run this again")
        return

    # THE PLUGIN ROUTE (0.21.0): the gt plugin ships hooks/hooks.json, whose entries run the
    # command in ~/.claude/golden-thread/wired/<event>__<script> when that file exists. So
    # wiring an enforcement hook is writing its file; settings.json is written only to take
    # out a gt entry for the same script, which would otherwise run twice. The route is
    # install.sh's (GT_HOOK_MODE, exported for this step), else what this machine is wired
    # for (the wired dir exists only on the plugin route). A gt_components too old to know
    # the plugin route (mod None, or no write_wired) keeps the settings route.
    route = (os.environ.get("GT_HOOK_MODE") or "").strip().lower()
    if mod is not None and hasattr(mod, "write_wired"):
        if route not in ("plugin", "settings"):
            route = mod.current_hook_mode(str(Path.home()))
        if route == "plugin":
            _wire_enforcement_plugin(mod, wanted, data, settings, command)
            return

    data.setdefault("hooks", {})
    changed = False
    for event, script, matcher in wanted:
        blocks = data["hooks"].setdefault(event, [])
        # Drop any previous entry that pointed at a vault-internal copy of this hook. Only a block that THIS
        # removal empties is dropped: a user's block with no `hooks` key, or an empty one of theirs, stays.
        emptied = set()
        for b in blocks:
            if isinstance(b, dict) and isinstance(b.get("hooks"), list):
                kept_h = [h for h in b["hooks"]
                          if not (isinstance(h, dict) and _is_old_gt_copy(h.get("command", ""), script, vault)
                                  and h.get("command") != command(script))]
                if b["hooks"] and not kept_h:
                    emptied.add(id(b))
                b["hooks"] = kept_h
        blocks[:] = [b for b in blocks if id(b) not in emptied]
        # Already-wired means wired WITH THE RIGHT MATCHER. A block carrying the correct
        # command under the wrong matcher fires on the wrong sources, which for the
        # compaction entry would mean re-injecting the Core rules on every session start
        # (harmless but wasteful) or on none (the hole this closes, still open while
        # reporting success). So an existing entry whose matcher disagrees is rewritten,
        # not skipped.
        # "Ours" is gt's own handler in any spelling of its path (_is_own_command), so a ~/ or $HOME form
        # is rewritten to the one canonical entry and the guard never runs twice.
        def own(h, script=script):
            return isinstance(h, dict) and _is_own_command(h.get("command"), script)
        existing = [b for b in blocks if isinstance(b, dict) and any(own(h) for h in b.get("hooks", []))]
        hook = {"type": "command", "command": command(script),
                **extras.get((event, script.name, matcher), {"timeout": 10})}
        # ...and with the right handler fields: an entry from before 0.22.0 has the command and the
        # matcher but no onFailure or `if`, and "already wired" must not leave it that way. Exactly one
        # handler of gt's own counts as wired; two is a guard that runs twice.
        n_own = sum(1 for b in existing for h in b.get("hooks", []) if own(h))
        if existing and n_own == 1 and all(
                b.get("matcher") == matcher and any(h == hook for h in b.get("hooks", []) if own(h))
                for b in existing):
            record("skipped", settings, f"{event} hook already wired")
            continue
        placed = False
        if existing:
            record("rewired", settings,
                   f"{event}/{script.name} matcher was {existing[0].get('matcher')!r}, "
                   f"now {matcher!r}; handler fields now {sorted(k for k in hook if k not in ('type', 'command'))}")
            # Take out only gt's own handler from each block it sits in: a user's hook in the same block
            # stays, in its place, with all its keys. gt's rewritten handler goes back where the first one
            # was when that block has the right matcher, else into a block of its own.
            touched = {id(b) for b in existing}
            for b in existing:
                kept = []
                for h in b.get("hooks", []):
                    if own(h):
                        if not placed and b.get("matcher") == matcher:
                            kept.append(hook)
                            placed = True
                        continue
                    kept.append(h)
                b["hooks"] = kept
            blocks[:] = [b for b in blocks if not (id(b) in touched and not b["hooks"])]
        if not placed:
            block = {"hooks": [hook]}
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
        if _write_settings(settings, data):
            record("updated", settings, "wired Core-rule hooks at the stable path")


def _wire_enforcement_plugin(mod, wanted, data, settings, command):
    """The plugin route of install_core_rules: one wired file per enforcement hook."""
    rows = [{"event": event, "script": script.name, "matcher": matcher,
             "owner": "vault_init.py install-core-rules", "command": command(script)}
            for event, script, matcher in wanted]
    home = str(Path.home())
    if DRY_RUN:
        for r in rows:
            dry("updated", Path(mod.wired_dir(home)) / mod.wired_name(r["event"], r["script"]),
                "wired Core-rule hook (plugin route)")
        return
    try:
        done = mod.write_wired(rows, home)
    except (OSError, ValueError) as exc:
        record("error", mod.wired_dir(home), f"could not write the wired hooks: {exc}")
        return
    if done:
        record("updated", mod.wired_dir(home),
               "wired Core-rule hooks for the gt plugin's hooks.json: " + ", ".join(done))
    else:
        record("skipped", mod.wired_dir(home), "Core-rule hooks already wired (plugin route)")
    # A settings.json entry running the same script would make it run twice.
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return
    scripts = [script for _e, script, _m in wanted]
    gone = []
    for event, blocks in list(hooks.items()):
        if not isinstance(blocks, list):
            continue
        emptied = set()
        for b in blocks:
            if isinstance(b, dict) and isinstance(b.get("hooks"), list):
                keep = [h for h in b["hooks"]
                        if not (isinstance(h, dict) and any(_is_own_command(h.get("command"), s) for s in scripts))]
                if len(keep) != len(b["hooks"]):
                    gone.append(event)
                    if not keep:
                        emptied.add(id(b))                   # only a block this removal empties; a user's empty one stays
                    b["hooks"] = keep
        blocks[:] = [b for b in blocks if id(b) not in emptied]
        if not blocks and event in gone:
            del hooks[event]
    if gone:
        if not hooks:
            del data["hooks"]
        if _write_settings(settings, data):
            record("updated", settings, "removed %d gt Core-rule hook entr%s now run by the gt "
                   "plugin's hooks.json" % (len(gone), "y" if len(gone) == 1 else "ies"))


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


def _default_core_rules(vault: Path):
    """Where a FRESH vault's Core rules go: the vault root, since 0.17.0.

    Read from gt_paths so there is exactly one definition of the default; the fallback
    spells it only for the case where gt_paths cannot be imported at all.
    """
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from gt_paths import default_core_rules
        return default_core_rules(vault)
    except Exception:
        return vault / "core-rules"


def _mentions_slug(md: Path, slug: str) -> bool:
    try:
        text = md.read_text(encoding="utf-8")
    except Exception:
        return False
    return (f"Projects/{slug}/" in text or f"({slug}/" in text or f"`{slug}`" in text
            or re.search(rf"^\s*(slug|parent):\s*{re.escape(slug)}\s*$", text, flags=re.M) is not None)


def _is_inside(md: Path, folder: Path) -> bool:
    try:
        md.relative_to(folder)
        return True
    except ValueError:
        return False


def _repoint_project(text: str, old: str, new: str, old_rel: str, new_rel: str, in_project: bool):
    """-> (text, kinds): `text` with every reference to project `old` re-pointed at `new`,
    `kinds` naming each spelling that changed. The spellings: `Projects/old/...` paths (in a
    wikilink or not), markdown relative links (`(old/x.md)`, `(../old/README.md)`), bare and
    path-style wikilinks (`[[old]]`, `[[old|label]]`, `[[old#h]]`, `[[Projects/old]]`), `old` in
    backticks, `slug:` / `parent:` frontmatter, and the H1 of a file in the project when it
    equals the old name. Until 0.22.0 only the first, the backticks and the frontmatter were
    rewritten, so a renamed project kept dangling markdown links, bare wikilinks and its old
    heading. A longer name that merely starts with `old` is never touched."""
    kinds = []

    def step(kind, pattern, repl, flags=0):
        nonlocal text
        out = re.sub(pattern, repl, text, flags=flags)
        if out != text:
            kinds.append(kind)
            text = out

    o, n = re.escape(old), new
    step("path", rf"Projects/{o}/", f"Projects/{n}/")
    step("path", rf"(\[\[Projects/){o}(?=[\]|#])", rf"\g<1>{n}")
    step("markdown link", rf"(\](?:\()(?:\.{{1,2}}/)*){o}(?=[/)#])", rf"\g<1>{n}")
    step("markdown link", rf"\({o}/", f"({n}/")
    step("markdown link", rf"\[{o}\](?=\()", f"[{n}]")      # a label that is just the name
    # a sub-project may be linked by `parent/child` or by the bare child name
    for a, b in dict.fromkeys(((old_rel, new_rel), (old, new))):
        step("wikilink", rf"(\[\[){re.escape(a)}(?=[\]|#/])", rf"\g<1>{b}")
    step("backtick", rf"`{o}`", f"`{n}`")
    step("frontmatter", rf"^(\s*slug:\s*){o}\s*$", rf"\g<1>{n}", re.M)
    # `parent:` too. Missing it silently orphans every sub-project: the folders
    # move with the parent, but their frontmatter keeps naming a slug that no
    # longer exists, so any Dataview grouping by parent quietly drops them.
    step("frontmatter", rf"^(\s*parent:\s*){o}\s*$", rf"\g<1>{n}", re.M)
    if in_project:
        step("heading", rf"^(#[ \t]+){o}[ \t]*$", rf"\g<1>{n}", re.M)
    return text, kinds


def _rewrite_kinds(md: Path, vault: Path, old, new, old_rel, new_rel, src_dir: Path):
    try:
        text = md.read_text(encoding="utf-8")
    except Exception:
        return []
    return _repoint_project(text, old, new, old_rel, new_rel, _is_inside(md, src_dir))[1]


def cmd_rename_project(vault: Path, old: str, new: str):
    """Rename a project and update every reference to it.

    Renames happen — projects get redefined, merged, retired. This makes that a
    supported operation rather than a manual sweep that misses something.
    """
    vault = vault.resolve()
    projects = vault / "Projects"
    # The shared resolver (0.18.1), not `rglob(old)[0]`: that took any folder of that name
    # and silently picked one of two same-named sub-projects.
    src_dir = _proj_dir(vault, old)
    if src_dir is None:
        return                                  # _proj_dir recorded why
    dst_dir = src_dir.parent / new
    if dst_dir.exists():
        record("conflict", dst_dir, "destination already exists")
        return
    spool_root = _decisions_spool_root(vault)
    new_spool = spool_root / dst_dir.relative_to(projects).as_posix()
    if (spool_root / src_dir.relative_to(projects).as_posix()).is_dir() and new_spool.exists():
        record("conflict", new_spool, "decisions spool already exists at the new name")
        return

    # What would be rewritten, per file: the same function serves the rehearsal and the real
    # run, so a --dry-run report is the real run's report.
    src_rel = src_dir.relative_to(projects).as_posix()
    dst_rel = dst_dir.relative_to(projects).as_posix()

    if DRY_RUN:
        # Path.rename below is outside the dry-run boundary (w_write): until 0.14.0 a
        # `--dry-run` rename really renamed the folder. Rehearse what would change, stop.
        refs = 0
        for md in vault.rglob("*.md"):
            if ".git" in md.parts:
                continue
            kinds = _rewrite_kinds(md, vault, old, new, src_rel, dst_rel, src_dir)
            if kinds:
                refs += 1
                record("would-update", md, "would re-point: " + ", ".join(kinds))
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
            original = md.read_text(encoding="utf-8")
        except Exception:
            continue
        text, kinds = _repoint_project(original, old, new, src_rel, dst_rel,
                               _is_inside(md, dst_dir))
        if text != original:
            w_write(md, text, encoding="utf-8")
            record("updated", md, "re-pointed: " + ", ".join(kinds))
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
            rel = core.relative_to(vault).as_posix()
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
    old = root / old_dir.relative_to(projects).as_posix()
    new = root / new_dir.relative_to(projects).as_posix()
    if not old.is_dir():
        return True
    if new.exists():
        record("conflict", new, "decisions spool already exists at the new name")
        return False
    if dry("move", new, f"decisions spool moved from {old.relative_to(root).as_posix()}"):
        return True
    new.parent.mkdir(parents=True, exist_ok=True)
    old.rename(new)
    record("updated", new, f"decisions spool moved from {old.relative_to(root).as_posix()}")
    return True


def _rerender_decisions(vault: Path, proj: Path):
    """Re-render decisions.md for `proj` and every sub-project with a spool."""
    root = _decisions_spool_root(vault)
    base = root / proj.relative_to(vault / "Projects").as_posix()
    if not base.is_dir() or not _adr_tool(vault).is_file():
        return
    for d in [base] + sorted(x for x in base.rglob("*") if x.is_dir()):
        rel = d.relative_to(root).as_posix()
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
    srel, drel = src.relative_to(projects).as_posix(), dst.relative_to(projects).as_posix()
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
    absent_parents, _p = [], dspool.parent       # folders the rehearsal below may create on its way
    while not _p.exists() and _p != vault:
        absent_parents.append(_p)
        _p = _p.parent
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
            for _p in absent_parents:
                if _p.is_dir() and not any(_p.iterdir()):
                    _p.rmdir()
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
                    fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o644)
                except FileExistsError:
                    clash = True           # someone allocated meanwhile: back out, recompute
                    break
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
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
    """The project folder `slug` names, via the vault tools' shared resolver
    (gt_spool.resolve_project, 0.18.1). Until 0.18.1 this took the FIRST `rglob(slug)`
    directory -- any folder of that name (a `memory/`, a `handoff/`), and silently one of
    two same-named sub-projects. Records the reason and returns None when `slug` is unknown
    or ambiguous; creates nothing."""
    tools = str(TEMPLATES_DIR / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import gt_spool
    try:
        return vault / "Projects" / gt_spool.resolve_project(vault, slug,
                                                             allow_unregistered=True)
    except gt_spool.ProjectNotFound as exc:
        record("error", vault, f"project not found: {exc}")
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


_HISTORY_TOPS = ("Sources", "Archive", "Daily Notes", ".git")


def _merge_link_sweep(vault: Path, src: Path, dst: Path, src_slug: str, dst_slug: str, apply: bool):
    """Re-point the bare wikilinks that name a merged-away project (`[[src]]`, `[[src|label]]`,
    `[[src#h]]`, `[[Projects/src]]`, and `[[parent/src]]` for a sub-project) at the project it was
    folded into, and report every file with how many links it changed. 0.22.0: after a merge the
    source is a tombstone, so such a link still resolved, but to a page that only says "merged".
    Left alone, as history: `Sources/`, `Archive/`, `Daily Notes/`, `log.md`, the tombstone and what
    the merge archived verbatim. `[[Projects/src/x]]` paths and a longer name that merely starts
    with `src` are never touched. -> [(file, links)]."""
    projects = vault / "Projects"
    s_rel, d_rel = src.relative_to(projects).as_posix(), dst.relative_to(projects).as_posix()
    names = list(dict.fromkeys((s_rel, src_slug)))
    pat = re.compile(r"\[\[((?:Projects/)?)(%s)(?=[\]|#])" % "|".join(re.escape(n) for n in names))
    out = []
    for md in sorted(vault.rglob("*.md")):
        rel = md.relative_to(vault)
        if rel.parts[0] in _HISTORY_TOPS or rel.as_posix() == "log.md" or ".git" in md.parts:
            continue
        if _is_inside(md, src) or _is_inside(md, dst / f"merged-{src_slug}") \
                or md.parent.name.startswith(f"merged-{src_slug}"):
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new, n = pat.subn(lambda m: "[[" + m.group(1) + (d_rel if m.group(2) == s_rel else dst_slug), text)
        if n:
            out.append((md, n))
            if apply:
                w_write(md, new, encoding="utf-8")
    return out


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
        return                                  # _proj_dir recorded which, and why
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
        for md, n in _merge_link_sweep(vault, src, dst, src_slug, dst_slug, False):
            record("would-update", md, f"would re-point {n} link(s) [[{src_slug}]] -> [[{dst_slug}]]")
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

    sroot = vault / "Projects" / "golden-thread" / "spool" / "decisions" / src.relative_to(
        vault / "Projects").as_posix()
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
    (src / "README.md").write_bytes((
        f"---\ntype: project\nslug: {src_slug}\ndomain: merged\nstage: merged\n"
        f"merged_into: {dst_slug}\nmerged: {today}\ntombstone: true\nredirect: {dst_slug}\ntags: [merged]\n---\n\n"
        f"# {src_slug} — merged into {dst_slug}\n\n"
        f"> Merged into [{dst_slug}](../{dst_slug}/) on {today}. This tombstone stays so that\n"
        f"> notes and links written before the merge still lead somewhere.\n\n"
        f"Its memory notes kept their filenames and moved to `{dst_slug}/memory/`, so existing\n"
        f"`[[wikilinks]]` still resolve. Its `idea.md` is preserved verbatim at\n"
        f"`{dst_slug}/memory/idea_{src_slug.replace('-', '_')}.md`.\n"
        + (f"Its open tasks moved to `{dst_slug}/README.md`.\n" if tasks else "")
        + (f"Everything else it held — its old README, CLAUDE.md and any other files — is kept\n"
           f"verbatim at `{dst_slug}/{archive.name}/`.\n" if archive else "")).encode("utf-8"))
    record("updated", src / "README.md", "replaced with a merge tombstone")
    for md, n in _merge_link_sweep(vault, src, dst, src_slug, dst_slug, True):
        record("updated", md, f"re-pointed {n} link(s) [[{src_slug}]] -> [[{dst_slug}]]")

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
            rel = core.relative_to(vault).as_posix()
            if cfg.get("core_rules_path") != rel:
                cfg["core_rules_path"] = rel
                w_write(cfg_path, json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
                record("updated", cfg_path, f"core_rules_path = {rel}")
        except Exception:
            pass
    return src, dst


def _open_work(vault: Path, proj: Path) -> list:
    """One line per open task (shelved p:: 7 and done ones excepted, as gt_close counts them,
    deferred ones included) and per open or deferred handoff in `proj` and its sub-projects."""
    tools = str(TEMPLATES_DIR / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import gt_task
    import gt_tasks
    import gt_handoff_status as H
    projects = vault / "Projects"
    out = []
    for slug, path in gt_task.readmes(vault):
        if slug == "inbox" or not (_is_inside(path, proj)):
            continue
        for r in gt_task.tasks_in(slug, path):
            if r["p"] < gt_tasks.SHELVED_P:
                out.append("task %s [P%d]: %s" % (r["id"], r["p"], r["text"]))
    me = proj.relative_to(projects).as_posix()
    for r in H.all_handoffs(vault):
        if (r["project"] == me or r["project"].startswith(me + "/")) \
                and r["status"] in ("open", "deferred"):
            out.append("handoff %s [%s]" % (r["path"], r["status"]))
    return out


def cmd_archive_project(vault: Path, slug: str, reason: str, today: str, force: bool = False):
    """Archive a project. Nothing is deleted.

    Refuses while the project (or a sub-project) has an open task or a waiting handoff, and
    lists them; `force` archives anyway (0.22.0: it used to archive silently and the task
    list went on showing the tasks as ready).

    Follows the vault's own vocabulary: `archived` is the STAGE (CONVENTIONS.md
    defines it as "Retired or replaced"); `retire` is the LOG VERB for log.md.
    """
    vault = vault.resolve()
    proj = _proj_dir(vault, slug)
    if proj is None:
        return                                  # _proj_dir recorded why

    held = _open_work(vault, proj)
    if held and not force:
        record("conflict", proj, "not archived: %d open item(s) would be left behind; "
               "settle them (gt_close.py project) or archive anyway with --force" % len(held))
        for h in held:
            record("conflict", proj, h)
        return
    if held:
        record("updated", proj, "--force: archived past %d open item(s): %s"
               % (len(held), "; ".join(held)))

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



def _move_claims(vault: Path, rel_dir: str):
    """-> (holders, error) for LIVE claims, by any session but this one, on a file under rel_dir.

    Read through gt_demote's parser of the session files (plugin-side, so a vault on an older
    release still answers). A claim we cannot read is an error, and an error refuses the move:
    moving a folder is a write to every file in it at once (Core rule 1)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gt_demote                                        # noqa: PLC0415
        claims, unreadable = gt_demote._claim_rows(str(vault))
    except Exception as exc:                                    # noqa: BLE001
        return [], "session files could not be read (%s)" % exc.__class__.__name__
    if unreadable:
        return [], "%d session file(s) unreadable" % len(unreadable)
    me = None
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID"):
        if os.environ.get(var):
            me = os.environ[var].strip()
            break
    pre = rel_dir.rstrip("/") + "/"
    return sorted({sid for sid, status, last, files in claims
                   if sid != me and gt_demote._is_live(status, last)
                   and any(f.startswith(pre) for f in files)}), None


def _repoint(text: str, rel: str, is_index: bool) -> str:
    """Path references to Projects/<rel>/ re-pointed at Archive/<rel>/. Wikilinks by NAME
    ([[design]]) need nothing: Obsidian resolves them wherever the file now lives."""
    new = text.replace(f"Projects/{rel}/", f"Archive/{rel}/")
    new = re.sub(rf"\[\[Projects/{re.escape(rel)}(?=[\]|#])", f"[[Archive/{rel}", new)
    if is_index:     # Projects/README.md links its rows relatively: [Title](<slug>/)
        new = new.replace(f"]({rel}/", f"](../Archive/{rel}/")
    return new


def cmd_archive_move(vault: Path, slug: str):
    """--move (0.18.1, /gt:gt-complete --move): relocate an ARCHIVED project to Archive/<rel>/.

    Archiving stays in place by default -- the owner's decision (2026-10-01): nothing moves,
    every link keeps resolving. --move is the explicit request to clear Projects/. The folder
    itself is renamed (the write queue writes files, it cannot move a directory), after
    refusing if any LIVE session claims a file in it. Every OTHER reference is re-pointed
    through the write queue as a replace-file whose base is the bytes read here, drained at
    once -- so an edit made in between is escalated by the broker, never overwritten. Files
    the queue may not write (log.md, decisions.md, Sources/, spool/, Core rules) are history:
    they keep naming the old path, and the count is reported. The decisions spool stays
    where it is (spool/decisions/<rel>): it is the ADR history, and gt_adr reads it by name.
    """
    vault = vault.resolve()
    proj = _proj_dir(vault, slug)
    if proj is None:
        return None                             # _proj_dir recorded why
    rel = proj.relative_to(vault / "Projects").as_posix()
    dest = vault / "Archive" / rel
    if dest.exists():
        record("conflict", dest, "Archive destination already exists")
        return None
    holders, err = _move_claims(vault, f"Projects/{rel}")
    if err:
        record("error", proj, f"not moved -- could not check claims: {err}")
        return None
    if holders:
        record("conflict", proj, "not moved -- claimed by live session(s): "
               + ", ".join(holders) + " (Core rule 1)")
        return None
    # what would be re-pointed, decided BEFORE the move (the reads are of the current tree)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gt_write_queue as WQ                                  # noqa: PLC0415
    writes, history = [], 0
    for md in sorted(vault.rglob("*.md")):
        if ".git" in md.parts or md == proj or proj in md.parents:
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        r = md.relative_to(vault).as_posix()
        new = _repoint(text, rel, r == "Projects/README.md")
        if new == text:
            continue
        if WQ.path_refusal(vault, r) or len(new.encode("utf-8")) > WQ.MAX_CONTENT:
            history += 1
            continue
        writes.append({"path": r, "op": "replace-file", "content": new,
                       "hint": f"archive --move {rel}: re-point links"})
    inner = []                                   # links inside the project to itself
    for md in sorted(proj.rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new = _repoint(text, rel, False)
        if new != text:
            inner.append((md.relative_to(proj).as_posix(), new))
    if DRY_RUN:
        record("would-move", proj, f"-> Archive/{rel}/; {len(writes) + len(inner)} file(s) "
               f"re-pointed through the write queue, {history} left as history")
        return proj
    dest.parent.mkdir(parents=True, exist_ok=True)
    proj.rename(dest)
    record("updated", dest, f"moved from Projects/{rel}/")
    for sub, new in inner:
        writes.append({"path": f"Archive/{rel}/{sub}", "op": "replace-file", "content": new,
                       "hint": f"archive --move {rel}: re-point links"})
    if writes:
        results, note = WQ.submit(vault, writes, session=None, origin="session", drain=True)
        counts = {}
        for res in results:
            counts[res.get("decision")] = counts.get(res.get("decision"), 0) + 1
        record("updated", vault, "links re-pointed through the write queue: " +
               ", ".join(f"{n} {d}" for d, n in sorted(counts.items()))
               + (f" ({note})" if note else ""))
        for res in results:
            if res.get("decision") not in ("apply", "deduplicate"):
                record("skipped", vault / res["path"],
                       f"not re-pointed yet: {res.get('decision')} -- {res.get('reason', '')}")
    if history:
        record("skipped", vault, f"{history} file(s) the queue may not write (log.md, "
               f"decisions.md, Sources/, spool/) still name Projects/{rel}/ -- history")
    return dest


def seed_code_baseline(vault: Path):
    """Accept, with a reason, the two findings gt's own seeded files produce.

    WITHOUT THIS, EVERY NEW VAULT IS UNCOMMITTABLE. The pre-commit gate runs source validation
    over the staged diff; a fresh vault stages `.githooks/post-commit` and
    `.githooks/prepare-commit-msg`, both of which gt seeds and both of which the
    `sh-missing-nounset` rule flags -- so the FIRST commit in a brand-new vault was refused by
    gt's own rule against gt's own file. Found by the test suite immediately, which is the only
    reason it is not a shipping defect.

    The rule is right in general and wrong for those two files: they must fail OPEN, because a
    commit may never be blocked by attribution bookkeeping, and `set -u` turns an unset
    variable into a non-zero exit -- which in a commit hook means a refused commit. So the
    finding is ACCEPTED with that reason recorded, not suppressed and not worked around by
    weakening the rule. `reasons` exists because a baselined finding is invisible, and an
    accepted finding with nobody's reason attached is indistinguishable from a silenced one.
    """
    baseline = vault / ".gt" / "code-baseline.json"
    if baseline.exists():
        return                      # the owner's, from here on
    why = ("DELIBERATE: this hook must fail OPEN -- a commit may never be blocked by "
           "attribution bookkeeping -- and `set -u` turns an unset variable into a non-zero "
           "exit, which for a commit hook means a blocked commit. The rule is right in "
           "general and wrong here. Seeded by vault_init.")
    ensure_file(baseline, json.dumps({
        "accepted": [[".githooks/post-commit", "sh-missing-nounset", 1],
                     [".githooks/prepare-commit-msg", "sh-missing-nounset", 1]],
        "reasons": {".githooks/post-commit::sh-missing-nounset": why,
                    ".githooks/prepare-commit-msg::sh-missing-nounset": why},
        "_note": ("Each accepted finding SHOULD have an entry in `reasons`, keyed "
                  "\"path::rule\". An accepted finding with no reason is a silenced one, and "
                  "nobody can tell later which it was."),
    }, indent=2) + "\n")


def seed_daily_notes(vault: Path):
    """The daily-note template, the folder it writes into, and the Obsidian settings that
    point one at the other.

    All three, or none of it works. `gt-review` sweeps `<vault>/Daily Notes/` for open
    `- [ ]` lines carrying no `[[wikilink]]` -- but only if the folder exists, and the
    folder only fills if Obsidian knows which template to stamp into it. Seeding the
    template alone leaves a file nobody is ever handed: the inert-artifact failure this
    release spent a week finding in three other places (a `lint` slot wired to nothing,
    `selftest.sh` printing PASSED over failures, `gt_doctor` reading an exit code as a
    verdict).

    This is the ONLY place gt writes under `.obsidian/`, and it writes each file only
    when ABSENT. That boundary is deliberate: `.obsidian/` is another application's
    state, and an owner who already keeps daily notes somewhere else has a
    `daily-notes.json` saying so. Skipping an existing file means their setting wins and
    gt never silently relocates their journal -- the template still lands, and
    `gt-review` still finds the folder they actually use.
    """
    tmpl = TEMPLATES_DIR / "daily-note.md"
    if not tmpl.is_file():
        return
    ensure_file(vault / "Templates" / "Daily Note.md", tmpl.read_text(encoding="utf-8"))
    ensure_dir(vault / "Daily Notes")
    # Obsidian stores these as two separate core-plugin configs; the template is useless
    # without `templates.json` naming the folder it lives in.
    ensure_file(vault / ".obsidian" / "daily-notes.json", json.dumps({
        "folder": "Daily Notes",
        "format": "YYYY-MM-DD",
        "template": "Templates/Daily Note",
        "autorun": False,
    }, indent=2) + "\n")
    ensure_file(vault / ".obsidian" / "templates.json",
                json.dumps({"folder": "Templates"}, indent=2) + "\n")


GITIGNORE = "# Written by Golden Thread (vault_init.py). Python bytecode from the vault tools.\n__pycache__/\n*.pyc\n"


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
                "pp: 3\ntopology: local\nrelease_pipeline: planned\n"
                "tags: [infrastructure, memory, tooling]\n---\n\n"
                "# Golden Thread\n\n> The memory system itself: its Core rules, tools, sessions "
                "and pending edits live under this folder.\n\n## Tasks\n\n"
                "<!-- Format: - [ ] text [p:: 1|2|3|7+] [waiting:: user|agent|external|parked] "
                "[due:: YYYY-MM-DD] [since:: YYYY-MM-DD]\n     See CONVENTIONS.md > Priority. "
                "Rolled up into /TASKS.md -- do not edit that file by hand. -->\n")
    inbox = TEMPLATES_DIR / "INBOX.md"
    if inbox.exists():
        ensure_file(vault / "INBOX.md", inbox.read_text(encoding="utf-8"))
    # Python bytecode never belongs in the vault (0.20.1): running a vault tool as
    # `python3 tools/x.py` left Projects/golden-thread/tools/__pycache__/ untracked in git
    # status. Seeded only when the vault has no .gitignore -- an owner's own is never edited.
    ensure_file(vault / ".gitignore", GITIGNORE)

    seed_daily_notes(vault)
    seed_code_baseline(vault)

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
            # 0.21.0: LF in the repository and in every checkout. A Windows clone with git's
            # default core.autocrlf=true checked the vault out with CRLF, and frontmatter
            # written "---\r\n" was no longer frontmatter to the tools that read it.
            # `text=auto` leaves a binary file (a PDF in Sources/) alone. Only in a repo this
            # run created: in an existing one it would renormalise files already committed.
            ensure_file(vault / ".gitattributes",
                        "# Golden Thread: text is LF everywhere, including Windows checkouts.\n"
                        "* text=auto eol=lf\n")
        hooks_src = TEMPLATES_DIR / "githooks"
        # The starter install (install.sh --starter, phase 116) adds no git hooks path: it sets
        # GT_STARTER=1, and the vault's attribution and secrets hooks come with the full install.
        if hooks_src.is_dir() and os.environ.get("GT_STARTER") != "1":
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

    # The rollup is seeded like everything else here: only when ABSENT (0.20.1). `connect` runs
    # on every install, and regenerating an existing TASKS.md rewrote its "Generated" stamp and
    # receipt each time -- a re-install that changed nothing left TASKS.md and .tasks-digest
    # dirty in the vault's git status. Keeping it current is gt_tasks.py's job, run by the
    # skills that change tasks. -B: no __pycache__ in the vault's tools/.
    rollup = tools_dst / "gt_tasks.py"
    if rollup.exists() and not (vault / "TASKS.md").exists():
        r = subprocess.run([sys.executable, "-B", str(rollup), "--vault", str(vault)],
                           capture_output=True, text=True)
        record("created" if r.returncode == 0 else "error", vault / "TASKS.md",
               "generated by gt_tasks.py" if r.returncode == 0 else (r.stderr or "").strip()[-200:])
    elif rollup.exists():
        record("skipped", vault / "TASKS.md", "already present (gt_tasks.py keeps it current)")


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
    rel = proj.relative_to(vault / "Projects").as_posix()
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


def _resolve_parent(vault: Path, parent: str) -> str:
    """`parent`'s path under Projects/, via the vault tools' shared resolver; the name
    itself when it resolves to nothing (a parent created on the fly, as before)."""
    tools = str(TEMPLATES_DIR / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    try:
        import gt_spool
        return gt_spool.resolve_project(vault, parent)
    except Exception:
        return parent


def _manifest_id_lines(vault: Path) -> str:
    """id/created/updated lines for a note being created, per the vault's manifest id_policy;
    "" under the default policy, with no manifest, or if the reader is not beside this script."""
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        import gt_vault_manifest as vm
        return vm.new_note_frontmatter(vault)
    except Exception:                                           # a bad manifest never fails a create
        return ""


def cmd_create_project(vault: Path, slug: str, title: str = None, tags: list = None,
                       parent: str = None, runbook: bool = False, project_dir=None,
                       topology: str = None, repo_url: str = None, fleet: str = None,
                       domain: str = None, release_pipeline: str = None):
    vault = vault.resolve()
    display_title = title or slug
    tags = tags or []

    if parent:
        # The parent is a NAME too: resolved the way gt_adr resolves a project
        # (gt_spool.resolve_project, 0.18.1), so `--parent child-of-x` lands under
        # Projects/x/child-of-x/ rather than in a new top-level folder. A parent that
        # does not exist yet keeps the old behaviour: it is created at Projects/<parent>/.
        parent_rel = _resolve_parent(vault, parent)
        proj = vault / "Projects" / parent_rel / slug
        master_index = vault / "Projects" / parent_rel / "README.md"
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
    # PROJECT_PATH, not `Projects/{{SLUG}}`: for a sub-project the slug is only the last
    # segment of the address, and until 0.18.1 every sub-project's CLAUDE.md pointed at a
    # `Projects/<slug>/` that did not exist -- read by exactly the reader least able to
    # notice. The path is the folder just created, so it cannot disagree with it.
    seed_template("project-CLAUDE.md", proj / "CLAUDE.md", {
        "TITLE": display_title,
        "SLUG": slug,
        "PROJECT_PATH": proj.relative_to(vault).as_posix(),
    })

    # source.md — where the code lives and how it is deployed.
    topology = topology or "TODO"
    if fleet:
        fleet_str = fleet if fleet.startswith("[[") else f"[[{fleet}]]"
    elif topology.startswith("bastion"):
        fleet_str = "[[INFRASTRUCTURE]]"
    else:
        fleet_str = "n/a"
    # release_pipeline (0.18.1): yes | no | planned. A project with code defaults to `planned`
    # here -- /gt:gt-create asks, and passes `yes` for a project that ships code. `yes` with a
    # --project-dir scaffolds release.sh + release-pipeline.tsv there (gt_pipeline.py init,
    # which never overwrites); source.md records where the pipeline lives.
    if release_pipeline is None:
        release_pipeline = "planned" if topology != "TODO" else "no"
    rp_line = {"no": "none (release_pipeline: no)",
               "planned": "planned -- adopt it with gt_pipeline.py init --repo <code root>"}.get(
        release_pipeline, "TODO -- run gt_pipeline.py init --repo <code root>")
    if release_pipeline == "yes" and project_dir:
        code_root = Path(project_dir).resolve()
        rp_line = "%s (steps: %s)" % (code_root / "release.sh",
                                      code_root / "release-pipeline.tsv")
        if not DRY_RUN:
            _scaffold_release_pipeline(code_root, slug)
        else:
            record("would-create", code_root / "release-pipeline.tsv",
                   "release pipeline (gt_pipeline.py init)")
    seed_template("source.md", proj / "source.md", {
        "TITLE": display_title,
        "TOPOLOGY": topology,
        "REPO_URL": repo_url or "TODO",
        "FLEET": fleet_str,
        "RELEASE_PIPELINE": rp_line,
    })

    tags_str = ", ".join(tags) if tags else ""
    # Frontmatter drives Obsidian's tag pane / properties and the Dataview views in
    # Projects/README.md. Categorisation lives here rather than in folder nesting,
    # because a second level under Projects/ silently disables gt_lint's
    # memory-unlisted check (single-level iterdir).
    fm_tags = list(tags)
    if domain and domain not in fm_tags:
        fm_tags.insert(0, domain)
    # id_policy (vault.manifest.json, phase 84): only a README being CREATED is stamped; an existing
    # one is never opened, and a dry run mints nothing.
    new_id_lines = ""
    if not DRY_RUN and not (proj / "README.md").exists():
        new_id_lines = _manifest_id_lines(vault)
    frontmatter = (
        "---\n"
        "type: project\n"
        f"slug: {slug}\n"
        f"domain: {domain or 'TODO'}\n"
        "stage: idea\n"
        f"topology: {topology}\n"
        f"release_pipeline: {release_pipeline}\n"
        f"tags: [{', '.join(fm_tags)}]\n"
        + (f"parent: {parent}\n" if parent else "")
        + new_id_lines
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

    # Personas (0.21.0): the resolved personas' scaffold, ADDITIVE ONLY -- extra files and
    # sections on top of the standard structure above, never replacing one. No persona set:
    # nothing here runs a write, so the project is byte-identical to a gt without personas.
    try:
        import gt_persona
        _personas = gt_persona.resolve(str(vault), slug)
        _scaffold = (gt_persona.resolve_scaffold(_personas, display_title) if _personas
                     else {"files": [], "sections": []})
    except Exception as exc:                            # a bad persona never fails the project
        record("error", proj, "persona scaffold skipped: %s" % exc)
        _scaffold = {"files": [], "sections": []}
    for _f in _scaffold["files"]:
        ensure_file(proj / _f["path"], _f["content"])
    for _s in _scaffold["sections"]:
        _target = proj / _s["file"]
        _head = "## " + _s["heading"].strip()
        if not _target.is_file():
            continue
        _cur = _target.read_text(encoding="utf-8")
        if re.search(r"(?m)^%s\s*$" % re.escape(_head), _cur):
            record("skipped", _target, "persona section already present: " + _head)
            continue
        _lead = "" if _cur.endswith("\n") else "\n"
        if w_write(_target, _cur + _lead + "\n" + _head + "\n\n" + _s["body"].rstrip("\n") + "\n",
                action="updated", note="persona section: " + _head):
            record("updated", _target, "persona section: " + _head)

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


def _scaffold_release_pipeline(code_root: Path, slug: str):
    """gt_pipeline.py init in the project's code root: idempotent, never overwrites."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gt_pipeline
        code_root.mkdir(parents=True, exist_ok=True)
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):   # stdout is this tool's JSON
            rc = gt_pipeline.cmd_init(gt_pipeline.repo_root(str(code_root)), slug)
        record("created" if rc == 0 else "error", code_root / "release-pipeline.tsv",
               "release pipeline (gt_pipeline.py init)" + ("" if rc == 0 else ", exit %d" % rc))
    except Exception as exc:                            # never fail the project for it
        record("error", code_root / "release-pipeline.tsv",
               "release pipeline not scaffolded: %s" % exc)


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


def _warn_vault_location(vault):
    """A vault in Documents or on a synced or shared drive (0.21.4): warn on stderr, in plain words,
    and carry on -- the person may keep it there (gt_vault_location.py has the why). install.sh
    warns (and asks at a terminal) itself before it calls this, and says so with
    GT_VAULT_LOCATION_WARNED=1."""
    if os.environ.get("GT_VAULT_LOCATION_WARNED"):
        return
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gt_vault_location as vl
        found = vl.assess(str(vault))
        if found:
            print(vl.warning(str(vault), found), file=sys.stderr)
    except Exception:                               # noqa: BLE001 -- a warning never stops the init
        pass


# ---- the finish screen (phase 135) -------------------------------------------------------------
FIRST_COMMANDS = (
    ("/gt:gt-ingest", "bring in notes you already have (skip if you start empty)"),
    ("/gt:gt-work", "write down what a session learned, at the end of any session"),
    ("/gt:gt-next", "ask what to do next when you are not sure"),
)
FINISH_GREEN_SHOWN = 8


def _created_lines(vault: Path):
    """What exists in the vault now, in plain words. Read-only."""
    out = []
    for name, what in (("Projects", "projects folder"), ("Knowledge", "knowledge pages"),
                       ("Sources", "sources (never edited)"), ("global-memory", "facts every session loads")):
        if (vault / name).is_dir():
            out.append("%s/  %s" % (name, what))
    rules = vault / "core-rules"
    if rules.is_dir():
        out.append("core-rules/  %d rules that hold every turn" % len(list(rules.glob("*.md"))))
    if (vault / ".obsidian").is_dir():
        out.append(".obsidian/  daily-note settings for Obsidian")
    return out


def render_finish(vault, project, rows):
    """-> the finish screen text. Pure: the doctor's rows come in, plain words go out."""
    from urllib.parse import quote
    vault = str(vault)
    lines = ["Golden Thread is ready.", "", "  Vault:    %s" % vault]
    if project:
        lines.append("  Project:  %s  (%s/Projects/%s/)" % (project, vault, project))
    created = _created_lines(Path(vault))
    if created:
        lines += ["", "What was created"] + ["  - " + c for c in created]
    shown = [r for r in rows if r.get("state") != "INFO"]
    bad = [r for r in shown if r.get("state") != "PASS"]
    good = [r for r in shown if r.get("state") == "PASS"]
    lines += ["", "Health check"]
    if not bad:
        lines.append("  all checks passed (%d)" % len(good))
    for r in good[:FINISH_GREEN_SHOWN] if bad else []:
        lines.append("  ok    %-14s %s" % (r.get("row", ""), r.get("summary", "")))
    if bad and len(good) > FINISH_GREEN_SHOWN:
        lines.append("  ... and %d more passed" % (len(good) - FINISH_GREEN_SHOWN))
    for r in bad:
        lines.append("  %-5s %-14s %s" % (r.get("state", "?"), r.get("row", ""), r.get("summary", "")))
        if r.get("fix"):
            lines.append("        fix: %s" % r["fix"])
    lines += ["", "Your first three commands"]
    for cmd, why in FIRST_COMMANDS:
        lines.append("  %-14s %s" % (cmd, why))
    lines += ["", "First, restart Claude Code so the new hooks load.",
              "",
              "Obsidian is optional; the vault is plain Markdown. To browse it there, open this link:",
              "  obsidian://open?path=%s" % quote(vault, safe="")]
    return "\n".join(lines) + "\n"


def _doctor_rows(vault: Path, doctor_json=None):
    """-> the post-install rows: from a file when given, else by running the doctor beside this
    script. A doctor that cannot run is one red row with its fix, never silence."""
    could_not = [{"row": "doctor", "state": "FAIL", "summary": "could not run the health check",
                  "fix": "run /gt:gt-doctor in Claude Code after the restart"}]
    try:
        if doctor_json:
            return json.loads(Path(doctor_json).read_text(encoding="utf-8"))["rows"]
        p = subprocess.run([sys.executable, str(SCRIPT_DIR / "gt_doctor.py"), "post-install",
                            "--vault", str(vault), "--stage", "session", "--json"],
                           capture_output=True, text=True, timeout=240)
        return json.loads(p.stdout)["rows"]
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return could_not


def cmd_finish(vault: Path, project=None, doctor_json=None):
    vault = vault.resolve()
    sys.stdout.write(render_finish(vault, project, _doctor_rows(vault, doctor_json)))


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
    p_proj.add_argument("--release-pipeline", choices=("yes", "no", "planned"), default=None,
                        help="does the project have a release pipeline (release.sh)? yes "
                             "scaffolds it in --project-dir; default: planned for a project "
                             "with a topology, no otherwise")
    p_proj.add_argument("--fleet", default=None,
                        help="Page name of the shared fleet definition (default: INFRASTRUCTURE "
                             "for bastion topologies)")

    p_fin = sub.add_parser("finish", parents=[common], help="Print the finish screen (writes nothing)")
    p_fin.add_argument("--vault", required=True, type=Path)
    p_fin.add_argument("--project", default=None, help="the project slug just wired, if any")
    p_fin.add_argument("--doctor-json", default=None,
                       help="read the doctor's rows from this file instead of running it")

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
    p_ret.add_argument("--force", action="store_true",
                       help="archive even with open tasks or waiting handoffs (default: refuse "
                            "and list them)")
    p_ret.add_argument("--move", action="store_true",
                       help="also relocate the project to Archive/<slug>/ and re-point links "
                            "through the write queue (default: archive in place)")

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
    # Set by the modes that either perform their move or do not; see the exit at the end.
    did_nothing = False

    if args.mode == "finish":
        cmd_finish(args.vault, args.project, args.doctor_json)
        return 0
    if args.mode in ("fresh", "connect"):
        _warn_vault_location(args.vault)
    if args.mode == "fresh":
        cmd_fresh(args.vault, args.domain, no_config=args.no_config)
    elif args.mode == "create-project":
        tags = [t.strip() for t in args.tags.split(",")] if args.tags else []
        made = cmd_create_project(args.vault, args.name, args.title, tags, args.parent,
                                  args.runbook, args.project_dir, args.topology,
                                  args.repo_url, args.fleet, args.domain,
                                  args.release_pipeline)
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
        did_nothing = not done
        if done:
            src, dst = (_rel_project(args.vault, d) for d in done)
            _emit_event(args.vault.resolve(), "merge", "Projects/" + src,
                        frm="Projects/" + src, to="Projects/" + dst, project=dst,
                        note=f"merge-project {src} into {dst}")
    elif args.mode == "archive-project":
        done = cmd_archive_project(args.vault, args.slug, args.reason,
                                   args.date or __import__("datetime").date.today().isoformat(),
                                   force=args.force)
        did_nothing = done is None
        if done is not None:
            rel = _rel_project(args.vault, done)
            _emit_event(args.vault.resolve(), "archive", "Projects/" + rel, project=rel,
                        note=f"archive-project: {' '.join(args.reason.split())}"[:120])
            if args.move:
                moved = cmd_archive_move(args.vault, args.slug)
                did_nothing = moved is None
                if moved is not None:
                    _emit_event(args.vault.resolve(), "relocate", "Archive/" + rel,
                                frm="Projects/" + rel, to="Archive/" + rel, project=rel,
                                note=f"archive-project --move {rel}"[:120])
    elif args.mode == "rename-project":
        done = cmd_rename_project(args.vault, args.old, args.new)
        did_nothing = not done
        if done:
            old, new = (_rel_project(args.vault, d) for d in done)
            _emit_event(args.vault.resolve(), "rename", "Projects/" + new,
                        frm="Projects/" + old, to="Projects/" + new, project=new,
                        note=f"rename-project {old} -> {new}")
    elif args.mode == "install-core-rules":
        install_core_rules(args.vault.resolve(), wire_hooks=not args.no_hooks,
                           settings_path=args.settings)

    print(json.dumps(RESULTS, indent=2))
    # THE EXIT CODE FOLLOWS WHETHER THE OPERATION HAPPENED. main() used to end in a bare
    # sys.exit(0), so `rename-project` on a project that does not exist, a rename onto a name
    # already taken, or a merge of a project into itself printed {"action": "error"} and told
    # its caller it had succeeded.
    #
    # It is deliberately NOT "any error row exits 1". The scaffolding modes record error rows
    # for conditions that are normal mid-install and that install.sh resolves itself -- the
    # enforcement hooks not being in ~/.claude yet is the standard one -- while still doing
    # everything they were asked to do. Failing those would make `fresh` report a broken
    # install every time a vault is created before the hooks are copied, and `fresh` into a
    # clean directory exiting 0 is the documented happy path.
    #
    # So BOTH have to be true: one of the three move-or-do-not modes (rename, merge, archive)
    # did not perform its move, AND it said why in an error or conflict row. A rehearsal that
    # would have worked records neither and exits 0. Every mode's rows stay in the JSON, which
    # is the surface carrying the detail; the code only says whether to go and read them.
    refusals = [r for r in RESULTS if r["action"] in ("error", "conflict")]
    sys.exit(1 if (did_nothing and refusals) else 0)


if __name__ == "__main__":
    # A refusal from the OS -- gt sandbox mode, or a macOS interpreter refusal -- is one line
    # naming the next step, not a traceback (0.20.1, B4). gt_errors sits beside this file.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import gt_errors as _gte
    except ImportError:
        _gte = None
    raise SystemExit(_gte.run(main, "vault_init") if _gte else main())
