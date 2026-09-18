#!/usr/bin/env python3
import json, os, re, socket, sys, time, datetime, pathlib

# FAIL OPEN STARTS AT LINE ONE. This was `HERE = sys.argv[1]`, outside any try, so invoking the
# hook with no argument raised IndexError and exited 1 -- a traceback, from a guard whose whole
# contract is that when it has nothing to say it says nothing and exits 0. A non-zero exit from
# a PreToolUse hook is not "no objection": it is a hook Claude Code reports as failing, on every
# tool call, and the first thing anyone does with a noisy guard is switch it off. The argument
# is the directory holding gt_paths.py; without it, fall back to this file's own directory,
# which is where install.sh puts both (found 2026-09-18).
HERE = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# NO OBJECTION = NO OUTPUT (2026-09-13). This hook used to print
# permissionDecision "allow" on every call it did not block. Claude Code's hooks
# reference: "allow" SKIPS the interactive permission prompt (only deny/ask rules still
# apply), while "exit 0 with no output" is no decision and the normal permission flow
# applies. Registered with no matcher, the guard runs on every tool call -- so gt was
# silently approving every tool call past the user's permission prompt. A guard only
# ever objects; when it has nothing to say it says nothing, and exits 0.


def no_objection():
    """Stay out of the permission decision: print nothing, exit 0 (fail open)."""
    sys.exit(0)


try:
    payload = json.load(sys.stdin)
except Exception:
    no_objection()

tool = payload.get("tool_name") or ""
if tool not in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
    no_objection()

ti = payload.get("tool_input") or {}
target = ti.get("file_path") or ti.get("notebook_path") or ""
if not target:
    no_objection()

try:
    from gt_paths import find_vault
    vault = find_vault()
except Exception:
    no_objection()

if not vault:
    no_objection()

try:
    tpath = pathlib.Path(target).resolve()
    vault = pathlib.Path(vault).resolve()
    rel = tpath.relative_to(vault)          # raises if outside the vault
except Exception:
    no_objection()                                  # not a vault file -> not our business

rel = str(rel)
sessions = vault / "Projects" / "golden-thread" / "sessions"
if not sessions.is_dir():
    no_objection()

me = ""
for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID"):
    if os.environ.get(var):
        me = os.environ[var].strip()
        break

STALE_MIN = 30.0
TS_FMT = "%Y-%m-%d %H:%M:%S %z"
# Zone names in heartbeats written before gt_session.py used a numeric offset.
# strptime discards %Z, so without this a claim from another zone looks hours old.
OLD_ZONES = {"UTC": 0, "GMT": 0, "Z": 0, "EST": -5, "EDT": -4, "CST": -6, "CDT": -5,
             "MST": -7, "MDT": -6, "PST": -8, "PDT": -7, "AKST": -9, "AKDT": -8,
             "HST": -10, "CET": 1, "CEST": 2, "JST": 9}


def parse(path):
    fm, body = {}, ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return fm, body
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
            body = text[end + 4:]
    return fm, body


def pid_alive(fm):
    """True/False when knowable on this host, else None."""
    if fm.get("host") != socket.gethostname():
        return None
    raw = (fm.get("pid") or "").strip()
    if not raw.isdigit():
        return None
    try:
        os.kill(int(raw), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def parse_ts(raw):
    raw = raw.strip()
    for f in (TS_FMT, "%Y-%m-%d %H:%M %z"):
        try:
            return datetime.datetime.strptime(raw, f)
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


def age_min(fm):
    raw = fm.get("last_execution")
    dt = parse_ts(raw) if raw else None
    if dt is None:
        return None
    return (datetime.datetime.now().astimezone() - dt).total_seconds() / 60.0


holder = None
try:
    for p in sorted(sessions.glob("*.md")):
        if p.name == "README.md":
            continue
        fm, body = parse(p)
        sid = fm.get("session_id", p.stem.split("_")[0])
        if me and sid == me:
            continue                          # my own claim never blocks me

        alive = pid_alive(fm)
        if alive is True:
            live = True
        elif alive is False:
            live = False                      # demonstrably dead -> ignore its claims
        else:
            a = age_min(fm)
            live = a is not None and a <= STALE_MIN
        if not live:
            continue

        claimed = re.findall(r"^-\s+`([^`]+)`", body, flags=re.M)
        for c in claimed:
            c = c.strip().rstrip("/")
            if not c:
                continue
            # exact file, or a claimed directory prefix
            if rel == c or rel.startswith(c + "/"):
                holder = (sid, fm, c)
                break
        if holder:
            break
except Exception:
    no_objection()

if not holder:
    no_objection()

sid, fm, claimed_as = holder
reason = (
    f"BLOCKED by Core rule core_concurrent_session_claim.\n\n"
    f"  {rel}\n"
    f"  is claimed by another LIVE session:\n"
    f"    session : {sid}\n"
    f"    pid     : {fm.get('pid','?')} on {fm.get('host','?')}\n"
    f"    task    : {fm.get('task','(unstated)')}\n"
    f"    claim   : {claimed_as}\n\n"
    f"One shared working tree means git will NOT warn you — a write here silently\n"
    f"destroys their uncommitted work.\n\n"
    f"Do this instead:\n"
    f"  1. Stage the change under Projects/golden-thread/pending/\n"
    f"     (a .patch for normal files, a .logline for append-only ones)\n"
    f"  -- log.md and decisions.md need no claim at all: they are generated.\n"
    f"     Use tools/gt_log.py add, or tools/gt_adr.py allocate <project>.\n"
    f"  2. Apply it once `gt_session.py list` shows the claim cleared.\n"
    f"If you believe that session is dead, confirm with `gt_session.py list`."
)

print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": reason,
}}))
