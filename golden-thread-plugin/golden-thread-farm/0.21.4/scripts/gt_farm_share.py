#!/usr/bin/env python3
"""gt_farm_share.py -- share routing and the budget guard for farmed agent tasks (no network).

Decides, for each eligible task, whether it goes to the farm engine or stays with Claude, so that a
chosen SHARE of a task class is farmed, the share grows only as results prove out, and spending or
failures pause it. It only decides and keeps score; it never sends anything itself.

THE TABLE (farm-share.tsv, tab-separated, one row per task class; `<vault>/farm-share.tsv` wins over
the module's template):

    task_class  share_target  ramp_step  ramp_window  audit_rate  max_usd_day  max_fail_rate  pause_after  engine

  share_target   percent (0-100) of eligible tasks to farm at the end of the ramp
  ramp_step      percent added after each clean window (0 = fixed: the share is the target from the start)
  ramp_window    tasks in a window (a window is clean when its fail rate is under max_fail_rate and no
                 audit in it failed)
  audit_rate     share of farmed results a blind Claude audit re-checks (- = the farm_audit_rate setting)
  max_usd_day    spend cap per day for the class (0 = none)
  max_fail_rate  0-1; routing pauses when the failures over the last window pass it
  pause_after    consecutive failures that pause routing
  engine         optional pinned engine name (- = any)
A class the table does not name uses its `default` row; with no `default` row it is not farmed.

DETERMINISTIC. A task goes to the farm when sha256(class + task_key) lands below the current share, on a
0-100 scale. The same key always gets the same answer, and over many keys the farmed share matches the
share. Only classes the calibration marks can-take are considered; anything else, an unknown class, the
setting `farm_share` being off, or a pause, means Claude (fail closed, and the reason is said in words).

STATE (per machine, ~/.claude/golden-thread/farm/share-state.json, or $GT_FARM_SHARE_STATE): per class the
effective share, counters, consecutive failures, spend today and any pause with its reason. Written
atomically. A pause ends at midnight (budget), after a cooldown (failures), or on `resume`.

Settings: farm_share (off|on, GT_FARM_SHARE overrides), farm_agent_budget_usd_day (off or dollars per day
across all classes, GT_FARM_AGENT_BUDGET_USD_DAY overrides).

Usage:
    gt_farm_share.py status                  # one table: class, target, effective, spend, state
    gt_farm_share.py explain CLASS [--key K] # what would happen to the next task of this class
    gt_farm_share.py pause [CLASS] [--reason TEXT]
    gt_farm_share.py resume [CLASS]          # clears pauses and failure counters
    gt_farm_share.py check-table [FILE]      # parse the table, name each problem
    options: --vault DIR  --table FILE  --state FILE  --dry-run (pause/resume: say, do not write)

Exit codes:
    0  ok
    1  the table has a problem (check-table), or nothing to do
    2  usage error
"""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import time

GT_VERSION = "0.21.4"

COLUMNS = ("task_class", "share_target", "ramp_step", "ramp_window", "audit_rate", "max_usd_day",
           "max_fail_rate", "pause_after", "engine")
COOLDOWN_HOURS = 6
DEFAULT_AUDIT_RATE = 0.2
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "templates", "farm-share.tsv")


# ---- settings ---------------------------------------------------------------------------

def _vault_config():
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def setting(name, default):
    """$GT_<NAME>, else the key in ~/.claude/vault-config.json, else the default (as gt_farm_send.setting)."""
    env = os.environ.get("GT_" + name.upper())
    if env and env.strip():
        return env.strip()
    v = _vault_config().get(name)
    return str(v).strip() if v not in (None, "") else default


def share_on():
    return setting("farm_share", "off").lower() == "on"


def global_budget():
    """Dollars per day across all classes, or None for no cap."""
    v = setting("farm_agent_budget_usd_day", "off").lower()
    if v in ("off", "", "0", "-", "none"):
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except ValueError:
        return None


def default_audit_rate():
    try:
        r = float(setting("farm_audit_rate", str(DEFAULT_AUDIT_RATE)))
        return r if 0.0 <= r <= 1.0 else DEFAULT_AUDIT_RATE
    except ValueError:
        return DEFAULT_AUDIT_RATE


# ---- the table --------------------------------------------------------------------------

