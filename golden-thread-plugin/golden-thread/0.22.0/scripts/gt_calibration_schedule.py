#!/usr/bin/env python3
"""gt_calibration_schedule.py -- when gt re-tests its model choices by itself (0.21.0, item 44).

    gt_calibration_schedule.py check --hook   SessionStart: when a scheduled pass is due, start it once
    gt_calibration_schedule.py check          the same, from a terminal (prints what it did)
    gt_calibration_schedule.py status         the schedule, the last run and when the next is due

The setting is model_calibration_schedule: off, manual (the default), weekly or monthly. Only weekly
and monthly ever start anything.

THE RULES (owner, 2026-10-09):
  * the next run is counted from the LAST run of ANY kind -- a manual run today moves the next
    automatic one to a week (a month) from today. Every pass that writes a plan records itself in
    ~/.claude/golden-thread/model-calibration/last-run.json ({"at": epoch, "kind": manual|scheduled});
  * a run missed because the machine was off happens ONCE, at the next session start, never as a
    backlog: the catch-up run is itself the new last run, and the count starts again from it;
  * with no last run at all a schedule is due at once;
  * the claim is taken and the run recorded BEFORE the pass is started, so two sessions starting
    together fire one pass, and a pass that is then refused (the allowance line, a budget) is not
    retried on every session start.

The pass is `gt_model_calibrate.py report --scheduled`, started detached; it keeps every guard the
manual pass has (budget, weekly allowance line, the preset's cap) and its output goes to
model-calibration/scheduled.log. Nothing here calls a model.

A hook: it prints nothing unless it started a pass, fails open on any error, and never blocks a
session. Standard library only.
"""
import datetime
import json
import os
import subprocess
import sys
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_SCHEDULE = "manual"
SCHEDULES = ("off", "manual", "weekly", "monthly")
WEEK = 7 * 86400
CLAIM_STALE_S = 120


def home():
    return os.path.expanduser("~")


def state_dir():
    return os.path.join(home(), ".claude", "golden-thread", "model-calibration")


def last_path():
    return os.path.join(state_dir(), "last-run.json")


def claim_path():
    from pathlib import Path
    return Path(state_dir()) / "schedule.claim"


def _cfg():
    try:
        with open(os.path.join(home(), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                                    # noqa: BLE001
        return {}


def schedule():
    v = _cfg().get("model_calibration_schedule")
    v = v.strip().lower() if isinstance(v, str) else ""
    return v if v in SCHEDULES else DEFAULT_SCHEDULE


def read_last():
    """-> {"at": epoch seconds, "kind": str} or None."""
    try:
        with open(last_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get("at"), (int, float)):
            return {"at": float(d["at"]), "kind": str(d.get("kind") or "manual")}
    except Exception:                                    # noqa: BLE001
        pass
    return None


def mark_run(kind, now=None):
    """Record a run (any kind) as the last one. Atomic; never raises."""
    now = time.time() if now is None else now
    try:
        os.makedirs(state_dir(), exist_ok=True)
        tmp = last_path() + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"at": now, "kind": kind}, fh)
        os.replace(tmp, last_path())
    except OSError:
        pass


def _add_months(ts, months):
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    m = dt.month - 1 + months
    y, m = dt.year + m // 12, m % 12 + 1
    for day in (dt.day, 30, 29, 28):
        try:
            return dt.replace(year=y, month=m, day=min(day, dt.day)).timestamp()
        except ValueError:
            continue
    return ts + 30 * 86400


def next_due(sched, last_at, now):
    """-> epoch seconds of the next scheduled run, `now` when a schedule has never run, or None for
    off and manual (nothing is scheduled)."""
    if sched not in ("weekly", "monthly"):
        return None
    if last_at is None:
        return now
    return last_at + WEEK if sched == "weekly" else _add_months(last_at, 1)


def is_due(sched, last_at, now):
    nd = next_due(sched, last_at, now)
    return nd is not None and now >= nd


