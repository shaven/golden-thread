#!/usr/bin/env python3
"""gt_load.py -- size a parallel run from the machine's LOAD, not from its core count.

    gt_load.py [--cap N] [--json]          what a parallel runner would start with, and why

A library first (tests/prun.py imports it); the CLI only explains its answer.

WHY (2026-10-01): the test runner sized its pool from the CPU count while the machine sat at load
average ~50 from processes that were not its own -- the indexer, the sync agents, the virus
scanner reacting to its throwaway files, and another session's suite. Install tests ran 160-315 s
against a normal ~30 s and the owner's keystrokes lagged and were dropped. More workers made every
unit slower; the cores were never the constraint.

TWO DECISIONS, both measured, neither guessed:

  1. START. `recommend(cap)` starts from the ceiling the settings allow (`parallel_profile` /
     `parallel_max`) and keeps it while the machine has fewer runnable processes than cores;
     above that it scales down by the square of the overload:
        over = (load1 - own) / cores;  headroom = 1 if over <= 1 else 1 / over^2  (>= 0.05)
     then halves it under memory pressure (< 15% available), and divides it among the other
     parallel runs already going (other tests/prun.py processes). Never below 1.

  2. BACK OFF. `Governor` watches each finished unit against its own baseline (the previous
     run's time for that unit). When the rolling median of (this run / baseline) exceeds
     BACKOFF_RATIO the pool shrinks by a quarter instead of growing; when it falls back under
     RECOVER_RATIO and load allows, it grows by one, never past the start ceiling.

GT_LOAD_OVERRIDE='{"load1": 50, "cores": 16, "mem_free": 0.5, "others": 0}' replaces the live
readings -- for tests, and for rehearsing what a loaded machine would do.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys

BACKOFF_RATIO = 2.0
RECOVER_RATIO = 1.3
WINDOW = 5
MEM_PRESSURE = 0.15


def _override():
    raw = os.environ.get("GT_LOAD_OVERRIDE")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def mem_free_fraction():
    """Available memory as a fraction of total, or None when it cannot be read."""
    try:
        with open("/proc/meminfo") as fh:
            info = dict(l.split(":", 1) for l in fh if ":" in l)
        total = int(info["MemTotal"].split()[0])
        avail = int(info.get("MemAvailable", info.get("MemFree", "0 kB")).split()[0])
        return avail / total if total else None
    except (OSError, KeyError, ValueError):
        pass
    try:                                      # macOS: percent of memory free, as the kernel sees it
        p = subprocess.run(["sysctl", "-n", "kern.memorystatus_level"], capture_output=True,
                           text=True, timeout=5)
        if p.returncode == 0 and p.stdout.strip().isdigit():
            return int(p.stdout.strip()) / 100.0
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def other_runs(pattern="tests/prun.py"):
    """How many OTHER parallel test runs are going on this machine."""
    try:
        p = subprocess.run(["ps", "-Ao", "pid=,args="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return 0
    me = {os.getpid(), os.getppid()}
    n = 0
    for line in p.stdout.splitlines():
        m = re.match(r"\s*(\d+)\s+(.*)$", line)
        if m and int(m.group(1)) not in me and pattern in m.group(2) and "python" in m.group(2):
            n += 1
    return n


def readings(own=0):
    o = _override()
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        load1 = 0.0
    return {"load1": float(o.get("load1", load1)),
            "cores": int(o.get("cores", os.cpu_count() or 4)),
            "mem_free": o.get("mem_free", mem_free_fraction()),
            "others": int(o.get("others", other_runs())),
            "own": own}


def recommend(cap, own=0, r=None):
    """-> (workers, why). `cap` is the ceiling the settings allow; `own` is how many of the
    load average's runnable processes are this run's own (so a run does not throttle itself)."""
    r = r or readings(own)
    cap = max(1, int(cap))
    why = []
    busy = max(0.0, r["load1"] - r["own"])
    over = busy / max(1, r["cores"])
    # Below one runnable process per core the machine has room: the ceiling stands (this work is
    # mostly waiting on I/O). Above it, scale down by the SQUARE of the overload -- at 3x the
    # cores (the 2026-10-01 Mac: load ~50 on 16) a 16-wide ceiling becomes 2 -- because every
    # extra worker there slows every other process on the machine, the owner's included.
    headroom = 1.0 if over <= 1.0 else max(0.05, 1.0 / (over * over))
    n = cap * headroom
    if headroom < 1.0:
        why.append("load %.1f on %d cores is %.1fx the cores" % (r["load1"], r["cores"], over))
    if r["mem_free"] is not None and r["mem_free"] < MEM_PRESSURE:
        n /= 2
        why.append("memory pressure (%d%% free)" % round(r["mem_free"] * 100))
    if r["others"]:
        n /= (r["others"] + 1)
        why.append("%d other parallel run(s) on this machine" % r["others"])
    n = max(1, min(cap, int(round(n))))
    return n, ("; ".join(why) or "machine idle: the full ceiling")


class Governor:
    """Decides, as units finish, whether the pool may grow or must shrink."""

    def __init__(self, start, cap, baseline=None):
        self.limit = max(1, int(start))
        self.cap = max(self.limit, int(cap))
        self.start = self.limit
        self.baseline = baseline or {}
        self.ratios = []
        self.events = []

    def finished(self, unit, secs):
        base = self.baseline.get(unit)
        if not base or base < 1.0:                  # sub-second units say nothing about load
            return self.limit
        self.ratios.append(secs / base)
        self.ratios = self.ratios[-WINDOW:]
        if len(self.ratios) < 3:
            return self.limit
        med = statistics.median(self.ratios)
        if med > BACKOFF_RATIO and self.limit > 1:
            new = max(1, int(self.limit * 0.75))
            if new == self.limit:
                new -= 1
            self.events.append("back off %d -> %d: units running %.1fx their baseline"
                               % (self.limit, new, med))
            self.limit = new
            self.ratios = []
        elif med < RECOVER_RATIO and self.limit < self.start:
            self.events.append("recover %d -> %d: units back to %.1fx baseline"
                               % (self.limit, self.limit + 1, med))
            self.limit += 1
        return self.limit


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cap", type=int, default=os.cpu_count() or 4,
                    help="the ceiling the settings allow (default: cores)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = readings()
    n, why = recommend(a.cap, r=r)
    if a.json:
        print(json.dumps({"workers": n, "cap": a.cap, "why": why, "readings": r}, indent=2))
    else:
        print("start %d of %d worker(s): %s" % (n, a.cap, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())
