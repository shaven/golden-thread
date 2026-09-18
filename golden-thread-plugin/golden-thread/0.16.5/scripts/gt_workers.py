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
since a pid means nothing on another host. Any pid may be declared, not only a shell
Claude Code spawned, and an entry is pruned on the next check only when its pid is
gone from the process table entirely -- so the file drains itself without ever
dropping a declaration for something still running.
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


def _parse_cpu(t):
    """'MM:SS.ss', 'HH:MM:SS' or '[DD-]HH:MM:SS' (procps) -> float seconds, or None."""
    t = t.strip()
    days = 0
    try:
        if "-" in t:
            d, t = t.split("-", 1)
            days = int(d)
        parts = [float(x) for x in t.split(":")]
    except Exception:
        return None
    if len(parts) == 3:
        return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2 and not days:
        return parts[0] * 60 + parts[1]
    if len(parts) == 1 and not days:
        return parts[0]
    return None


def _cpu_seconds(t):
    """CPU time -> float seconds; 0.0 if it cannot be read."""
    v = _parse_cpu(t)
    return 0.0 if v is None else v


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


_LAST_ROWS = {}


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

    rows, kids, cpu_of, unreadable = {}, {}, {}, set()
    for line in ps.stdout.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(.*)", line)
        if not m:
            continue
        pid, ppid, et, ct, args = m.groups()
        pid, ppid = int(pid), int(ppid)
        rows[pid] = (ppid, et, ct, args)
        cpu_of[pid] = _cpu_seconds(ct)
        if _parse_cpu(ct) is None:
            unreadable.add(pid)
        kids.setdefault(ppid, []).append(pid)
    _LAST_ROWS.clear()
    _LAST_ROWS.update(rows)

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

    def subtree_unreadable(pid, seen=None):
        seen = set() if seen is None else seen
        if pid in seen:
            return False
        seen.add(pid)
        return pid in unreadable or any(subtree_unreadable(k, seen)
                                        for k in kids.get(pid, []))

    def descendants(pid, seen=None):
        seen = set() if seen is None else seen
        for k in kids.get(pid, []):
            if k not in seen:
                seen.add(k)
                descendants(k, seen)
        return seen

    for pid, (ppid, et, ct, args) in rows.items():
        if "claude/shell-snapshots" not in args:
            continue
        cmd = args
        mm = re.search(r"eval '(.*?)' < /dev/null", args, re.S)
        if mm:
            cmd = mm.group(1)
        cmd = cmd.strip()
        owner = owning_session(pid, rows)
        below = descendants(pid)
        waits_on = _waits_on(cmd)
        # Waiting = a poll loop, or a shell whose only live children are `sleep`.
        waiting = bool(waits_on) or (bool(below) and all(
            _prog(rows[k][3]) == "sleep" for k in below if k in rows))
        out.append({"pid": pid, "ppid": ppid,
                    "elapsed": _elapsed_seconds(et), "elapsed_raw": et.strip(),
                    "cpu": subtree_cpu(pid), "cpu_unreadable": subtree_unreadable(pid),
                    "cmd": cmd, "owner": owner,
                    "owner_elapsed_raw": rows[owner][1].strip() if owner else "",
                    "session": _session_of(cmd), "waiting": waiting, "waits_on": waits_on})
    return out


def _prog(args):
    """Basename of the program a ps args string runs (skipping a node/bun launcher)."""
    toks = args.split()
    if not toks:
        return ""
    name = os.path.basename(toks[0])
    if name in ("node", "bun") and len(toks) > 1:
        name = os.path.basename(toks[1])
    return name


def is_claude(args):
    """The Claude Code session process itself -- not one of the shells it spawned."""
    return _prog(args) == "claude" and "claude/shell-snapshots" not in args


