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
session from an abandoned one, and a `files_claimed` list saying what is open.
A session that finishes its writes REMOVES its file -- absence means done.

    register   announce this session and what it intends to touch
    beat       refresh last_execution (call on every execution)
    claim      add files to this session's claim, refreshing the heartbeat
    check      who holds a given file? exit 1 if someone else does
    list       every live session, with stale ones flagged
    release    delete this session's file -- writing is finished

## Staleness, not locking

This is advisory. A session that crashes leaves its file behind, so a claim is
only trusted while its heartbeat is fresh (default 30 min, --stale-after).
Beyond that it is reported STALE and may be ignored. Deliberately not a hard
lock: a stuck lock in a single working tree is worse than a stale hint.

Advisory about WHOSE files they are, though -- not about whether a write lands.
Every change to a session file goes through a compare-and-swap (see `_update`),
so two processes sharing one id cannot lose each other's claims, and a write that
cannot be made says so and exits non-zero.
"""

import argparse
import datetime
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


def _path(sid, create_stamp=None):
    """Session files are named  <session-id>_<YYYY-MM-DD>_<HHMM>.md

    The opened-at stamp is in the filename so `ls` alone shows who has been
    live and for how long -- an abandoned file from two days ago is obvious
    without opening it. Lookup globs on the id, since the stamp is not known
    to later commands.
    """
    # NEWEST first: one session id can have several files (--resume reuses the
    # id), and the live one is always the most recent.
    hits = sorted(SESSIONS.glob(f"{sid}_*.md"), reverse=True) if SESSIONS.exists() else []
    if hits:
        return hits[0]
    if create_stamp:
        return SESSIONS / f"{sid}_{create_stamp}.md"
    return SESSIONS / f"{sid}.md"  # legacy/unstamped fallback


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


def _hold(fh, tries=50, pause=0.002):
    """Take the OS lock on this open descriptor, briefly. -> True if held.

    LOCK_NB with a short spin rather than a blocking wait: the lock is held for
    two syscalls, so anyone who cannot get it inside 100ms is not merely busy, and
    the honest answer then is to fail the command, not to hang the session.
    Where flock does not exist the CAS runs unguarded -- correct, with the
    check-to-rename window back open.
    """
    if fcntl is None:
        return True
    for _ in range(tries):
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            time.sleep(pause)
    return False


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


def cmd_register(args):
    sid = session_id(args.id)
    if not sid:
        sys.exit("cannot resolve session id -- pass --id")
    if dry("register %s in %s" % (sid, SESSIONS)):
        return 0
    SESSIONS.mkdir(parents=True, exist_ok=True)
    now = _now()

    # Same session id already registered? That means either a --resume/--continue
    # of this conversation, or a crashed run of it. Never quietly share one id
    # between two processes -- the whole point is that a claim identifies ONE
    # writer. Decide it explicitly.
    existing = None
    mine = False
    for cand in sorted(SESSIONS.glob(f"{sid}_*.md"), reverse=True):
        existing = cand
        break
    if existing is not None:
        efm, _ = _parse(existing)
        alive = _pid_alive(efm)
        mine = str(os.environ.get("CLAUDE_PID") or os.getpid()) == efm.get("pid", "")
        if mine:
            pass                       # same process re-registering: just refresh
        elif alive is True and not (args.new or args.resume):
            print(f"session {sid} is ALREADY OPEN in another live process\n"
                  f"  file : {existing.relative_to(VAULT)}\n"
                  f"  pid  : {efm.get('pid','?')} on {efm.get('host','?')} (running)\n"
                  f"  task : {efm.get('task','(unstated)')}\n\n"
                  f"Two processes sharing one session id makes a claim ambiguous.\n"
                  f"  --resume   take over that registration (the other process should stop writing)\n"
                  f"  --new      register a second, independently-tracked entry for this id",
                  file=sys.stderr)
            return 1
        elif args.resume:
            print(f"resuming registration {existing.name} (pid {efm.get('pid','?')})")
            existing.unlink()
        elif alive is False:
            print(f"previous run of {sid} (pid {efm.get('pid','?')}) is dead -- replacing its registration")
            existing.unlink()

    stamp = now.strftime("%Y-%m-%d_%H%M")
    p = SESSIONS / f"{sid}_{stamp}.md"
    # A file already on this exact name, written by someone else, means another
    # process registered this id within the same minute (--new asks for that on
    # purpose; a race produces it by accident). The old rule -- overwrite unless
    # --new -- threw that registration and its claims away. Step aside instead:
    # the pid makes the name unique, and `list` then shows both entries, which is
    # the honest picture of two processes on one id. Re-registering our OWN file
    # still overwrites it: there is no second writer to lose.
    if p.exists() and not (mine and p == existing):
        p = SESSIONS / f"{sid}_{stamp}-{os.getpid()}.md"
    fm = {
        "session_id": sid,
        "agent": args.agent,
        "task": args.task or "(unstated)",
        "started": _stamp(),
        "last_execution": _stamp(),
        "status": "active",
        "pid": str(os.environ.get("CLAUDE_PID") or os.getpid()),
        "host": socket.gethostname(),
        "cwd": str(pathlib.Path.cwd()),
    }
    body = "# What this session has open\n\n"
    body += "".join(f"- `{f}`\n" for f in args.files) if args.files else "_nothing claimed yet_\n"
    # Atomic even though this file is new: `list`, `check` and the guard hook read
    # this directory from other processes at any moment, and a half-written
    # frontmatter parses as a session with no heartbeat and no claims.
    _atomic_write(p, _render(fm, body))
    print(f"registered {sid}\n  {p.relative_to(VAULT)}")
    return 0


def cmd_beat(args):
    sid = session_id(args.id)
    p = _path(sid) if sid else None
    if not p or not p.exists():
        sys.exit("no session file -- run `register` first")
    stamp = _stamp()

    def mutate(fm, body):
        fm["last_execution"] = stamp
        return fm, body                 # body untouched: a heartbeat never drops a claim

    if not _update(p, mutate):
        return _lost(p, f"the heartbeat for {sid}")
    print(f"heartbeat {sid} @ {stamp}")
    return 0


def cmd_claim(args):
    sid = session_id(args.id)
    p = _path(sid) if sid else None
    if not p or not p.exists():
        sys.exit("no session file -- run `register` first")

    # refuse to claim what a live session already holds
    conflicts = []
    for other, ofm, obody, stale in _live(args.stale_after):
        if other == sid or stale:
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
        out.append((fm.get("session_id", p.stem.split("_")[0]), fm, body, stale))
    return out


def cmd_check(args):
    holders = []
    for sid, fm, body, stale in _live(args.stale_after):
        if args.file in _claimed_files(body):
            holders.append((sid, fm, stale))
    me = session_id(args.id)
    rc = 0
    if not holders:
        print(f"free: {args.file}")
    for sid, fm, stale in holders:
        tag = "STALE" if stale else "LIVE"
        mine = " (this session)" if sid == me else ""
        print(f"{tag}  {args.file}  held by {sid}{mine}  last_execution={fm.get('last_execution','?')}")
        if not stale and sid != me:
            rc = 1
    return rc


def cmd_list(args):
    rows = _live(args.stale_after)
    if not rows:
        print("no sessions registered")
        return 0
    me = session_id(args.id)
    for sid, fm, body, stale in rows:
        age = _age_min(fm)
        age_s = f"{age:.0f}m ago" if age is not None else "unknown"
        tag = "STALE" if stale else "LIVE "
        mine = "  <- this session" if sid == me else ""
        print(f"{tag} {sid}  ({age_s}){mine}")
        print(f"       task: {fm.get('task','(unstated)')}")
        for f in _claimed_files(body):
            print(f"       open: {f}")
    return 0


def cmd_release(args):
    sid = session_id(args.id)
    p = _path(sid) if sid else None
    if not p or not p.exists():
        print("nothing to release")
        return 0
    if dry("delete %s" % p):
        return 0
    p.unlink()
    print(f"released {sid} -- writing finished, claims cleared")
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
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], parents=[common])
    ap.add_argument("--id", help="session id (default: $CLAUDE_SESSION_ID, then $TMPDIR)")
    ap.add_argument("--stale-after", type=float, default=STALE_AFTER_MIN,
                    help=f"minutes before a heartbeat is stale (default {STALE_AFTER_MIN})")
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
    x.set_defaults(fn=cmd_release)

    args = ap.parse_args()
    global DRY_RUN
    DRY_RUN = getattr(args, "dry_run", False)
    if getattr(args, "vault", None):
        use_vault(args.vault)
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
