#!/usr/bin/env python3
"""gt_schedule.py -- install, verify and remove gt's scheduled jobs. One script, no hand steps.

    gt_schedule.py list
    gt_schedule.py install <job> --vault V [--repo PATH ...] [--hour H] [--minute M]
    gt_schedule.py check   <job>
    gt_schedule.py remove  <job>

Jobs: daily (--vault, --repo ... the repos that count as work), lint-weekly (reads
vault-config.json itself), sweep (--vault and exactly one --repo: the tree it sweeps),
reminder (0.18.1; --vault optional: given, the deadlines mirror is refreshed first -- the job
itself never reads the vault, see gt_reminder.py on TCC).

WHY THIS EXISTS. The weekly lint agent was installed BY HAND in 2026-09-08 and its only record
is two lines in a runbook. That is a manual step with no --check and no rollback, on a machine
where scheduled jobs fail in ways a terminal run cannot reproduce. The owner's standing rule is
that any human step becomes a script with --check, self-proof and rollback, and this retrofits
it for both jobs.

VALIDATION GOES THROUGH launchd, NOT THROUGH A TERMINAL RUN, and on this Mac that distinction
is the whole game. A scheduled job here cannot list `~/.claude/projects/` -- it can stat it and
get True, then read nothing and report a clean empty result. So `install` does not stop at
writing a plist: it bootstraps the job, KICKSTARTS it, waits for it to finish, then reads
launchd's own `last exit code` and the job's output. A job that installs and silently never
works is the failure this is built to prevent.

ROLLBACK is `remove`: bootout, then delete the plist. Nothing is left behind, which is what
makes `install` safe to re-run.

WINDOWS (0.19.3): the same job table on Task Scheduler, per user and without admin. Each job is
a task named like the launchd label, whose action is one wrapper, <logs>/jobs/gt-<job>.cmd,
holding the job's command line with its output appended to the same .out/.err logs launchd
uses; beside it <logs>/jobs/gt-<job>.json keeps the job's arguments and schedule, the part a
plist holds on macOS. A wrapper because a task's command line is capped at 261 characters and
cannot redirect output. The proof is the same: `install` runs the task once (`schtasks /Run`)
and reads Task Scheduler's own Last Result back. `reconcile` rewrites the wrapper only -- the
task runs the wrapper, so it never has to be registered again.

THE INTERPRETER (0.19.3). `choose-interpreter`, which install.sh runs, keeps the recorded
interpreter rather than replacing it with whichever python ran the install: 0.19.1 and 0.19.2
re-recorded Homebrew's python on every install, which macOS privacy refuses the vault under
launchd, and every install broke the jobs the user had just repaired. When a job is installed
and the vault is known it PROVES the choice -- each candidate runs a one-file write probe in
the vault as a launchd job, and the first that succeeds is recorded. Without a probe it keeps
the record; with no record it prefers /usr/bin/python3 on macOS when that is a working 3.8+
(see preferred_candidates), because a Homebrew path changes with every Python upgrade.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

OK, PROBLEM, USAGE = 0, 1, 2

HOME = Path.home()
AGENTS = HOME / "Library" / "LaunchAgents"
HOOKS = HOME / ".claude" / "golden-thread" / "hooks"
LOGS = HOME / ".claude" / "golden-thread"
# 0.19.1: the ONE interpreter every job runs, recorded by install.sh. Before it, each job ran
# whichever python installed it (gt-lint-weekly /usr/bin/python3, gt-daily python3.9), so a
# macOS privacy grant given to one never covered the other.
INTERPRETER_RECORD = LOGS / "interpreter.json"
# Windows (0.19.3): each job's Task Scheduler wrapper and its arguments live here.
TASKS = LOGS / "jobs"
# Task Scheduler's Last Result while a task has never run, and while it is running.
SCHTASKS_NEVER_RAN, SCHTASKS_RUNNING = "267011", "267009"
SCHTASKS_DAYS = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")   # launchd 0..7


def on_windows():
    """Read at call time, not import time, so a POSIX test can patch os.name."""
    return os.name == "nt"

# label -> (script, default hour, default minute, weekday or None, what it does)
JOBS = {
    "daily": ("gt_daily.py", 22, 0, None,
              "write the day's facts into Daily Notes/<today>.md"),
    "lint-weekly": ("gt_lint_weekly.py", 7, 0, 1,
                    "vault + wiki lint, report into the vault (Mondays)"),
    # gt_sweep.py's docstring said "run it from launchd, the same way gt_lint_weekly.py is
    # wired" from the day it shipped, and there was no job to run it (audit, 2026-09-28).
    # 07:30, not 07:00: two jobs walking the same vault at the same minute is a collision
    # nobody needs, and the lint's report lands first.
    "sweep": ("gt_sweep.py", 7, 30, 1,
              "whole-tree secrets + code sweep, verdict filed in the vault (Mondays)"),
    # 0.18.1: the reminder tool. Reads ~/.claude/golden-thread/reminder/deadlines.json, the
    # mirror every session start refreshes, never the vault: a launchd job is refused
    # CloudStorage by TCC. 08:30 daily, after the 07:xx jobs.
    "reminder": ("gt_reminder.py", 8, 30, None,
                 "send overdue / due-soon deadlines through the enabled reminder channels"),
}

# Exit codes that are a NORMAL outcome for each job, not a failure. Without this the check
# reported the live weekly job as broken because it exits 1 when the lint finds something --
# which is most weeks. A check that calls a normal result a failure is the crying-wolf defect
# this file's own docstring warns about, and it appeared here within minutes of being written.
#
# CORRECTED 2026-09-28: lint-weekly listed {"0", "1"} on the belief that it "exits 1 when the
# lint finds something". It never did -- gt_lint_weekly.py returns 0 on every path it chooses,
# and its test asserts exactly that -- so the only way it exited 1 was an UNCAUGHT EXCEPTION.
# That morning the scheduled run died with PermissionError on the vault, launchd recorded
# exit 1, and this table called it normal: three Mondays of failed runs, surfaced by nothing.
# A benign code must be one the script RETURNS on purpose, never Python's crash code.
BENIGN_EXITS = {
    "daily": {"0", "1"},          # 1 = nothing recorded for the day (a crash is 3, see gt_daily)
    "lint-weekly": {"0"},         # findings are still exit 0; 3 = could not run
    "sweep": {"0"},               # findings are a report, exit 0; 3 = a member could not run
    "reminder": {"0"},            # nothing due is 0; 1 = a channel failed, 3 = no mirror
}


def label_for(job: str) -> str:
    return "com.markethaven.gt-%s" % job


def plist_path(job: str) -> Path:
    return AGENTS / ("%s.plist" % label_for(job))


def wrapper_path(job: str) -> Path:
    """Windows: the .cmd the job's task runs."""
    return TASKS / ("gt-%s.cmd" % job)


