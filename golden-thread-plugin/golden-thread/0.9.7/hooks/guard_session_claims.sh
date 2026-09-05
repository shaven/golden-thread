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
# FAIL OPEN, ALWAYS: any error allows the write. A guard that blocks wrongly makes
# every session unusable — worse than the corruption it prevents.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ALLOW='{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
python3 "$HERE/guard_session_claims.py" "$HERE" 2>/dev/null || echo "$ALLOW"
