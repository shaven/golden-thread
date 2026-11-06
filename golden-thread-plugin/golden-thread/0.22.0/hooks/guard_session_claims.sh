#!/usr/bin/env bash
# Core-rule Validated mechanism for `core_concurrent_session_claim` — PreToolUse hook.
#
# LOCATION: ~/.claude/golden-thread/hooks/ — deliberately OUTSIDE the vault, like the
# other two. settings.json references it by absolute path, which must survive a
# project rename or a vault move.
#
# WHAT: before a Write/Edit against a vault file, deny it if another LIVE session has
# claimed that path in Projects/golden-thread/sessions/. The rule text lives in
# core-rules/core_concurrent_session_claim.md and is NOT duplicated here.
#
# STDIN MATTERS: the payload arrives on stdin, so the python must be a real file, not
# a heredoc. A heredoc would consume stdin as the script and the guard would silently
# fail open on every call (this happened while writing it, 2026-08-28).
#
# FAIL OPEN, ALWAYS: any error lets the write through (see below). A guard that
# blocks wrongly makes every session unusable — worse than the corruption it prevents.
#
# SILENT, NOT "allow" (2026-09-13): failing open means printing NOTHING and exiting 0.
# Claude Code's hooks reference: permissionDecision "allow" skips the user's permission
# prompt; exit 0 with no output is no decision and the normal permission flow applies.
# The old fallback echoed "allow", so a broken guard approved tool calls outright.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/gt_python.sh" 2>/dev/null || true   # Windows: the real interpreter, not the Store stub
# paranoid.signed_manifest (0.21.0): with a sealed record, check this hook's files before it runs. With
# the group off this is one test(1) and nothing else. Exit 3 = the check printed a refusal (a deny
# for a guard, a systemMessage otherwise) and this hook must not run.
if [ -f "${HOME:-${USERPROFILE:-/nonexistent}}/.claude/golden-thread/integrity.json" ] || [ -f "${HOME:-${USERPROFILE:-/nonexistent}}/.claude/golden-thread/paranoid-sealed.json" ]; then
  python3 "$HERE/gt_integrity_gate.py" check guard "$HERE/$(basename "$0")" 2>/dev/null
  [ "$?" -eq 3 ] && exit 0
fi
python3 "$HERE/guard_session_claims.py" "$HERE" 2>/dev/null || true
exit 0
