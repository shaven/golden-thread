"""Consent windows (0.5.1, `lotr grant`): a time-boxed pass for consent-tier ops on one connection.

A window removes the per-call dialog until it expires, which softens the consent gate. So: only a
person's terminal opens or revokes one (lotr.py gates that), the cap is eight hours, it names one
connection (and optionally one op), and the engine ignores it for hub clients and while a gt
unlock authority is on. The store is <home>/state/consent-windows.json, mode 600. A store that
cannot be read honours nothing.
"""
import contextlib
import math
import os
import re
import secrets
import time

from .errors import GatewayError
from .util import atomic_write, read_json

MIN_SECONDS = 60
MAX_SECONDS = 8 * 3600
_DUR = re.compile(r"^(\d{1,4})([mh])$")


def _path(home):
    return home / "state" / "consent-windows.json"


def parse_duration(text):
    m = _DUR.match(str(text or "").strip())
    secs = int(m.group(1)) * (60 if m and m.group(2) == "m" else 3600) if m else 0
    if not m or not (MIN_SECONDS <= secs <= MAX_SECONDS):
        raise GatewayError("bad_duration", "a window lasts 1m to 8h, written like 30m or 2h",
                           ["lotr grant open CONNECTION --for 30m"])
    return secs


@contextlib.contextmanager
def _locked(home):
    """Exclusive lock around every read-modify-write, so a prune cannot write back a revoked window
    and two openers cannot lose each other's update."""
    d = _path(home).parent
    d.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(str(_path(home)) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        os.close(fd)


def _plausible(w, now):
    """The store is a same-user file, so anything in it is checked: a string id and connection, a
    scope that is null or a string, finite times, opened already, and no longer than the cap."""
    try:
        o, e = w["opened_at"], w["expires_at"]
        scope_ok = w.get("op_filter") is None or isinstance(w.get("op_filter"), str)
        return (isinstance(w.get("id"), str) and isinstance(w.get("connection"), str)
                and scope_ok) and \
            all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                for x in (o, e)) and o <= now + 60 and 0 < e - o <= MAX_SECONDS + 60
    except (KeyError, TypeError):
        return False


def _load(home):
    """-> the stored list; raises window_store_corrupt when the file cannot be trusted."""
    p = _path(home)
    if not p.exists():
        return []
    try:
        data = read_json(p)
    except Exception:                                       # noqa: BLE001
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("windows"), list) or not all(
            isinstance(w, dict) and isinstance(w.get("expires_at"), (int, float))
            for w in data["windows"]):
        raise GatewayError("window_store_corrupt", f"{p} cannot be read, so no window is honoured",
                           ["revoke by deleting the file, then open a new window"])
    return data["windows"]


def _save(home, wins):
    import json
    p = _path(home)
    p.parent.mkdir(mode=0o700, exist_ok=True)
    atomic_write(p, json.dumps({"schema": 1, "windows": wins}, indent=2), 0o600)


def list_windows(home, now=None):
    """Live, plausible windows; expired and implausible ones are dropped from the file."""
    now = time.time() if now is None else now
    with _locked(home):
        wins = _load(home)
        live = [w for w in wins if _plausible(w, now) and w["expires_at"] > now]
        if len(live) != len(wins):
            _save(home, live)
        return live


def open_window(home, connection, seconds, op=None, now=None, opened_by=None):
    now = time.time() if now is None else now
    with _locked(home):
        wins = _load(home)
        live = [w for w in wins if _plausible(w, now) and w["expires_at"] > now]
        w = {"id": "w-" + secrets.token_hex(4), "connection": connection, "op_filter": op,
             "opened_at": now, "expires_at": now + seconds, "opened_by": opened_by}
        _save(home, live + [w])
        return w


def revoke(home, wid):
    with _locked(home):
        wins = _load(home)
        keep = [w for w in wins if w.get("id") != wid]
        if len(keep) == len(wins):
            raise GatewayError("unknown_window", f"no window {wid}", ["lotr grant list"])
        _save(home, keep)


def close_connection(home, connection):
    """End every window of one connection (a re-login, a disconnect or a new connect: a window granted
    against one sign-in must not cover the next). -> how many were closed."""
    with _locked(home):
        wins = _load(home)
        keep = [w for w in wins if w.get("connection") != connection]
        if len(keep) != len(wins):
            _save(home, keep)
        return len(wins) - len(keep)


def active(home, connection, op, now=None):
    """-> the window covering this call, or None. Never raises: any doubt honours nothing."""
    try:
        for w in list_windows(home, now):
            if w.get("connection") == connection and w.get("op_filter") in (None, op):
                return w
    except Exception:                                       # noqa: BLE001
        return None
    return None
