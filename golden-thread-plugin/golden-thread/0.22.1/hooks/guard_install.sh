#!/usr/bin/env bash
# Install guard -- PreToolUse hook (0.22.0, `install_guard` off|warn|block, default warn).
#
# LOCATION: ~/.claude/golden-thread/hooks/ -- outside the vault, like its siblings.
#
# WHAT: before a Bash or PowerShell command installs something (git clone, npx, pip install,
# npm i, claude mcp add, a download piped to a shell, ...), fetch it into a hardened temp dir WITHOUT running
# any of it and scan it with gt_intake_scan. Serious findings ask (warn) or deny (block); other
# findings are notes; a timeout or an incomplete scan asks. Everything else is in
# gt_install_guard.py, which also offers `scan <spec>` by hand.
#
# STDIN MATTERS: the payload arrives on stdin, so the python is a real file and not a
# heredoc -- a heredoc consumes stdin as the script and the guard fails open on every call.
#
# CLEAN PRINTS NOTHING: no output and exit 0, so Claude Code's normal permission flow decides.
# Printing "allow" would skip the user's permission prompt. Registered with onFailure: block,
# so this wrapper must always exit 0 itself; the body keeps its own deadline under the timeout.
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
# Installed, the body sits beside this file; in the source tree it is in ../scripts.
BODY="$HERE/gt_install_guard.py"
[ -f "$BODY" ] || BODY="$HERE/../scripts/gt_install_guard.py"
python3 "$BODY" 2>/dev/null || true
exit 0
