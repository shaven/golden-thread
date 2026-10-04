#!/usr/bin/env python3
"""gt_schedule.py -- install, verify and remove gt's scheduled jobs. One script, no hand steps.

    gt_schedule.py list
    gt_schedule.py install <job> --vault V [--repo PATH ...] [--hour H] [--minute M]
    gt_schedule.py check   <job>
    gt_schedule.py remove  <job>
    gt_schedule.py migrate-labels

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

INSTALLED MEANS REGISTERED (0.20.1). Until 0.20.0 a job was "installed" when its file was on
disk, whatever the scheduler said: a refused `launchctl bootstrap` left the plist behind and
`list` said [installed]; a failed `schtasks /Create` left the spec behind and the doctor FAILed
it on every run; the post-install gate PASSed a daily job no scheduler knew about. Now a
refused registration puts the files back as they were and the job is NOT installed, said in
words; `job_registered()` asks the scheduler itself, and `job_status` names a job whose file
is on disk while the scheduler does not have it. `remove` claims only what it removed.

LINUX (0.20.1). 0.20.0 wrote a launchd plist on Linux and called launchctl -- there is no
launchd there, so every Linux job was a file nothing ran. Linux now uses a systemd --user
timer (<label>.service + <label>.timer under ~/.config/systemd/user; enabled with
`systemctl --user enable --now`, proven with `systemctl --user start` and the unit's own
ExecMainStatus) and, where there is no user manager to talk to, a tagged crontab line
(`# gt-schedule:<label>`; only gt's lines are ever touched, the old crontab is backed up). The
job's arguments live in a spec beside the logs (<logs>/jobs/gt-<job>.json), as on Windows.
GT_SCHEDULE_BACKEND=systemd|cron forces one.

THE LABEL (0.20.1). Jobs are `io.goldenthread.gt-<job>`; until 0.20.0 they were
`com.markethaven.gt-<job>` -- a vendor's name inside a public product. `migrate-labels` (also
run by `reconcile`, which install.sh runs) moves a job installed under the old label to the new
one from its own arguments and schedule, then removes the old registration; if the new one
cannot be registered the old one is left exactly as it was, and said so.

WINDOWS (0.20.0): the same job table on Task Scheduler, per user and without admin. Each job is
a task named like the launchd label, whose action is one wrapper, <logs>/jobs/gt-<job>.cmd,
holding the job's command line with its output appended to the same .out/.err logs launchd
uses; beside it <logs>/jobs/gt-<job>.json keeps the job's arguments and schedule, the part a
plist holds on macOS. A wrapper because a task's command line is capped at 261 characters and
cannot redirect output. The proof is the same: `install` runs the task once (`schtasks /Run`)
and reads Task Scheduler's own Last Result back. `reconcile` rewrites the wrapper only -- the
task runs the wrapper, so it never has to be registered again.

THE INTERPRETER (0.20.0). `choose-interpreter`, which install.sh runs, keeps the recorded
interpreter rather than replacing it with whichever python ran the install: 0.19.1 and 0.19.2
re-recorded Homebrew's python on every install, which macOS privacy refuses the vault under
launchd, and every install broke the jobs the user had just repaired. When a job is installed
and the vault is known it PROVES the choice -- each candidate runs a one-file write probe in
the vault as a launchd job, and the first that succeeds is recorded. Without a probe it keeps
the record; with no record it prefers /usr/bin/python3 on macOS when that is a working 3.8+
(see preferred_candidates), because a Homebrew path changes with every Python upgrade.
interpreter.json also lists, under "refused" (0.20.1), the candidates macOS refused in the
write probe, so install.sh knows a bare `python3` would be one of them.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

OK, PROBLEM, USAGE = 0, 1, 2

HOME = Path.home()
AGENTS = HOME / "Library" / "LaunchAgents"
HOOKS = HOME / ".claude" / "golden-thread" / "hooks"
LOGS = HOME / ".claude" / "golden-thread"
# Linux (0.20.1): systemd --user unit files.
SYSTEMD_USER = Path(os.environ.get("XDG_CONFIG_HOME") or str(HOME / ".config")) / "systemd" / "user"
# 0.19.1: the ONE interpreter every job runs, recorded by install.sh. Before it, each job ran
# whichever python installed it (gt-lint-weekly /usr/bin/python3, gt-daily python3.9), so a
# macOS privacy grant given to one never covered the other.
INTERPRETER_RECORD = LOGS / "interpreter.json"
# Windows (0.20.0) and Linux (0.20.1): each job's spec (and on Windows its wrapper) live here.
TASKS = LOGS / "jobs"
# Task Scheduler's Last Result while a task has never run, and while it is running.
SCHTASKS_NEVER_RAN, SCHTASKS_RUNNING = "267011", "267009"
SCHTASKS_DAYS = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")   # launchd 0..7
SYSTEMD_DAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# The label (0.20.1): product-neutral. LEGACY_LABEL_PREFIX is what 0.20.0 and earlier used;
# migrate-labels moves a job from it.
LABEL_PREFIX = "io.goldenthread.gt-"
LEGACY_LABEL_PREFIX = "com.markethaven.gt-"
CRON_TAG = "# gt-schedule:"

# A test or throwaway install says so through the environment (0.20.0). launchd's domain is
# gui/<uid> and Task Scheduler's is the user's, WHATEVER HOME says, so a sandbox that reached
# either would replace the developer's real jobs with its own. tests/_harness.py sets
# GT_TEST_SANDBOX for every test and every process a test starts.
SANDBOX_MARKERS = ("GT_TEST_SANDBOX",)
# The scheduler verbs that CHANGE something. Read-only ones (launchctl print, schtasks /Query,
# systemctl --user show / is-enabled, crontab -l) stay allowed, so a sandbox can still report
# status.
SCHEDULER_WRITES = {"launchctl": {"bootstrap", "bootout", "kickstart", "load", "unload",
                                  "enable", "disable", "remove", "submit"},
                    "schtasks": {"/create", "/run", "/delete", "/change", "/end"},
                    "systemctl": {"enable", "disable", "start", "stop", "restart",
                                  "daemon-reload", "link", "reenable", "mask", "unmask"},
                    "crontab": {"-", "-r", "-e"}}


def sandboxed():
    """True when a test/sandbox marker is set: the real scheduler is never touched then."""
    return any(os.environ.get(k) for k in SANDBOX_MARKERS)


def on_windows():
    """Read at call time, not import time, so a POSIX test can patch os.name."""
    return os.name == "nt"


def on_linux():
    """Linux (and anything else that is neither macOS nor Windows): systemd --user or cron."""
    return not on_windows() and sys.platform != "darwin"


def platform_kind():
    if on_windows():
        return "windows"
    if on_linux():
        return "linux"
    return "macos"


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
    return LABEL_PREFIX + job


def legacy_label_for(job: str) -> str:
    return LEGACY_LABEL_PREFIX + job


def plist_path(job: str, label: str = None) -> Path:
    return AGENTS / ("%s.plist" % (label or label_for(job)))


def legacy_plist_path(job: str) -> Path:
    return plist_path(job, legacy_label_for(job))


def wrapper_path(job: str) -> Path:
    """Windows: the .cmd the job's task runs."""
    return TASKS / ("gt-%s.cmd" % job)


