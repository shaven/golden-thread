#!/usr/bin/env python3
"""gt_farm_export.py -- a READ-ONLY export of gt-farm's stored calibration verdicts, ranked by cost (gt 0.22.0).

    gt_farm_calibrate.py --export-json [--task-type T] [--top N] [--include-review] [--vault V]

WHY. Calibration says, for each engine and task class, can-take / review-only / not-suitable, with the
measured speed and what the engine costs. A coding assistant (opencode, Codex) that picks its model per task
can use that to take the cheapest engine that is capable, instead of a static model line. This module is the
bridge: it only reads and prints JSON; the calibration logic is untouched.

WHAT IT PRINTS. A JSON list, one row per (calibrated engine, task class), cheapest first:

    [{"engine": "...", "model": "...", "task": "draft", "verdict": "can-take", "cost_per_mtok": 0.0,
      "tok_per_s": 30.0, "usd_spent_calibration": 0.0, "hosted": false, "calibrated": "2026-10-28"}, ...]

  cost_per_mtok   USD per million tokens: 0.0 for a local engine; for a hosted engine the mean of the price
                  table's input and output rate stored in the record; null when the record has no price
                  (null sorts last: no number is invented).
  usd_spent_calibration  what the calibration run itself cost, or null when unknown.
  Only can-take rows; --include-review adds review-only. A not-suitable row is never printed.

SOURCE. The live store, read on every call: the vault's calibration files (Projects/golden-thread/farm/
calibrations/*.md), then the local cache for a model the vault has not got. A record that is stale (the probe
set, adjudicator or reference changed) contributes nothing, as in gt_farm_calibration.class_verdicts.

SAFE. Writes nothing anywhere (no file, no cache, no ledger). Calibration records hold no URL or key file; the
rows carry names and numbers only, and a value that looks like a URL or a key is refused rather than printed.

--task-type takes a class name (log-triage, quote-extract, extract, classify, draft, generalize, review,
summarise, code-fix, code-feature) or a friendly alias (quote-extraction, field-extraction, classification,
drafting, generalising-claims, code-review, summaries). An unknown one exits 2.

Exit: 0 ok (an empty list is ok) | 1 a row held a URL or key | 2 usage.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
import gt_farm_calibration as cal                                    # noqa: E402
import gt_farm_probes as probes                                      # noqa: E402

OK, REFUSED, USAGE = 0, 1, 2

ALIASES = {"quote-extraction": "quote-extract", "field-extraction": "extract", "classification": "classify",
           "drafting": "draft", "generalising-claims": "generalize", "code-review": "review",
           "summaries": "summarise"}
CLASSES = tuple(probes.TASK_CLASSES) + tuple(probes.CODE_CLASSES)
_VERDICTS = ("can-take", "review-only")
_RANK = {"can-take": 0, "review-only": 1}
_LEAK = re.compile(r"(://|key_file|api[_-]?key|secret|bearer\s|\bsk-[A-Za-z0-9]{8,})", re.I)


def resolve_task(name):
    """-> the class name for a class or an alias, or None."""
    n = str(name or "").strip().lower()
    n = ALIASES.get(n, n)
    return n if n in CLASSES else None


def cost_per_mtok(rec):
    """USD per million tokens: 0.0 local, mean of in/out rate when hosted and priced, else None."""
    if not rec.get("hosted"):
        return 0.0
    cost = rec.get("cost") if isinstance(rec.get("cost"), dict) else {}
    rates = []
    for k in ("in_per_mtok", "out_per_mtok"):
        v = cost.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            rates.append(float(v))
    return round(sum(rates) / len(rates), 6) if len(rates) == 2 else None


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _records(vault):
    """Current records: the vault's, then cache models the vault lacks; the newest per (engine, model, quant)."""
    recs, seen = [], set()
    for r in cal.vault_records(vault):
        seen.add(r.get("fingerprint"))
        recs.append(r)
    for fp, r in (cal.cache_load().get("models") or {}).items():
        if isinstance(r, dict) and fp not in seen and r.get("fingerprint"):
            recs.append(r)
    try:
        expected = cal.expected_context(vault)
    except Exception:                                               # noqa: BLE001 -- the defaults stand
        expected = {}
    newest = {}
    for r in recs:
        if not isinstance(r.get("kinds"), dict):
            continue
        try:
            if cal.staleness(r, expected):
                continue
        except Exception:                                           # noqa: BLE001 -- fail closed
            continue
        key = (r.get("engine"), r.get("model_id"), r.get("quant"))
        if key not in newest or str(r.get("at") or "") > str(newest[key].get("at") or ""):
            newest[key] = r
    return list(newest.values())


