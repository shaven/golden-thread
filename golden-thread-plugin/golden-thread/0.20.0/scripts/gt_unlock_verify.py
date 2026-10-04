#!/usr/bin/env python3
"""gt_unlock_verify -- `gt_unlock.py verify`: the safe subset of the red-team checks, run on
THIS machine against what is installed (0.20.0).

Each check prints PASS, FAIL or NOT-CHECKED with its reason, and the run starts with the level
the machine actually runs at (off / L1 / L2; L3 needs an admin service install, which 0.20.0
does not ship). It never prints "secure": a FAIL is a defect to fix, a NOT-CHECKED is a
question this run could not answer, and neither is hidden behind a summary.

Safe means: read-only except for one throwaway child process that asks the authority for a
scope it must refuse. No prompt is raised (request=False everywhere), no grant is created, no
secret is read. Exit 0 = no FAIL (NOT-CHECKED rows may remain), 1 = at least one FAIL.
"""
import json
import os
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_ipc                      # noqa: E402
import gt_unlock_client as C       # noqa: E402
import gt_unlock_policy as P       # noqa: E402

IS_WINDOWS = os.name == "nt"
PASS, FAIL, NC = "PASS", "FAIL", "NOT-CHECKED"


class Run:
    def __init__(self):
        self.rows = []

    def add(self, name, state, why):
        self.rows.append({"check": name, "state": state, "why": why})


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def check_files(r, home):
    if IS_WINDOWS:
        r.add("files", NC, "Windows has no mode bits; the unlock home sits in your profile, "
                           "whose ACL admits only you, SYSTEM and Administrators")
        return
    bad = []
    if os.path.isdir(home) and _mode(home) & 0o077:
        bad.append("%s is %o (want 700)" % (home, _mode(home)))
    for name in ("policy.json", "enrolment.json", "state.json", "totp.seed", "recovery.json",
                 "audit.jsonl"):
        p = os.path.join(home, name)
        if os.path.exists(p) and _mode(p) & 0o077:
            bad.append("%s is %o (want 600)" % (name, _mode(p)))
    r.add("files", FAIL if bad else PASS,
          "; ".join(bad) if bad else "unlock home 700, its files 600")


def check_transport(r, home):
    addr = C.address(home)
    if IS_WINDOWS:
        ps = ("$a = [System.IO.Directory]::GetAccessControl('%s'); "
              "$a.GetSecurityDescriptorSddlForm('Access')" % addr)
        try:
            out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                  ps], capture_output=True, text=True, timeout=60)
            sddl = out.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            sddl = ""
        if not sddl:
            r.add("transport", NC, "could not read the pipe's ACL")
            return
        broad = [b for b in ("WD", "AN", "BU", "AU", "S-1-1-0") if ";;;%s)" % b in sddl]
        r.add("transport", FAIL if broad or gt_ipc.own_sid() not in sddl else PASS,
              "pipe ACL grants %s" % ", ".join(broad) if broad else
              "pipe ACL admits only your SID")
        return
    if not os.path.exists(addr):
        r.add("transport", NC, "the authority's socket does not exist (not running)")
        return
    m, dm = _mode(addr), _mode(os.path.dirname(addr))
    ok = not (m & 0o077) and not (dm & 0o077)
    r.add("transport", PASS if ok else FAIL,
          "socket %o in a %o directory" % (m, dm) + ("" if ok else " (want 600 / 700)"))