def spec_path(job: str) -> Path:
    """Windows: the job's arguments and schedule (what the plist holds on macOS), as JSON."""
    return TASKS / ("gt-%s.json" % job)


def job_file(job: str) -> Path:
    """The file whose presence means the job is installed on this platform."""
    return spec_path(job) if on_windows() else plist_path(job)


def atomic_write(path, data: bytes):
    """Write beside the target, then rename over it. A failed write (EPERM included) leaves
    the old file exactly as it was -- never truncated, never removed (0.19.3)."""
    path = Path(path)
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    try:
        with open(str(tmp), "wb") as fh:
            fh.write(data)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def run(args, timeout=180):
    # GT_SCHTASKS_STUB (tests only): a Python script that stands in for schtasks.exe, so the
    # doctor's schedule row can be tested on Windows without touching the real Task Scheduler.
    if args and args[0] == "schtasks" and os.environ.get("GT_SCHTASKS_STUB"):
        args = [sys.executable, os.environ["GT_SCHTASKS_STUB"]] + list(args[1:])
    try:
        return subprocess.run([str(a) for a in args], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        class R:
            returncode, stdout, stderr = 127, "", str(exc)
        return R()


def recorded_interpreter():
    """The interpreter install.sh recorded, or None when there is none usable."""
    try:
        p = json.loads(INTERPRETER_RECORD.read_text(encoding="utf-8"))["python"]
    except Exception:
        return None
    return p if os.path.isfile(p) and os.access(p, os.X_OK) else None


def interpreter():
    return recorded_interpreter() or sys.executable or "/usr/bin/python3"


def domain() -> str:
    return "gui/%d" % os.getuid()


def build_plist(job, vault, repos, hour, minute, weekday):
    script, _, _, _, _ = JOBS[job]
    target = HOOKS / script
    args = [interpreter(), str(target)]
    if job == "daily":
        # gt_daily needs to be told its vault -- Core rule 2, never inferred -- and which
        # repos count as work. gt_lint_weekly reads vault-config.json itself.
        args += ["--vault", str(vault)]
        for r in repos:
            args += ["--repo", str(r)]
    elif job == "sweep":
        # Both explicit: gt_sweep refuses to guess its vault, and its --path defaults to the
        # cwd, which under launchd is `/` -- a sweep of the whole disk reported as one tree.
        args += ["--vault", str(vault), "--path", str(repos[0])]
    elif job == "reminder":
        # No --vault, deliberately: the job reads the mirror outside CloudStorage.
        args += ["run", "--scheduled"]
    cal = {"Hour": int(hour), "Minute": int(minute)}
    if weekday is not None:
        cal["Weekday"] = int(weekday)
    return {
        "Label": label_for(job),
        "ProgramArguments": args,
        "RunAtLoad": False,
        "StartCalendarInterval": cal,
        "StandardOutPath": str(LOGS / ("%s.out" % job)),
        "StandardErrorPath": str(LOGS / ("%s.err" % job)),
        # PATH is deliberately explicit: a launchd job inherits almost nothing, and `git` not
        # being on PATH is the quiet way this whole thing produces an empty report.
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:"
                                         "/usr/sbin:/sbin"},
    }