def spec_path(job: str) -> Path:
    """Windows and Linux: the job's arguments and schedule (what the plist holds on macOS)."""
    return TASKS / ("gt-%s.json" % job)


def exit_path(job: str) -> Path:
    """Linux cron route: the last exit code the cron line records."""
    return LOGS / ("%s.exit" % job)


def unit_paths(label: str):
    return SYSTEMD_USER / ("%s.service" % label), SYSTEMD_USER / ("%s.timer" % label)


def _job_files(job):
    """Where this platform keeps a job's description, primary first. A Linux machine may still
    hold the launchd plist 0.20.0 wrote there (it never ran): that counts as installed-and-broken
    until migrate-labels converts it, so the doctor names it rather than missing it."""
    kind = platform_kind()
    if kind == "windows":
        return [spec_path(job)]
    if kind == "linux":
        return [spec_path(job), legacy_plist_path(job)]
    return [plist_path(job), legacy_plist_path(job)]


def job_file(job: str) -> Path:
    """The file whose presence means the job is on this machine (registered or not -- see
    job_registered). The primary location when there is none."""
    files = _job_files(job)
    return next((f for f in files if f.is_file()), files[0])


def _read_file_doc(path):
    try:
        if path.suffix == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
        with path.open("rb") as fh:
            return plistlib.load(fh)
    except Exception:
        return None


def read_doc(job):
    """The installed job's description -- the plist, or on Windows/Linux the spec -- or None."""
    return _read_file_doc(job_file(job))


def installed_label(job: str) -> str:
    """The label this job is actually installed under: the new one, or the legacy one on a
    machine not yet migrated. The new label when the job is not installed."""
    path = job_file(job)
    if path.suffix == ".plist":
        return path.stem if path.is_file() else label_for(job)
    doc = _read_file_doc(path) if path.is_file() else None
    lab = (doc or {}).get("Label") if isinstance(doc, dict) else None
    return lab if lab in (label_for(job), legacy_label_for(job)) else label_for(job)


def atomic_write(path, data: bytes):
    """Write beside the target, then rename over it. A failed write (EPERM included) leaves
    the old file exactly as it was -- never truncated, never removed (0.20.0)."""
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


def _snapshot(paths):
    """{path: bytes or None} -- what to put back when a registration is refused."""
    out = {}
    for p in paths:
        p = Path(p)
        try:
            out[p] = p.read_bytes() if p.is_file() else None
        except OSError:
            out[p] = None
    return out


def _restore(snap):
    for p, data in snap.items():
        try:
            if data is None:
                if p.exists():
                    p.unlink()
            else:
                atomic_write(p, data)
        except OSError:
            pass


def _is_write(args):
    tool = os.path.basename(str(args[0])) if args else ""
    verbs = SCHEDULER_WRITES.get(tool)
    if not verbs:
        return False
    if tool == "launchctl" or tool == "schtasks":
        return len(args) > 1 and str(args[1]).lower() in verbs
    if tool == "crontab":
        return "-l" not in [str(a) for a in args[1:]]       # only `crontab -l` reads
    return any(str(a) in verbs for a in args[1:])          # systemctl --user <verb> ...


def run(args, timeout=180, input=None):
    # GT_SCHTASKS_STUB (tests only): a Python script that stands in for schtasks.exe, so the
    # doctor's schedule row can be tested on Windows without touching the real Task Scheduler.
    if args and args[0] == "schtasks" and os.environ.get("GT_SCHTASKS_STUB"):
        args = [sys.executable, os.environ["GT_SCHTASKS_STUB"]] + list(args[1:])
    elif _is_write(args) and sandboxed():
        # The last line of defence: whichever code path got here, a sandbox never changes the
        # real launchd domain, Task Scheduler, systemd user manager or crontab.
        class Refused:
            returncode, stdout = 1, ""
            stderr = ("refused: %s %s from a test/sandbox (%s set) -- the real scheduler is "
                      "never touched from one" % (args[0], " ".join(map(str, args[1:3])),
                                                  "/".join(SANDBOX_MARKERS)))
        return Refused()
    try:
        return subprocess.run([str(a) for a in args], capture_output=True, text=True,
                              timeout=timeout, input=input)
    except (OSError, subprocess.SubprocessError) as exc:
        class R:
            returncode, stdout, stderr = 127, "", str(exc)
        return R()


def _why(r):
    return (getattr(r, "stderr", "") or "").strip() or (getattr(r, "stdout", "") or "").strip() \
        or "exit %s" % getattr(r, "returncode", "?")


def recorded_interpreter():
    """The interpreter install.sh recorded, or None when there is none usable."""
    try:
        p = json.loads(INTERPRETER_RECORD.read_text(encoding="utf-8"))["python"]
    except Exception:
        return None
    return p if os.path.isfile(p) and os.access(p, os.X_OK) else None


def recorded_refused():
    """The interpreters the macOS write probe refused, as interpreter.json records them."""
    try:
        r = json.loads(INTERPRETER_RECORD.read_text(encoding="utf-8")).get("refused") or []
    except Exception:
        return []
    return [str(x) for x in r] if isinstance(r, list) else []


refused_interpreters = recorded_refused       # the public name (install.sh, gt_components)


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


# ---- Windows: Task Scheduler (0.20.0) ---------------------------------------------------------

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


