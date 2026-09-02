#!/usr/bin/env python3
"""Write a file even when the destination resists, and never fail silently.

WHY THIS EXISTS. On 2026-08-27 an overnight scan died one minute after being
reported as running, on:

    PermissionError: [Errno 1] Operation not permitted:
      '~/gt-test-vault/scan_results/results.json'

It then sat dead for 8.5 hours. The write failed, the process exited, and
nothing recorded that anything was outstanding.

SCOPE, measured rather than assumed (2026-08-28): the VAULT is fully writable
and always was -- every vault write that day succeeded. The blocked tree was
`~/gt-test-vault/`, which lies outside the session's working directory.
So this is not a vault workaround. It is for any destination that turns out to
be unwritable, which is only discoverable by trying.

## Strategy, in order

  1. atomic replace   write a sibling temp file, fsync, os.replace
  2. direct write     truncate and write in place
  3. sidecar          write next to the target as <name>.pending-<stamp>
  4. ledger-only      write into the ledger directory itself

The first success wins. Anything below (1) records an entry in the ledger so the
outstanding move is visible and replayable, rather than lost.

## The ledger

`~/.claude/safe_write_ledger.jsonl`, append-only, one JSON object per line:

    {"ts", "target", "wrote_to", "strategy", "action", "done"}

`action` is what a human or a later run still needs to do -- usually "mv
<wrote_to> <target>", sometimes "rm <stale> then mv". `replay()` attempts every
outstanding entry and marks the ones that succeed, so the ledger drains itself
once the obstruction clears.

Deliberately NOT automatic: nothing here deletes a target to make room. A
destructive step on a path already behaving unexpectedly is how a bad situation
becomes an unrecoverable one. Deletions are recorded as an action for a human.
"""
import json
import os
import sys
import tempfile
import time

LEDGER = os.path.expanduser("~/.claude/safe_write_ledger.jsonl")

# EDIT ATTRIBUTION (2026-08-29). Every successful write also records WHICH SESSION
# on WHICH MACHINE touched the file, so `git blame` gains the one dimension it
# cannot have: two Claude Code sessions commit as the same human from the same
# account, and the 2026-08-28 near-miss needed exactly that distinction.
#
# Kept in a SEPARATE module and a separate file from LEDGER above on purpose --
# LEDGER means "a write did not land where it should and a human must act", which
# `outstanding()` and `replay()` both depend on. Logging every successful write
# into it would make `outstanding()` permanently non-empty and destroy the one
# signal it exists to give.
#
# Imported defensively: attribution is bookkeeping, and bookkeeping must never be
# able to fail a write.
try:
    import gt_edits as _gt_edits
except Exception:                                   # pragma: no cover
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import gt_edits as _gt_edits
    except Exception:
        _gt_edits = None


def _attribute(target, strategy):
    if _gt_edits is None:
        return
    try:
        _gt_edits.record(target, strategy)
    except Exception:
        pass


def _ledger_dir():
    d = os.path.dirname(LEDGER)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = tempfile.gettempdir()
    return d


def _record(entry):
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry.setdefault("done", False)
    for path in (LEDGER, os.path.join(tempfile.gettempdir(), "safe_write_ledger.jsonl")):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
            return path
        except Exception:
            continue
    return None


def _resolve_append(target, data, mode):
    """Turn an append into the equivalent whole-file write, before any strategy runs.

    Only strategy 2 writes to the target in place. Strategy 1 replaces it
    (`os.replace`), and strategies 3 and 4 write a NEW file whose recorded replay
    action is `mv <new> <target>`. Under any of those three, opening with mode="a"
    appends to an *empty* file and the destination ends up holding only the new
    bytes -- an append that silently truncates, reporting success. Resolving it
    once, here, gives every strategy the complete old+new content, which is what
    makes the sidecar and ledger `mv` actions safe to replay.

    The absence test is a READ, never `os.path.exists`. `exists()` answers False for
    "I cannot tell" as well as for "not there": a file whose parent directory cannot
    be traversed reports False while holding content. Treating that as a new file
    reintroduced exactly the truncation this function exists to prevent, and the
    ledger's own replay then made it permanent. Only FileNotFoundError means absent;
    every other OSError means we could not read it, and we refuse.
    """
    if not _is_append(mode):
        return data, mode
    binary = "b" in mode
    existing = b"" if binary else ""
    try:
        with open(target, "rb" if binary else "r") as fh:
            existing = fh.read()
    except FileNotFoundError:
        pass                      # genuinely absent: an append is just a write
    except OSError as exc:
        # Every strategy that follows either replaces the target or records a `mv`
        # over it. Writing without what is already there is precisely the data loss
        # this function exists to prevent, so refuse -- a caller that cannot append
        # is recoverable; a truncated file may not be.
        raise OSError(
            "safe_write: cannot read %s in order to append to it (%s); "
            "refusing rather than replacing it with only the new bytes"
            % (target, exc))
    if binary and isinstance(data, str):
        data = data.encode("utf-8")     # mirrors the bytes->binary coercion in write()
    return existing + data, ("wb" if binary else "w")


