#!/usr/bin/env python3
"""gt_lockdown -- how much Claude may run without asking: four levels the user chooses (0.20.5).

    gt_lockdown.py levels                     the four levels, in order
    gt_lockdown.py table [--markdown]         what each level allows (the chart in the docs)
    gt_lockdown.py rules LEVEL [--json]       the exact allow / deny rules a level writes
    gt_lockdown.py current                    the level this machine is at
    gt_lockdown.py apply LEVEL [--json]       move to LEVEL (writes ~/.claude/settings.json)
    gt_lockdown.py remove [--json]            back to very-secure: take out every rule gt added

WHY. What wears users down is not missing capability but the number of approval prompts (owner,
2026-10-07). Claude Code asks before most commands and edits; in auto mode its classifier can
refuse outright, and the refusal itself says the cure: "the user can add a permission rule". A
lockdown level is a curated set of those rules, chosen by the USER -- at install, or with
`/gt:gt-settings lockdown <level>`. gt never loosens anything on its own.

    very-secure    nothing added: exactly what Claude Code asks today (the default)
    mostly-secure  read-only shell and git, edits in the project, running tests, local commits
    partly-secure  + ssh/scp/rsync, git push/pull/fetch, curl/wget, package installs, web fetch
    insecure       + any shell command, any edit, web search

At every level above very-secure, Claude's file tools are denied READ access to credential
files (ssh keys, cloud and gh credentials, Claude Code's login, .env files). That is a guard on
the Read tool only: once a level lets the shell run `cat` (mostly-secure and up), a command can
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

Stdlib only. Exit: 0 ok, 1 refused, 2 usage.
"""
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_sandbox as sb  # noqa: E402  (settings path, JSON load/merge helpers, atomic write)

STATE_SCHEMA = 1
LEVELS = ("very-secure", "mostly-secure", "partly-secure", "insecure")
DEFAULT = "very-secure"
LABELS = {l: l.replace("-", " ") for l in LEVELS}
KEYS = ("permissions.allow", "permissions.deny")

# Each level ADDS to the one below it; rules_for() accumulates.
_ADDS = {
    "very-secure": [],
    "mostly-secure": [
        "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(grep:*)",
        "Bash(rg:*)", "Bash(wc:*)", "Bash(stat:*)", "Bash(du:*)", "Bash(df:*)", "Bash(ps:*)",
        "Bash(which:*)", "Bash(pwd)", "Bash(file:*)", "Bash(diff:*)",
        "Bash(git status:*)", "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)",
        "Bash(git branch:*)", "Bash(git remote -v)", "Bash(git add:*)", "Bash(git commit:*)",
        "Bash(pytest:*)", "Bash(python3 -m pytest:*)", "Bash(python3 -m unittest:*)",
        "Bash(npm test:*)", "Bash(go test:*)", "Bash(cargo test:*)",
        "Edit(./**)",
    ],
    "partly-secure": [
        "Bash(ssh:*)", "Bash(scp:*)", "Bash(rsync:*)",
        "Bash(git push:*)", "Bash(git fetch:*)", "Bash(git pull:*)",
        "Bash(curl:*)", "Bash(wget:*)",
        "Bash(npm install:*)", "Bash(pip install:*)", "Bash(pip3 install:*)",
        "Bash(brew install:*)",
        "WebFetch",
    ],
    "insecure": ["Bash", "Edit", "WebSearch"],
}
# Denied to Claude's file tools at every level above very-secure.
CREDENTIAL_DENY = [
    "Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.gnupg/**)", "Read(~/.config/gh/**)",
    "Read(~/.claude/.credentials.json)", "Read(**/.env)",
]