def check_door(r, home):
    """A process started from THIS one asks for a LOTR scope without a prompt. Inside a Claude
    Code session that process is the session's shell child, so under mcp_only it must be
    refused; outside one there is nothing to prove."""
    code = ("import sys, json; sys.path.insert(0, %r); import gt_unlock_client as C; "
            "print(json.dumps(C.check('lotr:verify:read', h=%r, start=False)))" % (HERE, home))
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             timeout=30)
        v = json.loads(out.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        r.add("mcp-door", NC, "the probe process could not ask the authority")
        return
    if v.get("code") == "mcp_only":
        r.add("mcp-door", PASS, "a process from this session's shell is refused LOTR "
                                "(door: mcp_only)")
    elif v.get("code") in ("unreachable",):
        r.add("mcp-door", NC, "the authority is not running")
    elif not os.environ.get("CLAUDECODE"):
        r.add("mcp-door", NC, "not run from inside a Claude Code session, so the door the "
                              "assistant's shell meets was not exercised (got %s)" % v.get("code"))
    else:
        r.add("mcp-door", FAIL, "a shell child of this session was answered %s (%s), not "
                                "refused" % (v.get("code"), v.get("message")))


def check_gated_refused(r, home, status):
    code = ("import sys, json; sys.path.insert(0, %r); import gt_unlock_client as C; "
            "print(json.dumps(C.check('gt:unlock:policy', h=%r, start=False)))" % (HERE, home))
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             timeout=30)
        v = json.loads(out.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        r.add("step-up", NC, "the probe process could not ask the authority")
        return
    if not v.get("allowed"):
        r.add("step-up", PASS, "a policy change without a fresh confirmation is refused (%s)"
              % v.get("code"))
    else:
        r.add("step-up", FAIL, "gt:unlock:policy was granted without a fresh confirmation")


def check_helper(r):
    if sys.platform != "darwin":
        r.add("touchid-helper", NC, "macOS only")
        return
    path = os.path.expanduser("~/.claude/golden-thread/bin/gt-presence")
    if not os.path.exists(path):
        r.add("touchid-helper", NC, "no Touch ID helper installed (TOTP only: L1)")
        return
    st = os.stat(path)
    if st.st_mode & 0o022 or st.st_uid not in (os.getuid(), 0):
        r.add("touchid-helper", FAIL, "%s is writable by others or not yours" % path)
        return
    try:
        out = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", path],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        r.add("touchid-helper", NC, "codesign could not run")
        return
    if out.returncode != 0:
        r.add("touchid-helper", FAIL, "the helper's code signature does not verify")
        return
    r.add("touchid-helper", PASS, "helper present, yours, signature verifies (a forged helper "
                                  "still cannot sign: the authority checks every signature)")


def check_hooks(r):
    p = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        r.add("hooks", NC, "no ~/.claude/settings.json")
        return
    need = ["guard_protected_paths", "gt_unlock.py"]
    missing = [n for n in need if n not in text]
    r.add("hooks", FAIL if missing else PASS,
          "not wired: %s" % ", ".join(missing) if missing else
          "the settings/hooks guard and the session register/revoke hooks are wired")


def check_sandbox(r):
    """gt sandbox mode (0.20.0), independent of unlock: what it enforces on THIS platform, whether
    gt's settings are in place, what Claude Code itself reports, and -- run from Claude's shell --
    a live probe that the vault refuses a write."""
    try:
        import gt_sandbox
    except ImportError:
        r.add("sandbox", NC, "gt_sandbox.py is not installed beside this tool")
        return
    try:
        for row in gt_sandbox.verify_rows():
            r.add(row["check"], row["state"], row["why"])
    except Exception as e:                       # noqa: BLE001 - a check must not crash verify
        r.add("sandbox", NC, "the sandbox check could not run (%s)" % type(e).__name__)


def main(as_json=False):
    home = C.home()
    r = Run()
    level = {"level": "off", "why": "unlock is off (the default)"}
    status = None
    if C.enabled(home):
        try:
            status = C.call("status", {}, h=home, start=False)
            level = status.get("assurance") or level
            r.add("authority", PASS, "running, version %s" % status.get("version"))
            probs = status.get("problems") or []
            r.add("policy", FAIL if probs else PASS,
                  "; ".join(probs) if probs else "policy approved, admin floor %s"
                  % ("present and accepted" if status.get("admin_floor") else "absent"))
        except gt_ipc.IpcError:
            level = {"level": "locked", "why": "unlock is on but the authority is not running: "
                     "every gated scope is refused (fail closed)"}
            r.add("authority", NC, "not running (gt_unlock.py daemon start)")
        check_files(r, home)
        check_transport(r, home)
        if status is not None:
            check_door(r, home)
            check_gated_refused(r, home, status)
        check_helper(r)
        check_hooks(r)
    else:
        r.add("unlock", NC, "unlock is off, so nothing is gated; turn it on to check more "
                            "(SECURITY.md)")
    check_sandbox(r)
    fails = [x for x in r.rows if x["state"] == FAIL]
    out = {"level": level, "rows": r.rows, "fail": len(fails),
           "not_checked": sum(1 for x in r.rows if x["state"] == NC)}
    if as_json:
        print(json.dumps(out, indent=2))
    else:
        print("level: %s -- %s" % (level["level"], level["why"]))
        for x in r.rows:
            print("  %-11s %-16s %s" % (x["state"], x["check"], x["why"]))
        print("%d FAIL, %d NOT-CHECKED, %d PASS. Unlock is not anti-malware: something already "
              "running as you can wait for you to unlock." % (
                  out["fail"], out["not_checked"], len(r.rows) - out["fail"] - out["not_checked"]))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(as_json="--json" in sys.argv[1:]))
