#!/usr/bin/env bash
# Protected paths — PreToolUse hook (0.12.9). Governed by the `protected_paths` setting.
#
# LOCATION: ~/.claude/golden-thread/hooks/ — outside the vault, like its siblings.
# settings.json references it by absolute path, which must survive a project rename or
# a vault move. Registered by install.sh, not install-core-rules: it is not tied to a
# Core rule, and the ~/.claude half of it needs no vault at all.
#
# WHAT: before a Write/Edit/MultiEdit/NotebookEdit, force the permission prompt ("ask")
# when the target is the vault's core-rules/ or global-memory/, ~/.claude/golden-thread/,
# or ~/.claude/settings.json; refuse ("deny") editing or overwriting an EXISTING file
# under the vault's Sources/. Verified 2026-09-13: until this hook nothing but skill prose
# kept a session from rewriting the files that load into every session of every project,
# or the hooks and settings that enforce everything else.
#
# STDIN MATTERS: the payload arrives on stdin, so the python must be a real file, not
# a heredoc — a heredoc consumes stdin as the script and the guard fails open on every
# call. This cost the session-claim guard a day in 2026-08-28; do not reintroduce it.
#
# FAIL OPEN IN THE GUARD'S OWN CODE: an error in guard_protected_paths.py allows the write
# (this wrapper ends `|| true; exit 0`). This guard sits in front of every file-editing tool
# call in every session; a wrong deny is far more expensive than a missed one. What does stop
# the action is the wrapper itself failing or running past its timeout in hooks.json: it is
# registered with onFailure: block, so that is a block, not an allow.
#
# SILENT, NOT "allow": failing open means printing NOTHING and exiting 0. Claude Code's
# hooks reference: permissionDecision "allow" skips the user's permission prompt; exit 0
# with no output is no decision and the normal permission flow applies.
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
python3 "$HERE/guard_protected_paths.py" "$HERE" 2>/dev/null || true
exit 0
