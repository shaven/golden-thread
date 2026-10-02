#!/usr/bin/env python3
"""gt_surface.py -- put what a previous session left behind in front of the next one.

    gt_surface.py check    [--vault V] [--dry-run] [--json] [--hook]   # SessionStart
    gt_surface.py must-do  [--vault V]                                 # the MUST DO block only
    gt_surface.py handoffs --project SLUG [--vault V]                  # /gt:gt-open, per project

WHY THIS EXISTS. By 0.17.1 gt could WRITE everything a next session needs and put none of it
in front of that session:

  | writer          | writes                                        | read at start by |
  |-----------------|-----------------------------------------------|------------------|
  | `gt_handoff.py` | `Projects/<slug>/handoff/<date>-handoff.md`   | nothing          |
  | `gt_state.py`   | `~/.claude/golden-thread/state/state-*.md`    | nothing          |
  | `gt-work`       | `p:: 1` tasks citing a handoff                | nothing          |
  | the owner       | dated must-do items (rotations, decisions)    | nothing          |

Measured 2026-09-28 (`self-verified`): the SessionStart hooks were gt_components, gt_workers,
gt_version_check, gt_push_check, inject_core_rules and the report card, and none of them read a
handoff, a state file, `TASKS.md` or any `p::`. So a handoff was written and a task was raised
and neither reached the session that should act on them unless someone thought to look. Two
credential rotations sat 15 days overdue at the vault's top priority, worked around almost
daily, because priority is a sort order and not an alarm: 60 `p:: 0` tasks existed across ten
projects, and a 61st alerts nobody.

SURFACING IS THE ALARM; THE TASK IS THE RECORD. This script is the alarm. It never creates,
edits or closes a task, and it never writes to the vault. The one thing it writes is a
machine-local "already shown" ledger, so a handoff is announced once rather than every session
until it is ignored -- the `gt_closeout` failure, where a question asked every session trained
the owner to scroll past it. (Handoffs used that ledger in the first 0.17.2 cut too; the owner
ruled the same day that an unhandled handoff must keep coming back, so they moved to a status.)

WHAT IT SHOWS, and how often:

  MUST DO     every session, recomputed live   -- from `<vault>/deadlines.md`. Ages and
                                                   countdowns are derived from the row's date
                                                   at run time, never stored, so the block
                                                   counts itself down and disappears when the
                                                   last row is deleted.
  handoffs    every session UNTIL HANDLED       -- one line each: path, project, age, open
                                                   items. Status comes from gt_handoff_status
                                                   (open / deferred-to-a-date / handled); the
                                                   `handoff_surface` setting picks WHERE:
                                                     any      every session start (default)
                                                     project  only when /gt:gt-open opens it
                                                              (`handoffs --project`)
                                                     manual   only /gt:gt-handle handoff
                                                   The body is never loaded: showing a handoff
                                                   must not cost the context of handling it.
  write queue every session while non-empty     -- a count of gt_write_queue.py requests
                                                   waiting for `gt_broker.py drain`; read-only.
  state files once each                         -- written by gt_state before a compaction; after
                                                   a compaction (`source: compact`) the newest
                                                   one's content is handed to the model in full.

A clean run says so in one line. Silence would read the same as a hook that never ran, which
is the failure the startup checks were given `systemMessage` to end (gt 0.9.6).

`deadlines.md` is ONE vault-root file with one table, one row per outstanding item:

    | item | category | due | see |
    |---|---|---|---|
    | Rotate monitor-server + relay credentials | rotation | 2026-09-13 | [[pending-rotations]] |

Adding a category is a row, not code. The detail -- a rotation's ordered steps, a decision's
comparison -- stays in the file `see` points at, because a rotation's danger is its SEQUENCE and
a one-line alarm cannot carry that. A row that does not parse is skipped and counted; the rest
still report. A label shaped like a credential is withheld, never printed: this output reaches
both the terminal and the model's context (Core rules 3 and 9).

NEVER FATAL. Any failure prints nothing harmful and exits 0; a SessionStart hook that breaks
startup is worse than the gap it closes.

Exit: 0 always from `check` (it reports, it does not gate) | 2 usage.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GT_HOME = Path.home() / ".claude" / "golden-thread"
STATE_DIR = GT_HOME / "state"
SEEN = GT_HOME / "surface" / "seen.json"

DEADLINES = "deadlines.md"
MUST_DO_AHEAD_DAYS = 14        # rows further out are counted, not listed
MAX_CONTEXT = 8000             # SessionStart output must stay well under 10,000
STATE_EXCERPT = 3000

DATE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*$")


# ------------------------------------------------------------------ plumbing ----

def _settings():
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import gt_settings                                        # noqa: PLC0415
        return gt_settings
    except Exception:
        return None


def enabled() -> bool:
    s = _settings()
    try:
        return (s.get("surface") if s else "on") != "off"
    except Exception:
        return True


def find_vault(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_dir() else None
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import gt_paths                                           # noqa: PLC0415
        return gt_paths.find_vault()
    except Exception:
        pass
    env = os.environ.get("GT_VAULT")
    if env and Path(env).is_dir():
        return Path(env)
    try:
        v = json.loads((Path.home() / ".claude" / "vault-config.json").read_text()).get("vault_path")
        return Path(v) if v and Path(v).is_dir() else None
    except Exception:
        return None


def today() -> datetime.date:
    # GT_TODAY pins the clock for tests, so a fixture's dates are fixed RELATIVE to the run
    # rather than to the day the test was written.
    pinned = os.environ.get("GT_TODAY")
    if pinned:
        try:
            return datetime.date.fromisoformat(pinned)
        except ValueError:
            pass
    return datetime.date.today()


def load_seen() -> dict:
    try:
        d = json.loads(SEEN.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_seen(d: dict) -> None:
    try:
        SEEN.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".seen.", dir=str(SEEN.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1, sort_keys=True)
        os.replace(tmp, SEEN)
    except Exception:
        pass                      # the ledger is a courtesy; losing it means one repeat


# ------------------------------------------------------------------ must-do ----

# A key followed by an actual VALUE: six or more characters that are not quoting or table
# punctuation. `pass=` with nothing after it is a label naming a credential, not a credential --
# the first real row of the vault's own deadlines.md read "Rotate monitor-server `pass=`" and was
# withheld until this required a value.
SECRET_KEY = re.compile(r"(pass(word|wd)?|secret|token|api[_-]?key|auth|credential|bearer)"
                        r"\s*[:=]\s*[^\s`'\"|)\]]{6,}", re.I)
SECRET_PREFIX = re.compile(r"\b(ghp_|gho_|github_pat_|sk-|xox[abpr]-|AKIA|AIza)[A-Za-z0-9_-]{8,}")
TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{20,}")


def _entropy(s: str) -> float:
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def looks_secret(text: str) -> bool:
    """A label is printed to the terminal AND the model, so anything credential-shaped is
    withheld whole. Deliberately broader than gt_secrets' rules: a false positive here costs
    one withheld label, a false negative puts a value in a transcript."""
    if SECRET_KEY.search(text) or SECRET_PREFIX.search(text):
        return True
    for tok in TOKEN.findall(text):
        # `handoff-ladder-2026-09-28` is long, mixed and high-entropy, and is a filename: every
        # separator-delimited piece is a plain word or a plain number. A generated secret is not
        # built that way. (Also found on the vault's own file, by the same first run.)
        parts = [x for x in re.split(r"[-_./]", tok) if x]
        if parts and all(x.isalpha() or x.isdigit() for x in parts):
            continue
        if (any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok)
                and _entropy(tok) >= 3.5):
            return True
    return False


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def read_deadlines(vault: Path):
    """-> (rows, skipped). rows: [{item, category, due(date), see}]. Never raises."""
    path = vault / DEADLINES
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return [], 0
    rows, skipped, cols = [], 0, None
    for line in lines:
        if not line.lstrip().startswith("|"):
            cols = None                              # a table ends at its first non-row
            continue
        cells = _cells(line)
        if cols is None:
            low = [c.lower() for c in cells]
            if {"item", "due"} <= set(low):
                cols = {name: low.index(name) for name in low}
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue                                 # the |---| separator
        try:
            item = cells[cols["item"]]
            m = DATE.match(cells[cols["due"]])
            if not item or not m:
                raise ValueError
            due = datetime.date.fromisoformat(m.group(1))
        except (IndexError, KeyError, ValueError):
            skipped += 1
            continue
        get = lambda k: cells[cols[k]] if k in cols and cols[k] < len(cells) else ""  # noqa: E731
        rows.append({"item": item, "category": get("category") or "-", "due": due,
                     "see": get("see")})
    return rows, skipped


def must_do(vault: Path) -> list[str]:
    rows, skipped = read_deadlines(vault)
    if not rows and not skipped:
        return []
    now = today()
    for r in rows:
        r["days"] = (r["due"] - now).days
    rows.sort(key=lambda r: r["days"])                # overdue (negative) first
    over = [r for r in rows if r["days"] < 0]
    soon = [r for r in rows if 0 <= r["days"] <= MUST_DO_AHEAD_DAYS]
    later = [r for r in rows if r["days"] > MUST_DO_AHEAD_DAYS]
    if not over and not soon and not skipped:
        return []
    head = []
    if over:
        head.append("%d overdue" % len(over))
    if soon:
        head.append("%d due within %dd" % (len(soon), MUST_DO_AHEAD_DAYS))
    out = ["MUST DO: %s  (from %s)" % (", ".join(head) or "nothing due", DEADLINES)]
    for r in over + soon:
        label = r["item"]
        if looks_secret(label) or looks_secret(r["see"]):
            label, see = "[label withheld: it looks like a credential -- fix the row]", ""
        else:
            see = ("  -> %s" % r["see"]) if r["see"] else ""
        when = ("\U0001F534 OVERDUE %dd" % -r["days"] if r["days"] < 0 else
                "\U0001F7E1 due today" if r["days"] == 0 else "\U0001F7E1 due in %dd" % r["days"])
        out.append("  - %-16s %-9s %s%s" % (when, r["category"], label, see))
    if later:
        out.append("  (+%d more due after %dd)" % (len(later), MUST_DO_AHEAD_DAYS))
    if skipped:
        out.append("  (%d row%s in %s could not be read -- a date must be YYYY-MM-DD)"
                   % (skipped, "" if skipped == 1 else "s", DEADLINES))
    return out


# ------------------------------------------------------------------ handoffs ----

HANDOFF_MODES = ("any", "project", "manual")


def handoff_mode() -> str:
    s = _settings()
    try:
        v = s.get("handoff_surface") if s else None
    except Exception:
        v = None
    return v if v in HANDOFF_MODES else "any"


def _status_mod():
    """gt_handoff_status owns what 'waiting' means; this only displays it. A second copy of the
    rule here is the duplicated-gatherer defect 0.17.1 found four times."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import gt_handoff_status                                      # noqa: PLC0415
    return gt_handoff_status


