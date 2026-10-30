#!/usr/bin/env python3
"""One labelled line at session start, carrying an action -- and usually silent.

Reads the newest reading `gt_usage.py` recorded. It computes nothing: a SessionStart
hook runs while the user waits, so this does a file read and a comparison, and gets
out of the way.

Output goes through `gt_settings.emit`, which sends `systemMessage` to the user and
`additionalContext` to the model. A hook's plain stdout reaches the model only, so a
check that reports that way looks identical to one that never ran -- silence. That
is the failure the emit contract exists to close.

When it does speak (near a ceiling), and gt-farm has recorded packets in the last 7
days, one more line says how many tokens farming kept off Claude and names
/gt-farm:gt-farm (0.1.10). It never makes the brief speak on its own: farming is
worth mentioning to someone near a limit, and noise to everyone else.
"""

import json
import os
import sys
import time

BASE = os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "usage")
LEDGER = os.path.join(BASE, "readings.jsonl")
LATEST = os.path.join(BASE, "latest.json")     # the newest reading, written by gt_usage.py (0.1.8)

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
    try:                                                     # the newest reading, without a ledger scan
        with open(LATEST) as fh:
            row = json.load(fh)
        if isinstance(row, dict) and any(row.get(k) is not None for k in ("five_hour", "seven_day", "spend")):
            return row
    except Exception:                                        # noqa: BLE001
        pass
    try:
        with open(LEDGER, "rb") as fh:                       # fallback: the ledger's tail
            try:
                fh.seek(-512 * 1024, os.SEEK_END)
                fh.readline()
            except OSError:
                fh.seek(0)
            rows = []
            for t in fh.read().decode("utf-8", "replace").splitlines():
                try:
                    rows.append(json.loads(t))
                except ValueError:
                    continue
    except Exception:                                        # noqa: BLE001
        return None
    usable = [r for r in rows if isinstance(r, dict)
              and any(r.get(k) is not None for k in ("five_hour", "seven_day", "spend"))]
    return usable[-1] if usable else None


def compact():
    """gt's `startup_output` (0.21.4): compact unless it is `full`. Read through gt_settings, so the
    default lives in one place; no gt_settings (or an older one without the setting) means the
    long form this module printed before."""
    if gt_settings is None:
        return False
    try:
        return gt_settings.get("startup_output") == "compact"
    except Exception:                                        # noqa: BLE001
        return False


def describe(row, short=False):
    """The line, or "" for silence. Silence is the normal outcome.

    `short` (startup_output compact, 0.21.4): the percentages on one line and nothing else --
    no tip, no gt-farm line, no 'Quiet this'. Those are dropped from the default output rather
    than shown once a day: a daily repeat is still a line read once and then skipped, and both
    stay one setting away (`startup_output full`) and in /gt-usage:gt-usage."""
    levels = gt_usage.ALERTS if gt_usage else {"normal": (80.0, 75.0, 75.0, 85.0)}
    level = setting("usage_alert", "normal")
    loud_5h, loud_7d, loud_spend, _ = levels.get(level, (80.0, 75.0, 75.0, 85.0))
    # The session-start line speaks earlier than the status line: it is seen once,
    # not on every render, so it can afford to be useful sooner.
    loud_5h, loud_7d, loud_spend = loud_5h * 0.6, loud_7d * 0.55, loud_spend * 0.6

    # Whether this line is SHOWN and what it ADVISES are different questions. Under
    # `always` every threshold above is negative, so the silence check never fires --
    # correct, that is what `always` means. But the advice below must still be earned
    # from real numbers, or the line claims "the weekly window is the one to watch" at
    # 13%. That is the 0.1.1 bug, in the file the 0.1.1 fix did not reach.
    advise_5h, advise_7d, _s, _c = levels.get("normal", (80.0, 75.0, 75.0, 85.0))
    advise_5h, advise_7d = advise_5h * 0.6, advise_7d * 0.55

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
    if short:
        bits = []
        if five is not None:
            bits.append("%.0f%% 5-hour" % five)
        if seven is not None:
            bits.append("%.0f%% weekly" % seven)
        if spend is not None:
            bits.append("%.0f%% monthly spend" % spend)
        return "Claude usage: %s (%s)." % (", ".join(bits), when)

    tail = ""
    if (seven or 0) >= advise_7d:
        tail = " The weekly window is the one to watch."
    elif (five or 0) >= advise_5h:
        tail = " The 5-hour window resets sooner than the weekly one."

    return ("Claude usage: %s used, measured %s.%s\n"
            "What helps most: close a thread when it is finished rather than carrying "
            "it across a break. /gt-usage:gt-usage shows what a cut would save now.\n"
            "%sQuiet this: /gt:gt-settings usage_meter status"
            % (" and ".join(parts), when, tail, farm_line()))


def farm_line():
    """When the brief speaks anyway (near a ceiling), one line on what gt-farm kept off Claude
    in the last 7 days, ending in a newline; "" when there is nothing farmed (0.1.10). Read from
    the ledger's tail only: this runs while the user waits. Never a percent of an allowance."""
    try:
        import gt_savings
        g = gt_savings.farm_glance(7, tail_bytes=512 * 1024)
    except Exception:                                        # noqa: BLE001
        return ""
    if not g:
        return ""
    return ("gt-farm kept %s tokens off Claude in 7 days (%d packet%s): /gt-farm:gt-farm\n"
            % ("{:,}".format(g["tokens"]), g["packets"], "" if g["packets"] == 1 else "s"))


def main(argv):
    as_hook = None
    if gt_settings is not None:
        try:
            _, as_hook = gt_settings.hook_args(argv)
        except Exception:                                    # noqa: BLE001
            as_hook = "--hook" in argv
    else:
        as_hook = "--hook" in argv
    # Drain the hook's JSON only when run AS the hook: Claude Code closes stdin after writing
    # it. Run by hand, or by a parent that keeps an inherited pipe open (the parallel test
    # runner), an unconditional read() waited for an EOF that never came -- two full-suite
    # runs on 2026-10-11 lost 120 s each to it and failed.
    if as_hook:
        try:
            sys.stdin.read()
        except Exception:                                    # noqa: BLE001
            pass
    if not shows():
        return 0
    row = newest()
    text = describe(row, short=bool(as_hook) and compact()) if row else ""
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
