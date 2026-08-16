#!/usr/bin/env bash
# Core-rule Reminder mechanism — UserPromptSubmit hook.
#
# Per core_rule_priority_model.md: a Core rule is only as durable as the mechanism
# that re-asserts it. Storing a rule in a file is NOT enough — the file can stay
# unread. This re-injects the Core rules into attention on EVERY turn.
#
# Output is phrased as an IMPERATIVE, not a description. Descriptions drift;
# commands re-anchor.
#
# Canonical source: Projects/golden-thread/core-rules/
# Wire at user-global ~/.claude/settings.json for the Core tier (every project).
# Wiring per-project instead scopes it to that project = Context tier.

set -uo pipefail

STAMP="$(date '+%Y-%m-%d %H:%M %Z')"

# Keep this text short. It is paid on every turn, and a bloated reminder block
# dilutes attention on the very rules it is meant to protect.
read -r -d '' CONTEXT <<EOF || true
Current date and time: ${STAMP}

CORE RULES (always on, every turn, every project — see Projects/golden-thread/core-rules/):
1. Begin your reply with this exact timestamp: ${STAMP}
2. Write to global-memory/ only facts true in EVERY project; project-specific facts go to Projects/<slug>/memory/.
3. Do not auto-load the memory index. Load global-memory/MEMORY.md only when asked or when a /gt:* skill runs.
EOF

# jq is not guaranteed to be present; build the JSON with python3, which is a
# hard dependency of the gt plugin anyway.
python3 -c '
import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": sys.argv[1],
    }
}))' "$CONTEXT"