def _launchd_last_exit(label):
    r = run(["launchctl", "print", "%s/%s" % (domain(), label)])
    if r.returncode != 0:
        return None, "not loaded"
    code = None
    for line in r.stdout.splitlines():
        if "last exit code" in line:
            value = line.split("=", 1)[1].strip()
            code = None if value in ("(never exited)", "-") else value
    return code, None


# ---- Windows: Task Scheduler (0.19.3) ---------------------------------------------------------

def _cmd_quote(arg) -> str:
    """One argument for a .cmd line: double-quoted, with % doubled so cmd.exe does not expand
    it as a variable. (A Windows path cannot contain a double quote.)"""
    return '"%s"' % str(arg).replace("%", "%%")


def build_wrapper(doc) -> bytes:
    """The .cmd a job's task runs: the job's command line, output appended to the same .out and
    .err logs the launchd job writes, and the job's exit code as the task's Last Result.
    CRLF: cmd.exe misparses an LF batch file."""
    lines = [
        "@echo off",
        "rem %s -- written by gt_schedule.py; change it with gt_schedule.py, not by hand"
        % doc["Label"],
        # UTF-8 mode: otherwise Windows Python writes cp1252 and dies on the first "→".
        "set PYTHONUTF8=1",
        " ".join(_cmd_quote(a) for a in doc["ProgramArguments"])
        + " >> %s 2>> %s" % (_cmd_quote(doc["StandardOutPath"]),
                             _cmd_quote(doc["StandardErrorPath"])),
        "exit /b %ERRORLEVEL%",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def schtasks_create_args(job, doc):
    when = doc["StartCalendarInterval"]
    args = ["schtasks", "/Create", "/F", "/TN", label_for(job),
            # Quoted inside the value: a user profile path may hold a space.
            "/TR", '"%s"' % wrapper_path(job)]
    if "Weekday" in when:
        args += ["/SC", "WEEKLY", "/D", SCHTASKS_DAYS[int(when["Weekday"]) % 8]]
    else:
        args += ["/SC", "DAILY"]
    return args + ["/ST", "%02d:%02d" % (when["Hour"], when["Minute"])]


def schtasks_query(job):
    """Task Scheduler's own record of the task as {field: value}, or None when it has none."""
    r = run(["schtasks", "/Query", "/TN", label_for(job), "/V", "/FO", "LIST"])
    if r.returncode != 0:
        return None
    info = {}
    for line in r.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            info.setdefault(key.strip(), value.strip())
    return info


def _schtasks_last_exit(job):
    info = schtasks_query(job)
    if info is None:
        return None, "not registered with Task Scheduler"
    code = info.get("Last Result", "")
    if code in ("", SCHTASKS_NEVER_RAN, SCHTASKS_RUNNING) \
            or info.get("Status", "").lower() == "running":
        return None, None
    return code, None


def last_exit(job):
    """(last exit code or None, why it is unknown or None) from launchd or Task Scheduler."""
    if on_windows():
        return _schtasks_last_exit(job)
    return _launchd_last_exit(label_for(job))


def read_doc(job):
    """The installed job's description -- the plist, or on Windows the spec -- or None."""
    try:
        if on_windows():
            return json.loads(spec_path(job).read_text(encoding="utf-8"))
        with plist_path(job).open("rb") as fh:
            return plistlib.load(fh)
    except Exception:
        return None


def job_args(job):
    """The installed job's command line (interpreter first), or None. Platform-neutral, for
    gt_doctor: it must not open a plist on Windows."""
    doc = read_doc(job)
    try:
        return [str(a) for a in doc["ProgramArguments"]]
    except Exception:
        return None


def write_task_files(job, doc):
    """Windows: the wrapper first, then the spec -- the spec is what marks a job installed."""
    TASKS.mkdir(parents=True, exist_ok=True)
    atomic_write(wrapper_path(job), build_wrapper(doc))
    atomic_write(spec_path(job), (json.dumps(doc, indent=1, sort_keys=True) + "\n")
                 .encode("utf-8"))


def _schedule_on_windows(job, doc):
    """Register the task, run it once, read Task Scheduler's Last Result. -> (code, error)."""
    write_task_files(job, doc)
    print("gt-schedule: wrote %s" % wrapper_path(job))
    r = run(schtasks_create_args(job, doc))
    if r.returncode != 0:
        return None, "schtasks /Create failed: %s" % (r.stderr.strip() or r.stdout.strip())
    before = (schtasks_query(job) or {}).get("Last Run Time")
    r = run(["schtasks", "/Run", "/TN", label_for(job)])
    if r.returncode != 0:
        return None, "schtasks /Run failed: %s" % (r.stderr.strip() or r.stdout.strip())
    code, started = None, False
    for _ in range(PROOF_WAIT):
        time.sleep(1)
        info = schtasks_query(job) or {}
        if info.get("Last Run Time") == before:
            continue
        started = True
        code, _why = _schtasks_last_exit(job)
        if code is not None:
            break
    if not started:
        # The task is per user and "interactive only" -- the same terms as a launchd agent in
        # the gui domain: it runs while the user is logged on at the desktop. From an SSH or
        # service session /Run is accepted and nothing starts.
        return None, NOT_STARTED
    return code, None


NOT_STARTED = ("Task Scheduler accepted the run but did not start the task. gt's tasks run only "
               "while you are logged on at the desktop (an SSH or service session does not "
               "count), as launchd runs gt's jobs only while you are logged in. The task is "
               "registered; log on and prove it with: gt_schedule.py check %s")


PROOF_WAIT = 60      # seconds to wait for the proof run (launchd: 30)


def do_install(a) -> int:
    job = a.job
    script, dh, dm, dw, _ = JOBS[job]
    target = HOOKS / script
    if not target.is_file():
        print("gt-schedule: CANNOT INSTALL — %s is not in %s. Run install.sh first: the job "
              "must run from OUTSIDE CloudStorage, so it runs the installed copy, not the "
              "release tree." % (script, HOOKS), file=sys.stderr)
        return PROBLEM

    vault = Path(a.vault).expanduser() if a.vault else None
    if job in ("daily", "sweep") and not vault:
        print("gt-schedule: --vault is required for the %s job" % job, file=sys.stderr)
        return USAGE
    if job == "sweep" and len(a.repo) != 1:
        print("gt-schedule: the sweep job takes exactly one --repo (the tree it sweeps)",
              file=sys.stderr)
        return USAGE

    # Prove the job can actually do its work BEFORE scheduling it. Installing a job that
    # cannot run is how a scheduler becomes a thing nobody trusts.
    if job in ("daily", "sweep"):
        # gt_sweep --check matters more than it looks: its members resolve their rules from
        # packs/ beside the release, so a copy that cannot find them reports "could not run"
        # on every tree. Better refused here than proven broken after it is scheduled.
        extra = (sum((["--repo", str(r)] for r in a.repo), []) if job == "daily"
                 else ["--path", str(a.repo[0])])
        pre = run([interpreter(), str(target), "--vault", str(vault),
                   *extra, "--check"])
        if pre.returncode != 0:
            print(pre.stdout + pre.stderr, file=sys.stderr)
            print("gt-schedule: CANNOT INSTALL — the job's own --check failed, so scheduling "
                  "it would schedule a job that does nothing", file=sys.stderr)
            return PROBLEM

    if job == "reminder":
        # Refresh the mirror from THIS terminal (which can read the vault), then the job's own
        # preflight: a mirror, at least one channel on, each enabled channel configured.
        py = interpreter()
        if vault:
            mir = run([py, str(target), "mirror", "--vault", str(vault)])
            print((mir.stdout + mir.stderr).strip())
        pre = run([py, str(target), "run", "--check"])
        if pre.returncode != 0:
            print(pre.stdout + pre.stderr, file=sys.stderr)
            print("gt-schedule: CANNOT INSTALL — the reminder preflight failed, so scheduling "
                  "it would schedule a job that sends nothing", file=sys.stderr)
            return PROBLEM

    LOGS.mkdir(parents=True, exist_ok=True)
    doc = build_plist(job, vault, a.repo, a.hour if a.hour is not None else dh,
                      a.minute if a.minute is not None else dm, dw)
    when = doc["StartCalendarInterval"]
    sched = "%02d:%02d" % (when["Hour"], when["Minute"])
    if "Weekday" in when:
        sched += " weekday %d" % when["Weekday"]

    if on_windows():
        # The same proof through Task Scheduler (0.19.3): register, run once, read the task's
        # own Last Result. A task that never reports one within the wait is NOT proven.
        code, err = _schedule_on_windows(job, doc)
        if err == NOT_STARTED:
            print("gt-schedule: %s is installed but NOT PROVEN. %s" % (label_for(job), err % job),
                  file=sys.stderr)
            return PROBLEM
        if err:
            print("gt-schedule: %s" % err, file=sys.stderr)
            return PROBLEM
        if code is not None and code in BENIGN_EXITS.get(job, {"0"}):
            print("gt-schedule: %s installed and PROVEN through Task Scheduler — runs at %s, "
                  "last result %s" % (label_for(job), sched, code))
            return OK
        print("gt-schedule: installed but the proof run %s. Read %s and %s — a job that "
              "installs and never works is what this check exists to catch. Roll back with: "
              "gt_schedule.py remove %s"
              % ("reported no result within %ds" % PROOF_WAIT if code is None
                 else "exited %s" % code,
                 LOGS / ("%s.err" % job), LOGS / ("%s.out" % job), job), file=sys.stderr)
        return PROBLEM

    AGENTS.mkdir(parents=True, exist_ok=True)
    path = plist_path(job)
    atomic_write(path, plistlib.dumps(doc))
    print("gt-schedule: wrote %s" % path)

    run(["launchctl", "bootout", "%s/%s" % (domain(), label_for(job))])   # idempotent
    r = run(["launchctl", "bootstrap", domain(), str(path)])
    if r.returncode != 0:
        print("gt-schedule: bootstrap failed: %s" % (r.stderr.strip() or r.stdout.strip()),
              file=sys.stderr)
        return PROBLEM

    # THE SELF-PROOF. A terminal run proves nothing on this Mac; the job must be exercised
    # through launchd and its own exit code read back.
    run(["launchctl", "kickstart", "-k", "%s/%s" % (domain(), label_for(job))])
    for _ in range(30):
        time.sleep(1)
        code, why = last_exit(job)
        if code is not None:
            break
    else:
        code, why = last_exit(job)

    if code is None or code in BENIGN_EXITS.get(job, {"0"}):
        print("gt-schedule: %s installed and PROVEN through launchd — runs at %s, last exit "
              "%s" % (label_for(job), sched, code if code is not None else "0"))
        return OK
    print("gt-schedule: installed but the proof run exited %s. Read %s and %s — a job that "
          "installs and never works is what this check exists to catch. Roll back with: "
          "gt_schedule.py remove %s"
          % (code, LOGS / ("%s.err" % job), LOGS / ("%s.out" % job), job), file=sys.stderr)
    return PROBLEM


def job_status(job):
    """-> (last exit code or None, [problem, ...]) for one job. Empty list = healthy.

    Shared by `check` and gt_doctor's `schedule` check, so the doctor judges a job with the
    same BENIGN_EXITS knowledge rather than a second copy of it that drifts (2026-09-28)."""
    problems = []
    if on_windows():
        if not spec_path(job).is_file():
            problems.append("no task file at %s" % spec_path(job))
        if not wrapper_path(job).is_file():
            problems.append("no wrapper at %s" % wrapper_path(job))
        code, why = last_exit(job)
        if why:
            problems.append("Task Scheduler does not have it (%s)" % why)
        elif code is not None and code not in BENIGN_EXITS.get(job, {"0"}):
            problems.append("last result = %s (see %s)" % (code, LOGS / ("%s.err" % job)))
        script = JOBS[job][0]
        if not (HOOKS / script).is_file():
            problems.append("%s is missing from %s" % (script, HOOKS))
        return code, problems
    path = plist_path(job)
    if not path.is_file():
        problems.append("no plist at %s" % path)
    code, why = last_exit(job)
    if why:
        problems.append("launchd does not have it loaded (%s)" % why)
    elif code is not None and code not in BENIGN_EXITS.get(job, {"0"}):
        # Read the error path out of the INSTALLED plist rather than guessing it: a job
        # installed by hand may log somewhere else entirely, and pointing someone at a file
        # that does not exist is worse than not pointing at all.
        err = LOGS / ("%s.err" % job)
        try:
            import plistlib as _pl
            with plist_path(job).open("rb") as fh:
                err = _pl.load(fh).get("StandardErrorPath", str(err))
        except Exception:
            pass
        problems.append("last exit code = %s (see %s)" % (code, err))
        cause = privacy_cause(job, err)
        if cause:
            problems.append(cause)
    script = JOBS[job][0]
    if not (HOOKS / script).is_file():
        problems.append("%s is missing from %s" % (script, HOOKS))
    return code, problems


_EPERM = __import__("re").compile(
    r"PermissionError: \[Errno 1\] Operation not permitted: '([^']+)'")


def privacy_cause(job, err_path):
    """A job that died with EPERM on a cloud-storage path was blocked by macOS privacy (TCC),
    not by a bug: name the interpreter that needs access and the folder (0.19.1)."""
    try:
        tail = Path(err_path).read_text(encoding="utf-8", errors="replace")[-20000:]
    except OSError:
        return None
    hits = [m.group(1) for m in _EPERM.finditer(tail) if "/Library/CloudStorage/" in m.group(1)]
    if not hits:
        return None
    head, _, rest = hits[-1].partition("/Library/CloudStorage/")
    folder = head + "/Library/CloudStorage/" + rest.split("/", 1)[0]
    try:
        with plist_path(job).open("rb") as fh:
            python = plistlib.load(fh)["ProgramArguments"][0]
    except Exception:
        python = "the job's interpreter"
    return ("macOS privacy blocked %s from %s (EPERM): give that interpreter Full Disk "
            "Access in System Settings > Privacy & Security, or re-run install.sh so every "
            "job uses the one recorded interpreter" % (python, folder))


def installed_jobs():
    """Jobs installed on this machine (a plist, or on Windows a task file). A job never
    installed is a choice, not a fault."""
    return [job for job in sorted(JOBS) if job_file(job).is_file()]


def do_check(a) -> int:
    job = a.job
    code, problems = job_status(job)
    for p in problems:
        print("gt-schedule: %s — %s" % (label_for(job), p), file=sys.stderr)
    if not problems:
        print("gt-schedule: %s is wired, loaded, and last exited %s"
              % (label_for(job), code if code is not None else "(not yet run)"))
    return PROBLEM if problems else OK


def do_remove(a) -> int:
    job = a.job
    if on_windows():
        r = run(["schtasks", "/Delete", "/TN", label_for(job), "/F"])
        gone = [p for p in (spec_path(job), wrapper_path(job)) if p.is_file()]
        for p in gone:
            p.unlink()
        if r.returncode == 0 or gone:
            print("gt-schedule: removed %s from Task Scheduler%s" % (
                label_for(job), " and deleted %s" % ", ".join(str(p) for p in gone)
                if gone else ""))
        else:
            print("gt-schedule: nothing to remove for %s" % label_for(job))
        return OK
    run(["launchctl", "bootout", "%s/%s" % (domain(), label_for(job))])
    path = plist_path(job)
    if path.is_file():
        path.unlink()
        print("gt-schedule: removed %s and booted the job out" % path)
    else:
        print("gt-schedule: nothing to remove for %s" % label_for(job))
    return OK


def _record(p):
    INTERPRETER_RECORD.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(INTERPRETER_RECORD, (json.dumps(
        {"python": p, "recorded": time.strftime("%Y-%m-%dT%H:%M:%S%z")}) + "\n").encode("utf-8"))


def do_record_interpreter(a) -> int:
    p = os.path.abspath(os.path.expanduser(a.python))
    if not (os.path.isfile(p) and os.access(p, os.X_OK)):
        print("gt-schedule: %s is not an executable interpreter; nothing recorded" % p,
              file=sys.stderr)
        return USAGE
    _record(p)
    print("gt-schedule: every job runs %s" % p)
    return OK


# ---- choosing the interpreter (0.19.3) ---------------------------------------------------------
#
# 2026-10-03, on the publishing Mac: install.sh recorded Homebrew's python3.9 (the python it ran
# under), and under launchd that interpreter got PermissionError writing the vault -- macOS
# privacy grants are per interpreter, and the one the user had granted was /usr/bin/python3.
# The jobs had been repaired by hand the night before; the 0.19.2 install undid it.

SYSTEM_PYTHON = "/usr/bin/python3"
XCODE_SELECT = "/usr/bin/xcode-select"     # exits 0 only when the Command Line Tools are there
PROBE_LABEL = "com.markethaven.gt-probe"
# Exit 0: wrote and removed one file in the vault. 13: PermissionError. 3: any other OSError.
PROBE_CODE = ("import os, sys\n"
              "p = os.path.join(sys.argv[1], '.gt-launchd-probe-%d' % os.getpid())\n"
              "try:\n"
              "    open(p, 'w').close()\n"
              "    os.remove(p)\n"
              "except PermissionError:\n"
              "    sys.exit(13)\n"
              "except OSError:\n"
              "    sys.exit(3)\n")


def qualifies(py):
    """A working Python 3.8+ at this path. /usr/bin/python3 counts only when the Command Line
    Tools are installed: without them it is a stub, and running it opens an install dialog."""
    if not (py and os.path.isfile(py) and os.access(py, os.X_OK)):
        return False
    if py == SYSTEM_PYTHON and sys.platform == "darwin" \
            and run([XCODE_SELECT, "-p"], timeout=15).returncode != 0:
        return False
    r = run([py, "-c", "import sys; print(sys.version_info >= (3, 8))"], timeout=30)
    return r.returncode == 0 and r.stdout.strip() == "True"


def preferred_candidates(given):
    """In order: the recorded interpreter (never replaced by whichever python ran an install),
    then on macOS /usr/bin/python3 -- a system path that survives Python upgrades, where a
    Homebrew path (/usr/local/opt/python@3.9/...) is gone after the next `brew upgrade` and
    takes every job with it -- then the candidates given (install.sh: its own python)."""
    out = []
    rec = recorded_interpreter()
    if rec:
        out.append(rec)
    if sys.platform == "darwin" and not on_windows():
        out.append(SYSTEM_PYTHON)
    out += list(given)
    return [c for i, c in enumerate(out) if c and c not in out[:i]]


def probe_under_launchd(py, vault):
    """Run the write probe as a one-off launchd job. True / False, or None when it could not
    be run at all (launchd refused the job, or it never reported an exit)."""
    import shutil
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="gt-probe-"))
    plist = d / ("%s.plist" % PROBE_LABEL)
    target = "%s/%s" % (domain(), PROBE_LABEL)
    try:
        atomic_write(plist, plistlib.dumps({"Label": PROBE_LABEL, "RunAtLoad": False,
                                            "ProgramArguments": [py, "-c", PROBE_CODE,
                                                                 str(vault)]}))
        run(["launchctl", "bootout", target])
        if run(["launchctl", "bootstrap", domain(), str(plist)]).returncode != 0:
            return None
        run(["launchctl", "kickstart", "-k", target])
        code = None
        for _ in range(40):
            time.sleep(0.5)
            code, _why = _launchd_last_exit(PROBE_LABEL)
            if code is not None:
                break
        return None if code is None else code == "0"
    finally:
        run(["launchctl", "bootout", target])
        shutil.rmtree(str(d), ignore_errors=True)


