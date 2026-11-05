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
                                                   a compaction (`source: compact`) the content of
                                                   THAT session's newest one (state-<sid8>-*.md,
                                                   the payload's session_id; 0.21.4) is handed to
                                                   the model in full -- never another session's,
                                                   never an old-format file whose session is
                                                   unknown. Its "taken" mark is per session.

ORDER (0.21.4). The sections above print in a fixed default order: must_do, handoffs, tasks,
write_queue, sandbox, pickup, state. A user persona's `session_focus` (packs/personas, ADR-18)
moves its sections to the front; the rest follow in the default order. It reorders the surface
lines only: every section is still shown, and the extra notes for the model keep their default
order. No persona: output is byte-identical to before.

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
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
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


# startup_output compact (0.21.4): each MUST DO row is cut to about this many characters at session
# start; the row's `see` link is kept, the label gives way. The full row is in deadlines.md.
MUST_DO_WIDTH = 100


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[:max(width - 1, 0)].rstrip() + "\u2026"


def must_do(vault: Path, cap: bool = False, compact: bool = False) -> list[str]:
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
    shown = over + soon
    hidden = shown[SESSION_LIST_CAP:] if cap else []
    for r in (shown[:SESSION_LIST_CAP] if cap else shown):
        label = r["item"]
        if looks_secret(label) or looks_secret(r["see"]):
            label, see = "[label withheld: it looks like a credential -- fix the row]", ""
        else:
            see = ("  -> %s" % r["see"]) if r["see"] else ""
        when = ("\U0001F534 OVERDUE %dd" % -r["days"] if r["days"] < 0 else
                "\U0001F7E1 due today" if r["days"] == 0 else "\U0001F7E1 due in %dd" % r["days"])
        if compact:
            pre = "  - %s %s " % (when, r["category"])
            see = see.replace("  -> ", " -> ")
            out.append(pre + _fit(label, MUST_DO_WIDTH - len(pre) - len(see)) + see)
            continue
        out.append("  - %-16s %-9s %s%s" % (when, r["category"], label, see))
    if hidden:
        out.append("  (+%d more due soon or overdue: %s)" % (len(hidden), DEADLINES))
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


def handoff_lines(vault: Path, project: str | None = None, cap: bool = False,
                  compact: bool = False) -> list[str]:
    """One line per waiting handoff -- path, project, age, open items. Never the body: showing
    a handoff must not cost the context of handling it (owner, 2026-09-28)."""
    try:
        hs = _status_mod().waiting(vault, project)
    except Exception:
        return []
    if not hs:
        return []
    mod = _status_mod()
    if compact:
        # startup_output compact (0.21.4): the count and the single oldest; the list is one
        # command away.
        o = hs[0]
        return ["HANDOFFS WAITING: %d -- oldest %s (%s, %dd old) -- /gt:gt-list handoffs, "
                "/gt:gt-handle handoff" % (len(hs), o["path"], o["project"], o["age_days"])]
    out = ["HANDOFF%s WAITING%s -- handle with /gt:gt-handle handoff:"
           % ("S" if len(hs) > 1 else "", (" in " + project) if project else "")]
    out += ["  - " + mod.line(r) for r in (hs[:SESSION_LIST_CAP] if cap else hs)]
    if cap and len(hs) > SESSION_LIST_CAP:
        out.append("  (+%d more: /gt:gt-list handoffs)" % (len(hs) - SESSION_LIST_CAP))
    return out


# ----------------------------------------------------------------- calm start ----

def calm_start_on() -> bool:
    """0.22.0, phase 89d: the calm form is for a plain-mode persona (gt_persona.plain_mode, its own
    pack or one it extends) and is off with `calm_start off`. No persona, or any failure, is the
    ordinary surface."""
    if _setting_value("calm_start", "auto") == "off":
        return False
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import gt_persona                                         # noqa: PLC0415
        return bool(gt_persona.plain_mode(gt_persona.resolve()))
    except Exception:
        return False


