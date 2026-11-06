#!/usr/bin/env python3
"""gt_shadow.py -- shadow mode for a rule that is new or could block legitimate work (0.22.0).

    gate(rule, violates, shadow, session="", detail="") -> True when the action must be BLOCKED
    counts(days=7)                                        -> {rule: would-have-blocked events}
    gt_shadow.py count [--days N] [--json]

A rule in shadow mode, when it WOULD block, records one event and lets the action through; outside
shadow it blocks as before. The decision to enforce is then made from a number ("this rule would have
blocked 14 times last week") and not from a guess. `detail` is accepted so a caller can pass what it
matched, and is NEVER stored: an event holds the rule id (cleaned to a-z 0-9 . _ -, 40 characters),
the session (8 characters) and the time.

The record is local state, ~/.claude/golden-thread/state/shadow.jsonl, one JSON line per event. A hook
must not write the vault (Core rule 1), and this is not vault content. It NEVER raises: a record that
cannot be written still lets the action through, because shadow mode never blocks.
"""
import argparse
import datetime
import json
import os
import re
import sys

SCHEMA = 1


def _path():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "state", "shadow.jsonl")


def _clean(rule):
    return (re.sub(r"[^a-z0-9._-]", "-", str(rule).lower()) or "unknown")[:40]


def gate(rule, violates, shadow, session="", detail=""):
    """-> True when the caller must block. Shadow mode never blocks: it records and returns False."""
    if not violates:
        return False
    if not shadow:
        return True
    try:
        p = _path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        ev = {"v": SCHEMA, "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "rule": _clean(rule), "session": str(session or "")[:8]}
        data = (json.dumps(ev, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)
    except Exception:                                        # noqa: BLE001
        pass
    return False


def counts(days=7):
    """-> {rule: n} for events in the last `days` days. A missing or damaged record is {}."""
    out = {}
    try:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        with open(_path(), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                    when = datetime.datetime.strptime(ev["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=datetime.timezone.utc)
                    rule = str(ev["rule"])
                except (ValueError, KeyError, TypeError):
                    continue
                if when >= since:
                    out[rule] = out.get(rule, 0) + 1
    except OSError:
        pass
    return out


def _violations_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "state", "rule-violations.jsonl")


def record_violation(rule, session, turn):
    """0.22.0: one local record that the Stop validator BLOCKED a reply for `rule`, with the session (8
    characters) and the turn number (how many prompts the session has had). Never the reply or what
    matched. The same (session, turn, rule) is recorded once. Never raises."""
    try:
        p = _violations_path()
        sid, rule, turn = str(session or "")[:8], _clean(rule), int(turn)
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh.readlines()[-200:]:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if (r.get("session"), r.get("turn"), r.get("rule")) == (sid, turn, rule):
                        return False
        except OSError:
            pass
        os.makedirs(os.path.dirname(p), exist_ok=True)
        ev = {"v": SCHEMA, "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "rule": rule, "session": sid, "turn": turn}
        data = (json.dumps(ev, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)
        return True
    except Exception:                                        # noqa: BLE001
        return False


def violations_by_turn(days=30):
    """-> {"1-10": n, "11-25": n, "26-50": n, "51+": n} for validator blocks in the last `days` days,
    empty buckets left out. The answer to "does adherence fall as a session lengthens?"."""
    out = {}
    try:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        with open(_violations_path(), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    when = datetime.datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=datetime.timezone.utc)
                    turn = int(r["turn"])
                except (ValueError, KeyError, TypeError):
                    continue
                if when < since:
                    continue
                b = "1-10" if turn <= 10 else "11-25" if turn <= 25 else "26-50" if turn <= 50 else "51+"
                out[b] = out.get(b, 0) + 1
    except OSError:
        pass
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_shadow.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("count", help="would-have-blocked events by rule")
    c.add_argument("--days", type=int, default=7)
    c.add_argument("--json", action="store_true")
    v = sub.add_parser("violations", help="validator blocks grouped by turn number")
    v.add_argument("--days", type=int, default=30)
    v.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "violations":
        g = violations_by_turn(a.days)
        if a.json:
            print(json.dumps({"days": a.days, "by_turn": g}))
        else:
            print("validator blocks in %d day(s) by turn: %s" % (
                a.days, ", ".join("%s: %d" % kv for kv in sorted(g.items())) or "none"))
        return 0
    n = counts(a.days)
    if a.json:
        print(json.dumps({"days": a.days, "total": sum(n.values()), "rules": n}))
    elif n:
        print("%d would-have-blocked event(s) in %d day(s): %s" % (
            sum(n.values()), a.days, ", ".join("%s x%d" % (k, v) for k, v in sorted(n.items()))))
    else:
        print("no would-have-blocked events in %d day(s)" % a.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