def owning_session(pid, rows):
    """Pid of the live `claude` process this worker descends from, or None.

    2026-09-14: the check called two wait loops and a test run ORPHAN / UNDECLARED and
    offered `reap`; all three were shells of another session whose `claude` was alive.
    CPU says whether a worker is busy, not whether anyone owns it. Ownership is the
    parent chain: a shell whose session exited is reparented to launchd (pid 1) and no
    `claude` remains above it -- that, and only that, is an orphan."""
    seen, p = set(), rows.get(pid, (0,))[0]
    while p > 1 and p in rows and p not in seen:
        seen.add(p)
        if is_claude(rows[p][3]):
            return p
        p = rows[p][0]
    return None


def _session_of(cmd):
    """Session id from a Claude scratchpad/tasks path in the command, if one is there."""
    m = re.search(r"/claude-\d+/[^/\s]+/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                  r"[0-9a-f]{12})/", cmd)
    return m.group(1) if m else ""


def _waits_on(cmd):
    """The condition an `until/while ...; do sleep N` loop polls, with simple
    VAR=value assignments expanded, or '' when the command is not a poll loop."""
    m = re.search(r"\b(?:until|while)\s+(.+?)\s*;\s*do\s+sleep\b", cmd)
    if not m:
        return ""
    cond = re.sub(r"\s*2>\s*/dev/null", "", m.group(1))
    for var, val in re.findall(r"(?:^|;|&&)\s*([A-Za-z_]\w*)=([^\s;]+)", cmd[:m.start()]):
        cond = re.sub(r"\$\{?%s\}?(?!\w)" % var, lambda _m, v=val: v, cond)
    return cond.strip()


def my_session_pid():
    """Pid of the `claude` this check itself runs under (from the last `workers()`
    table), or None when it is not found -- then no worker counts as this session's."""
    rows = dict(_LAST_ROWS)
    rows[os.getpid()] = (os.getppid(), "", "", "")
    return owning_session(os.getpid(), rows)


def _mine(ws):
    """The claude pid whose workers are this session's own, or None."""
    me = my_session_pid()
    if me:
        return me
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    owners = {w["owner"] for w in ws if sid and w.get("owner") and w.get("session") == sid}
    return owners.pop() if len(owners) == 1 else None


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


def _alive_pids(ws):
    """Every pid this host is running, as the last `ps` saw it.

    NOT only the Claude-spawned shells. `report()` used to prune against those, while
    `declare <pid>` accepts any pid at all -- so a declaration naming a live python worker, or
    the child of a shell, was deleted on the next check and the alert it was suppressing came
    back with nothing left to explain it (found 2026-09-18). An empty set means `ps` answered
    with nothing, and then nothing is pruned."""
    return set(_LAST_ROWS) or {w["pid"] for w in ws}


def prune(alive_pids):
    """Drop declarations whose process is gone, so the file drains itself.

    Given an empty set this keeps everything: "the process table could not be read" is not
    "the process is gone", and a wrongly dropped declaration is silent."""
    rows = _load()
    if not alive_pids:
        return
    keep = [r for p, r in rows.items() if p in alive_pids]
    try:
        with open(REGISTRY, "w") as fh:
            for r in keep:
                fh.write(json.dumps(r) + "\n")
    except Exception:
        pass


def classify(w, declared, mine=None):
    if w["elapsed"] < MIN_AGE_SECONDS:
        return "young"
    # A CPU time ps printed in a form we cannot read is not evidence of idleness,
    # and `reap` kills stalled workers -- so it counts as working.
    working = w["cpu"] >= CPU_FLOOR_SECONDS or w.get("cpu_unreadable", False)
    owner = w.get("owner")
    if owner:
        # Its session is alive: never an orphan, never reaped. Another session's
        # workers are that session's business -- reported, not put to this user.
        if mine is None or owner != mine:
            return "live-session"
        if w.get("waiting") and not working:
            return "waiting"
        if w["pid"] in declared:
            return "active" if working else "own-idle"
        return "undeclared-working" if working else "own-idle"
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
    if buckets.get("waiting"):
        bits.append("%d waiting" % len(buckets["waiting"]))
    if buckets.get("live-session"):
        bits.append("%d belong to other live sessions" % len(buckets["live-session"]))
    if buckets.get("young"):
        bits.append("%d too new to judge" % len(buckets["young"]))
    return ("GT workers: clean — %d alive, none orphaned (%s)."
            % (len(ws), ", ".join(bits) or "all accounted for"))


