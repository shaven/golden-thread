"""Per-sub-endpoint health for a connection that hands each call to one of several backends
(0.22.0, owner request 2026-10-27).

Some connections are dispatchers: the call names an operation or an argument, and the service
routes it to one of N backends. When one backend fails, the connection as a whole used to look
broken, and the next session had to find out again which one it was. A connection that declares
`health_tracking: true` now keeps one small record per sub-endpoint, written from the results of
real calls only:

  state/endpoint-health.json   {"schema": 1, "connections": {CONNECTION: {SUB: record}}}
  record                       last_success, last_failure, last_failure_code, last_failure_message,
                               consecutive_failures, failing_since (all ISO UTC or null / a number)

This is a record, not a probe and not a circuit breaker: nothing is pinged, and a failing
sub-endpoint is never skipped for the caller. A record expires `health_expires_h` hours (24
unless the connection says otherwise) after its newest event. A success zeroes the failure count.

A sub-endpoint is found from the call: the argument named by `sub_endpoint_arg` when its value is
one of the connection's declared ids, else the declared sub-endpoint whose `ops` list holds the
operation. A call that matches neither is about the connection, not a sub-endpoint, and writes
nothing here. The failure message is stored scrubbed (no address, no secret reference, no
credential-shaped text) and short, because a hub client can read the record.
"""
import contextlib
import datetime
import json
import os
import re
import time

from . import redact, shaping
from .errors import GatewayError
from .util import atomic_write

FILE = "endpoint-health.json"
ID_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
DEFAULT_EXPIRES_H = 24
MAX_EXPIRES_H = 24 * 30
MAX_SUB_ENDPOINTS = 64
MESSAGE_CHARS = 200
OP_HEALTH = "endpoint_health"            # the read op: `lotr read CONNECTION endpoint_health`
_KEYS = ("health_tracking", "sub_endpoints", "sub_endpoint_arg", "health_expires_h")


def _path(home):
    return os.path.join(str(home), "state", FILE)


def iso(now):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _epoch(text):
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


# -- the connection's declaration ---------------------------------------------------------------

def problem(entry):
    """A plain-words reason these fields cannot be used, or None. Absent fields are fine."""
    if "health_tracking" in entry and not isinstance(entry["health_tracking"], bool):
        return "health_tracking must be true or false"
    if "health_expires_h" in entry:
        h = entry["health_expires_h"]
        if isinstance(h, bool) or not isinstance(h, (int, float)) or not 0 < h <= MAX_EXPIRES_H:
            return "health_expires_h must be a number of hours, more than 0 and at most %d" % MAX_EXPIRES_H
    if "sub_endpoint_arg" in entry and not (isinstance(entry["sub_endpoint_arg"], str)
                                            and ID_RX.fullmatch(entry["sub_endpoint_arg"])):
        return "sub_endpoint_arg must be an argument name (letters, digits, . _ -)"
    subs = entry.get("sub_endpoints")
    if subs is None:
        return None
    if not isinstance(subs, list) or len(subs) > MAX_SUB_ENDPOINTS:
        return "sub_endpoints must be a list of at most %d entries" % MAX_SUB_ENDPOINTS
    seen = set()
    for s in subs:
        if not isinstance(s, dict) or set(s) - {"id", "description", "ops"}:
            return "each sub_endpoints entry is {id, description, ops}"
        if not (isinstance(s.get("id"), str) and ID_RX.fullmatch(s["id"])):
            return "a sub_endpoints id is letters, digits, . _ - (at most 64)"
        if s["id"] in seen:
            return "sub_endpoints id %s appears twice" % s["id"]
        seen.add(s["id"])
        if "description" in s and not (isinstance(s["description"], str) and len(s["description"]) <= 200):
            return "a sub_endpoints description is text of at most 200 characters"
        ops = s.get("ops", [])
        if not (isinstance(ops, list) and len(ops) <= 32
                and all(isinstance(o, str) and 0 < len(o) <= 128 for o in ops)):
            return "a sub_endpoints ops list is at most 32 operation names"
    return None


def keys():
    return _KEYS


def tracked(conn):
    return conn.get("health_tracking") is True


def declared(conn):
    return [s["id"] for s in conn.get("sub_endpoints") or []]


def expires_h(conn):
    return conn.get("health_expires_h") or DEFAULT_EXPIRES_H


def resolve(conn, op, args):
    """-> the sub-endpoint id this call went to, or None (a call about the connection itself)."""
    ids = declared(conn)
    arg = conn.get("sub_endpoint_arg")
    if arg and isinstance(args, dict):
        v = args.get(arg)
        if isinstance(v, str) and ID_RX.fullmatch(v) and (v in ids if ids else True):
            return v
    for s in conn.get("sub_endpoints") or []:
        if isinstance(op, str) and op in (s.get("ops") or []):
            return s["id"]
    return None


# -- the store ----------------------------------------------------------------------------------