def handoff_lines(vault: Path, project: str | None = None) -> list[str]:
    """One line per waiting handoff -- path, project, age, open items. Never the body: showing
    a handoff must not cost the context of handling it (owner, 2026-09-28)."""
    try:
        hs = _status_mod().waiting(vault, project)
    except Exception:
        return []
    if not hs:
        return []
    mod = _status_mod()
    out = ["HANDOFF%s WAITING%s -- handle with /gt:gt-handle handoff:"
           % ("S" if len(hs) > 1 else "", (" in " + project) if project else "")]
    out += ["  - " + mod.line(r) for r in hs]
    return out


# --------------------------------------------------------------------- tasks ----

def task_line(vault: Path) -> list[str]:
    """One line: how many `p:: 1` tasks wait on the owner, how many are overdue or stale. Counted
    by the VAULT's gt_task.py (the one parser, shared with the TASKS.md rollup); a vault not yet
    refreshed to 0.17.2 has no gt_task.py and this says nothing rather than guessing."""
    s = _settings()
    try:
        if s and s.get("task_surface") == "off":
            return []
    except Exception:
        pass
    tool = vault / "Projects" / "golden-thread" / "tools" / "gt_task.py"
    if not tool.is_file():
        return []
    try:
        import subprocess                                         # noqa: PLC0415
        p = subprocess.run([sys.executable, str(tool), "count", "--vault", str(vault), "--json"],
                           capture_output=True, text=True, timeout=20)
        d = json.loads(p.stdout) if p.returncode == 0 else None
    except Exception:
        d = None
    if not d or not d.get("p1_waiting_on_user"):
        return []
    return ["TASKS: %d p:: 1 waiting on you (%d overdue, %d open over a week) -- "
            "/gt:gt-list tasks mine p1, /gt:gt-handle task to work through them"
            % (d["p1_waiting_on_user"], d.get("overdue", 0), d.get("stale", 0))]