def _date(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def describe(sched, now=None):
    """One line for gt-settings and the models page."""
    now = time.time() if now is None else now
    last = read_last()
    since = "last run %s, %s" % (_date(last["at"]), last["kind"]) if last else "no run yet"
    if sched == "off":
        return "off -- nothing runs by itself (%s)" % since
    if sched == "manual":
        return "manual -- only when you run it (%s)" % since
    nd = next_due(sched, last["at"] if last else None, now)
    if last is None or nd <= now:
        return "%s: due now, at the next session start (%s)" % (sched, since)
    return "%s: next run due %s (%s)" % (sched, _date(nd), since)


def _calibrate_script():
    own = os.path.join(HERE, "gt_model_calibrate.py")
    if os.path.isfile(own):
        return own
    root = os.path.join(home(), ".claude", "plugins", "cache", "golden-thread-plugin", "gt")
    try:
        vs = sorted((d for d in os.listdir(root) if d.replace(".", "").isdigit()),
                    key=lambda s: tuple(int(x) for x in s.split(".")), reverse=True)
    except OSError:
        vs = []
    for v in vs:
        cand = os.path.join(root, v, "scripts", "gt_model_calibrate.py")
        if os.path.isfile(cand):
            return cand
    return None


def default_launch():
    """Start `gt_model_calibrate.py report --scheduled`, detached, its output in scheduled.log."""
    fake = os.environ.get("GT_CALIBRATION_LAUNCH")        # tests: a stand-in that makes no model call
    if fake:
        argv = [fake, "report", "--scheduled"]
    else:
        script = _calibrate_script()
        if not script:
            return False
        argv = [sys.executable, "-B", script, "report", "--scheduled"]
    os.makedirs(state_dir(), exist_ok=True)
    kw = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200      # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    with open(os.path.join(state_dir(), "scheduled.log"), "ab") as log:
        subprocess.Popen(argv, stdout=log, stderr=log, **kw)
    return True


def _take_claim(now):
    """True when this process holds the claim. A claim older than CLAIM_STALE_S is a crashed one."""
    path = str(claim_path())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
            os.write(fd, str(now).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) < CLAIM_STALE_S:
                    return False
                os.remove(path)
            except OSError:
                return False
    return False


def _drop_claim():
    try:
        os.remove(str(claim_path()))
    except OSError:
        pass


def check(now=None, launch=None):
    """-> {"fired": bool, "schedule": str, "message": str}. Fires at most one pass."""
    now = time.time() if now is None else now
    sched = schedule()
    out = {"fired": False, "schedule": sched, "message": ""}
    if sched not in ("weekly", "monthly"):
        return out
    last = read_last()
    if not is_due(sched, last["at"] if last else None, now):
        return out
    if not _take_claim(now):
        return out
    try:
        last = read_last()                               # another session may have won just before
        if not is_due(sched, last["at"] if last else None, now):
            return out
        mark_run("scheduled", now)                       # the count restarts here, whatever follows
        ok = (launch or default_launch)()
        if ok is False:
            out["message"] = ("Scheduled model calibration (%s) is due, but gt_model_calibrate.py was "
                              "not found, so nothing was started." % sched)
            return out
    finally:
        _drop_claim()
    out["fired"] = True
    out["message"] = ("Scheduled model calibration (%s) was due and a pass has started in the "
                      "background. It keeps the budget and the weekly allowance line, and it counts as "
                      "the last run. Results: /gt:gt-settings models. To stop these: "
                      "/gt:gt-settings model_calibration_schedule manual." % sched)
    return out


def _hook():
    try:
        payload = json.load(sys.stdin)
    except Exception:                                    # noqa: BLE001
        payload = {}
    if isinstance(payload, dict) and payload.get("source") in ("compact", "clear"):
        return 0
    r = check()
    if r["message"]:
        sys.stdout.write(json.dumps({"systemMessage": r["message"]}) + "\n")
    return 0


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd == "check":
        if "--hook" in argv:
            return _hook()
        r = check()
        print(r["message"] or "nothing to do: %s" % describe(r["schedule"]))
        return 0
    if cmd == "status":
        s = schedule()
        print("model_calibration_schedule: %s" % s)
        print("next calibration: %s" % describe(s))
        return 0
    print(__doc__.strip().split("\n\n")[0])
    return 2


if __name__ == "__main__":
    try:
        import gt_integrity_gate as _gig
    except ImportError:
        _gig = None
    if _gig is not None and "--hook" in sys.argv:
        _gig.check(__file__, "reporter", when=True)
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:                                    # noqa: BLE001
        sys.exit(0 if "--hook" in sys.argv else 1)