def calm_lines(vault: Path, handoffs: bool = True) -> list[str]:
    """At most three plain lines in place of the TASKS and HANDOFFS lines: what is due soon, a handoff
    waiting (only when there is one), and what to say next. One definition per count: `overdue` is an
    open task past its due date, `due this week` is due in the next seven days, `waiting on you` is a
    task marked waiting:: user. The details are one command away; nothing is read in full here."""
    out = []
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import gt_next                                            # noqa: PLC0415
        now = today()
        ts = gt_next.tasks(vault, now)
    except Exception:
        ts = None
    if ts is not None:
        week = now + datetime.timedelta(days=7)
        od = sum(1 for t in ts if t["due"] and t["due"] < now)
        soon = sum(1 for t in ts if t["due"] and now <= t["due"] <= week)
        mine = sum(1 for t in ts if t["waiting"] == "user")
        bits = [("%d overdue" % od) if od else "", ("%d due this week" % soon) if soon else "",
                ("%d waiting on you" % mine) if mine else ""]
        bits = [b for b in bits if b]
        out.append("Today: " + (", ".join(bits) if bits else "nothing due this week and nothing waiting on you") + ".")
    if handoffs:
        try:
            n = len(_status_mod().waiting(vault))
        except Exception:
            n = 0
        if n:
            out.append("Handoff: %d left by an earlier session -- say \"handle the handoff\"." % n)
    out.append("Not sure where to start? Say \"what should I do next\" (or /gt:gt-next); "
               "/gt:gt-list shows the details.")
    return out


# --------------------------------------------------------------------- tasks ----

def _task_count(vault: Path):
    """The vault's gt_task.py `count --json`, or None (no tool, not runnable)."""
    tool = vault / "Projects" / "golden-thread" / "tools" / "gt_task.py"
    if not tool.is_file():
        return None
    try:
        import subprocess                                         # noqa: PLC0415
        p = subprocess.run([sys.executable, str(tool), "count", "--vault", str(vault), "--json"],
                           capture_output=True, text=True, timeout=20)
        return json.loads(p.stdout) if p.returncode == 0 else None
    except Exception:
        return None


def farmed_line(vault: Path) -> list[str]:
    """ONE short line, or nothing: items (handoff items and tasks) farmed to an external engine and
    still waiting for their answer (phase 78). `2 items farmed to gemini-row, waiting`. The engine is
    a farm/LOTR row name; gt_handoff_status owns what 'waiting' means."""
    try:
        mod = _status_mod()
        waiting = dict(mod.farmed_waiting(vault))
        d = _task_count(vault) or {}
        for eng, n in (d.get("farmed_engines") or {}).items():
            waiting[eng] = waiting.get(eng, 0) + int(n)
        text = mod.farmed_text(waiting)
    except Exception:
        return []
    return ["FARMED: " + text] if text else []


def farm_engines_lines(source: str) -> list[str]:
    """Phase 76: start the bounded background refresh of the farm's engine status (setting
    farm_engine_refresh, never on a compaction or clear) and return ONE line when an engine is down:
    `farm engines: 5 of 7 available, engine-5 down since 14:02`. Silent when gt-farm is not installed,
    no engine is configured or all are fine. The refresh never blocks and never raises."""
    try:
        import gt_farm_link
        mod = gt_farm_link.farm_module("gt_farm_engines_check")
        if mod is None:
            return []
        if source not in ("compact", "clear"):
            mod.session_refresh()
        line = mod.session_line()
        return [line] if line else []
    except Exception:
        return []


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
    d = _task_count(vault)
    if not d or not d.get("p1_waiting_on_user"):
        return []
    return ["TASKS: %d p:: 1 waiting on you (%d overdue, %d open over a week) -- "
            "/gt:gt-list tasks mine p1, /gt:gt-handle task to work through them"
            % (d["p1_waiting_on_user"], d.get("overdue", 0), d.get("stale", 0))]


# --------------------------------------------------------------- write queue ----

def _drain_command(vault: Path) -> str:
    """The drain spelled with real paths (0.20.1): this interpreter, the broker beside this file
    (the hooks dir), the vault -- never `<vault>` where the vault is known."""
    import shlex
    q = (lambda x: '"%s"' % x) if os.name == "nt" else shlex.quote
    return "%s %s drain --vault %s" % (q(sys.executable or "python3"),
                                       q(str(HERE / "gt_broker.py")), q(str(vault)))


