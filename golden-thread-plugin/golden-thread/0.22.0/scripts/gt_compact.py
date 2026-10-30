#!/usr/bin/env python3
"""gt_compact.py -- ask the open session to compact itself once this turn ends (0.21.4).

    gt_compact.py request [--handoff FILE] [--kind compact|clear] [--session ID] [--cwd DIR] [--json]
    gt_compact.py status [--session ID] [--cwd DIR] [--json]

The command half of /gt:gt-minimize compact. A shell script cannot compact a running Claude Code;
the in-session mod (hooks/guard_mod.js) can. So the skill has the model capture this session's
findings and write the handoff first, then runs `request`, which drops a small request file the
mod reads when the turn ends. The mod cuts in place and puts
"Read the handoff at <FILE> and pick up where we left off." in the prompt box: one step for
the user.

ONE SESSION, NOT ONE FOLDER. The owner runs several sessions at once, often in the same folder, so
the request is named and matched by SESSION ID: request-<session>.json, with the id inside it too.
Only the mod of that session takes it (Claude Code 2.1.296+ tells a mod its id; a mod that cannot
learn it takes no request, so below 2.1.296 this does nothing and the skill says to type /compact). The id comes from --session, else $CLAUDE_CODE_SESSION_ID (set in
every Claude Code shell), $CLAUDE_SESSION_ID or $GT_SESSION_ID; with none, `request` refuses.

  request   refuses (exit 1) unless --handoff names the handoff: capture before cut, so a
            compaction can never be asked for ahead of its handoff (cut_handoff none, a plain cut,
            needs none and records none). Accepted: an existing,
            non-empty file; or, under gt sandbox mode, a handoff still waiting in the queue inbox
            (~/.gt-inbox/queue/) -- either the inbox file itself or the vault path an inbox request
            will create. Otherwise writes ~/.claude/golden-thread/expiry-guard/mod/request-<id>.json,
            or, when that folder cannot be written (sandbox mode denies it),
            ~/.gt-scratch/compact/request-<id>.json, which the mod also reads. One line out.
            The request records its kind (cut_kind, or --kind) and cut_handoff, so the mod compacts in
            place or clears and starts the fresh session from the handoff. A clear with cut_handoff
            none is refused: it would discard the session and carry nothing over.
  status    prints the auto_compact setting (warn, off, auto, a percent or a token count), what a cut
            is with cut_kind and cut_handoff, and whether a request is waiting for this session.

  --handoff FILE  the handoff the skill just wrote (an absolute path; the pickup line names it)
  --kind K        compact or clear for this request (default: the cut_kind setting)
  --session ID    this session's id (default: the environment, above)
  --cwd DIR       the session's folder (default: the current directory); recorded, not matched
  --json          one JSON object instead of a line

auto_compact (gt_settings.py: auto, a percent 30-90 or a token count) is the same cut done without
asking, when the context passes that size; this script is not involved in that path. Its default,
`warn`, never cuts: gt_state.py says once that the session passed the `auto` line (notice_tokens
below) and names this command. `off` says nothing and cuts nothing.

cut_handoff (none | handoff | facts, default facts) and cut_kind (compact | clear, default compact) are
gt_settings.py settings (0.22.0). This script only reads them: the capture itself (findings for facts,
the handoff for handoff and facts) is the skill's, before `request`.

Exit: 0 done | 1 refused (no handoff, no session id, a clear with cut_handoff none) | 2 usage. Never
touches the vault.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID")


def mod_dir() -> Path:
    return Path.home() / ".claude" / "golden-thread" / "expiry-guard" / "mod"


def scratch_dir() -> Path:
    """Where the request goes when mod_dir() cannot be written (gt sandbox mode denies writes under
    ~/.claude/golden-thread; ~/.gt-scratch is the sandbox's writable place, as gt_spill uses it).
    guard_mod.js reads both."""
    return Path.home() / ".gt-scratch" / "compact"


def safe(sid: str) -> str:
    """The mod's safe(id), so both halves name the same file."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", sid or "")[:80]


def session_id(given: str = "") -> str:
    if given:
        return given
    for var in SESSION_ENV:
        v = os.environ.get(var, "").strip()
        if v:
            return v
    return ""


def request_paths(sid: str) -> list:
    name = "request-%s.json" % safe(sid)
    return [mod_dir() / name, scratch_dir() / name]


def state_dir() -> Path:
    return Path.home() / ".claude" / "golden-thread" / "state"


def note_ask(sid: str) -> None:
    """0.22.0: leave a note that gt asked this session to compact, so gt_minimize --record counts the cut
    as gt's and not a plain /compact. Best effort: the request is already written, and a note that cannot
    be written only means the cut is not recorded."""
    try:
        _write(state_dir() / ("compact-asked-%s.json" % safe(sid)),
               json.dumps({"v": 1, "session": sid, "at": time.time()}) + "\n")
    except OSError:
        pass


# auto_compact (0.22.0): warn (the default: a notice, never a cut), off, a percent 30-90, a token count
# 100000-400000 (150k or 150000), or auto. guard_mod.js carries the same rules; tests/test_auto_compact_tokens.py
# runs one table through both.
AUTO_TOKENS = 150000                # what `auto` means, held to HELD of the window when that is known
TOKENS_MIN, TOKENS_MAX = 100000, 400000
HELD = 0.75
PERCENT_WINDOW = 200000             # a percent setting with no window reported, as before 0.22.0
# model id fragment -> context window. Only windows that were measured; every other model is unknown
# (Opus 4.8 and Haiku 4.5 are NOT in the table: their windows are unverified).
WINDOWS = (("sonnet-5-5", 1_000_000), ("opus-5-5", 1_000_000), ("sonnet-4-6", 200_000))


def window_for(model):
    """-> the context window of a known model, else None (unknown)."""
    m = re.sub(r"[ ._]+", "-", str(model or "").strip().lower())
    for frag, win in WINDOWS:
        if frag in m:
            return win
    return None


def parse_setting(value):
    """-> 'warn', 'off', 'auto', a percent as '30'..'90', or a token count as '100000'..'400000'; None
    when the value is none of those. Case and surrounding space do not matter; 150k is 150000."""
    v = str(value).strip().lower()
    if v in ("warn", "off", "auto"):
        return v
    m = re.fullmatch(r"(\d+)(k?)", v)
    if not m:
        return None
    n = int(m.group(1)) * (1000 if m.group(2) else 1)
    if not m.group(2) and 30 <= n <= 90:
        return str(n)
    return str(n) if TOKENS_MIN <= n <= TOKENS_MAX else None


def threshold_tokens(value, window=None):
    """-> the context size at which an automatic cut is due, or None for warn / off / unusable (warn
    never cuts). A percent is a percent of the window (200,000 when it is not known); a token count or
    `auto` is held to 75% of the window when the window is known, and used as given when it is not."""
    v = parse_setting(value)
    if v is None or v in ("off", "warn"):
        return None
    if v == "auto" or int(v) >= TOKENS_MIN:
        t = AUTO_TOKENS if v == "auto" else int(v)
        return min(t, int(window * HELD)) if window else t
    return int((window or PERCENT_WINDOW) * int(v) / 100)


def notice_tokens(value, window=None):
    """-> the context size at which `warn` says once that the session is large, or None when the
    setting is not warn. The same line `auto` would cut at (150,000, held to 75% of a known window),
    so turning auto on later changes what happens there, not where."""
    return threshold_tokens("auto", window) if parse_setting(value) == "warn" else None


def setting() -> str:
    """The normalised auto_compact setting: a missing file or key is `warn` (0.22.0: opt-in cutting;
    the default only says so); an explicit value is kept; junk is `off` (a typo never turns cutting on)."""
    try:
        cfg = json.loads((Path.home() / ".claude" / "vault-config.json").read_text(encoding="utf-8"))
        if "auto_compact" not in cfg:
            return "warn"
        return parse_setting(cfg.get("auto_compact")) or "off"
    except (OSError, ValueError, AttributeError, TypeError):
        return "warn"


CUT_HANDOFFS = ("none", "handoff", "facts")
CUT_KINDS = ("compact", "clear")


def _config() -> dict:
    try:
        cfg = json.loads((Path.home() / ".claude" / "vault-config.json").read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def cut_settings(cfg=None):
    """-> (cut_kind, cut_handoff), normalised. Missing or junk is the safe default: compact (a typo
    never clears) and facts (the cut as it was before 0.22.0). guard_mod.js reads them the same way."""
    cfg = _config() if cfg is None else cfg
    kind = str(cfg.get("cut_kind") or "").strip().lower()
    mode = str(cfg.get("cut_handoff") or "").strip().lower()
    return (kind if kind in CUT_KINDS else "compact", mode if mode in CUT_HANDOFFS else "facts")


def cut_words(kind, mode):
    """What a cut with these settings is, in the notices' words (guard_mod.js cutWords, gt_settings.cut_words)."""
    if kind == "clear" and mode == "none":
        return "refused (cut_kind clear with cut_handoff none would carry nothing over)"
    if mode == "none":
        return "a plain compact"
    return ("a clear" if kind == "clear" else "a compact") + (" with a handoff" if mode == "handoff"
                                                               else " with a handoff and facts")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(str(tmp), "wb") as f:                         # bytes: LF on every platform
        f.write(text.encode("utf-8"))
    os.replace(str(tmp), str(path))


def _nonempty_file(p: Path) -> bool:
    """is_file() raises PermissionError (not False) on a path the sandbox denies: that is "not
    visible here", never a crash."""
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def inbox_dir() -> Path:
    return Path.home() / ".gt-inbox" / "queue"


def _in_inbox(p: Path) -> bool:
    try:
        root = os.path.realpath(str(inbox_dir()))
        return os.path.commonpath([os.path.realpath(str(p)), root]) == root
    except (ValueError, OSError):
        return False


def queued_in_inbox(h: Path) -> bool:
    """True when a queue-inbox request (gt_write_queue.deposit_inbox) will create `h` with content:
    {"gt_inbox": 1, "vault": <vault>, "request": {"path": <rel>, "content": ...}}."""
    try:
        names = sorted(os.listdir(str(inbox_dir())))
    except OSError:
        return False
    want = os.path.realpath(str(h))
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            d = json.loads((inbox_dir() / n).read_text(encoding="utf-8"))
            req = d.get("request") or {}
            if not (d.get("vault") and req.get("path") and str(req.get("content") or "").strip()):
                continue
            if os.path.realpath(os.path.join(str(d["vault"]), str(req["path"]))) == want:
                return True
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return False


def handoff_state(h: Path | None) -> str:
    """-> "file" | "inbox" | "" (refused)."""
    if h is None or not h.is_absolute():
        return ""
    if _nonempty_file(h):
        return "inbox" if _in_inbox(h) else "file"
    return "inbox" if queued_in_inbox(h) else ""


def _out(a, ok: bool, msg: str, **extra) -> None:
    if a.json:
        print(json.dumps(dict({"ok": ok, ("why" if not ok else "msg"): msg}, **extra)))
    else:
        print(msg, file=sys.stdout if ok else sys.stderr)


def do_request(a) -> int:
    cwd = a.cwd or os.getcwd()
    kind, mode = cut_settings()
    kind = a.kind or kind
    if kind == "clear" and mode == "none":
        _out(a, False, "refused: a clear with cut_handoff none would end this session and carry nothing over; "
             "set cut_handoff to handoff or facts, or ask for a compact")
        return 1
    h = Path(a.handoff) if a.handoff and mode != "none" else None
    where = handoff_state(h) if mode != "none" else "none"
    if not where:
        _out(a, False, "refused: no handoff written yet (%s); write it first, then ask for the %s"
             % (a.handoff or "none given", kind))
        return 1
    sid = session_id(a.session)
    if not sid:
        _out(a, False, "refused: this session's id is not known here (no --session, no "
             "CLAUDE_CODE_SESSION_ID), so no request can be matched to it; type /%s instead%s"
             % (kind, " (the handoff is at %s)" % h if h else ""))
        return 1
    rec = {"v": 3, "session": sid, "cwd": cwd, "handoff": str(h).replace("\\", "/") if h else "",
           "kind": kind, "handoff_mode": mode, "at": time.time()}
    written, errors = None, []
    for path in request_paths(sid):
        try:
            _write(path, json.dumps(rec) + "\n")
            written = path
            break
        except OSError as exc:
            errors.append("%s: %s" % (path.parent, exc.__class__.__name__))
    if written is None:
        _out(a, False, "refused: could not write the request (%s); type /%s instead%s"
             % ("; ".join(errors), kind, " (the handoff is at %s)" % rec["handoff"] if rec["handoff"] else ""))
        return 1
    note_ask(sid)
    note = " (queued in the sandbox inbox; it lands in the vault at the next drain)" if where == "inbox" else ""
    if mode == "none":
        msg = ("compact requested (a plain compact, cut_handoff none): it runs when this reply ends "
               "(if nothing happens, type /compact)")
    elif kind == "clear":
        msg = ("clear requested: when this reply ends the session is cleared and the handoff is offered in the "
               "fresh session's prompt box (if nothing happens, type /clear, then: Read the handoff at %s and "
               "pick up where we left off.%s)" % (rec["handoff"], note))
    else:
        msg = ("compact requested: it runs when this reply ends, then the handoff is offered in the prompt box "
               "(if nothing happens, type /compact; the handoff is at %s%s)" % (rec["handoff"], note))
    if a.json:
        print(json.dumps({"ok": True, "handoff": rec["handoff"], "request": str(written), "handoff_in": where,
                          "kind": kind, "handoff_mode": mode}))
    else:
        print(msg)
    return 0


def do_status(a) -> int:
    sid = session_id(a.session)
    waiting = bool(sid) and any(_nonempty_file(p) for p in request_paths(sid))
    kind, mode = cut_settings()
    out = {"auto_compact": setting(), "cut_kind": kind, "cut_handoff": mode, "request_waiting": waiting,
           "session": sid or None}
    print(json.dumps(out) if a.json else "auto_compact: %s; cut: %s; request waiting: %s"
          % (out["auto_compact"], cut_words(kind, mode), "yes" if waiting else "no"))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="gt_compact.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd")
    for name, fn in (("request", do_request), ("status", do_status)):
        c = sub.add_parser(name)
        c.add_argument("--cwd", default="")
        c.add_argument("--session", default="")
        c.add_argument("--json", action="store_true")
        if name == "request":
            c.add_argument("--handoff", default="")
            c.add_argument("--kind", default="", choices=("",) + CUT_KINDS)
        c.set_defaults(fn=fn)
    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        p.print_usage(sys.stderr)
        return 2
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
