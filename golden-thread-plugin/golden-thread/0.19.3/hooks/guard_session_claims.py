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
if tool not in ("Write", "Edit", "NotebookEdit", "MultiEdit", "Bash"):
    no_objection()

ti = payload.get("tool_input") or {}
target = ti.get("file_path") or ti.get("notebook_path") or ""
if tool != "Bash" and not target:
    no_objection()

try:
    from gt_paths import find_vault
    vault = find_vault()
except Exception:
    no_objection()

# ---------------------------------------------------------------------------------------------
# QUEUE FIRST (Core rule 1, owner 2026-10-01: "the agents write to the queues rather than
# directly to the files"). Vault CONTENT -- every .md outside the few trees gt's own tools own --
# is written only through gt_write_queue.py and applied by gt_broker.py drain, which checks live
# claims, deduplicates, and escalates conflicts and every design.md / global-memory write to the
# owner. So a direct Write/Edit to vault content is denied OUTRIGHT, claimed or not; the claim
# check below still governs the files this does not cover.
#
# Bash is covered for the shapes a guard can read without guessing -- a redirect (`>`, `>>`),
# `tee`, `sed -i`, or `cp`/`mv` whose LAST argument is a vault .md. A script that opens a file
# itself (python open(...,'w')) is not visible here; the rule file says so. Anything this cannot
# parse draws no objection (fail open), like every other guard.
# ---------------------------------------------------------------------------------------------
import shlex  # noqa: E402

QUEUE_EXEMPT_PREFIXES = (".obsidian/", ".git/", ".gt/", ".trash/", "Sources/", "core-rules/",
                         "Projects/golden-thread/spool/", "Projects/golden-thread/sessions/",
                         "Projects/golden-thread/tools/")


def queue_governed(vault_root, path_str, cwd=None):
    """-> vault-relative path when `path_str` is vault content the queue owns, else None."""
    try:
        p = pathlib.Path(os.path.expanduser(path_str))
        if not p.is_absolute():
            p = pathlib.Path(cwd or os.getcwd()) / p
        # "/" on every platform: the prefixes below and the claims are written with "/".
        rel_q = p.resolve().relative_to(pathlib.Path(vault_root).resolve()).as_posix()
    except Exception:
        return None
    if not rel_q.endswith(".md"):
        return None
    if any(rel_q.startswith(x) for x in QUEUE_EXEMPT_PREFIXES):
        return None              # immutable/protected trees have their own guard; gt state is gt's
    return rel_q


_HEREDOC = __import__("re").compile(r"""<<-?\s*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\1""")
_SEPARATORS = {";", "&&", "||", "|", "&"}
_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}


def _strip_heredoc_bodies(cmd):
    """A heredoc body is data, not shell: `> x.md` inside one is text, never a redirect."""
    lines, out, i = cmd.split("\n"), [], 0
    while i < len(lines):
        out.append(lines[i])
        ends = [m.group(2) for m in _HEREDOC.finditer(lines[i])]
        i += 1
        for end in ends:
            while i < len(lines) and lines[i].strip() != end:
                i += 1
            i += 1                                  # the delimiter line itself
    return "\n".join(out)


