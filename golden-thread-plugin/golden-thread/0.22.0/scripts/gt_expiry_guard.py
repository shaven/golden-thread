#!/usr/bin/env python3
"""gt_expiry_guard.py -- cut an idle session while its prompt cache is still warm (0.21.0).

    gt_expiry_guard.py arm --hook        Stop and Notification idle_prompt: arm one one-shot job
    gt_expiry_guard.py fire <session>    the job: re-check, then compact / fork / notify
    gt_expiry_guard.py backstop --hook   UserPromptSubmit: cancel the job; block a cold resume once
    gt_expiry_guard.py status [--json]   settings, armed jobs, the last actions
    gt_expiry_guard.py cancel [<session> | --all]

Request 2026-10-08-cache-expiry-guard-cut-before-reread. A session idle past its cache lifetime
(5 minutes, or 1 hour on a subscription) pays a full rebuild of its context on the next prompt;
measured on the owner's machine, expiry was 70.7% of all cache writes in a week. Cutting with
`/compact` BEFORE the lapse reads the context at cache price (about 0.1x) instead of writing it
again (1.25x or 2.0x). Verified live on 2026-10-08: `claude -p --resume <id> "/compact ..."` compacts
headlessly and its prefix is read from the cache (a 154K-token context: $0.058 warm, $0.355 cold).

HOW IT RUNS.
  arm       Runs on Stop (the end of every reply, since 0.21.2) and again on the idle_prompt hook
            (about 60 s after Claude finishes with no typing). Stop is the one that matters:
            Claude Code's idle notifier is a single 60 s timer that gives up for the whole idle
            period when the terminal was touched, a dialog is up, a loop wakeup is pending or
            background agents run, so arming from idle_prompt alone left such sessions unarmed
            for hours with nothing logged (2026-10-12). Whichever comes first arms; the other finds
            the same transcript already armed and only completes the pairing with the mod
            (below). Every decline is logged as `arm-skipped` with the reason. It measures
            the transcript (context from the last usage row, model, effort, the TTL from which of
            ephemeral_1h / ephemeral_5m the cache was written with), records a state file under
            ~/.claude/golden-thread/expiry-guard/ and schedules ONE job for last activity + TTL -
            margin. One job per idle period: the same transcript does not arm twice.
  fire      The job. Does nothing if the session changed since arming, is below
            expiry_guard.min_tokens, the master is off, or the cache already lapsed. Otherwise,
            by `expiry_guard` and the session being open or closed:
              auto      closed -> headless /compact (same model, effort, cwd)
                        open   -> the SAME compaction on a --fork-session copy + a notification with
                                  the resume command; the live session is never written to. Since
                                  0.21.2 the copy's summary is saved as a handoff (below).
              warn-only a notification only, never a model call   (the shipped default)
              off       nothing at all
  backstop  UserPromptSubmit. ANY prompt cancels an armed job. If the session is large and its
            cache has lapsed and the guard did not cut, the first prompt is blocked once (a blocked
            prompt never reaches the model, so it costs nothing) with the size, the estimated
            rebuild cost and its choices; sending again within 2 minutes goes through.

IN PLACE (0.21.2). A separate process cannot compact an open session: the running Claude Code keeps
the conversation in its own memory. hooks/guard_mod.js, a Claude Code mod (gt's floor 2.1.292; session id from 2.1.296), runs
INSIDE the session and can. It cannot learn its session id, so the two halves pair up through
expiry-guard/mod/: the mod drops turn-<turn>.json when a turn ends; arm, which knows the session,
finds exactly one marker in the same folder within MOD_LINK_WINDOW of the last activity and writes
plan-<turn>.json (act MOD_EARLY before this job, and how); the mod then compacts in place
(open_sessions compact) or shows a notice with /compact in the prompt box (ask), and writes
done-<turn>.json. This job reads that first and stands down after an in-place compact. No pairing,
an older Claude Code, or a failure in the mod: the job forks as before.

HANDOFF AND PICKUP (0.21.2). After a fork the copy's compact summary is saved, owner-only, as
expiry-guard/handoff-<session>.md. When the person comes back after the cache lapsed and nothing
happened since, the backstop offers "/clear -- continue from the handoff" first and arms a one-shot
pickup for that Claude Code process (pid + start time); the SessionStart that /clear causes loads
the handoff into the fresh session (gt_surface -> take_pickup). Another terminal never gets it.

LAST RESORT, NO HOOK (0.21.2). If neither hook ever paired the mod with a plan, the mod plans for
itself at its last poll from vault-config.json, the context size Claude Code reports and the cache
TTL, and compacts at the same time the plan would have. See guard_mod.js.

COLD RESUME, START SMALL (0.21.2). A compaction after the cache lapsed pays a full cold write at the
price of the model that does it. So a cold compaction (the job waking late, after a sleep) and the
backstop's offer for an open session use a ladder: the cheapest model whose context window holds the
session with headroom (gt_model_prices.json `ladder`, then the session's main model), moving up one
step when a step fails. A warm compaction stays on the model whose cache is warm: it reads at 0.1x,
already cheap, and another model would miss that cache.

PRICES. A session can run its last turn on a small model (a skill with model_intent fast) while the
rest of it runs on the main one. `measure` returns both: `model` (the last reply's, whose cache is
warm) and `main_model` (the most frequent of the last MAIN_WINDOW replies). A cost describing what the
NEXT turn pays uses main_model; the warm compaction uses model.

SAFETY. A session whose transcript changed since arming is never touched. The model and effort are
always the session's own (a different model misses the cache and costs more). Nothing is deleted.
Every automatic action is a line in guard.log. The headless run starts with every hook disabled and
GT_EXPIRY_GUARD_RUNNING=1, so it cannot arm, cancel or block itself.

WHY THE JSON `usage` CANNOT PROVE THE CACHE READ. A /compact call's `usage` reads 0 and the
transcript keeps no usage row for it. The proof is `modelUsage`: cumulative per session, so this
call's cache read is the result's figure minus the last `cost-state` row of the transcript.

SCHEDULING. macOS: a per-session launchd agent with a StartCalendarInterval (Month/Day/Hour/Minute),
bootstrapped by launchctl and removed by the job itself and by cancel; gt_schedule.py is built for
daily/weekly recurring jobs with a fixed table, a one-shot per session does not fit it, so only its
sandbox-safe runner and its recorded interpreter (the one macOS lets read ~/.claude) are reused.
Linux: `systemd-run --user --on-active`, else `at`. Windows: a ONCE task in Task Scheduler.

"OPEN". A live Claude Code process holds the session if the process that ran the arming hook is
still alive with the same start time, or any claude command line names the session id. When the
process table cannot be read the session counts as open: the safe side is a fork, never a write.

Exit codes: always 0 for the hooks (they fail open and silent); status/cancel 0.
"""
import argparse
import collections
import datetime
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GT_VERSION = "0.22.0"

# Defaults, in step with gt_settings.SETTINGS (a test pins that they agree). They are repeated here
# because a hook must not import gt_settings: it caches CONFIG at import and costs time.
DEFAULTS = {"mode": "warn-only", "closed": "compact", "open": "compact", "backstop": "on",
            "min_tokens": 150000, "margin_min": "auto", "notify": "desktop", "pickup": "on"}
CHOICES = {"mode": ("auto", "warn-only", "off"), "closed": ("compact", "notify", "off"),
           "open": ("compact", "ask", "clear", "fork", "notify", "off"), "backstop": ("on", "off"),
           "notify": ("desktop", "none"), "pickup": ("on", "off")}
KEYS = {"mode": "expiry_guard", "closed": "expiry_guard.closed_sessions",
        "open": "expiry_guard.open_sessions", "backstop": "expiry_guard.resume_backstop",
        "min_tokens": "expiry_guard.min_tokens", "margin_min": "expiry_guard.margin_min",
        "notify": "expiry_guard.notify", "pickup": "expiry_guard.handoff_pickup"}

