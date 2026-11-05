#!/usr/bin/env python3
"""gt_lockdown -- how much Claude may run without asking: four levels the user chooses (0.20.5).

    gt_lockdown.py levels                     the four levels, in order
    gt_lockdown.py table [--markdown]         what each level allows (the chart in the docs)
    gt_lockdown.py rules LEVEL [--json]       the exact allow / deny rules a level writes
    gt_lockdown.py current                    the level this machine is at
    gt_lockdown.py shown                      the level as the ladder shows it (paranoid, when strict
                                              and the paranoid switch are both on)
    gt_lockdown.py apply LEVEL [--json]       move to LEVEL (writes ~/.claude/settings.json)
    gt_lockdown.py remove [--json]            back to strict: take out every rule gt added
    gt_lockdown.py explain [LEVEL]            the merged rules, each with who contributed it
    gt_lockdown.py refresh [--json]           re-merge the current level (after a module on/off)
    gt_lockdown.py sweep [--hook]             SessionStart: remove what has expired or whose module
                                              is off; never adds a rule

WHY. What wears users down is not missing capability but the number of approval prompts (owner,
2026-10-07). Claude Code asks before most commands and edits; in auto mode its classifier can
refuse outright, and the refusal itself says the cure: "the user can add a permission rule". A
lockdown level is a curated set of those rules, chosen by the USER -- at install, or with
`/gt:gt-settings lockdown <level>`. gt never loosens anything on its own.

    strict         nothing added: exactly what Claude Code asks today (the default)
    trust-local    read-only shell and git, edits in the project, running tests, local commits
    trust-network  + ssh/scp/rsync, git push/pull/fetch, curl/wget, package installs, web fetch
    insecure       + any shell command, any edit, web search

    paranoid       (0.21.0) a choice, never a stored level: strict plus the paranoid switch (locks on
                   gt's own installed code). gt_settings.py set lockdown paranoid does both; leaving
                   it turns the switch off, with the step-up that already needs.

At every level above strict, Claude's file tools are denied READ access to credential
files (ssh keys, cloud and gh credentials, Claude Code's login, .env files). That is a guard on
the Read tool only: once a level lets the shell run `cat` (trust-local and up), a command can
still print a file -- Core rule 3 and its hook remain the line against a secret entering the
session. The chart says so; do not describe these levels as a boundary.

HOW IT MERGES (the gt_sandbox pattern). Only gt's own entries are added or removed. Every rule gt
adds is recorded in ~/.claude/golden-thread/lockdown/state.json; switching level removes the
recorded rules the new level does not want and adds the ones it lacks, so a user's own rule --
even one identical to gt's -- is never touched. Existing deny rules (the user's, gt sandbox
mode's) are never removed or reordered, and Claude Code applies deny before allow. The previous
settings.json is copied to ~/.claude/golden-thread/backups/settings.json.<stamp>.lockdown before
any change; a level that changes nothing writes nothing. Every change is one line in
~/.claude/golden-thread/lockdown/log.jsonl. Restart Claude Code after a change.

PER-MODULE RULES (0.21.0). A module's module.json may carry

    "lockdown": {"<level>": {"allow": [...], "deny": [...]}}

and the rules of every module that is ON are merged with core's, cumulative by level like the
table above. Each rule carries its owner (`explain`), and switching the module off takes exactly
its rules out at the next `refresh` or session-start `sweep`. A module can never loosen a core
deny (a rule that reaches a credential path is refused), can never allow a LOTR consent-tier op
(those always ask, at every level), and cannot widen a level with a bare tool name below
`insecure`. A bad rule is reported by module name and ignored; core's rules are untouched. The
`strict` entry of a module is ignored: that level adds nothing.

TIMED GRANTS (0.21.0, D1). A grant is a Claude Code permission rule gt writes for one command or
pattern, with a lifetime. It is recorded in this module's ownership state (`grants`, each with its
`expires_at`), removed by the session-start `sweep` and by `gt_grant.py revoke`. It is a rule in
settings.json, never a hook that says "allow": gt's guards only ever tighten.

Stdlib only. Exit: 0 ok, 1 refused, 2 usage.
"""
import json
import os
import re
import shutil
import sys
import time
from fnmatch import fnmatchcase

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_sandbox as sb  # noqa: E402  (settings path, JSON load/merge helpers, atomic write)