def schtasks_create_args(job, doc, label=None):
    when = doc["StartCalendarInterval"]
    args = ["schtasks", "/Create", "/F", "/TN", label or label_for(job),
            # Quoted inside the value: a user profile path may hold a space.
            "/TR", '"%s"' % wrapper_path(job)]
    if "Weekday" in when:
        args += ["/SC", "WEEKLY", "/D", SCHTASKS_DAYS[int(when["Weekday"]) % 8]]
    else:
        args += ["/SC", "DAILY"]
    return args + ["/ST", "%02d:%02d" % (when["Hour"], when["Minute"])]


def schtasks_query(job, label=None):
    """Task Scheduler's own record of the task as {field: value}, or None when it has none."""
    r = run(["schtasks", "/Query", "/TN", label or label_for(job), "/V", "/FO", "LIST"])
    if r.returncode != 0:
        return None
    info = {}
    for line in r.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            info.setdefault(key.strip(), value.strip())
    return info


def _schtasks_last_exit(job, label=None):
    info = schtasks_query(job, label)
    if info is None:
        return None, "not registered with Task Scheduler"
    code = info.get("Last Result", "")
    if code in ("", SCHTASKS_NEVER_RAN, SCHTASKS_RUNNING) \
            or info.get("Status", "").lower() == "running":
        return None, None
    return code, None


# ---- Linux: systemd --user timer, or a tagged crontab line (0.20.1) ---------------------------

def _systemd_quote(arg) -> str:
    """One ExecStart argument: double-quoted with systemd's escapes; % and $ doubled so neither
    a specifier nor a variable is expanded."""
    s = str(arg).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return '"%s"' % s


def build_units(doc):
    """-> (service text, timer text) for one job."""
    when = doc["StartCalendarInterval"]
    path = (doc.get("EnvironmentVariables") or {}).get("PATH", "/usr/bin:/bin")
    service = "\n".join([
        "# %s -- written by gt_schedule.py; change it with gt_schedule.py, not by hand"
        % doc["Label"],
        "[Unit]",
        "Description=Golden Thread scheduled job %s" % doc["Label"],
        "",
        "[Service]",
        "Type=oneshot",
        "Environment=%s" % _systemd_quote("PATH=%s" % path),
        "ExecStart=%s" % " ".join(_systemd_quote(a) for a in doc["ProgramArguments"]),
        "StandardOutput=append:%s" % doc["StandardOutPath"],
        "StandardError=append:%s" % doc["StandardErrorPath"],
        ""])
    day = ("%s " % SYSTEMD_DAYS[int(when["Weekday"]) % 8]) if "Weekday" in when else ""
    timer = "\n".join([
        "# %s -- written by gt_schedule.py; change it with gt_schedule.py, not by hand"
        % doc["Label"],
        "[Unit]",
        "Description=Golden Thread schedule for %s" % doc["Label"],
        "",
        "[Timer]",
        "OnCalendar=%s*-*-* %02d:%02d:00" % (day, int(when["Hour"]), int(when["Minute"])),
        "Persistent=true",
        "",
        "[Install]",
        "WantedBy=timers.target",
        ""])
    return service, timer


def _shell_command(job, doc):
    """The job as one sh command line: PATH set, output appended, exit code recorded."""
    path = (doc.get("EnvironmentVariables") or {}).get("PATH", "/usr/bin:/bin")
    return ("PATH=%s %s >> %s 2>> %s; echo $? > %s"
            % (shlex.quote(path), " ".join(shlex.quote(str(a)) for a in doc["ProgramArguments"]),
               shlex.quote(doc["StandardOutPath"]), shlex.quote(doc["StandardErrorPath"]),
               shlex.quote(str(exit_path(job)))))


def cron_line(job, doc):
    when = doc["StartCalendarInterval"]
    dow = str(int(when["Weekday"]) % 7) if "Weekday" in when else "*"
    # % is a newline in a crontab command unless escaped.
    return "%d %d * * %s %s %s%s" % (int(when["Minute"]), int(when["Hour"]), dow,
                                     _shell_command(job, doc).replace("%", "\\%"),
                                     CRON_TAG, doc["Label"])


def _is_our_cron_line(line, label):
    return line.rstrip().endswith(CRON_TAG + label)


def linux_backend():
    """-> ("systemd" | "cron" | None, why none). GT_SCHEDULE_BACKEND forces one."""
    forced = os.environ.get("GT_SCHEDULE_BACKEND", "").strip()
    if forced in ("systemd", "cron"):
        return forced, None
    why = []
    if shutil.which("systemctl"):
        r = run(["systemctl", "--user", "show-environment"], timeout=30)
        if r.returncode == 0:
            return "systemd", None
        why.append("systemctl --user has no user manager to talk to (%s)"
                   % _why(r).splitlines()[0])
    else:
        why.append("no systemctl")
    if shutil.which("crontab"):
        return "cron", None
    why.append("no crontab")
    return None, "; ".join(why)


def _crontab_read():
    """-> (lines, None) or (None, why). No crontab at all is an empty one."""
    r = run(["crontab", "-l"], timeout=30)
    if r.returncode == 0:
        return r.stdout.splitlines(), None
    if "no crontab" in (r.stderr + r.stdout).lower():
        return [], None
    return None, _why(r)


def _crontab_write(lines, old_text):
    """Replace the crontab, backing the old one up first. -> None or why it was refused."""
    backups = LOGS / "backups"
    try:
        backups.mkdir(parents=True, exist_ok=True)
        (backups / ("crontab.%s.gt-schedule" % time.strftime("%Y%m%d_%H%M%S"))).write_bytes(
            old_text.encode("utf-8"))
    except OSError:
        pass
    body = "\n".join(lines) + "\n" if lines else ""
    r = run(["crontab", "-"], timeout=30, input=body)
    return None if r.returncode == 0 else _why(r)


def _cron_set(job, label, line):
    """Put `line` in as the only line tagged for `label` (None: take them all out).
    -> (changed, why refused or None)."""
    old, why = _crontab_read()
    if old is None:
        return False, "crontab -l failed: %s" % why
    keep = [l for l in old if not _is_our_cron_line(l, label)]
    new = keep + ([line] if line else [])
    if new == old:
        return False, None
    why = _crontab_write(new, "\n".join(old) + ("\n" if old else ""))
    return (why is None), (None if why is None else "crontab refused the update: %s" % why)


