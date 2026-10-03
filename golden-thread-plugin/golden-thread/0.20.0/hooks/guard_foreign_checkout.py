#!/usr/bin/env python3
"""Deny a `git commit` or `git push` made inside a checkout another machine owns.

    guard_foreign_checkout.py                      # the PreToolUse hook (payload on stdin)
    guard_foreign_checkout.py list                 # the declared foreign checkouts
    guard_foreign_checkout.py add <path> [--label L] [--route R] [--dry-run]
    guard_foreign_checkout.py remove <path> [--dry-run]

## The incident

2026-09-11. A session asked to "push everything" committed a release into a checkout in a
shared folder that a SECOND machine owns, then tried to push it to that machine's remote. The
push failed for want of a credential that was never meant to be here; the session diagnosed a
missing credential and began editing the remote URL so only a token would be prompted --
steadily removing the one thing between it and a wrong push. The owner stopped it. The rule
was written down three times, and quoted by the same session two turns earlier. Prose cannot
fire at the moment of the mistake, and that moment looks exactly like an ordinary
authentication problem.

## Ownership is DECLARED, never guessed

The user lists foreign checkouts in `~/.claude/vault-config.json`:

    "foreign_checkouts": [
        "/abs/path/to/checkout",
        {"path": "/abs/other", "label": "the build machine", "route": "copygt.sh on that machine"}
    ]

Nothing is inferred from a path, a remote host, a hostname or a missing credential. Inferring
from the remote is wrong twice: it fires on a credential that merely expired, and it says
nothing at the `commit`, which is the step that creates the mess. A path matches any working
directory INSIDE it, after realpath, so a symlinked or relative cwd cannot slip past.

## What is denied

`git commit` and `git push` (including `git -C <dir> ...` and after a `cd <dir>` in the same
command line) whose target directory is inside a declared checkout. Every other git verb --
status, diff, log, fetch -- and every file write is untouched: syncing files into a foreign
checkout is the supported workflow. The guard refuses two verbs, not a directory.

Deliberate override, visible in the transcript: put `GT_FOREIGN_CHECKOUT=allow` in the command
(e.g. `GT_FOREIGN_CHECKOUT=allow git push`). Switch the guard off machine-wide with
`gt_settings.py set foreign_checkout_guard off`.

## FAIL OPEN, always

No declaration, a malformed or unreadable config, a declared path that does not exist, a
command line it cannot analyse with certainty (`$(...)`, backticks, `eval`, `sh -c`, a `cd`
to a variable), any exception: no objection -- no output, exit 0, and Claude Code's normal
permission flow decides. Same discipline as guard_vault_writes and guard_test_before_commit:
a guard that blocks work it merely does not understand gets switched off, and then it guards
nothing.
"""
import argparse
import json
import os
import re
import shlex
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

CONFIG = os.path.expanduser("~/.claude/vault-config.json")
KEY = "foreign_checkouts"
SETTING = "foreign_checkout_guard"
OVERRIDE = "GT_FOREIGN_CHECKOUT=allow"
VERBS = ("commit", "push")
DEFAULT_ROUTE = ("leave the commit and push to the machine that owns this checkout: sync the "
                 "files here if that is the workflow, and let its owner commit and push there")


class Uncertain(Exception):
    """The command could not be analysed with certainty -> no objection."""


# -- hook output: a guard only ever objects; otherwise it prints NOTHING ----------------------
def no_objection():
    sys.exit(0)


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


# -- declarations -----------------------------------------------------------------------------
def load_config():
    """-> dict, or None when the file is missing/unreadable/malformed (guard disabled)."""
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def declarations(cfg):
    """-> [(realpath, label, route)] for every declared checkout that exists. A malformed
    entry is skipped; a malformed list disables the guard (returns [])."""
    raw = (cfg or {}).get(KEY)
    if not isinstance(raw, list):
        return []
    out = []
    for e in raw:
        if isinstance(e, str):
            e = {"path": e}
        if not isinstance(e, dict) or not isinstance(e.get("path"), str) or not e["path"]:
            continue
        p = os.path.expanduser(e["path"])
        if not os.path.isabs(p) or not os.path.isdir(p):
            continue
        out.append((os.path.realpath(p), str(e.get("label") or "").strip(),
                    str(e.get("route") or "").strip()))
    return out


def enabled():
    try:
        import gt_settings
        return (gt_settings.get(SETTING) or "on") != "off"
    except Exception:
        return True


def owner_of(path, decl):
    """The declaration containing `path` (by realpath containment), or None."""
    try:
        real = os.path.realpath(path)
    except Exception:
        return None
    for root, label, route in decl:
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            return root, label, route
    return None


