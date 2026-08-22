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
# Canonical source: Projects/golden-thread/core-rules/

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

PLACEHOLDER = re.compile(
    r'^[\s"\'\`]*(?:'
    r'[$<{]|'                                  # $VAR  ${VAR}  <placeholder>  {{X}}
    r'(?:x{3,}|X{3,}|\.{3,}|\*{3,}|_{3,})|'     # xxxx  XXXX  ....  ****  ____
    r'(?:REDACTED|redacted|CHANGE-?ME|change-?me|example|EXAMPLE|placeholder|'
    r'your-|my-|test|TEST|dummy|fake|sample|none|null|N/A)'
    r')', re.I)

HIGH_CONFIDENCE = [
    ("Google OAuth client secret", re.compile(r'GOCSPX-[A-Za-z0-9_-]{20,}')),
    ("Google access token",        re.compile(r'\bya29\.[A-Za-z0-9_-]{30,}')),
    ("Google refresh token",       re.compile(r'\b1//0[A-Za-z0-9_-]{30,}')),
    ("AWS access key id",          re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ("GitHub token",               re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}')),
    ("Slack token",                re.compile(r'\bxox[abprs]-[A-Za-z0-9-]{12,}')),
    ("private key block",          re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
]

# keyword = literalvalue   /   -u user:literalpassword
KEYWORD_LITERAL = re.compile(
    r'(?:pass(?:word|wd)?|pwd|secret|api[_-]?key|token)\s*[=:]\s*(["\']?)([^\s"\'&<>]{6,})\1',
    re.I)
BASIC_AUTH = re.compile(r'-u\s+[A-Za-z0-9_.-]{2,}:([^\s"\']{4,})')

def secret_findings(text):
    hits = []
    for name, rx in HIGH_CONFIDENCE:
        if rx.search(text):
            hits.append(name)
    for rx, label in ((KEYWORD_LITERAL, "credential assigned a literal value"),
                      (BASIC_AUTH, "basic-auth pair with a literal password")):
        for m in rx.finditer(text):
            val = m.group(m.lastindex or 1)
            if val and not PLACEHOLDER.match(val):
                hits.append(label)
                break
    return sorted(set(hits))

all_text = " ".join(text_of(e) for e in entries[start + 1:] if e.get("type") == "assistant")
leaks = secret_findings(all_text)
if leaks:
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