def _num(text, lo, hi, what, n, cls, integer=False):
    try:
        v = int(text) if integer else float(text)
    except (TypeError, ValueError):
        raise ValueError("line %d (%s): %s must be a number from %s to %s, got %r"
                         % (n, cls, what, lo, hi, text))
    if not lo <= v <= hi:
        raise ValueError("line %d (%s): %s must be from %s to %s, got %r" % (n, cls, what, lo, hi, text))
    return v


def parse_table(text):
    """-> {task_class: row}. Raises ValueError naming every bad line and what to fix."""
    rows, problems, header = {}, [], None
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = [c.strip() for c in line.split("\t")]
        if header is None:
            header = [re.sub(r"[\s-]+", "_", c.lower()) for c in cells]
            missing = [c for c in COLUMNS[:-1] if c not in header]
            if missing:
                problems.append("line %d: the header row is missing column(s) %s; columns are separated by "
                                "tabs and must be: %s" % (n, ", ".join(missing), ", ".join(COLUMNS)))
                break
            continue
        d = dict(zip(header, cells))
        cls = d.get("task_class", "").lower()
        if not cls:
            problems.append("line %d: no task_class in the first column" % n)
            continue
        if cls in rows:
            problems.append("line %d (%s): the class appears twice; keep one row" % (n, cls))
            continue
        try:
            row = {"task_class": cls,
                   "share_target": _num(d.get("share_target", ""), 0, 100, "share_target (percent)", n, cls),
                   "ramp_step": _num(d.get("ramp_step", ""), 0, 100, "ramp_step (percent, 0 = fixed)", n, cls),
                   "ramp_window": _num(d.get("ramp_window", ""), 1, 100000, "ramp_window (tasks)", n, cls, True),
                   "max_usd_day": _num(d.get("max_usd_day", ""), 0, 1e9, "max_usd_day (dollars, 0 = none)", n, cls),
                   "max_fail_rate": _num(d.get("max_fail_rate", ""), 0, 1, "max_fail_rate (0 to 1)", n, cls),
                   "pause_after": _num(d.get("pause_after", ""), 1, 100000, "pause_after (failures in a row)",
                                       n, cls, True)}
            ar = d.get("audit_rate", "")
            row["audit_rate"] = None if ar in ("", "-") else _num(ar, 0, 1, "audit_rate (0 to 1, or -)", n, cls)
        except ValueError as e:
            problems.append(str(e))
            continue
        eng = d.get("engine", "")
        row["engine"] = None if eng in ("", "-") else eng
        rows[cls] = row
    if header is None and not problems:
        problems.append("the table has no header row; first line must be: " + "\t".join(COLUMNS))
    if problems:
        raise ValueError("farm-share table: " + "; ".join(problems))
    return rows


def load_share_table(path):
    """-> {task_class: row}. Raises ValueError (plain words, with the line) or OSError-turned-ValueError."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        raise ValueError("farm-share table %s cannot be read (%s); check the path" % (path, e.strerror or e))
    return parse_table(text)


def find_table(explicit=None, vault=None):
    if explicit:
        return os.path.expanduser(explicit)
    if vault and os.path.isfile(os.path.join(vault, "farm-share.tsv")):
        return os.path.join(vault, "farm-share.tsv")
    return os.path.normpath(TEMPLATE)


def class_row(table, cls):
    key = (cls or "").strip().lower()
    if key in table:
        return table[key]
    d = table.get("default")
    return dict(d, task_class=key) if d else None


# ---- state ------------------------------------------------------------------------------

def state_path():
    p = os.environ.get("GT_FARM_SHARE_STATE")
    if p:
        return os.path.expanduser(p)
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "farm", "share-state.json")


class StateUnreadable(Exception):
    """The share state file exists but cannot be read or parsed. Raised by load_state(strict=True). `path` is the
    file, `kept` the copy made of it (or None). Never treated as an empty state: that would wipe pauses, a manual
    stop and the day's spend, so every guard would fail open."""

    def __init__(self, path, reason="", kept=None):
        self.path, self.reason, self.kept = path, reason, kept
        Exception.__init__(self, "the share state file cannot be read (%s), so nothing is farmed until it is "
                                 "fixed: run gt_farm_share.py status" % path)


class StateLockTimeout(RuntimeError):
    """The state lock stayed held by another process for the whole wait. Fail closed: nothing was changed."""


LOCK_WAIT_S = 30.0


