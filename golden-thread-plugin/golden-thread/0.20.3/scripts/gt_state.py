#!/usr/bin/env python3
"""gt_state.py -- write the session's state BEFORE the context runs out, not as it does.

    gt_state.py check   [--margin N] [--write] [--json] [--session ID]  # crossed? write if so
    gt_state.py write   [--reason R] [--repo P] [--session ID]          # write the state file now
    gt_state.py show                                                    # the newest state file
    gt_state.py hook    [--repo P] [--session ID]                       # PreCompact: the backstop

(This block read `write [--reason R] [--force]` until 2026-09-28. `--force` never existed and
`check --write` -- the flag the hook actually passes -- was never shown. A docstring is not
executed, so nothing caught it; `dev/check_docstring_flags.py` now does.)

WHY THIS EXISTS. gt already had three pieces and joined none of them: `gt_usage` reads the
context percentage but only DISPLAYS it; `gt_report_card` runs at SessionEnd and PreCompact but
writes a HEALTH SUMMARY -- uncommitted files, stale rollup -- and nothing about what the session
was doing; `gt_handoff` gathers exactly the right content and is manual, so nothing ever calls
it. On 2026-09-28 the owner warned that compaction was near and the assistant wrote a handoff by
hand. This is that, without needing a person to think of it.

THE SIGNAL IS `ctx_pct`, AND THE DISTINCTION IS THE WHOLE DESIGN. `readings.jsonl` carries both
context fill and rate-limit usage, and they are unrelated. A real reading from the session that
prompted this:

    {"five_hour": 5, "seven_day": 10, "ctx_pct": 91, ...}

5% of the five-hour allowance, 91% of the context. Triggering on the allowance meter would have
fired at entirely the wrong moment AND LOOKED CORRECT DOING IT, because both are percentages
with plausible values. So this reads `ctx_pct` and nothing else; a test asserts it never reads
`rate_limits`.

PRECOMPACT IS THE BACKSTOP, NOT THE PRIMARY. It fires when compaction is already starting, so
the write competes with the thing it exists to survive. The threshold fires while there is still
room to write properly. Both run; `hook` says which one it is.

WHAT THE USER SEES (0.18.1). A hook's plain stdout reaches the MODEL only, never the terminal --
the failure fixed for the SessionStart checks in 0.9.6. So when this runs as a hook (Claude Code
pipes it a JSON payload naming `hook_event_name`), it speaks to the user through `systemMessage`:

    UserPromptSubmit, state written       one systemMessage line: the file and the percentage
    UserPromptSubmit, write failed        one systemMessage line saying it failed, and why
    UserPromptSubmit, below threshold     NO systemMessage -- a line every turn trains the user
                                          to ignore it; the model still gets its one line
    PreCompact                            ALWAYS a systemMessage: wrote (where), or did not
                                          and why

Run by hand (no payload) it prints plain text, as before.

ONE SESSION'S FIGURE, NEVER ANOTHER'S (0.18.1). The usage ledger is SHARED by every session on
the machine, and until 0.18.1 `newest_reading()` took the newest `ctx_pct` in it whoever wrote
it -- so a fresh session's first prompt could report `context 88% -- already written`, the
previous session's figure (observed 2026-09-29). The hook payload's `session_id` (or
`--session`) now filters the ledger to this session's rows (the ledger keeps the id's first 8
characters); a session with no reading of its own yet is "cannot tell", never someone else's
number. With no session known at all -- a manual run -- the newest reading is used and labelled
with the session it came from.

NEVER FATAL, NEVER BLOCKING. Every failure path exits 0 with a note. A state write that broke a
turn would be worse than the gap it closes.

Exit: 0 wrote / not yet due / could not tell | 2 usage.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
USAGE_LEDGER = HOME / ".claude" / "golden-thread" / "usage" / "readings.jsonl"
STATE_DIR = HOME / ".claude" / "golden-thread" / "state"
KEEP = 5

# The context flag point per `usage_alert` mode, mirroring gt_usage.ALERTS' 4th element.
# DUPLICATED DELIBERATELY: gt_usage lives in the usage MODULE, a separate version tree, so gt
# cannot import it -- the module may not be installed at all. A stale copy here would fire at
# the wrong number, so the test pins these against the module's table when it is present, the
# same way gt_paths' report_dir is pinned against gt_lint_weekly's copy.
CONTEXT_FLAG = {"early": 70.0, "normal": 85.0, "late": 95.0, "always": -1.0}
DEFAULT_MODE = "normal"
DEFAULT_MARGIN = 5.0


def alert_mode() -> str:
    """The owner's `usage_alert` setting, or the default. Never raises."""
    try:
        sys.path.insert(0, str(HERE))
        import gt_settings                                  # noqa: PLC0415
        return gt_settings.get("usage_alert") or DEFAULT_MODE
    except Exception:
        return DEFAULT_MODE