# --------------------------------------------------------------- write queue ----

def queue_line(vault: Path) -> list[str]:
    """One line when gt_write_queue.py requests are waiting for `gt_broker.py drain`. Counted
    by listing the directory, not by importing the broker: this runs from the hooks dir, and it
    only reads -- draining is always explicit."""
    try:
        q = vault / "Projects" / "golden-thread" / "spool" / "queue"
        n = sum(1 for p in q.iterdir() if p.suffix == ".json" and not p.name.startswith("."))
    except OSError:
        return []
    if not n:
        return []
    return ["WRITE QUEUE: %d vault write request%s waiting -- apply with "
            "`gt_broker.py drain --vault <vault>`" % (n, "" if n == 1 else "s")]


# ---------------------------------------------------------------- state files ----

def new_state_files(seen: dict):
    try:
        files = sorted(STATE_DIR.glob("state-*.md"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
    except OSError:
        return []
    last = seen.get("state:last", 0)
    return [f for f in files if f.stat().st_mtime > last]


# ------------------------------------------------------------------ assembly ----

def hook_source() -> str:
    """SessionStart's `source`: startup | resume | clear | compact. Read without blocking."""
    try:
        import select                                             # noqa: PLC0415
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        r, _, _ = select.select([sys.stdin], [], [], 0.3)
        if not r:
            return ""
        d = json.loads(sys.stdin.read() or "{}")
        return str(d.get("source") or "") if isinstance(d, dict) else ""
    except Exception:
        return ""


def build(vault: Path | None, source: str, seen: dict):
    """-> (message lines for the terminal, extra context for the model, seen updates)."""
    lines, ctx, updates = [], [], {}
    if vault is not None:
        lines += must_do(vault)
        if handoff_mode() == "any":
            hl = handoff_lines(vault)
            if hl:
                lines += hl
                ctx.append("A previous session left the handoff(s) above because it could not "
                           "capture everything. Do NOT read them now unless the user is working "
                           "in that project or asks; tell the user they are waiting and that "
                           "/gt:gt-handle handoff deals with them.")
        lines += task_line(vault)
        lines += queue_line(vault)
    st =new_state_files(seen)
    if st:
        lines.append("SESSION STATE written before a compaction: %s%s"
                     % (st[0], "" if len(st) == 1 else "  (+%d older)" % (len(st) - 1)))
        updates["state:last"] = st[0].stat().st_mtime
        if source == "compact":
            try:
                body = st[0].read_text(encoding="utf-8")[:STATE_EXCERPT]
                ctx.append("This session was just compacted. The state gt_state wrote before "
                           "it is below; treat it as the record of decisions already made -- "
                           "do not re-ask them.\n\n" + body)
            except Exception:
                pass
    return lines, ctx, updates


def emit(text: str, context: str, as_hook: bool) -> None:
    if not as_hook:
        print(text if not context else text + "\n\n" + context)
        return
    full = (text + ("\n\n" + context if context else ""))[:MAX_CONTEXT]
    json.dump({"systemMessage": text,
               "hookSpecificOutput": {"hookEventName": "SessionStart",
                                      "additionalContext": full}},
              sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def do_check(a, as_hook: bool) -> int:
    if not enabled():
        return 0
    vault = find_vault(a.vault)
    seen = load_seen()
    lines, ctx, updates = build(vault, hook_source() if as_hook else "", seen)
    if a.json:
        print(json.dumps({"vault": str(vault) if vault else None, "lines": lines,
                          "context": ctx}, indent=1))
    elif lines:
        emit("GOLDEN THREAD surface:\n" + "\n".join(lines), "\n\n".join(ctx), as_hook)
    else:
        why = "" if vault else " (no vault found -- handoffs and MUST DO not checked)"
        emit("GOLDEN THREAD surface: nothing waiting -- no MUST DO item, no handoff waiting, "
             "no new state file%s." % why, "", as_hook)
    if updates and not a.dry_run:
        seen.update(updates)
        save_seen(seen)
    if vault is not None and not a.dry_run:
        refresh_reminder_mirror(vault)
    return 0


def refresh_reminder_mirror(vault: Path) -> None:
    """0.18.1: the reminder tool's scheduled job cannot read a vault under CloudStorage (macOS
    TCC refuses launchd jobs), so every session start -- which can -- copies deadlines.md to
    ~/.claude/golden-thread/reminder/deadlines.json. Only when a push channel is on or a mirror
    already exists, so an install that never asked for reminders gets no new file. Outside the
    vault; never fatal."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import gt_reminder                                        # noqa: PLC0415
        if gt_reminder.enabled_channels() or gt_reminder.mirror_path().is_file():
            gt_reminder.refresh_mirror(vault)
    except Exception:
        pass


def do_handoffs(a) -> int:
    """For /gt:gt-open: this project's waiting handoffs, in `any` and `project` modes. `manual`
    means the owner asked to see them only through /gt:gt-handle handoff, so this is silent."""
    vault = find_vault(a.vault)
    if vault is None:
        print("gt-surface: no vault found; pass --vault", file=sys.stderr)
        return 0
    if handoff_mode() == "manual":
        return 0
    # A bare sub-project slug means its parent/child path (gt_spool.resolve_project, 0.18.1).
    # Soft on purpose: this runs inside /gt:gt-open, which must never fail on a surface line,
    # so a name the resolver cannot settle filters as typed (and so shows nothing), as before.
    try:
        sys.path.insert(0, str(HERE.parent / "templates" / "tools"))
        import gt_spool
        a.project = gt_spool.resolve_project(vault, a.project)
    except Exception:
        pass
    hl = handoff_lines(vault, a.project)
    print("\n".join(hl) if hl else "no handoff waiting in %s" % a.project)
    return 0


def do_must_do(a) -> int:
    vault = find_vault(a.vault)
    if vault is None:
        print("gt-surface: no vault found; pass --vault", file=sys.stderr)
        return 0
    block = must_do(vault)
    print("\n".join(block) if block else "MUST DO: nothing overdue or due within %dd."
          % MUST_DO_AHEAD_DAYS)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_hook = "--hook" in argv or os.environ.get("GT_HOOK") == "1"
    argv = [x for x in argv if x != "--hook"]
    ap = argparse.ArgumentParser(prog="gt_surface.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="SessionStart: surface MUST DO, handoffs and state")
    c.add_argument("--vault")
    c.add_argument("--dry-run", action="store_true",
                   help="report without recording anything as shown")
    c.add_argument("--json", action="store_true")
    c.add_argument("--hook", action="store_true",
                   help="emit SessionStart hook JSON (also GT_HOOK=1); install.sh passes it")
    m = sub.add_parser("must-do", help="print the MUST DO block only")
    m.add_argument("--vault")
    h = sub.add_parser("handoffs", help="one project's waiting handoffs (for /gt:gt-open)")
    h.add_argument("--project", required=True)
    h.add_argument("--vault")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "handoffs":
            return do_handoffs(a)
        return do_check(a, as_hook) if a.cmd == "check" else do_must_do(a)
    except Exception as exc:                  # never fatal at session start
        print("gt-surface: could not run (%s)" % exc.__class__.__name__, file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