def _systemd_register(job, doc):
    """Write the units, reload, enable the timer. -> None, or why it was refused (the unit files
    are then put back as they were)."""
    label = doc["Label"]
    svc, tmr = unit_paths(label)
    snap = _snapshot([svc, tmr])
    service, timer = build_units(doc)
    try:
        SYSTEMD_USER.mkdir(parents=True, exist_ok=True)
        atomic_write(svc, service.encode("utf-8"))
        atomic_write(tmr, timer.encode("utf-8"))
    except OSError as exc:
        _restore(snap)
        return "could not write %s (%s)" % (SYSTEMD_USER, exc)
    r = run(["systemctl", "--user", "daemon-reload"], timeout=60)
    if r.returncode == 0:
        r = run(["systemctl", "--user", "enable", "--now", "%s.timer" % label], timeout=60)
    if r.returncode != 0:
        _restore(snap)
        run(["systemctl", "--user", "daemon-reload"], timeout=60)
        return "systemctl --user refused it: %s" % _why(r)
    return None


def _systemd_unregister(label):
    """-> (did [str], problems [str])."""
    did, problems = [], []
    svc, tmr = unit_paths(label)
    present = svc.is_file() or tmr.is_file()
    enabled = run(["systemctl", "--user", "is-enabled", "%s.timer" % label], timeout=30)
    if enabled.returncode == 0:
        r = run(["systemctl", "--user", "disable", "--now", "%s.timer" % label], timeout=60)
        if r.returncode == 0:
            did.append("disabled the systemd --user timer %s.timer" % label)
        else:
            problems.append("systemctl --user disable %s.timer failed: %s" % (label, _why(r)))
    for p in (svc, tmr):
        if p.is_file():
            try:
                p.unlink()
                did.append("deleted %s" % p)
            except OSError as exc:
                problems.append("could not delete %s (%s)" % (p, exc))
    if present:
        run(["systemctl", "--user", "daemon-reload"], timeout=60)
    return did, problems


def _systemd_last_exit(label):
    r = run(["systemctl", "--user", "show", "%s.service" % label, "-p", "ExecMainStatus",
             "-p", "ExecMainStartTimestampMonotonic", "-p", "LoadState"], timeout=30)
    if r.returncode != 0:
        return None, "systemctl --user show failed (%s)" % _why(r).splitlines()[0]
    info = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    if info.get("LoadState") == "not-found":
        return None, "not loaded by systemd --user"
    if info.get("ExecMainStartTimestampMonotonic", "0") in ("", "0"):
        return None, None
    return info.get("ExecMainStatus") or None, None


def _cron_last_exit(job):
    try:
        v = exit_path(job).read_text(encoding="utf-8").strip()
        return (v or None), None
    except OSError:
        return None, None


def _linux_last_exit(job):
    path = job_file(job)
    if path.suffix == ".plist":
        return None, "a launchd plist an earlier gt wrote; Linux has no launchd, so it never ran"
    doc = read_doc(job) or {}
    if doc.get("Backend") == "cron":
        return _cron_last_exit(job)
    return _systemd_last_exit(installed_label(job))


def last_exit(job):
    """(last exit code or None, why it is unknown or None) from the job's scheduler."""
    kind = platform_kind()
    if kind == "windows":
        return _schtasks_last_exit(job, installed_label(job))
    if kind == "linux":
        return _linux_last_exit(job)
    return _launchd_last_exit(installed_label(job))


def job_registered(job):
    """Does the SCHEDULER itself have this job? True / False, or None when it cannot be asked
    honestly -- a test/sandbox, or a HOME that is not the account's own (launchd, the systemd
    user manager, crontab and Task Scheduler answer for the real user whatever HOME says)."""
    kind = platform_kind()
    label = installed_label(job)
    if kind == "windows":
        if sandboxed() and not os.environ.get("GT_SCHTASKS_STUB"):
            return None
        return schtasks_query(job, label) is not None
    if kind == "linux":
        path = job_file(job)
        if path.suffix == ".plist":
            return False if path.is_file() else None
        if not _is_real_home():
            return None
        doc = read_doc(job) or {}
        if not doc:
            return False
        if doc.get("Backend") == "cron":
            lines, _why_ = _crontab_read()
            return None if lines is None else any(_is_our_cron_line(l, label) for l in lines)
        if not shutil.which("systemctl"):
            return None
        r = run(["systemctl", "--user", "is-enabled", "%s.timer" % label], timeout=30)
        if r.returncode == 0:
            return True
        out = (r.stdout + r.stderr).lower()
        if "failed to connect" in out or r.returncode == 127:
            return None
        return False
    if not _is_real_home():
        return None
    return run(["launchctl", "print", "%s/%s" % (domain(), label)]).returncode == 0


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
    write_spec(job, doc)


def write_spec(job, doc):
    TASKS.mkdir(parents=True, exist_ok=True)
    atomic_write(spec_path(job), (json.dumps(doc, indent=1, sort_keys=True) + "\n")
                 .encode("utf-8"))


def _schedule_on_windows(job, doc):
    """Register the task, run it once, read Task Scheduler's Last Result. -> (code, error).
    A refused /Create puts the wrapper and spec back as they were: the job is NOT installed."""
    snap = _snapshot([wrapper_path(job), spec_path(job)])
    write_task_files(job, doc)
    r = run(schtasks_create_args(job, doc))
    if r.returncode != 0:
        _restore(snap)
        return None, CREATE_REFUSED % (label_for(job), _why(r))
    print("gt-schedule: wrote %s" % wrapper_path(job))
    before = (schtasks_query(job) or {}).get("Last Run Time")
    r = run(["schtasks", "/Run", "/TN", label_for(job)])
    if r.returncode != 0:
        return None, "schtasks /Run failed: %s" % _why(r)
    code, started = None, False
    for _ in range(PROOF_WAIT):
        time.sleep(1)
        info = schtasks_query(job) or {}
        if info.get("Last Run Time") == before:
            continue
        started = True
        code, _why_ = _schtasks_last_exit(job)
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
CREATE_REFUSED = ("NOT INSTALLED — schtasks /Create refused %s: %s. Nothing was registered and "
                  "no job file was kept.")


PROOF_WAIT = 60      # seconds to wait for the proof run (launchd: 30)