def configured_vault():
    try:
        cfg = json.loads((HOME / ".claude" / "vault-config.json").read_text(encoding="utf-8"))
        return cfg.get("vault_path") or None
    except Exception:
        return None


def can_probe(vault):
    """Only for the REAL user on macOS with a job installed: launchd's domain is gui/<uid>
    whatever HOME says, and with no job installed there is nothing the choice could break."""
    import shutil
    return (sys.platform == "darwin" and not on_windows() and bool(vault)
            and Path(vault).is_dir() and bool(installed_jobs()) and _is_real_home()
            and shutil.which("launchctl") is not None)


def do_choose_interpreter(a) -> int:
    given = [os.path.abspath(os.path.expanduser(c)) for c in a.candidate]
    cands = [c for c in preferred_candidates(given) if qualifies(c)]
    if not cands:
        print("gt-schedule: no working Python 3.8+ among %s; the record is left as it is"
              % (", ".join(preferred_candidates(given)) or "(none)"), file=sys.stderr)
        return PROBLEM
    rec = recorded_interpreter()
    vault = a.vault or configured_vault()
    rc = OK
    if not a.no_probe and can_probe(vault):
        chosen = None
        for c in cands:
            ok = probe_under_launchd(c, vault)
            if ok:
                chosen, why = c, "it wrote the vault under launchd"
                break
            print("gt-schedule: %s %s" % (c, "cannot write %s under launchd (macOS privacy)"
                                          % vault if ok is False else
                                          "could not be probed under launchd"))
        if chosen is None:
            chosen, why = cands[0], ("no candidate wrote the vault under launchd: give it Full "
                                     "Disk Access in System Settings > Privacy & Security")
            rc = PROBLEM
    else:
        chosen = cands[0]
        why = ("kept the recorded interpreter" if chosen == rec else
               "the macOS system python, which survives Python upgrades" if chosen == SYSTEM_PYTHON
               else "the python running the install")
    if chosen != rec:
        _record(chosen)
    print("gt-schedule: every job runs %s (%s)" % (chosen, why))
    return rc