TTL_5M, TTL_1H = 300, 3600
DEFAULT_TTL = TTL_1H                       # a subscription's default, used only when nothing recorded
MARGIN_AUTO_MIN = {TTL_5M: 5, TTL_1H: 10}
MIN_AFTER_IDLE = 150                       # never fire sooner than this after the last reply
RESEND_WINDOW = 120                        # a blocked prompt sent again within this goes through
LOCK_STALE = 900
WRITE_MULT = {TTL_5M: 1.25, TTL_1H: 2.0}   # cache write price / input price, by TTL
READ_MULT = 0.1
EFFORTS = ("low", "medium", "high", "xhigh", "max")
LABEL_PREFIX = "io.goldenthread.gt-expiry-"
KEEP_DAYS = 7
CUT_COMMANDS = ("/compact", "/clear", "/gt:gt-minimize", "/gt-minimize")
# The in-session half (hooks/guard_mod.js; gt supports Claude Code 2.1.292+) and how the two pair up (0.21.2).
MOD_LINK_WINDOW = 30                       # a turn marker this close to the last activity is the turn
MOD_EARLY = 120                            # the mod acts this long before the background job would
MOD_DONE_FRESH = 900                       # a claim or result the mod wrote this recently stands
MAIN_WINDOW = 50                           # replies looked at to find the session's main model
LOG_MAX_BYTES = 256 * 1024                 # guard.log is cut to its newest half past this
COLD_FIT = 0.8                             # a cold compaction needs the context under this share of the window
COLD_BACKGROUND_MAX_USD = 0.25             # the late job pays a cold compaction only up to this estimate
CHEAP_MIN_SAVING = 0.2                     # the backstop offers the small-model route if it saves this share
PICKUP_TTL = 6 * 3600                      # a handoff offered for /clear stays on offer this long
PICKUP_MAX_CHARS = 40000                   # what SessionStart injects; the full text stays on disk
COMPACT_INSTRUCTIONS = ("Summarise in at most 6,000 tokens. Keep: the task in progress and its next step, "
                        "decisions made and why, file paths and names touched, commands and settings that "
                        "worked, open items and questions, and where the handoff is saved. "
                        "Drop: tool output already acted on, repeated file dumps, dead ends.")


# ---- clock, paths -----------------------------------------------------------------------------

def clock():
    """GT_EXPIRY_GUARD_NOW (tests) or the wall clock."""
    v = os.environ.get("GT_EXPIRY_GUARD_NOW")
    try:
        return float(v) if v else time.time()
    except ValueError:
        return time.time()


def home():
    return os.path.expanduser("~")


def base_dir():
    return os.path.join(home(), ".claude", "golden-thread", "expiry-guard")


def state_path(sid):
    return os.path.join(base_dir(), "%s.json" % safe_id(sid))


def block_path(sid):
    return os.path.join(base_dir(), "%s.blocked" % safe_id(sid))


def log_path():
    return os.path.join(base_dir(), "guard.log")


def mod_dir():
    return os.path.join(base_dir(), "mod")


def handoff_path(sid):
    return os.path.join(base_dir(), "handoff-%s.md" % safe_id(sid))


def pickup_path(pid, start):
    key = hashlib.sha256(("%s|%s" % (pid, start)).encode("utf-8")).hexdigest()[:16]
    return os.path.join(base_dir(), "pickup-%s.json" % key)


def private_dirs():
    """The guard's folders owner-only, like Claude Code's own projects folder: a handoff and the
    mod's files hold what the transcript held, and a mod cannot set a file's mode itself."""
    for d in (base_dir(), mod_dir()):
        try:
            os.makedirs(d, exist_ok=True)
            os.chmod(d, 0o700)
        except OSError:
            pass


def write_private(path, text):
    """atomic_write, then owner-only: a handoff holds what the transcript held."""
    private_dirs()
    atomic_write(path, text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def safe_id(sid):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(sid))[:80]


def short(sid):
    return str(sid)[:8]


def label_for(sid):
    return LABEL_PREFIX + safe_id(sid)[:12]


def atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def log(event, sid="", **kw):
    """One JSON line per automatic action. Ids are shortened; no prompt text, no file content."""
    try:
        row = dict({"ts": datetime.datetime.fromtimestamp(clock(), datetime.timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"), "event": event, "session": short(sid)}, **kw)
        os.makedirs(base_dir(), exist_ok=True)
        _trim_log()
        with open(log_path(), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass


def _trim_log():
    """Keep guard.log small: past LOG_MAX_BYTES, drop the oldest half (whole lines)."""
    try:
        if os.path.getsize(log_path()) <= LOG_MAX_BYTES:
            return
        with open(log_path(), "rb") as fh:
            fh.seek(-LOG_MAX_BYTES // 2, os.SEEK_END)
            tail = fh.read().split(b"\n", 1)[-1]
        atomic_write(log_path(), tail.decode("utf-8", "replace"))
    except OSError:
        pass


def _last_log_for(sid, tail_bytes=16384):
    """-> the newest guard.log row for this session in the file's tail, or None."""
    try:
        with open(log_path(), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - tail_bytes))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("session") == short(sid):
            return row
    return None


# ---- settings ---------------------------------------------------------------------------------

def settings():
    """-> the eight settings, from vault-config.json (read directly: it is a hook). Anything
    unreadable or invalid falls back to the registered default."""
    cfg = read_json(os.path.join(home(), ".claude", "vault-config.json")) or {}
    out = dict(DEFAULTS)
    for k, name in KEYS.items():
        v = cfg.get(name)
        if not isinstance(v, str):
            continue
        v = v.strip().lower()
        if k in CHOICES:
            if v in CHOICES[k]:
                out[k] = v
        elif k == "min_tokens":
            if v.isdigit() and int(v) >= 1:
                out[k] = int(v)
        elif k == "margin_min":
            if v == "auto" or (v.isdigit() and int(v) >= 0):
                out[k] = v
    return out


def ledger_on():
    cfg = read_json(os.path.join(home(), ".claude", "vault-config.json")) or {}
    return str(cfg.get("savings_ledger") or "on").strip().lower() != "off"


# ---- running things (every outside effect goes through one of these, so tests can stub it) ----

def sandboxed():
    return bool(os.environ.get("GT_TEST_SANDBOX"))


class _Refused:
    returncode, stdout = 126, ""

    def __init__(self, what):
        self.stderr = "refused: %s from a test/sandbox (GT_TEST_SANDBOX set)" % what


def run_cmd(args, cwd=None, env=None, timeout=600):
    """Run claude or a notifier. A sandbox/test never starts either."""
    if sandboxed():
        return _Refused(" ".join(str(a) for a in args[:2]))
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, cwd=cwd, env=env,
                          timeout=timeout)


def _schedule_module():
    for p in (HERE, os.path.join(home(), ".claude", "golden-thread", "hooks")):
        if os.path.isfile(os.path.join(p, "gt_schedule.py")):
            if p not in sys.path:
                sys.path.insert(0, p)
            try:
                import gt_schedule
                return gt_schedule
            except Exception:                                   # noqa: BLE001
                return None
    return None


def sched_run(args, timeout=60, input=None, **_kw):
    """The scheduler runner. gt_schedule's refuses every state-changing scheduler verb under
    GT_TEST_SANDBOX; mine says the same for the verbs it adds (systemd-run, at, atrm)."""
    if sandboxed():
        return _Refused(" ".join(str(a) for a in args[:3]))
    gs = _schedule_module()
    if gs is not None:
        return gs.run(args, timeout=timeout, input=input)
    try:
        return subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout,
                              input=input)
    except (OSError, subprocess.SubprocessError) as exc:
        return _Refused(str(exc))


def have(tool):
    return shutil.which(tool) is not None


def platform_kind():
    forced = os.environ.get("GT_EXPIRY_GUARD_PLATFORM")
    if forced in ("macos", "linux", "windows"):
        return forced
    if os.name == "nt":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def interpreter():
    gs = _schedule_module()
    try:
        return gs.interpreter() if gs else (sys.executable or "python3")
    except Exception:                                           # noqa: BLE001
        return sys.executable or "python3"


def find_claude():
    p = shutil.which("claude")
    if p:
        return p
    for c in (os.path.join(home(), ".local", "bin", "claude"), "/usr/local/bin/claude",
              "/opt/homebrew/bin/claude"):
        if os.path.isfile(c):
            return c
    return "claude"


def notify(title, body, **_kw):
    """A desktop notification, best effort. macOS osascript; Linux notify-send; Windows a balloon."""
    kind = platform_kind()
    try:
        if kind == "macos":
            script = 'display notification %s with title %s' % (_osa(body), _osa(title))
            run_cmd(["osascript", "-e", script], timeout=15)
        elif kind == "linux":
            if have("notify-send"):
                run_cmd(["notify-send", title, body], timeout=15)
        else:
            ps = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object "
                  "System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; "
                  "$n.Visible = $true; $n.ShowBalloonTip(15000, '%s', '%s', 'Info'); Start-Sleep 16"
                  % (title.replace("'", "''"), body.replace("'", "''")))
            run_cmd(["powershell", "-NoProfile", "-Command", ps], timeout=30)
    except Exception:                                           # noqa: BLE001
        pass


def _osa(s):
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def say(title, body, cfg):
    if cfg.get("notify") == "desktop":
        notify(title, body)


# ---- processes: is the session open? -----------------------------------------------------------

def ps_table():
    """-> [{pid, ppid, cmd}] or None when the process table cannot be read (Windows, no ps)."""
    if os.name == "nt" or not shutil.which("ps"):
        return None
    try:
        r = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True,
                           timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    out = []
    for line in r.stdout.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)$", line)
        if m:
            out.append({"pid": int(m.group(1)), "ppid": int(m.group(2)), "cmd": m.group(3)})
    return out


