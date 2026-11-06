#!/usr/bin/env bash
# Knowledge read log — PostToolUse hook on Read (0.18.1). Governed by the `knowledge_access_log`
# setting.
#
# LOCATION: ~/.claude/golden-thread/hooks/ — outside the vault, like its siblings. Registered by
# install.sh (gt_components.HOOK_REGISTRATIONS, matcher "Read").
#
# WHAT: after a Read of a page under the vault's Knowledge/, append one JSON line to
# <vault>/usage/knowledge.jsonl, so `gt_optimize.py` can tell a page that is read every week
# from one written once and never consulted (finding `knowledge-unused`). Git history shows
# when a page was WRITTEN; nothing showed whether it was ever READ.
#
# WHY A HOOK, NOT SKILL PROSE: a skill telling the model to log its reads is a log of the reads
# the model remembered to log. The harness runs this on every Read, whichever skill (or none)
# caused it.
#
# STDIN MATTERS: the payload arrives on stdin, so the python must be a real file, not a heredoc.
#
# FAIL OPEN, SILENT, ALWAYS: a failed log line must never fail or slow the Read it follows. Any
# error prints nothing and exits 0.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/gt_python.sh" 2>/dev/null || true   # Windows: the real interpreter, not the Store stub
# paranoid.signed_manifest (0.21.0): with a sealed record, check this hook's files before it runs. With
# the group off this is one test(1) and nothing else. Exit 3 = the check printed a refusal (a deny
# for a guard, a systemMessage otherwise) and this hook must not run.
if [ -f "${HOME:-${USERPROFILE:-/nonexistent}}/.claude/golden-thread/integrity.json" ] || [ -f "${HOME:-${USERPROFILE:-/nonexistent}}/.claude/golden-thread/paranoid-sealed.json" ]; then
  python3 "$HERE/gt_integrity_gate.py" check reporter "$HERE/$(basename "$0")" 2>/dev/null
  [ "$?" -eq 3 ] && exit 0
fi
python3 "$HERE/log_knowledge_read.py" 2>/dev/null || true
exit 0