STATE_SCHEMA = 1
LEVELS = ("strict", "trust-local", "trust-network", "insecure")
DEFAULT = "strict"
LABELS = {l: l.replace("-", " ") for l in LEVELS}
# 0.21.0 renamed the levels. BREAKING: a typed old name is refused like any unknown one. A level
# STORED by an earlier release is read under its new name, so neither the installer's "keep the
# current level" nor the session-start sweep mistakes it for the default and strips its rules;
# gt_machine_migrate.py ("lockdown-levels-renamed") rewrites the stored names.
FORMER_NAMES = {"very-secure": "strict", "mostly-secure": "trust-local", "partly-secure": "trust-network"}
# `paranoid` (0.21.0) is a rung on the ladder the person sees, not a level this module stores:
# picking it applies strict and turns gt_settings' paranoid switch on (gt_settings does both). It
# shows as paranoid whenever the stored level is strict and the switch is on, so nothing can drift.
SHORTCUTS = {"paranoid": "strict"}
CHOICES = LEVELS + tuple(SHORTCUTS)
KEYS = ("permissions.allow", "permissions.deny")

# Each level ADDS to the one below it; rules_for() accumulates.
_ADDS = {
    "strict": [],
    "trust-local": [
        "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(grep:*)",
        "Bash(rg:*)", "Bash(wc:*)", "Bash(stat:*)", "Bash(du:*)", "Bash(df:*)", "Bash(ps:*)",
        "Bash(which:*)", "Bash(pwd)", "Bash(file:*)", "Bash(diff:*)",
        "Bash(git status:*)", "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)",
        "Bash(git branch:*)", "Bash(git remote -v)", "Bash(git add:*)", "Bash(git commit:*)",
        "Bash(pytest:*)", "Bash(python3 -m pytest:*)", "Bash(python3 -m unittest:*)",
        "Bash(npm test:*)", "Bash(go test:*)", "Bash(cargo test:*)",
        "Edit(./**)",
    ],
    "trust-network": [
        "Bash(ssh:*)", "Bash(scp:*)", "Bash(rsync:*)",
        "Bash(git push:*)", "Bash(git fetch:*)", "Bash(git pull:*)",
        "WebFetch",
    ],
    "insecure": ["Bash", "Edit", "WebSearch"],
}
# Downloads and installs (0.22.0, tool vetting): trust-network allows these without a prompt ONLY
# while install_guard is on (warn or block), because then the guard scans what they fetch. With
# install_guard off they ask again -- otherwise this level would remove the last check on a
# download piped to a shell, or a package install. The SessionStart sweep removes them when the
# guard is turned off.
_GUARDED_BY_INSTALL_GUARD = {
    "trust-network": [
        "Bash(curl:*)", "Bash(wget:*)",
        "Bash(npm install:*)", "Bash(pip install:*)", "Bash(pip3 install:*)",
        "Bash(brew install:*)",
    ],
}


def _install_guard_on():
    """True when the install_guard setting is not `off` AND the guard is actually registered here.
    An unreadable setting counts as its default (warn), the same answer the guard itself reaches.
    A guard that is switched on but not wired (no wired file, no settings entry) checks nothing, so
    it must not be the reason curl and installs stop asking."""
    try:
        import gt_settings
        if gt_settings.get("install_guard") == "off":
            return False
    except Exception:
        pass
    return _install_guard_wired()


def _install_guard_wired():
    try:
        import gt_components
        events = gt_components.effective_hook_events()
    except Exception:
        return False
    for blocks in (events or {}).values():
        for b in blocks if isinstance(blocks, list) else []:
            for h in (b.get("hooks") or []) if isinstance(b, dict) else []:
                if "guard_install.sh" in ((h.get("command") or "") if isinstance(h, dict) else ""):
                    return True
    return False


# Denied to Claude's file tools at every level above strict.
CREDENTIAL_DENY = [
    "Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.gnupg/**)", "Read(~/.config/gh/**)",
    "Read(~/.claude/.credentials.json)", "Read(**/.env)",
]