def _is_append(mode):
    """True only for a real append mode.

    `"a" in mode` is a substring test, so it also matched "wa" -- not a valid mode
    for open(), but rewritten to "w" here it would have been silently accepted as a
    truncating write. Reject anything declaring more than one primary mode instead.
    """
    primary = [c for c in mode if c in "rwax"]
    if len(primary) > 1:
        raise ValueError("safe_write: ambiguous mode %r" % mode)
    return primary == ["a"]


def write(target, data, mode="w"):
    """Write `data` to `target`. Returns (path_written, strategy).

    Returns the path ACTUALLY written, which is not always `target`: strategies 3
    and 4 write a sibling or ledger file and leave `target` untouched, recording a
    `mv` for `replay()`. Callers that must know the bytes reached `target` have to
    check the returned path, not just that the call returned.

    Raises if every strategy fails, and -- for an append -- if `target` exists but
    cannot be read (refusing is safer than replacing it with only the new bytes),
    or if `mode` declares more than one primary mode.

    Note: strategy 1 replaces the target via `os.replace` of a fresh `mkstemp`
    file, so the target's permission bits, ownership and xattrs are NOT preserved
    (a 0666 file becomes 0600). Callers that depend on the mode must re-apply it.
    """
    if isinstance(data, bytes) and "b" not in mode:
        mode += "b"
    target = os.path.abspath(target)
    data, mode = _resolve_append(target, data, mode)
    parent = os.path.dirname(target)

    # 1. atomic replace via a sibling temp file
    try:
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".sw-")
        try:
            with os.fdopen(fd, mode) as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
            _attribute(target, "atomic")
            return target, "atomic"
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception as e_atomic:
        pass

    # 2. direct write in place
    try:
        with open(target, mode) as fh:
            fh.write(data)
        _attribute(target, "direct")
        return target, "direct"
    except Exception:
        pass

    # 3. sidecar beside the target
    stamp = time.strftime("%Y%m%d_%H%M%S")
    side = "%s.pending-%s" % (target, stamp)
    try:
        with open(side, mode) as fh:
            fh.write(data)
        _record({"target": target, "wrote_to": side, "strategy": "sidecar",
                 "action": "mv %s %s" % (side, target)})
        # Attribute against the INTENDED target, not the sidecar: the question
        # this answers is "who tried to change this file", and the answer is the
        # same whether or not the write reached its final path.
        _attribute(target, "sidecar")
        return side, "sidecar"
    except Exception:
        pass

    # 4. ledger directory as the last resort
    d = _ledger_dir()
    fallback = os.path.join(d, "pending_%s_%s" % (stamp, os.path.basename(target)))
    with open(fallback, mode) as fh:
        fh.write(data)
    _record({"target": target, "wrote_to": fallback, "strategy": "ledger-dir",
             "action": "mv %s %s" % (fallback, target)})
    _attribute(target, "ledger-dir")
    return fallback, "ledger-dir"


def outstanding():
    """-> list of ledger entries not yet marked done."""
    if not os.path.exists(LEDGER):
        return []
    seen, out = {}, []
    for line in open(LEDGER):
        try:
            e = json.loads(line)
        except Exception:
            continue
        seen[(e.get("target"), e.get("wrote_to"))] = e
    for e in seen.values():
        if not e.get("done") and os.path.exists(e.get("wrote_to") or ""):
            out.append(e)
    return out


def replay(dry_run=False):
    """Attempt every outstanding move. Marks the ones that succeed.

    Never deletes a target to make room -- if the target is in the way and
    cannot be replaced, the entry stays outstanding and a human decides.
    """
    results = []
    for e in outstanding():
        src, dst = e["wrote_to"], e["target"]
        if dry_run:
            results.append((dst, "would-move"))
            continue
        try:
            os.replace(src, dst)
            _record({"target": dst, "wrote_to": src, "strategy": e.get("strategy"),
                     "action": "completed", "done": True})
            results.append((dst, "moved"))
        except Exception as ex:
            results.append((dst, "still-blocked: %s" % type(ex).__name__))
    return results


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        o = outstanding()
        print("outstanding writes: %d" % len(o))
        for e in o:
            print("  %s\n      -> %s\n      %s" % (e["wrote_to"], e["target"], e["action"]))
    elif cmd == "replay":
        for dst, what in replay():
            print("  %-60s %s" % (dst, what))
    elif cmd == "selftest":
        import shutil
        d = tempfile.mkdtemp()
        p, s = write(os.path.join(d, "a.txt"), "hello")
        print("writable dir  -> %s via %s" % (p, s))
        blocked = "/System/Library/__nope__/x.txt"
        p, s = write(blocked, "hello")
        print("blocked dir   -> %s via %s" % (p, s))
        print("outstanding   -> %d" % len(outstanding()))
        shutil.rmtree(d, ignore_errors=True)