def _keep_corrupt(path):
    """Copy an unreadable state file to <name>.corrupt-<timestamp> (once per distinct content). -> the copy or None."""
    import glob
    import shutil
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    for old in sorted(glob.glob(glob.escape(path) + ".corrupt-*")):
        try:
            with open(old, "rb") as fh:
                if fh.read() == data:
                    return old
        except OSError:
            continue
    base = path + ".corrupt-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest, n = base, 1
    while os.path.exists(dest):
        n += 1
        dest = "%s-%d" % (base, n)
    try:
        shutil.copy2(path, dest)
    except OSError:
        return None
    return dest


def load_state(path=None, strict=False, keep=True):
    """-> the state dict. A MISSING file is a normal empty state. A file that exists but cannot be read or parsed
    is copied to <name>.corrupt-<timestamp> and then: strict=True raises StateUnreadable (use it wherever the
    answer decides whether to farm); strict=False returns an empty state (status and repair tools). keep=False
    skips the copy, for read-only callers that must write nothing."""
    p = path or state_path()
    try:
        with open(p, encoding="utf-8") as fh:
            s = json.load(fh)
        if isinstance(s, dict) and isinstance(s.get("classes"), dict):
            return s
        reason = "it is not a share state file"
    except FileNotFoundError:
        return {"v": 1, "classes": {}}
    except (OSError, ValueError) as e:
        reason = getattr(e, "strerror", None) or "%s" % type(e).__name__
    kept = _keep_corrupt(p) if keep else None
    if strict:
        raise StateUnreadable(p, reason, kept)
    return {"v": 1, "classes": {}}


def _lock_try(fh):
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _lock_release(fh):
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def state_lock(path=None, timeout=None):
    """Cross-process lock around a read-modify-write of the state file: `with state_lock(path): ...`. The lock
    file is <state file>.lock (fcntl.flock on POSIX, msvcrt.locking on Windows); it is released if the process
    dies. Waits up to `timeout` seconds (default LOCK_WAIT_S, 30), then raises StateLockTimeout. Not re-entrant:
    do not nest it for one file."""
    p = path or state_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    fh = open(p + ".lock", "a+b")
    try:
        if os.name == "nt":
            fh.seek(0, 2)
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()
        wait = LOCK_WAIT_S if timeout is None else float(timeout)
        deadline = time.monotonic() + wait
        while True:
            try:
                _lock_try(fh)
                break
            except (OSError, IOError):
                if time.monotonic() >= deadline:
                    raise StateLockTimeout("the share state is in use by another farm run (waited %d s for %s), "
                                           "so nothing was changed; try again in a minute" % (wait, p + ".lock"))
                time.sleep(0.02)
        try:
            yield
        finally:
            _lock_release(fh)
    finally:
        fh.close()


def update_state(fn, path=None, strict=True):
    """Under the lock: reload the state, apply fn(state) (it changes the dict in place), save atomically.
    -> the new state. Raises StateUnreadable (strict) or StateLockTimeout; nothing is written then."""
    p = path or state_path()
    with state_lock(p):
        st = load_state(p, strict=strict)
        fn(st)
        save_state(st, p)
    return st


def reserve_spend(state, task_class, usd, table, now=None):
    """Add `usd` (a run's cost ceiling) to the class's spend today so concurrent runs see it. -> the day it was
    booked on (pass it to release_spend)."""
    now = _now(now)
    cls = (task_class or "").strip().lower()
    cs = class_state(state, cls, class_row(table, cls))
    if cs.get("spend_day") != _day(now):
        cs["spend_day"], cs["spend_today"] = _day(now), 0.0
    cs["spend_today"] = round(cs["spend_today"] + max(0.0, float(usd or 0)), 6)
    return _day(now)


def release_spend(state, task_class, usd, day):
    """Take a reservation made by reserve_spend back out (only if the spend day has not rolled over since)."""
    cs = state.get("classes", {}).get((task_class or "").strip().lower())
    if cs and cs.get("spend_day") == day:
        cs["spend_today"] = round(max(0.0, cs["spend_today"] - max(0.0, float(usd or 0))), 6)


def save_state(state, path=None):
    """Atomic: write a temp file beside the target, then replace."""
    path = path or state_path()
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".share-state.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now(now):
    return now if now is not None else datetime.datetime.now()


def _day(now):
    return now.strftime("%Y-%m-%d")


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse(s):
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None