def pid_start(pid):
    """The start time string of a pid (identifies it across pid reuse), or None."""
    if os.name == "nt":
        return None
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    s = r.stdout.strip()
    return s if r.returncode == 0 and s else None


def is_claude(cmd):
    toks = str(cmd).split()
    if not toks:
        return False
    exe = os.path.basename(toks[0]).lower()
    if exe in ("claude", "claude.exe"):
        return True
    return exe.startswith("node") and any("claude-code" in t for t in toks[1:3])


def claude_ancestor(table=None):
    """-> (pid, start, binary) of the Claude Code process above this hook, or (None, None, None)."""
    table = ps_table() if table is None else table
    if not table:
        return None, None, None
    by = {p["pid"]: p for p in table}
    pid, seen = os.getpid(), set()
    while pid in by and pid not in seen:
        seen.add(pid)
        p = by[pid]
        if is_claude(p["cmd"]):
            exe = p["cmd"].split()[0]
            return p["pid"], pid_start(p["pid"]), exe if os.path.isabs(exe) else None
        pid = p["ppid"]
    return None, None, None


def session_open(rec):
    """-> ("open" | "closed", why). Unknown counts as open: the safe side is a fork."""
    table = ps_table()
    if table is None:
        return "open", "process table unreadable"
    pid = rec.get("claude_pid")
    if pid:
        for p in table:
            if p["pid"] == pid and is_claude(p["cmd"]):
                start = rec.get("claude_start")
                if not start or pid_start(pid) == start:
                    return "open", "pid %s is alive" % pid
    sid = rec.get("session_id") or ""
    for p in table:
        if sid and sid in p["cmd"] and is_claude(p["cmd"]):
            return "open", "a claude command line names the session"
    return "closed", "no live claude process holds it"


# ---- the transcript ----------------------------------------------------------------------------