@contextlib.contextmanager
def _locked(home):
    """One lock around every read-modify-write: the daemon records while `lotr reset-health` clears."""
    os.makedirs(os.path.dirname(_path(home)), mode=0o700, exist_ok=True)
    fd = os.open(_path(home) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
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


def _load(home):
    """The stored connections; an unreadable or implausible file is no health, never an error."""
    try:
        with open(_path(home), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    conns = data.get("connections") if isinstance(data, dict) else None
    if not isinstance(conns, dict):
        return {}
    return {c: {s: r for s, r in subs.items() if isinstance(r, dict)}
            for c, subs in conns.items() if isinstance(subs, dict)}


def _save(home, conns):
    atomic_write(_path(home), json.dumps({"schema": 1, "connections": conns}, indent=2) + "\n", 0o600)


def _newest(rec):
    return max([e for e in (_epoch(rec.get("last_success")), _epoch(rec.get("last_failure"))) if e is not None],
               default=None)


def _live(subs, now, hours):
    """The records whose newest event is within `hours` of `now`."""
    out = {}
    for sub, rec in subs.items():
        newest = _newest(rec)
        if newest is not None and now - newest < hours * 3600:
            out[sub] = rec
    return out


def scrub(message):
    """One short line, with no address, no secret reference and nothing credential-shaped."""
    text = " ".join(redact.scrub_text(str(message)).split())
    if shaping.scan_credentials(text):
        return "(withheld: credential-shaped text)"
    return text[:MESSAGE_CHARS]


def note(home, cid, sub, ok, *, code=None, message="", now, hours=DEFAULT_EXPIRES_H):
    """Record one call result for CONNECTION / SUB-ENDPOINT. -> the new record."""
    stamp = iso(now)
    with _locked(home):
        conns = _load(home)
        subs = _live(conns.get(cid, {}), now, hours)
        rec = subs.get(sub) or {"last_success": None, "last_failure": None, "last_failure_code": None,
                                "last_failure_message": None, "consecutive_failures": 0,
                                "failing_since": None}
        if ok:
            rec["last_success"] = stamp
            rec["consecutive_failures"] = 0
            rec["failing_since"] = None
        else:
            rec["last_failure"] = stamp
            rec["last_failure_code"] = str(code) if code is not None else None
            rec["last_failure_message"] = scrub(message)
            rec["consecutive_failures"] = int(rec.get("consecutive_failures") or 0) + 1
            rec["failing_since"] = rec.get("failing_since") or stamp
        subs[sub] = rec
        while len(subs) > MAX_SUB_ENDPOINTS:                  # a runaway id list cannot grow the file
            subs.pop(min(subs, key=lambda s: _newest(subs[s]) or 0))
        conns[cid] = subs
        _save(home, conns)
        return dict(rec)


def view(home, cid, now, hours=DEFAULT_EXPIRES_H):
    """-> {sub-endpoint: record} for a connection, without the expired ones. Reads only."""
    return {s: dict(r) for s, r in _live(_load(home).get(cid, {}), now, hours).items()}


def reset(home, cid, sub=None):
    """Clear a connection's records, or one sub-endpoint's. -> how many records were removed."""
    with _locked(home):
        conns = _load(home)
        subs = conns.get(cid, {})
        gone = [sub] if sub else list(subs)
        removed = sum(1 for s in gone if subs.pop(s, None) is not None)
        if not subs:
            conns.pop(cid, None)
        _save(home, conns)
        return removed


def failing(rec):
    return int(rec.get("consecutive_failures") or 0) > 0


def degraded(conn, records):
    """True when every sub-endpoint of the connection (declared, or else recorded) is failing now.
    One healthy or never-called sub-endpoint is enough for the connection not to be degraded."""
    universe = set(declared(conn)) | set(records)
    return bool(universe) and all(failing(records.get(s) or {}) for s in universe)


def rows(conn, records):
    """The readable list: declared sub-endpoints first, in order, then any other recorded one."""
    order = declared(conn) + sorted(set(records) - set(declared(conn)))
    out = []
    for s in order:
        rec = records.get(s)
        if rec is None:
            continue
        out.append(dict({"id": s, "failing": failing(rec)}, **rec))
    return out


def error_fields(sub, rec):
    """What a failed call's error says about the sub-endpoint: its name, its history and a hint."""
    since = rec.get("failing_since")
    n = int(rec.get("consecutive_failures") or 0)
    health = {"last_success": rec.get("last_success"), "consecutive_failures": n, "since": since}
    at = since[11:16] if since else "?"
    hint = ("%s has failed %d time%s since %s UTC. Other sub-endpoints on this connection may be healthy."
            % (sub, n, "" if n == 1 else "s", at))
    return {"sub_endpoint": sub, "health": health}, hint


def read_view(home, conn, now):
    """The data the `endpoint_health` read op returns."""
    recs = view(home, conn["id"], now, expires_h(conn))
    return {"connection": conn["id"], "degraded": degraded(conn, recs),
            "expires_after_h": expires_h(conn), "declared": declared(conn),
            "sub_endpoints": {s: r for s, r in recs.items()}}


def no_such_op(conn):
    return GatewayError("unknown_op", "%s does not track sub-endpoint health" % conn["id"],
                        ["set health_tracking: true in the connection's template"])
