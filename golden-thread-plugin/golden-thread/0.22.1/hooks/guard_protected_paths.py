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
  ask   (0.22.0, setting addon_writes, default on) target inside ~/.claude/skills/,
        <cwd>/.claude/skills/, ~/.claude/plugins/ (but not gt's own cache, marketplace or
        data dirs), ~/.codex/, or equal to ~/.claude.json or <cwd>/.mcp.json: a skill,
        plugin or MCP server written there runs in later sessions. protected_paths=off is
        the master switch for this whole guard and silences these too.
  ask   (0.22.0, phase 84 review) target is <vault>/vault.manifest.json or inside the vault's gt
        tools folder (Projects/golden-thread/tools/ and, when the manifest moves it, that folder):
        the write broker runs the scripts there. A Bash command writing them is denied.
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

CORE RULES LOCATION: the default core-rules/ at the vault root (was
Projects/golden-thread/core-rules before 0.17.0), the recorded
vault-config.json:core_rules_path, and -- mirroring gt_paths.find_core_rules' search --
any ancestor directory named core-rules that holds the marker model file. The ancestor
walk replaces the rglob over the whole vault that find_core_rules falls back to: this
runs before every file edit, and a vault-wide walk there is too slow to pay each time.
record=False semantics throughout: a guard never writes config.

NO OBJECTION = NO OUTPUT: exit 0 with empty stdout. Never print permissionDecision
"allow" -- Claude Code treats that as "skip the permission prompt".

GT UNLOCK (0.20.1). When unlock is ON, an edit to ~/.claude/settings.json, ~/.claude/vault-config.json
or anything under ~/.claude/golden-thread/ (the hooks, the unlock state) first needs the scope
gt:settings:hooks -- a FRESH confirmation (Touch ID / Windows Hello, else TOTP) raised by the
unlock authority, never by this hook. This gate runs BEFORE the protected_paths=off check, so a
session cannot switch the setting off and then edit its own guards. It FAILS CLOSED: an
authority that is down, cannot be imported, or does not answer within HOOK_WAIT_S denies the
edit -- the one deliberate exception to "fail open" in this file, and only when unlock is on.
With unlock OFF the cost is two stat() calls and nothing else changes. Honest limit: Write/Edit are checked exactly; a Bash command is checked by a pattern reader
(shell_branch, 0.22.0), which a determined session can phrase around -- sandbox mode and the
`paranoid` group's Bash guard (gt_integrity.py bash-guard) with its OS lock are the real barriers.