def _ts(raw):
    try:
        return datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _n(d, k):
    try:
        return max(0, int(d.get(k) or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def _main_model(recent, last):
    """The model most of the session runs on: the most frequent of `recent`, a tie going to the
    pricier one (the safe side for a cost estimate). `last` when nothing was recorded."""
    counts = collections.Counter(recent)
    if not counts:
        return last
    top = max(counts.values())
    return max((m for m, c in counts.items() if c == top), key=lambda m: (price_for(m), m == last))


def measure(path):
    """-> {context, model, main_model, effort, ttl, last_activity, sig, compacted_after, cost_state} or None.

    context = input + cache read + cache write of the LAST main-chain usage row. `model` is the last
    reply's; `main_model` is what the session mostly runs on (a one-off turn on a small model, such
    as a `model_intent fast` skill, must not price the next turn). ttl is read from
    the newest row that records which cache the write went to (1h or 5m). The signature covers
    message rows only -- Claude Code appends title/mode/last-prompt rows while idle, and those must
    not count as the session changing."""
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except (OSError, TypeError):
        return None
    last_a = None                                        # (ts, context, model, effort, uuid)
    recent = collections.deque(maxlen=MAIN_WINDOW)       # models of the newest main-chain replies
    ttl = None
    n_msg, last_msg, last_msg_ts, compact_ts, cost = 0, "", None, None, None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue                                   # a half-written last line
            if not isinstance(row, dict):
                continue
            t = row.get("type")
            ts = _ts(row.get("timestamp"))
            if t == "cost-state":
                cost = row
                continue
            if t == "system" and (row.get("subtype") == "compact_boundary" or row.get("compactMetadata")):
                compact_ts = ts
                continue
            if t in ("user", "assistant") and not row.get("isSidechain"):
                if not row.get("isCompactSummary"):
                    n_msg += 1
                    last_msg = str(row.get("uuid") or "")
                    last_msg_ts = ts or last_msg_ts
            if t != "assistant" or row.get("isSidechain"):
                continue
            msg = row.get("message") if isinstance(row.get("message"), dict) else {}
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            model = str(msg.get("model") or "")
            if not usage or model.startswith("<"):
                continue
            ctx = _n(usage, "input_tokens") + _n(usage, "cache_read_input_tokens") \
                + _n(usage, "cache_creation_input_tokens")
            cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
            if _n(cc, "ephemeral_1h_input_tokens"):
                ttl = TTL_1H
            elif _n(cc, "ephemeral_5m_input_tokens"):
                ttl = TTL_5M
            last_a = (ts, ctx, model, str(row.get("effort") or ""), str(row.get("uuid") or ""))
            recent.append(model)
    if last_a is None:
        return None
    if ttl is None:
        v = (os.environ.get("CLAUDE_CODE_PROMPT_CACHE_TTL") or "").strip().lower()
        ttl = TTL_5M if v in ("5m", "300", "5") else DEFAULT_TTL
    ts, ctx, model, effort, uuid = last_a
    last = ts
    if last is None:
        try:
            last = os.path.getmtime(path)
        except OSError:
            last = None
    compacted = bool(compact_ts and (ts is None or compact_ts >= ts))
    if compacted:
        ctx, last = 0, max(compact_ts, last or 0)
    return {"context": ctx, "model": model, "main_model": _main_model(recent, model), "effort": effort,
            "ttl": ttl, "last_activity": last,
            "compacted_after": compacted,
            "sig": "%d:%s:%s" % (n_msg, last_msg, int(last_msg_ts or 0)), "cost_state": cost}


# ---- pricing, ledger ---------------------------------------------------------------------------

def _price_dirs():
    dirs = [HERE, os.path.join(home(), ".claude", "golden-thread", "hooks")]
    cache = os.path.join(home(), ".claude", "plugins", "cache", "golden-thread-plugin", "gt")
    try:
        dirs += [os.path.join(cache, v, "scripts") for v in sorted(os.listdir(cache), reverse=True)]
    except OSError:
        pass
    return dirs


def _price_doc():
    for d in _price_dirs():
        doc = read_json(os.path.join(d, "gt_model_prices.json"))
        if doc and isinstance(doc.get("prices"), list):
            return doc
    return {}


def _prices():
    doc = _price_doc()
    try:
        return [(str(n), float(p)) for n, p in doc["prices"]], float(doc.get("default", 3.0))
    except (KeyError, TypeError, ValueError):
        return [("opus", 5.0), ("haiku", 1.0), ("sonnet", 3.0)], 3.0


def window_for(model):
    """-> (context window in tokens, verified) for a model id, from gt_model_prices.json `windows`
    (first substring match wins). An unknown model gets the conservative default, unverified."""
    doc, m = _price_doc(), (model or "").lower()
    for row in doc.get("windows") or []:
        try:
            if str(row[0]).lower() in m:
                return int(row[1]), bool(row[2]) if len(row) > 2 else False
        except (TypeError, ValueError, IndexError):
            continue
    try:
        return int(doc.get("default_window") or 200000), False
    except (TypeError, ValueError):
        return 200000, False


def fits_window(model, ctx):
    """Can a cold /compact of `ctx` tokens run on this model? The request carries the context plus
    the compaction prompt and its summary, so it must sit well inside the window."""
    return ctx <= window_for(model)[0] * COLD_FIT


def model_ladder(main_model, ctx):
    """-> the models a cold compaction tries, cheapest first, ending with the session's main model.
    Only models that are cheaper than the main one and whose window holds the context take a place:
    start small, move up a step on failure, and the main model is always the last resort."""
    doc, steps = _price_doc(), []
    for name in doc.get("ladder") or []:
        name = str(name)
        if name in steps or name == main_model:
            continue
        if price_for(name) < price_for(main_model) and fits_window(name, ctx):
            steps.append(name)
    steps.sort(key=price_for)
    return steps + [main_model]


def price_for(model):
    """USD per million input tokens, list price; an unknown model gets the table's default."""
    table, default = _prices()
    m = (model or "").lower()
    for needle, price in table:
        if needle in m:
            return price
    return default


def rebuild_usd(ctx, ttl, model):
    """What the cold rebuild of `ctx` tokens costs at list price (cache write at 1.25x / 2.0x)."""
    return ctx / 1e6 * price_for(model) * WRITE_MULT.get(ttl, 2.0)


def opaque(s):
    return hashlib.sha256(str(s).encode("utf-8", "replace")).hexdigest()[:8]


def record_event(rec, action, now, warm=True, extra=None):
    """Append an `expiry-guard` event to the usage ledger gt_savings reads. Estimated: a cut saves
    (cold rebuild - warm compaction); a warning, a notification or a backstop claims nothing."""
    if not ledger_on():
        return
    ctx, ttl = int(rec.get("context") or 0), int(rec.get("ttl") or DEFAULT_TTL)
    cold = ctx * WRITE_MULT.get(ttl, 2.0)
    cut = action in ("compact", "fork", "inplace") and warm
    ev = {"v": 1, "gt": GT_VERSION, "mechanism": "expiry-guard", "state": "estimated", "action": action,
          "key": "expiry-guard:%s:%d:%s" % (short(rec.get("session_id")), int(now), action),
          "session": short(rec.get("session_id")),
          "project": opaque(os.path.basename(os.path.dirname(rec.get("transcript_path") or ""))),
          "model": rec.get("model"), "main_model": rec.get("main_model") or rec.get("model"),
          "context": ctx, "ttl": ttl,
          "actual_eq": round(ctx * READ_MULT) if cut else round(cold), "counterfactual_eq": round(cold),
          "ts": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    ev.update(extra or {})
    try:
        d = os.path.join(home(), ".claude", "golden-thread", "usage")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "savings.jsonl"), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(ev, sort_keys=True, separators=(",", ":")) + "\n")
    except OSError:
        pass


# ---- scheduling --------------------------------------------------------------------------------

def fire_command(sid):
    return [interpreter(), os.path.abspath(__file__), "fire", sid]


def plist_path(sid):
    return os.path.join(home(), "Library", "LaunchAgents", label_for(sid) + ".plist")


def _uid_domain():
    return "gui/%d" % os.getuid() if hasattr(os, "getuid") else "gui/0"


def schedule_once(rec, fire_at):
    """Register a one-shot job for `fire_at`. -> (ok, why). Records scheduler handles in rec."""
    sid, kind = rec["session_id"], platform_kind()
    cmd = fire_command(sid)
    lt = time.localtime(fire_at)
    if kind == "macos":
        path = plist_path(sid)
        doc = {"Label": label_for(sid), "ProgramArguments": cmd, "RunAtLoad": False,
               "StartCalendarInterval": {"Month": lt.tm_mon, "Day": lt.tm_mday, "Hour": lt.tm_hour,
                                         "Minute": lt.tm_min},
               "StandardOutPath": os.path.join(base_dir(), "job.out"),
               "StandardErrorPath": os.path.join(base_dir(), "job.err"),
               "EnvironmentVariables": {"PATH": ":".join(filter(None, [
                   os.path.dirname(rec.get("claude_bin") or ""),
                   "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"]))}}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.makedirs(base_dir(), exist_ok=True)
            with open(path, "wb") as fh:
                plistlib.dump(doc, fh)
        except OSError as exc:
            return False, "could not write the launchd job (%s)" % exc
        r = sched_run(["launchctl", "bootstrap", _uid_domain(), path])
        if r.returncode != 0:
            _rm(path)
            return False, "launchctl refused it: %s" % _why(r)
        rec["job"] = {"kind": "launchd", "label": label_for(sid), "plist": path}
        return True, ""
    if kind == "windows":
        name = label_for(sid)
        tr = " ".join('"%s"' % c for c in cmd)
        r = sched_run(["schtasks", "/Create", "/TN", name, "/SC", "ONCE",
                       "/ST", time.strftime("%H:%M", lt), "/SD", time.strftime("%m/%d/%Y", lt),
                       "/TR", tr, "/F"])
        if r.returncode != 0:
            return False, "schtasks refused it: %s" % _why(r)
        rec["job"] = {"kind": "schtasks", "label": name}
        return True, ""
    delay = max(1, int(fire_at - clock()))
    if have("systemd-run") and have("systemctl"):
        unit = "gt-expiry-" + safe_id(sid)[:12]
        r = sched_run(["systemd-run", "--user", "--unit", unit, "--on-active=%ds" % delay,
                       "--timer-property=AccuracySec=1s", "--"] + cmd)
        if r.returncode == 0:
            rec["job"] = {"kind": "systemd", "unit": unit}
            return True, ""
        why = "systemd-run refused it: %s" % _why(r)
    else:
        why = "no systemd-run"
    if have("at"):
        stamp = time.strftime("%Y%m%d%H%M", lt)
        # `at` reads the job from stdin and answers "job N at ..." on stderr
        r = sched_run(["at", "-t", stamp], input=" ".join(shlex.quote(c) for c in cmd) + "\n")
        m = re.search(r"job\s+(\d+)", (r.stderr or "") + (r.stdout or ""))
        if r.returncode == 0 and m:
            rec["job"] = {"kind": "at"}
            rec["at_job"] = m.group(1)
            return True, ""
        why += "; at refused it: %s" % _why(r)
    return False, why


def unschedule(rec):
    """Remove a session's one-shot job. Never raises. The plist goes first: a launchd job that
    boots itself out ends its own process."""
    job = rec.get("job") or {}
    sid, kind = rec.get("session_id") or "", job.get("kind")
    try:
        if kind == "launchd":
            _rm(job.get("plist") or plist_path(sid))
            sched_run(["launchctl", "bootout", "%s/%s" % (_uid_domain(), job.get("label") or label_for(sid))])
        elif kind == "schtasks":
            sched_run(["schtasks", "/Delete", "/TN", job.get("label") or label_for(sid), "/F"])
        elif kind == "systemd":
            for suffix in (".timer", ".service"):
                sched_run(["systemctl", "--user", "stop", job["unit"] + suffix])
        elif kind == "at" and rec.get("at_job"):
            sched_run(["atrm", str(rec["at_job"])])
    except Exception:                                           # noqa: BLE001
        pass


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _why(r):
    return ((getattr(r, "stderr", "") or "").strip() or (getattr(r, "stdout", "") or "").strip()
            or "exit %s" % getattr(r, "returncode", "?"))[:200]


# ---- state records -----------------------------------------------------------------------------

def load_state(sid):
    return read_json(state_path(sid))


def save_state(rec):
    atomic_write(state_path(rec["session_id"]), json.dumps(rec, indent=1, sort_keys=True))


def prune(now):
    """Forget finished records older than a week and unload jobs nothing is armed for."""
    try:
        names = os.listdir(base_dir())
    except OSError:
        return
    for n in names:
        if not n.endswith(".json"):
            continue
        rec = read_json(os.path.join(base_dir(), n))
        if not rec:
            continue
        if rec.get("status") != "armed" and now - float(rec.get("armed_at") or 0) > KEEP_DAYS * 86400:
            _rm(os.path.join(base_dir(), n))
            _rm(os.path.join(base_dir(), n[:-5] + ".blocked"))
            _rm(handoff_path(rec.get("session_id") or n[:-5]))
    # The mod's pairing files after a day, a pickup after PICKUP_TTL: by the time written inside
    # each file (the guard's clock), else by the file's age on the wall clock.
    for d, prefix, keep in ((mod_dir(), "", 86400), (base_dir(), "pickup-", PICKUP_TTL)):
        try:
            names = [n for n in os.listdir(d) if n.startswith(prefix) and n.endswith(".json")]
        except OSError:
            continue
        for n in names:
            f = os.path.join(d, n)
            j = read_json(f) or {}
            try:
                age = now - float(j.get("written_at") or j.get("at"))
            except (TypeError, ValueError):
                try:
                    age = time.time() - os.path.getmtime(f)
                except OSError:
                    continue
            if age > keep:
                _rm(f)


# ---- arm ---------------------------------------------------------------------------------------

def margin_seconds(cfg, ttl):
    m = cfg.get("margin_min")
    mins = MARGIN_AUTO_MIN.get(ttl, 10) if m == "auto" else int(m)
    # A margin as long as a 5 minute TTL would fire at the last reply, before the idle hook has even
    # run. Cap it so the job fires at least MIN_AFTER_IDLE seconds after the last reply.
    return max(0, min(mins * 60, ttl - MIN_AFTER_IDLE))


# Outcomes that are not worth a log line: the guard's own run, and the second arm of one idle period.
QUIET_WHY = ("the guard's own run", "already armed for this idle period")


def trigger_of(p):
    """-> "idle" for the idle_prompt notification, "stop" for the Stop hook (or a payload that says
    neither: the Stop route is the general one), "other" for any other notification type."""
    nt = p.get("notification_type")
    if nt == "idle_prompt":
        return "idle"
    if nt:
        return "other"
    return "stop"


BUSY_KEEP_S = 86400                         # bg-<session>.json older than this is removed


def mark_busy(p, now):
    """The Stop hook's word on background work, for guard_mod.js's auto_compact (0.21.4).

    Claude Code's Stop payload carries `background_tasks`: the in-flight background work of THIS
    session (running/pending agents, shells, monitors), an empty list when there is none. It is the
    one verifiable source -- the mod's own guesses at task fields were never seen live -- so the
    mod cuts only on a marker written this turn that says known: true, running: 0. A payload with
    no such field (a Claude Code that does not send it) writes known: false, which the mod treats
    as "cannot tell" and never cuts on. One file per session id, never per folder: several sessions
    run in one folder. Runs whatever expiry_guard is set to; never raises."""
    if trigger_of(p) != "stop" or p.get("notification_type"):
        return None
    sid = str(p.get("session_id") or "")
    if not sid:
        return None
    tasks = p.get("background_tasks")
    known = isinstance(tasks, list)
    running = [t for t in tasks if isinstance(t, dict)] if known else []
    rec = {"v": 1, "session": sid, "cwd": p.get("cwd") or "", "at": now, "known": known,
           "running": len(running) if known else None,
           "kinds": sorted({str(t.get("type") or "?")[:20] for t in running})[:6]}
    try:
        private_dirs()
        atomic_write(os.path.join(mod_dir(), "bg-%s.json" % safe_id(sid)), json.dumps(rec))
        for n in os.listdir(mod_dir()):
            if n.startswith("bg-") and n.endswith(".json"):
                f = os.path.join(mod_dir(), n)
                try:
                    if now - os.path.getmtime(f) > BUSY_KEEP_S:
                        os.unlink(f)
                except OSError:
                    pass
    except OSError:
        return None
    return rec


def arm(payload, now=None):
    """The Stop and idle_prompt hooks. -> {armed, why, ...}. Never raises. Every outcome that did
    not arm is one `arm-skipped` line in guard.log (the reason and which hook), bar the two quiet
    ones, so a session that never armed can be explained afterwards. Stop also writes the busy
    marker first (mark_busy), whatever the guard's mode."""
    now = clock() if now is None else now
    p = payload or {}
    if not os.environ.get("GT_EXPIRY_GUARD_RUNNING"):
        try:
            mark_busy(p, now)
        except Exception:                                       # noqa: BLE001
            pass
    try:
        r = _arm(p, now)
    except Exception as exc:                                    # noqa: BLE001
        log("arm-error", p.get("session_id", ""), error=type(exc).__name__)
        return {"armed": False, "why": "error: %s" % type(exc).__name__}
    if not r.get("armed") and r.get("why") not in QUIET_WHY and not str(r.get("why", "")).startswith(
            "could not schedule"):                              # schedule-failed already says why
        _log_skip(p, r)
    return r


def _log_skip(p, r):
    sid, why, trig = str(p.get("session_id") or ""), r.get("why", ""), trigger_of(p)
    prev = _last_log_for(sid) if sid else None
    if prev and prev.get("event") == "arm-skipped" and prev.get("why") == why:
        return                              # the same decline again (every small-session turn): once
    extra = {k: r[k] for k in ("context", "min_tokens") if k in r}
    log("arm-skipped", sid, why=why, trigger=trig, **extra)


def _arm(p, now):
    if os.environ.get("GT_EXPIRY_GUARD_RUNNING"):
        return {"armed": False, "why": "the guard's own run"}
    nt = p.get("notification_type")
    if nt and nt != "idle_prompt":
        return {"armed": False, "why": "not an idle_prompt"}
    sid, path = str(p.get("session_id") or ""), p.get("transcript_path")
    cfg = settings()
    if cfg["mode"] == "off":
        return {"armed": False, "why": "off"}
    if not sid or not path:
        return {"armed": False, "why": "no session or transcript"}
    m = measure(path)
    if not m or m["last_activity"] is None:
        return {"armed": False, "why": "no transcript"}
    if m["compacted_after"]:
        return {"armed": False, "why": "compacted-after", "context": m["context"]}
    if m["context"] < cfg["min_tokens"]:
        return {"armed": False, "why": "below-threshold", "context": m["context"],
                "min_tokens": cfg["min_tokens"]}
    last, ttl = m["last_activity"], m["ttl"]
    old = load_state(sid)
    if old and old.get("status") == "armed" and old.get("sig") == m["sig"]:
        # The other hook armed this idle period already. What it may have missed is the pairing with
        # the in-session mod: at Stop the mod's turn marker may not be written yet, so the first
        # arm cannot pair; this one (about 60 s later, when the marker exists) can.
        if not old.get("mod_turn") and cfg["mode"] == "auto" and cfg["open"] in ("compact", "ask", "clear") \
                and old.get("claude_pid") and link_mod(old, cfg, m, now):
            save_state(old)
            log("mod-linked", sid, mod=short(old["mod_turn"]), late=True)
        return {"armed": False, "why": "already armed for this idle period"}
    if old and old.get("status") == "armed":
        unschedule(old)                                          # the session moved on since
    if now >= last + ttl:
        return {"armed": False, "why": "the cache has already lapsed"}
    fire_at = max(last + ttl - margin_seconds(cfg, ttl), now + 20)
    if fire_at >= last + ttl:
        return {"armed": False, "why": "too late to cut"}
    pid, start, binary = claude_ancestor()
    rec = {"session_id": sid, "transcript_path": path, "cwd": p.get("cwd") or "", "model": m["model"],
           "main_model": m["main_model"], "effort": m["effort"], "ttl": ttl, "context": m["context"], "sig": m["sig"],
           "last_activity": last, "armed_at": now, "fire_at": fire_at, "status": "armed",
           "claude_pid": pid, "claude_start": start,
           "claude_bin": binary or find_claude(),
           "ttl_env": os.environ.get("CLAUDE_CODE_PROMPT_CACHE_TTL") or ""}
    ok, why = schedule_once(rec, fire_at)
    if not ok:
        log("schedule-failed", sid, why=why)
        return {"armed": False, "why": "could not schedule: " + why}
    if cfg["mode"] == "auto" and cfg["open"] in ("compact", "ask", "clear") and pid:
        link_mod(rec, cfg, m, now)
        if not rec.get("mod_turn") and trigger_of(p) == "stop" and _id_mods_running():
            _await_own_marker(rec, cfg, m, now)
    save_state(rec)
    prune(now)
    log("armed", sid, fire_at=int(fire_at), context=m["context"], ttl=ttl, mode=cfg["mode"],
        trigger=trigger_of(p), mod=short(rec.get("mod_turn") or ""))
    return {"armed": True, "fire_at": fire_at, "why": ""}


STOP_LINK_WAIT = 3.0                        # seconds the Stop arm waits for the mod's turn marker


def _id_mods_running():
    """True when some turn marker carries a session id: mods that can be paired exactly run here."""
    try:
        names = [n for n in os.listdir(mod_dir()) if n.startswith("turn-") and n.endswith(".json")]
    except OSError:
        return False
    return any((read_json(os.path.join(mod_dir(), n)) or {}).get("session") for n in names[-50:])


def _await_own_marker(rec, cfg, m, now):
    """The Stop hook runs about a second before the mod's turn.complete writes its marker
    (seen live 2026-10-13: armed 17:25:13, marker 17:25:14). Wait briefly and pair, so pairing
    never depends on Claude Code's one-shot idle notification."""
    import time
    end = time.monotonic() + STOP_LINK_WAIT
    while not rec.get("mod_turn") and time.monotonic() < end:
        time.sleep(0.25)
        link_mod(rec, cfg, m, now)


def _same_dir(a, b):
    try:
        return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)
    except (OSError, ValueError):
        return False


