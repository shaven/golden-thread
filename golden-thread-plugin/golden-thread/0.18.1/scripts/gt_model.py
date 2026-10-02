#!/usr/bin/env python3
"""gt_model.py -- a skill declares a model INTENT; this says which model that means here.

    gt_model.py resolve [INTENT] [--vault V] [--json]
    gt_model.py skill PATH [--vault V] [--json]      # a SKILL.md, or the skill's directory
    gt_model.py check PATH [PATH ...]                # lint: every model_intent is a known value

A skill or agent states what kind of thinking its work needs, never which model does it:

    model_intent: fast | balanced | deep

    fast      many parallel, narrow, mechanical passes (a dimension-sliced review)
    balanced  the default -- what a skill gets today when it says nothing
    deep      one pass where being wrong is expensive (adversarial verification, a design
              decision, a security judgement)

WHY AN INTENT AND NOT A NAME. A model id changes on someone else's schedule, and a skill that
pins a retired name fails at the moment it is used, for a reason invisible from the skill. So
the ONE place a model name may appear is the `model` slot of the pack registry -- data, never
code, with the registry's precedence (community < core < local): a release ships defaults in
packs/core/model.intents.pack.json, and the owner's own pack in the vault always wins, with
the loser reported SHADOWED. Each entry records the date its name was last verified, because
a name nobody re-checked reads as evidence while being wrong (the SPDX_LIST_VERSION lesson).

WHAT A RUN SAYS. Every resolution states the model it resolved to. An unknown intent value is
refused (exit 2) -- `check` and the release gate's skill lint refuse it before anything runs.
An intent with no mapping is NOT silently passed: it is reported UNMAPPED and resolves to
`balanced`, naming both facts, and it never refuses to run. A skill that declares nothing
resolves to "the session's own model" -- exactly what happened before intents existed.

Exit: 0 resolved (UNMAPPED included -- loud, not fatal) | 1 resolved, but the registry reported
problems, or `check` found a bad value | 2 usage, or an unknown intent value.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gt_registry                                        # noqa: E402

SLOT = "model"
INTENTS = ("fast", "balanced", "deep")
DEFAULT = "balanced"
SESSION_DEFAULT = "the session's own model"
FRONTMATTER_KEY = re.compile(r"^model_intent:[ \t]*(.*?)[ \t]*$")
OK, PROBLEMS, USAGE = 0, 1, 2


def mapping(vault=None):
    """-> ({intent: record}, shadowed, retracted, problems) from the registry."""
    effective, shadowed, retracted, problems = gt_registry.resolve(SLOT, vault=vault)
    out = {}
    for rec in effective:
        e = rec["entry"]
        out[e.get("intent")] = {"model": e.get("model"), "verified": e.get("verified"),
                                "source": rec["source"], "tier": rec["tier"]}
    return out, shadowed, retracted, problems


def resolve(intent, vault=None):
    """-> dict: intent, declared, model, ran_at, unmapped, verified, source, tier, line, shadowed,
    problems. Raises ValueError for a value that is not an intent."""
    if intent is not None and intent not in INTENTS:
        raise ValueError("model_intent %r is not one of %s" % (intent, "|".join(INTENTS)))
    table, shadowed, _retracted, problems = mapping(vault)
    res = {"intent": intent, "declared": intent is not None, "model": None, "ran_at": None,
           "unmapped": False, "verified": None, "source": None, "tier": None,
           "shadowed": [{"intent": s["entry"].get("intent"), "model": s["entry"].get("model"),
                         "source": s["source"], "tier": s["tier"], "lost_to": s["lost_to"]}
                        for s in shadowed],
           "problems": [{"where": w, "what": t} for w, t in problems]}
    if intent is None:
        res["line"] = ("model_intent: none declared -> %s (unchanged; no intent resolved)"
                       % SESSION_DEFAULT)
        return res
    hit, at = table.get(intent), intent
    if hit is None:
        res["unmapped"] = True
        at = DEFAULT
        hit = table.get(DEFAULT)
    res["ran_at"] = at
    if hit is None:
        res["line"] = ("model_intent %s is %s -> running at %s, which is unmapped too -> %s"
                       % (intent, "UNMAPPED" if res["unmapped"] else "unmapped", at,
                          SESSION_DEFAULT))
        return res
    res.update(model=hit["model"], verified=hit["verified"], source=hit["source"],
               tier=hit["tier"])
    where = "%s pack %s, verified %s" % (hit["tier"], hit["source"], hit["verified"] or "never")
    if res["unmapped"]:
        res["line"] = ("model_intent %s is UNMAPPED -> running at %s -> model %s (%s)"
                       % (intent, at, hit["model"], where))
    else:
        res["line"] = "model_intent %s -> model %s (%s)" % (intent, hit["model"], where)
    return res


def read_intent(path):
    """-> (value or None, line number or None) from a SKILL.md's (or agent .md's) frontmatter."""
    p = Path(path)
    if p.is_dir():
        p = p / "SKILL.md"
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None
    for i, line in enumerate(lines[1:], 2):
        if line.strip() == "---":
            break
        m = FRONTMATTER_KEY.match(line)
        if m:
            return m.group(1).strip().strip("'\""), i
    return None, None


def check_paths(paths):
    """-> [problem str]. Every model_intent found must be a known value; position named."""
    out = []
    files = []
    for p in paths:
        p = Path(p)
        files += sorted(p.rglob("SKILL.md")) + sorted(p.rglob("agents/*.md")) if p.is_dir() else [p]
    for f in files:
        try:
            val, line = read_intent(f)
        except (OSError, UnicodeDecodeError) as exc:
            out.append("%s: unreadable (%s)" % (f, exc.__class__.__name__))
            continue
        if line is not None and val not in INTENTS:
            out.append("%s:%d: model_intent %r is not one of %s"
                       % (f, line, val[:40], "|".join(INTENTS)))
    return out


def _emit(res, as_json):
    if as_json:
        print(json.dumps(res, indent=2))
        return
    print(res["line"])
    for s in res["shadowed"]:
        print("  SHADOWED %s %s=%s <- lost to %s" % (s["tier"], s["intent"], s["model"],
                                                     s["lost_to"]))
    for p in res["problems"]:
        print("  PROBLEM %s: %s" % (gt_registry._clean(p["where"], 200),
                                    gt_registry._clean(p["what"], 300)), file=sys.stderr)
    if res["unmapped"]:
        print("gt_model: model_intent %s has no mapping in the `model` slot; ran at %s"
              % (res["intent"], res["ran_at"]), file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_model.py", description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", help="vault whose local packs apply (default: $GT_VAULT, config)")
    common.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve", parents=[common], help="what an intent means here")
    r.add_argument("intent", nargs="?", default=None)
    s = sub.add_parser("skill", parents=[common], help="resolve a skill's declared intent")
    s.add_argument("path")
    c = sub.add_parser("check", help="lint model_intent values in skills/agents")
    c.add_argument("paths", nargs="+")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        bad = check_paths(a.paths)
        for b in bad:
            print(b)
        print("model_intent: %s" % ("%d bad value(s)" % len(bad) if bad else "every value known"))
        return PROBLEMS if bad else OK
    intent = a.intent if a.cmd == "resolve" else None
    if a.cmd == "skill":
        try:
            intent, line = read_intent(a.path)
        except OSError as exc:
            print("cannot read %s (%s)" % (a.path, exc.__class__.__name__), file=sys.stderr)
            return USAGE
        if line is not None and intent not in INTENTS:
            print("%s:%d: model_intent %r is not one of %s -- refused"
                  % (a.path, line, (intent or "")[:40], "|".join(INTENTS)), file=sys.stderr)
            return USAGE
    try:
        res = resolve(intent, gt_registry.find_vault(a.vault))
    except ValueError as exc:
        print("%s -- refused" % exc, file=sys.stderr)
        return USAGE
    _emit(res, a.json)
    return PROBLEMS if res["problems"] else OK


if __name__ == "__main__":
    sys.exit(main())
