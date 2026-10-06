"""gt-lotr as a CONSUMER of gt core's unlock authority (0.3.0, gt 0.20.1).

LOTR never decides who is unlocked. It asks gt core's authority (gt_unlockd) through gt core's
own client, gt_unlock_client.py, which gt installs beside its hooks. This module only finds
that client at run time and turns its answers into GatewayErrors.

Where the client comes from: GT_HOOKS_DIR, else ~/.claude/golden-thread/hooks (where gt
installs gt_unlock_client.py and gt_ipc.py). The core still never imports gt at import time
(ADR-6 rule 1, as amended in 0.3.0): nothing here is loaded until unlock is asked about, and a
machine without gt (a Linux hub) runs exactly as 0.2.0 did while unlock is off.

SECURITY INVARIANTS
  U1  OFF IS 0.2.0. enabled() False means no socket, no daemon, no new refusal anywhere: every
      caller in lotr checks enabled() before doing anything unlock-related.
  U2  ON AND UNLOADABLE IS CLOSED. When gt's policy file or admin floor says unlock is on and the
      client cannot be imported, every gated action is refused with `unlock_unavailable`
      (policy_says_on() reads the same files the client would, without the client).
  U3  GT_HOOKS_DIR is honoured ONLY while the real home's unlock is off -- the same rule
      gt_unlock_client applies to GT_UNLOCK_HOME. Otherwise any process that can set lotrd's
      environment could point it at a fake client that always says "allowed".
  U4  LOTR ASKS, IT NEVER ASSERTS. Nothing here can tell the authority that a factor passed;
      the subject it names is a process LOTR identified through its own kernel peer lookup.
"""
import json
import os
import sys
import threading

from .errors import GatewayError

IS_WINDOWS = os.name == "nt"
_LOCK = threading.Lock()
_CACHE = {}


# ---------------------------------------------------------------- where gt lives

def installed_hooks_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "hooks")


def _real_unlock_home():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "unlock")


def _admin_paths():
    # The same locations as gt_unlock_policy.admin_paths(); duplicated so U2 holds when the
    # client itself is what cannot be loaded.
    if IS_WINDOWS:
        pd = os.environ.get("ProgramData") or r"C:\ProgramData"
        return [os.path.join(pd, "gt", "unlock-policy.json")]
    if sys.platform == "darwin":
        return ["/Library/Application Support/gt/unlock-policy.json"]
    return ["/etc/gt/unlock-policy.json"]


def _admin_registry_set():
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\gt") as k:
            winreg.QueryValueEx(k, "UnlockPolicy")
            return True
    except OSError:
        return False


def _markers(home):
    # The same two `unlock-on` markers as gt_unlock_policy.marker_paths (duplicated so U2
    # holds when gt's client cannot be loaded): in the unlock home, and beside it.
    home = os.path.abspath(home)
    return [os.path.join(home, "unlock-on"),
            os.path.join(os.path.dirname(home), "." + os.path.basename(home) + "-unlock-on")]


def _home_on(home):
    """policy.json says enabled, or the authority's `unlock-on` markers (beside and inside the
    home) or its `unlock_on` flag in state.json say it was turned on through the authority
    (gt_unlock_policy.enabled_at: a policy.json edited to "enabled": false, or deleted with
    state.json, behind the authority's back does not switch anything off); an unreadable
    policy counts as ON (fail closed)."""
    if any(os.path.lexists(p) for p in _markers(home)):
        return True
    try:
        with open(os.path.join(home, "state.json"), "r", encoding="utf-8") as f:
            if json.load(f).get("unlock_on"):
                return True
    except (OSError, ValueError, AttributeError):
        pass
    p = os.path.join(home, "policy.json")
    try:
        if not os.path.exists(p):
            return False
        with open(p, "r", encoding="utf-8") as f:
            return bool(json.load(f).get("enabled", False))
    except Exception:                                    # noqa: BLE001
        return True


def _real_on():
    try:
        if any(os.path.lexists(p) for p in _admin_paths()) or _admin_registry_set():
            return True
    except Exception:                                    # noqa: BLE001
        return True
    return _home_on(_real_unlock_home())


def policy_says_on():
    """Is unlock on, judged from the files alone (no gt code)? The real home and the admin
    floor; also GT_UNLOCK_HOME when it is set (only ever stricter)."""
    if _real_on():
        return True
    env = os.environ.get("GT_UNLOCK_HOME")
    return bool(env) and _home_on(os.path.abspath(env))


