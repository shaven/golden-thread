#!/usr/bin/env python3
"""gt_refusals.py -- a repeat counter on guard refusals (0.22.0).

    annotate(guard, reason, session=None) -> the reason, with one stop line added when it is due
    use_session(payload)                   remember the hook payload's session for this process

A model that a guard has refused tends to try a fourth variant of the same thing. Each guard passes its
deny text through `annotate`. The setting `refusal_limit` decides what happens:

  off     nothing is recorded
  count   (default) every refusal is counted per session; the text is never changed
  stop    as count, and once the SAME guard has refused the SAME kind of action 3 times, or the session
          has been refused 20 times in all, one line is added to the text: stop retrying variants, say
          what you were trying to do, and ask the user

"The same kind of action" is the guard plus the first line of its reason (every guard opens with a
stable heading), digits collapsed, hashed. The record is local state,
~/.claude/golden-thread/state/refusals-<session8>.json, mode 600: counts by hash. It holds no command,
no path and no text. Hooks must not write the vault (Core rule 1), and this is not vault content.

It NEVER raises, and a record that cannot be read or written leaves the deny text exactly as it was.
"""
import hashlib
import json
import os
import re
import sys
import time

SAME_LIMIT = 3
TOTAL_LIMIT = 20
KEEP_DAYS = 14
_SESSION = ""


def use_session(payload):
    """Remember the session of the hook payload being handled (module state, one process per hook)."""
    global _SESSION
    try:
        _SESSION = str((payload or {}).get("session_id") or "")
    except Exception:                                        # noqa: BLE001
        _SESSION = ""


def _mode():
    """The `refusal_limit` setting, read straight from vault-config.json: a guard must not depend on
    gt_settings (tests/test_gt_use_deps.py pins that), so the registry is not imported here. A value that
    is not off, count or stop is the default, count."""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            v = str(json.load(fh).get("refusal_limit") or "count").strip().lower()
    except Exception:                                        # noqa: BLE001
        return "count"
    return v if v in ("off", "count", "stop") else "count"


def _dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "state")


def _path(session):
    sid = re.sub(r"[^A-Za-z0-9]", "", str(session))[:8] or "unknown"
    return os.path.join(_dir(), "refusals-%s.json" % sid)


def _key(guard, reason):
    head = str(reason).strip().splitlines()[0] if str(reason).strip() else ""
    head = re.sub(r"\d+", "#", head.lower())[:120]
    return hashlib.sha256(("%s|%s" % (guard, head)).encode("utf-8", "replace")).hexdigest()[:10]


def _prune():
    try:
        cutoff = time.time() - KEEP_DAYS * 86400
        for n in os.listdir(_dir()):
            if n.startswith("refusals-") and os.path.getmtime(os.path.join(_dir(), n)) < cutoff:
                os.unlink(os.path.join(_dir(), n))
    except OSError:
        pass


def annotate(guard, reason, session=None):
    try:
        mode = _mode()
        if mode == "off":
            return reason
        sid = session or _SESSION or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        p = _path(sid)
        try:
            with open(p, encoding="utf-8") as fh:
                state = json.load(fh)
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        by = state.get("by") if isinstance(state.get("by"), dict) else {}
        k = _key(guard, reason)
        by[k] = int(by.get(k, 0)) + 1
        total = int(state.get("total", 0)) + 1
        os.makedirs(_dir(), exist_ok=True)
        tmp = p + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, json.dumps({"v": 1, "total": total, "by": by}).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, p)
        if total == 1:
            _prune()
        if mode != "stop":
            return reason
        if by[k] >= SAME_LIMIT:
            why = "gt has now refused this same kind of action %d times in this session" % by[k]
        elif total >= TOTAL_LIMIT:
            why = "gt has now refused %d actions in this session" % total
        else:
            return reason
        return (str(reason).rstrip("\n") + "\n\n" + why + ". Stop retrying variants of it. Tell the user "
                "what you were trying to do and why, and ask how they want to proceed.")
    except Exception:                                        # noqa: BLE001
        return reason


if __name__ == "__main__":
    print(__doc__.strip())
