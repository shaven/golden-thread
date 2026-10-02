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

  0. in-place append  (append mode only) open(target, "a") -- what an append IS
  1. atomic replace   write a sibling temp file, fsync, os.replace
  2. direct write     truncate and write in place -- reported as "direct-degraded"
  3. sidecar          write next to the target as <name>.pending-<stamp>
  4. ledger-only      write into the ledger directory itself

The first success wins. An append that cannot land in place (0) is first
resolved into old bytes + new bytes, so none of the replacing strategies below
it can truncate the target. Strategies 3 and 4 are the ones that do NOT reach
`target`, and each records a ledger entry so the outstanding move is visible and
replayable rather than lost. Strategies 0, 1 and 2 all land ON `target`, so they
leave nothing outstanding and write no ledger entry -- what they owe the caller
is an honest strategy name and, when they degraded, a reason on stderr.

## Degradation is reported, never swallowed (2026-09-17, completed 2026-09-18)

Strategy 2 is NOT atomic: it truncates the target and writes into it, so a crash
or a full disk mid-write leaves the file short. Until 2026-09-17 a failed (1) fell
through to (2) under a bare `except Exception: pass` and returned the strategy
name "direct" -- the same name strategy 0 returns for a perfectly ordinary
in-place append. A caller had no way to tell a guaranteed-atomic write from a
truncating one, and the reason (1) failed was discarded on the way past.

So: strategy 2 returns "direct-degraded", and the exception from (1) is
carried forward -- printed once to stderr before the fallback runs, and recorded
as `why_not_atomic` on the ledger entries for (3) and (4).

That fix stopped one rung short. Strategies 2 and 3 kept their own bare
`except Exception: pass`, so a vault that fell all the way to a sidecar or to the
ledger directory still could not say WHY the two strategies above it had failed --
the same defect, one level down. As of 2026-09-18 every rung reports: each failure
is described on stderr before the next rung is attempted, and the reasons ride
into the ledger as `why_not_atomic`, `why_not_direct` and `why_not_sidecar`.

The fallback chain itself stays: on a filesystem where `os.replace` genuinely
cannot work, a truncating write is the only write there is. The defect was the
silence, not the fallback.

## NOT concurrency-safe -- read the name narrowly

"Safe" here means ONLY "safe against a destination that refuses the write":
it tries progressively weaker strategies and records what did not land. It is
not safe against another writer. Specifically, it has:

  * NO change detection -- it never compares the target against what the caller
    last saw, so a concurrent modification between read and write is overwritten
    without a word (strategies 1 and 2 both replace the whole file);
  * NO read-back verification -- nothing re-reads the target to confirm the bytes
    that landed are the bytes handed in;
  * NO locking -- two processes calling write() on one path race, and with
    strategy 1 the last `os.replace` simply wins.

The in-place append (strategy 0) is the exception, and only by luck of the OS:
line-sized appends to a file opened "a" are effectively atomic per call. Every
other path rewrites the file entire. Callers needing any of the three guarantees
above must provide them themselves.

## The ledger

`~/.claude/safe_write_ledger.jsonl`, append-only, one JSON object per line:

    {"ts", "target", "wrote_to", "strategy", "action", "done",
     "why_not_atomic", "why_not_direct"[, "why_not_sidecar"]}

`action` is what a human or a later run still needs to do: always "mv <wrote_to>
<target>" on an outstanding entry, and "completed" on the entry `replay()` writes
once the move has happened. The `why_not_*` fields describe, in order, the
exceptions that defeated the stronger strategies; `why_not_sidecar` is present
only on a strategy-4 (ledger-dir) entry, because on a sidecar entry strategy 3 is
the one that succeeded.

`replay()` attempts every outstanding entry and marks the ones that succeed, so
the ledger drains itself once the obstruction clears.

