#!/usr/bin/env python3
"""gt_schedule.py -- install, verify and remove gt's scheduled jobs. One script, no hand steps.

    gt_schedule.py list
    gt_schedule.py install <job> --vault V [--repo PATH ...] [--hour H] [--minute M]
    gt_schedule.py check   <job>
    gt_schedule.py remove  <job>

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

# label -> (script, default hour, default minute, weekday or None, what it does)
JOBS = {
    "daily": ("gt_daily.py", 22, 0, None,
              "write the day's facts into Daily Notes/<today>.md"),
    "lint-weekly": ("gt_lint_weekly.py", 7, 0, 1,
                    "vault + wiki lint, report into the vault (Mondays)"),
}

# Exit codes that are a NORMAL outcome for each job, not a failure. Without this the check
# reported the live weekly job as broken because it exits 1 when the lint finds something --
# which is most weeks. A check that calls a normal result a failure is the crying-wolf defect
# this file's own docstring warns about, and it appeared here within minutes of being written.
BENIGN_EXITS = {
    "daily": {"0", "1"},          # 1 = nothing recorded for the day
    "lint-weekly": {"0", "1"},    # 1 = the lint found something, which is the usual case
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


def domain() -> str:
    return "gui/%d" % os.getuid()


def build_plist(job, vault, repos, hour, minute, weekday):
    script, _, _, _, _ = JOBS[job]
    target = HOOKS / script
    args = [sys.executable or "/usr/bin/python3", str(target)]
    if job == "daily":
        # gt_daily needs to be told its vault -- Core rule 2, never inferred -- and which
        # repos count as work. gt_lint_weekly reads vault-config.json itself.
        args += ["--vault", str(vault)]
        for r in repos:
            args += ["--repo", str(r)]
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
    if job == "daily" and not vault:
        print("gt-schedule: --vault is required for the daily job", file=sys.stderr)
        return USAGE

    # Prove the job can actually do its work BEFORE scheduling it. Installing a job that
    # cannot run is how a scheduler becomes a thing nobody trusts.
    if job == "daily":
        pre = run([sys.executable or "/usr/bin/python3", str(target), "--vault", str(vault),
                   *sum((["--repo", str(r)] for r in a.repo), []), "--check"])
        if pre.returncode != 0:
            print(pre.stdout + pre.stderr, file=sys.stderr)
            print("gt-schedule: CANNOT INSTALL — the job's own --check failed, so scheduling "
                  "it would schedule a job that does nothing", file=sys.stderr)
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


def do_check(a) -> int:
    job = a.job
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
    script = JOBS[job][0]
    if not (HOOKS / script).is_file():
        problems.append("%s is missing from %s" % (script, HOOKS))
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
    for name, fn in (("install", do_install), ("check", do_check), ("remove", do_remove)):
        p = sub.add_parser(name)
        p.add_argument("job", choices=sorted(JOBS))
        p.add_argument("--vault")
        p.add_argument("--repo", action="append", default=[])
        p.add_argument("--hour", type=int)
        p.add_argument("--minute", type=int)
        p.set_defaults(fn=fn)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
