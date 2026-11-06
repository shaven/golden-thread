#!/usr/bin/env bash
# Foreign-checkout guard — PreToolUse hook (0.18.1). Not tied to a Core rule.
#
# LOCATION: ~/.claude/golden-thread/hooks/ — outside the vault, like its siblings.
# settings.json references it by absolute path, which must survive a vault move.
#
# WHAT: deny `git commit` / `git push` inside a checkout the user has DECLARED as owned by
# another machine (`foreign_checkouts` in ~/.claude/vault-config.json). With nothing
# declared it denies nothing. Everything else is in guard_foreign_checkout.py.
#
# STDIN MATTERS: the payload arrives on stdin, so the python is a real file and not a
# heredoc — a heredoc consumes stdin as the script and the guard fails open on every call.
#
# FAIL OPEN, SILENTLY: any error prints nothing and exits 0, so Claude Code's normal
# permission flow decides. Printing "allow" would skip the user's permission prompt.
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
python3 "$HERE/guard_foreign_checkout.py" 2>/dev/null || true
exit 0
