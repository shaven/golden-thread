#!/usr/bin/env python3
"""Announce that this session is live, and what it has open.

WHY THIS EXISTS. On 2026-08-28 two Claude Code sessions worked this vault at the
same time. One built `claudebox` and wrote `index.md`, `INFRASTRUCTURE.md`,
`log.md` and a memory file. The other ran MSv6/NVDA work and wrote `research.md`,
`TASKS.md`, `log.md` and a project `README.md` -- growing `research.md` from +73
to +124 lines within a few minutes. Neither could see the other.

Git does not help. Every session shares one working tree, so there are no
branches to collide, no merge, and no conflict markers -- just last-writer-wins.
A whole-file rewrite, or a `git checkout` meant to revert your own edit, silently
destroys the other session's uncommitted work, and it is invisible when it
happens. Nothing was lost that day only because one session ran `git status`
before committing and noticed files it had never touched.

## The model

One file per live session, named for its session id and when it opened:

    Projects/golden-thread/sessions/<session-id>_<YYYY-MM-DD>_<HHMM>.md

The opened-at stamp is in the NAME so a bare `ls` shows who is live and since
when -- a file from two days ago is visibly abandoned without opening it.

It carries a `last_execution` heartbeat so other sessions can tell a working
session from an abandoned one, and a body of markdown bullets -- one ``- `path` ``
per claimed file -- saying what is open. (There is no `files_claimed` frontmatter
key; the docstring claimed one until 0.16.5 and no code ever wrote or read it.)
A session that finishes its writes REMOVES its file -- absence means done.

    register   announce this session and what it intends to touch
    beat       refresh last_execution (call on every execution)
    claim      add files to this session's claim, refreshing the heartbeat
    check      who holds a given file? exit 1 if someone else does
    list       every live session, with stale ones flagged
    release    delete this session's file -- writing is finished

## Exit codes

    0   done
    1   refused, or not recorded: a live session holds the file, the id is
        already open in another live process, or the write never won its swap.
        In every case NOTHING of what you asked for was written.
    2   the command could not be aimed: no session id, no registration, or an
        AMBIGUOUS id (two registrations share it -- see below). Nothing written.

## Staleness, not locking

This is advisory. A session that crashes leaves its file behind, so a claim is
only trusted while its heartbeat is fresh (default 30 min, --stale-after).
Beyond that it is reported STALE and may be ignored. Deliberately not a hard
lock: a stuck lock in a single working tree is worse than a stale hint.

Advisory about WHOSE files they are, though -- not about whether a write lands.
Every change to an EXISTING session file -- `beat`, `claim`, and since 0.16.5
re-`register` too -- goes through a compare-and-swap (see `_update`), so two
processes sharing one id cannot lose each other's claims, and a write that cannot
be made says so and exits non-zero. Two paths are not a CAS, on purpose:

  * creating a registration that does not exist yet (`register` on a fresh id, or
    `--new`) is a single atomic write -- there are no claims to lose;
  * `release` and replacing a DEAD predecessor delete a file outright. Both say
    how many claims go with it rather than dropping them in silence.

And the guarantee is only as good as the filesystem. The CAS's check and rename
are made indivisible by an flock; where flock does not exist the window reopens
and a claim CAN be lost. That is not silent either -- see `_degraded`.

## One id, two registrations

`--new`, and a race inside one minute, both produce two files for one session id.
Nothing can tell them apart from the outside: `$CLAUDE_PID` is IDENTICAL for a
session, its subagents and its hooks, and each CLI run has its own os.getpid(),
so there is no durable handle a later `claim` could match on. Until 0.16.5 the
lookup simply took the newest-sorting name, which meant a process that had stepped
aside into the `-<pid>` name addressed the OTHER process's file on every command
-- its claims landed there and the other process's `release` cleared them.

So an ambiguous id is now REFUSED (exit 2), not guessed: name the registration
with `--entry` (or `$GT_SESSION_FILE`). A process never silently operates on
another process's registration.
"""

import argparse
import datetime
import errno
import hashlib
import os
import pathlib
import re
import socket
import stat
import sys
import tempfile
import time

try:
    import fcntl                        # POSIX only; _hold explains the fallback
except ImportError:                     # pragma: no cover -- not a platform this vault runs on
    fcntl = None

STALE_AFTER_MIN = 30
# How many times a read-modify-write is redone when another writer beat it to the
# file. Five is far more than a real race needs (each pass is two reads, a staged
# write and a rename); the number exists so a pathological case FAILS, not loops.
CAS_ATTEMPTS = 5
# Where this copy of the tool was installed (<vault>/Projects/golden-thread/tools/),
# overridable by $GT_VAULT and then by --vault. Until 0.12.0 this was a hardcoded
# parents[3] with no way to point the tool anywhere else, so a session could not
# rehearse against a copy -- see core_explicit_vault_target.
VAULT = pathlib.Path(os.environ.get("GT_VAULT")
                     or pathlib.Path(__file__).resolve().parents[3])
