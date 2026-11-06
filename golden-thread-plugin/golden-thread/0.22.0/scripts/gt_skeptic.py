#!/usr/bin/env python3
"""gt_skeptic.py -- rank the skeptic's flags and keep what the user did with them. (0.21.0)

    gt_skeptic.py rank     <record.json> [--max N | --all] [--json] [--vault V]
    gt_skeptic.py outcome  --session ID (--entry E --kind K --decision fix|keep|drop | --decisions-file F) [--vault V]
    gt_skeptic.py outcomes [--json] [--vault V]

`skeptic_pass` is on by default (owner, 2026-10-08). The skeptic can raise a long list, and a long
list is not read; so what is SHOWN to the user is the top `skeptic_max_flags` (default 7) by
severity, and the rest are one command away (`rank <record> --all`). Nothing is dropped.

SEVERITY, most severe first: a contradiction with a recorded fact; overclaimed and
unsupported-number (same tier); scope; stale; unverified. Ties keep the order the skeptic gave them.

OUTCOMES. When the user decides on a flag -- fix the entry, keep it as written, or drop it --
gt-work records the decision (`skeptic_record_outcomes`, on by default) as one line in
`<vault>/Projects/golden-thread/spool/skeptic/outcomes.jsonl`: time, session, the entry's title, the
flag's kind and the decision. Append-only, gt's own spool, never vault Markdown. `outcomes` reads
them back as acceptance rates per kind (accepted = fix or drop; kept = the user disagreed), so a kind
of flag that is always kept can be tuned or retired. The skeptic only advises; nothing here gates a
write-back.

Exit: 0 ok | 2 usage.
"""
import argparse
import datetime
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

TIER = {"contradiction": 0, "overclaimed": 1, "unsupported-number": 1, "scope": 2, "stale": 3,
        "unverified": 4}
DECISIONS = ("fix", "keep", "drop")
SPOOL = ("Projects", "golden-thread", "spool", "skeptic")
OUTCOMES = "outcomes.jsonl"


def setting(name, default):
    try:
        import gt_settings
        v = gt_settings.get(name)
        return v if v not in (None, "") else default
    except Exception:                                  # noqa: BLE001 - the default, never fail
        return default


