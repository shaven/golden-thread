#!/usr/bin/env python3
"""guard_protected_paths -- the PreToolUse body behind guard_protected_paths.sh.

WHY (verified 2026-09-13): a session's Write/Edit tools could rewrite, with no prompt
under a permissive mode, every file that shapes every other session:

  * <vault>/.../core-rules/ and <vault>/global-memory/ load into every session of every
    project -- one bad edit is a bad instruction everywhere;
  * ~/.claude/golden-thread/ (these hooks) and ~/.claude/settings.json (which wires
    them) enforce everything else -- a session could switch its own guards off;
  * <vault>/Sources/ is immutable by convention: a changed source is superseded by a
    new file, never edited, or every Knowledge page citing it silently changes meaning;
  * <vault>/Projects/golden-thread/packs/ holds LOCAL definition packs (0.16.0), the
    highest-precedence tier of the slot registry and the only tier no validator and no
    release gate ever sees -- what counts as a credential, which paths are ignored, and
    prose that reaches model context, all writable with no prompt until 2026-09-16.

No hook and no Core rule covered any of these. Skill prose was the only guard.

DECISIONS
  ask   target inside core-rules/, global-memory/, packs/, ~/.claude/golden-thread/, or
        equal to ~/.claude/settings.json. `ask` rather than `deny`: promoting a fact to
        global-memory, editing a rule, or writing your own pack is legitimate work; the
        point is that a person sees it happen, whatever the permission mode.
  deny  target is an EXISTING regular file under <vault>/Sources/. Creating a new
        Source is how superseding works, so it passes untouched.
  none  anything else, protected_paths=off, or any error.

PATHS are compared by FILE IDENTITY (st_dev, st_ino) of the nearest existing ancestor,
not by string, so `..` segments and symlinks cannot walk around the check AND -- fixed
0.15.1 -- neither can a case- or Unicode-variant spelling of a protected path on a
case-insensitive volume (APFS, exFAT). os.path.realpath returns the CALLER's spelling,
not the on-disk name, so `Global-Memory/x.md`, `CORE-RULES/x.md`, `~/.claude/Settings.json`
and an NFC/NFD or U+017F variant all resolve to the protected inode and are now caught;
before 0.15.1 the string compare missed them and the write got no prompt. Where a path
does not exist yet (a new file), the nearest existing ancestor's identity is used; a
last-resort string compare uses NFC + casefold, never str.lower().

THAT PROTECTION IS INHERITED FROM THE VOLUME, NOT IMPOSED BY THIS GUARD, and the two are
worth telling apart. Identity matching catches a case variant *because the filesystem makes
both spellings one inode*. On a case-SENSITIVE volume (ext4, and so most Linux) `PACKS/` is
a different directory from `packs/` -- the guard does not fire, and should not: nothing reads
that spelling as the local pack tier, so there is no protected content behind it to reach.
The guarantee is therefore "a variant that resolves to protected content is caught", which is
the true statement on every volume; "case variants are caught" is only true where the volume
folds case. Found when the suite first ran on Linux (CI, 2026-09-17) against a test that had
encoded the macOS answer as universal.

CORE RULES LOCATION: the default Projects/golden-thread/core-rules, the recorded
vault-config.json:core_rules_path, and -- mirroring gt_paths.find_core_rules' search --
any ancestor directory named core-rules that holds the marker model file. The ancestor
walk replaces the rglob over the whole vault that find_core_rules falls back to: this
runs before every file edit, and a vault-wide walk there is too slow to pay each time.
record=False semantics throughout: a guard never writes config.

NO OBJECTION = NO OUTPUT: exit 0 with empty stdout. Never print permissionDecision
"allow" -- Claude Code treats that as "skip the permission prompt".
"""
import json
import os
import sys
import unicodedata

HERE = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
DEFAULT_CORE_RULES = os.path.join("Projects", "golden-thread", "core-rules")


def no_objection():
    """Stay out of the permission decision: print nothing, exit 0 (fail open)."""
    sys.exit(0)


def real(p):
    return os.path.realpath(os.path.expanduser(p))


def _ident(p):
    """(st_dev, st_ino) of p, or None if it does not exist / cannot be stat'd."""
    try:
        st = os.stat(p)                       # follows symlinks; p is already realpath'd
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def _nearest_existing(p):
    """The deepest ancestor of p (or p itself) that exists on disk, or None."""
    p = os.path.abspath(p)
    while True:
        if os.path.lexists(p):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def _norm(p):
    """NFC + casefold of a realpath'd path -- the last-resort string comparison.
    casefold(), not lower(): lower() misses U+017F 'long s' and the Kelvin sign."""
    return unicodedata.normalize("NFC", real(p)).casefold()


def same_file(a, b):
    """True when a and b name the same file. By (st_dev, st_ino) when both exist --
    so a case/Unicode variant on a case-insensitive volume matches -- else by _norm."""
    ia, ib = _ident(a), _ident(b)
    if ia is not None and ib is not None:
        return ia == ib
    return _norm(a) == _norm(b)


