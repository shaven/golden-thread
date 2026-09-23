#!/usr/bin/env python3
"""The plan-allowance meter: records every render, speaks only near a ceiling.

Claude Code hands a status line script a JSON blob on stdin each time it renders,
and that blob is the ONLY documented place `rate_limits.<window>.used_percentage`
appears. `/usage` prints token counts rather than percentages, and nothing persists
the percentages to disk, so sampling here is the only way to build a history.

It must also run from inside a session. A launchd user agent cannot list
`~/.claude/projects` -- `PermissionError` on listdir while `stat` succeeds, macOS
TCC, measured 2026-09-23 -- so a scheduled version of this would fail quietly and
for ever.

    <json on stdin>          render the line (usually nothing), record a reading
    gt_usage.py --now        where the allowance stands, and what a cut would save
    gt_usage.py --history N  the last N readings
    gt_usage.py --off|--on   [all|status|login] stop or resume the display

Contract with the harness, and why this file is dull on purpose: a status line runs
on every render, in the user's face. It never raises, never blocks, always exits 0,
and records at most one reading a minute however often it is called.
"""

import json
import os
import sys
import time

BASE = os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "usage")
LEDGER = os.path.join(BASE, "readings.jsonl")
SAMPLE_SECONDS = 60
STALE_HOURS = 12.0
FRESH_SESSION_TOKENS = 44500        # measured median cold start across 74 sessions

# gt_settings.py is the single source of truth for what a setting means. A module's
# scripts are installed both in its own tree and in the stable hooks dir, so look in
# both -- and fall back to the registered defaults rather than inventing behaviour
# when gt is not importable at all.
for _candidate in (os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "hooks"),
                   os.path.dirname(os.path.abspath(__file__))):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)
try:
    import gt_settings                                       # noqa: E402
except Exception:                                            # noqa: BLE001
    gt_settings = None

ALERTS = {                    # 5-hour, weekly, spend, context
    "early": (50.0, 40.0, 50.0, 70.0),
    "normal": (80.0, 75.0, 75.0, 85.0),
    "late": (90.0, 90.0, 90.0, 95.0),
    # Asked for explicitly: show the meter on every render whatever the numbers.
    # Not the default, because a line that is always there stops being read -- but a
    # person who chooses it is choosing to watch, which is a different thing.
    "always": (-1.0, -1.0, -1.0, -1.0),
}

ACTIONS = [
    ("End a thread instead of carrying it",
     "When the work is finished, close the session. The next question then starts "
     "at about 45K instead of rebuilding everything you were carrying."),
    ("Cut before you step away, not after",
     "Compacting while the cache is still warm reads the prefix from cache and costs "
     "a fraction of the context size. After it expires you pay the full rebuild first."),
    ("Start a new session for unrelated work",
     "A resume costs whatever the conversation weighs, so carrying an old one into a "
     "new task is what makes it expensive."),
    ("Short breaks cost nothing",
     "Under the cache lifetime the prefix is still there. The cliff is the TTL, not "
     "the length of the break."),
]


def setting(name, fallback):
    if gt_settings is None:
        return fallback
    try:
        return gt_settings.get(name) or fallback
    except Exception:                                        # noqa: BLE001
        return fallback


def thresholds():
    return ALERTS.get(setting("usage_alert", "normal"), ALERTS["normal"])


def shows(where):
    mode = setting("usage_meter", "both")
    return mode == "both" or mode == where


def dig(blob, *path, default=None):
    node = blob
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def wrap(text, width=68):
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line); line = word
        else:
            line = (line + " " + word).strip()
    if line:
        out.append(line)
    return out