def _next_midnight(now):
    return datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time())


def class_state(state, cls, row=None):
    """The class's counters, created on first use. The share starts at ramp_step (or the target if fixed)."""
    cs = state.setdefault("classes", {}).get(cls)
    if cs is None:
        eff = 0.0
        if row:
            eff = row["share_target"] if row["ramp_step"] <= 0 else min(row["share_target"], row["ramp_step"])
        cs = {"effective": float(eff), "tasks": 0, "ok": 0, "failed": 0, "window_tasks": 0,
              "window_failed": 0, "window_audit_failed": 0, "recent": [], "consecutive_failures": 0,
              "spend_day": "", "spend_today": 0.0, "paused_until": None, "paused_reason": "",
              "paused_manual": False}
        state["classes"][cls] = cs
    return cs


def _spend_today(cs, now):
    return cs["spend_today"] if cs.get("spend_day") == _day(now) else 0.0


def _total_spend_today(state, now):
    return sum(_spend_today(c, now) for c in state.get("classes", {}).values())


def active_pause(cs, now):
    """-> reason text if the class is paused at `now`, else None. Pure: never changes the state."""
    if cs.get("paused_manual"):
        return cs.get("paused_reason") or "paused: stopped by you (run `resume` to start again)"
    until = _parse(cs.get("paused_until"))
    if until and now < until:
        return "%s (until %s)" % (cs.get("paused_reason") or "paused", until.strftime("%Y-%m-%d %H:%M"))
    return None


def budget_pause(state, cs, row, now):
    """-> reason text if a spending cap is reached today, else None."""
    if row and row["max_usd_day"] > 0 and _spend_today(cs, now) >= row["max_usd_day"]:
        return "paused: daily budget reached ($%.2f of $%.2f for this class; resets at midnight)" \
               % (_spend_today(cs, now), row["max_usd_day"])
    g = global_budget()
    if g is not None and _total_spend_today(state, now) >= g:
        return "paused: daily budget reached ($%.2f of the $%.2f all-class limit; resets at midnight)" \
               % (_total_spend_today(state, now), g)
    return None


def _expire(cs, now):
    """Clear a finished timed pause and the counters that caused it, so it does not re-trip at once."""
    until = _parse(cs.get("paused_until"))
    if until and now >= until and not cs.get("paused_manual"):
        cs.update(paused_until=None, paused_reason="", consecutive_failures=0, recent=[],
                  window_tasks=0, window_failed=0, window_audit_failed=0)


# ---- deciding ---------------------------------------------------------------------------

