#!/usr/bin/env python3
"""One labelled line at session start, carrying an action -- and usually silent.

Reads the newest reading `gt_usage.py` recorded. It computes nothing: a SessionStart
hook runs while the user waits, so this does a file read and a comparison, and gets
out of the way.

Output goes through `gt_settings.emit`, which sends `systemMessage` to the user and
`additionalContext` to the model. A hook's plain stdout reaches the model only, so a
check that reports that way looks identical to one that never ran -- silence. That
is the failure the emit contract exists to close.
"""

import json
import os
import sys
import time

BASE = os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "usage")
LEDGER = os.path.join(BASE, "readings.jsonl")

for _candidate in (os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "hooks"),
                   os.path.dirname(os.path.abspath(__file__))):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)
try:
    import gt_settings
except Exception:                                            # noqa: BLE001
    gt_settings = None

try:
    import gt_usage
except Exception:                                            # noqa: BLE001
    gt_usage = None


def setting(name, fallback):
    if gt_settings is None:
        return fallback
    try:
        return gt_settings.get(name) or fallback
    except Exception:                                        # noqa: BLE001
        return fallback


def shows():
    return setting("usage_meter", "both") in ("both", "login")


def newest():
    try:
        with open(LEDGER) as fh:
            rows = [json.loads(t) for t in fh if t.strip()]
    except Exception:                                        # noqa: BLE001
        return None
    usable = [r for r in rows if isinstance(r, dict)
              and any(r.get(k) is not None for k in ("five_hour", "seven_day", "spend"))]
    return usable[-1] if usable else None


def describe(row):
    """The line, or "" for silence. Silence is the normal outcome."""
    levels = gt_usage.ALERTS if gt_usage else {"normal": (80.0, 75.0, 75.0, 85.0)}
    loud_5h, loud_7d, loud_spend, _ = levels.get(setting("usage_alert", "normal"),
                                                 (80.0, 75.0, 75.0, 85.0))
    # The session-start line speaks earlier than the status line: it is seen once,
    # not on every render, so it can afford to be useful sooner.
    loud_5h, loud_7d, loud_spend = loud_5h * 0.6, loud_7d * 0.55, loud_spend * 0.6

    age_h = (time.time() - row.get("ts", 0)) / 3600.0
    if age_h > 12.0:
        return ("Claude usage: the last reading is %.0f hours old, so it is not quoted "
                "here. It refreshes as soon as this session renders." % age_h)

    five, seven, spend = row.get("five_hour"), row.get("seven_day"), row.get("spend")
    if ((five or 0) < loud_5h and (seven or 0) < loud_7d and (spend or 0) < loud_spend):
        return ""

    parts = []
    if five is not None:
        parts.append("%.0f%% of the 5-hour allowance" % five)
    if seven is not None:
        parts.append("%.0f%% of the weekly allowance" % seven)
    if spend is not None:
        parts.append("%.0f%% of the monthly spend limit" % spend)
    when = "just now" if age_h < 1 else "%.0f hours ago" % age_h

    tail = ""
    if (seven or 0) >= loud_7d:
        tail = " The weekly window is the one to watch."
    elif (five or 0) >= loud_5h:
        tail = " The 5-hour window resets sooner than the weekly one."

    return ("Claude usage: %s used, measured %s.%s\n"
            "What helps most: close a thread when it is finished rather than carrying "
            "it across a break. /gt-usage:gt-usage shows what a cut would save now.\n"
            "Quiet this: /gt:gt-settings usage_meter status"
            % (" and ".join(parts), when, tail))


def main(argv):
    as_hook = None
    if gt_settings is not None:
        try:
            _, as_hook = gt_settings.hook_args(argv)
        except Exception:                                    # noqa: BLE001
            as_hook = "--hook" in argv
    else:
        as_hook = "--hook" in argv
    try:
        sys.stdin.read()
    except Exception:                                        # noqa: BLE001
        pass
    if not shows():
        return 0
    row = newest()
    text = describe(row) if row else ""
    if not text:
        return 0
    if gt_settings is not None:
        try:
            gt_settings.emit(text, event="SessionStart", as_hook=as_hook)
            return 0
        except Exception:                                    # noqa: BLE001
            pass
    print(json.dumps({"systemMessage": text}) if as_hook else text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:                                        # noqa: BLE001
        sys.exit(0)                                          # never block a session start