def inside(path, root):
    """True when path is root or below it, robust to case/Unicode variants on a
    case-insensitive volume. Compares FILE IDENTITY (st_dev, st_ino) rather than
    strings: os.path.realpath keeps the caller's spelling, so `Global-Memory` and
    `global-memory` share a string prefix on neither, yet resolve to one inode.

    When root exists, walk from path's nearest existing ancestor up to the filesystem
    root, matching each ancestor's identity against root's; any match means path is at
    or below root (a new file under a protected dir is caught via its parent). When root
    does not exist, fall back to an NFC+casefold string containment test."""
    rid = _ident(root)
    if rid is None:
        rp = _norm(root)
        pp = _norm(path)
        return pp == rp or pp.startswith(rp.rstrip(os.sep) + os.sep)
    cur = _nearest_existing(path)
    if cur is None:
        return False
    cur = real(cur)
    while True:
        if _ident(cur) == rid:
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def decide(kind, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": kind,
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def ask(area, why, target):
    decide("ask", (
        f"Protected path (gt setting protected_paths=ask): {area}.\n\n"
        f"  {target}\n\n"
        f"{why}\n\n"
        f"If this change is intended -- promoting a fact, editing a rule, updating a hook on "
        f"purpose -- approving it is fine. The prompt exists so a person sees it happen. "
        f"Turn the check off with `gt_settings.py set protected_paths off`."))


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        no_objection()
    if not isinstance(payload, dict):
        no_objection()

    tool = payload.get("tool_name") or ""
    if tool not in TOOLS:
        no_objection()
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        no_objection()
    target = ti.get("file_path") or ti.get("notebook_path") or ""
    if not isinstance(target, str) or not target.strip():
        no_objection()

    try:
        import gt_settings
        if gt_settings.get("protected_paths") == "off":
            no_objection()
    except SystemExit:
        raise
    except Exception:
        pass            # no settings module -> the registered default, ask

    target = os.path.expanduser(target)
    if not os.path.isabs(target):
        cwd = payload.get("cwd")
        base = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else os.getcwd()
        target = os.path.join(base, target)
    t = os.path.realpath(target)

    # -- ~/.claude: needs no vault ----------------------------------------------------
    home = os.path.expanduser("~")
    claude = os.path.join(home, ".claude")
    if inside(t, real(os.path.join(claude, "golden-thread"))):
        ask("the Golden Thread hooks directory (~/.claude/golden-thread/)",
            "These scripts enforce every Core rule and guard in every session; a session "
            "editing them can switch its own enforcement off.", t)
    if same_file(t, real(os.path.join(claude, "settings.json"))):
        ask("~/.claude/settings.json",
            "This file wires every hook and permission rule for every session; a session "
            "editing it can unwire the guards that constrain it.", t)

    # -- the vault --------------------------------------------------------------------
    try:
        from gt_paths import find_vault, read_config, MODEL_FILE
        vault = find_vault()
    except Exception:
        no_objection()
    if not vault:
        no_objection()
    v = real(str(vault))
    if not inside(t, v):
        no_objection()

    core_roots = {real(os.path.join(v, DEFAULT_CORE_RULES))}
    try:
        rel = read_config().get("core_rules_path")
        if isinstance(rel, str) and rel.strip():
            core_roots.add(real(os.path.join(v, rel)))
    except Exception:
        pass
    is_core = any(inside(t, r) for r in core_roots)
    if not is_core:
        d = os.path.dirname(t)
        while inside(d, v) and d != v:
            if os.path.basename(d).casefold() == "core-rules" and os.path.isfile(os.path.join(d, MODEL_FILE)):
                is_core = True
                break
            d = os.path.dirname(d)
    if is_core:
        ask("the vault's Core rules (core-rules/)",
            "Core rules are injected into every turn of every session in every project; "
            "a wrong edit here is a wrong instruction everywhere.", t)

    if inside(t, real(os.path.join(v, "global-memory"))):
        ask("the vault's global-memory/",
            "global-memory is loaded into every session regardless of project; anything "
            "written here is believed everywhere.", t)

    # Local definition packs. The path mirrors gt_registry.pack_dirs()'s `local` tier; if one
    # moves, the other must. `local` is the HIGHEST-precedence tier, and packs never pass
    # through the submission validator or the release gate -- nothing else checks them. A write
    # here changes what counts as a credential (`secrets`), which paths are skipped (`ignore`),
    # and, for the model-reachable slots, text that is read back as fact in later sessions.
    # Security review 2026-09-16 confirmed an AI session could plant one unchallenged; it is
    # guarded BEFORE the first consumer ships rather than after, because the whole point of a
    # durable definition is that it is believed later, by a session that never saw it written.
    packs = real(os.path.join(v, "Projects", "golden-thread", "packs"))
    if inside(t, packs):
        ask("the vault's local definition packs (packs/)",
            "local packs outrank both core and contributed definitions and are not checked by "
            "the submission validator; a definition written here is believed by every later "
            "session in this vault.", t)

    sources = real(os.path.join(v, "Sources"))
    if inside(t, sources) and t != sources and os.path.isfile(t):
        decide("deny", (
            f"BLOCKED (gt setting protected_paths): Sources/ is immutable.\n\n"
            f"  {t}\n"
            f"  already exists, and {tool} would change it in place.\n\n"
            f"Sources are raw input that Knowledge pages cite; editing one silently changes "
            f"what every citing page rests on. Supersede it instead: write the new version "
            f"as a NEW file under Sources/ and mark the old one superseded from the pages "
            f"that cite it (see /gt:gt-refresh). Creating a new file under Sources/ is not "
            f"blocked."))

    no_objection()


try:
    main()
except SystemExit:
    raise
except Exception:
    sys.exit(0)