# The chart: one row per level, read by `table` and by the docs check.
CHART = {
    "strict": ("nothing added -- exactly what Claude Code asks today (default)",
               "everything Claude Code asks today"),
    "trust-local": ("read-only shell (ls, cat, grep, wc, ...) and git (status, log, diff, "
                    "show, branch); edits in the project; running tests; git add/commit",
                    "network, remote hosts, pushes, installs, deletes, anything not listed"),
    "trust-network": ("trust local, plus ssh/scp/rsync, git push/pull/fetch, web fetch; "
                      "curl/wget and npm/pip/brew install while install_guard is on",
                      "any other shell command or edit outside the project"),
    "insecure": ("trust network, plus any shell command, any edit, web search",
                 "LOTR send/merge/delete still ask for consent"),
}
CHART_NOTE = ("At every level above strict, Claude's Read tool is denied credential files "
              "(ssh keys, cloud and gh credentials, Claude Code's login, .env). That guards the "
              "Read tool only: from trust local up a shell command can still print a file, so "
              "Core rule 3 (no secret's value in the session) remains the line. These levels "
              "reduce prompts; they are not a security boundary.")


class LockdownError(Exception):
    pass


# ------------------------------------------------------------------ the levels
def check_level(level):
    if level not in LEVELS:
        raise ValueError("unknown lockdown level %r: use one of %s" % (level, ", ".join(LEVELS)))
    return level


# A read-only command with a flag that writes a file or runs a program (0.22.0, validation F8): the
# trust-local allow list names `git diff:*`, and `git diff --output=<path>` writes there. Deny wins over
# allow, so these keep the command allowed and the flag asked for.
FLAG_DENY = ("Bash(git diff*--output*)", "Bash(git log*--output*)", "Bash(git show*--output*)",
             "Bash(rg*--pre*)", "Bash(grep*--pre*)")


def rules_for_core(level, guard=None):
    """Core's own rules for `level`, with no module's contribution.

    `guard`: None reads this machine (install_guard on and wired). The help page and its tests pass
    True, because they document the shipped default and must not depend on where they ran."""
    check_level(level)
    allow = []
    guarded = _install_guard_on() if guard is None else bool(guard)
    for l in LEVELS[:LEVELS.index(level) + 1]:
        allow.extend(_ADDS[l])
        if guarded:
            allow.extend(_GUARDED_BY_INSTALL_GUARD.get(l, []))
    deny = (list(CREDENTIAL_DENY) + list(FLAG_DENY)) if level != "strict" else []
    return {"allow": allow, "deny": deny}


def rules_for(level, h=None, plugin_root=None):
    """The merged rules for `level`: core's plus every enabled module's (0.21.0)."""
    core = rules_for_core(level)
    mod = module_rules(level, h, plugin_root)
    return {"allow": core["allow"] + mod["allow"], "deny": core["deny"] + mod["deny"]}


def table_markdown():
    rows = ["| Level | Runs without a prompt | Still asks |", "|---|---|---|"]
    for l in LEVELS:
        runs, asks = CHART[l]
        rows.append("| **%s** | %s | %s |" % (LABELS[l], runs, asks))
    return "\n".join(rows) + "\n\n" + CHART_NOTE + "\n"


def table_text():
    out = []
    for l in LEVELS:
        runs, asks = CHART[l]
        out.append("%-14s runs without a prompt: %s" % (LABELS[l], runs))
        out.append("%-14s still asks:            %s" % ("", asks))
    out.append("")
    out.append(CHART_NOTE)
    return "\n".join(out)


# ------------------------------------------------------------------ per-module rules (0.21.0)
CORE = "core"
_RULE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]*(\([^()\n]+\))?$")
_BARE_TOOLS = ("Bash", "Edit", "Write", "Read", "WebFetch", "WebSearch", "MultiEdit")
# the lowest level at which a module may allow a whole tool (core itself allows WebFetch at partly)
_BARE_FROM = {"WebFetch": "trust-network"}


def _split_rule(rule):
    m = re.match(r"^([^()]+?)(?:\((.*)\))?$", rule)
    return (m.group(1), m.group(2)) if m else (rule, None)