PLUGIN DIRS (0.21.0, `paranoid.plugin_dirs`): with that part on, Write/Edit to the golden-thread-
plugin directories of the plugin cache and the marketplace ask too, and with unlock on need the
same fresh confirmation as the hooks dir. Off (the default) these paths are not protected, as before.
"""
import json
import os
import re
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
        if not st.st_ino:                     # a volume that reports inode 0 names nothing:
            return None                       # two different files must not compare equal
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



# 0.22.0: a repeat counter on this guard's refusals (gt_refusals.py, setting refusal_limit). Fails open:
# without the module beside the guard, or on any error, the deny text is exactly what it was.
sys.dont_write_bytecode = True          # the hooks folder is signed: importing a sibling must not leave a __pycache__ in it
try:
    import gt_refusals as _gtr
except Exception:                                            # noqa: BLE001
    _gtr = None


def _annotated(reason):
    try:
        return _gtr.annotate('guard_protected_paths', reason) if _gtr else reason
    except Exception:                                        # noqa: BLE001
        return reason


def _use_session(payload):
    try:
        if _gtr:
            _gtr.use_session(payload)
    except Exception:                                        # noqa: BLE001
        pass

def decide(kind, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": kind,
        "permissionDecisionReason": _annotated(reason) if kind == "deny" else reason,
    }}))
    sys.exit(0)


MANIFEST = "vault.manifest.json"
DEFAULT_TOOLS = os.path.join("Projects", "golden-thread", "tools")


def _vault_tools_dirs(v):
    """The vault's gt tools folders: the default and, when vault.manifest.json moves it, that one
    too (read through gt_vault_manifest from beside this guard; without it, the default only)."""
    out = [os.path.join(v, DEFAULT_TOOLS)]
    try:
        import gt_vault_manifest
        rel = gt_vault_manifest.load(v).tools_rel
        moved = os.path.join(v, *rel.parts)
        if os.path.normpath(moved) != os.path.normpath(out[0]):
            out.append(moved)
    except Exception:                                  # noqa: BLE001 - no reader: the default
        pass
    return out


def ask(area, why, target):
    decide("ask", (
        f"Protected path (gt setting protected_paths=ask): {area}.\n\n"
        f"  {target}\n\n"
        f"{why}\n\n"
        f"If this change is intended -- promoting a fact, editing a rule, updating a hook on "
        f"purpose -- approving it is fine. The prompt exists so a person sees it happen. "
        f"Turn the check off with `gt_settings.py set protected_paths off`."))


HOOK_WAIT_S = 45          # under Claude Code's 60 s hook budget: deny before being killed


def _unlock_on():
    """Is unlock on? Never raises. A policy module that cannot be imported while a policy or
    admin file exists counts as ON (fail closed)."""
    try:
        import gt_unlock_policy
        return gt_unlock_policy.enabled_fast()
    except Exception:
        candidates = [os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "unlock",
                                   "policy.json"),
                      "/Library/Application Support/gt/unlock-policy.json",
                      "/etc/gt/unlock-policy.json",
                      os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData", "gt",
                                   "unlock-policy.json")]
        return any(os.path.lexists(c) for c in candidates)


def unlock_gate(scope, area, target):
    """Deny unless the unlock authority grants `scope` (with a fresh confirmation it raises
    itself). Returns normally when unlock is off or the scope is granted."""
    if not _unlock_on():
        return
    import threading
    box = {}

    def ask_authority():
        try:
            import gt_unlock_client
            box["v"] = gt_unlock_client.check(
                scope, request=True, reason="edit %s (%s)" % (area, os.path.basename(target)))
        except Exception as e:                       # noqa: BLE001
            box["v"] = {"allowed": False, "code": "unavailable", "message": type(e).__name__}
    t = threading.Thread(target=ask_authority, daemon=True)
    t.start()
    t.join(HOOK_WAIT_S)
    v = box.get("v") or {"allowed": False, "code": "timeout",
                         "message": "no confirmation within %d s" % HOOK_WAIT_S}
    if v.get("allowed"):
        return
    decide("deny", (
        f"BLOCKED (gt unlock, scope {scope}): {area}.\n\n  {target}\n\n"
        f"{v.get('message')} ({v.get('code')}). Editing this needs a fresh confirmation from "
        f"you, raised by the unlock authority. Approve the prompt and retry, or run "
        f"`gt_unlock.py status` to see why it was refused."))


def _plugin_dirs():
    """The plugin cache and marketplace copies of golden-thread-plugin, when the paranoid part
    `paranoid.plugin_dirs` is on (0.21.0); [] otherwise -- with the group off this is one small
    config read and the guard decides exactly as it always did."""
    try:
        import gt_paranoid
        if not gt_paranoid.enforced("paranoid.plugin_dirs"):
            return []
    except Exception:                                  # noqa: BLE001 - no module, no group
        return []
    return _gt_plugin_dirs()


GT_MARKETPLACE = "golden-thread-plugin"


def _winpath(p):
    """Git-Bash style /c/Users/x -> C:/Users/x, on Windows only; anything else unchanged."""
    if os.name == "nt" and isinstance(p, str) and len(p) >= 2 and p[0] == "/" \
            and p[1].isalpha() and (len(p) == 2 or p[2] == "/"):
        return p[1].upper() + ":" + (p[2:] or "/")
    return p


def _home():
    """The home directory: USERPROFILE first on Windows, like os.path.expanduser."""
    if os.name == "nt":
        up = os.environ.get("USERPROFILE")
        if up and up.strip():
            return _winpath(up.strip())
    return os.path.expanduser("~")


def _env_path(name):
    """An absolute path from an environment variable, or None when unset / empty."""
    v = os.environ.get(name)
    if not isinstance(v, str) or not v.strip():
        return None
    return os.path.abspath(_winpath(os.path.expanduser(v.strip())))


def _claude_dirs():
    """~/.claude and, when CLAUDE_CONFIG_DIR relocates it, that directory too."""
    out = [os.path.join(_home(), ".claude")]
    cc = _env_path("CLAUDE_CONFIG_DIR")
    if cc and cc not in out:
        out.append(cc)
    return out


def _gt_plugin_dirs():
    """gt's own copies under ~/.claude/plugins -- the cache and marketplace dirs of its
    marketplace -- whatever paranoid says. _plugin_dirs() guards them under paranoid;
    _addon_area() exempts them, so gt's own updates never ask as an add-on write."""
    out = []
    for c in _claude_dirs():
        plugins = os.path.join(c, "plugins")
        out += [real(os.path.join(plugins, "cache", GT_MARKETPLACE)),
                real(os.path.join(plugins, "marketplaces", GT_MARKETPLACE))]
    return out