def rows(vault, task=None, include_review=False):
    want = _VERDICTS if include_review else _VERDICTS[:1]
    out = []
    for r in _records(vault):
        for cls in CLASSES:
            if task and cls != task:
                continue
            k = cal.kind_of(r, cls)
            if not isinstance(k, dict) or k.get("verdict") not in want:
                continue
            out.append({"engine": str(r.get("engine") or r.get("model_id") or ""),
                        "model": str(r.get("model_id") or ""), "task": cls, "verdict": k["verdict"],
                        "cost_per_mtok": cost_per_mtok(r), "tok_per_s": _num(k.get("tok_per_s")),
                        "usd_spent_calibration": _num(r.get("usd_spent")), "hosted": bool(r.get("hosted")),
                        "calibrated": str(r.get("date") or "")})
    out.sort(key=lambda x: (x["cost_per_mtok"] is None, x["cost_per_mtok"] or 0.0, _RANK[x["verdict"]],
                            -(x["tok_per_s"] or 0.0), x["engine"], x["task"]))
    return out


def decide(vault, task, task_key, include_review=False, table_path=None):
    """ADVICE at selection time: one decision for one task, as a dict (never raises).

    decision is an engine id, or "keep" when the task stays with the caller's own model. With farm_share off the
    share table is not applied and the decision is the plain cheapest capable engine (the rows() answer). With it
    on, gt_farm_share.select() decides (its share, ramp, pause, daily-cap and calibration rules, not a copy of
    them): "keep" unless it routes the task to the farm. The engine is the share row's pinned engine, else the
    cheapest capable one; a pinned engine that is not capable for the class is "keep". Nothing is recorded: no
    spend, no ramp step, no state file written."""
    import gt_farm_share as share
    capable = rows(vault, task, include_review)
    out = {"task": task, "task_key": str(task_key or ""), "decision": "keep", "applied": False, "route": "claude",
           "share": None, "audit_rate": None, "audit": False, "paused": False, "paused_reason": "",
           "budget_left": None, "why": ""}
    if not share.share_on():
        out["decision"] = capable[0]["engine"] if capable else "keep"
        out["why"] = ("farm_share is off, so the share table is not applied; "
                      + ("the cheapest capable engine" if capable else "no calibrated engine can take it"))
        return out
    out["applied"] = True
    try:
        table = share.load_share_table(share.find_table(None, vault))
        sel = share.select(table, task, task_key, calibrated=cal.class_verdicts(vault))
    except Exception as ex:                                          # noqa: BLE001 -- fail closed to keep
        out["why"] = "the share table could not be applied (%s), so the task stays with you" % ex
        return out
    for k in ("share", "audit_rate", "audit", "paused", "paused_reason", "budget_left", "why"):
        out[k] = sel[k]
    out["route"] = sel["route"]
    if sel["route"] != "farm":
        return out
    pinned = sel.get("engine")
    names = [r["engine"] for r in capable]
    if pinned:
        if pinned not in names:
            out["route"] = "claude"
            out["why"] = "the share row pins engine '%s', which is not capable for '%s', so the task stays with you" % (
                pinned, task)
            return out
        out["decision"] = pinned
    elif names:
        out["decision"] = names[0]
    else:
        out["route"] = "claude"
        out["why"] = "no calibrated engine can take '%s', so the task stays with you" % task
    return out


def run_decide(task_type, task_key, include_review, vault, stream=None):
    stream = stream or sys.stdout
    task = resolve_task(task_type) if task_type else None
    if not task:
        sys.stderr.write("--task-key needs --task-type, one of: %s (or an alias: %s)\n" % (
            ", ".join(CLASSES), ", ".join(sorted(ALIASES))))
        return USAGE
    if not str(task_key or "").strip():
        sys.stderr.write("--task-key needs a non-empty key (the same key always gets the same answer)\n")
        return USAGE
    text = json.dumps(decide(vault, task, task_key, include_review), indent=1, sort_keys=True)
    if _LEAK.search(text):
        sys.stderr.write("refused: the decision held something that looks like a URL or a key; nothing printed\n")
        return REFUSED
    stream.write(text + "\n")
    return OK


def run(task_type, top, include_review, vault, stream=None):
    stream = stream or sys.stdout
    task = None
    if task_type:
        task = resolve_task(task_type)
        if not task:
            sys.stderr.write("unknown task type %r; use one of: %s (or an alias: %s)\n" % (
                task_type, ", ".join(CLASSES), ", ".join(sorted(ALIASES))))
            return USAGE
    if top is not None and top < 1:
        sys.stderr.write("--top takes a number of 1 or more\n")
        return USAGE
    result = rows(vault, task, include_review)
    if top:
        result = result[:top]
    text = json.dumps(result, indent=1, sort_keys=True)
    if _LEAK.search(text):
        sys.stderr.write("refused: a calibration row held something that looks like a URL or a key; nothing printed\n")
        return REFUSED
    stream.write(text + "\n")
    return OK