def _report_proof(job, code, sched, how):
    if code is not None and code in BENIGN_EXITS.get(job, {"0"}):
        print("gt-schedule: %s installed and PROVEN through %s — runs at %s, last exit %s"
              % (label_for(job), how, sched, code))
        return OK
    print("gt-schedule: installed but the proof run %s. Read %s and %s — a job that "
          "installs and never works is what this check exists to catch. Roll back with: "
          "gt_schedule.py remove %s"
          % ("reported no result" if code is None else "exited %s" % code,
             LOGS / ("%s.err" % job), LOGS / ("%s.out" % job), job), file=sys.stderr)
    return PROBLEM


def _install_linux(job, doc, sched):
    be, why = linux_backend()
    if be is None:
        print("gt-schedule: NOT INSTALLED — this Linux machine has no scheduler gt can use (%s). "
              "Install cron, or run under a systemd user session, then re-run." % why,
              file=sys.stderr)
        return PROBLEM
    doc["Backend"] = be
    label = doc["Label"]
    snap = _snapshot([spec_path(job)])
    try:
        write_spec(job, doc)
    except OSError as exc:
        print("gt-schedule: NOT INSTALLED — could not write %s (%s)" % (spec_path(job), exc),
              file=sys.stderr)
        return PROBLEM
    if be == "systemd":
        err = _systemd_register(job, doc)
    else:
        changed, err = _cron_set(job, label, cron_line(job, doc))
    if err:
        _restore(snap)
        print("gt-schedule: NOT INSTALLED — %s. Nothing was registered and no job file was kept."
              % err, file=sys.stderr)
        return PROBLEM
    print("gt-schedule: registered %s (%s)" % (label, "systemd --user timer" if be == "systemd"
                                               else "crontab line"))
    _remove_legacy(job)
    # THE SELF-PROOF, through the scheduler where there is one.
    if be == "systemd":
        run(["systemctl", "--user", "start", "%s.service" % label], timeout=max(PROOF_WAIT, 60) * 5)
        code, _w = _systemd_last_exit(label)
        return _report_proof(job, code, sched, "systemd --user")
    try:
        exit_path(job).unlink()
    except OSError:
        pass
    run(["sh", "-c", _shell_command(job, doc)], timeout=max(PROOF_WAIT, 60) * 5)
    code, _w = _cron_last_exit(job)
    return _report_proof(job, code, sched, "the crontab line's own command")


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

    kind = platform_kind()
    if kind == "linux":
        return _install_linux(job, doc, sched)

    if kind == "windows":
        # The same proof through Task Scheduler (0.20.0): register, run once, read the task's
        # own Last Result. A task that never reports one within the wait is NOT proven.
        code, err = _schedule_on_windows(job, doc)
        if err == NOT_STARTED:
            _remove_legacy(job)
            print("gt-schedule: %s is installed but NOT PROVEN. %s" % (label_for(job), err % job),
                  file=sys.stderr)
            return PROBLEM
        if err:
            print("gt-schedule: %s" % err, file=sys.stderr)
            return PROBLEM
        _remove_legacy(job)
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
    snap = _snapshot([path])
    atomic_write(path, plistlib.dumps(doc))

    run(["launchctl", "bootout", "%s/%s" % (domain(), label_for(job))])   # idempotent
    r = run(["launchctl", "bootstrap", domain(), str(path)])
    if r.returncode != 0:
        # NOT installed: the plist goes back to what it was (gone, or the previous job, which is
        # loaded again) so neither `list` nor the doctor reports a job launchd does not have.
        _restore(snap)
        again = ""
        if snap[path] is not None:
            rr = run(["launchctl", "bootstrap", domain(), str(path)])
            again = (" The previous job is back in place%s."
                     % ("" if rr.returncode == 0 else ", but launchd refused to load it again too"))
        print("gt-schedule: NOT INSTALLED — launchctl bootstrap refused %s: %s.%s"
              % (label_for(job), _why(r), again or " No plist was kept."), file=sys.stderr)
        return PROBLEM
    print("gt-schedule: wrote %s" % path)
    _remove_legacy(job)

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
    kind = platform_kind()
    script = JOBS[job][0]
    if kind == "windows":
        if not spec_path(job).is_file():
            problems.append("no task file at %s" % spec_path(job))
        if not wrapper_path(job).is_file():
            problems.append("no wrapper at %s" % wrapper_path(job))
        code, why = last_exit(job)
        if why:
            problems.append("Task Scheduler does not have it (%s)" % why)
        elif code is not None and code not in BENIGN_EXITS.get(job, {"0"}):
            problems.append("last result = %s (see %s)" % (code, LOGS / ("%s.err" % job)))
        if not (HOOKS / script).is_file():
            problems.append("%s is missing from %s" % (script, HOOKS))
        return code, problems
    if kind == "linux":
        path = job_file(job)
        if not path.is_file():
            problems.append("no job file at %s" % path)
        code, why = last_exit(job)
        if why:
            problems.append("%s — it never runs. Re-run install.sh (it converts it) or: "
                            "gt_schedule.py install %s ..." % (why, job))
        elif job_registered(job) is False:
            problems.append("%s is on disk but the scheduler does not have it registered — it "
                            "never runs. Re-install it: gt_schedule.py install %s ..."
                            % (path, job))
        elif code is not None and code not in BENIGN_EXITS.get(job, {"0"}):
            problems.append("last exit code = %s (see %s)" % (code, LOGS / ("%s.err" % job)))
        if not (HOOKS / script).is_file():
            problems.append("%s is missing from %s" % (script, HOOKS))
        return code, problems
    path = job_file(job)
    if not path.is_file():
        problems.append("no plist at %s" % path)
    code, why = last_exit(job)
    if why:
        problems.append("launchd does not have it loaded (%s)" % why)
    elif job_registered(job) is False:
        problems.append("%s is on disk but launchd does not have it loaded — it never runs"
                        % path)
    elif code is not None and code not in BENIGN_EXITS.get(job, {"0"}):
        # Read the error path out of the INSTALLED plist rather than guessing it: a job
        # installed by hand may log somewhere else entirely, and pointing someone at a file
        # that does not exist is worse than not pointing at all.
        err = LOGS / ("%s.err" % job)
        try:
            with path.open("rb") as fh:
                err = plistlib.load(fh).get("StandardErrorPath", str(err))
        except Exception:
            pass
        problems.append("last exit code = %s (see %s)" % (code, err))
        cause = privacy_cause(job, err)
        if cause:
            problems.append(cause)
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
    args = job_args(job)
    python = args[0] if args else "the job's interpreter"
    return ("macOS privacy blocked %s from %s (EPERM): give that interpreter Full Disk "
            "Access in System Settings > Privacy & Security, or re-run install.sh so every "
            "job uses the one recorded interpreter" % (python, folder))