def _sessions_lines(buckets):
    """One line per other live session, then one per worker: what it is doing."""
    lines, by_owner = [], {}
    for w in buckets.get("live-session", []) + buckets.get("waiting", []):
        by_owner.setdefault(w["owner"], []).append(w)
    for owner, group in sorted(by_owner.items()):
        sid = next((w["session"] for w in group if w.get("session")), "")
        lines.append("  session %s(claude pid %d, up %s): %d worker(s), ok"
                     % (sid[:8] + " " if sid else "", owner,
                        group[0]["owner_elapsed_raw"], len(group)))
        for w in sorted(group, key=lambda x: x["pid"]):
            if w.get("waiting"):
                what = "WAITING on: %s" % (w["waits_on"] or "sleep")
            else:
                what = "running, %.1fs CPU: %s" % (w["cpu"], w["cmd"])
            lines.append(("    pid %d  " % w["pid"] + what)[:160])
    return lines


def report():
    ws = workers()
    declared = _load()
    prune(_alive_pids(ws))
    mine = _mine(ws)

    buckets = {}
    for w in ws:
        buckets.setdefault(classify(w, declared, mine), []).append(w)
    info = _sessions_lines(buckets)

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
              buckets.get("undeclared-working", []) +
              buckets.get("own-idle", []))
    if not alerts:
        print(_clean_line(ws, buckets))
        for l in info:
            print(l)
        return 0

    print("CLAUDE WORKERS needing a decision (%d):" % len(alerts))
    for w in buckets.get("own-idle", []):
        print("  IDLE in this session — pid %d, alive %s, only %.2fs CPU, not waiting on anything"
              % (w["pid"], w["elapsed_raw"], w["cpu"]))
        print("      %s" % w["cmd"][:88])
        print("      Not an orphan (this session owns it). Stop it if it is no longer needed.")
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
    if buckets.get("declared-stalled") or buckets.get("undeclared-stalled"):
        print("  reap the stalled ones: python3 %s reap" % os.path.abspath(__file__))
    for l in info:
        print(l)
    return len(alerts)


def reap(dry_run=False):
    """Kill STALLED ORPHANS only. Never touches one consuming CPU, and never one whose
    `claude` session is still alive -- whatever classify says, that guard holds here."""
    ws = workers()
    declared = _load()
    mine = _mine(ws)
    killed, owned = [], 0
    for w in ws:
        if w.get("owner"):
            owned += 1
            continue
        state = classify(w, declared, mine)
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
    if owned:
        print("%d belong to live sessions, never reaped" % owned)
    return 0



def _emit(fn, *a, **kw):
    """Run a reporting function and deliver its output to the user as well as the
    model. See gt_settings.emit -- as a SessionStart hook, plain stdout reaches the
    model only, so a healthy check was invisible to the person it was reassuring.

    Degrades to a plain print, never to silence: if gt_settings cannot be imported
    OR is an older copy without capture/emit (a stale file in the hooks dir looked
    exactly like this on 2026-09-05 and would have crashed all four SessionStart
    checks while the component check reported clean), the report is printed
    directly. fn runs exactly once on every path."""
    try:
        import gt_settings
        capture, emit = gt_settings.capture, gt_settings.emit
    except Exception:
        return fn(*a, **kw)
    r, text = capture(fn, *a, **kw)
    try:
        emit(text)
    except Exception:
        print(text)
    return r

def main():
    a = [x for x in sys.argv[1:] if x != "--hook"]      # see gt_settings.hook_args
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
