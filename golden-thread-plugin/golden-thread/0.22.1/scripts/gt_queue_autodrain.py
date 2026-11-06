#!/usr/bin/env python3
"""gt_queue_autodrain.py -- the write queue applies itself (0.22.0, review C2).

  gt_queue_autodrain.py stop          --hook [--vault V]   Stop: drain what this turn queued
  gt_queue_autodrain.py session-start --hook [--vault V]   SessionStart: drain what a session that
                                                           ended left, and name anything older than a day

A write queued through gt_write_queue.py waited for someone to run `gt_broker.py drain`, so a note
that nobody drained was in neither the vault nor the log. With the setting `queue_autodrain` on (the
default) this runs the same drain at the next safe point. It decides nothing itself: the broker keeps
every rule -- a file another live session claimed stays queued, a contradiction is escalated, a repeat
is not written -- and two drains at once apply each request once (the broker's own lock).

SILENT when nothing is queued: the whole cost is one directory listing. Prints at most one
`systemMessage`. FAILS OPEN: any problem is silence and exit 0; it never blocks a turn.
With the setting off nothing drains, and session start names a request that has waited a day.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

STALE_S = 86400
DRAIN_TIMEOUT_S = 25


def _vault(argv):
    if "--vault" in argv:
        k = argv.index("--vault")
        if k + 1 < len(argv):
            return argv[k + 1]
    import gt_paths
    v = gt_paths.find_vault()
    return str(v) if v else None


def _queued(vault):
    q = os.path.join(vault, "Projects", "golden-thread", "spool", "queue")
    try:
        return [os.path.join(q, n) for n in os.listdir(q) if n.endswith(".json") and not n.startswith(".")]
    except OSError:
        return []


def _setting():
    try:
        import gt_settings
        return gt_settings.get("queue_autodrain")
    except Exception:                                        # noqa: BLE001 - the default
        return "on"


def _stale(files, now=None):
    now = time.time() if now is None else now
    n = 0
    for f in files:
        try:
            if now - os.stat(f).st_mtime > STALE_S:
                n += 1
        except OSError:
            pass
    return n


def _drain(vault):
    """-> (applied, left, not run). Never raises."""
    broker = os.path.join(HERE, "gt_broker.py")
    if not os.path.isfile(broker):
        return 0, 0, True
    try:
        p = subprocess.run([sys.executable, broker, "drain", "--vault", vault, "--json"],
                           capture_output=True, text=True, timeout=DRAIN_TIMEOUT_S)
        data = json.loads(p.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0, 0, True
    results = data.get("results") or []
    applied = sum(1 for r in results if r.get("decision") in ("apply", "deduplicate"))
    return applied, int(data.get("left") or 0), False


def run(event, argv):
    vault = _vault(argv)
    if not vault or not os.path.isdir(vault):
        return None
    files = _queued(vault)
    if not files:
        return None
    if _setting() == "off":
        n = _stale(files)
        if event == "session-start" and n:
            return ("gt: %d queued vault write(s) are older than a day and queue_autodrain is off; "
                    "drain them: python3 ~/.claude/golden-thread/hooks/gt_broker.py drain --vault \"%s\""
                    % (n, vault))
        return None
    applied, left, failed = _drain(vault)
    if failed:
        return None
    parts = []
    if applied:
        parts.append("applied %d queued vault write(s)" % applied)
    if left:
        parts.append("%d left queued (claimed, held or escalated)" % left)
    n = _stale(_queued(vault)) if left else 0
    if n:
        parts.append("%d of them older than a day" % n)
    return ("gt: " + "; ".join(parts)) if parts else None


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    try:
        if a and a[0] in ("stop", "session-start"):
            try:
                sys.stdin.read()                      # the hook payload; not needed
            except Exception:                         # noqa: BLE001
                pass
            msg = run(a[0], a[1:])
            if msg:
                sys.stdout.write(json.dumps({"systemMessage": msg}) + "\n")
    except Exception:                                 # noqa: BLE001 - never fail a turn
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