def _is_consent_allow(rule):
    """An allow rule that would cover a LOTR consent-tier op: the op itself, or the whole server.
    Consent ops ask at every level, so no level, module or grant may allow one."""
    base, _ = _split_rule(rule)
    if not base.startswith("mcp__"):
        return False
    if "call_consent" in base:
        return True
    return "lotr" in base and (base.count("__") < 2 or base.endswith("*"))


def _loosens_core_deny(rule):
    """True when allowing `rule` would reach what core's CREDENTIAL_DENY keeps closed."""
    tool, pat = _split_rule(rule)
    for d in CREDENTIAL_DENY:
        dt, dp = _split_rule(d)
        if tool != dt:
            continue
        if pat is None or pat == dp or fnmatchcase(dp, pat) or fnmatchcase(pat, dp):
            return True
    return False


def _star_before_end(rule):
    """True when a Bash rule has a `*` anywhere but its end. `python3 *gt_x.py list *` also
    matches `python3 -c '<anything>' gt_x.py list x`: Claude Code warns about the shape at every
    exit, and it approves far more than the subcommand it names."""
    tool, pat = _split_rule(rule)
    if tool != "Bash" or not pat:
        return False
    for tail in (":*", " *", "*"):
        if pat.endswith(tail):
            pat = pat[:-len(tail)]
            break
    return "*" in pat


def _judge_rule(rule, key, level):
    """-> None when a module may contribute `rule`, else the reason it is refused."""
    if not isinstance(rule, str) or not _RULE_RE.match(rule):
        return "%r is not a well-formed permission rule" % (rule,)
    if key == "allow":
        if _is_consent_allow(rule):
            return "%s would allow a consent-tier op, which always asks" % rule
        if _loosens_core_deny(rule):
            return "%s would loosen a core deny (credential files stay closed)" % rule
        if rule in _BARE_TOOLS and LEVELS.index(level) < LEVELS.index(
                _BARE_FROM.get(rule, "insecure")):
            return "%s allows a whole tool; that is only the %s level's to do" % (
                rule, _BARE_FROM.get(rule, "insecure"))
    return None


def _plugin_root(h=None, plugin_root=None):
    if plugin_root:
        return plugin_root
    try:
        import gt_components as gc
        return gc.plugin_root_from_settings(h)
    except Exception:                            # noqa: BLE001 - no modules is the safe answer
        return None


def _active_modules(h=None, plugin_root=None):
    """-> [(name, data)] of every module that is ON and declares a lockdown block, by name."""
    root = _plugin_root(h, plugin_root)
    if not root:
        return []
    try:
        import gt_components as gc
        mods = gc.active_modules(root, h)
    except Exception:                            # noqa: BLE001
        return []
    return sorted(((n, d) for n, _vd, d in mods if isinstance(d.get("lockdown"), dict)),
                  key=lambda x: x[0])


def module_problems(h=None, plugin_root=None):
    """Modules whose lockdown block is structurally malformed (so the module is off): by name."""
    root = _plugin_root(h, plugin_root)
    out = []
    if not root:
        return out
    try:
        import gt_components as gc
        mods = gc.discover_modules(root)
    except Exception:                            # noqa: BLE001
        return out
    for m in mods:
        bad = [r for r in m["reasons"] if r.startswith("lockdown")]
        if bad:
            out.append("module %s: lockdown ignored (%s)" % (m["name"], "; ".join(bad)))
    return out


MARKETPLACE = "golden-thread-plugin"


def _installed_scripts(plugin, h=None):
    """-> `<installPath>/scripts` of `plugin` as Claude Code installed it, or None."""
    p = os.path.join(sb.claude_dir(h), "plugins", "installed_plugins.json")
    d = sb._read_json(p, None)
    entries = ((d or {}).get("plugins") or {}).get("%s@%s" % (plugin, MARKETPLACE)) \
        if isinstance(d, dict) else None
    if isinstance(entries, dict):
        entries = [entries]
    for e in entries if isinstance(entries, list) else []:
        ip = (e or {}).get("installPath") if isinstance(e, dict) else None
        if isinstance(ip, str) and ip:
            return os.path.join(ip, "scripts").replace("\\", "/")
    return None


