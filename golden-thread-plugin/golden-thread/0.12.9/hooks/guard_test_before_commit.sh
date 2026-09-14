#!/usr/bin/env bash
# Core-rule Validated mechanism for `core_test_before_commit` — PreToolUse hook.
#
# LOCATION: ~/.claude/golden-thread/hooks/ — outside the vault, like its siblings.
# settings.json references it by absolute path, which must survive a vault move.
#
# WHAT: before a Bash command runs, deny a `git commit` that would carry code whose
# tests have not been seen to pass. The rule text lives in
# core-rules/core_test_before_commit.md and is NOT duplicated here.
#
# STDIN MATTERS: the payload arrives on stdin, so the python is a real file and not a
# heredoc — a heredoc consumes stdin as the script and the guard fails open on every
# call. That cost the session-claim guard a day in 2026-08-28.
#
# FAIL OPEN, ALWAYS: any error lets the command through (see below).
#
# SILENT, NOT "allow" (2026-09-13): failing open means printing NOTHING and exiting 0.
# Claude Code's hooks reference: permissionDecision "allow" skips the user's permission
# prompt; exit 0 with no output is no decision and the normal permission flow applies.
# The old fallback echoed "allow", so a broken guard approved tool calls outright.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
python3 "$HERE/guard_test_before_commit.py" 2>/dev/null || true
exit 0
