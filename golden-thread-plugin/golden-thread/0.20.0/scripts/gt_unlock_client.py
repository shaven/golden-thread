#!/usr/bin/env python3
"""gt_unlock_client -- how everything else asks the unlock authority (0.20.0).

Used by gt's hooks, gt_unlock.py, gt-lotr and the credential broker. Three rules:

  * OFF IS FREE. enabled() is two stat() calls (gt_unlock_policy.enabled_fast); with unlock off
    no socket is opened, no daemon is started, and check() answers "disabled" at once. This is
    what keeps every hook's path unchanged by default.
  * ON AND UNREACHABLE IS CLOSED. When unlock is on and the authority cannot be reached (and
    cannot be started), check() answers allowed=False, code "unreachable" -- never allowed.
    Hooks therefore fail CLOSED for gated scopes, the one deliberate exception to gt's
    fail-open hook rule.
  * A CLIENT ASKS, IT NEVER ASSERTS. Nothing here can tell the authority that a factor passed;
    a code typed at the client's own terminal is sent for the AUTHORITY to verify.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_ipc                       # noqa: E402
import gt_unlock_policy as P        # noqa: E402

IS_WINDOWS = os.name == "nt"
START_WAIT_S = 4.0


def home():
    """The unlock home. GT_UNLOCK_HOME relocates it (tests, a second profile) ONLY while the
    real home's unlock is off: otherwise any process could point its own checks at an empty
    home where unlock is off, and a gate evaluated in that process would pass."""
    real = P.unlock_home()
    env = os.environ.get("GT_UNLOCK_HOME")
    if env and os.path.abspath(env) != os.path.abspath(real) and not P.enabled_fast():
        return os.path.abspath(env)
    return os.path.abspath(real)


def enabled(h=None):
    return _enabled_at(h or home())


def _enabled_at(h):
    return P.enabled_at(h)


def address(h=None):
    return gt_ipc.default_address(h or home(), "unlockd")


def tty_answer(need):
    """Answer the authority's question at THIS process's terminal, if it has one."""
    import getpass
    if not (sys.stdin and sys.stdin.isatty()) and not os.environ.get("GT_UNLOCK_STDIN_ANSWERS"):
        return None
    try:
        return getpass.getpass((need or {}).get("prompt", "code") + "\n> ")
    except (EOFError, KeyboardInterrupt, OSError):
        return None


def start_daemon(h=None, wait=START_WAIT_S):
    """Start the authority detached, then wait until it answers. -> True when it answers.
    GT_UNLOCK_NO_START=1 (set by gt's test harness) refuses, so a test never leaves a daemon
    behind that it did not start and stop on purpose."""
    h = h or home()
    if os.environ.get("GT_UNLOCK_NO_START") == "1":
        return gt_ipc.alive_at(address(h), timeout=0.5)
    addr = address(h)
    if gt_ipc.alive_at(addr, timeout=0.5):
        return True
    os.makedirs(h, mode=0o700, exist_ok=True)
    cmd = [sys.executable, "-B", os.path.join(HERE, "gt_unlockd.py"), "--home", h]
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL, "close_fds": True}
    if IS_WINDOWS:
        kw["creationflags"] = 0x00000008 | 0x00000200      # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kw)
    except OSError:
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if gt_ipc.alive_at(addr, timeout=0.5):
            return True
        time.sleep(0.05)
    return False


def call(method, params=None, *, h=None, start=True, answer=None, timeout=5.0):
    """One request. Raises gt_ipc.IpcError (code "unreachable" when no authority answers)."""
    h = h or home()
    addr = address(h)
    try:
        c = gt_ipc.connect(addr, timeout=timeout, answer=answer)
    except gt_ipc.IpcError:
        if not start or not start_daemon(h):
            raise gt_ipc.IpcError("unreachable", "the unlock authority is not running",
                                  ["gt_unlock.py daemon start"])
        c = gt_ipc.connect(addr, timeout=timeout, answer=answer)
    try:
        return c.call(method, params or {})
    finally:
        c.close()


def check(scope, *, request=False, reason="", subject=None, job=None, h=None, answer=None,
          start=True):
    """-> {"allowed", "code", "message", "grant", "level", "hints"}. See the module rules."""
    h = h or home()
    if not enabled(h):
        return {"allowed": True, "code": "disabled", "message": "unlock is off", "grant": None,
                "level": "off", "hints": []}
    params = {"scope": scope, "request": bool(request), "reason": reason,
              "tty": answer is not None}
    if subject is not None:
        params["subject"] = subject
    # An unattended job names itself with GT_JOB in its job definition (launchd plist /
    # Task Scheduler action). The authority honours the name only for a process with no
    # `claude` ancestor, and only for the allow-listed narrow scopes (gt_unlockd I7).
    job = job or os.environ.get("GT_JOB")
    if job:
        params["job"] = job
    try:
        return call("check", params, h=h, start=start, answer=answer)
    except gt_ipc.IpcError as e:
        return {"allowed": False, "code": "unreachable" if e.code == "unreachable" else e.code,
                "message": e.message, "grant": None, "level": "?",
                "hints": e.hints or ["gt_unlock.py daemon start"]}