def link_mod(rec, cfg, m, now):
    """Pair this idle period with the in-session mod's turn marker and write it a plan (0.21.2).

    Claude Code 2.1.296 gives a mod its session id, and its marker carries it: then only a marker
    with THIS session's id pairs (the newest one within MOD_LINK_WINDOW of the last activity).
    A marker without an id (an older Claude Code) pairs the old way, by folder and time, and only
    when no marker in the window carries an id: that rule once paired a session with another
    session's turn in the same folder (2026-10-13), so it is the fallback, never the first try.
    Exactly one candidate, or nothing is linked and the background job acts alone."""
    try:
        names = os.listdir(mod_dir())
    except OSError:
        return None
    private_dirs()
    hits, own, with_id = [], [], 0
    for n in names:
        if not (n.startswith("turn-") and n.endswith(".json")):
            continue
        t = read_json(os.path.join(mod_dir(), n))
        if not t or not t.get("turn"):
            continue
        try:
            at = float(t.get("at") or 0)
        except (TypeError, ValueError):
            continue
        if t.get("session"):
            if str(t["session"]) == rec["session_id"]:
                own.append((at, str(t["turn"])))
            if abs(at - m["last_activity"]) <= MOD_LINK_WINDOW:
                with_id += 1
            continue
        if abs(at - m["last_activity"]) <= MOD_LINK_WINDOW and _same_dir(t.get("cwd"), rec.get("cwd")):
            hits.append(str(t["turn"]))
    if own:
        at, turn = max(own)
        hits = [turn] if abs(at - m["last_activity"]) <= MOD_LINK_WINDOW else []
    elif with_id:
        hits = []                                # id-carrying mods are running: never guess by folder
    if len(hits) != 1:
        if hits:
            log("mod-ambiguous", rec["session_id"], candidates=len(hits))
        return None
    turn = hits[0]
    expires = m["last_activity"] + rec["ttl"]
    plan = {"turn": turn, "action": "compact" if cfg["open"] == "clear" else cfg["open"], "written_at": now,
            "fire_at": max(now + 5, rec["fire_at"] - MOD_EARLY), "expires_at": expires,
            "context": m["context"], "name": _name(rec), "handoff": handoff_path(rec["session_id"]),
            "instructions": COMPACT_INSTRUCTIONS}
    if cfg["open"] == "clear":
        # open_sessions clear (0.22.0): the mod captures a handoff, arms the pickup named here just before it runs
        # /clear, and the SessionStart that /clear causes loads the handoff (take_pickup). A clear is never plain:
        # strict means no handoff, no clear.
        plan.update(kind="clear", handoffMode="facts", strict=True)
        pid, start, _ = claude_ancestor()
        if pid and cfg["pickup"] != "off":
            plan["pickup_file"] = pickup_path(pid, start)
    atomic_write(os.path.join(mod_dir(), "plan-%s.json" % safe_id(turn)), json.dumps(plan))
    rec["mod_turn"] = turn
    return turn