# The chart: one row per level, read by `table` and by the docs check.
CHART = {
    "very-secure": ("nothing added -- exactly what Claude Code asks today (default)",
                    "everything Claude Code asks today"),
    "mostly-secure": ("read-only shell (ls, cat, grep, wc, ...) and git (status, log, diff, "
                      "show, branch); edits in the project; running tests; git add/commit",
                      "network, remote hosts, pushes, installs, deletes, anything not listed"),
    "partly-secure": ("mostly secure, plus ssh/scp/rsync, git push/pull/fetch, curl/wget, "
                      "npm/pip/brew install, web fetch",
                      "any other shell command or edit outside the project"),
    "insecure": ("partly secure, plus any shell command, any edit, web search",
                 "LOTR send/merge/delete still ask for consent"),
}
CHART_NOTE = ("At every level above very secure, Claude's Read tool is denied credential files "
              "(ssh keys, cloud and gh credentials, Claude Code's login, .env). That guards the "
              "Read tool only: from mostly secure up a shell command can still print a file, so "
              "Core rule 3 (no secret's value in the session) remains the line. These levels "
              "reduce prompts; they are not a security boundary.")


class LockdownError(Exception):
    pass


# ------------------------------------------------------------------ the levels
def check_level(level):
    if level not in LEVELS:
        raise ValueError("unknown lockdown level %r: use one of %s" % (level, ", ".join(LEVELS)))
    return level


def rules_for(level):
    check_level(level)
    allow = []
    for l in LEVELS[:LEVELS.index(level) + 1]:
        allow.extend(_ADDS[l])
    deny = list(CREDENTIAL_DENY) if level != "very-secure" else []
    return {"allow": allow, "deny": deny}


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


def current(h=None):
    lvl = load_state(h).get("level")
    return lvl if lvl in LEVELS else DEFAULT


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
def apply(level, h=None):
    """Move this machine to `level`. -> {"level", "from", "changes": [...]}"""
    check_level(level)
    try:
        d = sb.load_settings(h)          # refuses unreadable JSON before anything is written
    except sb.SandboxError as e:
        raise LockdownError(str(e).replace("sandbox mode", "the lockdown level"))
    st = load_state(h)
    was = st.get("level") if st.get("level") in LEVELS else DEFAULT
    want = rules_for(level)
    changes, owned_after = [], {}
    for key, target in (("permissions.allow", want["allow"]), ("permissions.deny", want["deny"])):
        owned = list(st["added"].get(key) or [])
        lst = sb._get_list(d, key)
        # take out gt's rules the new level does not want
        for e in owned:
            if e not in target and lst is not None and e in lst:
                lst.remove(e)
                changes.append("removed %s %s" % (key, e))
        owned = [e for e in owned if e in target]
        # add what the level wants and the file lacks; only those become gt's
        for e in target:
            lst = sb._ensure_list(d, key) if lst is None else lst
            if e not in lst:
                lst.append(e)
                owned.append(e)
                changes.append("added %s %s" % (key, e))
        owned_after[key] = [e for e in owned if lst is not None and e in lst]
        sb._prune_empty(d, key)
    if isinstance(d.get("permissions"), dict) and not d["permissions"]:
        del d["permissions"]
    if changes:
        _save_settings(d, h)
    new_state = {"schema": STATE_SCHEMA, "level": level,
                 "added": {k: v for k, v in owned_after.items() if v}}
    if level == DEFAULT and not new_state["added"]:
        try:
            os.unlink(state_path(h))
        except OSError:
            pass
    else:
        os.makedirs(_dir(h), exist_ok=True)
        sb._atomic_write(state_path(h), json.dumps(new_state, indent=2, sort_keys=True) + "\n")
    if changes or level != was:
        _log({"from": was, "level": level, "changes": len(changes)}, h)
    return {"level": level, "from": was, "changes": changes, "restart": bool(changes)}


def remove(h=None):
    return apply(DEFAULT, h)


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


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in a
    a = [x for x in a if x != "--json"]
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
            r = rules_for(rest[0])
            if as_json:
                print(json.dumps(r, indent=2))
            else:
                for k in ("allow", "deny"):
                    for e in r[k]:
                        print("%-5s %s" % (k, e))
        elif cmd == "current":
            print(current())
        elif cmd == "apply" and len(rest) == 1:
            _report(apply(rest[0]), as_json)
        elif cmd == "remove" and not rest:
            _report(remove(), as_json)
        else:
            print("usage: gt_lockdown.py levels|table|rules LEVEL|current|apply LEVEL|remove",
                  file=sys.stderr)
            return 2
    except (ValueError, LockdownError) as e:
        print("✗ %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
