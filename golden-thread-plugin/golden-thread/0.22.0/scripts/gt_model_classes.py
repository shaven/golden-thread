#!/usr/bin/env python3
"""gt_model_classes.py -- class routing for a spawn that names a model (0.22.0, phase 82 part 4).

The table is the registry slot `model_class`, shipped as packs/core/model.classes.pack.json (the owner's own
pack in the vault wins, as for every slot). One row per class of work:

  class     a short name (security, sweeps, ...); the advice pack's rows use the same names
  keywords  lowercase words or phrases; one matches a word that STARTS with it
  tier      the intent the class needs: fast, balanced or deep, mapped to a model by the model.intents pack
  verified  YYYY-MM-DD

This table says what a class needs in principle; the advice table (gt_model_advice) says what was measured, and
a class only ever lowers a named model when an advice row for it passes that table's evidence gate (30 judged
runs, Opus needed in at most 10 percent). Override or extend it with your own pack in the vault. The pack's
`notes` field is capped at 300 characters (dev/submissions.py), so this docstring is the full description and
the notes point here.

A spawn is classified from its `description` and `prompt`, read in memory: only the class name is ever
returned, logged or shown. A deep class beats any cheaper match; otherwise the most keyword hits win, ties by
class name. The setting `explicit_model_policy` (gt_agent_model.hook) decides what the class does:

  warn       a deep-class spawn that names a cheaper model gets one line saying to use Opus
  downgrade  a spawn that names Opus for a class the table puts lower is changed to the cheaper tier, but ONLY
             with evidence: an advice row (gt_model_advice) for the class that speaks for the spawn type and
             passes its gate. A deep class is never downgraded; neither is a model the user typed in the prompt.

Every function fails open: any error means no class, no note, no change.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

SLOT = "model_class"
TIERS = ("fast", "balanced", "deep")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# model alias -> depth, for "is this cheaper than that"
RANK = {"haiku": 0, "sonnet": 1, "opus": 2}
ALIAS = {0: "haiku", 1: "sonnet", 2: "opus"}
TIER_ALIAS = {"fast": "haiku", "balanced": "sonnet", "deep": "opus"}   # fallback if the intent pack is unreadable


def row_problem(r):
    """-> why this row may not route a spawn, else None."""
    if not isinstance(r, dict):
        return "row is not an object"
    if not (isinstance(r.get("class"), str) and r["class"].strip()):
        return "no class"
    kw = r.get("keywords")
    if not (isinstance(kw, list) and kw and all(isinstance(k, str) and k.strip() for k in kw)):
        return "keywords must be a non-empty list of words"
    if r.get("tier") not in TIERS:
        return "tier must be one of %s" % "|".join(TIERS)
    if not (isinstance(r.get("verified"), str) and DATE.match(r["verified"])):
        return "verified must be YYYY-MM-DD"
    return None


def usable(rows):
    """-> (rows that pass, [(class, reason)] for those that do not)."""
    good, problems = [], []
    for r in rows:
        bad = row_problem(r)
        if bad:
            problems.append((r.get("class") if isinstance(r, dict) else "?", bad))
        else:
            good.append(r)
    return good, problems


def load_rows(vault=None):
    """-> (usable rows, problems) from the registry slot."""
    import gt_registry
    effective, _shadowed, _retracted, reg_problems = gt_registry.resolve(SLOT, vault=vault)
    rows, bad = usable([rec["entry"] for rec in effective])
    return rows, list(reg_problems) + bad


def _text(tool_input):
    return " ".join(str(tool_input.get(k) or "") for k in ("description", "prompt")).lower()


def _hits(text, keywords):
    n = 0
    for k in keywords:
        if re.search(r"(?<![a-z0-9])" + re.escape(k.strip().lower()), text):
            n += 1
    return n


def classify(tool_input, rows):
    """-> the matching table row (a dict; use only its class and tier), or None. Deep classes win."""
    try:
        if not isinstance(tool_input, dict):
            return None
        text = _text(tool_input)
        best = None
        for r in rows:
            if row_problem(r) is not None:
                continue
            h = _hits(text, r["keywords"])
            if not h:
                continue
            key = (r["tier"] == "deep", h, r["class"])
            if best is None or key > best[0]:
                best = (key, r)
        return best[1] if best else None
    except Exception:                                         # noqa: BLE001 - fail open
        return None


def alias_of(model):
    """-> opus, sonnet or haiku for whatever a spawn named, else None."""
    m = str(model or "").lower()
    return next((a for a in RANK if a in m), None)


def tier_alias(tier):
    """-> the model alias the intent pack gives this tier."""
    try:
        import gt_model_policy as P
        a = P.intent_settings(tier, None)[0]
        if a in RANK:
            return a
    except Exception:                                         # noqa: BLE001
        pass
    return TIER_ALIAS.get(tier)


def escalate_note(tool_input, effective_model, rows):
    """-> one line when a deep-class spawn runs on a cheaper model than Opus, else None. Suggest only."""
    try:
        c = classify(tool_input, rows)
        a = alias_of(effective_model)
        if not c or c["tier"] != "deep" or a is None or RANK[a] >= RANK["opus"]:
            return None
        return ("gt: this spawn looks like %s work, which gets Opus here; it runs on %s. "
                "Suggestion only, the model is yours." % (c["class"], a))
    except Exception:                                         # noqa: BLE001
        return None


def downgrade(tool_input, class_rows, advice_rows):
    """-> (new alias, one line) for a spawn that names Opus where the table and the evidence say a cheaper tier
    suffices, else None. Never for a deep class, never upward, never for a model typed in the prompt."""
    try:
        if not isinstance(tool_input, dict):
            return None
        named = alias_of(tool_input.get("model"))
        if named != "opus":
            return None
        c = classify(tool_input, class_rows)
        if not c or c["tier"] == "deep":
            return None
        if re.search(r"(?<![a-z0-9])opus(?![a-z0-9])", _text(tool_input)):
            return None                                       # the user asked for it in words
        import gt_model_advice as A
        t = tool_input.get("subagent_type") or "general-purpose"
        ev = next((r for r in advice_rows if A.row_problem(r) is None and r["class"] == c["class"]
                   and t in r["agent_types"]), None)
        if not ev:
            return None
        table = tier_alias(c["tier"])
        if table not in RANK or ev["recommend"] not in RANK:
            return None
        pick = ALIAS[max(RANK[table], RANK[ev["recommend"]])]  # the deeper of table and evidence: the safer
        if RANK[pick] >= RANK["opus"]:
            return None
        return pick, ("gt: %s work (%s) moved from opus to %s: Opus needed in %d%% of %d judged runs "
                      "(verified %s); explicit_model_policy downgrade."
                      % (c["class"], t, pick, ev["opus_needed_pct"], ev["samples"], ev["verified"]))
    except Exception:                                         # noqa: BLE001 - fail open
        return None
