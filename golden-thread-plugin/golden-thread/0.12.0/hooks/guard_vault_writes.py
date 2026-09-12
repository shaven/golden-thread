#!/usr/bin/env python3
"""Deny a vault-mutating tool run that does not say WHICH vault it means.

## The incident

2026-09-11. A session about to migrate the vault to the 0.11.0 spool model did the
right thing first: it copied the vault to a scratch directory and ran the migration
there, to see the diff before touching anything real. `gt_log.py` took `--vault`, so
that half stayed in the copy. `gt_adr.py migrate` did NOT take one -- it resolved the
vault from `~/.claude/vault-config.json` -- so the same rehearsal, in the same loop,
migrated all 42 `decisions.md` files of the LIVE vault. Nothing was lost, the content
survived byte-for-byte, and the intent was the exact opposite of what happened.

A rehearsal that cannot be told where to rehearse is not a rehearsal.

## What this denies

A Bash command that invokes a known vault tool with a WRITING subcommand and names no
target: no `--vault`, no `--dry-run`, no `GT_VAULT` in the environment or in front of
the command. Read-only subcommands (`status`, `list`, `check`) are never denied, and
anything this cannot parse with certainty is allowed -- see below.

## FAIL OPEN, always

Every uncertainty allows: unparseable JSON, unparseable shell, an unknown tool, an
unknown subcommand, a missing command string. A guard that blocks work it merely does
not understand gets switched off, and then it guards nothing. The cost of a false
allow here is one careless write; the cost of a false deny is the whole mechanism.
"""
import json
import os
import re
import shlex
import sys

ALLOW = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}

# Subcommands that WRITE. Anything not listed here for a tool is treated as read-only
# and allowed. `ALL` means the tool writes however it is invoked.
ALL = "*"
MUTATORS = {
    "gt_adr.py": {"allocate", "merge", "migrate"},
    "gt_log.py": {"add", "merge", "migrate"},
    "gt_spool.py": {"add", "merge", "migrate"},
    "gt_tasks.py": ALL,          # always rewrites TASKS.md
    "vault_init.py": {"fresh", "create-project", "connect", "rename-project",
                      "merge-project", "archive-project", "install-core-rules"},
}

# Naming the target, in any of these forms, is enough.
TARGET_FLAGS = ("--vault", "--dry-run", "-n")


def allow():
    print(json.dumps(ALLOW))
    sys.exit(0)


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def segments(command):
    """Split a command line into the pieces that run as separate commands.

    Shell operators only -- this is not a parser, and it does not need to be. A piece
    it splits wrongly produces at worst an unrecognised tool, which allows.
    """
    return [s for s in re.split(r"&&|\|\||[;\n|]", command) if s.strip()]


def inspect(segment):
    """(tool, subcommand, tokens) for a recognised vault tool, else None."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return None                      # unbalanced quotes -> not our business
    for i, tok in enumerate(tokens):
        base = os.path.basename(tok)
        if base in MUTATORS:
            rest = tokens[i + 1:]
            sub = next((t for t in rest if not t.startswith("-")), "")
            return base, sub, tokens
    return None


def writes(tool, sub):
    allowed = MUTATORS[tool]
    return True if allowed is ALL else sub in allowed


def targeted(tokens, segment):
    """Does this command say which vault it means?"""
    for tok in tokens:
        if tok in TARGET_FLAGS or tok.startswith("--vault="):
            return True
    # GT_VAULT=... in front of the command, or exported into the session
    if re.search(r"\bGT_VAULT=", segment):
        return True
    if os.environ.get("GT_VAULT", "").strip():
        return True
    return False


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()

    if (payload.get("tool_name") or "") != "Bash":
        allow()

    command = (payload.get("tool_input") or {}).get("command") or ""
    if not command.strip():
        allow()

    for segment in segments(command):
        found = inspect(segment)
        if not found:
            continue
        tool, sub, tokens = found
        if not writes(tool, sub):
            continue
        if targeted(tokens, segment):
            continue
        deny(
            "BLOCKED by Core rule core_explicit_vault_target.\n\n"
            "  %s %s writes to a vault, and this command does not say which one.\n"
            "  It would resolve the vault from ~/.claude/vault-config.json -- your REAL\n"
            "  vault -- whatever you meant.\n\n"
            "Do this instead:\n"
            "  1. Rehearsing on a copy?   add --vault <copy>\n"
            "  2. Just want to see it?    add --dry-run\n"
            "  3. Meant the live vault?   add --vault \"$(python3 -c \"import json,os;\"\n"
            "     \"print(json.load(open(os.path.expanduser('~/.claude/vault-config.json')))\"\n"
            "     \"['vault_path'])\")\" -- say it, so the next reader can see you meant it.\n\n"
            "Why: on 2026-09-11 a rehearsal meant for a scratch copy migrated the live\n"
            "vault's 42 decisions.md files, because gt_adr.py migrate took no --vault.\n"
            % (tool, sub or "(no subcommand)")
        )

    allow()


if __name__ == "__main__":
    main()