def queue_line(vault: Path, sandbox: bool = False) -> list[str]:
    """One line when gt_write_queue.py requests are waiting for `gt_broker.py drain`. Counted
    by listing the directory, not by importing the broker: this runs from the hooks dir, and it
    only reads -- draining is always explicit.

    Under gt sandbox mode (0.20.1, B4) the line must not tell the model to run the drain: from
    Claude's shell it is refused. No hook drains either (SessionStart and Stop only read); the
    drain runs outside the sandbox through the gt-vault MCP (vault_queue_drain, and every
    vault_queue_write drains at once), or from a terminal."""
    try:
        q = vault / "Projects" / "golden-thread" / "spool" / "queue"
        n = sum(1 for p in q.iterdir() if p.suffix == ".json" and not p.name.startswith("."))
    except OSError:
        return []
    if not n:
        return []
    if sandbox:
        return ["WRITE QUEUE: %d vault write request%s waiting -- sandbox mode: the drain runs "
                "outside the sandbox, through the gt-vault MCP tool vault_queue_drain (not from "
                "Claude's shell), or from a terminal: %s"
                % (n, "" if n == 1 else "s", _drain_command(vault))]
    return ["WRITE QUEUE: %d vault write request%s waiting -- apply with `%s`"
            % (n, "" if n == 1 else "s", _drain_command(vault))]