def hooks_dir():
    env = os.environ.get("GT_HOOKS_DIR")
    if env and not _real_on():                           # U3
        return os.path.abspath(env)
    return installed_hooks_dir()


def _load(name):
    """Import gt core module `name` from hooks_dir(), or None. Cached per (dir, name)."""
    d = hooks_dir()
    key = (d, name)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        mod = None
        if os.path.isfile(os.path.join(d, name + ".py")):
            if d not in sys.path:
                sys.path.append(d)       # appended: gt's own modules re-insert their dir
            try:
                import importlib
                mod = importlib.import_module(name)
                # A module of that name already imported from somewhere else is not gt's.
                here = os.path.dirname(os.path.realpath(getattr(mod, "__file__", "") or ""))
                if here != os.path.realpath(d):
                    mod = None
            except Exception:                            # noqa: BLE001 - unloadable = absent
                mod = None
        _CACHE[key] = mod
        return mod


def reset():
    """Forget what was loaded (tests switch GT_HOOKS_DIR between cases)."""
    with _LOCK:
        _CACHE.clear()


def client():
    return _load("gt_unlock_client")


def ipc():
    return _load("gt_ipc")


def brokers():
    return _load("gt_unlock_brokers")


# ---------------------------------------------------------------- questions

def enabled():
    """Is gt unlock on? Never raises; any doubt is ON (fail closed)."""
    c = client()
    if c is None:
        return policy_says_on()
    try:
        return bool(c.enabled())
    except Exception:                                    # noqa: BLE001
        return True


def _require():
    c = client()
    if c is None:                                        # U2
        raise GatewayError("unlock_unavailable",
                           "gt unlock is on, but gt core's unlock client cannot be loaded from "
                           f"{hooks_dir()}; refusing rather than serving ungated",
                           hints=["reinstall gt (./install.sh) so gt_unlock_client.py is in its "
                                  "hooks directory"])
    return c


def _subject(subject):
    if not subject:
        return None
    return {"pid": int(subject["pid"]), "start": subject["start"]}


def _err(e, fallback="unreachable"):
    code = getattr(e, "code", None) or fallback
    return GatewayError(str(code), str(getattr(e, "message", "") or type(e).__name__),
                        list(getattr(e, "hints", None) or []))


def check(scope, subject=None, *, request=False, reason="", answer=None):
    """Ask the authority about `scope` for `subject` (a kernel-identified peer; None = this
    process). -> the grant id (None when unlock is off or the scope is open). Raises
    GatewayError carrying the authority's code (locked, mcp_only, step_up, failed_closed,
    unreachable, ...) and hints."""
    if not enabled():                                    # U1
        return None
    c = _require()
    kw = {"request": bool(request), "reason": reason[:300]}
    if subject is not None:
        kw["subject"] = _subject(subject)
    if answer is not None:
        kw["answer"] = answer
    try:
        v = c.check(scope, **kw)
    except Exception as e:                               # noqa: BLE001 - never "allowed"
        raise _err(e)
    if isinstance(v, dict) and v.get("allowed"):
        return v.get("grant")
    v = v if isinstance(v, dict) else {}
    hints = list(v.get("hints") or [])
    if v.get("code") == "locked":
        hints.append("the gt-lotr MCP tools ask for the unlock themselves; from a terminal, "
                     "run gt_unlock.py unlock")
    raise GatewayError(str(v.get("code") or "locked"),
                       str(v.get("message") or f"{scope} is not allowed"), hints)


def call(method, params=None, *, start=True, answer=None):
    """One request to the authority. Raises GatewayError (unlock_unavailable when gt's client
    cannot be loaded; the authority's own code otherwise)."""
    c = _require()
    kw = {"start": start}
    if answer is not None:
        kw["answer"] = answer
    try:
        return c.call(method, params or {}, **kw)
    except Exception as e:                               # noqa: BLE001
        raise _err(e)


def tty_answer():
    """gt's terminal answerer (a code typed at THIS process's terminal), or None."""
    c = client()
    return getattr(c, "tty_answer", None) if c is not None else None


def kernel_peer(sock):
    """{pid, start} of a unix-socket peer, from the kernel via gt_ipc -- only while unlock is
    on (U1: off, nothing new is looked up). None when it cannot be told."""
    if not enabled():
        return None
    m = ipc()
    if m is None:
        return None
    try:
        p = m.unix_peer(sock)
    except Exception:                                    # noqa: BLE001
        return None
    if not p or p.get("pid") is None or p.get("start") is None:
        return None
    return {"pid": int(p["pid"]), "start": p["start"]}
