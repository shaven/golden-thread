#!/usr/bin/env python3
"""Orphaned background workers: find them at session start, and say which are real.

## The incident

2026-08-29. Ten shell processes spawned by Claude Code were still alive across
three sessions -- the oldest at **10 days 23 hours**. Every one was a `find` over
`~` or a cloud-synced folder. Nobody had noticed, because nothing ever looked, and
they survive the session that started them: they are reparented on exit, not
reaped.

## Why elapsed time is not the test, and a written note is not either

The obvious design says a worker is legitimate if a previous session wrote down
that it started one. That is not enough. All ten of those processes would have
passed a documentation check. What actually separated them from real work was:

    elapsed 10d 23h   ->   CPU consumed: 0.01 seconds

A worker that has burned no CPU is not slow, it is stalled, whatever its note
claims. So the declaration is not an exemption from the liveness test -- and a
DECLARED worker that is idle is the more urgent case, not the less, because
someone was told that work was happening and it is not.

    declared + consuming CPU   ->  banner. Already approved; do not re-ask.
    declared + idle            ->  ALERT. Promised work is not happening.
    undeclared                 ->  ALERT. Nobody knows what this is.

## Declaring a worker

    gt_workers.py declare <pid> "why this is running"

Writes to `~/.claude/golden-thread/workers.jsonl` -- machine-local by design,
since a pid means nothing on another host. Entries whose pid is gone are pruned on
every check, so the file drains itself.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time

REGISTRY = os.path.expanduser("~/.claude/golden-thread/workers.jsonl")
# A worker that has consumed less than this much CPU is treated as stalled no
# matter how long it has been alive. Today's ten orphans were all under 0.05s;
# anything doing genuine work crosses a second quickly.
CPU_FLOOR_SECONDS = 2.0
# Below this age, do not judge -- a worker that started moments ago has not had
# time to accumulate CPU, and flagging it would make the check cry wolf.
MIN_AGE_SECONDS = 300


def _cpu_seconds(t):
    """'MM:SS.ss' or 'HH:MM:SS' -> float seconds."""
    try:
        parts = [float(x) for x in t.strip().split(":")]
    except Exception:
        return 0.0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] if parts else 0.0


def _elapsed_seconds(e):
    """ps etime: [[DD-]HH:]MM:SS -> float seconds."""
    e = e.strip()
    days = 0
    if "-" in e:
        d, e = e.split("-", 1)
        try:
            days = int(d)
        except Exception:
            days = 0
    parts = [p for p in e.split(":")]
    try:
        nums = [float(p) for p in parts]
    except Exception:
        return 0.0
    while len(nums) < 3:
        nums.insert(0, 0.0)
    return days * 86400 + nums[0] * 3600 + nums[1] * 60 + nums[2]


def workers():
    """Shells Claude Code spawned that are still alive.

    Identified by the shell-snapshot path Claude Code sources into every command;
    that is what distinguishes a spawned WORKER from the `claude` session process
    itself, which must never be touched.
    """
    out = []
    try:
        ps = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,etime=,time=,args="],
            capture_output=True, text=True, timeout=20)
    except Exception:
        return out

    rows, kids, cpu_of = {}, {}, {}
    for line in ps.stdout.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(.*)", line)
        if not m:
            continue
        pid, ppid, et, ct, args = m.groups()
        pid, ppid = int(pid), int(ppid)
        rows[pid] = (ppid, et, ct, args)
        cpu_of[pid] = _cpu_seconds(ct)
        kids.setdefault(ppid, []).append(pid)

    def subtree_cpu(pid, seen=None):
        """CPU of a worker AND everything it spawned.

        Measuring only the wrapper is wrong and was caught by testing a worker
        that genuinely burned CPU: the shell reported 0.02s because the work ran
        in its CHILD python. Every real worker would have classified as stalled,
        and `reap` kills stalled workers -- so this bug would have made the
        cleanup tool terminate live work. The wrapper is a launcher; the work is
        underneath it.
        """
        if seen is None:
            seen = set()
        if pid in seen:
            return 0.0
        seen.add(pid)
        total = cpu_of.get(pid, 0.0)
        for k in kids.get(pid, []):
            total += subtree_cpu(k, seen)
        return total

    for pid, (ppid, et, ct, args) in rows.items():
        if "claude/shell-snapshots" not in args:
            continue
        cmd = args
        mm = re.search(r"eval '(.*?)' < /dev/null", args, re.S)
        if mm:
            cmd = mm.group(1)
        out.append({"pid": pid, "ppid": ppid,
                    "elapsed": _elapsed_seconds(et), "elapsed_raw": et.strip(),
                    "cpu": subtree_cpu(pid), "cmd": cmd.strip()})
    return out


def _load():
    rows = {}
    if not os.path.exists(REGISTRY):
        return rows
    try:
        with open(REGISTRY) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("host") == socket.gethostname() and r.get("pid"):
                    rows[int(r["pid"])] = r
    except Exception:
        pass
    return rows


def declare(pid, why):
    os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
    rec = {"pid": int(pid), "host": socket.gethostname(), "why": why,
           "declared": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "session": os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:8]}
    with open(REGISTRY, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def prune(alive_pids):
    """Drop declarations whose process is gone, so the file drains itself."""
    rows = _load()
    keep = [r for p, r in rows.items() if p in alive_pids]
    try:
        with open(REGISTRY, "w") as fh:
            for r in keep:
                fh.write(json.dumps(r) + "\n")
    except Exception:
        pass


def classify(w, declared):
    if w["elapsed"] < MIN_AGE_SECONDS:
        return "young"
    working = w["cpu"] >= CPU_FLOOR_SECONDS
    if w["pid"] in declared:
        return "active" if working else "declared-stalled"
    return "undeclared-working" if working else "undeclared-stalled"


BANNER = "*" * 99


def _clean_line(ws, buckets):
    """What a clean check says out loud.

    Silence was the original design, on the reasoning that a check with nothing
    to report should not add noise. That is wrong for a SessionStart hook: a
    check that says nothing when clean is indistinguishable from a check that is
    not installed, or is installed and crashed. A whole session was spent
    establishing that this hook had in fact run. One line is cheaper than that.
    """
    if not ws:
        return "GT workers: clean — no background workers alive."
    bits = []
    if buckets.get("active"):
        bits.append("%d active" % len(buckets["active"]))
    if buckets.get("young"):
        bits.append("%d too new to judge" % len(buckets["young"]))
    return ("GT workers: clean — %d alive, none orphaned (%s)."
            % (len(ws), ", ".join(bits) or "all accounted for"))


def report():
    ws = workers()
    declared = _load()
    prune({w["pid"] for w in ws})

    buckets = {}
    for w in ws:
        buckets.setdefault(classify(w, declared), []).append(w)

    # Declared and genuinely working: information, never a question. The user
    # already asked for this; re-confirming it every session is noise.
    for w in buckets.get("active", []):
        why = declared.get(w["pid"], {}).get("why", "")
        print(BANNER)
        print("****  ACTIVE CLAUDE WORKER: %s" % (w["cmd"][:70]))
        print("****  pid %-7d running %-12s cpu %.1fs   %s"
              % (w["pid"], w["elapsed_raw"], w["cpu"], why))
        print(BANNER)

    alerts = (buckets.get("declared-stalled", []) +
              buckets.get("undeclared-stalled", []) +
              buckets.get("undeclared-working", []))
    if not alerts:
        print(_clean_line(ws, buckets))
        return 0

    print("CLAUDE WORKERS needing a decision (%d):" % len(alerts))
    for w in buckets.get("declared-stalled", []):
        print("  STALLED, though declared — pid %d, alive %s, only %.2fs CPU"
              % (w["pid"], w["elapsed_raw"], w["cpu"]))
        print("      declared for: %s" % declared.get(w["pid"], {}).get("why", "?"))
        print("      %s" % w["cmd"][:88])
        print("      Work someone was told was happening is NOT happening.")
    for w in buckets.get("undeclared-stalled", []):
        print("  ORPHAN — pid %d, alive %s, only %.2fs CPU (nothing declared it)"
              % (w["pid"], w["elapsed_raw"], w["cpu"]))
        print("      %s" % w["cmd"][:88])
    for w in buckets.get("undeclared-working", []):
        print("  UNDECLARED but ACTIVE — pid %d, alive %s, %.1fs CPU"
              % (w["pid"], w["elapsed_raw"], w["cpu"]))
        print("      %s" % w["cmd"][:88])
        print("      Doing real work, but nothing recorded why. Declare or stop it.")
    print("  reap the stalled ones: python3 %s reap" % os.path.abspath(__file__))
    return len(alerts)


def reap(dry_run=False):
    """Kill STALLED workers only. Never touches one consuming CPU."""
    ws = workers()
    declared = _load()
    killed = []
    for w in ws:
        state = classify(w, declared)
        if state not in ("declared-stalled", "undeclared-stalled"):
            continue
        if dry_run:
            killed.append(w["pid"])
            continue
        try:
            os.kill(w["pid"], 15)
            killed.append(w["pid"])
        except Exception:
            continue
    if not dry_run and killed:
        time.sleep(2)
        for pid in list(killed):
            try:
                os.kill(pid, 0)
                os.kill(pid, 9)
            except Exception:
                pass
    print("%s %d stalled worker(s): %s"
          % ("would reap" if dry_run else "reaped", len(killed),
             ", ".join(str(p) for p in killed) or "none"))
    return 0



def _emit(fn, *a, **kw):
    """Run a reporting function and deliver its output to the user as well as the
    model. See gt_settings.emit -- as a SessionStart hook, plain stdout reaches the
    model only, so a healthy check was invisible to the person it was reassuring.
    Falls back to printing if the settings module cannot be imported, because a
    degraded delivery path must never swallow the report entirely."""
    try:
        import gt_settings
    except Exception:
        return fn(*a, **kw)
    r, text = gt_settings.capture(fn, *a, **kw)
    gt_settings.emit(text)
    return r

def main():
    a = sys.argv[1:]
    cmd = a[0] if a else "check"
    if cmd == "check":
        # Policy comes from the shared registry so the default lives in ONE place.
        pol = "report"
        try:
            import gt_settings
            pol = gt_settings.get("orphan_check") or "report"
        except Exception:
            pass
        if pol == "off":
            return 0

        def _run():
            # reap() prints too, and its output is the most important thing this
            # check ever says -- it is captured with the report, not after it.
            n = report()
            if pol == "reap" and n:
                reap()

        _emit(_run)
        return 0                       # advisory: never fails a session start
    if cmd == "declare" and len(a) > 2:
        r = declare(a[1], " ".join(a[2:]))
        print("declared pid %s: %s" % (r["pid"], r["why"]))
        return 0
    if cmd == "reap":
        return reap("--dry-run" in a)
    if cmd == "list":
        for w in workers():
            print("%-7d %-12s cpu %6.2fs  %s"
                  % (w["pid"], w["elapsed_raw"], w["cpu"], w["cmd"][:70]))
        return 0
    print("usage: gt_workers.py [check | list | declare <pid> <why> | reap [--dry-run]]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