def _is_real_home():
    try:
        import pwd
        real = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except Exception:
        return False
    return AGENTS.resolve().is_relative_to(real) if hasattr(Path, 'is_relative_to') \
        else str(AGENTS.resolve()).startswith(str(real) + os.sep)


def job_interpreter(job):
    """The interpreter an installed job runs, or None."""
    args = job_args(job)
    return args[0] if args else None


def _cannot_rewrite(job, path, exc, have):
    """A rewrite that failed leaves the job exactly as it was. Said in words, not a traceback:
    on macOS the cause has been a plist's com.apple.provenance protection (2026-10-03)."""
    print("gt-schedule: could not rewrite %s (%s). %s is left as it was, still running %s. "
          "Re-create it from a terminal (gt_schedule.py install %s ...), or keep that "
          "interpreter for every job: gt_schedule.py record-interpreter %s"
          % (path, getattr(exc, "strerror", None) or "%s: %s" % (exc.__class__.__name__, exc),
             label_for(job), have, job, have),
          file=sys.stderr)


def do_reconcile(a) -> int:
    """Rewrite any installed job whose interpreter is not the recorded one; reload it. A
    rewrite that fails (EPERM included) leaves that job as it was -- never removed."""
    want = recorded_interpreter()
    if not want:
        print("gt-schedule: no recorded interpreter; jobs left as they are")
        return OK
    rc = OK
    for job in installed_jobs():
        path = job_file(job)
        doc = read_doc(job)
        try:
            have = doc["ProgramArguments"][0]
        except Exception:
            print("gt-schedule: %s unreadable; left alone" % path, file=sys.stderr)
            rc = PROBLEM
            continue
        if have == want:
            continue
        doc["ProgramArguments"][0] = want
        try:
            if on_windows():
                # The task runs the wrapper, so rewriting it is the whole change.
                write_task_files(job, doc)
            else:
                atomic_write(path, plistlib.dumps(doc))
        except (OSError, KeyError, TypeError, ValueError) as exc:
            # A write refused (EPERM), or a job description missing a field the rewrite needs:
            # either way the job is left exactly as it was, and said so in words.
            _cannot_rewrite(job, path, exc, have)
            rc = PROBLEM
            continue
        print("gt-schedule: %s now runs %s (was %s)" % (label_for(job), want, have))
        # Reload only the REAL user's jobs: launchd's domain is gui/<uid> whatever HOME says,
        # so a sandbox HOME (a test, a throwaway install) reloading would replace the
        # developer's real job with the sandbox's plist.
        if not on_windows() and not a.no_reload and _is_real_home():
            run(["launchctl", "bootout", "%s/%s" % (domain(), label_for(job))])
            r = run(["launchctl", "bootstrap", domain(), str(path)])
            if r.returncode != 0:
                print("gt-schedule: reload of %s failed: %s"
                      % (label_for(job), (r.stderr or r.stdout).strip()), file=sys.stderr)
                rc = PROBLEM
    return rc