def installed_jobs():
    """Jobs on this machine (a plist, or a spec on Windows/Linux -- whether the scheduler has
    them is job_registered's question, and job_status names one it does not). A job never
    installed is a choice, not a fault."""
    return [job for job in sorted(JOBS) if job_file(job).is_file()]


def do_check(a) -> int:
    job = a.job
    code, problems = job_status(job)
    label = installed_label(job)
    for p in problems:
        print("gt-schedule: %s — %s" % (label, p), file=sys.stderr)
    if not problems:
        print("gt-schedule: %s is wired, loaded, and last exited %s"
              % (label, code if code is not None else "(not yet run)"))
    return PROBLEM if problems else OK


# ---- remove: only what really happened is claimed (0.20.1) ------------------------------------

def _unregister_macos(job, label):
    did, problems = [], []
    path = plist_path(job, label)
    if _is_real_home():
        target = "%s/%s" % (domain(), label)
        if run(["launchctl", "print", target]).returncode == 0:
            r = run(["launchctl", "bootout", target])
            if r.returncode == 0:
                did.append("booted %s out of launchd" % label)
            else:
                problems.append("launchctl bootout %s failed: %s" % (label, _why(r)))
    if path.is_file():
        try:
            path.unlink()
            did.append("deleted %s" % path)
        except OSError as exc:
            problems.append("could not delete %s (%s)" % (path, exc))
    return did, problems


def _unregister_windows(job, label, files=True):
    did, problems = [], []
    if schtasks_query(job, label) is not None:
        r = run(["schtasks", "/Delete", "/TN", label, "/F"])
        if r.returncode == 0:
            did.append("removed %s from Task Scheduler" % label)
        else:
            problems.append("schtasks /Delete %s failed: %s" % (label, _why(r)))
    for p in ((spec_path(job), wrapper_path(job)) if files else ()):
        if p.is_file():
            try:
                p.unlink()
                did.append("deleted %s" % p)
            except OSError as exc:
                problems.append("could not delete %s (%s)" % (p, exc))
    return did, problems


def _unregister_linux(job, label, doc):
    did, problems = [], []
    if (doc or {}).get("Backend") == "cron" or not shutil.which("systemctl"):
        if shutil.which("crontab"):
            changed, why = _cron_set(job, label, None)
            if changed:
                did.append("removed the crontab line tagged %s%s" % (CRON_TAG, label))
            elif why:
                problems.append(why)
    if (doc or {}).get("Backend") != "cron":
        if shutil.which("systemctl") or any(p.is_file() for p in unit_paths(label)):
            d, p = _systemd_unregister(label)
            did += d
            problems += p
    return did, problems


def _remove_files(paths):
    did, problems = [], []
    for p in paths:
        if p.is_file():
            try:
                p.unlink()
                did.append("deleted %s" % p)
            except OSError as exc:
                problems.append("could not delete %s (%s)" % (p, exc))
    return did, problems


def _remove_job(job, include_new=True):
    """Unregister and delete every copy of `job` -- under the new label and the legacy one.
    -> (did, problems)."""
    did, problems = [], []
    kind = platform_kind()
    if kind == "macos":
        for label in ([label_for(job)] if include_new else []) + [legacy_label_for(job)]:
            d, p = _unregister_macos(job, label)
            did += d
            problems += p
    elif kind == "windows":
        # Removing the job: the new label, and whatever label its spec names. After an install
        # under the new label (include_new=False): the legacy task, whose spec was just replaced.
        if include_new:
            labels = [label_for(job)]
            doc = _read_file_doc(spec_path(job)) if spec_path(job).is_file() else None
            lab = (doc or {}).get("Label") if isinstance(doc, dict) else None
            if lab in (legacy_label_for(job),):
                labels.append(lab)
        else:
            labels = [legacy_label_for(job)]
        for label in labels:
            d, p = _unregister_windows(job, label, files=False)
            did += d
            problems += p
        if include_new:
            d, p = _remove_files([spec_path(job), wrapper_path(job)])
            did += d
            problems += p
    else:
        if include_new:
            doc = _read_file_doc(spec_path(job)) if spec_path(job).is_file() else None
            d, p = _unregister_linux(job, label_for(job), doc)
            did += d
            problems += p
            d, p = _remove_files([spec_path(job)])
            did += d
            problems += p
        d, p = _remove_files([legacy_plist_path(job)])
        did += d
        problems += p
    return did, problems


def _remove_legacy(job):
    """After the job is registered under the new label: the old-label copy goes (said)."""
    did, problems = _remove_job(job, include_new=False)
    for d in did:
        print("gt-schedule: %s (the old %s label)" % (d, LEGACY_LABEL_PREFIX + "*"))
    for p in problems:
        print("gt-schedule: ⚠ %s" % p, file=sys.stderr)


def do_remove(a) -> int:
    job = a.job
    did, problems = _remove_job(job)
    for d in did:
        print("gt-schedule: %s" % d)
    for p in problems:
        print("gt-schedule: could not remove %s cleanly — %s" % (label_for(job), p),
              file=sys.stderr)
    if not did and not problems:
        print("gt-schedule: nothing to remove for %s" % label_for(job))
    return PROBLEM if problems else OK


# ---- the label migration (0.20.1) -------------------------------------------------------------

def _legacy_installed(job):
    """-> the legacy job's file and doc, or (None, None)."""
    kind = platform_kind()
    if kind == "windows":
        p = spec_path(job)
        doc = _read_file_doc(p) if p.is_file() else None
        if isinstance(doc, dict) and doc.get("Label") == legacy_label_for(job):
            return p, doc
        return None, None
    for p in (legacy_plist_path(job),):
        if p.is_file():
            doc = _read_file_doc(p)
            if isinstance(doc, dict):
                return p, doc
            return p, None
    return None, None