def _gt_plugin_names(plugins):
    """Names of the plugins installed from gt's own marketplace under `plugins`."""
    names = set()
    try:
        names.update(os.listdir(os.path.join(plugins, "cache", GT_MARKETPLACE)))
    except OSError:
        pass
    try:
        with open(os.path.join(plugins, "marketplaces", GT_MARKETPLACE, ".claude-plugin",
                               "marketplace.json"), encoding="utf-8") as f:
            for e in json.load(f).get("plugins") or []:
                if isinstance(e, dict) and isinstance(e.get("name"), str):
                    names.add(e["name"])
    except Exception:                                  # noqa: BLE001
        pass
    return {_norm_name(n) for n in names}


def _norm_name(n):
    return unicodedata.normalize("NFC", n).casefold()


def _is_gt_plugin_path(t, plugins):
    """t is inside one of gt's own plugin dirs: its cache / marketplace copies, or the data dir
    Claude Code made for one of gt's plugins (plugins/data/<plugin>-golden-thread-plugin).
    The data dir name is <plugin>-<marketplace>; it passes only when <plugin> is a plugin of
    gt's marketplace, so a third-party marketplace called evil-golden-thread-plugin does not."""
    if any(inside(t, d) for d in _gt_plugin_dirs()):
        return True
    data = os.path.join(plugins, "data")
    if not inside(t, data):
        return False
    d = t
    while True:                       # the first component below plugins/data
        parent = os.path.dirname(d)
        if parent == d:
            return False
        if same_file(parent, data):
            break
        d = parent
    name = _norm_name(os.path.basename(d))
    suffix = "-" + GT_MARKETPLACE
    if not name.endswith(suffix):
        return False
    return name[:-len(suffix)] in _gt_plugin_names(plugins)


def _lexnorm(p):
    return unicodedata.normalize("NFC", os.path.normpath(os.path.abspath(p))).casefold()


def _inside_unresolved(raw, root):
    """Like inside(), but walks the path as SPELLED: a symlinked directory below `root` is
    still below it, although its realpath is not."""
    rp, pp = _lexnorm(root), _lexnorm(raw)
    if pp == rp or pp.startswith(rp.rstrip(os.sep) + os.sep):
        return True
    rid = _ident(root)
    if rid is None:
        return False
    cur = os.path.abspath(raw)
    while True:
        if _ident(cur) == rid:
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def _in_dir(t, raw, d):
    return any(inside(t, v) or (raw and _inside_unresolved(raw, v))
               for v in {d, real(d)})


def _is_file(t, raw, f):
    if same_file(t, real(f)):
        return True
    return bool(raw) and _lexnorm(raw) in {_lexnorm(f), _lexnorm(real(f))}


def _project_roots(cwd):
    """The cwd and every ancestor holding .git, .mcp.json or .claude, up to and including the
    first with .git (the git toplevel) or the filesystem root."""
    roots = []
    for start in {os.path.abspath(cwd), real(cwd)}:
        cur = start
        for _ in range(64):
            git = os.path.lexists(os.path.join(cur, ".git"))
            if cur == start or git or os.path.lexists(os.path.join(cur, ".mcp.json")) \
                    or os.path.lexists(os.path.join(cur, ".claude")):
                if cur not in roots:
                    roots.append(cur)
            parent = os.path.dirname(cur)
            if git or parent == cur:
                break
            cur = parent
    return roots


def _addon_area_basic(t, cwd):
    """The 0.22.0 check as first shipped (realpath only, under the payload cwd): what the
    broader check degrades to if it raises."""
    home = os.path.expanduser("~")
    claude = real(os.path.join(home, ".claude"))
    if inside(t, os.path.join(claude, "skills")):
        return "~/.claude/skills/ (a Claude Code skill)"
    plugins = os.path.join(claude, "plugins")
    if inside(t, plugins) and not _is_gt_plugin_path(t, plugins):
        return "~/.claude/plugins/ (a Claude Code plugin outside gt's own)"
    if same_file(t, real(os.path.join(home, ".claude.json"))):
        return "~/.claude.json (Claude Code's MCP server and project config)"
    if inside(t, real(os.path.join(home, ".codex"))):
        return "~/.codex/ (Codex config, skills and MCP servers)"
    if cwd:
        c = real(cwd)
        if inside(t, os.path.join(c, ".claude", "skills")):
            return "<project>/.claude/skills/ (a project Claude Code skill)"
        if same_file(t, os.path.join(c, ".mcp.json")):
            return "<project>/.mcp.json (the project's MCP server config)"
    return None