def mod_result(rec):
    """-> the mod's done record for this idle period, if it wrote one after arming, else None."""
    turn = rec.get("mod_turn")
    if not turn:
        return None
    d = read_json(os.path.join(mod_dir(), "done-%s.json" % safe_id(turn)))
    try:
        if d and float(d.get("at") or 0) >= float(rec.get("armed_at") or 0):
            return d
    except (TypeError, ValueError):
        pass
    return None


def withdraw_plan(rec):
    if rec and rec.get("mod_turn"):
        _rm(os.path.join(mod_dir(), "plan-%s.json" % safe_id(rec["mod_turn"])))


# ---- fire --------------------------------------------------------------------------------------

def _cost_baseline(m, model):
    cs = (m or {}).get("cost_state") or {}
    use = (cs.get("modelUsage") or {}).get(model) or {}
    return {"read": _n(use, "cacheReadInputTokens"), "write": _n(use, "cacheCreationInputTokens"),
            "cost": cs.get("totalCostUSD") if isinstance(cs.get("totalCostUSD"), (int, float)) else None,
            "known": bool(use)}


def _parse_result(out):
    for text in (out, (out or "").strip().splitlines()[-1] if (out or "").strip() else ""):
        try:
            d = json.loads(text)
            if isinstance(d, dict):
                return d
        except ValueError:
            continue
    return None


def headless_compact(rec, m, fork, model=None):
    """Run `claude -p --resume <id> [--fork-session] "/compact ..."` in the session's cwd.
    -> dict(ok, session_id, cache_read, warm, cost_usd, why).

    The model is `rec["model"]` unless a cold ladder step names another. Why the WARM compaction
    uses rec["model"], the LAST reply's model, and not the main model that prices the next turn:
    a prompt cache belongs to one model, and the last reply is what warmed it. Compacting on that
    model reads the context at 0.1x; any other model would miss the cache and pay a full write.
    Effort is passed only on the session's own model (a ladder step may not support it)."""
    model = model or rec["model"]
    args = [rec.get("claude_bin") or find_claude(), "-p", "--model", model,
            "--output-format", "json", "--settings", '{"disableAllHooks": true}', "--resume",
            rec["session_id"]]
    if fork:
        args.append("--fork-session")
    if rec.get("effort") in EFFORTS and model == rec["model"]:
        args += ["--effort", rec["effort"]]
    args.append("/compact " + COMPACT_INSTRUCTIONS)
    env = dict(os.environ, GT_EXPIRY_GUARD_RUNNING="1")
    if rec.get("ttl_env"):
        env["CLAUDE_CODE_PROMPT_CACHE_TTL"] = rec["ttl_env"]
    if rec.get("claude_bin"):
        env["PATH"] = os.path.dirname(rec["claude_bin"]) + os.pathsep + env.get("PATH", "")
    try:
        r = run_cmd(args, cwd=rec.get("cwd") or None, env=env, timeout=900)
    except Exception as exc:                                    # noqa: BLE001
        return {"ok": False, "why": "could not start claude (%s)" % type(exc).__name__}
    doc = _parse_result(r.stdout)
    if r.returncode != 0 or not doc or doc.get("is_error") or doc.get("local_command") != "compact":
        return {"ok": False, "why": "claude did not run /compact headless: %s"
                % (_why(r) if r.returncode != 0 else "unexpected result")}
    new = str(doc.get("session_id") or "")
    if fork and (not new or new == rec["session_id"]):
        return {"ok": False, "why": "the fork did not get a new session id"}
    after = (doc.get("modelUsage") or {}).get(model) or {}
    base = _cost_baseline(m, model)
    out = {"ok": True, "session_id": new, "cache_read": None, "warm": None, "cost_usd": None, "why": ""}
    if base["known"] and after:
        # modelUsage is cumulative per session; a fork starts from its own total
        read, cost_before = _n(after, "cacheReadInputTokens"), base["cost"]
        out["cache_read"] = read - base["read"] if read >= base["read"] else read
        out["warm"] = out["cache_read"] > 0
        if isinstance(doc.get("total_cost_usd"), (int, float)) and cost_before is not None:
            out["cost_usd"] = round(doc["total_cost_usd"] - cost_before, 6) \
                if doc["total_cost_usd"] >= cost_before else doc["total_cost_usd"]
    return out


def cold_compact(rec, m, fork, steps):
    """Compact a session whose cache has lapsed, trying `steps` (model ids, cheapest first) and
    moving up one on a failure: an error, a context the model refused, or no summary written.
    -> the last headless_compact result plus `model` (the step that worked, or the last tried) and
    `tried` (every step run, in order)."""
    tried, res = [], {"ok": False, "why": "no model to try"}
    for model in steps:
        tried.append(model)
        res = headless_compact(rec, m, fork, model=model)
        if res.get("ok") and not _summary_written(rec, res, fork):
            res = {"ok": False, "why": "%s wrote no summary" % model}
        if res.get("ok"):
            break
        log("cold-step-failed", rec["session_id"], model=model, why=str(res.get("why"))[:120])
    return dict(res, model=tried[-1] if tried else "", tried=tried)


def _summary_written(rec, res, fork):
    """Did the compaction leave a summary? A fork keeps it in its own transcript; an in-place run
    shows as a compaction after the last reply in the session's."""
    if fork:
        path = os.path.join(os.path.dirname(rec.get("transcript_path") or ""),
                            "%s.jsonl" % res.get("session_id"))
        return bool(compact_summary(path))
    again = measure(rec["transcript_path"])
    return bool(again and again["compacted_after"])


def _lock(sid):
    p = os.path.join(base_dir(), "%s.lock" % safe_id(sid))
    os.makedirs(base_dir(), exist_ok=True)
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        os.close(fd)
        return p
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(p) > LOCK_STALE:
                os.utime(p)
                return p
        except OSError:
            pass
        return None


def fire(sid, now=None):
    """The one-shot job. -> {action, why, ...}. Never raises."""
    now = clock() if now is None else now
    lock = _lock(sid)
    if lock is None:
        return {"action": "skip", "why": "busy"}
    try:
        return _fire(sid, now)
    except Exception as exc:                                    # noqa: BLE001
        log("fire-error", sid, error=type(exc).__name__)
        return {"action": "skip", "why": "error: %s" % type(exc).__name__}
    finally:
        _rm(lock)


def _finish(rec, status, action, why, **extra):
    rec["status"] = status
    rec["finished_at"] = clock()
    rec.update({k: v for k, v in extra.items() if k in ("fork_id", "cache_read")})
    withdraw_plan(rec)
    save_state(rec)
    unschedule(rec)                                              # last: a launchd job ends itself here
    return dict({"action": action, "why": why}, **extra)


def _skip(rec, status, why):
    log("skip", rec["session_id"], why=why)
    return _finish(rec, status, "skip", why)


def _name(rec):
    return os.path.basename((rec.get("cwd") or "").rstrip("/\\")) or "session"


def _k(n):
    return "%dK" % round(n / 1000)