def _migrate_one(job):
    """-> (line or None, ok)."""
    kind = platform_kind()
    old, doc = _legacy_installed(job)
    if old is None:
        return None, True
    new_label, old_label = label_for(job), legacy_label_for(job)
    if doc is None or not isinstance(doc.get("ProgramArguments"), list) \
            or not isinstance(doc.get("StartCalendarInterval"), dict):
        return ("⚠ %s: %s is unreadable or incomplete; left as it is — re-create the job: "
                "gt_schedule.py install %s ..." % (job, old, job)), False
    new = dict(doc)
    new["Label"] = new_label
    new.setdefault("StandardOutPath", str(LOGS / ("%s.out" % job)))
    new.setdefault("StandardErrorPath", str(LOGS / ("%s.err" % job)))
    if kind == "macos":
        target = plist_path(job)
        if target.is_file():
            did, problems = _unregister_macos(job, old_label)
            if problems:
                return "⚠ %s: %s" % (job, "; ".join(problems)), False
            return "%s: removed the leftover %s (already installed as %s)" % (
                job, old_label, new_label), True
        if not _is_real_home():
            return ("%s: not migrated from %s — this is not the account's own launchd domain "
                    "(a test, a sandbox or another HOME)" % (job, old_label)), True
        try:
            atomic_write(target, plistlib.dumps(new))
        except OSError as exc:
            return "⚠ %s: could not write %s (%s); %s left as it was" % (
                job, target, exc, old_label), False
        r = run(["launchctl", "bootstrap", domain(), str(target)])
        if r.returncode != 0:
            try:
                target.unlink()
            except OSError:
                pass
            return ("⚠ %s: launchd refused %s (%s); %s left exactly as it was"
                    % (job, new_label, _why(r), old_label)), False
        did, problems = _unregister_macos(job, old_label)
        return ("%s: %s → %s%s" % (job, old_label, new_label,
                                   "" if not problems else " (⚠ %s)" % "; ".join(problems))), \
            not problems
    if kind == "windows":
        r = run(schtasks_create_args(job, new, new_label))
        if r.returncode != 0:
            return ("⚠ %s: schtasks /Create %s refused (%s); %s left exactly as it was"
                    % (job, new_label, _why(r), old_label)), False
        try:
            write_task_files(job, new)
        except OSError as exc:
            return "⚠ %s: could not rewrite %s (%s)" % (job, spec_path(job), exc), False
        did, problems = _unregister_windows(job, old_label, files=False)
        return ("%s: %s → %s%s" % (job, old_label, new_label,
                                   "" if not problems else " (⚠ %s)" % "; ".join(problems))), \
            not problems
    # Linux: 0.20.0 wrote a launchd plist that nothing ever ran. Register the job for real.
    be, why = linux_backend()
    if be is None:
        return ("⚠ %s: %s is a launchd plist an earlier gt wrote (Linux never ran it), and "
                "there is no scheduler to move it to (%s); left as it is" % (job, old, why)), False
    new["Backend"] = be
    snap = _snapshot([spec_path(job)])
    try:
        write_spec(job, new)
    except OSError as exc:
        return "⚠ %s: could not write %s (%s)" % (job, spec_path(job), exc), False
    if be == "systemd":
        err = _systemd_register(job, new)
    else:
        _c, err = _cron_set(job, new_label, cron_line(job, new))
    if err:
        _restore(snap)
        return ("⚠ %s: %s could not be registered (%s); %s left as it is"
                % (job, new_label, err, old)), False
    d, p = _remove_files([legacy_plist_path(job)])
    return ("%s: %s (a launchd plist Linux never ran) → %s %s"
            % (job, old, new_label, "systemd --user timer" if be == "systemd"
               else "crontab line")), not p


def migrate_labels():
    """-> (rc, [line]). Idempotent: nothing installed under the old label is nothing to do."""
    lines, rc = [], OK
    for job in sorted(JOBS):
        line, ok = _migrate_one(job)
        if line:
            lines.append(line)
        if not ok:
            rc = PROBLEM
    return rc, lines


def do_migrate_labels(_a) -> int:
    rc, lines = migrate_labels()
    for l in lines:
        print("gt-schedule: %s" % l, file=sys.stderr if l.startswith("⚠") else sys.stdout)
    if not lines:
        print("gt-schedule: no job under the old %s* label — nothing to migrate"
              % LEGACY_LABEL_PREFIX)
    return rc