def _addon_area(t, cwd, raw=None):
    """-> a description of the add-on area t falls in, or None (tool vetting A4, 0.22.0).
    Skills, agents, commands, plugins, MCP server config, project settings (which can wire
    hooks) and Codex config: what is written there runs or is loaded by later sessions, and
    until 0.22.0 a session could plant one with no prompt. `t` is the realpath, `raw` the
    normalised path as given; either one in an area counts. Any error falls back to the
    narrower first check."""
    try:
        return _addon_area_wide(t, cwd, raw)
    except Exception:                                  # noqa: BLE001 - never block on a bug here
        try:
            return _addon_area_basic(t, cwd)
        except Exception:                              # noqa: BLE001
            return None


def _addon_area_wide(t, cwd, raw):
    home = _home()
    for c in _claude_dirs():
        if _in_dir(t, raw, os.path.join(c, "skills")):
            return "~/.claude/skills/ (a Claude Code skill)"
        plugins = os.path.join(c, "plugins")
        if _in_dir(t, raw, plugins) and not _is_gt_plugin_path(t, plugins):
            return "~/.claude/plugins/ (a Claude Code plugin outside gt's own)"
        if _in_dir(t, raw, os.path.join(c, "agents")):
            return "~/.claude/agents/ (Claude Code subagent definitions)"
        if _in_dir(t, raw, os.path.join(c, "commands")):
            return "~/.claude/commands/ (Claude Code slash commands)"
    jsons = [os.path.join(home, ".claude.json")]
    cc = _env_path("CLAUDE_CONFIG_DIR")
    if cc:
        jsons.append(os.path.join(cc, ".claude.json"))
    if any(_is_file(t, raw, f) for f in jsons):
        return "~/.claude.json (Claude Code's MCP server and project config)"
    codex = [os.path.join(home, ".codex")]
    ch = _env_path("CODEX_HOME")
    if ch:
        codex.append(ch)
    if any(_in_dir(t, raw, d) for d in codex):
        return "~/.codex/ (Codex config, skills and MCP servers)"
    if cwd:
        for r in _project_roots(cwd):
            if _in_dir(t, raw, os.path.join(r, ".claude", "skills")):
                return "<project>/.claude/skills/ (a project Claude Code skill)"
            if _in_dir(t, raw, os.path.join(r, ".claude", "agents")):
                return "<project>/.claude/agents/ (project Claude Code subagents)"
            if _in_dir(t, raw, os.path.join(r, ".claude", "commands")):
                return "<project>/.claude/commands/ (project Claude Code slash commands)"
            if _is_file(t, raw, os.path.join(r, ".mcp.json")):
                return "<project>/.mcp.json (the project's MCP server config)"
            for name in ("settings.json", "settings.local.json"):
                if _is_file(t, raw, os.path.join(r, ".claude", name)):
                    return ("<project>/.claude/%s (project settings, which can wire hooks "
                            "that run code)" % name)
    return None


def ask_addon(area, target):
    decide("ask", (
        f"Add-on write (gt setting addon_writes=on): {area}.\n\n"
        f"  {target}\n\n"
        f"This is a Claude Code skill/plugin/MCP config: what's written here runs or is loaded "
        f"by every session, so a person should see it added or changed. If you meant to add or "
        f"edit it, approving is fine. The same goes for agents, commands and project settings, "
        f"which can wire hooks that run code. Only the Write/Edit tools are checked here: a Bash "
        f"command writing the same file is not covered by this guard. Turn the check off with "
        f"`gt_settings.py set addon_writes off`."))


# A shell command that WRITES to these (0.22.0, review A3). Pattern guard, not a boundary: it reuses
# gt_integrity's command reader, so quoting tricks and a script that writes on the model's behalf
# get past it; sandbox mode is the real control. Reads, listings and running a script pass.
_INLINE_WRITE = re.compile(
    r"""['"][wax][+b]*['"]|\.write\w*\(|write_text|write_bytes|shutil\.|\.unlink\(|"""
    r"""os\.(?:replace|rename|remove|unlink|chmod|truncate|symlink)""")