def _expand(rule, plugin, h=None):
    """-> (rule with {scripts} / {hooks} filled in, None) or (None, why it cannot be).

    A module names its script by the exact path it runs from, never by a leading `*`, so the
    rule matches that one file. The path holds the release, which is why install.sh refreshes
    the level after every install."""
    if "{scripts}" in rule:
        sd = _installed_scripts(plugin, h)
        if not sd:
            return None, "plugin %s is not installed, so {scripts} has no path" % plugin
        rule = rule.replace("{scripts}", sd)
    if "{hooks}" in rule:
        rule = rule.replace("{hooks}", os.path.join(sb.gt_home(h), "hooks").replace("\\", "/"))
    return rule, None


def _judge_module_rule(rule, key, level):
    """_judge_rule, plus what only a module may not do: allow a Bash rule with a `*` before its
    end. A module names its script by {scripts} or {hooks}; the rules a person writes for
    themselves (a grant, the testing profile) are theirs to shape."""
    why = _judge_rule(rule, key, level)
    if why is None and key == "allow" and _star_before_end(rule):
        why = ("%s has a * before its end, so it approves whatever is inserted there; "
               "name the script by {scripts} or {hooks} instead" % rule)
    return why


def module_rules(level, h=None, plugin_root=None):
    """The rules the enabled modules add at `level`, cumulative like core's.

    -> {"allow": [...], "deny": [...], "owners": {"allow": {rule: module}, "deny": {...}},
        "problems": ["module zed: ..."]}. A rule that is refused is named in `problems` and left
    out; it never raises and never touches core's rules."""
    check_level(level)
    out = {"allow": [], "deny": [], "owners": {"allow": {}, "deny": {}}, "problems": []}
    out["problems"].extend(module_problems(h, plugin_root))
    if level == DEFAULT:
        return out
    core = rules_for_core(level)
    seen = {"allow": set(core["allow"]), "deny": set(core["deny"])}
    for lvl in LEVELS[1:LEVELS.index(level) + 1]:
        for name, data in _active_modules(h, plugin_root):
            blk = data["lockdown"].get(lvl)
            if not isinstance(blk, dict):
                continue
            for key in ("allow", "deny"):
                for rule in blk.get(key) or []:
                    rule, why = _expand(rule, data.get("plugin") or "gt-" + name, h) \
                        if isinstance(rule, str) else (rule, None)
                    if why:
                        out["problems"].append("module %s: %s rule ignored: %s" % (name, key, why))
                        continue
                    why = _judge_module_rule(rule, key, lvl)
                    if why:
                        out["problems"].append("module %s: %s rule ignored: %s" % (name, key, why))
                        continue
                    if rule in seen[key]:
                        continue
                    seen[key].add(rule)
                    out[key].append(rule)
                    out["owners"][key][rule] = name
    return out


def explain(level, h=None, plugin_root=None):
    """-> [{"key", "rule", "source"}] for every rule `level` writes: core's, then each module's."""
    check_level(level)
    core = rules_for_core(level)
    mod = module_rules(level, h, plugin_root)
    rows = []
    for key in ("allow", "deny"):
        rows.extend({"key": key, "rule": r, "source": CORE} for r in core[key])
        rows.extend({"key": key, "rule": r, "source": mod["owners"][key][r]} for r in mod[key])
    return rows


# ------------------------------------------------------------------ state, log
def _dir(h=None):
    return os.path.join(sb.gt_home(h), "lockdown")


def state_path(h=None):
    return os.path.join(_dir(h), "state.json")


def log_path(h=None):
    return os.path.join(_dir(h), "log.jsonl")


def load_state(h=None):
    st = sb._read_json(state_path(h), None)
    if not isinstance(st, dict) or st.get("schema") != STATE_SCHEMA:
        return {"schema": STATE_SCHEMA, "level": DEFAULT, "added": {}}
    st.setdefault("added", {})
    st.setdefault("level", DEFAULT)
    return st


def stored_level(lvl):
    """A level as recorded on this machine -> the level it is now (an unknown one is the default)."""
    lvl = FORMER_NAMES.get(lvl, lvl)
    return lvl if lvl in LEVELS else DEFAULT


def current(h=None):
    return stored_level(load_state(h).get("level"))


