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
"""

import argparse
import datetime
import os
import pathlib
import re
import socket
import sys

STALE_AFTER_MIN = 30
VAULT = pathlib.Path(__file__).resolve().parents[3]
SESSIONS = VAULT / "Projects" / "golden-thread" / "sessions"
TS_FMT = "%Y-%m-%d %H:%M:%S %Z"


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
    text = path.read_text()
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


def _age_min(fm):
    raw = fm.get("last_execution")
    if not raw:
        return None
    for fmt in (TS_FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M %Z", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.datetime.strptime(raw.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_now().tzinfo)
            return (_now() - dt).total_seconds() / 60.0
        except ValueError:
            continue
    return None


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
    SESSIONS.mkdir(parents=True, exist_ok=True)
    now = _now()

    # Same session id already registered? That means either a --resume/--continue
    # of this conversation, or a crashed run of it. Never quietly share one id
    # between two processes -- the whole point is that a claim identifies ONE
    # writer. Decide it explicitly.
    existing = None
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
    if p.exists() and args.new:        # second registration inside the same minute
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
    p.write_text(_render(fm, body))
    print(f"registered {sid}\n  {p.relative_to(VAULT)}")
    return 0


def cmd_beat(args):
    sid = session_id(args.id)
    p = _path(sid) if sid else None
    if not p or not p.exists():
        sys.exit("no session file -- run `register` first")
    fm, body = _parse(p)
    fm["last_execution"] = _stamp()
    p.write_text(_render(fm, body))
    print(f"heartbeat {sid} @ {fm['last_execution']}")
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

    fm, body = _parse(p)
    have = _claimed_files(body)
    new = [f for f in args.files if f not in have]
    if body.strip() == "_nothing claimed yet_":
        body = "# What this session has open\n\n"
    body = body.rstrip("\n") + "\n" + "".join(f"- `{f}`\n" for f in new)
    fm["last_execution"] = _stamp()
    p.write_text(_render(fm, body))
    print(f"claimed {len(new)} file(s) for {sid}")
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
    p.unlink()
    print(f"released {sid} -- writing finished, claims cleared")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--id", help="session id (default: $CLAUDE_SESSION_ID, then $TMPDIR)")
    ap.add_argument("--stale-after", type=float, default=STALE_AFTER_MIN,
                    help=f"minutes before a heartbeat is stale (default {STALE_AFTER_MIN})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("register", help="announce this session")
    r.add_argument("--task", help="one line: what this session is doing")
    r.add_argument("--agent", default="Claude Code")
    r.add_argument("--files", nargs="*", default=[])
    r.add_argument("--resume", action="store_true",
                   help="take over an existing registration for this session id")
    r.add_argument("--new", action="store_true",
                   help="register a second entry for this id on purpose")
    r.set_defaults(fn=cmd_register)

    b = sub.add_parser("beat", help="refresh last_execution")
    b.set_defaults(fn=cmd_beat)

    c = sub.add_parser("claim", help="add files to this session's claim")
    c.add_argument("files", nargs="+")
    c.add_argument("--force", action="store_true", help="claim even if another live session holds it")
    c.set_defaults(fn=cmd_claim)

    k = sub.add_parser("check", help="who holds this file?")
    k.add_argument("file")
    k.set_defaults(fn=cmd_check)

    l = sub.add_parser("list", help="every live session")
    l.set_defaults(fn=cmd_list)

    x = sub.add_parser("release", help="delete this session's file -- done writing")
    x.set_defaults(fn=cmd_release)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
