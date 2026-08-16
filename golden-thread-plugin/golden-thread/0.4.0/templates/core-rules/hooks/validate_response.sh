#!/usr/bin/env bash
# Core-rule Validated mechanism — Stop hook.
#
# Per core_rule_priority_model.md, Validated is the only enforcement that does not
# depend on the model's in-the-moment discipline: it inspects the finished reply and
# blocks if a mechanically-checkable Core rule was broken.
#
# Currently validates: core_timestamp_every_message (Core/Validated).
#
# SAFETY — this hook can block replies, so it is built to FAIL OPEN. Any ambiguity,
# parse failure, missing transcript, or unexpected shape exits 0 (allow). A validator
# that blocks wrongly makes every session unusable, which is far worse than missing
# an occasional violation.
#
# Canonical source: Projects/obsidian-vault/core-rules/

set -uo pipefail

INPUT="$(cat 2>/dev/null || true)"
[ -z "$INPUT" ] && exit 0

python3 - "$INPUT" <<'PY' 2>/dev/null || exit 0
import json, re, sys

ALLOW = 0  # fail open

try:
    payload = json.loads(sys.argv[1])
except Exception:
    sys.exit(ALLOW)

# Loop guard: Claude Code sets this when the model is already continuing because a
# Stop hook blocked. Never block twice, or the session cannot terminate.
if payload.get("stop_hook_active"):
    sys.exit(ALLOW)

path = payload.get("transcript_path")
if not path:
    sys.exit(ALLOW)

try:
    with open(path, encoding="utf-8") as fh:
        entries = [json.loads(l) for l in fh if l.strip()]
except Exception:
    sys.exit(ALLOW)

if not entries:
    sys.exit(ALLOW)

def text_of(entry):
    """Concatenated text blocks of an assistant entry ('' if it is tool-only)."""
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""

# Find the start of the current turn: the last genuine user prompt. Tool results are
# recorded as type 'user' too, so skip entries whose content is tool_result blocks.
start = 0
for i in range(len(entries) - 1, -1, -1):
    e = entries[i]
    if e.get("type") != "user":
        continue
    content = (e.get("message") or {}).get("content")
    if isinstance(content, list) and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        continue  # tool result, not a real prompt
    start = i
    break

# The reply "begins" with the FIRST text the model emitted this turn — a turn may be
# text -> tool_use -> more text, and the timestamp belongs at the very start.
first = ""
for e in entries[start + 1:]:
    if e.get("type") != "assistant":
        continue
    t = text_of(e).strip()
    if t:
        first = t
        break

if not first:
    sys.exit(ALLOW)  # tool-only turn, nothing user-visible to check

# Accept a leading timestamp with common markdown wrappers: **2026-08-16 15:32 CDT**,
# `## 2026-08-16 15:32`, plain, etc. Requires date + HH:MM.
if re.match(r'^[\s*_`#>\-\[]*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}', first):
    sys.exit(ALLOW)

print(json.dumps({
    "decision": "block",
    "reason": (
        "Core rule violated (core_timestamp_every_message): begin the reply with the "
        "current wall-clock timestamp, e.g. 2026-08-16 15:32 CDT. The timestamp is "
        "injected each turn by the UserPromptSubmit hook — use that value, do not guess. "
        "Re-send the reply with the timestamp first."
    ),
}))
sys.exit(ALLOW)
PY