SESSIONS = VAULT / "Projects" / "golden-thread" / "sessions"
DRY_RUN = False


def use_vault(path):
    """Repoint the tool at another vault. Called once, from main()."""
    global VAULT, SESSIONS
    VAULT = pathlib.Path(path)
    SESSIONS = VAULT / "Projects" / "golden-thread" / "sessions"


def dry(action):
    """True if this write should be described instead of performed."""
    if DRY_RUN:
        print("dry run: would %s" % action)
    return DRY_RUN
# A numeric offset, not a zone name: strptime discards %Z, so a heartbeat written
# in another zone was misread by hours and a live claim looked dead.
TS_FMT = "%Y-%m-%d %H:%M:%S %z"
# Zone names in heartbeats written before the offset format (hours from UTC).
OLD_ZONES = {"UTC": 0, "GMT": 0, "Z": 0, "EST": -5, "EDT": -4, "CST": -6, "CDT": -5,
             "MST": -7, "MDT": -6, "PST": -8, "PDT": -7, "AKST": -9, "AKDT": -8,
             "HST": -10, "CET": 1, "CEST": 2, "JST": 9}


def _now():
    return datetime.datetime.now().astimezone()


def _stamp(dt=None):
    return (dt or _now()).strftime(TS_FMT).strip()


def session_id(explicit=None):
    """Resolve the session id: --id, then env, then the cwd-derived fallback."""
    if explicit:
        return explicit
    # CLAUDE_CODE_SESSION_ID is the one Claude Code actually sets (verified
    # 2026-08-28); the others are checked in case that name changes.
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID"):
        if os.environ.get(var):
            return os.environ[var].strip()
    # Last resort: any env value carrying a uuid (scratchpad paths do).
    # macOS TMPDIR is /var/folders/..., so do not special-case it.
    uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    for val in os.environ.values():
        m = uuid_re.search(val)
        if m:
            return m.group(0)
    return None


def _my_pid():
    """The identity this process registers under.

    $CLAUDE_PID is the Claude Code session's pid, shared by the parent, every
    subagent and every hook inside that session -- deliberately, because they ARE
    one session as far as a claim is concerned. It is therefore NOT a way to tell
    a subagent's registration from its parent's; see the module docstring.
    """
    return str(os.environ.get("CLAUDE_PID") or os.getpid())


def _candidates(sid):
    """Every registration file carrying this session id, oldest name first."""
    return sorted(SESSIONS.glob(f"{sid}_*.md")) if SESSIONS.exists() else []


def _ambiguous(sid, cands):
    return (f"session id {sid} has {len(cands)} registrations and none of them is\n"
            f"  unambiguously this process's:\n"
            + "".join(f"    {c.name}   pid {_parse(c)[0].get('pid','?')}\n" for c in cands)
            + f"  Refusing to guess: until 0.16.5 this picked the newest-sorting name,\n"
              f"  which is how one process's claims ended up in another's file and were\n"
              f"  then cleared by its release. Name the one you mean:\n"
              f"    --entry {cands[-1].name}      (or $GT_SESSION_FILE)\n"
              f"  `gt_session.py list` shows what each one holds.")


def _select(sid, entry=None):
    """Which registration does THIS process address? -> (path or None, error or None).

    Session files are named  <session-id>_<YYYY-MM-DD>_<HHMM>[-<pid>].md -- the
    opened-at stamp is in the name so a bare `ls` shows who has been live and for
    how long. Lookup globs on the id, since the stamp is not known to later
    commands; the `-<pid>` suffix appears only when two processes registered one
    id inside the same minute.

    One hit is the answer. Several hits are NOT resolved by sort order (that was
    the H8 defect: `_1702.md` sorts after `_1702-73851.md` because `.` > `-`, so
    the stepped-aside process could never address its own file). They are resolved
    by an explicit `--entry`/$GT_SESSION_FILE, or by our own pid when exactly one
    registration carries it -- and otherwise refused.
    """
    cands = _candidates(sid)
    if not cands:
        legacy = SESSIONS / f"{sid}.md"          # pre-stamp files, still readable
        return (legacy if legacy.exists() else None), None
    if len(cands) == 1:
        return cands[0], None
    entry = entry or os.environ.get("GT_SESSION_FILE")
    if entry:
        hit = [c for c in cands if entry in (c.name, str(c), c.stem)]
        if len(hit) == 1:
            return hit[0], None
        return None, (f"--entry {entry} matches {len(hit)} of the {len(cands)} "
                      f"registrations for {sid}")
    mine = [c for c in cands if _parse(c)[0].get("pid", "") == _my_pid()]
    if len(mine) == 1:
        return mine[0], None
    return None, _ambiguous(sid, cands)


def _aim(args, verb):
    """Resolve the session id and the registration for a command. -> (sid, path, rc).

    rc is None when the command may proceed; otherwise it is the exit code and the
    reason has already been printed.
    """
    sid = session_id(getattr(args, "id", None))
    if not sid:
        print("cannot resolve session id -- pass --id", file=sys.stderr)
        return None, None, 2
    p, err = _select(sid, getattr(args, "entry", None))
    if err:
        print(err, file=sys.stderr)
        return sid, None, 2
    if p is None or not p.exists():
        print(f"no session file for {sid} -- run `register` first "
              f"(nothing to {verb})", file=sys.stderr)
        return sid, None, 2
    return sid, p, None