def shown(level, paranoid_on):
    """A stored level and the paranoid switch -> the rung the ladder shows."""
    return "paranoid" if level == "strict" and paranoid_on else level


def shown_current(h=None):
    try:
        import gt_paranoid
        on = gt_paranoid.effective(gt_paranoid.MASTER)
    except Exception:                                      # noqa: BLE001
        on = False
    return shown(current(h), on)


def _log(entry, h=None):
    os.makedirs(_dir(h), exist_ok=True)
    entry = dict(entry, ts=time.strftime("%Y-%m-%d %H:%M:%S %z"))
    with open(log_path(h), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def _save_settings(d, h=None):
    p = sb.settings_path(h)
    if os.path.exists(p):
        backups = os.path.join(sb.gt_home(h), "backups")
        os.makedirs(backups, exist_ok=True)
        bak = os.path.join(backups, "settings.json.%s.lockdown" % time.strftime("%Y%m%d_%H%M%S"))
        if not os.path.exists(bak):
            shutil.copyfile(p, bak)
    sb._atomic_write(p, json.dumps(d, indent=2) + "\n")


# ------------------------------------------------------------------ apply / remove
def apply(level, h=None, plugin_root=None, add=True):
    """Move this machine to `level`. -> {"level", "from", "changes": [...]}

    add=False is the session-start sweep: it only takes out rules gt owns and the level no longer
    wants (a module switched off), and never adds one -- gt never loosens on its own."""
    check_level(level)
    try:
        d = sb.load_settings(h)          # refuses unreadable JSON before anything is written
    except sb.SandboxError as e:
        raise LockdownError(str(e).replace("sandbox mode", "the lockdown level"))
    st = load_state(h)
    was = stored_level(st.get("level"))
    if not add:
        level = was
    want = rules_for(level, h, plugin_root)
    modr = module_rules(level, h, plugin_root)
    changes, owned_after = [], {}
    owners_after = {}
    for key, target in (("permissions.allow", want["allow"]), ("permissions.deny", want["deny"])):
        short = key.split(".")[1]
        owned = list(st["added"].get(key) or [])
        old_owners = (st.get("owners") or {}).get(key) or {}
        lst = sb._get_list(d, key)
        # take out gt's rules the new level does not want
        for e in owned:
            if e not in target and lst is not None and e in lst:
                lst.remove(e)
                changes.append("removed %s %s" % (key, e))
        owned = [e for e in owned if e in target]
        # add what the level wants and the file lacks; only those become gt's
        for e in target if add else ():
            lst = sb._ensure_list(d, key) if lst is None else lst
            if e not in lst:
                lst.append(e)
                owned.append(e)
                changes.append("added %s %s" % (key, e))
        owned_after[key] = [e for e in owned if lst is not None and e in lst]
        mine = {e: (modr["owners"][short].get(e) or old_owners.get(e))
                for e in owned_after[key]}
        mine = {e: m for e, m in mine.items() if m and m != CORE}
        if mine:
            owners_after[key] = mine
        sb._prune_empty(d, key)
    if isinstance(d.get("permissions"), dict) and not d["permissions"]:
        del d["permissions"]
    if changes:
        _save_settings(d, h)
    new_state = {"schema": STATE_SCHEMA, "level": level,
                 "added": {k: v for k, v in owned_after.items() if v}}
    if owners_after:
        new_state["owners"] = owners_after
    if st.get("grants"):
        new_state["grants"] = st["grants"]
    if level == DEFAULT and not new_state["added"] and not new_state.get("grants"):
        try:
            os.unlink(state_path(h))
        except OSError:
            pass
    else:
        os.makedirs(_dir(h), exist_ok=True)
        sb._atomic_write(state_path(h), json.dumps(new_state, indent=2, sort_keys=True) + "\n")
    if changes or level != was:
        _log({"from": was, "level": level, "changes": len(changes)}, h)
    return {"level": level, "from": was, "changes": changes, "restart": bool(changes),
            "problems": modr["problems"]}


def remove(h=None):
    """Back to strict, and end every grant that has a lifetime (once, timed): `remove` is what
    the uninstall runs, and a timed rule left behind with its ownership record gone would never be
    swept. A `forever` grant is the user's own plain allow rule and stays."""
    rep = apply(DEFAULT, h)
    for gid, g in sorted(grants(h).items()):
        if g.get("kind") != "forever":
            grant_remove(gid, h, why="lockdown removed")
            rep["changes"].append("ended grant %s %s" % (gid, g.get("rule")))
            rep["restart"] = True
    return rep


def refresh(h=None, plugin_root=None):
    """Re-merge the level this machine is at (a module was switched on or off)."""
    return apply(current(h), h, plugin_root)


# ------------------------------------------------------------------ timed grants (D1)
def grants(h=None):
    return dict(load_state(h).get("grants") or {})


def _rule_in_use(rule, st, skip_id=None):
    for lst in (st.get("added") or {}).values():
        if rule in lst:
            return True
    for gid, g in (st.get("grants") or {}).items():
        if gid != skip_id and g.get("rule") == rule and not g.get("preexisting"):
            return True
    return False


def grant_add(grant, h=None):
    """Write `grant["rule"]` into permissions.allow and record the grant (with its expiry) in
    the ownership state. A rule the user already had is recorded as theirs: a revoke leaves it."""
    rule = grant["rule"]
    if _judge_rule(rule, "allow", "insecure") or _is_consent_allow(rule):
        raise LockdownError("refused: " + (_judge_rule(rule, "allow", "insecure") or
                                           "%s would allow a consent-tier op" % rule))
    try:
        d = sb.load_settings(h)
    except sb.SandboxError as e:
        raise LockdownError(str(e).replace("sandbox mode", "grants"))
    st = load_state(h)
    lst = sb._ensure_list(d, "permissions.allow")
    g = dict(grant)
    if rule in lst:
        g["preexisting"] = True
    else:
        lst.append(rule)
        _save_settings(d, h)
    st.setdefault("grants", {})[g["id"]] = g
    os.makedirs(_dir(h), exist_ok=True)
    sb._atomic_write(state_path(h), json.dumps(st, indent=2, sort_keys=True) + "\n")
    _log({"grant": g["id"], "rule": rule, "kind": g.get("kind"),
          "expires_at": g.get("expires_at")}, h)
    return g


def grant_record(grant, h=None):
    """Record a grant that is NOT a permission rule (0.21.0, the I/O brokers): the same ownership
    state, expiry, sweep and revoke as a rule grant, but nothing is written to settings.json. The
    disk-read broker uses it for "this session may read folder X for N minutes"; a broker consults
    it itself, so no Claude Code permission rule ever widens. `grant` carries id, kind, rule (an
    opaque label, never matched against a tool call), expires_at and its own fields."""
    g = dict(grant, virtual=True)
    st = load_state(h)
    st.setdefault("grants", {})[g["id"]] = g
    os.makedirs(_dir(h), exist_ok=True)
    sb._atomic_write(state_path(h), json.dumps(st, indent=2, sort_keys=True) + "\n")
    _log({"grant": g["id"], "rule": g.get("rule"), "kind": g.get("kind"), "virtual": True,
          "expires_at": g.get("expires_at")}, h)
    return g


def grant_remove(gid, h=None, why="revoked"):
    """End one grant: take its rule out of settings.json unless the user or a level owns it too."""
    st = load_state(h)
    g = (st.get("grants") or {}).get(gid)
    if not g:
        return None
    rule = g["rule"]
    if not g.get("virtual") and not g.get("preexisting") and not _rule_in_use(rule, st, skip_id=gid):
        try:
            d = sb.load_settings(h)
        except sb.SandboxError as e:
            raise LockdownError(str(e).replace("sandbox mode", "grants"))
        lst = sb._get_list(d, "permissions.allow")
        if lst is not None and rule in lst:
            lst.remove(rule)
            sb._prune_empty(d, "permissions.allow")
            if isinstance(d.get("permissions"), dict) and not d["permissions"]:
                del d["permissions"]
            _save_settings(d, h)
    del st["grants"][gid]
    if not st["grants"]:
        del st["grants"]
    if st.get("level") == DEFAULT and not st.get("added") and not st.get("grants"):
        try:
            os.unlink(state_path(h))
        except OSError:
            pass
    else:
        sb._atomic_write(state_path(h), json.dumps(st, indent=2, sort_keys=True) + "\n")
    _log({"grant": gid, "rule": rule, "ended": why}, h)
    return g


def grant_active(g, now):
    """A grant still allows while it is unspent and unexpired."""
    exp = g.get("expires_at")
    if exp is not None and now >= exp:
        return False
    return g.get("uses_left") is None or g.get("uses_left") > 0


def sweep(h=None, plugin_root=None, now=None):
    """The session-start sweep: end every expired or spent grant, and take out the rules of a
    module that is no longer on. Removes only; never adds a rule. -> {"ended": [ids], "changes"}"""
    now = time.time() if now is None else now
    ended = []
    for gid, g in sorted(grants(h).items()):
        if not grant_active(g, now):
            grant_remove(gid, h, why="expired" if (g.get("expires_at") is not None
                                                  and now >= g["expires_at"]) else "spent")
            ended.append(gid)
    rep = apply(current(h), h, plugin_root, add=False)
    try:                                     # a testing profile whose --until has passed (0.21.0, #40)
        import gt_testing_profile
        testing = gt_testing_profile.sweep(h, now=now)
    except Exception:                        # noqa: BLE001 - the sweep never fails a session start
        testing = []
    return {"ended": ended, "changes": rep["changes"], "testing": testing}


# ------------------------------------------------------------------ CLI
def _report(rep, as_json):
    if as_json:
        print(json.dumps(rep, indent=2))
        return
    if rep["changes"]:
        print("lockdown: %s -> %s (%d rule change(s); settings.json backed up first)"
              % (LABELS[rep["from"]], LABELS[rep["level"]], len(rep["changes"])))
        for c in rep["changes"]:
            print("  " + c)
        print("Restart Claude Code for the change to take effect.")
    else:
        print("lockdown: %s (nothing to change)" % LABELS[rep["level"]])


def _take(a, flag):
    if flag in a:
        i = a.index(flag)
        if i + 1 < len(a):
            v = a[i + 1]
            del a[i:i + 2]
            return v
        del a[i]
    return None


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in a
    a = [x for x in a if x != "--json"]
    root = _take(a, "--plugin-root")
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if a else 2
    cmd, rest = a[0], a[1:]
    try:
        if cmd == "levels":
            print("\n".join(LEVELS))
        elif cmd == "table":
            print(table_markdown() if "--markdown" in rest else table_text())
        elif cmd == "rules" and len(rest) == 1:
            r = rules_for(rest[0], plugin_root=root)
            if as_json:
                print(json.dumps(r, indent=2))
            else:
                for k in ("allow", "deny"):
                    for e in r[k]:
                        print("%-5s %s" % (k, e))
        elif cmd == "current":
            print(current())
        elif cmd == "shown":
            print(shown_current())
        elif cmd == "explain" and len(rest) <= 1:
            lvl = rest[0] if rest else current()
            rows = explain(lvl, plugin_root=root)
            probs = module_rules(lvl, plugin_root=root)["problems"]
            if as_json:
                print(json.dumps({"level": lvl, "rules": rows, "problems": probs}, indent=2))
            else:
                print("level: %s" % LABELS[lvl])
                for r in rows:
                    print("%-5s %-45s %s" % (r["key"], r["rule"], r["source"]))
                for p in probs:
                    print("note: " + p)
        elif cmd == "refresh" and not rest:
            _report(refresh(plugin_root=root), as_json)
        elif cmd == "sweep":
            rep = sweep(plugin_root=root)
            if as_json:
                print(json.dumps(rep, indent=2))
            elif rep["ended"] or rep["changes"]:
                print("lockdown sweep: ended %d grant(s), %d rule change(s)"
                      % (len(rep["ended"]), len(rep["changes"])))
        elif cmd == "apply" and len(rest) == 1:
            _report(apply(rest[0], plugin_root=root), as_json)
        elif cmd == "remove" and not rest:
            _report(remove(), as_json)
        else:
            print("usage: gt_lockdown.py levels|table|rules LEVEL|current|shown|apply LEVEL|remove|"
                  "explain [LEVEL]|refresh|sweep", file=sys.stderr)
            return 2
    except (ValueError, LockdownError) as e:
        print("✗ %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
