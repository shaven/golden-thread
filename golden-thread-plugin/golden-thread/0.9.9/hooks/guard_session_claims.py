#!/usr/bin/env python3
import json, os, re, socket, sys, datetime, pathlib

HERE = sys.argv[1]
sys.path.insert(0, HERE)

ALLOW = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}


def allow():
    print(json.dumps(ALLOW))
    sys.exit(0)


try:
    payload = json.load(sys.stdin)
except Exception:
    allow()

tool = payload.get("tool_name") or ""
if tool not in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
    allow()

ti = payload.get("tool_input") or {}
target = ti.get("file_path") or ti.get("notebook_path") or ""
if not target:
    allow()

try:
    from gt_paths import find_vault
    vault = find_vault()
except Exception:
    allow()

if not vault:
    allow()

try:
    tpath = pathlib.Path(target).resolve()
    vault = pathlib.Path(vault).resolve()
    rel = tpath.relative_to(vault)          # raises if outside the vault
except Exception:
    allow()                                  # not a vault file -> not our business

rel = str(rel)
sessions = vault / "Projects" / "golden-thread" / "sessions"
if not sessions.is_dir():
    allow()

me = ""
for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID"):
    if os.environ.get(var):
        me = os.environ[var].strip()
        break

STALE_MIN = 30.0
TS_FMT = "%Y-%m-%d %H:%M:%S %Z"


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


def age_min(fm):
    raw = fm.get("last_execution")
    if not raw:
        return None
    now = datetime.datetime.now().astimezone()
    for f in (TS_FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M %Z", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.datetime.strptime(raw.strip(), f)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=now.tzinfo)
            return (now - dt).total_seconds() / 60.0
        except ValueError:
            continue
    return None


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
    allow()

if not holder:
    allow()

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
    f"  2. Apply it once `gt_session.py list` shows the claim cleared.\n"
    f"If you believe that session is dead, confirm with `gt_session.py list`."
)

print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": reason,
}}))
