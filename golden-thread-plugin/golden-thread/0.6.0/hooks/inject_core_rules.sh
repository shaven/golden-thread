#!/usr/bin/env bash
# Core-rule Reminder mechanism — UserPromptSubmit hook.
#
# LOCATION: installed to ~/.claude/golden-thread/hooks/ — deliberately OUTSIDE the
# vault. settings.json references it by absolute path, so that path must never move.
# Projects get renamed and merged; the vault itself can move. Neither may break
# enforcement.
#
# The RULES are vault content and are read at run time from core-rules/*.md, located
# via gt_paths.py (config, then search). So editing a rule changes what is injected —
# no second copy of the rule text lives in this script.
#
# Degrades rather than fails: if the vault cannot be found, still emit the timestamp,
# because the timestamp rule is the one that is Validated and must never silently stop.

set -uo pipefail
STAMP="$(date '+%Y-%m-%d %H:%M %Z')"
HERE="$(cd "$(dirname "$0")" && pwd)"

python3 "$HERE/gt_paths.py" >/dev/null 2>&1 || true   # opportunistic self-heal of a stale path

python3 - "$STAMP" "$HERE" <<'PY' 2>/dev/null || printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"Current date and time: %s\nBegin your reply with this exact timestamp: %s"}}\n' "$STAMP" "$STAMP"
import json, sys, os
stamp, here = sys.argv[1], sys.argv[2]
sys.path.insert(0, here)

lines = [f"Current date and time: {stamp}", ""]
try:
    from gt_paths import find_core_rules, core_rule_files, parse_rule, find_vault, MODEL_FILE
    core = find_core_rules()
    picked = []
    for p in core_rule_files(core):
        # The priority-model file describes the system; it is not a per-turn rule.
        # A rule can also opt out with `inject: false`.
        if p.name == MODEL_FILE:
            continue
        r = parse_rule(p)
        if r.get("level") != "core" or not r.get("imperative"):
            continue
        if str(r.get("inject", "")).lower() == "false":
            continue
        picked.append(r)
    # Validated rules first: they are the ones with a hard backstop, and putting them
    # at the top keeps the most consequential rule in the most salient position.
    picked.sort(key=lambda r: (r.get("enforcement") != "validated", r["name"]))
    rules = [r["imperative"] for r in picked]
    vault = find_vault()
    where = str(core.relative_to(vault)) if (core and vault and vault in core.parents) else "core-rules/"
    if rules:
        lines.append(f"CORE RULES (always on, every turn, every project — see {where}):")
        for i, r in enumerate(rules, 1):
            lines.append(f"{i}. {r}")
    else:
        lines.append(f"Begin your reply with this exact timestamp: {stamp}")
except Exception:
    lines.append(f"Begin your reply with this exact timestamp: {stamp}")

print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "\n".join(lines),
}}))
PY
