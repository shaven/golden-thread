#!/usr/bin/env bash
# Core-rule Validated mechanism for `core_explicit_vault_target` — PreToolUse hook.
#
# LOCATION: ~/.claude/golden-thread/hooks/ — outside the vault, like its three
# siblings. settings.json references it by absolute path, which must survive a
# project rename or a vault move.
#
# WHAT: before a Bash command runs, deny it if it invokes a vault tool with a WRITING
# subcommand and names no target — no --vault, no --dry-run, no GT_VAULT. The rule
# text lives in core-rules/core_explicit_vault_target.md and is NOT duplicated here.
#
# STDIN MATTERS: the payload arrives on stdin, so the python must be a real file, not
# a heredoc — a heredoc consumes stdin as the script and the guard fails open on every
# call. This cost the session-claim guard a day in 2026-08-28; do not reintroduce it.
#
# FAIL OPEN, ALWAYS: any error allows the command. This guard sits in front of EVERY
# Bash call in every session, so a wrong deny is far more expensive than a missed one.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ALLOW='{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
python3 "$HERE/guard_vault_writes.py" "$HERE" 2>/dev/null || echo "$ALLOW"
