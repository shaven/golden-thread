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
# FAIL OPEN, ALWAYS: any error lets the command through (see below). This guard sits
# in front of EVERY Bash call in every session, so a wrong deny is far more expensive than a missed one.
#
# SILENT, NOT "allow" (2026-09-13): failing open means printing NOTHING and exiting 0.
# Claude Code's hooks reference: permissionDecision "allow" skips the user's permission
# prompt; exit 0 with no output is no decision and the normal permission flow applies.
# The old fallback echoed "allow", so a broken guard approved tool calls outright.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
python3 "$HERE/guard_vault_writes.py" "$HERE" 2>/dev/null || true
exit 0
