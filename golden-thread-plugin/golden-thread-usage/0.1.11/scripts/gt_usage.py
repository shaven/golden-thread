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
    gt_usage.py --now        where the allowance stands, and what a cut would save; since
                             0.1.10 also the work gt-farm kept off Claude in the last 7 days
                             (only when the savings ledger holds farm lines)
    gt_usage.py --history N  the last N readings
    gt_usage.py --context [DAYS]
                             (0.1.10) where OUTPUT tokens went over the last DAYS (default 7) of
                             local transcripts: share by model and by the context each reply
                             carried (<32k, 32-128k, 128-400k, >400k). Streams every
                             ~/.claude/projects/**/*.jsonl touched in the window, so it is its
                             own command, never on the status-line path; `--now` shows the
                             same section from a cache under a day old, else scans and caches
    gt_usage.py --off|--on   [all|status|login] stop or resume the display
    gt_usage.py --setup-statusline [--check]
                             add the status-line entry to ~/.claude/settings.json, only when
                             there is none (an existing statusLine is the user's and is kept);
                             --check says what it would do and writes nothing (0.1.7)

Contract with the harness, and why this file is dull on purpose: a status line runs
on every render, in the user's face. It never raises, never blocks, always exits 0,
and records at most one reading a minute however often it is called.
"""

import datetime
import json
import os
import re
import sys
import time

BASE = os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "usage")
LEDGER = os.path.join(BASE, "readings.jsonl")
GLANCE = os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "farm", "usage-glance.json")
LATEST = os.path.join(BASE, "latest.json")     # the newest reading alone (0.1.8): readers use this, not the ledger
TAIL_BYTES = 512 * 1024                        # the most of the ledger a reader scans
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

# Whether the meter is SHOWN and what it ADVISES are different questions. In `always`
# mode every threshold is -1 so the line renders unconditionally -- but the advice
# attached to it must still be earned, or "cutting now is cheap" appears at 10% of a
# context window, where there is nothing to cut and the sentence is simply false.
CONTEXT_HINT_AT = ALERTS["normal"][3]

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
        write_latest(row)
    except Exception:                                        # noqa: BLE001
        pass


def write_latest(row):
    """The newest reading, whole, to latest.json (atomic replace). The pane footer, the allowance
    note, the session-start line and `--now` read this instead of scanning the ever-growing ledger.
    Never raises: a failure here must cost neither the ledger line nor the render."""
    tmp = LATEST + ".%d.tmp" % os.getpid()
    try:
        with open(tmp, "w") as fh:
            fh.write(json.dumps(row) + "\n")
        os.replace(tmp, LATEST)
    except Exception:                                        # noqa: BLE001
        try:
            os.remove(tmp)
        except OSError:
            pass


def latest_row():
    """The row in latest.json, or None when it is missing, unreadable or has no plan figure."""
    try:
        with open(LATEST) as fh:
            row = json.load(fh)
    except Exception:                                        # noqa: BLE001
        return None
    if isinstance(row, dict) and any(row.get(k) is not None for k in ("five_hour", "seven_day", "spend")):
        return row
    return None


def line(blob):
    """What renders under the prompt -- USUALLY NOTHING. The allowance text, then (0.1.11) the farm segment."""
    if not shows("status"):
        return ""
    parts = [p for p in (allowance_line(blob), farm_segment()) if p]
    return " | ".join(parts)


FARM_LOUD = 0.8                 # a hosted engine speaks at this share of its monthly budget
FARM_IDLE_DAYS = 30             # an engine unused this long is not mentioned
FARM_429_HOURS = 1              # a 429 warns for this long


def _utc(ts):
    try:
        return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n and abs(n) != float("inf") else None


def farm_segment(now=None):
    """The hosted-engine segment, or "". Reads ONLY the small glance file gt_farm_send.py rewrites after each
    packet (never the ledger: a render must not pay for it). Quiet unless an engine is at 80% of its budget or
    was answered with a 429 in the last hour; `usage_external on` or `usage_alert always` show every engine used
    in the last 30 days. Names and numbers only; every dollar figure is an estimate."""
    mode = setting("usage_external", "auto")
    if mode == "off":
        return ""
    try:
        with open(GLANCE, encoding="utf-8") as fh:
            data = json.load(fh)
        engines = data["engines"]
        if not isinstance(engines, dict):
            return ""
    except Exception:                                        # noqa: BLE001
        return ""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    this_month = now.strftime("%Y-%m")
    everything = mode == "on" or setting("usage_alert", "normal") == "always"
    out = []
    provs = data.get("providers") if isinstance(data.get("providers"), dict) else {}
    # an engine's own entry, then each provider's (the sum over its engines, for a provider budget)
    entries = [(n, engines[n], n) for n in sorted(engines)] + [("provider " + n, provs[n], n) for n in sorted(provs)]
    for label, g, name in entries:
        if not isinstance(g, dict) or not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", str(name)):
            continue
        used = _utc(g.get("last_used"))
        hit = _utc(g.get("last_429"))
        recent = [t for t in (used, hit) if t and (now - t).days < FARM_IDLE_DAYS]
        if not recent:
            continue
        spent = _num(g.get("month_usd")) if data.get("month") == this_month else None
        budget = _num(g.get("budget_usd"))
        bits = []
        if budget and budget > 0 and spent is not None and (everything or spent >= FARM_LOUD * budget):
            bits.append("$%.2f of $%g (estimated)" % (spent, budget))
        if hit and (now - hit).total_seconds() < FARM_429_HOURS * 3600:
            bits.append("429 at %s" % hit.astimezone().strftime("%H:%M"))
        if everything and not bits and spent is not None:
            bits.append("$%.2f (estimated)" % spent)
        calls = g.get("calls_today")
        if everything and bits and isinstance(calls, int) and not isinstance(calls, bool) and calls > 0:
            bits.append("%d call%s today" % (calls, "" if calls == 1 else "s"))
        if bits:
            out.append("%s %s" % (label, " · ".join(bits)))
    return "farm: " + "; ".join(out) if out else ""


def allowance_line(blob):
    """The plan-allowance text, "" while every number is under its threshold."""
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
        if (ctx or 0) >= CONTEXT_HINT_AT else ""
    return "usage: " + " · ".join(bits) + tail


def readings():
    """The ledger's rows -- its tail only (TAIL_BYTES): it grows without bound, and every reader
    wants recent rows."""
    if not os.path.exists(LEDGER):
        return []
    out = []
    with open(LEDGER, "rb") as fh:
        try:
            fh.seek(-TAIL_BYTES, os.SEEK_END)
            fh.readline()                                    # drop the partial first line
        except OSError:
            fh.seek(0)
        for text in fh.read().decode("utf-8", "replace").splitlines():
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


def farm_glance(days=7, tail_bytes=None):
    """gt_savings.farm_glance from beside this file or the hooks dir, or None. Imported only
    here, never on the status-line path: a render must not pay for it."""
    try:
        import gt_savings                                    # noqa: E402
        return gt_savings.farm_glance(days, tail_bytes=tail_bytes)
    except Exception:                                        # noqa: BLE001
        return None


def farm_section(days=7):
    """-> the `--now` lines for work gt-farm kept off Claude (0.1.10), or [] when there is none.
    Tokens only: they are never turned into a percent of an allowance (the rule below)."""
    g = farm_glance(days)
    if not g:
        return []
    out = ["", "== work kept off Claude (gt-farm, last %d days)" % days,
           "   %d packet%s, %s tokens, %s on your own engines%s"
           % (g["packets"], "" if g["packets"] == 1 else "s", "{:,}".format(g["tokens"]),
              "-" if g["local_share"] is None else "%.0f%%" % (100 * g["local_share"]),
              "" if not g["estimated"] else " (%d estimated)" % g["estimated"]),
           "   as Claude work: about %s token-equivalents (estimated)" % "{:,}".format(g["farm_eq"])]
    if g["usd_avoided"] is not None:
        out.append("   cost avoided at the reference rates: $%.4f" % g["usd_avoided"])
    out.append("   the breakdown: /gt-usage:gt-usage-savings")
    return out


# -- where output went, by model and by context carried (0.1.10) -----------------------------
CONTEXT_CACHE = os.path.join(BASE, "context-bands.json")
CONTEXT_BANDS = ((32000, "<32k"), (128000, "32-128k"), (400000, "128-400k"), (None, ">400k"))


def context_band(ctx):
    for top, label in CONTEXT_BANDS:
        if top is None or ctx < top:
            return label
    return CONTEXT_BANDS[-1][1]


def context_bands(days=7, root=None, now_ts=None):
    """-> output tokens by model and by context band, from local transcripts.

    Each assistant reply's `usage` gives what it wrote (output_tokens) and what it carried:
    input + cache_read + cache_creation, the whole context sent with it. Claude Code writes one
    line per content block, each repeating the reply's usage, so a reply is counted once (by
    message id, its largest output figure). Lines are streamed and pre-filtered as bytes; a file
    or line that cannot be read or parsed is skipped and counted, never fatal."""
    root = root or os.path.join(os.path.expanduser("~"), ".claude", "projects")
    now_ts = time.time() if now_ts is None else now_ts
    cutoff = now_ts - days * 86400.0
    cut_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cutoff))
    replies, files, skipped = {}, 0, 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.stat(path).st_mtime < cutoff:
                    continue
                fh = open(path, "rb")
            except OSError:
                skipped += 1
                continue
            files += 1
            try:
                with fh:
                    for raw in fh:
                        if b'"output_tokens"' not in raw or b'"assistant"' not in raw:
                            continue
                        try:
                            d = json.loads(raw)
                        except ValueError:
                            skipped += 1
                            continue
                        if not isinstance(d, dict) or d.get("type") != "assistant":
                            continue
                        ts = d.get("timestamp")
                        if isinstance(ts, str) and ts[:19] < cut_iso:
                            continue
                        m = d.get("message")
                        u = m.get("usage") if isinstance(m, dict) else None
                        model = (m.get("model") if isinstance(m, dict) else None) or "?"
                        if not isinstance(u, dict) or model == "<synthetic>":
                            continue
                        try:
                            ctx = sum(int(u.get(k) or 0) for k in (
                                "input_tokens", "cache_read_input_tokens",
                                "cache_creation_input_tokens"))
                            out = int(u.get("output_tokens") or 0)
                        except (TypeError, ValueError):
                            skipped += 1
                            continue
                        key = m.get("id") or d.get("requestId") or d.get("uuid") or (path, len(replies))
                        prev = replies.get(key)
                        if prev is None or out > prev[2]:
                            replies[key] = (model, ctx, out)
            except OSError:
                skipped += 1
    by_model, by_band = {}, {label: 0 for _top, label in CONTEXT_BANDS}
    for model, ctx, out in replies.values():
        by_model[model] = by_model.get(model, 0) + out
        by_band[context_band(ctx)] += out
    return {"ts": int(now_ts), "days": days, "replies": len(replies), "files": files,
            "skipped": skipped, "output": sum(by_model.values()), "by_model": by_model,
            "by_band": by_band}


def _share(part, whole):
    return "-" if not whole else "%.0f%%" % (100.0 * part / whole)


def context_section(r):
    """-> the lines for a context_bands() result."""
    out = ["== where output went (%d days, measured from local transcripts)" % r["days"]]
    if not r["output"]:
        out.append("   no assistant replies with usage in the window")
        return out
    out.append("   %s replies, %s output tokens%s"
               % ("{:,}".format(r["replies"]), "{:,}".format(r["output"]),
                  "" if not r["skipped"] else " (%d unreadable skipped)" % r["skipped"]))
    models = sorted(r["by_model"].items(), key=lambda kv: -kv[1])
    out.append("   by model")
    for model, n in models[:6]:
        out.append("      %-28s %5s" % (model[:28], _share(n, r["output"])))
    if len(models) > 6:
        out.append("      %-28s %5s" % ("(%d others)" % (len(models) - 6),
                                         _share(sum(n for _m, n in models[6:]), r["output"])))
    out.append("   by context carried (input + cache read + cache write per reply)")
    for _top, label in CONTEXT_BANDS:
        out.append("      %-28s %5s" % (label, _share(r["by_band"].get(label, 0), r["output"])))
    return out


CONTEXT_CACHE_HOURS = 24.0


def save_context(r):
    try:
        os.makedirs(BASE, exist_ok=True)
        tmp = CONTEXT_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(r, fh)
        os.replace(tmp, CONTEXT_CACHE)
    except OSError:
        pass


def context_report(days=7):
    r = context_bands(days)
    for text in context_section(r):
        print(text)
    save_context(r)
    return 0


def cached_context_section(days=7):
    """-> the `--now` lines: the last 7-day result when it is under a day old (one small file),
    else a fresh scan (measured 1.7 s over 750 MB of transcripts), cached for the next run.
    Any failure is one pointer line, never an error in the readout."""
    try:
        with open(CONTEXT_CACHE, encoding="utf-8") as fh:
            r = json.load(fh)
        if (not isinstance(r, dict) or r.get("days") != days
                or time.time() - r.get("ts", 0) > CONTEXT_CACHE_HOURS * 3600):
            r = None
    except (OSError, ValueError, TypeError):
        r = None
    try:
        if r is None:
            r = context_bands(days)
            save_context(r)
        lines = context_section(r)
    except Exception:                                        # noqa: BLE001
        return ["", "== where output went: gt_usage.py --context %d measures it" % days]
    age_h = (time.time() - r.get("ts", 0)) / 3600.0
    if age_h >= 1:
        lines[0] = lines[0][:-1] + ", %.0fh ago)" % age_h
    return [""] + lines


def now():
    rows = readings()
    latest = latest_row() or newest(rows)
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

    for text in farm_section():
        print(text)

    for text in cached_context_section():
        print(text)

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


def statusline_command():
    """The command the entry runs: the installed copy, through the interpreter install.sh
    resolved on native Windows (where `python3` in Git Bash is the Microsoft Store stub)."""
    script = os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "hooks",
                          "gt_usage.py")
    py = "python3"
    if os.name == "nt":
        try:
            with open(os.path.join(os.path.expanduser("~"), ".claude", "golden-thread",
                                   "python"), encoding="utf-8") as fh:
                py = fh.readline().strip() or sys.executable
        except OSError:
            py = sys.executable
        py, script = py.replace("\\", "/"), script.replace("\\", "/")
    import shlex
    return " ".join(shlex.quote(x) for x in (py, script))


def setup_statusline(check=False):
    """Add {"statusLine": ...} to user settings when there is none. -> exit code.

    The status line is the user's display, so this never replaces one that exists and is
    never run without being asked (install.sh offers it at a terminal, on a yes)."""
    path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        print("settings.json is unreadable (%s); nothing changed" % exc.__class__.__name__)
        return 1
    if not isinstance(data, dict):
        print("settings.json is not a JSON object; nothing changed")
        return 1
    if data.get("statusLine"):
        print("a status line is already configured (yours); kept: %s"
              % json.dumps(data["statusLine"])[:160])
        return 0
    entry = {"type": "command", "command": statusline_command()}
    if check:
        print("would add to %s: \"statusLine\": %s" % (path, json.dumps(entry)))
        return 0
    data["statusLine"] = entry
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        bdir = os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "backups")
        os.makedirs(bdir, exist_ok=True)
        with open(path, "rb") as a, open(os.path.join(
                bdir, "settings.json.%s.pre-statusline" % time.strftime("%Y%m%d_%H%M%S")),
                "wb") as b:
            b.write(a.read())
    tmp = path + ".gt-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
    print("status line added: %s (restart Claude Code to see it)" % entry["command"])
    return 0


def main(argv):
    if argv and argv[0] == "--setup-statusline":
        return setup_statusline(check="--check" in argv[1:])
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
    if argv and argv[0] in ("--context", "context"):
        try:
            days = int(argv[1]) if len(argv) > 1 else 7
        except ValueError:
            print("usage: gt_usage.py --context [DAYS]")
            return 2
        return context_report(max(days, 1))
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
