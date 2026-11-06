#!/usr/bin/env bash
# Scope-contract guard — PreToolUse hook (0.21.4). Not tied to a Core rule.
#
# LOCATION: ~/.claude/golden-thread/hooks/ — outside the vault, like its siblings.
# settings.json (or the plugin's hooks.json, through a wired file) references it by absolute
# path, which must survive a vault move.
#
# WHAT: before a Bash command runs, check a `git commit` against the scope contract in the
# repository's approved plan (the `scope:` block /gt:gt-plan writes), the way /gt:gt-allin-commit
# does. With scope_gate warn it only ADDS the deviation report as context; with block it refuses a
# commit that holds an unjustified deviation. With no approved plan for the branch it says
# nothing. Everything else is in guard_scope_contract.py.
#
# STDIN MATTERS: the payload arrives on stdin, so the python is a real file and not a
# heredoc — a heredoc consumes stdin as the script and the guard fails open on every call.
#
# FAIL OPEN, ALWAYS: any error lets the command through. SILENT, NOT "allow": failing open means
# printing NOTHING and exiting 0. Claude Code's hooks reference: permissionDecision "allow" skips
# the user's permission prompt; exit 0 with no output is no decision and the normal permission
# flow applies.
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
python3 "$HERE/guard_scope_contract.py" 2>/dev/null || true
exit 0
