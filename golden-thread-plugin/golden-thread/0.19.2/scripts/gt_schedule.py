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


def run(args, timeout=180):
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


def last_exit(job):
    r = run(["launchctl", "print", "%s/%s" % (domain(), label_for(job))])
    if r.returncode != 0:
        return None, "not loaded"
    code = None
    for line in r.stdout.splitlines():
        if "last exit code" in line:
            value = line.split("=", 1)[1].strip()
            code = None if value in ("(never exited)", "-") else value
    return code, None


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

    AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    doc = build_plist(job, vault, a.repo, a.hour if a.hour is not None else dh,
                      a.minute if a.minute is not None else dm, dw)
    path = plist_path(job)
    with path.open("wb") as fh:
        plistlib.dump(doc, fh)
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

    when = doc["StartCalendarInterval"]
    sched = "%02d:%02d" % (when["Hour"], when["Minute"])
    if "Weekday" in when:
        sched += " weekday %d" % when["Weekday"]
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
    path = plist_path(job)
    problems = []
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
    """Jobs with a plist on this machine. A job never installed is a choice, not a fault."""
    return [job for job in sorted(JOBS) if plist_path(job).is_file()]


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
    run(["launchctl", "bootout", "%s/%s" % (domain(), label_for(job))])
    path = plist_path(job)
    if path.is_file():
        path.unlink()
        print("gt-schedule: removed %s and booted the job out" % path)
    else:
        print("gt-schedule: nothing to remove for %s" % label_for(job))
    return OK


def do_record_interpreter(a) -> int:
    p = os.path.abspath(os.path.expanduser(a.python))
    if not (os.path.isfile(p) and os.access(p, os.X_OK)):
        print("gt-schedule: %s is not an executable interpreter; nothing recorded" % p,
              file=sys.stderr)
        return USAGE
    INTERPRETER_RECORD.parent.mkdir(parents=True, exist_ok=True)
    tmp = INTERPRETER_RECORD.with_name(INTERPRETER_RECORD.name + ".tmp")
    tmp.write_text(json.dumps({"python": p, "recorded": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
                   + "\n", encoding="utf-8")
    os.replace(tmp, INTERPRETER_RECORD)
    print("gt-schedule: every job runs %s" % p)
    return OK


def _is_real_home():
    try:
        import pwd
        real = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except Exception:
        return False
    return AGENTS.resolve().is_relative_to(real) if hasattr(Path, 'is_relative_to') \
        else str(AGENTS.resolve()).startswith(str(real) + os.sep)


def job_interpreter(job):
    """The interpreter an installed job's plist runs, or None."""
    try:
        with plist_path(job).open("rb") as fh:
            return plistlib.load(fh)["ProgramArguments"][0]
    except Exception:
        return None


def do_reconcile(a) -> int:
    """Rewrite any installed job whose interpreter is not the recorded one; reload it."""
    want = recorded_interpreter()
    if not want:
        print("gt-schedule: no recorded interpreter; jobs left as they are")
        return OK
    rc = OK
    for job in installed_jobs():
        path = plist_path(job)
        try:
            with path.open("rb") as fh:
                doc = plistlib.load(fh)
            have = doc["ProgramArguments"][0]
        except Exception as exc:
            print("gt-schedule: %s unreadable (%s); left alone" % (path, exc.__class__.__name__),
                  file=sys.stderr)
            rc = PROBLEM
            continue
        if have == want:
            continue
        doc["ProgramArguments"][0] = want
        with path.open("wb") as fh:
            plistlib.dump(doc, fh)
        print("gt-schedule: %s now runs %s (was %s)" % (label_for(job), want, have))
        # Reload only the REAL user's jobs: launchd's domain is gui/<uid> whatever HOME says,
        # so a sandbox HOME (a test, a throwaway install) reloading would replace the
        # developer's real job with the sandbox's plist.
        if not a.no_reload and _is_real_home():
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
        state = "installed" if plist_path(job).is_file() else "-"
        print("%-14s %-20s %-8s %s  [%s]" % (job, script, when, what, state))
    return OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="install and verify gt's scheduled jobs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=do_list)
    ri = sub.add_parser("record-interpreter", help="the one python every job runs (install.sh)")
    ri.add_argument("python")
    ri.set_defaults(fn=do_record_interpreter)
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
    # Native Windows (0.19.2): jobs are launchd jobs, and there is no launchd -- os.getuid()
    # does not even exist there, so these three died on a traceback. Refused in words instead.
    # list, record-interpreter and reconcile still work: with no plists they change nothing,
    # and install.sh runs the last two on every install.
    if os.name == "nt" and a.cmd in ("install", "check", "remove"):
        print("gt-schedule: scheduled jobs need macOS launchd and are not available on "
              "Windows. Run the job by hand, or schedule it yourself with Task Scheduler: "
              "%s %s" % (interpreter(), HOOKS / JOBS[a.job][0]), file=sys.stderr)
        return PROBLEM
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
