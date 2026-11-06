#!/usr/bin/env python3
"""gt_model_advice.py -- the decision table behind the spawn-time note (0.22.0, phase 104, item N41).

The table is the registry slot `model_advice`, shipped as packs/core/model.advice.pack.json (the owner's own
pack in the vault wins, as for every slot). One row per class of work:

  class             a short name for the work (sweeps, rubric-judging, ...)
  agent_types       the spawn types the row speaks for (subagent_type values); a spawn is matched by its
                    TYPE only, never by its prompt
  recommend         the cheaper tier the evidence supports: sonnet or haiku
  evidence          one line saying where the numbers came from (`gt_model_calibrate.py opus-needed rates`)
  samples           judged samples behind the rate (an integer, at least MIN_SAMPLES)
  opus_needed_pct   the measured share of those samples where Opus was needed (an integer percent, at most
                    MAX_OPUS_NEEDED_PCT)
  verified          YYYY-MM-DD, when someone last checked the row

EXAMPLE ROWS (owner ruling 2026-10-27: "we won't know exactly what is needed for the table, so make it look
like you think it could and ship it"). A row with `state: example` is an illustration, not evidence: it needs
class, agent_types, recommend, evidence (the reason, no figures) and verified, and it must NOT carry samples or
opus_needed_pct (an example with numbers would look like a measurement). It never drives a note or a downgrade
(row_problem refuses it, usable() leaves it out); `gt_settings.py models advise` shows it under the banner
EXAMPLE_BANNER. A row of the same class in the owner's own pack replaces it, as for every slot. Rows without a
`state` (or `state: measured`) follow the rules below unchanged.

A row missing any of these, or under the thresholds, is DROPPED with a problem: a downgrade row without a
measured rate is a guess (phase 104 plan), so the shipped pack has none and the note below never fires until
a measured row is added (in the owner's own vault pack, or in the shipped pack once measured). The note is for
a spawn that NAMES a deeper model (opus) than the row recommends, only with setting explicit_model_policy warn;
it is one line, SUGGEST ONLY, never changes the model, and fails open: any error means no note.
The pack's `notes` field is capped at 300 characters (dev/submissions.py), so this docstring is the full
description of the row format and the notes point here.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

SLOT = "model_advice"
MIN_SAMPLES = 30
MAX_OPUS_NEEDED_PCT = 10
TIERS = ("sonnet", "haiku")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STATE_EXAMPLE = "example"
STATE_MEASURED = "measured"
EXAMPLE_BANNER = "EXAMPLE rows, edit for your own work; not measured."
EXAMPLE_TIERS = TIERS + ("opus",)                        # an example row may also say WHEN the deepest model is worth it
RULE_MAX = 300
# The deep-verifier policy (owner 2026-10-27): an example row may carry it, all eight fields or none. The headline is
# printed by `models advise`; each field is one statement of at most RULE_MAX characters.
POLICY_HEAD = "Use a deep-tier (Opus) verifier on Sonnet-built work ONLY when ALL four hold:"
POLICY_FIELDS = ("when_high_stakes", "when_new_logic", "when_tests_pass", "when_no_gate",
                 "limit_cluster", "limit_fixer", "limit_differ", "policy_evidence")


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def is_example(r):
    return isinstance(r, dict) and r.get("state") == STATE_EXAMPLE


def example_problem(r):
    """-> why this row is not a valid EXAMPLE row, else None. No figures: an example must not look like evidence."""
    if not isinstance(r, dict):
        return "row is not an object"
    if r.get("state") != STATE_EXAMPLE:
        return "state must be example"
    if not (isinstance(r.get("class"), str) and r["class"].strip()):
        return "no class"
    at = r.get("agent_types")
    if not (isinstance(at, list) and at and all(isinstance(x, str) and x for x in at)):
        return "agent_types must be a non-empty list of spawn types"
    if r.get("recommend") not in EXAMPLE_TIERS:
        return "recommend must be one of %s" % "|".join(EXAMPLE_TIERS)
    if not (isinstance(r.get("evidence"), str) and r["evidence"].strip()):
        return "no reason text"
    if "rule" in r and not (isinstance(r["rule"], str) and r["rule"].strip() and len(r["rule"]) <= RULE_MAX):
        return "rule must be a short sentence (at most %d characters)" % RULE_MAX
    if not (isinstance(r.get("verified"), str) and DATE.match(r["verified"])):
        return "verified must be YYYY-MM-DD"
    for k in ("samples", "opus_needed_pct"):
        if k in r:
            return "an example row must not carry %s: it would look like a measurement" % k
    have = [f for f in POLICY_FIELDS if f in r]
    if have and len(have) != len(POLICY_FIELDS):
        return "the verifier policy is all eight fields or none (missing %s)" % ", ".join(
            f for f in POLICY_FIELDS if f not in r)
    for f in have:
        if not (isinstance(r[f], str) and r[f].strip() and len(r[f]) <= RULE_MAX):
            return "%s must be a short statement (at most %d characters)" % (f, RULE_MAX)
    return None


def row_problem(r):
    """-> why this row may not drive a note, else None."""
    if not isinstance(r, dict):
        return "row is not an object"
    if is_example(r):
        return "example row: an illustration, never evidence, so it drives no note or downgrade"
    if r.get("state") not in (None, STATE_MEASURED):
        return "state must be measured or example (or absent)"
    if not (isinstance(r.get("class"), str) and r["class"].strip()):
        return "no class"
    at = r.get("agent_types")
    if not (isinstance(at, list) and at and all(isinstance(x, str) and x for x in at)):
        return "agent_types must be a non-empty list of spawn types"
    if r.get("recommend") not in TIERS:
        return "recommend must be one of %s" % "|".join(TIERS)
    if not (isinstance(r.get("evidence"), str) and r["evidence"].strip()):
        return "no evidence text"
    if not (isinstance(r.get("verified"), str) and DATE.match(r["verified"])):
        return "verified must be YYYY-MM-DD"
    if not _int(r.get("samples")) or r["samples"] < MIN_SAMPLES:
        return "needs a measured sample of at least %d judged runs" % MIN_SAMPLES
    p = r.get("opus_needed_pct")
    if not _int(p) or p < 0 or p > MAX_OPUS_NEEDED_PCT:
        return "opus_needed_pct must be a measured integer percent, at most %d" % MAX_OPUS_NEEDED_PCT
    return None


def usable(rows):
    """-> (rows that pass the gate, [(class, reason)] for those that do not)."""
    good, problems = [], []
    for r in rows:
        if is_example(r):
            bad = example_problem(r)                      # a valid example is simply not usable evidence
            if bad:
                problems.append((r.get("class"), bad))
            continue
        bad = row_problem(r)
        if bad:
            problems.append((r.get("class") if isinstance(r, dict) else "?", bad))
        else:
            good.append(r)
    return good, problems


def examples(rows):
    """-> (valid example rows, [(class, reason)] for example rows that are not valid). Other rows are ignored."""
    good, problems = [], []
    for r in rows:
        if not is_example(r):
            continue
        bad = example_problem(r)
        if bad:
            problems.append((r.get("class"), bad))
        else:
            good.append(r)
    return good, problems


def load_examples(vault=None):
    """-> (example rows, problems) from the registry slot. A measured row of the same class in the owner's own
    pack shadows the shipped example (the slot is keyed by class), so it does not appear here."""
    import gt_registry
    effective, _shadowed, _retracted, reg_problems = gt_registry.resolve(SLOT, vault=vault)
    rows, bad = examples([rec["entry"] for rec in effective])
    return rows, list(reg_problems) + bad


def load_rows(vault=None):
    """-> (usable rows, problems) from the registry slot."""
    import gt_registry
    effective, _shadowed, _retracted, reg_problems = gt_registry.resolve(SLOT, vault=vault)
    rows, bad = usable([rec["entry"] for rec in effective])
    return rows, list(reg_problems) + bad


def example_note_for(tool_input, class_rows, example_rows):
    """-> one line, or None: the EXAMPLE guidance for a spawn whose work class has an example row that says Opus
    is worth it (and, for a class with a rule such as 'the reviewer must differ from the builder', that rule).
    Context only: it never changes the model, it is labelled not measured, and any error means no line."""
    try:
        import gt_model_classes
        c = gt_model_classes.classify(tool_input, class_rows)
        if not c:
            return None
        for r in example_rows:
            if r.get("class") == c["class"] and r.get("recommend") == "opus" and example_problem(r) is None:
                rule = (" Rule: " + r["rule"].rstrip(".") + ".") if r.get("rule") else ""
                gate = (" A deep verifier on Sonnet-built work needs all four conditions to hold, once per risk "
                        "cluster per release." if all(f in r for f in POLICY_FIELDS) else "")
                return ("gt: this spawn looks like %s work. Example guidance, not measured: Opus is worth it here "
                        "(%s).%s%s `gt_settings.py models advise` shows the table. Suggestion only, the model is yours."
                        % (c["class"], r["evidence"].rstrip("."), rule, gate))
        return None
    except Exception:                                         # noqa: BLE001 - advice never blocks
        return None


def note_for(tool_input, rows):
    """-> one line, or None. A spawn that names opus, whose type a usable row speaks for."""
    try:
        if not isinstance(tool_input, dict):
            return None
        t = tool_input.get("subagent_type") or "general-purpose"
        if "opus" not in str(tool_input.get("model") or "").lower():
            return None
        for r in rows:
            if row_problem(r) is None and t in r["agent_types"]:
                return ("gt: %s work (%s) measured fine on %s, Opus needed in %d%% of %d judged runs "
                        "(verified %s); suggestion only, the model is yours."
                        % (r["class"], t, r["recommend"], r["opus_needed_pct"], r["samples"],
                           r["verified"]))
        return None
    except Exception:                                         # noqa: BLE001 - advice never blocks
        return None
