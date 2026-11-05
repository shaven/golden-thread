#!/usr/bin/env python3
"""gt_ledger.py -- the one savings-ledger writer the gt hooks share (0.22.0).

A hook that saves tokens calls `record_event(...)`; it appends ONE line to
~/.claude/golden-thread/usage/savings.jsonl, the file gt-usage's `gt_savings.py report` reads.
The hooks cannot import gt_savings.py (it lives in the separate usage plugin), and gt_spill.py
carries its own copy of this writer; the rows added in 0.22.0 (paste-guard, auto-compact,
gt-minimize, model-profile, core-rules-wider) share this one instead of each carrying a fourth.

Rules it keeps:
  * only `measured` or `estimated` is recordable. `design` means "no measurement" and is never a
    line; a row with no events stays `design` in the report.
  * nothing is written unless the usage module is there (the ledger folder exists) and the
    `savings_ledger` setting is not `off`. It never creates the ledger folder.
  * the project is stored as an opaque 8-character hash, the session as 8 characters; no path,
    command or text is ever written.
  * an event with a key already in the ledger is not written twice.
  * it NEVER raises: a hook must not fail because the ledger could not be written.
"""
import datetime
import hashlib
import json
import os
import time

SCHEMA = 1
RECORDABLE = ("measured", "estimated")


def _home():
    return os.path.expanduser("~")


def _version():
    """The release this file shipped in: the folder two levels up is named for it."""
    return os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ledger_on():
    try:
        with open(os.path.join(_home(), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            v = json.load(fh).get("savings_ledger")
    except (OSError, ValueError, AttributeError):
        v = None
    return str(v or "on").strip().lower() != "off"


def _opaque(s):
    return hashlib.sha256(str(s).encode("utf-8", "replace")).hexdigest()[:8]


def _has_key(path, key):
    needle = json.dumps(key)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if needle in line and '"key":' + needle in line.replace(" ", ""):
                    return True
    except OSError:
        pass
    return False


def record_event(mechanism, actual_eq, counterfactual_eq, state="estimated", note="", key=None,
                 session="", project_seed=""):
    """Append one ledger line. -> True when a line was written, False otherwise. Never raises."""
    try:
        if state not in RECORDABLE:
            return False
        usage = os.path.join(_home(), ".claude", "golden-thread", "usage")
        if not os.path.isdir(usage) or not _ledger_on():
            return False
        path = os.path.join(usage, "savings.jsonl")
        key = key or "%s:%d" % (mechanism, time.time_ns())
        if _has_key(path, key):
            return False
        ev = {"v": SCHEMA, "gt": _version(), "mechanism": mechanism, "state": state,
              "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "key": key, "session": str(session or "")[:8],
              "project": _opaque(project_seed) if project_seed else "",
              "actual_eq": int(actual_eq), "counterfactual_eq": int(counterfactual_eq)}
        if note:
            ev["note"] = str(note)[:200]
        data = (json.dumps(ev, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        try:
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)
        return True
    except Exception:                                        # noqa: BLE001
        return False
