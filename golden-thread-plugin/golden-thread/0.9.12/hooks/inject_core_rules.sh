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
# DEGRADATION IS ANNOUNCED, NOT SILENT.
# If the vault cannot be reached the rules cannot be loaded, and this script says so
# loudly on the very first turn. It previously emitted a bare timestamp instead, which
# looked identical to a healthy turn to anyone not counting rules — on 2026-08-17 a
# Dropbox mount dropped and two of three Core rules went unasserted for ~20 hours with
# nothing indicating it. The timestamp kept appearing, so the canary read green while
# the tier was dark.
#
# The timestamp is still emitted alongside the banner: it is the Validated rule and a
# Stop hook blocks replies that omit it, so suppressing it would turn one failure into
# two. The banner is what makes the degraded state visible.

set -uo pipefail
STAMP="$(date '+%Y-%m-%d %H:%M %Z')"
HERE="$(cd "$(dirname "$0")" && pwd)"

DEGRADED_1="** ENFORCEMENT DEGRADED — the Golden Thread vault could not be reached, so Core rules are NOT loaded."
DEGRADED_2="Only the timestamp rule is being asserted, from this hook's fallback. Treat every other Core rule as unenforced until this clears, and say so rather than implying rules are holding."
DEGRADED_3="Fix: check that ~/.claude/vault-config.json exists and its vault_path resolves, then re-run this hook to confirm rules load."

python3 "$HERE/gt_paths.py" >/dev/null 2>&1 || true   # opportunistic self-heal of a stale path

python3 - "$STAMP" "$HERE" "$DEGRADED_1" "$DEGRADED_2" "$DEGRADED_3" <<'PY' 2>/dev/null || printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"Current date and time: %s\n\n%s\n%s\n%s\n\nBegin your reply with this exact timestamp: %s"}}\n' "$STAMP" "$DEGRADED_1" "$DEGRADED_2" "$DEGRADED_3" "$STAMP"
import json, sys, os
stamp, here = sys.argv[1], sys.argv[2]
d1, d2, d3 = sys.argv[3], sys.argv[4], sys.argv[5]
sys.path.insert(0, here)

lines = [f"Current date and time: {stamp}", ""]

def degraded(reason):
    """Announce, rather than quietly emitting a timestamp that looks healthy."""
    lines.append(d1)
    lines.append(d2)
    lines.append(f"Reason: {reason}")
    lines.append(d3)
    lines.append("")
    lines.append(f"Begin your reply with this exact timestamp: {stamp}")

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
        degraded("no core rules resolved" if core else "core-rules folder not found")
except Exception as exc:
    degraded(f"{type(exc).__name__}: {exc}")

print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "\n".join(lines),
}}))
PY