def _fire(sid, now):
    rec = load_state(sid)
    if not rec or rec.get("status") != "armed":
        return {"action": "skip", "why": "not-armed"}
    cfg = settings()
    if cfg["mode"] == "off":
        return _skip(rec, "off", "off")
    done = mod_result(rec)
    if done and done.get("status") == "compacted":
        try:
            os.chmod(handoff_path(sid), 0o600)
        except OSError:
            pass
        log("inplace", sid, before=done.get("before"), after=done.get("after"))
        record_event(rec, "inplace", now)
        return _finish(rec, "inplace", "inplace", "the session compacted itself")
    if done and done.get("status") == "compacting" and now - float(done.get("at") or 0) < MOD_DONE_FRESH:
        return _skip(rec, "inplace", "the session is compacting itself")
    m = measure(rec["transcript_path"])
    if not m:
        return _skip(rec, "gone", "no-transcript")
    if m["sig"] != rec.get("sig"):
        return _skip(rec, "changed", "changed")
    if m["last_activity"] is None or now >= m["last_activity"] + rec["ttl"]:
        return _cold_fire(rec, m, cfg, now)
    if m["compacted_after"] or m["context"] < cfg["min_tokens"]:
        return _skip(rec, "below", "below-threshold")
    rec["context"] = m["context"]
    left = max(0, int((m["last_activity"] + rec["ttl"] - now) // 60))
    usd = rebuild_usd(m["context"], rec["ttl"], m["main_model"])        # what the NEXT turn would pay
    name = _name(rec)

    if cfg["mode"] == "warn-only":
        say("Prompt cache about to expire",
            "%s: %s tokens, the cache lapses in about %d min. Run /compact now to avoid a rebuild "
            "of roughly $%.2f." % (name, _k(m["context"]), left, usd), cfg)
        record_event(rec, "warn", now)
        log("warn", sid, context=m["context"], minutes_left=left)
        return _finish(rec, "warned", "warn", "warn-only")

    state, why_open = session_open(rec)
    policy = cfg["open"] if state == "open" else cfg["closed"]
    if state == "open" and policy in ("compact", "ask", "clear"):
        policy = "fork"            # in place is the mod's job; this is the copy for /clear to pick up
    log("decide", sid, state=state, why=why_open, policy=policy)
    if policy == "off":
        return _finish(rec, "none", "none", "policy-off")
    if policy == "notify":
        say("Prompt cache about to expire",
            "%s: %s tokens, the cache lapses in about %d min. Run /compact now to avoid a rebuild "
            "of roughly $%.2f." % (name, _k(m["context"]), left, usd), cfg)
        record_event(rec, "notify", now)
        return _finish(rec, "notified", "notify", "policy-notify")

    fork = policy == "fork"
    again = measure(rec["transcript_path"])
    if not again or again["sig"] != rec["sig"]:                   # last look before touching it
        return _skip(rec, "changed", "changed")
    res = headless_compact(rec, m, fork)
    if not res["ok"]:
        say("Cache guard could not compact", "%s: %s. Nothing was changed; run /compact yourself."
            % (name, res["why"]), cfg)
        record_event(rec, "notify", now, extra={"failed": True})
        log("compact-failed", sid, why=res["why"])
        return _finish(rec, "failed", "notify", "compact-failed")
    warm = res["warm"] is not False
    extra = {"cache_read": res["cache_read"]}
    if fork:
        say("Compacted copy ready",
            "A compacted copy of %s is ready: claude -r %s. The open session was not touched."
            % (name, res["session_id"]), cfg)
    else:
        say("Session compacted while the cache was warm",
            "%s (%s tokens) was compacted. Resume as usual: claude -r %s"
            % (name, _k(m["context"]), sid), cfg)
    record_event(rec, "fork" if fork else "compact", now, warm=warm,
                 extra={"cache_read": res["cache_read"], "cost_usd": res["cost_usd"]})
    log("fork" if fork else "compact", sid, cache_read=res["cache_read"], warm=res["warm"],
        cost_usd=res["cost_usd"], new_session=short(res["session_id"]) if fork else "")
    if fork:
        extra["fork_id"] = res["session_id"]
        h = write_fork_handoff(rec, res["session_id"], now)
        if h:
            rec["handoff_path"], rec["handoff_at"] = h, now
    return _finish(rec, "forked" if fork else "cut", "fork" if fork else "compact", "",
                   warm=res["warm"], cost_usd=res["cost_usd"], **extra)


def _cold_fire(rec, m, cfg, now):
    """The job woke after the cache had lapsed (a laptop that slept through its fire time). A warm
    cut is gone; the choice left is a cold compaction, paid for at the price of the model that does
    it. Only for a closed session under `auto` with closed_sessions compact, only when the cheapest
    step on the ladder is estimated under COLD_BACKGROUND_MAX_USD (nobody is waiting for this
    session, so it is not worth a large bill); everything else stays the old "expired" skip."""
    if m["last_activity"] is None or cfg["mode"] != "auto" or cfg["closed"] != "compact" \
            or m["compacted_after"] or m["context"] < cfg["min_tokens"]:
        return _skip(rec, "expired", "expired")
    state, _why = session_open(rec)
    steps = model_ladder(m["main_model"], m["context"])
    est = rebuild_usd(m["context"], rec["ttl"], steps[0])
    if state != "closed" or est > COLD_BACKGROUND_MAX_USD:
        return _skip(rec, "expired", "expired")
    again = measure(rec["transcript_path"])
    if not again or again["sig"] != rec["sig"]:
        return _skip(rec, "changed", "changed")
    res = cold_compact(rec, m, False, steps)
    name = _name(rec)
    if not res["ok"]:
        log("compact-failed", rec["session_id"], why=res["why"], tried=res["tried"], cold=True)
        return _finish(rec, "failed", "skip", "expired")
    record_event(rec, "compact", now, warm=False,
                 extra={"cold": True, "ladder": res["tried"], "compact_model": res["model"],
                        "cost_usd": res["cost_usd"], "est_usd": round(rebuild_usd(
                            m["context"], rec["ttl"], res["model"]), 4)})
    log("compact", rec["session_id"], cold=True, model=res["model"], tried=res["tried"],
        cost_usd=res["cost_usd"])
    say("Session compacted after its cache lapsed",
        "%s (%s tokens) was compacted on %s. Resume as usual: claude -r %s"
        % (name, _k(m["context"]), res["model"], rec["session_id"]), cfg)
    return _finish(rec, "cut", "compact", "cold", warm=False, cost_usd=res["cost_usd"])


# ---- handoff and pickup (0.21.2) ----------------------------------------------------------------

def compact_summary(path):
    """-> the text of the LAST compact summary in a transcript, or ''."""
    out = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not (isinstance(row, dict) and row.get("isCompactSummary")):
                    continue
                c = (row.get("message") or {}).get("content")
                if isinstance(c, str):
                    out = c
                elif isinstance(c, list):
                    out = "\n".join(x.get("text") or "" for x in c if isinstance(x, dict))
    except (OSError, TypeError):
        return ""
    return out.strip()


def write_fork_handoff(rec, fork_id, now):
    """Save the fork's compact summary as this session's handoff. -> its path, or None."""
    path = os.path.join(os.path.dirname(rec.get("transcript_path") or ""), "%s.jsonl" % fork_id)
    text = compact_summary(path)
    if not text:
        log("handoff-missing", rec["session_id"], fork=short(fork_id))
        return None
    when = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
    head = ("# Handoff: %s\n\nWritten %s by the gt expiry guard from a compacted copy of the session, "
            "while its prompt cache was warm.\nThe whole copy can be resumed with: claude -r %s\n\n"
            % (_name(rec), when, fork_id))
    out = handoff_path(rec["session_id"])
    write_private(out, head + text + "\n")
    log("handoff", rec["session_id"], chars=len(text))
    return out


def handoff_on_offer(rec, m):
    """The handoff a returning person can /clear into, or None: it exists, and the session did
    nothing after it was written (otherwise it is behind)."""
    if not rec or not rec.get("handoff_path") or not os.path.isfile(rec["handoff_path"]):
        return None
    try:
        at = float(rec.get("handoff_at") or 0)
    except (TypeError, ValueError):
        return None
    if m.get("last_activity") is not None and m["last_activity"] > at + 5:
        return None
    return rec["handoff_path"]


def offer_pickup(sid, handoff, now):
    """Arm a one-shot pickup for THIS Claude Code process: after /clear, SessionStart loads it."""
    pid, start, _ = claude_ancestor()
    if not pid:
        return False
    atomic_write(pickup_path(pid, start), json.dumps({"handoff": handoff, "session_id": sid,
                                                       "at": now}))
    return True


def take_pickup(source):
    """SessionStart: -> (context text, terminal line) when this process has a pickup armed and the
    session was just cleared, else (None, None). One use: the pickup is removed. Never raises."""
    try:
        if source != "clear" or settings()["pickup"] == "off":
            return None, None
        pid, start, _ = claude_ancestor()
        if not pid:
            return None, None
        f = pickup_path(pid, start)
        d = read_json(f)
        if not d:
            return None, None
        _rm(f)
        if clock() - float(d.get("at") or 0) > PICKUP_TTL:
            return None, None
        path = d.get("handoff") or ""
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        log("pickup", d.get("session_id") or "", chars=len(text))
        cut = text[:PICKUP_MAX_CHARS]
        more = "" if len(text) <= PICKUP_MAX_CHARS else "\n\n[cut here; the whole handoff is %s]" % path
        ctx = ("This session continues one that was compacted while its prompt cache was warm. The "
               "handoff below is its record: treat the decisions in it as made and do not re-ask "
               "them; carry on from its next step. Full text: %s\n\n%s%s" % (path, cut, more))
        return ctx, "gt: continuing from the handoff saved before the cache lapsed (%s)" % path
    except Exception:                                           # noqa: BLE001
        return None, None


# ---- backstop ----------------------------------------------------------------------------------

def cancel(session=None):
    """Cancel an armed job (one session, or every one). -> how many were cancelled."""
    n = 0
    try:
        names = [safe_id(session) + ".json"] if session else sorted(os.listdir(base_dir()))
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".json"):
            continue
        rec = read_json(os.path.join(base_dir(), name))
        if rec and rec.get("status") == "armed":
            unschedule(rec)
            withdraw_plan(rec)
            rec["status"] = "cancelled"
            save_state(rec)
            log("cancelled", rec.get("session_id", ""))
            n += 1
    return n


def backstop(payload, now=None):
    """The UserPromptSubmit hook. Cancels an armed job, then maybe blocks the prompt once.
    -> None (let it through) or {"decision": "block", "reason": ...}. Never raises."""
    now = clock() if now is None else now
    try:
        return _backstop(payload or {}, now)
    except Exception as exc:                                    # noqa: BLE001
        log("backstop-error", (payload or {}).get("session_id", ""), error=type(exc).__name__)
        return None


def _cheap_route(m, main, main_usd):
    """-> the backstop's "start small" choice, or None. The context is rebuilt once either way; doing
    it on a cheaper model that fits makes that one rebuild cost less, and the summary that follows
    is small on the main model. Offered only when a cheaper model fits the context and saves at
    least CHEAP_MIN_SAVING of the estimate."""
    steps = [s for s in model_ladder(main, m["context"]) if s != main]
    if not steps:
        return None
    small = steps[0]
    small_usd = rebuild_usd(m["context"], m["ttl"], small)
    if main_usd <= 0 or small_usd > main_usd * (1 - CHEAP_MIN_SAVING):
        return None
    return ("/model %s, then /compact, then /model %s  -- the same rebuild on a cheaper model that "
            "holds this context: about $%.2f instead of $%.2f (estimated)"
            % (small, main, small_usd, main_usd))


def _backstop(p, now):
    if os.environ.get("GT_EXPIRY_GUARD_RUNNING"):
        return None
    sid = str(p.get("session_id") or "")
    if sid:
        cancel(sid)                                  # any new prompt: the idle period is over
    cfg = settings()
    if cfg["mode"] == "off" or cfg["backstop"] == "off" or not sid:
        return None
    text = str(p.get("prompt") or "").strip().lower()
    if any(text == c or text.startswith(c + " ") for c in CUT_COMMANDS):
        return None
    prior = read_json(block_path(sid))
    if prior and now - float(prior.get("at") or 0) <= RESEND_WINDOW:
        _rm(block_path(sid))
        log("backstop-passed", sid)
        return None
    m = measure(p.get("transcript_path"))
    if not m or m["last_activity"] is None or m["compacted_after"]:
        return None
    gap = now - m["last_activity"]
    if gap <= m["ttl"] or m["context"] < cfg["min_tokens"]:
        return None
    # The next prompt runs on the session's main model, not necessarily the last reply's (a skill on a
    # small model can end a long opus session): price the rebuild at the main one.
    main = m["main_model"]
    usd = rebuild_usd(m["context"], m["ttl"], main)
    handoff = handoff_on_offer(load_state(sid), m) if cfg["pickup"] == "on" else None
    if handoff and not offer_pickup(sid, handoff, now):
        handoff = None
    atomic_write(block_path(sid), json.dumps({"at": now, "context": m["context"]}))
    rec = {"session_id": sid, "transcript_path": p.get("transcript_path"), "model": m["model"],
           "main_model": main, "context": m["context"], "ttl": m["ttl"]}
    # The lapse cost this session a cold rebuild, and a warm handoff + compact would have cost the
    # cache-read price instead. Added fields only (actual_eq/counterfactual_eq keep claiming no
    # saving, so every existing reader is unchanged); gt_savings can sum `missed_eq` / `missed_usd`.
    warm_usd = m["context"] / 1e6 * price_for(m["model"]) * READ_MULT
    cold_eq = m["context"] * WRITE_MULT.get(m["ttl"], 2.0)
    record_event(rec, "backstop", now, extra={
        "missed": True, "lost_usd": round(usd, 4), "lost_eq": round(cold_eq),
        "warm_usd": round(warm_usd, 4), "missed_usd": round(max(0.0, usd - warm_usd), 4),
        "missed_eq": round(max(0.0, cold_eq - m["context"] * READ_MULT))})
    log("backstop-blocked", sid, context=m["context"], gap=int(gap))
    ttl_text = "1 hour" if m["ttl"] == TTL_1H else "5 minutes"
    choices = []
    if handoff:
        try:
            size = _k(os.path.getsize(handoff) / 4)
        except OSError:
            size = "a few K"
        choices.append("/clear  -- continue from the handoff gt saved before the cache lapsed "
                       "(about %s tokens, estimated); nothing is rebuilt" % size)
    cheap = _cheap_route(m, main, usd)
    choices += ["/compact  -- pay the rebuild once, then every later turn is small"]
    if cheap:
        choices.append(cheap)
    choices += ["/gt:gt-minimize, then /clear  -- keep the few things worth keeping and start fresh",
                "send the same prompt again within 2 minutes to continue as it is"]
    reason = (
        "gt expiry guard: this session holds %s tokens and its prompt cache lapsed %d min ago "
        "(cache lifetime %s). Sending this prompt now rebuilds the whole context, about $%.2f at "
        "list price (estimated). Your prompt was NOT sent. Choose:\n%s\n"
        "Turn this check off: gt_settings.py set expiry_guard.resume_backstop off"
        % (_k(m["context"]), max(1, round((gap - m["ttl"]) / 60)), ttl_text, usd,
           "\n".join("  %d. %s" % (i, c) for i, c in enumerate(choices, 1))))
    return {"decision": "block", "reason": reason}


# ---- status, cli -------------------------------------------------------------------------------

def status(as_json=False):
    cfg = settings()
    recs = []
    try:
        for n in sorted(os.listdir(base_dir())):
            if n.endswith(".json"):
                r = read_json(os.path.join(base_dir(), n))
                if r:
                    recs.append(r)
    except OSError:
        pass
    tail = []
    try:
        with open(log_path(), encoding="utf-8") as fh:
            tail = [x.rstrip() for x in fh.readlines()[-5:]]
    except OSError:
        pass
    if as_json:
        return json.dumps({"settings": cfg, "sessions": recs, "log": tail}, indent=1)
    lines = ["expiry guard: %s (closed sessions: %s, open: %s, backstop: %s, min tokens %d, margin %s, notify %s)"
             % (cfg["mode"], cfg["closed"], cfg["open"], cfg["backstop"], cfg["min_tokens"],
                cfg["margin_min"], cfg["notify"])]
    armed = [r for r in recs if r.get("status") == "armed"]
    lines.append("armed jobs: %d" % len(armed))
    for r in armed:
        lines.append("  %s  %s  fires %s  %sK tokens  ttl %ds" % (
            short(r["session_id"]), _name(r),
            datetime.datetime.fromtimestamp(r["fire_at"]).strftime("%H:%M:%S"),
            round(r["context"] / 1000), r["ttl"]))
    done = [r for r in recs if r.get("status") != "armed"]
    if done:
        lines.append("finished: " + ", ".join("%s=%s" % (short(r["session_id"]), r.get("status")) for r in done[-5:]))
    if tail:
        lines.append("last actions:")
        lines += ["  " + t for t in tail]
    return "\n".join(lines)


def read_payload():
    try:
        raw = sys.stdin.read()
        d = json.loads(raw) if raw.strip() else {}
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_expiry_guard.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    for name in ("arm", "backstop"):
        sp = sub.add_parser(name)
        sp.add_argument("--hook", action="store_true")
    sp = sub.add_parser("fire")
    sp.add_argument("session")
    sp = sub.add_parser("status")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("cancel")
    sp.add_argument("session", nargs="?")
    sp.add_argument("--all", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 0 if (argv and ("--hook" in argv)) else 2
    if a.cmd == "arm":
        arm(read_payload())
    elif a.cmd == "backstop":
        out = backstop(read_payload())
        if out:
            sys.stdout.write(json.dumps(out))
    elif a.cmd == "fire":
        r = fire(a.session)
        print("expiry guard: %s%s" % (r["action"], (" (%s)" % r["why"]) if r.get("why") else ""))
    elif a.cmd == "status":
        print(status(a.json))
    elif a.cmd == "cancel":
        n = cancel(None if a.all or not a.session else a.session)
        print("expiry guard: cancelled %d armed job(s)" % n)
    else:
        ap.print_help()
        return 2
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
    sys.exit(main())