def _record(p, refused=None):
    INTERPRETER_RECORD.parent.mkdir(parents=True, exist_ok=True)
    doc = {"python": p, "recorded": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    if refused:
        doc["refused"] = list(refused)
    atomic_write(INTERPRETER_RECORD, (json.dumps(doc) + "\n").encode("utf-8"))


def do_record_interpreter(a) -> int:
    p = os.path.abspath(os.path.expanduser(a.python))
    if not (os.path.isfile(p) and os.access(p, os.X_OK)):
        print("gt-schedule: %s is not an executable interpreter; nothing recorded" % p,
              file=sys.stderr)
        return USAGE
    _record(p, recorded_refused())
    print("gt-schedule: every job runs %s" % p)
    return OK


# ---- choosing the interpreter (0.20.0) ---------------------------------------------------------
#
# 2026-10-03, on the publishing Mac: install.sh recorded Homebrew's python3.9 (the python it ran
# under), and under launchd that interpreter got PermissionError writing the vault -- macOS
# privacy grants are per interpreter, and the one the user had granted was /usr/bin/python3.
# The jobs had been repaired by hand the night before; the 0.19.2 install undid it.

SYSTEM_PYTHON = "/usr/bin/python3"
XCODE_SELECT = "/usr/bin/xcode-select"     # exits 0 only when the Command Line Tools are there
PROBE_LABEL = "io.goldenthread.gt-probe"
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
            code, _why_ = _launchd_last_exit(PROBE_LABEL)
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
    return (sys.platform == "darwin" and not on_windows() and bool(vault)
            and Path(vault).is_dir() and bool(installed_jobs()) and _is_real_home()
            and shutil.which("launchctl") is not None)


def writes_ok(py, vault):
    """The write probe (gt_write_probe.py), run BY `py`: create, write, os.replace, remove in
    the vault and in gt's home, and open the vault's log.md for append. -> True / False.
    macOS only (0.20.0): a file carrying com.apple.provenance refuses some interpreters --
    Homebrew's python3.9 got EPERM replacing log.md where /usr/bin/python3 did not."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import gt_write_probe as wp
    spots = [(vault, os.path.join(vault, "log.md"))] if vault and os.path.isdir(vault) else []
    if LOGS.is_dir():
        spots.append((str(LOGS), None))
    return all(wp.probe_one(py, d, f) == "pass" for d, f in spots)


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
    # 0.20.1: what the write probe refused is recorded beside the choice, so install.sh can tell
    # whether a bare `python3` would be refused (and a session then needs gt's python3 first on
    # PATH). Kept from the record when the probe did not run this time.
    refused = recorded_refused()
    # ONE interpreter for hooks, tools and jobs (0.20.0): on macOS it must also pass the write
    # probe in a normal process, not only under launchd. Never from a test/sandbox: the vault
    # it would name is the real one (HOME here is the module's, not the sandbox's).
    if sys.platform == "darwin" and not on_windows() and not a.no_probe and not sandboxed():
        writable = [c for c in cands if writes_ok(c, vault)]
        refused = [c for c in cands if c not in writable]
        for c in refused:
            print("gt-schedule: %s cannot write where gt writes (macOS refused it: a "
                  "com.apple.provenance file or privacy protection) -- passed over" % c)
        if writable:
            cands = writable
        else:
            print("gt-schedule: no candidate passed the write probe; keeping %s. Give it Full "
                  "Disk Access in System Settings > Privacy & Security" % cands[0],
                  file=sys.stderr)
            rc = PROBLEM
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
    refused = [c for c in refused if c != chosen]
    if chosen != rec or refused != recorded_refused():
        _record(chosen, refused)
    print("gt-schedule: every job runs %s (%s)" % (chosen, why))
    return rc


def _is_real_home():
    """True only when the scheduler files this run would write are EXACTLY the account's own
    and no sandbox marker is set: on macOS <pwd home>/Library/LaunchAgents (realpath-compared),
    elsewhere HOME itself.

    Until 0.20.0 any HOME *inside* the real home counted as the real user, so a sandbox or test
    install under it -- test_tmpdir=noindex puts every test HOME in
    ~/Library/Caches/gt-tests.noindex -- would bootout/bootstrap the developer's real jobs."""
    if sandboxed():
        return False
    try:
        import pwd
        real = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return False
    if platform_kind() == "macos":
        return (os.path.realpath(str(AGENTS))
                == os.path.realpath(os.path.join(real, "Library", "LaunchAgents")))
    return os.path.realpath(str(HOME)) == real


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
             installed_label(job), have, job, have),
          file=sys.stderr)


def do_reconcile(a) -> int:
    """Move any job off the old label (migrate-labels), then rewrite any installed job whose
    interpreter is not the recorded one; reload it. A rewrite that fails (EPERM included)
    leaves that job as it was -- never removed."""
    rc = OK
    mrc, lines = migrate_labels()
    for l in lines:
        print("gt-schedule: %s" % l, file=sys.stderr if l.startswith("⚠") else sys.stdout)
    want = recorded_interpreter()
    if not want:
        print("gt-schedule: no recorded interpreter; jobs left as they are")
        return mrc
    kind = platform_kind()
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
        label = installed_label(job)
        try:
            if path.suffix == ".plist":
                atomic_write(path, plistlib.dumps(doc))
            elif kind == "windows":
                # The task runs the wrapper, so rewriting it is the whole change.
                write_task_files(job, doc)
            else:
                write_spec(job, doc)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            # A write refused (EPERM), or a job description missing a field the rewrite needs:
            # either way the job is left exactly as it was, and said so in words.
            _cannot_rewrite(job, path, exc, have)
            rc = PROBLEM
            continue
        print("gt-schedule: %s now runs %s (was %s)" % (label, want, have))
        # Reload only the REAL user's jobs: launchd's domain is gui/<uid> whatever HOME says,
        # so a sandbox HOME (a test, a throwaway install) reloading would replace the
        # developer's real job with the sandbox's plist.
        if a.no_reload or not _is_real_home():
            continue
        if kind == "macos":
            run(["launchctl", "bootout", "%s/%s" % (domain(), label)])
            r = run(["launchctl", "bootstrap", domain(), str(path)])
            if r.returncode != 0:
                print("gt-schedule: reload of %s failed: %s"
                      % (label, (r.stderr or r.stdout).strip()), file=sys.stderr)
                rc = PROBLEM
        elif kind == "linux" and path.suffix == ".json":
            if doc.get("Backend") == "cron":
                _c, err = _cron_set(job, label, cron_line(job, doc))
            else:
                err = _systemd_register(job, doc)
            if err:
                print("gt-schedule: reload of %s failed: %s" % (label, err), file=sys.stderr)
                rc = PROBLEM
    return rc if rc != OK else mrc


def do_list(_a) -> int:
    print("%-14s %-20s %-8s %s" % ("JOB", "SCRIPT", "DEFAULT", "WHAT"))
    for job, (script, h, m, w, what) in sorted(JOBS.items()):
        when = "%02d:%02d" % (h, m) + (" wd%d" % w if w else "")
        if job_file(job).is_file():
            state = "installed" if job_registered(job) is not False \
                else "on disk, NOT registered"
        else:
            state = "-"
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
    rc = sub.add_parser("reconcile", help="migrate old labels, rewrite jobs on another "
                                          "interpreter, then reload")
    rc.add_argument("--no-reload", action="store_true", help="rewrite the job files only")
    rc.set_defaults(fn=do_reconcile)
    sub.add_parser("migrate-labels", help="move jobs from the old com.markethaven.gt-* label to "
                                          "io.goldenthread.gt-* (install.sh)"
                   ).set_defaults(fn=do_migrate_labels)
    for name, fn in (("install", do_install), ("check", do_check), ("remove", do_remove)):
        p = sub.add_parser(name)
        p.add_argument("job", choices=sorted(JOBS))
        p.add_argument("--vault")
        p.add_argument("--repo", action="append", default=[])
        p.add_argument("--hour", type=int)
        p.add_argument("--minute", type=int)
        p.set_defaults(fn=fn)
    a = ap.parse_args(argv)
    # Native Windows: 0.19.2 refused install/check/remove here (no launchd); since 0.20.0 they
    # run on Task Scheduler (schtasks), per user and without admin. Linux (0.20.1): systemd
    # --user or cron.
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
