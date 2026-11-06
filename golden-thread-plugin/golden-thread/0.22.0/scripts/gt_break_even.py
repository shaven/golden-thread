#!/usr/bin/env python3
"""gt_break_even.py -- is handing a read to a subagent worth what the subagent itself costs? (gt 0.21.4)

    gt_break_even.py advise TOKENS [--local] [--json]   which route a read of ~TOKENS tokens should take
    gt_break_even.py show [--json]                      the threshold in force, where it came from
    gt_break_even.py paused [MECHANISM] [--json]        mechanisms whose 7-day net is negative (paused)

THE BALANCE RULE. Keeping a big payload out of the main conversation is not free: a subagent opens
its own context (its system prompt, tool list and instructions) before it reads a single byte of
the payload, and every token of that is billed. Delegating a small read spends more than it saves.
So gt's guidance (the gt-digest skill, the spill and intake messages) recommends a subagent only
when the expected payload is larger than the subagent's own fixed cost, and says so when it does
not:

    payload under the threshold -> read it inline (cheaper than the agent's start-up)
    payload over it             -> a subagent, which returns only the answer
    a local engine on the machine (gt-farm, secret-scanned first) -> always, whatever the size:
                                   it spends no Claude tokens at all

THE NUMBER. DEFAULT_SUBAGENT_OVERHEAD is the opening context of an Explore-class agent (the one
gt's guidance names), MEASURED 2026-10-15 on the owner's machine with `gt_savings.py overhead`: the
first model request of each subagent transcript under ~/.claude/projects (.../subagents/agent-*.jsonl)
carries input + cache-write + cache-read tokens that are all system prompt, tool definitions and
instructions, because the agent has not read anything yet. Over 14 days, 629 agent runs: Explore n=27,
median 17,947 tokens (quartiles 17,786 to 18,185; range 17,678 to 18,638); general-purpose n=212,
median 37,155; gt stage agents n=110, medians 5,497 to 12,991; the agent types Claude Code did not
record (older transcripts) n=252, median 27,866; all agents, median 27,747. The first request was a
cache WRITE in 99.2% of runs (billed at 1.25x), so the real opening bill is about 1.25x the figure.
18,000 is the Explore median rounded. It is conservative in the direction that matters: a larger
agent type (general-purpose, 37,000) would need a payload twice that before delegating pays, and
the rule recommends delegating less often, never more. Re-measure on any machine with
`gt_savings.py overhead --save`; `show` and `advise` then use that machine's own Explore median
(5 or more runs; else the constant). Setting `break_even_tokens` (auto | a number) pins it by hand.

WHAT IT MEANS FOR A PAYLOAD. The main conversation pays for a payload once to write it (1.25x) and
again on every later turn to re-read it (0.1x) until the next compaction. A subagent that reads it
pays the same write plus its own opening bill, and the main conversation then carries only the answer.
Delegating wins when the re-reads it saves exceed the opening bill: P x 0.1 x (turns left) > 1.25 x
opening. With 18,000 tokens of opening and 20 turns left that is a payload of about 11,000 tokens; with
40 turns left, about 6,000; with 10 turns left, about 23,000. The shipped threshold is the opening
cost itself, 18,000, because the turns left are not known when the question is asked: right at the
middle of that range, and it errs toward reading inline, the cheaper mistake.

WHAT THE OWNER'S OWN WEEK SAYS (`gt_savings.py record` + `report`, last 7 days, 68 agent runs, net =
kept - spent as the savings report defines it, estimated): agents that read under 5,000 tokens lost
(3 of 4 behind, median -5,600 token-equivalents, median 2 model calls); agents that read 5,000 to
18,000 tokens all won (5 of 5, median +284,000) because they made about 9 calls in main contexts of
about 300,000 tokens, each call a main-session call saved at a tenth of that context; above 18,000,
51 of 59 won. So the single-read rule is deliberately the cautious one: it is asked before the job
exists, when the number of calls the job will take is not known. A job that needs several reads or
steps pays sooner in a big context, and `/gt-usage:gt-usage-savings`' NET table is where that
measurement lives, not this threshold.

ADVISORY ONLY. Nothing here blocks or rewrites anything; it answers a question for the text that
asks it. `break_even off` turns the size rule off (guidance then recommends delegating whatever the
size). Stdlib only; fails to the constant on any error.

PAUSED MECHANISMS. gt_savings.py writes ~/.claude/golden-thread/usage/net-paused.json when a
mechanism's trailing 7-day NET (tokens kept out of the main context minus the tokens the mechanism
spent on Claude) is negative. `paused()` reads it; an auto-routing feature (farm_first auto) asks
here before it routes, and stands down while the mechanism is paused. A missing or unreadable file
means nothing is paused. With `savings_ledger` off nothing is paused either: nothing rewrites the file
then (gt_savings.py record removes it), and a pause written before the ledger was turned off would
otherwise stand for good with no NET behind it (0.21.4).

Exit: 0 | 2 usage.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DEFAULT_SUBAGENT_OVERHEAD = 18000          # tokens; see THE NUMBER above
MIN_THRESHOLD, MAX_THRESHOLD = 4000, 200000
CHARS_PER_TOKEN = 4                        # the estimate every gt size figure uses
MEASURE_AGENT = "Explore"                  # the agent type the guidance names
MEASURE_MIN_RUNS = 5                       # fewer runs than this and the constant stands


def _home():
    return os.path.expanduser("~")


def usage_dir():
    return os.path.join(_home(), ".claude", "golden-thread", "usage")


def overhead_path():
    return os.path.join(usage_dir(), "overhead.json")


def paused_path():
    return os.path.join(usage_dir(), "net-paused.json")


def _setting(name, default):
    """A setting's value, from gt_settings when it is importable, else from vault-config.json."""
    try:
        import gt_settings
        v = gt_settings.get(name)
        if v not in (None, ""):
            return str(v)
    except Exception:                                        # noqa: BLE001
        pass
    try:
        with open(os.path.join(_home(), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            v = json.load(fh).get(name)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    except (OSError, ValueError, AttributeError):
        pass
    return default


def enabled():
    """The `break_even` setting: anything but `off` is on."""
    return _setting("break_even", "on") != "off"


def measured():
    """-> (tokens, runs) from overhead.json for the guided agent type, or (None, 0)."""
    try:
        with open(overhead_path(), encoding="utf-8") as fh:
            doc = json.load(fh)
        row = (doc.get("by_type") or {}).get(MEASURE_AGENT) or {}
        tokens, runs = int(row.get("median")), int(row.get("runs"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None, 0
    if runs < MEASURE_MIN_RUNS or not MIN_THRESHOLD <= tokens <= MAX_THRESHOLD:
        return None, 0
    return tokens, runs


def threshold():
    """-> (tokens, source). source is 'setting', 'measured' or 'default'."""
    raw = _setting("break_even_tokens", "auto")
    if raw.isdigit() and MIN_THRESHOLD <= int(raw) <= MAX_THRESHOLD:
        return int(raw), "setting"
    tokens, _runs = measured()
    if tokens is not None:
        return tokens, "measured"
    return DEFAULT_SUBAGENT_OVERHEAD, "default"


def advise(payload_tokens, local_engine=False):
    """-> {"route": "local" | "inline" | "subagent", "threshold": N, "source": s, "reason": text,
    "advisory": bool}. `payload_tokens` is the expected size of what would be read."""
    try:
        payload = max(0, int(payload_tokens))
    except (TypeError, ValueError):
        payload = 0
    limit, source = threshold()
    out = {"payload_tokens": payload, "threshold": limit, "source": source, "advisory": True}
    if local_engine:
        out.update(route="local", reason=(
            "a local engine spends no Claude tokens, so it is worth using at any size"))
    elif not enabled():
        out.update(route="subagent", advisory=False, reason=(
            "break_even is off: the size rule is not applied"))
    elif payload < limit:
        out.update(route="inline", reason=(
            "about %d tokens is under the %d a subagent costs just to start; reading it here "
            "is cheaper" % (payload, limit)))
    else:
        out.update(route="subagent", reason=(
            "about %d tokens is over the %d a subagent costs to start; it returns only the "
            "answer" % (payload, limit)))
    return out


def ledger_off():
    """The savings_ledger setting is `off`. Read from vault-config.json directly, as gt_savings.py
    does: the spill hook asks on every Bash call, so no gt_settings import here."""
    try:
        with open(os.path.join(_home(), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            v = json.load(fh).get("savings_ledger")
    except (OSError, ValueError, AttributeError):
        return False
    return str(v or "on").strip().lower() == "off"


def paused(mechanism=None):
    """-> {mechanism: {"net": N, "days": 7, "since": date}} for the mechanisms paused now, or the
    one entry for `mechanism` ({} when it is not paused, or when savings_ledger is off). Never raises."""
    if ledger_off():
        return {}
    try:
        with open(paused_path(), encoding="utf-8") as fh:
            doc = json.load(fh)
        rows = doc.get("paused") if isinstance(doc, dict) else None
        rows = {k: v for k, v in rows.items() if isinstance(v, dict)} if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        return {}
    if mechanism is None:
        return rows
    row = rows.get(mechanism)
    return {mechanism: row} if isinstance(row, dict) else {}


def is_paused(mechanism):
    return bool(paused(mechanism))


def _show(as_json):
    limit, source = threshold()
    tokens, runs = measured()
    doc = {"enabled": enabled(), "threshold": limit, "source": source,
           "default": DEFAULT_SUBAGENT_OVERHEAD, "measured": tokens, "measured_runs": runs,
           "agent": MEASURE_AGENT, "chars": limit * CHARS_PER_TOKEN, "paused": paused()}
    if as_json:
        print(json.dumps(doc, sort_keys=True, indent=1))
        return 0
    print("break-even: %s" % ("on" if doc["enabled"] else "OFF (no size rule)"))
    print("a subagent costs about %d tokens just to start (%s)" % (limit, {
        "setting": "break_even_tokens, set by hand", "default": "the shipped constant",
        "measured": "measured here over %d %s runs" % (runs, MEASURE_AGENT)}[source]))
    print("so: under about %d characters read inline; over it, delegate; a local engine, always"
          % doc["chars"])
    if doc["paused"]:
        for name, row in sorted(doc["paused"].items()):
            print("paused: %s (7-day net %s token-equivalents)" % (name, row.get("net")))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_break_even.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("advise")
    p.add_argument("tokens", type=int)
    p.add_argument("--local", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("paused")
    p.add_argument("mechanism", nargs="?")
    p.add_argument("--json", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if a.cmd == "advise":
        out = advise(a.tokens, a.local)
        if a.json:
            print(json.dumps(out, sort_keys=True))
        else:
            print("%s -- %s" % (out["route"], out["reason"]))
        return 0
    if a.cmd == "show":
        return _show(a.json)
    if a.cmd == "paused":
        rows = paused(a.mechanism)
        if a.json:
            print(json.dumps(rows, sort_keys=True))
        elif rows:
            for name, row in sorted(rows.items()):
                print("%s paused: 7-day net %s token-equivalents" % (name, row.get("net")))
        else:
            print("nothing paused")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