def do_list(_a) -> int:
    print("%-14s %-20s %-8s %s" % ("JOB", "SCRIPT", "DEFAULT", "WHAT"))
    for job, (script, h, m, w, what) in sorted(JOBS.items()):
        when = "%02d:%02d" % (h, m) + (" wd%d" % w if w else "")
        state = "installed" if job_file(job).is_file() else "-"
        print("%-14s %-20s %-8s %s  [%s]" % (job, script, when, what, state))
    return OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="install and verify gt's scheduled jobs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=do_list)
    ri = sub.add_parser("record-interpreter", help="the one python every job runs (install.sh)")
    ri.add_argument("python")
    ri.set_defaults(fn=do_record_interpreter)
    ci = sub.add_parser("choose-interpreter",
                        help="keep, prove or pick the one python every job runs (install.sh)")
    ci.add_argument("--candidate", action="append", default=[],
                    help="a python to consider after the recorded one (repeatable)")
    ci.add_argument("--vault", help="the vault the probe writes (default: vault-config.json)")
    ci.add_argument("--no-probe", action="store_true", help="choose without a launchd probe")
    ci.set_defaults(fn=do_choose_interpreter)
    rc = sub.add_parser("reconcile", help="rewrite jobs on another interpreter, then reload")
    rc.add_argument("--no-reload", action="store_true", help="rewrite the plists only")
    rc.set_defaults(fn=do_reconcile)
    for name, fn in (("install", do_install), ("check", do_check), ("remove", do_remove)):
        p = sub.add_parser(name)
        p.add_argument("job", choices=sorted(JOBS))
        p.add_argument("--vault")
        p.add_argument("--repo", action="append", default=[])
        p.add_argument("--hour", type=int)
        p.add_argument("--minute", type=int)
        p.set_defaults(fn=fn)
    a = ap.parse_args(argv)
    # Native Windows: 0.19.2 refused install/check/remove here (no launchd); since 0.19.3 they
    # run on Task Scheduler (schtasks), per user and without admin.
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