def _parse(path):
    """-> (frontmatter dict, body str). Tolerant of hand-edited files."""
    return _parse_text(path.read_text())


def _parse_text(text):
    """The parse, split out from the read: a compare-and-swap pass already holds
    the bytes it is going to swap against, and must parse exactly those -- not
    whatever a second read would return."""
    fm, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
            body = text[end + 4:].lstrip("\n")
    return fm, body


def _render(fm, body):
    keys = ["session_id", "agent", "task", "started", "last_execution", "status", "cwd"]
    ordered = [k for k in keys if k in fm] + [k for k in fm if k not in keys]
    lines = ["---"] + [f"{k}: {fm[k]}" for k in ordered] + ["---", ""]
    return "\n".join(lines) + body


# --- writing a session file without losing another writer's claims -------------
#
# Until 0.16.4 every write here was `_parse(p)` -> mutate -> `p.write_text(...)`:
# a plain read-modify-write with nothing between the read and the write. Two
# processes sharing one session id -- a --resume beside the run it resumed, a
# hook firing next to the session it describes, an agent and its parent -- could
# interleave, and the second write silently dropped the first one's claims.
#
# That loss is not cosmetic. This registry IS the mechanism Core rule 1 rests on
# ("never write a file another live session has claimed"); gt_lint_weekly.py reads
# it before deciding INBOX.md is safe to touch; and since 0.16.2 gt_demote.py
# refuses to move a note another live session has claimed by reading these same
# files. A dropped claim disarms all three at once, and nothing anywhere prints a
# word about it: the tool that lost the claim reports success.
#
# The fix is compare-and-swap. Deliberately NOT a lock FILE: a `.lock` left behind
# by a session that was killed mid-write wedges the registry for every session
# after it, which is the same "stuck lock in a single working tree" this tool
# refuses at the top of this file, and a worse failure than the one being fixed.
# A CAS leaves nothing behind -- a writer that dies simply never wins its swap.
#
# One pass: read the bytes and remember their sha256, build the new content from
# exactly those bytes, stage it in a temp file, re-read the target and confirm the
# digest has not moved, then os.replace the temp over it. If the digest HAS moved,
# the whole pass runs again against the new content -- so the mutation (new claim
# lines, a fresh heartbeat) is applied ON TOP of the other writer's work rather
# than over it, and the two sets of claims union. After CAS_ATTEMPTS losses the
# command fails loudly and non-zero. "Could not record" must never reach the user
# as "recorded".
#
# The check and the rename are two separate syscalls, and a plain CAS loses the
# file to anyone who lands between them. That window is not theoretical: four
# concurrent claimers on one session id dropped a claim in roughly half of the
# measured runs, because every writer stages and fsyncs before it renames and the
# stagger between two renames is milliseconds, not microseconds. So the check and
# the rename are made indivisible by an flock held on the target's own descriptor
# for exactly those two calls.
#
# That flock is NOT the forbidden lock file. It creates no file, it is a property
# of an open descriptor, and the kernel drops it the instant the process exits
# however it exits -- there is no state for a killed session to leave behind. It
# is taken LOCK_NB and given up after a short spin, so a wedged process makes a
# claim FAIL loudly rather than block forever. And it is only an optimisation of
# the window: correctness still rests on the digest comparison, which is why the
# code keeps working (with the window reopened) where flock does not exist.


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _default_mode():
    """What `open(path, 'w')` would have given a new file here: 0666 less the umask."""
    cur = os.umask(0)                   # the only way to read it is to set it
    os.umask(cur)
    return 0o666 & ~cur