def _short(session) -> str:
    """The ledger records the first 8 characters of a session id (gt_usage)."""
    return str(session or "")[:8]


def newest_reading(session=None):
    """-> (ctx_pct, session, why_not).

    A missing, unreadable or empty ledger is "CANNOT TELL", never "plenty of room". The usage
    module may not be installed; silence from a source that was never there must not read as a
    measurement saying everything is fine.

    With `session`, only that session's rows count. The ledger is shared by every session on the
    machine, and the newest row overall is often another session's -- which is how a fresh
    session once reported a predecessor's 88%.
    """
    if not USAGE_LEDGER.is_file():
        return None, None, "no usage ledger at %s (is the usage module installed?)" % USAGE_LEDGER
    try:
        # The ledger grows without bound; only the tail matters.
        with USAGE_LEDGER.open("rb") as fh:
            try:
                fh.seek(-65536, os.SEEK_END)
            except OSError:
                fh.seek(0)
            tail = fh.read().decode("utf-8", "replace")
    except OSError as exc:
        return None, None, "could not read the usage ledger (%s)" % exc.__class__.__name__
    want = _short(session)
    newest = None
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not (isinstance(d, dict) and d.get("ctx_pct") is not None):
            continue
        if want and _short(d.get("session")) != want:
            continue
        newest = d
    if not newest:
        if want:
            return None, want, "the ledger carries no context reading for this session yet"
        return None, None, "the ledger carries no context reading yet"
    try:
        return float(newest["ctx_pct"]), newest.get("session") or want or None, None
    except (TypeError, ValueError):
        return None, None, "the newest context reading is not a number"


def _stdin_ready(timeout):
    """True when stdin has data (or EOF) within `timeout` seconds -- select() on POSIX.

    Native Windows (0.20.1): select() takes sockets only, so on a pipe it raised, the caller
    caught it, and every hook payload read as {} -- the hook never knew its event or session.
    There PeekNamedPipe answers the same question; a stdin that is not a pipe (a file, NUL)
    makes it fail, and read() will not block on those."""
    if os.name != "nt":
        import select                                             # noqa: PLC0415
        return bool(select.select([sys.stdin], [], [], timeout)[0])
    import ctypes                                                 # noqa: PLC0415
    import time                                                   # noqa: PLC0415
    import msvcrt                                                 # noqa: PLC0415
    from ctypes import wintypes                                   # noqa: PLC0415
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.PeekNamedPipe.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                  ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    avail, end = wintypes.DWORD(), time.monotonic() + timeout
    while True:
        if not k32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None):
            return True                     # not a pipe, or the writer closed it: EOF
        if avail.value or time.monotonic() >= end:
            return bool(avail.value)
        time.sleep(0.01)