def _sandbox_config():
    """vault-config.json when gt sandbox mode is on there, else None."""
    try:
        import json as _json
        cfg = _json.loads((Path.home() / ".claude" / "vault-config.json").read_text(
            encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return None
    if not isinstance(cfg, dict) or str(cfg.get("sandbox_mode") or "").strip().lower() != "on":
        return None
    return cfg


# ------------------------------------------------------------ gt sandbox mode ----

SANDBOX_CONTEXT = (
    "gt sandbox mode is ON: Claude Code's sandbox and permission rules deny this session's "
    "shell and file tools %s the vault and gt's state. Skills that say to Read, Grep or "
    "Edit a vault file, or to run a vault script, must instead use the gt-vault MCP tools: "
    "vault_search / vault_read / vault_list to read, vault_queue_write to write (the broker "
    "applies it outside the sandbox and the result says applied, held or escalated). "
    "gt_write_queue.py from the shell leaves its request in ~/.gt-inbox/queue; vault_queue_drain "
    "applies it. Never run `gt_broker.py drain` from the shell: no hook drains the queue, the "
    "drain runs outside the sandbox only through the gt-vault MCP (vault_queue_drain; "
    "vault_queue_write drains at once) or from the user's terminal. gt's vault tools under "
    "<vault>/Projects/golden-thread/tools (gt_log, gt_tasks, gt_adr, gt_task, gt_closeout) and "
    "gt_lint, gt_promote_detect and vault_init are refused from the shell in sandbox mode "
    "(preview): when a skill says to run one, give the user the exact command to run in a "
    "terminal instead. Do not try to bypass the sandbox.")


def sandbox_lines(vault: Path | None):
    """(lines, context) while gt sandbox mode is on (0.20.1): one line saying so, the inbox count,
    and the routing note for the model. Two small file reads -- no vault walk at session start."""
    cfg = _sandbox_config()
    if cfg is None:
        return [], []
    reads = str(cfg.get("sandbox_vault_reads") or "deny").strip().lower() != "allow"
    win = os.name == "nt"
    line = ("SANDBOX MODE: on (preview) -- %s; the vault is reached through the gt-vault MCP tools"
            % ("native Windows: permission rules only (friction, not a boundary)" if win else
               "Claude's shell and file tools cannot write the vault%s" % (
                   " or read it" if reads else "")))
    inbox = Path.home() / ".gt-inbox" / "queue"
    try:
        n = sum(1 for p in inbox.iterdir() if p.suffix == ".json" and not p.name.startswith("."))
    except OSError:
        n = 0
    lines = [line]
    if n:
        lines.append("SANDBOX INBOX: %d write request%s waiting in %s -- the vault_queue_drain "
                     "tool applies %s" % (n, "" if n == 1 else "s", inbox,
                                         "it" if n == 1 else "them"))
    return lines, [SANDBOX_CONTEXT % ("both reads of and writes to" if reads else "writes to")]


# ---------------------------------------------------------------- state files ----

# state-<sid8>-<stamp>.md since 0.21.4 (gt_state.state_name); state-<stamp>.md before, and still
# when gt_state knew no session. Several sessions run at once, often in one folder: after a
# compaction only THAT session's file is handed to the model, never the newest of any session.
STATE_NEW = re.compile(r"^state-([A-Za-z0-9_]{1,8})-\d{4}-\d\d-\d\dT\d{6}\.md$")


def state_session(path) -> str | None:
    """The 8-character session prefix a state file was written for; None for an old-format file."""
    m = STATE_NEW.match(Path(path).name)
    return m.group(1) if m else None


def sid8(session) -> str:
    """gt_state._sid8: the first 8 characters of a session id, filename-safe."""
    return re.sub(r"[^A-Za-z0-9]", "_", str(session or "")[:8])


def _state_files():
    try:
        return sorted(STATE_DIR.glob("state-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def new_state_files(seen: dict, files=None):
    """Files no session start has pointed at yet (the shared "named once" pointer)."""
    files = _state_files() if files is None else files
    last = seen.get("state:last", 0)
    return [f for f in files if f.stat().st_mtime > last]


def own_state_file(seen: dict, session, files=None):
    """This session's newest state file not yet handed to it, or None. Its own ledger key
    (state:<sid8>), so another session's start never uses it up."""
    me = sid8(session)
    if not me:
        return None
    files = _state_files() if files is None else files
    last = seen.get("state:" + me, 0)
    for f in files:
        if state_session(f) == me and f.stat().st_mtime > last:
            return f
    return None


# ------------------------------------------------------------------ assembly ----

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
    """SessionStart's payload ({} when there is none). Read without blocking."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        if not _stdin_ready(0.3):
            return {}
        d = json.loads(sys.stdin.read() or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# The session-start block names this many items per list; the rest are one command away (0.21.4).
SESSION_LIST_CAP = 3
SECTIONS = ("must_do", "handoffs", "tasks", "write_queue", "sandbox", "pickup", "state")


def section_order() -> tuple:
    """The order the sections are shown in: the user's personas' `session_focus` first, then the
    rest in the default order (gt_persona.surface_order, 0.21.4). A persona reorders, never hides:
    every section is still built and shown. No persona, or any failure, is the default order."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import gt_persona                                         # noqa: PLC0415
        names = gt_persona.resolve()
        if not names:
            return SECTIONS
        order = tuple(gt_persona.surface_order(names))
        return order if sorted(order) == sorted(SECTIONS) else SECTIONS
    except Exception:
        return SECTIONS


def _compact() -> bool:
    s = _settings()
    try:
        return bool(s and s.startup_compact())
    except Exception:
        return False


def build(vault: Path | None, source: str, seen: dict, compact: bool = False, session=None,
          cwd=None):
    """-> (message lines for the terminal, extra context for the model, seen updates).

    `session` is the payload's session_id: after a compaction only that session's own state file
    is handed to the model (0.21.4); with no session, none is.

    Each section is built in the default order and the extra context notes keep that order; only
    the surface lines are put in section_order(), and every section is still in them."""
    parts, ctx, updates = {k: [] for k in SECTIONS}, [], {}
    if vault is not None:
        parts["must_do"] = must_do(vault, cap=True, compact=compact)
        if handoff_mode() == "any":
            hl = handoff_lines(vault, cap=True, compact=compact)
            if hl:
                parts["handoffs"] = hl
                ctx.append("Handoffs: do not read them now unless the user works in that project "
                           "or asks; say they are waiting (/gt:gt-handle handoff)." if compact else
                           "A previous session left the handoff(s) above because it could not "
                           "capture everything. Do NOT read them now unless the user is working "
                           "in that project or asks; tell the user they are waiting and that "
                           "/gt:gt-handle handoff deals with them.")
        if calm_start_on():
            parts["handoffs"] = []
            parts["tasks"] = calm_lines(vault, handoffs=handoff_mode() == "any")
        else:
            parts["tasks"] = task_line(vault)
        parts["tasks"] = list(parts["tasks"]) + farmed_line(vault)
        # native Windows has no sandbox: its shell can still run the drain
        parts["write_queue"] = queue_line(vault, sandbox=os.name != "nt"
                                          and _sandbox_config() is not None)
    parts["tasks"] = list(parts["tasks"]) + farm_engines_lines(source)
    sl, sc = sandbox_lines(vault)
    try:                                         # 0.22.0, 89e: prompts off is said once, here, or not at all
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import gt_bypass                                          # noqa: PLC0415
        warn = gt_bypass.line(cwd or os.getcwd())
        sl = sl + [warn] if warn else sl
    except Exception:
        pass
    parts["sandbox"] = sl
    ctx += sc
    # /clear after the expiry guard offered its handoff (0.21.2): load it into the fresh session
    try:
        import gt_expiry_guard
        pc, pl = gt_expiry_guard.take_pickup(source, session)
        if pc:
            parts["pickup"] = [pl]
            ctx.append(pc)
    except Exception:
        pass
    files = _state_files()
    st = new_state_files(seen, files)
    own = own_state_file(seen, session, files) if source == "compact" else None
    if st:
        updates["state:last"] = st[0].stat().st_mtime
    if own is not None:
        parts["state"] = ["SESSION STATE written before this compaction: %s" % own]
        updates["state:" + sid8(session)] = own.stat().st_mtime
        try:
            body = own.read_text(encoding="utf-8")[:STATE_EXCERPT]
            ctx.append("This session was just compacted. The state gt_state wrote before "
                       "it is below; treat it as the record of decisions already made -- "
                       "do not re-ask them.\n\n" + body)
        except Exception:
            pass
    elif st:
        parts["state"] = ["SESSION STATE written before a compaction: %s%s"
                          % (st[0], "" if len(st) == 1 else "  (+%d older)" % (len(st) - 1))]
    # per-session keys of sessions whose files are gone (pruned) are dropped, so seen.json stays small
    live = {state_session(f) for f in files}
    for k in [k for k in seen if k.startswith("state:") and k != "state:last" and k[6:] not in live]:
        updates[k] = None
    lines = []
    for k in (section_order() if any(parts.values()) else SECTIONS):
        lines += parts[k]
    return lines, ctx, updates


def emit(text: str, context: str, as_hook: bool, user_only: str = "") -> None:
    """`user_only` (0.22.0) is shown to the user in systemMessage and is NEVER put in the model's context."""
    if not as_hook:
        shown = text if not user_only else text + "\n" + user_only
        print(shown if not context else shown + "\n\n" + context)
        return
    full = (text + ("\n\n" + context if context else ""))[:MAX_CONTEXT]
    json.dump({"systemMessage": text if not user_only else text + "\n" + user_only,
               "hookSpecificOutput": {"hookEventName": "SessionStart",
                                      "additionalContext": full}},
              sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def _tokens_label(tokens: int) -> str:
    return "~%.1fk tokens" % (tokens / 1000.0)


def load_receipt(vault: Path | None) -> str:
    """0.22.0: ONE line for the user saying what gt can see it loads and how big global-memory is against a
    soft budget. -> "" when off, with no vault, or when nothing can be measured. Estimates are characters
    divided by four. Never raises; a folder it cannot read is left out of the line."""
    try:
        if vault is None or not _settings_on("load_receipt", "on"):
            return ""
        parts = []
        try:
            n = len([f for f in (vault / "core-rules").iterdir()
                     if f.suffix == ".md" and f.name.lower() not in ("readme.md", "index.md")])
            if n:
                parts.append("%d Core rules" % n)
        except OSError:
            pass
        try:
            files = [f for f in (vault / "global-memory").iterdir() if f.is_file() and f.suffix == ".md"]
            chars = sum(f.stat().st_size for f in files)
            tokens = chars // 4
            piece = "global-memory %d files, %s" % (len(files), _tokens_label(tokens))
            budget = _setting_value("global_memory_budget", "12000")
            if budget.isdigit() and int(budget) > 0:
                b = int(budget)
                piece += (", over the soft budget of %.1fk (nothing is refused; trim it with /gt:gt-lint)"
                          % (b / 1000.0) if tokens > b else ", of a soft %.1fk budget" % (b / 1000.0))
            parts.append(piece)
        except OSError:
            pass
        return "gt loaded: " + " · ".join(parts) if parts else ""
    except Exception:
        return ""


def _setting_value(name: str, default: str) -> str:
    try:
        v = _settings()
        got = v.get(name) if hasattr(v, "get") else None
        return str(got if got not in (None, "") else default).strip().lower()
    except Exception:
        return default


def _settings_on(name: str, default: str) -> bool:
    return _setting_value(name, default) != "off"


# What a recorded gt model profile assumes the session runs on (phase 89d). `inherit` assumes nothing.
PROFILE_FAMILY = {"very-high": "opus", "average": "sonnet"}
MODEL_FAMILIES = ("opus", "sonnet", "haiku", "fable")


def _model_family(model) -> str | None:
    if isinstance(model, dict):
        model = model.get("id") or model.get("display_name")
    if not isinstance(model, str):
        return None
    low = model.lower()
    return next((f for f in MODEL_FAMILIES if f in low), None)


def model_line(model, profile) -> list[str]:
    """ONE line when the session's model is not the family gt's recorded profile assumes; [] when they
    agree, when the profile is inherit/unset, or when the model is unknown. Names families, not ids. It
    only says so: nothing is changed (the profile is gt's own record; /gt:gt-settings models owns it)."""
    want = PROFILE_FAMILY.get(profile if isinstance(profile, str) else "")
    have = _model_family(model)
    if not want or not have or want == have:
        return []
    return ["MODEL: gt's model profile is %s (it assumes %s) but this session runs %s; nothing was "
            "changed. /gt:gt-settings models shows or changes the profile." % (profile, want, have)]


def _session_model_line(payload: dict) -> list[str]:
    try:
        return model_line(payload.get("model"), _setting_value("model_profile", "inherit"))
    except Exception:                                   # noqa: BLE001 - advisory, fail open
        return []


def do_check(a, as_hook: bool) -> int:
    if not enabled():
        return 0
    vault = find_vault(a.vault)
    seen = load_seen()
    payload = hook_payload() if as_hook else {}
    lines, ctx, updates = build(vault, str(payload.get("source") or ""), seen,
                                compact=as_hook and _compact(), session=payload.get("session_id"),
                                cwd=payload.get("cwd"))
    lines = lines + _session_model_line(payload)
    receipt = load_receipt(vault)
    if a.json:
        print(json.dumps({"vault": str(vault) if vault else None, "lines": lines,
                          "context": ctx, "receipt": receipt}, indent=1))
    elif lines:
        emit("GOLDEN THREAD surface:\n" + "\n".join(lines), "\n\n".join(ctx), as_hook, receipt)
    else:
        why = "" if vault else " (no vault found -- handoffs and MUST DO not checked)"
        emit("GOLDEN THREAD surface: nothing waiting -- no MUST DO item, no handoff waiting, "
             "no new state file%s." % why, "", as_hook, receipt)
    if updates and not a.dry_run:
        # re-read just before writing: another session may have started meanwhile
        seen = load_seen()
        seen.update(updates)
        save_seen({k: v for k, v in seen.items() if v is not None})
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
    # paranoid.signed_manifest (0.21.0): when gt's code is sealed, a hook checks its own files
    # first (gt_integrity_gate: one stat() when the group is off). Only as a hook -- the CLI
    # forms of this script stay usable, and are the repair route.
    try:
        import gt_integrity_gate as _gig
    except ImportError:
        _gig = None
    if _gig is not None:
        _gig.check(__file__, "reporter", when="--hook" in sys.argv)
    raise SystemExit(main())
