#!/usr/bin/env bash
# Core-rule Validated mechanism — Stop hook.
#
# Per core_rule_priority_model.md, Validated is the only enforcement that does not
# depend on the model's in-the-moment discipline: it inspects the finished reply and
# blocks if a mechanically-checkable Core rule was broken.
#
# Currently validates: core_timestamp_every_message and
#                      core_no_secrets_in_transcript (both Core/Validated).
#
# SAFETY — this hook can block replies, so it is built to FAIL OPEN. Any ambiguity,
# parse failure, missing transcript, or unexpected shape exits 0 (allow). A validator
# that blocks wrongly makes every session unusable, which is far worse than missing
# an occasional violation.
#
# Canonical source: core-rules/ at the vault root (Projects/golden-thread/core-rules
# before 0.17.0; resolved via gt_paths, never spelled)

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

INPUT="$(cat 2>/dev/null || true)"
[ -z "$INPUT" ] && exit 0

python3 - "$INPUT" "$HERE" <<'PY' 2>/dev/null || exit 0
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

# 0.22.0: the turn number (prompts the session has had so far) and a recorder for the rule-adherence
# record. A block appends one local line -- rule, session, turn -- to state/rule-violations.jsonl through
# gt_shadow; never the reply or what matched. Best effort: it can change no verdict.
def _is_prompt(e):
    if e.get("type") != "user":
        return False
    c = (e.get("message") or {}).get("content")
    return not (isinstance(c, list) and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in c))

turn_number = sum(1 for e in entries[:start + 1] if _is_prompt(e))

def record_violation(rule):
    try:
        sys.path.insert(0, sys.argv[2])
        import gt_shadow
        gt_shadow.record_violation(rule, str(payload.get("session_id") or ""), turn_number)
    except Exception:
        pass

# A turn may be text -> tool_use -> more text. `first` is the first text the model emitted
# this turn; `last` is its final answer, the part the user reads (0.20.2).
first, first_at, last = "", len(entries), ""
for j in range(start + 1, len(entries)):
    e = entries[j]
    if e.get("type") != "assistant":
        continue
    t = text_of(e).strip()
    if t:
        if not first:
            first, first_at = t, j
        last = t

# Was this turn GIVEN the time? (0.19.1) Claude Code records inject_core_rules.sh's output as a
# UserPromptSubmit `hook_additional_context` attachment beside the prompt. install.sh wires this
# Stop hook partway through the session that runs it, so the prompt of that turn went out
# before the reminder was wired: the reply had no time to show and was blocked for a rule it
# was never handed. The search runs from the end of the previous turn to the reply's first text.
prev_end = max([i for i in range(start) if entries[i].get("type") == "assistant"] or [-1])
given_time = any(
    e.get("type") == "attachment"
    and (e.get("attachment") or {}).get("type") == "hook_additional_context"
    and (e.get("attachment") or {}).get("hookEvent") == "UserPromptSubmit"
    and "Current date and time" in json.dumps((e.get("attachment") or {}).get("content"))
    for e in entries[prev_end + 1:first_at])

if not first:
    sys.exit(ALLOW)  # tool-only turn, nothing user-visible to check

# ── Core rule 2: core_no_secrets_in_transcript (Core/Validated) ──────────────
# Scans ALL assistant text in the turn, not just the first block: a secret pasted
# mid-reply is the same disclosure as one pasted at the top.
#
# FALSE POSITIVES ARE THE REAL RISK HERE. A validator that trips on the word
# "password" would make every security conversation impossible, so this matches
# only high-confidence shapes: known credential prefixes with enough trailing
# entropy, PEM private-key headers, and literal values after a credential keyword.
# Anything that looks like a variable reference, a placeholder or a redaction is
# explicitly allowed — talking ABOUT credentials must stay possible.

# The shapes and the placeholder rule live in scripts/gt_secret_shapes.py (0.22.0), shared with the
# scanner pack's tests; this hook is a fail-open validator, so a missing module skips this one check
# (the timestamp rule still runs) rather than blocking the reply. Do not copy the patterns back in here: two lists drifted once already.
try:
    sys.path.insert(0, sys.argv[2])
    from gt_secret_shapes import secret_findings
except Exception:
    def secret_findings(text):
        return []

all_text = " ".join(text_of(e) for e in entries[start + 1:] if e.get("type") == "assistant")
leaks = secret_findings(all_text)
if leaks:
    # 0.22.0 (savings recorders): ONE count-only line per blocked turn in the savings ledger, so the
    # core-rules-wider row stops reading "design". The clean-up a leak causes is not benchmarked, so no
    # saving is claimed (0 against 0) and the note says so. Never the value, never an exception.
    try:
        import hashlib
        sys.path.insert(0, sys.argv[2])
        import gt_ledger
        sid = str(payload.get("session_id") or "")[:8]
        gt_ledger.record_event(
            "core-rules-wider", 0, 0, "estimated",
            note="a reply holding a credential was blocked (%s); the clean-up it avoids is not benchmarked, "
                 "so no saving is claimed" % ", ".join(leaks)[:80],
            key="core-rules-wider:%s" % hashlib.sha256(
                ("%s|%d|%s" % (sid, start, ",".join(leaks))).encode()).hexdigest()[:16],
            session=sid)
    except Exception:
        pass
    record_violation("core_no_secrets_in_transcript")
    print(json.dumps({
        "decision": "block",
        "reason": (
            "Core rule violated (core_no_secrets_in_transcript): the reply contains "
            + ", ".join(leaks) + ". Never paste a secret into the session, including to "
            "inspect or redact it — read the file directly, or report only its shape "
            "(length, prefix, which key it is). Re-send without the value. If this is a "
            "false positive on a placeholder, use an obviously non-real form such as "
            "<client-secret>."
        ),
    }))
    sys.exit(ALLOW)

if not given_time:
    sys.exit(ALLOW)   # never handed a time this turn: nothing to hold the reply to

# rules_set lite (0.22.0, phase 117): the timestamp rule is not in the set, so its check does not run.
# The secret check above is in the set and has already run. Trouble reading the setting runs the check.
try:
    import gt_settings
    _lite = gt_settings.get("rules_set") == "lite"
except Exception:
    _lite = False
if _lite:
    sys.exit(ALLOW)

# Accept a leading timestamp with common markdown wrappers: **2026-08-16 15:32 CDT**,
# `## 2026-08-16 15:32`, plain, etc. Requires date + HH:MM.
#
# 0.20.2: the FINAL answer satisfies the rule on its own. Until then only the first text
# counted, so a one-line lead-in before a tool call ("Checking X:") blocked a turn whose
# answer began with the stamp -- the most common block in practice, and it read as wrong
# to the owner because the reply they saw was stamped. A stamped first text still passes,
# so nothing that passed before is newly blocked. Text in between counts either way.
STAMP = re.compile(r'^[\s*_`#>\-\[]*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}')
if STAMP.match(last) or STAMP.match(first):
    sys.exit(ALLOW)

record_violation("core_timestamp_every_message")
print(json.dumps({
    "decision": "block",
    "reason": (
        "Core rule violated (core_timestamp_every_message): neither the final answer nor "
        "the first text of this turn begins with the timestamp. "
        "The timestamp is the first text you write this turn, including any line before your first tool call. "
        "Start the final answer "
        "with the value injected by the UserPromptSubmit hook, e.g. "
        "'2026-08-16 15:32 CDT'; do not guess it."
    ),
}))
sys.exit(ALLOW)
PY
