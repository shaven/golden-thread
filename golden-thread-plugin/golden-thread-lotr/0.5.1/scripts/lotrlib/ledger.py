"""One line in gt-usage's savings ledger per find: what the find cost against what loading the same
connections' tool schemas would have (gt-lotr 0.5.0, gt 0.21.4; request item 8, "measure it").

The ledger is `~/.claude/golden-thread/usage/savings.jsonl`, the file gt-usage reads and gt-farm
also appends to. This module appends ONE `lotr-find` line when gt-usage is in use on this machine
and its `savings_ledger` setting is not off:

  actual_eq          tokens of the find result the model received (results + notes)
  counterfactual_eq  tokens of every operation of the same connections as a full tool definition
  state              `estimated`: both are characters / 4, the ledger's own estimate

A line holds numbers and counts only: no query, no argument, no connection id, no path. It is
written only if the `usage` folder already exists (gt-usage creates it), so a hub without gt, or a
machine that never installed gt-usage, gets no stray file; and a write that fails is dropped, never
raised -- measuring must not break a find. Text mode with newline="\\n", like every gt text write.
"""
import json
import os
import secrets
import time

MECHANISM = "lotr-find"
LOTR_VERSION = "0.5.1"


def _base():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "usage")


def ledger_path():
    return os.path.join(_base(), "savings.jsonl")


def enabled():
    """gt-usage's `savings_ledger` setting, read from the one config file gt_settings keeps;
    anything but "off" is on."""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json"),
                  encoding="utf-8") as fh:
            v = json.load(fh).get("savings_ledger")
    except (OSError, ValueError, AttributeError):
        v = None
    return str(v or "on").strip().lower() != "off"


def record_find(actual, counterfactual, connections, ops, shown, left_out):
    """Append the line. -> True when it was written. Never raises."""
    try:
        if not enabled() or not os.path.isdir(_base()):
            return False
        line = {"v": 1, "lotr": LOTR_VERSION, "mechanism": MECHANISM, "state": "estimated",
                "key": "%s:%d:%s" % (MECHANISM, time.time_ns(), secrets.token_hex(4)),
                "session": "", "project": "",
                "actual_eq": int(actual), "counterfactual_eq": int(counterfactual),
                "connections": int(connections), "ops": int(ops), "shown": int(shown),
                "left_out": int(left_out),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with open(ledger_path(), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n")
        return True
    except Exception:                                    # noqa: BLE001 - measuring never breaks a find
        return False
