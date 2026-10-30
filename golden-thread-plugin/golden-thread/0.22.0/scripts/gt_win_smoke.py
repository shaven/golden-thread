#!/usr/bin/env python3
"""gt_win_smoke -- the Windows security smoke, one script for one gt-win VM (gt 0.22.0, phase 122 F8).

Run it on a Windows machine, in a console, as the signed-in user:

    python gt_win_smoke.py [--json] [--host NAME]

It starts the unlock authority if it is not running, then runs six checks and prints a receipt the CHANGELOG
test-counts line can cite:

  pipe-sddl        the named pipe's DACL is exactly "protected, this user's SID only"
  ssh-presence     over SSH the lock probe reads (not locked, no desktop): presence would be false
  second-user      a second local user cannot connect to the pipe (needs GT_WIN_SMOKE_OTHER_USER and
                   GT_WIN_SMOKE_OTHER_PASSWORD in the environment; SKIP without them)
  acl-unlock-home  the unlock home's ACL lists only this user (FAIL when open)
  acl-lotr-home    the same for each LOTR home (WARN when open)
  hello-selftest   gt_unlock_hello.py selftest with a throwaway key (a person answers the Hello prompt)

PASS / WARN / FAIL / SKIP per check. Exit 0 when nothing FAILed and something ran, 1 on any FAIL, 2 when this
is not Windows (nothing ran; nothing is claimed). Every Windows call sits behind an injected seam in
run_checks(), so the logic is unit-tested off Windows (tests/test_win_security.py); the REAL seams below are
what only a Windows run proves. No secret, SID or password is ever printed or written.
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

NAMES = ("pipe-sddl", "ssh-presence", "second-user", "acl-unlock-home", "acl-lotr-home", "hello-selftest")


def _r(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def _guard(name, fn):
    """A check that crashes is a FAIL, never a silent pass."""
    try:
        return fn()
    except Exception as exc:                                  # noqa: BLE001
        return _r(name, "FAIL", "the check crashed: %s" % type(exc).__name__)


def check_sddl(own_sid, sddl):
    s = sddl()
    aces = re.findall(r"\(([^)]*)\)", s)
    only_me = len(aces) == 1 and aces[0].split(";")[-1] == own_sid() and aces[0].startswith("A;")
    if s.startswith("D:P") and only_me:
        return _r("pipe-sddl", "PASS", "protected DACL, one allow entry, this user's SID")
    return _r("pipe-sddl", "FAIL", "the pipe's DACL is not 'protected, this user only' (%d entries)" % len(aces))


def check_presence(probe, over_ssh):
    if not over_ssh:
        return _r("ssh-presence", "SKIP", "not an SSH session: run this check from one")
    locked, seen = probe()
    if locked or seen:
        return _r("ssh-presence", "FAIL", "over SSH the probe read locked=%s desktop_seen=%s" % (locked, seen))
    return _r("ssh-presence", "PASS", "over SSH: no desktop seen, not locked, so presence answers no")


def check_second_user(other_user):
    if other_user is None:
        return _r("second-user", "SKIP", "no second local user configured")
    refused, detail = other_user()
    return _r("second-user", "PASS" if refused else "FAIL", detail)


def check_acl(name, folders, acl, bad):
    if not folders:
        return _r(name, "SKIP", "no such folder exists yet")
    worst, notes = "PASS", []
    for f in folders:
        state, why = acl(f)
        if state == "open":
            worst = bad
            notes.append(why)
        elif state != "ok" and worst == "PASS":
            worst = "FAIL" if bad == "FAIL" else "WARN"
            notes.append(why)
    return _r(name, worst, "; ".join(notes) or "owner only")


def check_hello(hello):
    rc, out = hello()
    return _r("hello-selftest", "PASS" if rc == 0 else "FAIL",
              "selftest exited 0" if rc == 0 else "selftest exited %s" % rc)


def run_checks(platform, own_sid, sddl, acl, homes, probe, over_ssh, other_user, hello):
    if platform != "nt":
        return [_r("platform", "SKIP", "not Windows: nothing ran and nothing is claimed")]
    unlock, lotrs = homes()
    return [
        _guard("pipe-sddl", lambda: check_sddl(own_sid, sddl)),
        _guard("ssh-presence", lambda: check_presence(probe, over_ssh)),
        _guard("second-user", lambda: check_second_user(other_user)),
        _guard("acl-unlock-home", lambda: check_acl("acl-unlock-home", [unlock], acl, "FAIL")),
        _guard("acl-lotr-home", lambda: check_acl("acl-lotr-home", lotrs, acl, "WARN")),
        _guard("hello-selftest", lambda: check_hello(hello)),
    ]


def exit_code(results):
    if any(r["status"] == "FAIL" for r in results):
        return 1
    if not any(r["status"] in ("PASS", "WARN") for r in results):
        return 2
    return 0


def receipt(results, host="", when=""):
    n = {s: sum(1 for r in results if r["status"] == s) for s in ("PASS", "WARN", "FAIL", "SKIP")}
    lines = ["gt Windows security smoke %s%s" % (when, " on " + host if host else "")]
    lines += ["  %-4s %-16s %s" % (r["status"], r["name"], r["detail"]) for r in results]
    lines.append("%d PASS, %d WARN, %d FAIL, %d SKIP" % (n["PASS"], n["WARN"], n["FAIL"], n["SKIP"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- the real seams (Windows only; unproven off it)

def _real_seams():
    import gt_ipc
    import gt_private
    import gt_unlock_client as C
    import gt_unlock_policy as P
    import gt_unlockd as D

    def sddl():
        C.start_daemon()
        path = C.address()
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
               "(Get-Acl -LiteralPath '%s').Sddl" % path.replace("'", "''")]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()

    def homes():
        env = os.environ.get("LOTR_HOME")
        base = os.path.join(os.path.expanduser("~"), ".config", "gt-lotr")
        lotrs = [os.path.expanduser(env)] if env else (
            [os.path.join(base, d) for d in sorted(os.listdir(base))] if os.path.isdir(base) else [])
        return P.unlock_home(), [d for d in lotrs if os.path.isdir(d)]

    def other_user():
        user, pw = os.environ.get("GT_WIN_SMOKE_OTHER_USER"), os.environ.get("GT_WIN_SMOKE_OTHER_PASSWORD")
        if not user or not pw:
            return None
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import gt_ipc; "
                "sys.exit(0 if gt_ipc.alive_at(sys.argv[2], timeout=2) else 3)")
        ps = ("$p = ConvertTo-SecureString $env:GT_WIN_SMOKE_OTHER_PASSWORD -AsPlainText -Force; "
              "$c = New-Object PSCredential($env:GT_WIN_SMOKE_OTHER_USER, $p); "
              "$r = Start-Process -FilePath '%s' -ArgumentList @('-I','-c',$env:GT_WIN_SMOKE_CODE,'%s','%s') "
              "-Credential $c -Wait -PassThru -WindowStyle Hidden; exit $r.ExitCode"
              % (sys.executable.replace("'", "''"), HERE.replace("'", "''"), C.address().replace("'", "''")))
        env = dict(os.environ, GT_WIN_SMOKE_CODE=code)
        rc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                            env=env, capture_output=True, timeout=60).returncode
        return (rc == 3, "the second user was refused" if rc == 3 else "the second user got exit %s" % rc)

    def hello():
        p = subprocess.run([sys.executable, os.path.join(HERE, "gt_unlock_hello.py"), "selftest"],
                           capture_output=True, text=True, timeout=300)
        return p.returncode, p.stdout

    return dict(
        platform=os.name, own_sid=gt_ipc.own_sid, sddl=sddl,
        acl=gt_private.acl_state, homes=homes,
        probe=lambda: (D.screen_locked_now(), D.desktop_seen_now()),
        over_ssh=bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT")),
        other_user=other_user if os.environ.get("GT_WIN_SMOKE_OTHER_USER") else None,
        hello=hello)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_win_smoke", description="Windows security smoke for one gt-win VM")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--host", default=os.environ.get("COMPUTERNAME", ""))
    a = ap.parse_args(argv)
    seams = _real_seams() if os.name == "nt" else dict(
        platform=os.name, own_sid=None, sddl=None, acl=None, homes=None, probe=None, over_ssh=False,
        other_user=None, hello=None)
    results = run_checks(**seams)
    import datetime
    when = datetime.date.today().isoformat()
    if a.json:
        print(json.dumps({"host": a.host, "date": when, "results": results}, indent=2))
    else:
        print(receipt(results, host=a.host, when=when))
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
