#!/usr/bin/env python3
"""gt_model_advise.py -- the Opus-vs-Sonnet advisor (0.22.0, phase 104, item N41).

    gt_settings.py models advise [--agent TYPE] [--days N] [--context TOKENS] [--json]
    python3 gt_model_advise.py   [same flags]

For each agent type (and each task shape the Opus-needed sampler has judged) that ran on a deep-tier model,
says whether Sonnet would have sufficed, citing the recorded numbers. It READS three things that already
exist and builds no second store:
  * the 7-day usage by agent type (gt_models_page.usage_by_agent: runs, tokens, cost, runs per model);
  * the stored calibration verdicts (model-calibration/latest.json, one per stage);
  * the Opus-needed rate per class (gt_model_calibrate.opus_rates over opus-needed.jsonl, phase 82b).
and the model record table (gt_model_records) for tier and price.

VERDICTS
  sonnet-sufficed  evidence says one tier down held (calibration move-down, or a judged move-down candidate)
  opus-needed      calibration kept or raised it, or the judged Opus-needed rate is high; any evidence that
                   says Opus was needed beats evidence that says it was not
  no-data          no verdict and not enough judged samples: nothing is invented, the current model stays
  not-opus         the agent ran on a model that is not deep tier: nothing to advise
  unpriced         the model has no price row: no dollar figure is shown for it

Every figure carries a state: measured (counted from transcripts, the ledger or a judged sample), estimated
(tokens at list price) or unknown. SUGGEST ONLY: it prints the exact `gt_settings.py models set` command and
changes no setting. It stores nothing: only agent types and class names are read or shown, never a prompt.
Fails open: any error prints one line and exits 0.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.dont_write_bytecode = True

import gt_model_records                                  # noqa: E402
from gt_model_advice import EXAMPLE_BANNER, POLICY_FIELDS, POLICY_HEAD   # noqa: E402 - the one wording of each

OK = 0
DEEP, BALANCED = "deep", "balanced"
DEFAULT_DAYS = 7
DEFAULT_CONTEXT = 100000
CACHE_READ_DEFAULT = 0.1                                 # the multiple used when a row has no verified one
MOVE_DOWN = "move-down"


def _table():
    return gt_model_records.TABLE


def _short(agent):
    """The type the models page and `models set` use: a gt stage has no `gt:` prefix there."""
    return agent[len("gt:"):] if agent.startswith("gt:") else agent


def cost_line(deep_model, balanced_model, context_tokens, table=None):
    """-> the price of ONE prompt of `context_tokens` on each model, fresh and cache-read, from the price file
    (long-prompt multiple applied past a verified threshold). A model with no price row -> state unknown."""
    t = table or _table()
    out = {"context_tokens": context_tokens, "state": "estimated", "ratio": None}
    for key, model in (("deep", deep_model), ("balanced", balanced_model)):
        p, known = t.price(model, context_tokens)
        if not known:
            out[key] = {"model": model, "fresh_usd": None, "cached_usd": None}
            out["state"] = "unknown"
            continue
        fresh = p * context_tokens / 1e6
        out[key] = {"model": model, "fresh_usd": fresh,
                    "cached_usd": fresh * t.cache_read(model, CACHE_READ_DEFAULT)}
    if out["state"] == "estimated" and out["balanced"]["fresh_usd"]:
        out["ratio"] = out["deep"]["fresh_usd"] / out["balanced"]["fresh_usd"]
    return out


def _fig(name, value, state, text=None):
    return {"name": name, "value": value, "state": state, "text": text}


def _evidence(agent, rates, latest):
    """-> (votes, evidence, figures): votes are 'sufficed' / 'needed' from each source that has a real one."""
    votes, ev, figs = [], [], []
    stage = _short(agent)
    st = (latest.get("stages") or {}).get(stage) if isinstance(latest, dict) else None
    if isinstance(st, dict) and st.get("verdict"):
        v = st["verdict"]
        votes.append("sufficed" if v == MOVE_DOWN else "needed")
        agree = st.get("agreement")
        ev.append({"source": "calibration", "state": "measured",
                   "text": "calibration verdict %s on %s%s" % (
                       v, st.get("date") or "an unrecorded date",
                       ", agreement %.2f" % agree if isinstance(agree, (int, float)) else "")})
        figs.append(_fig("calibration", v, "measured"))
    else:
        figs.append(_fig("calibration", None, "unknown", "no stored verdict"))
    rate = None
    if isinstance(rates, dict):
        rate = rates.get(agent) or rates.get(stage)
    if isinstance(rate, dict) and rate.get("n"):
        state = rate.get("state")
        txt = "Opus-needed: original better in %d of %d judged replays (%s)" % (
            rate.get("needed", 0), rate["n"], state)
        if state == "move-down-candidate":
            votes.append("sufficed")
        elif state == "keep-opus":
            votes.append("needed")
        else:
            txt += "; " + str(rate.get("note") or "more samples needed")
        ev.append({"source": "opus_needed", "state": "measured", "text": txt})
        figs.append(_fig("opus_needed", rate.get("rate"), "measured"))
    else:
        figs.append(_fig("opus_needed", None, "unknown", "no judged samples"))
    return votes, ev, figs


def _modal(models):
    return max(models.items(), key=lambda kv: kv[1])[0] if models else None


def _agent_row(agent, h, rates, latest, context, table):
    model = _modal(h.get("models") or {})
    row = {"type": agent, "kind": "agent", "model": model, "tier": None, "verdict": "no-data",
           "command": None, "evidence": [], "figures": []}
    runs = h.get("runs", 0)
    row["figures"].append(_fig("runs", runs, "measured"))
    if not runs or model is None:
        row["evidence"].append({"source": "history", "state": "unknown", "text": "no runs in the window"})
        row["figures"].append(_fig("cost", None, "unknown"))
        row["figures"].append(_fig("saving_if_moved", None, "unknown"))
        return row
    price, known = table.price(model)
    row["tier"] = table.tier_of(model)
    if not known:
        row["verdict"] = "unpriced"
        row["evidence"].append({"source": "price", "state": "unknown",
                                "text": "%s has no price row: shown as unpriced, no dollar figure" % model})
        row["figures"].append(_fig("cost", None, "unknown"))
        row["figures"].append(_fig("saving_if_moved", None, "unknown"))
        return row
    row["figures"].append(_fig("cost", h.get("cost_usd"), "estimated"))
    if row["tier"] != DEEP:
        row["verdict"] = "not-opus"
        row["evidence"].append({"source": "tier", "state": "measured",
                                "text": "%s is %s tier, not deep: nothing to advise" % (model, row["tier"] or "no")})
        row["figures"].append(_fig("saving_if_moved", None, "unknown"))
        return row
    votes, ev, figs = _evidence(agent, rates, latest)
    row["evidence"] += ev
    row["figures"] += figs
    sonnet = table.tier_model(BALANCED)
    sp, sknown = table.price(sonnet) if sonnet else (None, False)
    if "needed" in votes:
        row["verdict"] = "opus-needed"
    elif "sufficed" in votes:
        row["verdict"] = "sonnet-sufficed"
        row["command"] = "python3 gt_settings.py models set %s %s" % (_short(agent), sonnet)
    if not votes:
        row["evidence"].append({"source": "history", "state": "unknown",
                                "text": "no calibration verdict and no judged Opus-needed sample: no data, "
                                        "the current model stays"})
    if row["verdict"] == "sonnet-sufficed" and sknown and price:
        saved = round((h.get("cost_usd") or 0.0) * (1 - sp / price), 4)
        row["figures"].append(_fig("saving_if_moved", saved, "estimated",
                                   "the window's cost at the %s price instead" % sonnet))
    else:
        row["figures"].append(_fig("saving_if_moved", None, "unknown"))
    row["cost_line"] = cost_line(model, sonnet, context, table) if sonnet else None
    return row


def _shape_row(name, rate):
    votes = {"move-down-candidate": "sonnet-sufficed", "keep-opus": "opus-needed"}
    return {"type": name, "kind": "shape", "model": None, "tier": None, "command": None,
            "verdict": votes.get(rate.get("state"), "no-data"),
            "evidence": [{"source": "opus_needed", "state": "measured",
                          "text": "Opus-needed: original better in %d of %d judged replays (%s)" % (
                              rate.get("needed", 0), rate.get("n", 0), rate.get("state"))}],
            "figures": [_fig("opus_needed", rate.get("rate"), "measured")]}


def _example_view(r):
    """One example row as shown: its state is always `example`, and no figure is ever attached to it."""
    return {"class": r["class"], "agent_types": list(r["agent_types"]), "recommend": r["recommend"],
            "reason": r.get("evidence") or "", "rule": r.get("rule") or "", "state": "example",
            "verified": r.get("verified"),
            "policy": {f: r[f] for f in POLICY_FIELDS if f in r} if all(f in r for f in POLICY_FIELDS) else None}


def advise(history, rates, latest, context=DEFAULT_CONTEXT, only=None, table=None, examples=None,
           deep_classes=None):
    """-> {rows, context_tokens, examples, banner, deep_classes}. history {agent: usage}, rates {class: opus
    rate}, latest the calibration store. `examples` are the EXAMPLE rows of the advice table (illustrations, never
    evidence) and `deep_classes` the classes the class table keeps on the deepest model. Pure: reads nothing,
    writes nothing."""
    t = table or _table()
    rows = []
    for agent in sorted(history or {}):
        if only and only not in (agent, _short(agent)):
            continue
        rows.append(_agent_row(agent, history[agent], rates or {}, latest or {}, context, t))
    known = {r["type"] for r in rows} | {_short(r["type"]) for r in rows}
    for name in sorted(rates or {}):
        if name not in known and not only and isinstance((rates or {})[name], dict) \
                and name not in {"gt:" + k for k in known}:
            rows.append(_shape_row(name, rates[name]))
    ex = [_example_view(r) for r in (examples or [])] if not only else []
    return {"rows": rows, "context_tokens": context, "examples": ex,
            "banner": EXAMPLE_BANNER if ex else None, "deep_classes": sorted(deep_classes or []) if ex else []}


def _money(v):
    return "unknown" if v is None else "$%.4f" % v


WRAP = 100                                               # example text wraps here so a laptop terminal does not break it


def _wrap(prefix, text, indent):
    """One statement as lines of at most WRAP characters: `prefix` on the first, `indent` spaces after it."""
    import textwrap
    return textwrap.fill(text, width=WRAP, initial_indent=prefix, subsequent_indent=" " * indent).split("\n")


def render(out):
    lines = ["Opus or Sonnet? (advice only: nothing is changed; figures are [measured], [estimated] or "
             "[unknown])"]
    if not out["rows"]:
        lines.append("  no agent runs in the window and no judged Opus-needed samples: no data")
    for r in out["rows"]:
        lines.append("")
        lines.append("%s%s: %s" % (r["type"], " (task shape)" if r["kind"] == "shape" else "", r["verdict"]))
        if r.get("model"):
            lines.append("  ran on %s (%s tier)" % (r["model"], r["tier"] or "no"))
        for f in r["figures"]:
            val = "unknown" if f["value"] is None else (
                _money(f["value"]) if f["name"] in ("cost", "saving_if_moved") else
                ("%.0f%%" % (100 * f["value"]) if f["name"] == "opus_needed" else f["value"]))
            lines.append("  %-16s %s [%s]%s" % (f["name"], val, f["state"], "  " + f["text"] if f.get("text") else ""))
        for e in r["evidence"]:
            lines.append("  - %s [%s]" % (e["text"], e["state"]))
        c = r.get("cost_line")
        if c and c["ratio"] is not None:
            lines.append("  one %d-token prompt: %s %s fresh / %s cached; %s %s fresh / %s cached; %.1fx [estimated]"
                         % (c["context_tokens"], c["deep"]["model"], _money(c["deep"]["fresh_usd"]),
                            _money(c["deep"]["cached_usd"]), c["balanced"]["model"],
                            _money(c["balanced"]["fresh_usd"]), _money(c["balanced"]["cached_usd"]), c["ratio"]))
        if r["command"]:
            lines.append("  to apply (suggest only, you run it): %s" % r["command"])
    if out.get("examples"):
        lines.append("")
        lines.append(out.get("banner") or EXAMPLE_BANNER)
        lines.append("  Illustrations of when each tier is worth it. None is a measurement and none changes any model;")
        lines.append("  a measured row of your own replaces its example (same class name).")
        deep = [e for e in out["examples"] if e["recommend"] == "opus"]
        cheap = [e for e in out["examples"] if e["recommend"] != "opus"]
        if deep:
            lines.append("")
            lines.append("  When Opus (the deepest tier) is worth it:")
            for e in deep:
                lines.append("    %s [%s]" % (e["class"], e["state"]))
                lines.extend(_wrap("      why:  ", e["reason"], 12))
                if e.get("rule"):
                    lines.extend(_wrap("      rule: ", e["rule"], 12))
        pol = next((e["policy"] for e in deep if e.get("policy")), None)
        if pol:
            who = ", ".join(e["class"] for e in deep if e.get("policy"))
            lines.append("")
            lines.append("  Deep verifier policy [example; rows: %s]" % who)
            lines.extend(_wrap("  ", POLICY_HEAD, 2))
            for f in ("when_high_stakes", "when_new_logic", "when_tests_pass", "when_no_gate"):
                lines.extend(_wrap("    ", pol[f], 8))
            for f in ("limit_cluster", "limit_fixer", "limit_differ"):
                lines.extend(_wrap("    ", pol[f], 12))
            lines.extend(_wrap("  Evidence: ", pol["policy_evidence"], 12))
        if cheap:
            lines.append("")
            lines.append("  When Sonnet or the fast tier is enough:")
            for e in cheap:
                lines.append("    %-15s try %-6s for %s [%s]" % (e["class"], e["recommend"],
                                                               ", ".join(e["agent_types"]), e["state"]))
                lines.extend(_wrap("      why:  ", e["reason"], 12))
        if out.get("deep_classes"):
            lines.append("")
            lines.append("  The class table keeps these on the deepest model and never downgrades them: %s."
                         % ", ".join(out["deep_classes"]))
    return "\n".join(lines)


def load_history(days):
    import gt_models_page
    return gt_models_page.usage_by_agent(days) or {}


def load_rates():
    import gt_model_calibrate
    return gt_model_calibrate.opus_rates(gt_model_calibrate.read_opus_rows())


def load_examples():
    """-> (example rows, class names the class table keeps deep). Any failure -> ([], [])."""
    try:
        import gt_model_advice
        import gt_model_classes
        rows, _ = gt_model_advice.load_examples()
        deep = [r["class"] for r in gt_model_classes.load_rows()[0] if r.get("tier") == "deep"]
        return rows, deep
    except Exception:                                    # noqa: BLE001 - the examples are a convenience
        return [], []


def load_latest():
    import gt_models_page
    return gt_models_page.load_latest()


def build_parser():
    ap = argparse.ArgumentParser(
        prog="gt_model_advise.py",
        description="Would Sonnet have sufficed? Reads the measured usage, calibration verdicts and Opus-needed "
                    "rates; prints advice and the exact command. Suggest only: it changes no setting.")
    ap.add_argument("--agent", metavar="TYPE", help="only this agent type (e.g. verify or gt:verify)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help="usage window in days (default %d)" % DEFAULT_DAYS)
    ap.add_argument("--context", type=int, default=DEFAULT_CONTEXT, metavar="TOKENS",
                    help="prompt size for the cost line (default %d)" % DEFAULT_CONTEXT)
    ap.add_argument("--json", action="store_true", help="print the advice as JSON")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        ex_rows, deep = load_examples()
        out = advise(load_history(a.days), load_rates(), load_latest(), context=a.context, only=a.agent,
                     examples=ex_rows, deep_classes=deep)
        print(json.dumps(out, indent=1) if a.json else render(out))
    except Exception as e:                               # noqa: BLE001 - advice never breaks anything
        print("advisor unavailable (%s): no advice given" % type(e).__name__)
    return OK


if __name__ == "__main__":
    sys.exit(main())