def _stage(path, text, mode=None):
    """Write `text` to a temp file beside `path`, ready to be renamed over it.

    The temp file gets a RANDOM name from mkstemp, not `<target>.tmp`: a fixed
    name is itself a collision between concurrent writers -- the second one
    scribbles over the first one's half-finished temp file and then renames that
    mixture into place. Random names cannot collide. The fsync happens here, so
    that the rename which follows is durable AND is the only slow-path-free step
    left between the compare and the swap.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp makes 0600. Restore the mode the file already had, or -- for one
        # being created -- the mode a plain write would have given it, so that
        # going through a temp file never narrows a session file's permissions.
        os.chmod(tmp, _default_mode() if mode is None else mode)
    except BaseException:
        _discard(tmp)
        raise
    return tmp


def _discard(tmp):
    """Remove a staged temp file. A no-op once it has been renamed into place."""
    try:
        os.unlink(tmp)
    except OSError:
        pass


def _atomic_write(path, text, mode=None):
    """Put `text` at `path` in one indivisible step, preserving `mode`.

    os.replace is atomic on POSIX, so a concurrent reader -- `list`, `check`, the
    guard hook, gt_demote.py -- sees either the whole old file or the whole new
    one, never a half-written frontmatter.
    """
    os.replace(_stage(path, text, mode), str(path))


# Errnos that mean "this filesystem will never give you an flock", as opposed to
# "someone else holds it": a network mount, a FAT volume, a kernel without the
# call. Retrying these is pointless, and failing on them would make the tool
# unusable on a vault in Dropbox/NFS -- so the run goes on, loudly degraded.
# EBADF is deliberately NOT here: a bad descriptor is a bug in this file, not a
# property of the mount, and swallowing it would hide one behind a warning.
_NO_FLOCK = {getattr(errno, n) for n in
             ("ENOLCK", "ENOSYS", "EOPNOTSUPP", "ENOTSUP", "EINVAL")
             if hasattr(errno, n)}
DEGRADED = None                         # the reason claim writes are unguarded here
_WARNED = False


def _degraded(reason):
    """Say ONCE, on stderr, that claim writes here are not lock-guarded.

    H7: `_hold` returning True without a lock is CORRECT -- the CAS still runs and
    a vault on a mount without flock must keep working -- but it is not equivalent.
    Measured with four concurrent claimers, ten trials each: with flock, 0/10 runs
    dropped a claim; without it, 9/10 did, and every process printed success. A
    degradation that nothing announces is exactly the "reports success, recorded
    nothing" failure this whole module exists to stop, so it is announced: here on
    every write path, and by `list` and `check` for anyone surveying the vault.
    """
    global DEGRADED, _WARNED
    DEGRADED = reason
    if _WARNED:
        return                          # once per run, not once per attempt
    _WARNED = True
    print(f"WARNING: session-claim writes are NOT lock-guarded on this vault\n"
          f"  reason: {reason}\n"
          f"  The compare-and-swap still runs, but its check and its rename are no\n"
          f"  longer indivisible, so two processes writing one session id at the same\n"
          f"  moment CAN silently lose a claim (measured: 9 of 10 trials with four\n"
          f"  concurrent claimers). Claims recorded here are best-effort: this vault's\n"
          f"  filesystem cannot guarantee claim integrity.", file=sys.stderr)


def _hold(fh, tries=50, pause=0.002):
    """Take the OS lock on this open descriptor, briefly. -> True if held.

    LOCK_NB with a short spin rather than a blocking wait: the lock is held for
    two syscalls, so anyone who cannot get it inside 100ms is not merely busy, and
    the honest answer then is to fail the command, not to hang the session.
    Where flock does not exist the CAS runs unguarded -- correct, with the
    check-to-rename window back open, and `_degraded` says so out loud.
    """
    if fcntl is None:
        _degraded("this Python build has no fcntl module")
        return True
    for _ in range(tries):
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno in _NO_FLOCK:
                _degraded(f"the filesystem holding this vault does not support "
                          f"flock ({errno.errorcode.get(e.errno, e.errno)})")
                return True
            time.sleep(pause)           # merely contended: spin
    return False


def _lock_probe():
    """-> the reason claim writes cannot be guarded here, or None if they can.

    A read-only survey (`list`, `check`) never takes a lock, so it would otherwise
    never learn that this vault cannot hold one. It tries for real rather than
    guessing from the mount type: stage a temp file where session files live and
    flock it.
    """
    if fcntl is None:
        return "this Python build has no fcntl module"
    d = SESSIONS if SESSIONS.is_dir() else (VAULT if VAULT.is_dir() else None)
    if d is None:
        return None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".gt-lock-probe.", suffix=".tmp")
    except OSError:
        return None                     # cannot probe; do not invent a verdict
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in _NO_FLOCK:
                return (f"the filesystem holding this vault does not support flock "
                        f"({errno.errorcode.get(e.errno, e.errno)})")
        return None
    finally:
        os.close(fd)
        _discard(tmp)


def _commit_if_unchanged(path, tmp, before):
    """Swap `tmp` into `path`, but only while `path` still holds `before`."""
    try:
        fh = open(str(path), "rb")
    except FileNotFoundError:
        return False
    try:
        if not _hold(fh):
            return False                # someone is mid-swap: redo the pass
        # Re-read BY PATH, never from the descriptor we locked. If another writer
        # has already renamed its own file into place, our descriptor still points
        # at the old, now-unlinked inode and would answer "unchanged" forever --
        # and we would then rename straight over their claims.
        try:
            if _sha(path.read_bytes()) != _sha(before):
                return False
        except FileNotFoundError:
            return False
        # Nobody can slip in between this check and this rename: to rename they
        # would have to hold the lock on the inode currently at `path`, which is
        # the inode we are holding.
        os.replace(tmp, str(path))
        return True
    finally:
        fh.close()                      # closing releases the flock


def _update(path, mutate, attempts=CAS_ATTEMPTS):
    """Read-modify-write `path` under compare-and-swap. -> True if it landed.

    `mutate(fm, body) -> (fm, body)` is called ONCE PER PASS, against the file as
    it is at that moment. That is the whole point: on a retry it sees the other
    writer's claims already in `body` and adds to them, instead of replaying a
    list computed before the race.

    False is a real failure -- every attempt lost its swap and NOTHING was
    written. It never means "probably fine"; callers must report it and exit
    non-zero.
    """
    for _ in range(attempts):
        try:
            before = path.read_bytes()
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            return False                # released (or never registered) mid-flight
        tmp = _stage(path, _render(*mutate(*_parse_text(before.decode()))), mode)
        try:
            if _commit_if_unchanged(path, tmp, before):
                return True
        finally:
            _discard(tmp)               # already gone if the swap happened
    return False


def _lost(path, what):
    """Report a read-modify-write that never won its swap. Always returns 1.

    Says the filename and says nothing was recorded, because the failure this
    whole mechanism exists to prevent is a claim that the user believes is held
    and that no file actually carries.
    """
    print(f"could not record {what}\n"
          f"  file : {path}\n"
          f"  another process rewrote it on every one of {CAS_ATTEMPTS} attempts.\n"
          f"  NOTHING WAS WRITTEN -- claims you believe you hold are NOT recorded,\n"
          f"  so do not treat this file as yours. Re-run the command; if it keeps\n"
          f"  failing, `gt_session.py list` and see whether two processes share\n"
          f"  this session id.", file=sys.stderr)
    return 1


def _parse_ts(raw):
    """-> aware datetime, or None. Reads the offset format and the older zone-name one."""
    raw = raw.strip()
    for fmt in (TS_FMT, "%Y-%m-%d %H:%M %z"):
        try:
            return datetime.datetime.strptime(raw, fmt)
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{4}-\d\d-\d\d \d\d:\d\d(:\d\d)?)(?:\s+([A-Za-z]+))?", raw)
    if not m:
        return None
    try:
        dt = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M" + (":%S" if m.group(2) else ""))
    except ValueError:
        return None
    zone = (m.group(3) or "").upper()
    if zone in OLD_ZONES and zone not in (n.upper() for n in time.tzname):
        return dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=OLD_ZONES[zone])))
    return dt.astimezone()          # no zone, our own, or unknown: local time, as before


def _age_min(fm):
    raw = fm.get("last_execution")
    dt = _parse_ts(raw) if raw else None
    return None if dt is None else (_now() - dt).total_seconds() / 60.0


def _pid_alive(fm):
    """Is the process behind this session file still running?

    Only meaningful on the machine that wrote it, so the host must match.
    Returns True/False, or None when it cannot be determined -- callers then
    fall back to the heartbeat age.
    """
    if fm.get("host") != socket.gethostname():
        return None
    raw = fm.get("pid")
    if not raw or not raw.strip().isdigit():
        return None
    try:
        os.kill(int(raw), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by someone else
    except OSError:
        return None


def _claimed_files(body):
    return re.findall(r"^-\s+`([^`]+)`", body, flags=re.M)


def _take_over(p, sid, args, how):
    """Re-register an EXISTING registration in place, keeping every claim it holds.

    H1: until 0.16.5 `register` never went through `_update`. It rebuilt the body
    from `--files` alone, and `--resume` unlinked the old file first -- so
    re-registering a session (which a --resume, a hook, or a subagent does as a
    matter of course) silently threw away every claim that session held, printed
    "registered", and exited 0. The registry that Core rule 1, gt_demote.py and
    gt_lint_weekly.py all read was disarmed by a command that looked like a no-op.

    So the rule is: nothing a live registration holds may be dropped by
    re-registering it. Existing claims are carried forward and `--files` is
    UNIONed into them -- never replaced. The frontmatter IS taken over (task,
    agent, pid, host, cwd move to this process; `started` stays, because the
    session opened when it opened), and it goes through the CAS like any other
    write, so a claim landing concurrently is merged rather than lost.
    """
    stamp = _stamp()
    carried, added = [], []

    def mutate(fm, body):
        have = _claimed_files(body)     # recomputed per pass: see _update
        new = [f for f in args.files if f not in have]
        carried[:], added[:] = have, new
        if new:
            body = re.sub(r"^_nothing claimed yet_\n?", "", body, flags=re.M)
            body = body.rstrip("\n") + "\n" + "".join(f"- `{f}`\n" for f in new)
        fm["session_id"] = sid
        fm["agent"] = args.agent
        if args.task:
            fm["task"] = args.task      # no --task keeps what the entry already says
        fm.setdefault("task", "(unstated)")
        fm.setdefault("started", stamp)
        fm["last_execution"] = stamp
        fm["status"] = "active"
        fm["pid"] = _my_pid()
        fm["host"] = socket.gethostname()
        fm["cwd"] = str(pathlib.Path.cwd())
        return fm, body

    if dry("take over %s (%s), keeping the %d claim(s) it holds and adding %s"
           % (sid, p, len(_claimed_files(_parse(p)[1])),
              " ".join(args.files) or "nothing")):
        return 0
    if not _update(p, mutate):
        return _lost(p, f"the registration for {sid}")
    print(f"{how} {sid}\n  {p.relative_to(VAULT)}\n"
          f"  carried forward {len(carried)} claim(s){': ' + ' '.join(carried) if carried else ''}\n"
          f"  added {len(added)} claim(s){': ' + ' '.join(added) if added else ''}")
    return 0


def cmd_register(args):
    sid = session_id(getattr(args, "id", None))
    if not sid:
        print("cannot resolve session id -- pass --id", file=sys.stderr)
        return 2
    if not DRY_RUN:
        SESSIONS.mkdir(parents=True, exist_ok=True)
    stale_after = getattr(args, "stale_after", STALE_AFTER_MIN)

    # Same session id already registered? That means either a --resume/--continue
    # of this conversation, a subagent or hook of it, or a crashed run of it.
    # Never quietly share one id between two processes -- the whole point is that
    # a claim identifies ONE writer. Decide it explicitly. (--new asks for a
    # second entry on purpose and skips all of this.)
    if not args.new:
        existing, err = _select(sid, getattr(args, "entry", None))
        if err:
            print(err, file=sys.stderr)
            return 2
        if existing is not None:
            efm, ebody = _parse(existing)
            alive = _pid_alive(efm)
            held = _claimed_files(ebody)
            # `mine` is pid equality and cannot be narrower: $CLAUDE_PID is shared
            # by a session, its subagents and its hooks, and each CLI run has its
            # own os.getpid(), so there is no handle that separates them. They are
            # therefore ONE registration -- which is safe now only because taking
            # it over unions claims instead of replacing them. A subagent that
            # genuinely wants its own entry asks for `--new`.
            if efm.get("pid", "") == _my_pid():
                return _take_over(existing, sid, args, "re-registered")
            if args.resume:
                return _take_over(existing, sid, args, "resumed")
            age = _age_min(efm)
            fresh = age is not None and age <= stale_after
            if alive is True or (alive is None and fresh):
                why = ("running" if alive is True
                       else "pid not judgeable here; heartbeat is fresh")
                print(f"session {sid} is ALREADY OPEN in another live process\n"
                      f"  file : {existing.relative_to(VAULT)}\n"
                      f"  pid  : {efm.get('pid','?')} on {efm.get('host','?')} ({why})\n"
                      f"  task : {efm.get('task','(unstated)')}\n"
                      f"  holds: {len(held)} claim(s){': ' + ' '.join(held) if held else ''}\n\n"
                      f"Two processes sharing one session id makes a claim ambiguous.\n"
                      f"  --resume   take over that registration, keeping its claims\n"
                      f"  --new      register a second, independently-tracked entry for this id",
                      file=sys.stderr)
                return 1
            # Demonstrably dead, or unjudgeable and long past its heartbeat. Its
            # claims MAY be released -- that is the point of detecting a dead
            # session -- but never in silence: say how many go with it, so a claim
            # that disappears is always a claim someone was told about.
            gone = "is dead" if alive is False else f"has not beaten in {stale_after:.0f}+ min"
            print(f"previous run of {sid} (pid {efm.get('pid','?')}) {gone} -- "
                  f"replacing its registration\n"
                  f"  releasing {len(held)} claim(s) it held"
                  f"{': ' + ' '.join(held) if held else ''}")
            if dry("delete %s and register %s afresh" % (existing, sid)):
                return 0
            existing.unlink()

    stamp = _now().strftime("%Y-%m-%d_%H%M")
    p = SESSIONS / f"{sid}_{stamp}.md"
    # A file already on this exact name means another process registered this id
    # within the same minute (--new asks for that on purpose; a race produces it
    # by accident). The old rule -- overwrite unless --new -- threw that
    # registration and its claims away. Step aside instead: the pid makes the name
    # unique, `list` shows both, and neither can address the other's file, because
    # an ambiguous id is now refused rather than resolved by sort order.
    if p.exists():
        p = SESSIONS / f"{sid}_{stamp}-{os.getpid()}.md"
    fm = {
        "session_id": sid,
        "agent": args.agent,
        "task": args.task or "(unstated)",
        "started": _stamp(),
        "last_execution": _stamp(),
        "status": "active",
        "pid": _my_pid(),
        "host": socket.gethostname(),
        "cwd": str(pathlib.Path.cwd()),
    }
    body = "# What this session has open\n\n"
    body += "".join(f"- `{f}`\n" for f in args.files) if args.files else "_nothing claimed yet_\n"
    if dry("register %s as %s" % (sid, p)):
        return 0
    # Atomic even though this file is new: `list`, `check` and the guard hook read
    # this directory from other processes at any moment, and a half-written
    # frontmatter parses as a session with no heartbeat and no claims.
    _atomic_write(p, _render(fm, body))
    print(f"registered {sid}\n  {p.relative_to(VAULT)}")
    if len(_candidates(sid)) > 1:
        print(f"  NOTE: {sid} now has {len(_candidates(sid))} registrations. Later\n"
              f"  commands must name this one: --entry {p.name}", file=sys.stderr)
    return 0


def cmd_beat(args):
    sid, p, rc = _aim(args, "beat")
    if rc is not None:
        return rc
    stamp = _stamp()

    def mutate(fm, body):
        fm["last_execution"] = stamp
        return fm, body                 # body untouched: a heartbeat never drops a claim

    # A heartbeat IS a write, and not only of our own file: `last_execution` is
    # what every other session's staleness judgement reads, so advancing it under
    # --dry-run changes what other processes conclude about us. It had no dry()
    # guard until 0.16.5.
    if dry("refresh the heartbeat for %s in %s" % (sid, p)):
        return 0
    if not _update(p, mutate):
        return _lost(p, f"the heartbeat for {sid}")
    print(f"heartbeat {sid} @ {stamp}")
    return 0


def cmd_claim(args):
    sid, p, rc = _aim(args, "claim")
    if rc is not None:
        return rc

    # refuse to claim what a live session already holds
    conflicts = []
    for other, ofm, obody, stale, opath in _live(getattr(args, "stale_after", STALE_AFTER_MIN)):
        # Compared by FILE, not by session id: with `--new` there are two
        # registrations under one id, and the other one's claims are somebody
        # else's even though the id matches ours.
        if opath == p or stale:
            continue
        for f in args.files:
            if f in _claimed_files(obody):
                conflicts.append((f, other))
    if conflicts and not args.force:
        for f, other in conflicts:
            print(f"CONFLICT  {f}  held by {other}", file=sys.stderr)
        print("\nStage your change in Projects/golden-thread/pending/ instead,\n"
              "or re-run with --force if you know the claim is dead.", file=sys.stderr)
        return 1

    stamp = _stamp()
    added = []                          # what the winning pass actually wrote

    def mutate(fm, body):
        # Every line of this is recomputed from the file as it stands NOW, on each
        # pass. That is what makes a retry union the two writers' claims: the other
        # process's lines are already in `body`, `have` sees them, and they survive
        # into the render. Hoisting any of it out of the closure would reintroduce
        # exactly the lost update this is here to stop.
        have = _claimed_files(body)
        new = [f for f in args.files if f not in have]
        if new:
            body = re.sub(r"^_nothing claimed yet_\n?", "", body, flags=re.M)
        body = body.rstrip("\n") + "\n" + "".join(f"- `{f}`\n" for f in new)
        fm["last_execution"] = stamp
        added[:] = new
        return fm, body

    if dry("claim %d file(s) for %s" % (len(args.files), sid)):
        return 0
    if not _update(p, mutate):
        return _lost(p, "claims for %s: %s" % (sid, " ".join(args.files)))
    print(f"claimed {len(added)} file(s) for {sid}")
    return 0


def _live(stale_after):
    out = []
    if not SESSIONS.exists():
        return out
    for p in sorted(SESSIONS.glob("*.md")):
        if p.name == "README.md":
            continue
        fm, body = _parse(p)
        age = _age_min(fm)
        alive = _pid_alive(fm)
        if alive is True:
            stale = False                      # the process is demonstrably running
        elif alive is False:
            stale = True                       # demonstrably dead, whatever the clock says
        else:
            stale = age is None or age > stale_after   # fall back to the heartbeat
        out.append((fm.get("session_id", p.stem.split("_")[0]), fm, body, stale, p))
    return out


def _my_registration(args):
    """This process's own registration file, or None. Never an error: the survey
    commands must still print a picture of the vault when our own id is ambiguous."""
    sid = session_id(getattr(args, "id", None))
    if not sid:
        return None, None
    p, err = _select(sid, getattr(args, "entry", None))
    return sid, (None if err else p)


def cmd_check(args):
    reason = _lock_probe()              # H7: a survey must say when claims are best-effort
    if reason:
        _degraded(reason)
    stale_after = getattr(args, "stale_after", STALE_AFTER_MIN)
    me, mypath = _my_registration(args)
    holders = []
    for sid, fm, body, stale, p in _live(stale_after):
        if args.file in _claimed_files(body):
            holders.append((sid, fm, stale, p))
    rc = 0
    if not holders:
        print(f"free: {args.file}")
    for sid, fm, stale, p in holders:
        tag = "STALE" if stale else "LIVE"
        ours = (p == mypath) if mypath is not None else (sid == me)
        mine = " (this session)" if ours else ""
        print(f"{tag}  {args.file}  held by {sid}{mine}  last_execution={fm.get('last_execution','?')}")
        if not stale and not ours:
            rc = 1
    return rc


def cmd_list(args):
    reason = _lock_probe()
    if reason:
        _degraded(reason)
    rows = _live(getattr(args, "stale_after", STALE_AFTER_MIN))
    if not rows:
        print("no sessions registered")
        return 0
    me, mypath = _my_registration(args)
    for sid, fm, body, stale, p in rows:
        age = _age_min(fm)
        age_s = f"{age:.0f}m ago" if age is not None else "unknown"
        tag = "STALE" if stale else "LIVE "
        ours = (p == mypath) if mypath is not None else (sid == me)
        mine = "  <- this session" if ours else ""
        print(f"{tag} {sid}  ({age_s}){mine}  [{p.name}]")
        print(f"       task: {fm.get('task','(unstated)')}")
        for f in _claimed_files(body):
            print(f"       open: {f}")
    return 0


def cmd_release(args):
    sid = session_id(getattr(args, "id", None))
    if not sid:
        print("cannot resolve session id -- pass --id", file=sys.stderr)
        return 2
    p, err = _select(sid, getattr(args, "entry", None))
    if err:
        # H8: releasing the WRONG one of two registrations deletes a live
        # process's claims and tells the next session the files are free.
        print(err + "\n  Refusing to delete a registration that may not be ours.",
              file=sys.stderr)
        return 2
    if p is None or not p.exists():
        print("nothing to release")
        return 0
    fm, body = _parse(p)
    held = _claimed_files(body)
    # A release must never clear claims it does not own. Our own pid, a dead pid
    # or an unjudgeable one may be released; another process that is demonstrably
    # RUNNING may not, unless the caller insists.
    if fm.get("pid", "") != _my_pid() and _pid_alive(fm) is True and not getattr(args, "force", False):
        print(f"refusing to release {p.name}: it belongs to pid {fm.get('pid','?')} "
              f"on {fm.get('host','?')}, which is still running, and it holds "
              f"{len(held)} claim(s){': ' + ' '.join(held) if held else ''}.\n"
              f"  Releasing it would tell every other session those files are free.\n"
              f"  Use --force if you know that process has stopped writing.",
              file=sys.stderr)
        return 1
    if dry("delete %s (%d claim(s): %s)" % (p, len(held), " ".join(held) or "none")):
        return 0
    p.unlink()
    print(f"released {sid} -- writing finished, {len(held)} claim(s) cleared"
          f"{': ' + ' '.join(held) if held else ''}")
    return 0


def main():
    # Shared flags declared once and attached to the top level AND every subcommand,
    # so they parse on either side of the verb. default=SUPPRESS keeps a subparser
    # from resetting a value given before the verb.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", default=argparse.SUPPRESS,
                        help="the vault to act on (default: $GT_VAULT, else this "
                             "tool's own vault)")
    common.add_argument("--dry-run", "-n", action="store_true", default=argparse.SUPPRESS,
                        help="say what would change; write nothing")
    # --id and --stale-after live here too, so `--id X claim f` and `claim f --id X`
    # both parse. They used to be top-level only, which made the natural form
    # (`--id X claim f`) fail with "unrecognized arguments" -- the same defect
    # gt_log.py and gt_adr.py fixed by putting the shared flags in `parents`.
    common.add_argument("--id", default=argparse.SUPPRESS,
                        help="session id (default: $CLAUDE_SESSION_ID, then $TMPDIR)")
    common.add_argument("--entry", default=argparse.SUPPRESS,
                        help="which registration to act on, by filename, when one "
                             "session id has more than one (see also $GT_SESSION_FILE)")
    common.add_argument("--stale-after", type=float, default=argparse.SUPPRESS,
                        help=f"minutes before a heartbeat is stale (default {STALE_AFTER_MIN})")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("register", help="announce this session", parents=[common])
    r.add_argument("--task", help="one line: what this session is doing")
    r.add_argument("--agent", default="Claude Code")
    r.add_argument("--files", nargs="*", default=[])
    r.add_argument("--resume", action="store_true",
                   help="take over an existing registration for this session id")
    r.add_argument("--new", action="store_true",
                   help="register a second entry for this id on purpose")
    r.set_defaults(fn=cmd_register)

    b = sub.add_parser("beat", help="refresh last_execution", parents=[common])
    b.set_defaults(fn=cmd_beat)

    c = sub.add_parser("claim", help="add files to this session's claim", parents=[common])
    c.add_argument("files", nargs="+")
    c.add_argument("--force", action="store_true", help="claim even if another live session holds it")
    c.set_defaults(fn=cmd_claim)

    k = sub.add_parser("check", help="who holds this file?", parents=[common])
    k.add_argument("file")
    k.set_defaults(fn=cmd_check)

    l = sub.add_parser("list", help="every live session", parents=[common])
    l.set_defaults(fn=cmd_list)

    x = sub.add_parser("release", help="delete this session's file -- done writing", parents=[common])
    x.add_argument("--force", action="store_true",
                   help="release even a registration held by another running process")
    x.set_defaults(fn=cmd_release)

    args = ap.parse_args()
    global DRY_RUN
    DRY_RUN = getattr(args, "dry_run", False)
    # SUPPRESS leaves these absent unless given on one side or the other; fill the
    # defaults in once, here, so every command sees the same attributes.
    args.id = getattr(args, "id", None)
    args.entry = getattr(args, "entry", None)
    args.stale_after = getattr(args, "stale_after", STALE_AFTER_MIN)
    if getattr(args, "vault", None):
        use_vault(args.vault)
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