Deliberately NOT automatic: nothing here deletes or overwrites a target to make
room. A destructive step on a path already behaving unexpectedly is how a bad
situation becomes an unrecoverable one, so `replay()` moves a file into place only
when nothing is there -- an occupied target leaves the entry outstanding for a
human. (Until 2026-09-18 replay used `os.replace`, which overwrites whatever is at
the destination; the docstring said it never would, and it did.)
"""
import json
import os
import stat
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


def _describe(exc):
    """`TypeName: message`, for a reason that has to survive into a log line."""
    return "%s: %s" % (type(exc).__name__, exc)


def _warn_degraded(target, why):
    """Say on stderr that the atomic write failed and a weaker one is being tried.

    Printed BEFORE the fallback runs, so the reason is on the record even if what
    follows crashes the process. Wrapped, because a closed or non-writable stderr
    must not be able to fail a write -- but unlike the bare `except: pass` this
    replaces, the failure it is reporting is no longer invisible when stderr works.
    """
    try:
        sys.stderr.write(
            "safe_write: atomic write to %s failed (%s); falling back to a "
            "NON-ATOMIC truncating write\n" % (target, why))
        sys.stderr.flush()
    except Exception:                               # pragma: no cover
        pass


def _warn_fallback(target, failed, why, nxt):
    """Say on stderr that `failed` did not work for `target`, and what is tried next.

    The same contract as _warn_degraded one rung down: printed BEFORE the next
    strategy runs, so the reason survives even if that one crashes the process, and
    wrapped only because a broken stderr must not be able to fail a write. Strategies
    2 and 3 used a bare `except Exception: pass` until 2026-09-18, so a write that
    ended in a sidecar or the ledger directory could not say what had gone wrong
    above it.
    """
    try:
        sys.stderr.write("safe_write: %s for %s failed (%s); %s\n"
                         % (failed, target, why, nxt))
        sys.stderr.flush()
    except Exception:                               # pragma: no cover
        pass


def _move_if_absent(src, dst):
    """Move `src` onto `dst`, but ONLY when nothing is at `dst`.

    `os.replace` is the obvious call and the wrong one: it silently destroys whatever
    is at the destination, which is precisely what replay() promises never to do. A
    hard link fails with FileExistsError when `dst` is taken -- atomically, with no
    check-then-act window -- and the exclusive-create fallback covers a destination on
    another filesystem or one without hard links, where `open(dst, "x")` gives the same
    all-or-nothing guarantee.
    """
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError:
        with open(src, "rb") as fh:
            payload = fh.read()
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
    os.unlink(src)


def _existing_bytes(target):
    """Bytes currently in `target`, or b"" when it is genuinely absent.

    Used only when an in-place append has already failed and the append must be
    resolved into a whole-file write, so that the replacing strategies cannot
    truncate the target. Read as BYTES, never text: the old content is not ours to
    re-encode. The 0.9.6 text round-trip rewrote every CRLF in the file to LF and
    raised UnicodeDecodeError -- a ValueError, not an OSError -- on a single
    Latin-1 byte, so an append changed lines it never touched or crashed with an
    exception the contract did not mention (found 2026-09-05).

    The absence test is a READ, never `os.path.exists`. `exists()` answers False for
    "I cannot tell" as well as for "not there": a file whose parent directory cannot
    be traversed reports False while holding content. Treating that as a new file
    reintroduced exactly the truncation this function exists to prevent, and the
    ledger's own replay then made it permanent. Only FileNotFoundError means absent;
    every other OSError means we could not read it, and we refuse.
    """
    try:
        with open(target, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return b""                # genuinely absent: an append is just a write
    except OSError as exc:
        # Every strategy that follows either replaces the target or records a `mv`
        # over it. Writing without what is already there is precisely the data loss
        # this function exists to prevent, so refuse -- a caller that cannot append
        # is recoverable; a truncated file may not be.
        raise OSError(
            "safe_write: cannot read %s in order to append to it (%s); "
            "refusing rather than replacing it with only the new bytes"
            % (target, exc))


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


def _copy_mode(src, dst):
    """Carry the target's permission bits onto its replacement. mkstemp creates
    0600, so without this an atomic replace silently turned a 0666 file into one
    only its owner could read."""
    try:
        os.chmod(dst, stat.S_IMODE(os.stat(src).st_mode))
    except Exception:
        pass


def write(target, data, mode="w"):
    """Write `data` to `target`. Returns (path_written, strategy).

    Returns the path ACTUALLY written, which is not always `target`: strategies 3
    and 4 write a sibling or ledger file and leave `target` untouched, recording a
    `mv` for `replay()`. Callers that must know the bytes reached `target` have to
    check the returned path, not just that the call returned.

    The strategy name is the truth about HOW the bytes landed, and the five names
    are distinct on purpose:

        "atomic"          strategy 1: temp file, fsync, os.replace. The only
                          name that promises the target was never half-written.
        "direct"          strategy 0 ONLY: an in-place append, which is what an
                          append is -- never a fallback.
        "direct-degraded" strategy 2: the atomic write FAILED and the target was
                          truncated and rewritten in place. It landed, but not
                          atomically; the reason is on stderr.
        "sidecar" /       strategies 3 and 4: the bytes are NOT at `target` at
        "ledger-dir"      all, and a ledger entry holds the outstanding `mv`.

    No failed strategy is silent any more. Each one describes its exception on stderr
    before the next is attempted, and the reasons are carried into the ledger entry
    written by 3 and 4 as `why_not_atomic`, `why_not_direct` and (on a ledger-dir
    entry) `why_not_sidecar`. Strategies 0, 1 and 2 reach `target` and so leave
    nothing outstanding: they write no ledger entry, by design.

    An APPEND is tried in place first, which is what an append is: O(new bytes),
    atomic per call for line-sized writes, and the inode, permission bits, line
    endings and encoding of the old content are never touched. Only if that fails
    is it resolved into old bytes + new bytes so the replacing strategies cannot
    truncate the target. (0.9.6 resolved first and rewrote the whole file on every
    append; a 300 KB log paid a full rewrite, fsync and Dropbox re-upload to land
    one line, two sessions appending together lost lines, and a failed fallback
    left the file empty.)

    `target` is resolved through symlinks before anything is written: every
    replacing strategy would otherwise swap the LINK for a regular file and leave
    the real file untouched.

    Raises if every strategy fails, and -- for an append -- if `target` exists but
    cannot be read (refusing is safer than replacing it with only the new bytes),
    or if `mode` declares more than one primary mode.
    """
    if isinstance(data, bytes) and "b" not in mode:
        mode += "b"
    append = _is_append(mode)
    target = os.path.realpath(os.path.abspath(target))
    parent = os.path.dirname(target)

    # 0. in place -- the only strategy that appends rather than rewrites
    if append:
        try:
            with open(target, mode) as fh:
                fh.write(data)
            _attribute(target, "direct")
            return target, "direct"
        except Exception:
            pass
        if isinstance(data, str):
            data = data.encode("utf-8")
        data = _existing_bytes(target) + data
        mode = "wb"

    # 1. atomic replace via a sibling temp file
    try:
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".sw-")
        try:
            with os.fdopen(fd, mode) as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if os.path.exists(target):
                _copy_mode(target, tmp)
            os.replace(tmp, target)
            _attribute(target, "atomic")
            return target, "atomic"
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception as exc:
        # KEPT, not swallowed. Everything below this line is weaker than what the
        # caller asked for, and `why` is the only account of why it had to be.
        # (`exc` is unbound once this clause ends, so the reason is kept as text.)
        why = _describe(exc)

    _warn_degraded(target, why)

    # 2. direct write in place -- TRUNCATING. The bytes reach `target`, but the file
    # is empty for the length of the write, so the name must not say "atomic" and
    # must not be the "direct" a healthy in-place append returns.
    try:
        with open(target, mode) as fh:
            fh.write(data)
        _attribute(target, "direct-degraded")
        return target, "direct-degraded"
    except Exception as exc:
        # Same rule as strategy 1: KEPT, not swallowed. Everything below writes
        # somewhere that is not `target` at all, so why the last strategy that could
        # still reach it failed is the most useful line in the ledger entry.
        why_direct = _describe(exc)

    _warn_fallback(target, "in-place write", why_direct,
                   "writing a SIDECAR file beside it instead; the target is unchanged")

    # 3. sidecar beside the target
    stamp = time.strftime("%Y%m%d_%H%M%S")
    side = "%s.pending-%s" % (target, stamp)
    try:
        with open(side, mode) as fh:
            fh.write(data)
        _record({"target": target, "wrote_to": side, "strategy": "sidecar",
                 "action": "mv %s %s" % (side, target),
                 "why_not_atomic": why, "why_not_direct": why_direct})
        # Attribute against the INTENDED target, not the sidecar: the question
        # this answers is "who tried to change this file", and the answer is the
        # same whether or not the write reached its final path.
        _attribute(target, "sidecar")
        return side, "sidecar"
    except Exception as exc:
        why_sidecar = _describe(exc)

    _warn_fallback(side, "sidecar write", why_sidecar,
                   "writing into the ledger directory instead; the target is unchanged")

    # 4. ledger directory as the last resort
    d = _ledger_dir()
    fallback = os.path.join(d, "pending_%s_%s" % (stamp, os.path.basename(target)))
    with open(fallback, mode) as fh:
        fh.write(data)
    _record({"target": target, "wrote_to": fallback, "strategy": "ledger-dir",
             "action": "mv %s %s" % (fallback, target),
             "why_not_atomic": why, "why_not_direct": why_direct,
             "why_not_sidecar": why_sidecar})
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

    Never deletes or overwrites a target to make room -- if anything at all is at the
    target path, the entry stays outstanding and a human decides. That is what
    `_move_if_absent` buys: until 2026-09-18 this used `os.replace`, which overwrites
    an existing destination, so a target that had been rewritten in the meantime was
    destroyed by the very routine documented as never doing so.
    """
    results = []
    for e in outstanding():
        src, dst = e["wrote_to"], e["target"]
        if dry_run:
            results.append((dst, "would-move"))
            continue
        try:
            _move_if_absent(src, dst)
            _record({"target": dst, "wrote_to": src, "strategy": e.get("strategy"),
                     "action": "completed", "done": True})
            results.append((dst, "moved"))
        except FileExistsError:
            results.append((dst, "still-blocked: something is already at the target; "
                                 "left in place for a human"))
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
