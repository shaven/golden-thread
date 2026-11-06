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
. "$HERE/gt_python.sh" 2>/dev/null || true   # Windows: the real interpreter, not the Store stub
# paranoid.signed_manifest (0.21.0): with a sealed record, check this hook's files before it runs. With
# the group off this is one test(1) and nothing else. Exit 3 = the check printed a refusal (a deny
# for a guard, a systemMessage otherwise) and this hook must not run.
# 0.22.0 (review): a refusal here used to print only the gate's user-facing message and exit, so the
# model got no time, no rules and no hint that enforcement was gone -- a turn that read as healthy. The
# refusal is held and turned into a banner for the model below, once the event name is known.
GATE_OUT=""; GATE_FAIL=0
if [ -f "${HOME:-${USERPROFILE:-/nonexistent}}/.claude/golden-thread/integrity.json" ] || [ -f "${HOME:-${USERPROFILE:-/nonexistent}}/.claude/golden-thread/paranoid-sealed.json" ]; then
  GATE_OUT="$(python3 "$HERE/gt_integrity_gate.py" check reporter "$HERE/$(basename "$0")" 2>/dev/null)"
  [ "$?" -eq 3 ] && GATE_FAIL=1
fi

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

if [ "$GATE_FAIL" -eq 1 ]; then
  python3 - "$EVENT" "$STAMP" "$GATE_OUT" <<'PY' 2>/dev/null || printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":"Current date and time: %s\\n\\n** ENFORCEMENT DEGRADED -- gt installed files do not match the integrity record, so the Core rules are NOT loaded.\\nTreat every Core rule as unenforced until this clears, and say so rather than implying rules are holding.\\n\\nBegin your reply with this exact timestamp: %s"}}\n' "$EVENT" "$STAMP" "$STAMP"
import json, sys
event, stamp, gate = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    note = json.loads(gate).get("systemMessage", "")
except Exception:
    note = ""
ctx = ("Current date and time: %s\n\n** ENFORCEMENT DEGRADED -- gt's installed files do not match the "
       "integrity record (gt paranoid.signed_manifest), so the Core rules are NOT loaded.\n"
       "Treat every Core rule as unenforced until this clears, and say so rather than implying rules "
       "are holding.\nFix: python3 ~/.claude/golden-thread/hooks/gt_integrity.py verify (what changed).\n\n"
       "Begin your reply with this exact timestamp: %s" % (stamp, stamp))
out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": ctx}}
if note:
    out["systemMessage"] = note
print(json.dumps(out))
PY
  exit 0
fi

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
# No bytecode in the hooks dir (0.20.1): the imports below would leave __pycache__ beside the
# installed copies, which the component check and tests/test_no_bytecode_in_src forbid. Seen
# on Windows, where nothing else in the hook's environment turned it off.
sys.dont_write_bytecode = True
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
    # rules_set lite (0.22.0): only the four rules gt_paths.LITE_RULES names. Any trouble reading the
    # setting sends the full set: the safe direction.
    try:
        from gt_paths import LITE_RULES
        if gt_settings and gt_settings.get("rules_set") == "lite":
            picked = [r for r in picked if r["name"] in LITE_RULES]
    except Exception:
        pass
    # Validated rules first: they are the ones with a hard backstop, and putting them
    # at the top keeps the most consequential rule in the most salient position.
    picked.sort(key=lambda r: (r.get("enforcement") != "validated", r["name"]))
    rules = [r["imperative"] for r in picked]
    vault = find_vault()
    where = core.relative_to(vault).as_posix() if (core and vault and vault in core.parents) else "core-rules/"
    if rules:
        # Each rule's rendered lines, built once: the full text lists them all, and the compact
        # form (rules_injection, 0.21.4) hashes them to tell whether anything changed.
        blocks = []
        for i, r in enumerate(picked, 1):
            rl = [f"{i}. {r['imperative']}"]
            # A ceiling the model cannot see is a ceiling it cannot honour, so a rule
            # naming `budget_from` carries that setting's current value on its own line,
            # directly under the rule it applies to.
            b = r.get("budget_from")
            v = gt_settings.get(b) if (b and gt_settings) else None
            if v:
                shown = "as many as the machine allows" if v == "auto" else f"at most {v}"
                rl.append(f"   ({b}: {shown})")
            blocks.append((r["name"], rl))
        # rules_injection (0.21.4): `full` (the default) sends every rule every prompt. `compact` sends
        # the full text on the first prompt of a session, after a compaction and every 20 prompts, and
        # one line otherwise. Enforcement (the Stop and PreToolUse validators) does not depend on this
        # (ADR-9). ANY failure in the compact path sends the full text: the safe direction.
        form = None
        auto_note = ""
        try:
            mode = (os.environ.get("GT_RULES_INJECTION") or (gt_settings.get("rules_injection") if gt_settings else "") or "full")
            auto_note = ""
            if mode == "auto":
                # rules_injection auto (0.22.0): the compact form while the session is small, the full
                # text every prompt once its context passes rules_injection.full_after_tokens. No reading
                # of the context means the full form.
                import gt_rules_once
                try:
                    pdict0 = json.loads(payload)
                except (ValueError, TypeError):
                    pdict0 = {}
                ctxn = gt_rules_once.context_tokens((pdict0 or {}).get("transcript_path")) if isinstance(pdict0, dict) else None
                try:
                    limit = int(gt_settings.get("rules_injection.full_after_tokens") or 60000) if gt_settings else 60000
                except (TypeError, ValueError):
                    limit = 60000
                if ctxn is not None and ctxn < limit:
                    mode = "compact"
                else:
                    mode = "full"
                    auto_note = ("rules_injection: auto -> full (context %s tokens, over %d)" % (ctxn, limit)
                                 if ctxn is not None else "rules_injection: auto -> full (no context reading)")
            if mode == "compact":
                import gt_rules_once
                try:
                    pdict = json.loads(payload)
                except (ValueError, TypeError):
                    pdict = {}
                if isinstance(pdict, dict):
                    form = gt_rules_once.decide(pdict, event, blocks)
        except Exception:
            form = None
        if form and form.get("form") == "compact":
            lines.extend(form["lines"])
        else:
            # In compact mode the full text's header carries the rules' hash: it is how the NEXT prompt
            # can tell, from the transcript, that the copy above is the current one. `full` mode (the
            # default) keeps the header it has always had, byte for byte.
            tag = f"; hash {form['hash']}" if form else ""
            lines.append(f"CORE RULES (always on, every turn, every project — see {where}{tag}):")
            for _name, rl in blocks:
                lines.extend(rl)
            if auto_note:
                lines.append(auto_note)
    else:
        degraded("no core rules resolved" if core else "core-rules folder not found")
except Exception as exc:
    degraded(f"{type(exc).__name__}: {exc}")

# Per-turn settings lines (0.21.0): reply_length, the allowance note, next-step choices. Each is one
# line, silent when its setting is off or its data is missing; a failure here drops only these.
try:
    import gt_turn_notes
    notes = gt_turn_notes.lines()
    if notes:
        lines.append("")
        lines.extend(notes)
except Exception:
    pass

print(json.dumps({"hookSpecificOutput": {
    "hookEventName": event,
    "additionalContext": "\n".join(lines),
}}))
PY