def hook_payload() -> dict:
    """The hook's JSON payload on stdin, read without blocking; {} when run by hand."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        if not _stdin_ready(0.3):
            return {}
        d = json.loads(sys.stdin.read() or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def emit(user, model, event):
    """Hook output: `user` (or None) goes to the terminal as systemMessage, `model` to context.

    PreCompact has no additionalContext field, so there only systemMessage is sent.
    """
    out = {}
    if user:
        out["systemMessage"] = user
    if model and event == "UserPromptSubmit":
        out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": model}
    if out:
        json.dump(out, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")


def threshold(margin: float) -> float:
    flag = CONTEXT_FLAG.get(alert_mode(), CONTEXT_FLAG[DEFAULT_MODE])
    if flag < 0:                       # "always" has no flag point to sit under
        flag = CONTEXT_FLAG[DEFAULT_MODE]
    return max(flag - margin, 1.0)


def marker_path(session) -> Path:
    return STATE_DIR / ("%s.crossed" % (session or "unknown"))


def _first_cannot_tell(session) -> bool:
    """True the first time this session could not read its context; records that it was said.
    A marker that cannot be written means "say it" -- never silence by accident."""
    p = STATE_DIR / ("%s.cannot-tell" % (_short(session) or "unknown"))
    if p.is_file():
        return False
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
    except OSError:
        pass
    return True


def already_written(session) -> bool:
    return marker_path(session).is_file()


def gather(repo: Path | None):
    """What a fresh session cannot re-derive. Facts only."""
    def sh(args, cwd=None):
        try:
            p = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True,
                               text=True, timeout=30)
            return p.stdout.strip() if p.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    out = {"when": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    if repo and (repo / ".git").exists() or repo:
        out["repo"] = str(repo)
        out["branch"] = sh(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"])
        out["head"] = sh(["git", "-C", repo, "log", "-1", "--pretty=%h %s"])
        # -uall so untracked FILES are listed rather than collapsed to their directory:
        # "sub/" tells the next session less than "sub/dirty.py".
        dirty = sh(["git", "-C", repo, "status", "--porcelain", "-uall"])
        out["uncommitted"] = [l for l in dirty.splitlines() if l.strip()]
    return out


def render(state, ctx, why, reason) -> str:
    L = ["# Session state — %s" % state["when"], ""]
    if ctx is not None:
        L.append("Written because **context reached %.0f%%** (%s)." % (ctx, reason))
    else:
        L.append("Written as the **%s**; context could not be read (%s)." % (reason, why))
    L.append("")
    if state.get("repo"):
        L += ["## Where the work is", "",
              "- repo: `%s`" % state["repo"],
              "- branch: `%s`" % (state.get("branch") or "?"),
              "- HEAD: `%s`" % (state.get("head") or "?")]
        un = state.get("uncommitted") or []
        L.append("- uncommitted: **%d file(s)**" % len(un))
        for row in un[:40]:
            L.append("    - `%s`" % row)
        if len(un) > 40:
            L.append("    - … and %d more" % (len(un) - 40))
        L.append("")
    L += ["## What a fresh session must not assume", "",
          "- This file records FACTS at a moment, not decisions taken since.",
          "- Uncommitted work above may be mid-edit; read it before building on it.",
          "- The vault is the durable record — `Projects/<slug>/research.md` and",
          "  `decisions.md` outrank anything here.", ""]
    return "\n".join(L) + "\n"


def prune():
    try:
        files = sorted(STATE_DIR.glob("state-*.md"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        for old in files[KEEP:]:
            old.unlink()
    except OSError:
        pass


def write_state(a, ctx, why):
    """-> (path, None) on a write, (None, reason) on a failure. Never raises."""
    repo = Path(a.repo).resolve() if a.repo else None
    if repo is None:
        try:
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True, timeout=30)
            repo = Path(top.stdout.strip()) if top.returncode == 0 and top.stdout.strip() else None
        except (OSError, subprocess.SubprocessError):
            repo = None
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state = gather(repo)
        stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H%M%S")
        out = STATE_DIR / ("state-%s.md" % stamp)
        out.write_bytes(render(state, ctx, why, a.reason or "threshold").encode("utf-8"))
        if a.marker_session:
            marker_path(a.marker_session).write_bytes(
                ("%s\n" % (ctx if ctx is not None else "?")).encode("utf-8"))
        prune()
        return out, None
    except OSError as exc:
        # Never fatal: a state write that breaks a turn is worse than the gap it closes.
        return None, "%s on %s" % (exc.__class__.__name__, STATE_DIR)


def _session_from(a, payload):
    return getattr(a, "session", None) or payload.get("session_id") or None


def do_write(a, payload=None) -> int:
    payload = hook_payload() if payload is None else payload
    ctx, session, why = newest_reading(_session_from(a, payload))
    a.marker_session = session if ctx is not None else None
    out, err = write_state(a, ctx, why)
    if out:
        print("gt-state: wrote %s" % out)
    else:
        print("gt-state: could not write state (%s)" % err, file=sys.stderr)
    return 0


def do_check(a) -> int:
    payload = hook_payload()
    event = payload.get("hook_event_name") or ""
    as_hook = bool(event)
    ctx, session, why = newest_reading(_session_from(a, payload))
    limit = threshold(a.margin)
    if ctx is None:
        # "Cannot tell" is said out loud and is NOT a pass. It exits 0 because this must never
        # block a turn, but it never claims there is room. Not a systemMessage: with no usage
        # module it would be every turn, which is how a user learns to ignore the channel.
        # 0.20.1: as a HOOK it is said at most once per session -- stderr every turn was the
        # same noise by another route ("no usage ledger" on every prompt, Linux) -- and not at
        # all when there is simply no ledger: the usage module being off is a choice, not a
        # fault. Run by hand it is always said.
        if not as_hook or (USAGE_LEDGER.is_file() and _first_cannot_tell(session or
                                                                          payload.get("session_id"))):
            print("gt-state: cannot tell — %s" % why, file=sys.stderr)
        if a.json:
            print(json.dumps({"ctx_pct": None, "threshold": limit, "due": None, "why": why,
                              "session": session}))
        return 0
    due = ctx >= limit and not already_written(session)
    line = ("gt-state: context %.0f%%, threshold %.0f%% (%s) — %s"
            % (ctx, limit, alert_mode(),
               "DUE" if due else ("already written" if ctx >= limit else "not yet")))
    if a.json:
        print(json.dumps({"ctx_pct": ctx, "threshold": limit, "due": due,
                          "session": session, "mode": alert_mode()}))
    elif not (as_hook and due and a.write):
        print(line)
    if due and a.write:
        a.reason = "context %.0f%% crossed the %.0f%% threshold" % (ctx, limit)
        a.repo = getattr(a, "repo", None)
        a.marker_session = session
        out, err = write_state(a, ctx, why)
        if out:
            msg = "gt-state: context %.0f%% — session state written to %s" % (ctx, out)
        else:
            msg = ("gt-state: context %.0f%% — session state could NOT be written (%s)"
                   % (ctx, err))
        if as_hook and not a.json:
            emit(msg, line + "\n" + msg, event)
        else:
            print(msg, file=sys.stdout if out else sys.stderr)
    return 0


def do_hook(a) -> int:
    """PreCompact / SessionEnd: the BACKSTOP, and it says so.

    Runs regardless of the threshold, because by here the context is being compacted anyway.
    It identifies itself so nobody reads a backstop write as the early one having worked. As a
    hook it ALWAYS tells the user what happened -- wrote, or did not and why.
    """
    payload = hook_payload()
    event = payload.get("hook_event_name") or ""
    a.reason = "PreCompact/SessionEnd backstop — the threshold write is the primary"
    a.repo = getattr(a, "repo", None)
    ctx, session, why = newest_reading(_session_from(a, payload))
    a.marker_session = session if ctx is not None else None
    out, err = write_state(a, ctx, why)
    pct = ("context %.0f%%" % ctx) if ctx is not None else ("context unknown: %s" % why)
    if out:
        msg = "gt-state: before compaction (%s) — session state written to %s" % (pct, out)
    else:
        msg = "gt-state: before compaction (%s) — session state NOT written (%s)" % (pct, err)
    if event:
        emit(msg, msg, event)
    else:
        print(msg, file=sys.stdout if out else sys.stderr)
    return 0


def do_show(_a) -> int:
    try:
        files = sorted(STATE_DIR.glob("state-*.md"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
    except OSError:
        files = []
    if not files:
        print("gt-state: no state has been written")
        return 0
    print(files[0].read_text(encoding="utf-8"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="write session state before the context runs out")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check"); c.set_defaults(fn=do_check)
    c.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    c.add_argument("--write", action="store_true", help="write if the threshold is crossed")
    c.add_argument("--repo"); c.add_argument("--json", action="store_true")
    c.add_argument("--session", help="this session's id (a hook payload's session_id wins "
                                     "when absent); filters the shared usage ledger")

    w = sub.add_parser("write"); w.set_defaults(fn=do_write)
    w.add_argument("--reason"); w.add_argument("--repo"); w.add_argument("--session")

    h = sub.add_parser("hook"); h.set_defaults(fn=do_hook)
    h.add_argument("--repo"); h.add_argument("--session")

    s = sub.add_parser("show"); s.set_defaults(fn=do_show)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
