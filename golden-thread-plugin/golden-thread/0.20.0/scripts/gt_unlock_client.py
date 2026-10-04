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
  * A CLIENT VERIFIES THE SERVER (review F1, 2026-10-03). Before a request is sent, the process
    serving the authority's address is identified from the kernel (macOS LOCAL_PEERTOKEN, Linux
    SO_PEERCRED, Windows GetNamedPipeServerProcessId) and its __main__ file must be the
    INSTALLED gt_unlockd.py, by realpath. A fake server bound after the real one stopped --
    "allowed" for everything -- is refused: code "server_unverified", never allowed. Residual,
    documented: a same-user process can run the REAL gt_unlockd.py with its own home; at
    L1/L2 that is friction, not a boundary.
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
    # -I (isolated mode, review L1): no PYTHON* variable or user site-packages runs code inside
    # the authority before its first line -- and verify_server() refuses a server without it.
    cmd = [sys.executable, "-I", "-B", os.path.join(HERE, "gt_unlockd.py"), "--home", h]
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


def server_scripts():
    """The files a real authority runs: this client's own gt_unlockd.py and the installed hooks
    directory's (where install.sh puts gt's unlock scripts) -- realpaths."""
    out = {os.path.realpath(os.path.join(HERE, "gt_unlockd.py"))}
    out.add(os.path.realpath(os.path.join(os.path.expanduser("~"), ".claude", "golden-thread",
                                          "hooks", "gt_unlockd.py")))
    return out


def _test_server_pid():
    """GT_UNLOCK_TEST_SERVER_PID names an in-process test authority. Honoured ONLY while the
    unlock home is relocated -- which home() allows only while the REAL home's unlock is off --
    so it can never vouch for a server when real unlock is on."""
    v = os.environ.get("GT_UNLOCK_TEST_SERVER_PID")
    env = os.environ.get("GT_UNLOCK_HOME")
    if not v or not env or os.path.abspath(env) == os.path.abspath(P.unlock_home()) \
            or P.enabled_fast():
        return None
    try:
        return int(v)
    except ValueError:
        return None


def verify_server(client):
    """Raise IpcError("server_unverified") unless the process serving `client` runs the
    installed gt_unlockd.py (realpath of its __main__ file)."""
    peer = gt_ipc.peer_of(client)
    if peer is None:
        raise gt_ipc.IpcError("server_unverified", "the process serving the unlock authority's "
                              "address could not be identified; refusing it")
    if _test_server_pid() is not None and peer["pid"] == _test_server_pid():
        return
    main = gt_ipc.main_script(peer["pid"])
    if not main or os.path.realpath(main) not in server_scripts():
        raise gt_ipc.IpcError("server_unverified", "the process serving the unlock authority's "
                              "address is not gt_unlockd.py (pid %s); refusing it" % peer["pid"],
                              ["gt_unlock.py daemon status", "a process of yours may be "
                               "impersonating the authority"])
    # L1 (review 2026-10-04): the REAL gt_unlockd.py started with PYTHONPATH pointing at a
    # sitecustomize.py runs someone else's code under the authority's identity. Only an
    # isolated interpreter (python -I, which start_daemon uses) counts.
    why = gt_ipc.isolation_problem(peer["pid"])
    if why:
        raise gt_ipc.IpcError("server_unverified", "the process serving the unlock authority's "
                              "address runs gt_unlockd.py, but %s (pid %s); refusing it"
                              % (why, peer["pid"]),
                              ["gt_unlock.py daemon stop, then let gt start it again"])


def connect(h=None, timeout=5.0, answer=None):
    """A VERIFIED connection to the authority (see the module rules). Raises IpcError."""
    c = gt_ipc.connect(address(h or home()), timeout=timeout, answer=answer)
    try:
        verify_server(c)
    except gt_ipc.IpcError:
        c.close()
        raise
    return c


def call(method, params=None, *, h=None, start=True, answer=None, timeout=5.0):
    """One request. Raises gt_ipc.IpcError (code "unreachable" when no authority answers,
    "server_unverified" when what answers is not the authority)."""
    h = h or home()
    try:
        c = connect(h, timeout=timeout, answer=answer)
    except gt_ipc.IpcError as e:
        if e.code == "server_unverified":
            raise
        if not start or not start_daemon(h):
            raise gt_ipc.IpcError("unreachable", "the unlock authority is not running",
                                  ["gt_unlock.py daemon start"])
        c = connect(h, timeout=timeout, answer=answer)
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
        # never allowed: unreachable, server_unverified, or the authority's own refusal
        return {"allowed": False, "code": "unreachable" if e.code == "unreachable" else e.code,
                "message": e.message, "grant": None, "level": "?",
                "hints": e.hints or ["gt_unlock.py daemon start"]}


# ------------------------------------------------------------------ M12: old code after upgrade

def code_status(h=None):
    """-> None when no authority answers, else {"stale", "pid", "since", ...}. An authority
    that predates this check (0.20.0: no `code_status` method) is old code by definition:
    {"stale": True, "old_protocol": True, "pid": <its pid from the kernel>}. Never raises,
    never starts anything."""
    h = h or home()
    try:
        c = connect(h, timeout=2.0)
    except gt_ipc.IpcError:
        return None
    try:
        try:
            return c.call("code_status", {})
        except gt_ipc.IpcError as e:
            if e.code != "unknown_method":
                return None
            peer = gt_ipc.peer_of(c) or {}
            return {"stale": True, "old_protocol": True, "pid": peer.get("pid"),
                    "since": None}
    finally:
        c.close()


def restart_if_stale(h=None, wait=10.0):
    """Stop an authority that runs old code, then start it again from the installed code when
    unlock is on (off: it starts on demand, as always). -> (state, info), state one of
    "not_running", "current", "restarted", "stopped", "failed". It never grants: the old
    authority's grants are all dropped. A pre-0.20.1 authority has no orderly stop that skips
    the factor, so it is ended by signal -- only after the client verified, from the kernel,
    that the process is the installed gt_unlockd.py (connect -> verify_server)."""
    h = h or home()
    info = code_status(h)
    if info is None:
        return "not_running", None
    if not info.get("stale"):
        return "current", info
    addr = address(h)
    if info.get("old_protocol"):
        pid = info.get("pid")
        try:
            import signal
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, TypeError, ValueError):
            return "failed", info
    else:
        try:
            c = connect(h, timeout=5.0)
            try:
                res = c.call("stop_if_stale", {})
            finally:
                c.close()
        except gt_ipc.IpcError:
            return "failed", info
        if not res.get("stopping"):
            return "current", res
        info = dict(info, revoked=res.get("revoked"))
    deadline = time.monotonic() + wait
    while gt_ipc.alive_at(addr, timeout=0.3) and time.monotonic() < deadline:
        time.sleep(0.1)
    if gt_ipc.alive_at(addr, timeout=0.3):
        return "failed", info
    if enabled(h):
        return ("restarted" if start_daemon(h) else "stopped"), info
    return "stopped", info
