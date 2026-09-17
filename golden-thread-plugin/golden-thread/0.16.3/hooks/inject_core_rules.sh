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

# WHICH EVENT AM I? Since 0.16.2 this script is wired to TWO events, and the emitted
# `hookEventName` has to name the one that actually fired.
#
#   UserPromptSubmit    every turn -- the carrier of the Core rules
#   SessionStart        matcher "compact" -- because compaction does NOT preserve what a
#                       hook added. Project-root CLAUDE.md is re-injected from disk; hook
#                       context is "summarized with the rest of the conversation", so after
#                       a compaction the rules survive only as whatever the summariser kept.
#                       The gap is the remainder of that turn -- UserPromptSubmit restores
#                       them verbatim at the next prompt -- but a rule present in degraded
#                       form while everything reads healthy is the 2026-08-17 shape again.
#
# The payload is read HERE, before the heredoc below takes stdin: `python3 - <<PY` feeds the
# SCRIPT on stdin, so a payload not captured first is gone. The `-t 0` guard is so running
# this by hand at a terminal -- as the vault's CLAUDE.md instructs, to confirm enforcement is
# live -- does not hang waiting on a pipe that will never arrive.
PAYLOAD=""
[ -t 0 ] || PAYLOAD="$(cat)"

# The Python below parses this properly. This crude match exists only for the printf fallback,
# which runs when Python is unavailable -- at which point naming the wrong event would be a
# second failure stacked on the first.
EVENT="UserPromptSubmit"
case "$PAYLOAD" in
    *'"hook_event_name"'*'"SessionStart"'*) EVENT="SessionStart" ;;
esac

DEGRADED_1="** ENFORCEMENT DEGRADED — the Golden Thread vault could not be reached, so Core rules are NOT loaded."
DEGRADED_2="Only the timestamp rule is being asserted, from this hook's fallback. Treat every other Core rule as unenforced until this clears, and say so rather than implying rules are holding."
DEGRADED_3="Fix: check that ~/.claude/vault-config.json exists and its vault_path resolves, then re-run this hook to confirm rules load."

python3 "$HERE/gt_paths.py" >/dev/null 2>&1 || true   # opportunistic self-heal of a stale path

# The printf fallback below writes \\n so the JSON string carries escaped newlines;
# a bare \n there becomes a raw newline and the output is no longer valid JSON.
python3 - "$STAMP" "$HERE" "$DEGRADED_1" "$DEGRADED_2" "$DEGRADED_3" "$PAYLOAD" <<'PY' 2>/dev/null || printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":"Current date and time: %s\\n\\n%s\\n%s\\n%s\\n\\nBegin your reply with this exact timestamp: %s"}}\n' "$EVENT" "$STAMP" "$DEGRADED_1" "$DEGRADED_2" "$DEGRADED_3" "$STAMP"
import json, sys, os
stamp, here = sys.argv[1], sys.argv[2]
d1, d2, d3 = sys.argv[3], sys.argv[4], sys.argv[5]
payload = sys.argv[6] if len(sys.argv) > 6 else ""
sys.path.insert(0, here)

# A malformed or absent payload means UserPromptSubmit, the event this script has always
# served. Guessing SessionStart on bad input would suppress the per-turn injection, which
# is the one outcome worse than re-injecting once too often.
try:
    event = json.loads(payload).get("hook_event_name") or "UserPromptSubmit"
except (ValueError, AttributeError):
    event = "UserPromptSubmit"
if not isinstance(event, str) or not event.isalnum():
    event = "UserPromptSubmit"

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
    try:
        import gt_settings
    except Exception:
        gt_settings = None   # a rule with no reachable settings module is injected as written
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
        # A rule may name the setting that governs it. Switched off, the rule is not
        # injected at all: continuing to assert a behaviour the user disabled is how a
        # settings registry becomes decoration, and it would leave the model following
        # a rule the tools no longer honour.
        gate = r.get("gated_by")
        if gate and gt_settings and gt_settings.get(gate) == "off":
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
        for i, r in enumerate(picked, 1):
            lines.append(f"{i}. {r['imperative']}")
            # A ceiling the model cannot see is a ceiling it cannot honour, so a rule
            # naming `budget_from` carries that setting's current value on its own line,
            # directly under the rule it applies to.
            b = r.get("budget_from")
            v = gt_settings.get(b) if (b and gt_settings) else None
            if v:
                shown = "as many as the machine allows" if v == "auto" else f"at most {v}"
                lines.append(f"   ({b}: {shown})")
    else:
        degraded("no core rules resolved" if core else "core-rules folder not found")
except Exception as exc:
    degraded(f"{type(exc).__name__}: {exc}")

print(json.dumps({"hookSpecificOutput": {
    "hookEventName": event,
    "additionalContext": "\n".join(lines),
}}))
PY