# -- command analysis -------------------------------------------------------------------------
def strip_heredocs(command):
    """Heredoc bodies are data, not commands (shared with guard_vault_writes when present)."""
    try:
        from guard_vault_writes import strip_heredocs as impl
        return impl(command)
    except Exception:
        return command


def _resolve(base, d):
    if d is None:
        return base
    if "$" in d or "~" in d[1:]:
        raise Uncertain(d)
    d = os.path.expanduser(d)
    # Native Windows (0.20.0): the Bash tool is Git Bash, whose own spelling of C:\x is /c/x.
    # Read literally that is \c\x on the current drive and the checkout is never matched.
    if os.name == "nt" and re.match(r"^/[A-Za-z](/|$)", d):
        d = d[1].upper() + ":/" + d[3:]
    return d if os.path.isabs(d) else os.path.join(base, d)


OPERATORS = {";", "&&", "||", "|", "&", "|&", ";;", ")", "}"}


def _segments(body):
    """Split a command line into simple commands, respecting quotes.

    Newlines separate commands; inside quotes they are message text, so turning them into
    `;` before tokenising changes nothing a quoted argument means. A commit message with a
    `;` in it, or Claude Code's usual `-m "$(cat <<'EOF' ... EOF\n)"`, stays ONE argument.
    """
    lx = shlex.shlex(body.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lx.whitespace_split = True
    try:
        toks = list(lx)
    except ValueError:
        raise Uncertain("unbalanced quotes")
    seg, prev = [], ""
    for t in toks:
        # An UNQUOTED substitution runs a command this cannot see: `$(` tokenises as "$", "(".
        # A quoted one is a single argument (a commit message) and is harmless.
        if (t == "(" and prev.endswith("$")) or t.startswith("`"):
            raise Uncertain("command substitution")
        prev = t
        if t in OPERATORS or (t and set(t) <= set(";&|")):
            if seg:
                yield seg
            seg = []
        elif t in ("(", "{"):
            continue                              # a subshell/group opens: same commands
        else:
            seg.append(t)
    if seg:
        yield seg


def _dynamic(tok):
    return "$" in tok or "`" in tok


def git_targets(command, cwd):
    """-> [(verb, directory)] for every `git commit`/`git push` the command would run.

    Raises Uncertain when the command word, a `cd` target or a `-C` path is computed at run
    time (`$VAR`, `$(...)`, backticks), or the line runs through eval / `sh -c`: its effect
    cannot be known statically, and the guard then raises no objection.
    """
    out = []
    here = cwd
    for toks in _segments(strip_heredocs(command)):
        while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
            toks = toks[1:]                       # leading VAR=value assignments
        if not toks:
            continue
        cmd = toks[0]
        if _dynamic(cmd) or cmd in ("eval", "exec", "source", "."):
            raise Uncertain(cmd)
        if os.path.basename(cmd) in ("sh", "bash", "zsh", "dash", "ksh") and "-c" in toks[1:]:
            raise Uncertain("nested shell")
        if cmd in ("cd", "pushd"):
            if len(toks) < 2 or toks[1] == "-" or _dynamic(toks[1]):
                raise Uncertain("cd with no fixed target")
            here = _resolve(here, toks[1])
            continue
        if os.path.basename(cmd) != "git":
            continue
        i, target = 1, here
        while i < len(toks) and toks[i].startswith("-"):
            t = toks[i]
            if t in ("--help", "-h", "--version"):
                target = None
                break
            if t == "-C":
                if i + 1 >= len(toks) or _dynamic(toks[i + 1]):
                    raise Uncertain("-C with no fixed path")
                target = _resolve(target, toks[i + 1])
                i += 2
                continue
            if t == "-c":
                i += 2
                continue
            if t.startswith(("--git-dir", "--work-tree", "--namespace")):
                raise Uncertain(t)
            i += 1
        if target is None or i >= len(toks):
            continue
        verb = toks[i]
        if _dynamic(verb):
            raise Uncertain(verb)
        if verb in VERBS and "--help" not in toks[i + 1:] and "-h" not in toks[i + 1:]:
            out.append((verb, target))
    return out


def reason_text(verb, root, label, route, cwd):
    owner = (" (owned by %s)" % label) if label else ""
    return (
        "BLOCKED: `git %s` inside %s, a checkout this machine has DECLARED FOREIGN%s.\n\n"
        "  Ownership is declared in ~/.claude/vault-config.json (`%s`), never guessed.\n"
        "  A commit here creates work this machine cannot publish; a push here goes to a\n"
        "  remote this machine was never meant to write. A failing push in this checkout is\n"
        "  the boundary, not an authentication problem to work around.\n\n"
        "Supported route: %s.\n\n"
        "Read-only git here (status, diff, log, fetch) and file writes are unaffected.\n"
        "A deliberate one-off exception, visible in the transcript:\n"
        "  %s git %s ...\n"
        "List or change declarations: python3 %s list | remove <path>\n\n"
        "Why: 2026-09-11 a session committed a release into another machine's checkout and\n"
        "started rewriting its remote URL to get the push through."
        % (verb, root, owner, KEY, route or DEFAULT_ROUTE, OVERRIDE, verb,
           os.path.join("~/.claude/golden-thread/hooks", os.path.basename(__file__))))


def hook():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        no_objection()
    try:
        if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
            no_objection()
        command = (payload.get("tool_input") or {}).get("command") or ""
        if "git" not in command or not any(v in command for v in VERBS):
            no_objection()
        if OVERRIDE in command:
            no_objection()                      # the deliberate exception, said out loud
        if not enabled():
            no_objection()
        decl = declarations(load_config())
        if not decl:
            no_objection()                      # nothing declared: inert by default
        cwd = payload.get("cwd") or os.getcwd()
        try:
            targets = git_targets(command, cwd)
        except Uncertain:
            no_objection()
        for verb, d in targets:
            hit = owner_of(d, decl)
            if hit:
                deny(reason_text(verb, hit[0], hit[1], hit[2], cwd))
    except SystemExit:
        raise
    except Exception:
        pass
    no_objection()


# -- declaration management (writes ~/.claude/vault-config.json, never the vault) -----------
def _entries(cfg):
    raw = cfg.get(KEY)
    return list(raw) if isinstance(raw, list) else []


def _path_of(e):
    return e if isinstance(e, str) else (e.get("path") if isinstance(e, dict) else None)


def _save(cfg):
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, CONFIG)