def _shell_write_roots():
    claude = os.path.join(os.path.expanduser("~"), ".claude")
    roots = [os.path.join(claude, "settings.json"), os.path.join(claude, "vault-config.json"),
             os.path.join(claude, "golden-thread"),
             ] + _lotr_homes()                                       # gateway.json, consent-windows.json
    return roots


# gt_grant.py add is the user's to give: it needs a terminal and a Bash tool call has none, so the
# command could only fail (phase 110). Denied here with the cure rather than left to refuse itself.
_GRANT_ADD = re.compile(r"gt_grant\.py['\"]?\s+add\b")


def _lotr_homes():
    homes = [os.path.join(os.path.expanduser("~"), ".config", "gt-lotr")]
    env = os.environ.get("LOTR_HOME")
    if env:
        homes.append(os.path.expanduser(env))
    return homes


def shell_branch(payload):
    """PreToolUse(Bash): deny a command that writes to gt's settings, hooks or gateway state.
    Fails open on anything it cannot read."""
    cmd = (payload.get("tool_input") or {}).get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return
    try:
        import gt_settings
        if gt_settings.get("protected_paths") == "off":
            return
        import gt_integrity
        if _GRANT_ADD.search(cmd):
            decide("deny", (
                "BLOCKED (gt setting protected_paths): `gt_grant.py add` writes a permission rule, and a "
                "grant is yours to give: it needs a terminal, which a session's shell does not have. Run "
                "it yourself from a terminal (`! <command>` in the prompt), or use /gt:gt-grant. Turn this "
                "guard off with `gt_settings.py set protected_paths off`."))
        cwd = payload.get("cwd")
        cwd = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else os.getcwd()
        why = gt_integrity.bash_write_reason(cmd, cwd, os.path.expanduser("~"), _shell_write_roots())
    except Exception:                                        # noqa: BLE001 - fail open
        return
    if not why:
        _vault_shell_branch(cmd, cwd)
        return
    if "runs inline code" in why and not _INLINE_WRITE.search(cmd):
        return                                               # inline code that only names the path: a read
    decide("deny", (
        "BLOCKED (gt setting protected_paths): this command %s.\n\n"
        "Those files wire every hook and permission rule for every session, and hold the gateway's "
        "consent state; a session writing them can unwire the guards that constrain it. If the change "
        "is meant, run it yourself from a terminal (`! <command>` in the prompt), or use the gt tool "
        "that owns the file (gt_settings.py, gt_grant.py, the installer). Turn this guard off with "
        "`gt_settings.py set protected_paths off`." % why))


def _vault_shell_branch(cmd, cwd):
    """A shell command that writes vault.manifest.json or a vault gt tools folder (0.22.0, phase 84
    review H1): denied like the writes above, with the same honest limit. Fails open."""
    try:
        import gt_integrity
        from gt_paths import find_vault
        vault = find_vault()
        if not vault:
            return
        roots = []
        for v in dict.fromkeys((os.path.abspath(str(vault)), real(str(vault)))):  # as named, as resolved
            roots += [os.path.join(v, MANIFEST)] + _vault_tools_dirs(v)
        why = gt_integrity.bash_write_reason(cmd, cwd, os.path.expanduser("~"), roots)
    except Exception:                                        # noqa: BLE001 - fail open
        return
    if not why or ("runs inline code" in why and not _INLINE_WRITE.search(cmd)):
        return
    decide("deny", (
        "BLOCKED (gt setting protected_paths): this command %s.\n\n"
        "vault.manifest.json decides where gt keeps its queue, broker and tools folders, and the write "
        "broker runs the scripts in the tools folder; a session writing them changes what runs on the "
        "owner's next drain. If the change is meant, run it yourself from a terminal (`! <command>` in "
        "the prompt), or let install.sh refresh the tools. Turn this guard off with "
        "`gt_settings.py set protected_paths off`." % why))