def find_vault(explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    if os.environ.get("GT_VAULT"):
        return os.path.abspath(os.environ["GT_VAULT"])
    try:
        from gt_paths import find_vault as fv
        v = fv()
        return str(v) if v else None
    except Exception:                                  # noqa: BLE001
        return None


def items_of(record):
    """-> the skeptic's findings as one list [{kind, entry, ...}], severity-sorted (stable)."""
    res = record.get("result") if isinstance(record.get("result"), dict) else record
    items = []
    for c in res.get("contradictions") or []:
        if isinstance(c, dict):
            items.append({"kind": "contradiction", "entry": str(c.get("finding", "")),
                          "why": str(c.get("detail", "")), "fact_ref": str(c.get("fact_ref", ""))})
    for f in res.get("flags") or []:
        if isinstance(f, dict):
            items.append({"kind": str(f.get("kind", "unverified")), "entry": str(f.get("entry", "")),
                          "quote": str(f.get("quote", "")), "why": str(f.get("why", "")),
                          "suggested_fix": str(f.get("suggested_fix", ""))})
    return sorted(items, key=lambda i: TIER.get(i["kind"], len(TIER)))


def limit(a):
    if a.all:
        return None
    if a.max is not None:
        return max(0, a.max)
    v = str(setting("skeptic_max_flags", "7"))
    return None if v == "all" else int(v) if v.isdigit() else 7


def cmd_rank(a):
    try:
        with open(a.record, encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError) as exc:
        print("gt_skeptic: cannot read %s: %s" % (a.record, exc), file=sys.stderr)
        return 2
    items = items_of(rec)
    n = limit(a)
    shown = items if n is None else items[:n]
    hidden = len(items) - len(shown)
    if a.json:
        print(json.dumps({"shown": shown, "hidden": hidden, "total": len(items)}, indent=1))
        return 0
    if not items:
        print("The skeptic raised nothing.")
        return 0
    print("Skeptic: %d flag(s), showing the %d most severe" % (len(items), len(shown)))
    for i in shown:
        print("  [%s] %s -- %s" % (i["kind"], i["entry"][:80], i["why"][:200]))
        if i.get("suggested_fix"):
            print("      smallest fix: %s" % i["suggested_fix"][:200])
    if hidden:
        print("%d more not shown: python3 %s rank %s --all" % (hidden, os.path.abspath(__file__),
                                                              a.record))
    return 0


def spool_file(vault):
    return os.path.join(vault, *SPOOL, OUTCOMES)


def cmd_outcome(a):
    if str(setting("skeptic_record_outcomes", "on")) == "off":
        print("skeptic_record_outcomes is off; nothing recorded")
        return 0
    vault = find_vault(a.vault)
    if not vault:
        print("gt_skeptic: no vault (pass --vault or set GT_VAULT)", file=sys.stderr)
        return 2
    rows = []
    if a.decisions_file:
        try:
            with open(a.decisions_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            print("gt_skeptic: cannot read %s: %s" % (a.decisions_file, exc), file=sys.stderr)
            return 2
        rows = data if isinstance(data, list) else []
    elif a.entry is not None and a.kind and a.decision:
        rows = [{"entry": a.entry, "kind": a.kind, "decision": a.decision}]
    else:
        print("gt_skeptic: outcome needs --entry, --kind and --decision, or --decisions-file",
              file=sys.stderr)
        return 2
    clean = []
    for r in rows:
        if not isinstance(r, dict) or r.get("decision") not in DECISIONS:
            print("gt_skeptic: decision must be one of %s" % ", ".join(DECISIONS), file=sys.stderr)
            return 2
        clean.append({"ts": datetime.datetime.now().replace(microsecond=0).isoformat(),
                      "session": str(a.session or "")[:64], "entry": str(r.get("entry", ""))[:80],
                      "kind": str(r.get("kind", "unverified"))[:40], "decision": r["decision"]})
    path = spool_file(vault)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        for r in clean:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    print("recorded %d skeptic outcome(s)" % len(clean))
    return 0


def read_outcomes(vault):
    rows = []
    try:
        with open(spool_file(vault), encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("decision") in DECISIONS:
                    rows.append(r)
    except OSError:
        pass
    return rows


def cmd_outcomes(a):
    vault = find_vault(a.vault)
    if not vault:
        print("gt_skeptic: no vault (pass --vault or set GT_VAULT)", file=sys.stderr)
        return 2
    rows = read_outcomes(vault)
    kinds = {}
    for r in rows:
        k = kinds.setdefault(r.get("kind", "unverified"), {"total": 0, "fix": 0, "keep": 0, "drop": 0})
        k["total"] += 1
        k[r["decision"]] += 1
    for k in kinds.values():
        k["accepted_rate"] = round((k["fix"] + k["drop"]) / k["total"], 4)
    if a.json:
        print(json.dumps({"total": len(rows), "kinds": kinds}, indent=1, sort_keys=True))
        return 0
    if not rows:
        print("no skeptic outcomes recorded yet")
        return 0
    print("Skeptic outcomes: %d decision(s)" % len(rows))
    for name, k in sorted(kinds.items(), key=lambda kv: TIER.get(kv[0], 9)):
        print("  %-20s %3d flagged  fix %d  drop %d  keep %d  -> accepted %d%%" % (
            name, k["total"], k["fix"], k["drop"], k["keep"], round(100 * k["accepted_rate"])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_skeptic.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("rank")
    p.add_argument("record")
    p.add_argument("--max", type=int)
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--vault")
    p = sub.add_parser("outcome")
    p.add_argument("--session", default="")
    p.add_argument("--entry")
    p.add_argument("--kind")
    p.add_argument("--decision")
    p.add_argument("--decisions-file")
    p.add_argument("--vault")
    p = sub.add_parser("outcomes")
    p.add_argument("--json", action="store_true")
    p.add_argument("--vault")
    a = ap.parse_args(argv)
    return {"rank": cmd_rank, "outcome": cmd_outcome, "outcomes": cmd_outcomes}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