def bash_write_targets(cmd, cwd=None):
    """Absolute paths a shell command visibly writes: redirects, tee, sed -i, cp/mv/install.

    0.19.1 (request queue-guard-bash-false-positives): read from shell TOKENS, not the raw
    string, so a quoted `>` is text; a target built from `$VAR` or a backtick is unknowable and
    draws no objection; and a `cd` is followed across `;`, `&&`, `||` and newlines -- after an
    unresolvable `cd`, every relative target is unknowable too. Anything that does not parse
    draws no objection, like every other guard.
    """
    try:
        lex = shlex.shlex(_strip_heredoc_bodies(cmd).replace("\n", " ; "), posix=True,
                          punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return []
    segments = [[]]
    for t in tokens:
        if t in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(t)

    here = cwd or os.getcwd()

    def resolve(word):
        if "$" in word or "`" in word:
            return None
        p = os.path.expanduser(word)
        # Native Windows (0.19.3): the Bash tool is Git Bash, whose own spelling of C:\x is
        # /c/x. Read literally that is \c\x on the current drive, so a vault write spelled
        # that way was never seen.
        if os.name == "nt" and re.match(r"^/[A-Za-z](/|$)", p):
            p = p[1].upper() + ":/" + p[3:]
        if os.path.isabs(p):
            return p
        return os.path.join(here, p) if here else None

    out = []
    for toks in segments:
        words, i = [], 0                            # the command with its redirections removed
        while i < len(toks):
            t = toks[i]
            if t in _REDIRECTS and i + 1 < len(toks):
                out.append(resolve(toks[i + 1]))
                i += 2
            elif t[:1] in ("<", ">"):               # input, heredoc, fd duplication (2>&1)
                i += 2
            else:
                words.append(t)
                i += 1
        if not words:
            continue
        head = os.path.basename(words[0])
        if head == "cd":
            arg = words[1] if len(words) > 1 else "~"
            here = None if arg == "-" else resolve(arg)
        elif head == "tee":
            out.extend(resolve(w) for w in words[1:] if not w.startswith("-"))
        elif head == "sed" and any(w.startswith("-i") for w in words[1:]):
            out.extend(resolve(w) for w in words[1:] if w.endswith(".md"))
        elif head in ("cp", "mv", "install") and len(words) >= 3:
            out.append(resolve(words[-1]))
    return [p for p in out if p]


def deny_queue(rel_q, how):
    q = f"{vault}/Projects/golden-thread/tools"
    reason = (
        f"BLOCKED by Core rule core_concurrent_session_claim (queue first).\n\n"
        f"  {rel_q}\n  is vault content, and vault content is written only through the write queue"
        f" ({how} was refused).\n\n"
        f"Queue the write instead, then apply the queue:\n"
        f"  python3 <gt scripts>/gt_write_queue.py --vault \"{vault}\" --path \"{rel_q}\" \\\n"
        f"      --op append|replace-section|create [--section \"<heading>\"] --content-file <file>\n"
        f"  python3 <gt scripts>/gt_broker.py drain --vault \"{vault}\"\n\n"
        f"The broker holds a write another live session has claimed, removes duplicates, and sends\n"
        f"conflicts and every design.md / global-memory write to the owner for review.\n"
        f"Generated files have their own tools: log.md -> {q}/gt_log.py add, decisions.md ->\n"
        f"{q}/gt_adr.py allocate, TASKS.md -> gt_tasks.py.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    sys.exit(0)


if vault:
    try:
        if tool == "Bash":
            cmd = ti.get("command") or ""
            for t in bash_write_targets(cmd, payload.get("cwd")):
                rq = queue_governed(vault, t, payload.get("cwd"))
                if rq:
                    deny_queue(rq, "a shell write")
            no_objection()          # Bash never reaches the claim check below
        rq = queue_governed(vault, target, payload.get("cwd"))
        if rq:
            deny_queue(rq, f"a direct {tool}")
    except SystemExit:
        raise
    except Exception:
        if tool == "Bash":
            no_objection()

# WHICH MACHINE, by id and not by name (0.17.2). This compared `host` to
# socket.gethostname() until the 2026-09-28 incident: a laptop moved onto a wired
# network, DHCP renamed it, and every claim recorded under the old name had its pid
# un-judged -- so a busy session whose heartbeat had aged past 30 minutes simply
# stopped being protected, and this guard said nothing (it CANNOT say anything
# except by denying). gt_paths.same_machine decides by the uuid in
# ~/.claude/golden-thread/machine-id; a gt_paths too old to have it, or an id that
# cannot be read, falls back to the old label comparison rather than failing open
# on every call.
try:
    from gt_paths import machine_id as _machine_id, same_machine as _same_machine
    MINE = _machine_id()[0]
except Exception:
    _same_machine, MINE = None, None
HOSTNAME = socket.gethostname()
# A pid identifies a PROCESS, not a session: one Claude Code process hosts successive
# sessions (2026-09-28, pid 91999 carried several), so a finished session's file that
# records OUR pid would look live for as long as we run. $CLAUDE_PID is shared by a
# session and its hooks, so here it names the process we are running inside.
MY_PID = (os.environ.get("CLAUDE_PID") or "").strip()

if not vault:
    no_objection()

try:
    tpath = pathlib.Path(target).resolve()
    vault = pathlib.Path(vault).resolve()
    rel = tpath.relative_to(vault)          # raises if outside the vault
except Exception:
    no_objection()                                  # not a vault file -> not our business

rel = rel.as_posix()          # claims name files with "/", on every platform (0.19.3)
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


def this_machine(fm):
    """-> (written on this machine?, rename/legacy notice or None)."""
    if _same_machine is None:
        return fm.get("host") == HOSTNAME, None
    return _same_machine(fm.get("machine", ""), fm.get("host", ""), MINE, HOSTNAME)


def _windows_pid_alive(pid):
    """Native Windows (0.19.3): os.kill(pid, 0) is NOT a probe there -- any signal but the two
    CTRL events is TerminateProcess, so the old liveness check killed the process it asked
    about. Ask the kernel instead: True running, False gone, None unknown."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        h = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return True if ctypes.get_last_error() == 5 else False   # 5 = access denied
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return None
            return code.value == 259                    # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None


def pid_alive(fm, sid):
    """True/False when knowable on this machine, else None."""
    if not this_machine(fm)[0]:
        return None
    raw = (fm.get("pid") or "").strip()
    if not raw.isdigit():
        return None
    if MY_PID and raw == MY_PID and me and sid != me:
        return None                           # our process, another session: see MY_PID
    if os.name == "nt":
        return _windows_pid_alive(int(raw))
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

        alive = pid_alive(fm, sid)
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
    # NO REGISTRATION, SAID OUT LOUD (owner request 2026-09-29). A session that released and
    # kept writing -- or never registered -- wrote unattributed and unprotected, and nothing
    # said so until the report card at session end. Warn, do not block: the write still lands
    # (recorded as unattributed), but the writer is told now, with the file and the command.
    # Silent when the session id is unknown -- "cannot tell" must not read as "unregistered".
    try:
        registered = any(
            parse(p)[0].get("session_id", p.stem.split("_")[0]) == me
            for p in sessions.glob("*.md") if p.name != "README.md") if me else True
    except Exception:
        registered = True
    if not registered:
        msg = ("gt: this session is NOT registered, so this write to %s is unattributed and "
               "no other session is warned off it. Run: python3 %s/Projects/golden-thread/tools/"
               "gt_session.py --vault \"%s\" register --task \"...\" and claim the file."
               % (rel, vault, vault))
        print(json.dumps({"systemMessage": msg,
                          "hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "additionalContext": msg}}))
    no_objection()

sid, fm, claimed_as = holder
# A deny is the only voice this guard has, so a renamed host (or an unprovable legacy
# file) is reported HERE -- silently correcting it is how the next person loses an
# afternoon working out why the label names a machine that no longer exists.
try:
    notice = this_machine(fm)[1]
except Exception:
    notice = None
reason = (
    f"BLOCKED by Core rule core_concurrent_session_claim.\n\n"
    f"  {rel}\n"
    f"  is claimed by another LIVE session:\n"
    f"    session : {sid}\n"
    f"    pid     : {fm.get('pid','?')} on {fm.get('host','?')}\n"
    + (f"    note    : this session {notice}\n" if notice else "")
    + f"    task    : {fm.get('task','(unstated)')}\n"
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