def cli(argv):
    ap = argparse.ArgumentParser(prog="guard_foreign_checkout.py",
                                 description="declare checkouts another machine owns")
    ap.add_argument("--vault", help="accepted for the CLI contract; declarations live in "
                                    "~/.claude/vault-config.json, not the vault")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a_add = sub.add_parser("add")
    a_add.add_argument("path")
    a_add.add_argument("--label", help="who owns it, named in the denial")
    a_add.add_argument("--route", help="the supported way to publish from it, named in the denial")
    a_add.add_argument("--dry-run", action="store_true")
    a_rm = sub.add_parser("remove")
    a_rm.add_argument("path")
    a_rm.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    cfg = load_config()
    if a.cmd == "list":
        if cfg is None:
            print("no readable %s -- the guard is inert" % CONFIG)
            return 0
        rows = _entries(cfg)
        live = {r for r, _, _ in declarations(cfg)}
        if not rows:
            print("no foreign checkouts declared -- the guard denies nothing")
        for e in rows:
            p = _path_of(e) or "?"
            real = os.path.realpath(os.path.expanduser(p)) if isinstance(p, str) else p
            extra = ""
            if isinstance(e, dict):
                extra = "".join("  %s: %s" % (k, e[k]) for k in ("label", "route") if e.get(k))
            print("%s %s%s" % ("active " if real in live else "MISSING", p, extra))
        print("guard setting %s: %s" % (SETTING, "on" if enabled() else "off"))
        return 0
    if cfg is None:
        print("refusing to write %s: it is missing or unreadable. Run /gt:gt-init first." % CONFIG)
        return 2
    path = os.path.realpath(os.path.expanduser(a.path))
    rows = _entries(cfg)
    keep = [e for e in rows
            if not (isinstance(_path_of(e), str)
                    and os.path.realpath(os.path.expanduser(_path_of(e))) == path)]
    if a.cmd == "add":
        if not os.path.isdir(path):
            print("not a directory: %s" % path)
            return 2
        e = {"path": path}
        if a.label:
            e["label"] = a.label
        if a.route:
            e["route"] = a.route
        new = keep + [e if len(e) > 1 else path]
        verb = "declared foreign"
    else:
        if len(keep) == len(rows):
            print("not declared: %s" % path)
            return 1
        new = keep
        verb = "no longer declared foreign"
    if a.dry_run:
        print("dry-run: %s would be %s" % (path, verb))
        return 0
    cfg[KEY] = new
    _save(cfg)
    print("%s: %s" % (path, verb))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    hook()