def main():
    try:
        payload = json.load(sys.stdin)
        _use_session(payload)
    except Exception:
        no_objection()
    if not isinstance(payload, dict):
        no_objection()

    tool = payload.get("tool_name") or ""
    if tool == "Bash":
        shell_branch(payload)
        no_objection()
    if tool not in TOOLS:
        no_objection()
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        no_objection()
    target = ti.get("file_path") or ti.get("notebook_path") or ""
    if not isinstance(target, str) or not target.strip():
        no_objection()

    target = _winpath(os.path.expanduser(target))
    if not os.path.isabs(target):
        cwd = payload.get("cwd")
        base = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else os.getcwd()
        target = os.path.join(base, target)
    t = os.path.realpath(target)
    raw = os.path.normpath(target)           # the spelling the tool was given: symlinks unresolved
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else os.getcwd()

    # gt unlock (0.20.1): BEFORE the protected_paths switch, so switching it off cannot be the
    # first step of disabling the guards. Cheap when unlock is off (two stats).
    if _unlock_on():
        _claude = real(os.path.join(os.path.expanduser("~"), ".claude"))
        if inside(t, os.path.join(_claude, "golden-thread")):
            unlock_gate("gt:settings:hooks", "the Golden Thread hooks and unlock state", t)
        elif same_file(t, os.path.join(_claude, "settings.json")) or \
                same_file(t, os.path.join(_claude, "vault-config.json")):
            unlock_gate("gt:settings:hooks", "Claude Code / gt settings", t)
        elif any(inside(t, d) for d in _plugin_dirs()):
            unlock_gate("gt:settings:hooks", "the Golden Thread plugin cache / marketplace", t)

    try:
        import gt_settings
        if gt_settings.get("protected_paths") == "off":
            no_objection()
    except SystemExit:
        raise
    except Exception:
        pass            # no settings module -> the registered default, ask


    # -- ~/.claude: needs no vault ----------------------------------------------------
    home = os.path.expanduser("~")
    claude = os.path.join(home, ".claude")
    if inside(t, real(os.path.join(claude, "golden-thread"))):
        ask("the Golden Thread hooks directory (~/.claude/golden-thread/)",
            "These scripts enforce every Core rule and guard in every session; a session "
            "editing them can switch its own enforcement off.", t)
    if any(inside(t, d) for d in _plugin_dirs()):
        ask("the Golden Thread plugin cache / marketplace (paranoid.plugin_dirs)",
            "Claude Code runs gt's skill scripts from these copies; an edit here changes what "
            "every session runs, and an upgrade is the one thing that should write to them.", t)
    if same_file(t, real(os.path.join(claude, "settings.json"))):
        ask("~/.claude/settings.json",
            "This file wires every hook and permission rule for every session; a session "
            "editing it can unwire the guards that constrain it.", t)
    if same_file(t, real(os.path.join(claude, "vault-config.json"))):
        ask("~/.claude/vault-config.json (gt's settings)",
            "This file holds the switches for every guard (protected_paths, test_gate, lockdown...); "
            "a session editing it can turn them off. Change a setting with gt_settings.py set, which "
            "asks for a person at a terminal.", t)

    # The LOTR gateway home (0.22.0, review of phases 68/70): gateway.json, the consent windows and the
    # connection and client records. Writing them rewrites what the gateway lets through.
    for lotr in _lotr_homes():
        if inside(t, real(lotr)):
            ask("the LOTR gateway home (%s)" % lotr,
                "gateway.json, the consent windows and the connection records decide what the gateway "
                "lets through; a session editing them can widen its own access. Change them with the "
                "lotr tool, or yourself from a terminal.", t)

    # -- add-on areas (0.22.0): skills, non-gt plugins, MCP config, Codex. Under the
    # protected_paths master switch above, with addon_writes to quiet just these.
    area = _addon_area(t, cwd, raw)
    if area:
        try:
            import gt_settings
            on = gt_settings.get("addon_writes") != "off"
        except Exception:                              # noqa: BLE001 - the default, on
            on = True
        if on:
            ask_addon(area, t)

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

    # The vault layout manifest and the vault's gt tools folder (0.22.0, phase 84 review H1). The
    # manifest decides where gt keeps its queue, broker and tools; the write broker RUNS the
    # gt_session.py and gt_task.py in the tools folder. The default folder and one the manifest
    # moved it to are both guarded (read through gt_vault_manifest, the one reader).
    if _is_file(t, raw, os.path.join(v, MANIFEST)):
        ask("the vault layout manifest (vault.manifest.json)",
            "It decides where gt keeps its queue, broker and tools folders, and the write broker "
            "runs the scripts in that tools folder; a session editing it can point gt at a folder "
            "it has filled itself.", t)
    for tools in _vault_tools_dirs(v):
        if _in_dir(t, raw, tools):
            ask("the vault's gt tools folder (%s)" % os.path.relpath(tools, v).replace(os.sep, "/"),
                "The write broker and the vault's own commands run the scripts here; a session "
                "editing them changes what runs on the owner's next drain. install.sh is what "
                "should write them.", t)

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