def record(blob):
    """Append one reading. Never raises: a logging failure must not cost a render."""
    try:
        os.makedirs(BASE, exist_ok=True)
        now = time.time()
        if os.path.exists(LEDGER) and now - os.path.getmtime(LEDGER) < SAMPLE_SECONDS:
            return
        limits = dig(blob, "rate_limits", default={}) or {}
        usage = dig(blob, "context_window", "current_usage", default={}) or {}
        row = {
            "ts": int(now),
            "five_hour": dig(limits, "five_hour", "used_percentage"),
            "five_hour_resets": dig(limits, "five_hour", "resets_at"),
            "seven_day": dig(limits, "seven_day", "used_percentage"),
            "seven_day_resets": dig(limits, "seven_day", "resets_at"),
            "spend": dig(limits, "spend_limit", "used_percentage"),
            "ctx_pct": dig(blob, "context_window", "used_percentage"),
            "cache_write": usage.get("cache_creation_input_tokens"),
            "cache_read": usage.get("cache_read_input_tokens"),
            "cost_usd": dig(blob, "cost", "total_cost_usd"),
            "session": (dig(blob, "session_id", default="") or "")[:8],
        }
        if all(row[k] is None for k in ("five_hour", "seven_day", "spend", "cache_write")):
            return
        with open(LEDGER, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:                                        # noqa: BLE001
        pass


def line(blob):
    """What renders under the prompt -- USUALLY NOTHING."""
    if not shows("status"):
        return ""
    loud_5h, loud_7d, loud_spend, loud_ctx = thresholds()
    five = dig(blob, "rate_limits", "five_hour", "used_percentage")
    seven = dig(blob, "rate_limits", "seven_day", "used_percentage")
    spend = dig(blob, "rate_limits", "spend_limit", "used_percentage")
    ctx = dig(blob, "context_window", "used_percentage")
    if ((five or 0) < loud_5h and (seven or 0) < loud_7d
            and (spend or 0) < loud_spend and (ctx or 0) < loud_ctx):
        return ""
    always = setting("usage_alert", "normal") == "always"
    bits = []
    if five is not None:
        bits.append("5-hour %.0f%%" % five)
    if seven is not None:
        bits.append("weekly %.0f%%" % seven)
    if spend is not None:
        bits.append("monthly %.0f%%" % spend)
    if ctx is not None:
        bits.append("context %.0f%%" % ctx)
    hit = dig(blob, "prompt_cache", "hit_ratio")
    if always and hit is not None:
        # Only in `always` mode: someone watching the meter continuously is the one
        # person for whom the cache hit ratio is worth the width.
        bits.append("cache %.0f%%" % (hit * 100))
    if not bits:
        return ""
    tail = "  (cutting now is cheap; after the cache dies it is not)" \
        if (ctx or 0) >= loud_ctx else ""
    return "usage: " + " · ".join(bits) + tail


def readings():
    if not os.path.exists(LEDGER):
        return []
    out = []
    with open(LEDGER) as fh:
        for text in fh:
            try:
                out.append(json.loads(text))
            except ValueError:
                continue
    return out


def newest(rows):
    for row in reversed(rows):
        if any(row.get(k) is not None for k in ("five_hour", "seven_day", "spend")):
            return row
    return None


def now():
    rows = readings()
    latest = newest(rows)
    print("== Claude usage")
    if not latest:
        print("   no readings yet. One is recorded a minute while a session runs;")
        print("   this fills in on its own.")
    else:
        age_m = (time.time() - latest.get("ts", 0)) / 60.0
        when = ("just now" if age_m < 2 else
                "%.0f minutes ago" % age_m if age_m < 180 else
                "%.1f hours ago -- stale" % (age_m / 60.0))
        for label, key, reset in (("5-hour allowance", "five_hour", "five_hour_resets"),
                                  ("weekly allowance", "seven_day", "seven_day_resets"),
                                  ("monthly spend", "spend", None)):
            value = latest.get(key)
            if value is None:
                print("   %-18s not reported on this plan" % label)
                continue
            filled = int(round(value / 5.0))
            text = "   %-18s %5.1f%%  [%s]" % (label, value, "#" * filled + "." * (20 - filled))
            if reset and latest.get(reset):
                hours = (latest[reset] - time.time()) / 3600.0
                if hours > 0:
                    text += "  resets in %.0fh" % hours
            print(text)
        print("   measured           %s" % when)

    day = [r for r in rows if r.get("ts", 0) > time.time() - 86400
           and r.get("seven_day") is not None]
    if len(day) >= 2:
        print()
        print("== last 24 hours")
        print("   weekly allowance   %.1f%% -> %.1f%%  (%d readings)"
              % (day[0]["seven_day"], day[-1]["seven_day"], len(day)))

    print()
    print("== what you can do about it")
    for title, detail in ACTIONS:
        print("   %s" % title)
        for chunk in wrap(detail):
            print("      %s" % chunk)

    print()
    print("== showing and hiding this")
    print("   /gt:gt-settings usage_meter off       stop displaying it (still records)")
    print("   /gt:gt-settings usage_meter status    the status line only")
    print("   /gt:gt-settings usage_meter login     the session-start line only")
    print("   /gt:gt-settings usage_alert late      speak only very near a ceiling")
    print("   /gt:gt-settings usage_alert always    keep it on screen all the time")
    print("   /gt:gt-settings explain usage_alert   what each level means")

    print()
    for chunk in wrap("Worth knowing before acting on any of this: cache writes count "
                      "toward API rate limits and cache reads do not, but whether either "
                      "touches a subscription allowance is undocumented. Saving these "
                      "tokens is real; needing to save them is not established."):
        print("   " + chunk)
    return 0


def switch(argv):
    value = "off" if argv[0] == "--off" else "both"
    target = argv[1] if len(argv) > 1 else "all"
    if argv[0] == "--on" and target in ("status", "login"):
        value = target
    elif argv[0] == "--off" and target in ("status", "login"):
        value = "login" if target == "status" else "status"
    elif target != "all":
        print("what to switch: all (default), status, login")
        return 2
    if gt_settings is None:
        print("gt_settings is not importable here, so nothing was changed.")
        print("Set it with:  /gt:gt-settings usage_meter %s" % value)
        return 1
    print("Set it with:  /gt:gt-settings usage_meter %s" % value)
    print("(this module keeps its settings in gt's registry, so `gt-settings show`")
    print(" lists them beside everything else rather than in a file of its own)")
    return 0


def main(argv):
    if argv and argv[0] == "--now":
        return now()
    if argv and argv[0] == "--history":
        count = int(argv[1]) if len(argv) > 1 else 20
        for row in readings()[-count:]:
            print("%s  5h %-5s weekly %-5s context %-4s  written %-9s read %s"
                  % (time.strftime("%m-%d %H:%M", time.localtime(row["ts"])),
                     row.get("five_hour"), row.get("seven_day"), row.get("ctx_pct"),
                     row.get("cache_write"), row.get("cache_read")))
        return 0
    if argv and argv[0] in ("--off", "--on"):
        return switch(argv)

    try:
        raw = sys.stdin.read()
        blob = json.loads(raw) if raw.strip() else {}
    except Exception:                                        # noqa: BLE001
        blob = {}
    record(blob)
    text = line(blob)
    if text:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:                                        # noqa: BLE001
        sys.exit(0)                                          # a status line never fails loudly