def bucket(task_class, task_key):
    """A stable position 0 <= b < 100 for this task: same class and key, same answer, any machine."""
    h = hashlib.sha256(("%s\0%s" % ((task_class or "").lower(), task_key)).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") % 10000 / 100.0


def _audit_pick(task_class, task_key, rate):
    h = hashlib.sha256(("audit\0%s\0%s" % ((task_class or "").lower(), task_key)).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") % 10000 / 10000.0 < rate


def _verdict(calibrated, cls):
    if calibrated is None:
        return None
    try:
        v = calibrated.get(cls) if isinstance(calibrated, dict) else calibrated(cls)
    except Exception:
        return None
    if isinstance(v, dict):
        v = v.get("verdict")
    return v


def gate(task_class, state, table, now=None, calibrated=None, enabled=None):
    """-> (blocked_reason or None, effective share percent, row). The task-independent part of decide()."""
    now = _now(now)
    cls = (task_class or "").strip().lower()
    if not (share_on() if enabled is None else enabled):
        return "farm_share is off, so every task stays with Claude", 0.0, None
    row = class_row(table, cls)
    if row is None:
        return "no share row for class '%s' (and no default row), so it stays with Claude" % cls, 0.0, None
    verdict = _verdict(calibrated, cls)
    if verdict != "can-take":
        return ("calibration does not mark '%s' as can-take (%s), so it stays with Claude"
                % (cls, verdict or "not calibrated")), 0.0, row
    cs = state.get("classes", {}).get(cls) or class_state({"classes": {}}, cls, row)
    reason = active_pause(cs, now) or budget_pause(state, cs, row, now)
    share = min(float(cs["effective"]), float(row["share_target"]))
    if reason:
        return (reason if reason.startswith("paused") else "paused: " + reason), share, row
    return None, share, row


def decide(task_class, task_key, state, table, now=None, calibrated=None, enabled=None):
    """-> {route: 'farm'|'claude', why: sentence, audit: bool, share: effective percent, engine: name|None}.

    Fail closed: Claude unless farm_share is on, the class has a row, the calibration says can-take,
    nothing is paused and the task's position falls under the current share. Does not change `state`."""
    cls = (task_class or "").strip().lower()
    blocked, share, row = gate(cls, state, table, now, calibrated, enabled)
    if blocked:
        return {"route": "claude", "why": blocked, "audit": False, "share": share, "engine": None}
    if bucket(cls, task_key) >= share:
        return {"route": "claude", "audit": False, "share": share, "engine": None,
                "why": "this task falls outside the current %.0f%% share (target %.0f%%), so Claude does it"
                       % (share, row["share_target"])}
    rate = row["audit_rate"] if row["audit_rate"] is not None else default_audit_rate()
    return {"route": "farm", "share": share, "engine": row["engine"], "audit": _audit_pick(cls, task_key, rate),
            "why": "this task falls inside the current %.0f%% share (target %.0f%%)" % (share, row["share_target"])}


def _as_now(today):
    """A date, a datetime, an ISO date/time string or None -> a datetime."""
    if today is None or isinstance(today, datetime.datetime):
        return _now(today)
    if isinstance(today, datetime.date):
        return datetime.datetime(today.year, today.month, today.day, 12, 0)
    return _parse(str(today)) or datetime.datetime.strptime(str(today)[:10], "%Y-%m-%d").replace(hour=12)


def select(table, task_class, task_key, today=None, state=None, calibrated=None, enabled=None):
    """Read-only selection for callers that only need the answer (the opencode export, dashboards).

    -> {route: 'farm'|'claude', engine: name|None, share: effective percent, audit_rate: configured rate,
        audit: this task is picked for a blind Claude audit, paused: bool, paused_reason: str,
        budget_left: dollars left today (the smaller of the class cap and the all-class cap) or None, why: sentence}.
    The same rules as decide(): fail closed unless farm_share is on, the class has a row and `calibrated`
    says can-take. It records no spend, changes no ramp state and does not write the state file.
    `engine` is the row's pinned engine, or None when the row pins none (the caller then picks one)."""
    import copy
    now = _as_now(today)
    cls = (task_class or "").strip().lower()
    try:
        st = copy.deepcopy(state if state is not None else load_state(strict=True, keep=False))
    except StateUnreadable as ex:
        return {"route": "claude", "engine": None, "share": 0.0, "audit_rate": default_audit_rate(), "audit": False,
                "paused": True, "paused_reason": str(ex), "budget_left": None, "why": str(ex)}
    d = decide(cls, task_key, st, table, now=now, calibrated=calibrated, enabled=enabled)
    row = class_row(table, cls)
    cs = st.get("classes", {}).get(cls)
    paused = ""
    budget_left = None
    if row is not None:
        cs = cs or class_state({"classes": {}}, cls, row)
        paused = active_pause(cs, now) or budget_pause(st, cs, row, now) or ""
        left = []
        if row["max_usd_day"] > 0:
            left.append(max(0.0, row["max_usd_day"] - _spend_today(cs, now)))
        g = global_budget()
        if g is not None:
            left.append(max(0.0, g - _total_spend_today(st, now)))
        budget_left = round(min(left), 6) if left else None
    rate = row["audit_rate"] if row is not None and row["audit_rate"] is not None else default_audit_rate()
    return {"route": d["route"], "engine": d["engine"], "share": d["share"], "audit_rate": rate, "audit": d["audit"],
            "paused": bool(paused), "paused_reason": paused, "budget_left": budget_left, "why": d["why"]}


# ---- outcomes ---------------------------------------------------------------------------

def _pause(cs, until, reason):
    cs["paused_until"] = _iso(until)
    cs["paused_reason"] = reason


def record_outcome(state, task_class, ok, usd=0.0, audited_ok=None, table=None, now=None):
    """Score one farmed task and update the share and the guards. `audited_ok` is None when the result was
    not audited. Raises ValueError without a table. Returns the class's counters."""
    if table is None:
        raise ValueError("record_outcome needs the share table to know the limits")
    now = _now(now)
    cls = (task_class or "").strip().lower()
    row = class_row(table, cls)
    if row is None:
        raise ValueError("no share row for class '%s'" % cls)
    cs = class_state(state, cls, row)
    _expire(cs, now)
    if cs.get("spend_day") != _day(now):
        cs["spend_day"], cs["spend_today"] = _day(now), 0.0
    cs["spend_today"] = round(cs["spend_today"] + max(0.0, float(usd or 0)), 6)
    cs["tasks"] += 1
    cs["window_tasks"] += 1
    cs["recent"] = (cs["recent"] + [0 if ok else 1])[-row["ramp_window"]:]
    if ok:
        cs["ok"] += 1
        cs["consecutive_failures"] = 0
    else:
        cs["failed"] += 1
        cs["window_failed"] += 1
        cs["consecutive_failures"] += 1
    step = row["ramp_step"]
    if audited_ok is False:
        cs["window_audit_failed"] += 1
        drop = step if step > 0 else row["share_target"] / 2.0
        cs["effective"] = max(0.0, cs["effective"] - drop)
        cs["window_tasks"] = cs["window_failed"] = cs["window_audit_failed"] = 0   # start a fresh window lower down
    cooldown = now + datetime.timedelta(hours=COOLDOWN_HOURS)
    if not active_pause(cs, now):
        if cs["consecutive_failures"] >= row["pause_after"]:
            _pause(cs, cooldown, "paused: %d failures in a row" % cs["consecutive_failures"])
        elif len(cs["recent"]) >= row["ramp_window"] and sum(cs["recent"]) / float(len(cs["recent"])) > row["max_fail_rate"]:
            _pause(cs, cooldown, "paused: fail rate %.0f%% over the last %d tasks is above the %.0f%% limit"
                   % (100.0 * sum(cs["recent"]) / len(cs["recent"]), len(cs["recent"]), 100.0 * row["max_fail_rate"]))
        elif budget_pause(state, cs, row, now):
            _pause(cs, _next_midnight(now), budget_pause(state, cs, row, now))
    if cs["window_tasks"] >= row["ramp_window"]:
        clean = (cs["window_failed"] / float(cs["window_tasks"]) <= row["max_fail_rate"]
                 and cs["window_audit_failed"] == 0)
        if clean and step > 0 and not active_pause(cs, now):
            cs["effective"] = min(float(row["share_target"]), cs["effective"] + step)
        cs["window_tasks"] = cs["window_failed"] = cs["window_audit_failed"] = 0
    return cs


def set_pause(state, cls, reason, manual=True, table=None):
    cs = class_state(state, cls, class_row(table, cls) if table else None)
    cs["paused_manual"] = manual
    cs["paused_reason"] = reason
    return cs


def reset_class(state, cls, table=None):
    cs = class_state(state, cls, class_row(table, cls) if table else None)
    cs.update(paused_until=None, paused_reason="", paused_manual=False, consecutive_failures=0, recent=[],
              window_tasks=0, window_failed=0, window_audit_failed=0)
    return cs


# ---- reports ----------------------------------------------------------------------------

def status_text(state, table, now=None, calibrated=None):
    now = _now(now)
    head = "%-12s %6s %9s %8s  %s" % ("class", "target", "effective", "spent", "state")
    lines = ["Farm share routing is %s." % ("ON" if share_on() else "OFF (every task stays with Claude)"),
             "", head, "-" * len(head)]
    for cls in sorted(table):
        row = table[cls]
        cs = state.get("classes", {}).get(cls) or class_state({"classes": {}}, cls, row)
        why = active_pause(cs, now) or budget_pause(state, cs, row, now)
        verdict = _verdict(calibrated, cls) if calibrated is not None else None
        if why:
            st = why
        elif calibrated is not None and verdict != "can-take":
            st = "claude only: not calibrated can-take"
        else:
            st = "routing"
        lines.append("%-12.12s %5.0f%% %8.0f%% %8s  %s" % (cls, row["share_target"], cs["effective"],
                                                           "$%.2f" % _spend_today(cs, now), st))
    g = global_budget()
    lines += ["", "Spent today, all classes: $%.2f%s." % (_total_spend_today(state, now),
              " of a $%.2f limit" % g if g else " (no all-class limit)")]
    return "\n".join(lines)


def explain_text(cls, state, table, now=None, calibrated=None, key=None):
    now = _now(now)
    cls = cls.strip().lower()
    row = class_row(table, cls)
    cs = state.get("classes", {}).get(cls)
    lines = ["Class '%s'." % cls]
    if row:
        lines.append("Target share %.0f%%, ramp +%.0f%% per %d clean tasks, spend cap %s a day, pauses after %d "
                     "failures in a row or a fail rate above %.0f%%."
                     % (row["share_target"], row["ramp_step"], row["ramp_window"],
                        "$%.2f" % row["max_usd_day"] if row["max_usd_day"] else "none", row["pause_after"],
                        100 * row["max_fail_rate"]))
    if cs:
        lines.append("So far: %d tasks, %d ok, %d failed; share now %.0f%%." % (cs["tasks"], cs["ok"], cs["failed"], cs["effective"]))
    if key is None:
        blocked, share, _ = gate(cls, state, table, now, calibrated)
        if blocked:
            lines.append("The next task goes to Claude: %s." % blocked)
        else:
            lines.append("The next task goes to the farm if its key falls in the lowest %.0f%% of keys; "
                         "otherwise to Claude." % share)
    else:
        d = decide(cls, key, state, table, now, calibrated)
        lines.append("Task key '%s' goes to %s: %s." % (key, "the farm engine" if d["route"] == "farm" else "Claude", d["why"]))
    return "\n".join(lines)


# ---- command line -----------------------------------------------------------------------

def _calibrated_from_disk(vault):
    """Best effort: the calibration module's per-class verdicts, else nothing (fail closed to Claude)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import gt_farm_calibration as C
        fn = getattr(C, "class_verdicts", None)
        if fn:
            m = fn(vault)
            return lambda c: m.get(c)
    except Exception:
        pass
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_farm_share.py", description="Share routing and the budget guard.")
    ap.add_argument("--vault")
    ap.add_argument("--table")
    ap.add_argument("--state")
    ap.add_argument("--dry-run", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status")
    e = sub.add_parser("explain")
    e.add_argument("task_class")
    e.add_argument("--key")
    p = sub.add_parser("pause")
    p.add_argument("task_class", nargs="?")
    p.add_argument("--reason", default="")
    r = sub.add_parser("resume")
    r.add_argument("task_class", nargs="?")
    c = sub.add_parser("check-table")
    c.add_argument("file", nargs="?")
    try:
        a = ap.parse_args(argv)
    except SystemExit as ex:
        return 2 if ex.code else 0
    if not a.cmd:
        ap.print_usage(sys.stderr)
        return 2
    vault = os.path.abspath(os.path.expanduser(a.vault or os.environ.get("GT_VAULT") or _vault_config().get("vault_path") or "")) \
        if (a.vault or os.environ.get("GT_VAULT") or _vault_config().get("vault_path")) else None
    path = find_table(getattr(a, "file", None) or a.table, vault)
    try:
        table = load_share_table(path)
    except ValueError as ex:
        print(str(ex))
        return 1
    if a.cmd == "check-table":
        print("%s: %d class(es) ok: %s" % (path, len(table), ", ".join(sorted(table))))
        return 0
    sp = a.state or state_path()
    cal = _calibrated_from_disk(vault)
    if a.cmd in ("status", "explain"):
        try:
            state = load_state(sp, strict=True)
        except StateUnreadable as ex:
            print("%s\nA copy of the unreadable file is kept at %s. To start from a clean state, run "
                  "`gt_farm_share.py resume` (this forgets pauses and today's spend, so use it only when none is "
                  "wanted)." % (ex, ex.kept or "(it could not be copied)"))
            return 1
        if a.cmd == "status":
            print(status_text(state, table, calibrated=cal))
        else:
            print(explain_text(a.task_class, state, table, calibrated=cal, key=a.key))
        return 0
    classes = [a.task_class.strip().lower()] if a.task_class else sorted(table)

    def apply(state):
        for cls in classes:
            if a.cmd == "pause":
                set_pause(state, cls, "paused: " + (a.reason or "stopped by you; run `resume` to start again"), table=table)
                print("%s %s: paused." % ("Would pause" if a.dry_run else "Paused", cls))
            else:
                reset_class(state, cls, table=table)
                print("%s %s: pause and failure counters cleared." % ("Would resume" if a.dry_run else "Resumed", cls))
    if a.dry_run:
        apply(load_state(sp))
        return 0
    try:
        update_state(apply, sp, strict=False)
    except StateLockTimeout as ex:
        print(str(ex))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
