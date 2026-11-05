#!/usr/bin/env python3
"""gt_testing_doctor -- "why is my test run blocked?": name the gate, offer the one scoped fix (0.21.4).

    gt_testing_doctor.py [diagnose] [--project-dir DIR] [--json] [--transcript FILE]
                         [--projects-dir DIR] [--limit N]
    gt_testing_doctor.py fix add-dir DIR [--project-dir DIR] [--until 4h] [--dry-run] [--yes]
    gt_testing_doctor.py fix allow PREFIX [--project-dir DIR] [--until 4h] [--dry-run] [--yes]
    gt_testing_doctor.py fix accept-edits [--project-dir DIR] [--until 4h] [--dry-run] [--yes]
    gt_testing_doctor.py fix profile [--sandbox DIR] [--runners a,b] [--project-dir DIR] [--until 4h]
                         [--dry-run] [--yes]
    gt_testing_doctor.py off [--project-dir DIR] [--scope project|local|user]

Also reachable as `gt_doctor.py testing ...` (the same arguments).

WHY. An unattended test run stops, and the person running it often cannot tell WHICH of several
different gates fired: a permission prompt (no allow rule matched), the working-directory fence
(`blockReadsOutsideWorkingDirectories`), the permission mode, a Core-rule hook, or the model
provider refusing requests (a spend cap, a login that expired). They look alike -- "it stopped" --
and need different cures (request 2026-10-09-testing-blockers-doctor, owner: "lots of users are not
going to be technical enough to know what is wrong or how to fix it").

WHAT `diagnose` DOES (the default; read-only, writes nothing, needs no terminal). It reads the live
settings Claude Code merges for this project (user, project, local, and managed policy when present)
and the LAST DENIALS, PROMPTS AND API ERRORS in the project's own session transcripts, then prints,
for each blocker found: the gate by name, one plain sentence on what that gate protects, why it
fired here, and the one scoped fix with the exact command. Exit 0 nothing found, 1 at least one
blocker found, 2 usage.

WHERE THE LAST DENIAL IS (found on a live Claude Code 2.1.300 install, 2026-10-15; read-only). Claude
Code writes every session to ~/.claude/projects/<cwd with each non-alphanumeric character turned
into "-">/<session id>.jsonl, and a subagent's own run to <session id>/subagents/agent-<id>.jsonl.
One JSON object per line. The rows this reads:

    user row with a tool_result block         is_error text for a refusal, and a `permissionDecision`
                                              {decision accept|reject, reasonType rule|mode|hook|
                                              classifier|safetyCheck|..., source config|hook|user_*};
                                              a `user_*` source on an accept means a PROMPT was
                                              shown and answered; the tool call is in the assistant
                                              row it points back to (tool_use id)
    assistant row, isApiErrorMessage true     the run's final provider failure: `error` (rate_limit,
                                              authentication_failed, server_error, invalid_request),
                                              `apiErrorStatus`, and the message text
    system row, subtype api_error             one retried failure: error.status, error.formatted
    permission-mode row                       the mode the session ran in

Not verified live on this machine (no example existed in its transcripts): the exact refusal text of
the working-directory fence, of a deny rule, and a failed background agent's notice for a 402. They
are matched by wording that names /add-dir, the directory fence or `denied`, and a miss degrades to
"no gate named" -- never to a wrong fix. Nothing it reads is ever written back: a denied command is
shown only as its first few words with anything secret-looking replaced, and is never stored.

WHAT `fix` DOES. Each fix is ONE change to the project's .claude/settings.local.json (the personal,
uncommitted file; this project only), made by gt_testing_profile.apply_fix -- the profile's own
checks, backup, state record and `off`, not a copy of them. It prints the diff, needs a person at a
terminal (gt_tty.has_terminal, the same guard a grant uses), asks one yes (or --yes), and asks gt
unlock for a fresh confirmation when unlock is on.

    add-dir DIR      DIR becomes a working folder (permissions.additionalDirectories). Never
                     blockReadsOutsideWorkingDirectories: that fence protects every project.
    allow PREFIX     one runner rule, Bash(PREFIX:*), judged by the profile's runner checks
    accept-edits     permissions.defaultMode acceptEdits, only from default / unset; the value it
                     replaced is recorded and put back
    profile          the whole testing profile (gt_testing_profile.py on) into the local file

REFUSED, by name and for every fix: a folder or project that is, is inside or contains the vault
(accept-edits: contains too), your home folder, ~/.claude and the credential, login and start-up
folders; a runner that is ssh, rm, curl, a bare shell and the rest of the profile's refusal list.
Never touched: blockReadsOutsideWorkingDirectories, hooks, the write queue, the secrets checks, the
explicit-vault-target check -- the Core-rule gates. When one of THEM is what blocked you, this tells
you so and offers no fix.

`off` is the teardown: it takes out every entry the profile and these fixes added to this project's
.claude/settings.json and .claude/settings.local.json, puts defaultMode back, and prints the diff;
a file that did not exist is gone again and an untouched one is restored byte for byte.

Stdlib only. Exit: 0 ok / nothing found, 1 a blocker found or a fix refused, 2 usage.
"""
import sys
# A published tree must keep matching its checksums: write no bytecode, here or in what this imports
# (the same rule gt_doctor.py keeps).
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import textwrap  # noqa: E402
import time  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_sandbox as sb  # noqa: E402  (settings paths, managed policy, JSON helpers)
import gt_testing_profile as sp  # noqa: E402  (the profile: checks, state, apply_fix, off)

TAIL_BYTES = 6 * 1024 * 1024          # the end of a transcript is where the last denial is
MAX_FILES = 8                         # newest transcripts read (main sessions and subagents)
WINDOW = 60                           # newest events considered
WIDTH = 78

READ_TOOLS = ("Read", "Glob", "Grep", "NotebookRead", "LS")
# Tools whose "prompt" is a question put to the person, not a permission check.
ASKS_BY_DESIGN = ("AskUserQuestion", "ExitPlanMode", "EnterPlanMode")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
LOOSER_MODES = ("acceptEdits", "bypassPermissions", "auto", "dontAsk")

# A refusal naming the working-directory fence. Matched by wording; see "Not verified live" above.
FENCE_TEXT = re.compile(
    r"(/add-dir|outside (of )?(the |your |this )?(current |project |allowed |additional )?working "
    r"director|not (in|within) (an? |the |your )?(allowed |additional )?(working )?director|"
    r"blockReadsOutsideWorkingDirectories)", re.I)
CORE_BLOCK = re.compile(r"hook error: BLOCKED by Core rule (\w+)")
DENY_TEXT = re.compile(r"(permission to use .{0,80}? (has been |was )?denied|"
                       r"denied by (a |your )?(permission )?(rule|setting)|"
                       r"is denied by your permission settings)", re.I)

SECRET_OPTS = ("-u", "--user", "-p", "--password", "--passwd", "-H", "--header", "--token",
               "--api-key", "--apikey", "--auth", "--cookie", "-b", "-e", "--env")
_SECRET_TOKEN = re.compile(
    r"(?i)^(sk|ghp|gho|ghu|ghs|ghr|xox[abpr]|AKIA)[-_A-Za-z0-9]|"
    r"(password|passwd|secret|token|apikey|api_key|auth|key)=|://[^/@\s]+:[^@\s]+@|"
    r"^[^/\s:=]{1,40}:[^/\s]{4,}$|^bearer$")


# --------------------------------------------------------------------- showing things safely
def safe_shape(command, limit=7):
    """A denied command as the first few words, with anything secret-looking replaced. The
    command may carry a credential, so this is all that is ever printed or put in --json, and
    nothing is stored."""
    try:
        toks = shlex.split(command or "")
    except ValueError:
        toks = (command or "").split()
    out, skip = [], False
    for t in toks[:limit]:
        if skip:
            out.append("<hidden>")
            skip = False
        elif t in SECRET_OPTS:
            out.append(t)
            skip = True
        elif re.search(r"\s", t):
            out.append('"..."')            # a quoted argument may be a whole program or a secret
        elif _SECRET_TOKEN.search(t) or (len(t) >= 32 and "/" not in t) \
                or re.match(r"^-p.{4,}$", t):
            out.append("<hidden>")
        else:
            out.append(t if len(t) <= 80 else t[:77] + "...")
    if len(toks) > limit:
        out.append("...")
    return " ".join(out)


def host_of(url):
    """Only the host of a URL: never a path, a query or credentials."""
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^/@\s]*@)?([^/:?#\s]+)", url or "")
    return m.group(1) if m else None


def wrap(text, indent="", width=WIDTH, first=None):
    return textwrap.fill(text, width=width, initial_indent=first if first is not None else indent,
                         subsequent_indent=indent, break_long_words=False,
                         break_on_hyphens=False)


def short_path(p, h=None):
    home = sb.home(h)
    p = os.path.abspath(p)
    if p == home or p.startswith(home.rstrip(os.sep) + os.sep):
        return "~" + p[len(home):].replace(os.sep, "/")
    return p


def self_command(h=None):
    return "python3 %s" % _quote_path(os.path.join(HERE, "gt_testing_doctor.py"), h)


def _quote_path(path, h=None):
    sp_ = short_path(path, h)
    if sp_.startswith("~/"):
        return "~/" + shlex.quote(sp_[2:]) if re.search(r"[^\w@%+=:,./-]", sp_[2:]) else sp_
    return shlex.quote(sp_)


# --------------------------------------------------------------------- the live configuration
def _load(path):
    try:
        with open(path, "rb") as fh:
            d = json.loads(fh.read().decode("utf-8"))
    except OSError:
        return None, None
    except ValueError as e:
        return None, "not valid JSON (%s)" % e
    return (d, None) if isinstance(d, dict) else (None, "not a JSON object")


def _truthy(v):
    return v is True or (isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "on"))


def load_config(project_dir=None, h=None, managed=None):
    """The settings Claude Code merges for a session started in this project.
    -> {"root", "docs": [{scope, path, doc, error}], ...}. `managed`: a managed-settings
    `permissions` dict to use instead of reading the machine's (tests)."""
    root = sp.project_root(project_dir)
    cands = [("user", sb.settings_path(h)),
             ("project", os.path.join(root, ".claude", "settings.json")),
             ("local", sp.settings_for("local", root, h)),
             ("local", os.path.join(root, ".claude", "settings.local.json"))]
    docs, seen = [], set()
    for scope, path in cands:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        d, err = _load(path)
        docs.append({"scope": scope, "path": path, "doc": d, "error": err})
    if managed is None:
        try:
            managed = sb.managed_settings().get("permissions") or {}
        except Exception:                                  # noqa: BLE001 -- policy unreadable: skip
            managed = {}
    if managed:
        mp = sb.managed_paths()[0]
        docs.append({"scope": "managed", "path": mp, "doc": {"permissions": managed},
                     "error": None})
    return {"root": root, "docs": docs}


PRECEDENCE = ("managed", "local", "project", "user")        # highest first


def _perms(entry):
    d = entry.get("doc")
    p = d.get("permissions") if isinstance(d, dict) else None
    return p if isinstance(p, dict) else {}


def effective(cfg, key, top_level_too=False):
    """-> (value, scope) of a scalar setting from the highest scope that sets it, or (None, None)."""
    for scope in PRECEDENCE:
        for e in cfg["docs"]:
            if e["scope"] != scope or not isinstance(e.get("doc"), dict):
                continue
            p = _perms(e)
            if key in p:
                return p[key], scope
            if top_level_too and key in e["doc"]:
                return e["doc"][key], scope
    return None, None


def rules_of(cfg, kind):
    """[(rule, scope)] of permissions.<kind> across the scopes."""
    out = []
    for e in cfg["docs"]:
        lst = _perms(e).get(kind)
        if isinstance(lst, list):
            out += [(r, e["scope"]) for r in lst if isinstance(r, str)]
    return out


def working_set(cfg, h=None):
    """The project folder and every additionalDirectories entry, as real paths."""
    roots = [os.path.realpath(cfg["root"])]
    for e in cfg["docs"]:
        lst = _perms(e).get("additionalDirectories")
        if not isinstance(lst, list):
            continue
        anchor = os.path.dirname(os.path.dirname(e["path"]))
        for x in lst:
            p = sb._entry_path(x, "dir", anchor, cfg["root"], h)
            if p:
                roots.append(os.path.realpath(p))
    return roots


def inside_any(path, roots):
    import gt_io_policy as iop
    real = os.path.realpath(path)
    return any(iop._inside(real, r) for r in roots)


def base_url(cfg):
    """-> (host or None, where): Claude Code's provider endpoint when it is not Anthropic's own."""
    for scope in PRECEDENCE:
        for e in cfg["docs"]:
            env = e["doc"].get("env") if isinstance(e.get("doc"), dict) else None
            if e["scope"] == scope and isinstance(env, dict) and env.get("ANTHROPIC_BASE_URL"):
                return host_of(str(env["ANTHROPIC_BASE_URL"])), "%s settings" % scope
    v = os.environ.get("ANTHROPIC_BASE_URL")
    return (host_of(v), "the environment") if v else (None, None)


def credential_names(cfg):
    """Which credential variables are set, by NAME only -- never a value."""
    names = []
    for n in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(n) or any(isinstance((e.get("doc") or {}).get("env"), dict)
                                    and (e["doc"]["env"] or {}).get(n) for e in cfg["docs"]):
            names.append(n)
    return names


# --------------------------------------------------------------------- the transcripts
def slug_of(path):
    """Claude Code's folder name for a working directory: every non-alphanumeric becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def project_transcript_dir(root, projects_dir=None, h=None):
    """The folder Claude Code keeps this project's sessions in, if there is one."""
    base = projects_dir or os.path.join(sb.claude_dir(h), "projects")
    for spelling in (os.path.abspath(root), os.path.realpath(root)):
        d = os.path.join(base, slug_of(spelling))
        if os.path.isdir(d):
            return d
    return None


def transcript_files(root, projects_dir=None, h=None, transcript=None, max_files=MAX_FILES):
    """The newest transcripts for this project (sessions and their subagents), newest first."""
    if transcript:
        return [transcript] if os.path.isfile(transcript) else []
    base = projects_dir or os.path.join(sb.claude_dir(h), "projects")
    dirs = []
    for spelling in (os.path.abspath(root), os.path.realpath(root)):
        d = os.path.join(base, slug_of(spelling))
        if os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    files = []
    for d in dirs:
        for dirpath, _subdirs, names in os.walk(d):
            for n in names:
                if n.endswith(".jsonl"):
                    p = os.path.join(dirpath, n)
                    try:
                        files.append((os.path.getmtime(p), p))
                    except OSError:
                        pass
    files.sort(reverse=True)
    return [p for _m, p in files[:max_files]]


# A row is parsed only if it can matter: a tool call (to link a result to), an answered prompt or
# a refusal, a provider error, a mode change, or a failed agent's notice.
NEEDLES = ("tool_use", "permissionDecision", "isApiErrorMessage", "api_error", "permission-mode",
           "<status>failed</status>")


def tail_rows(path, max_bytes=TAIL_BYTES):
    """The JSON rows that can matter in the last `max_bytes` of a transcript. A half-written first
    or last line is skipped, never an error."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            start = max(0, size - max_bytes)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").split("\n")
    if start:
        lines = lines[1:]
    rows = []
    for ln in lines:
        ln = ln.strip()
        if not ln.startswith("{") or not any(n in ln for n in NEEDLES):
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


def _ts(raw):
    try:
        import datetime
        d = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return d.timestamp()
    except (TypeError, ValueError):
        return None


def _text_of(block):
    c = block.get("content")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c if isinstance(c, str) else ""


def _flat(text, n=2000):
    return re.sub(r"\s+", " ", text or "").strip()[:n]


def extract_events(rows, source):
    """The events in a transcript's rows: refusals, answered prompts, API errors.
    -> (events, last permission mode)."""
    uses, events, mode, last_ok = {}, [], None, -1
    for i, r in enumerate(rows):
        t = r.get("type")
        ts = _ts(r.get("timestamp"))
        if t == "permission-mode":
            mode = r.get("permissionMode") or mode
            continue
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        if t == "assistant":
            if r.get("isApiErrorMessage"):
                text = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
                events.append({"kind": "api-error", "ts": ts, "order": i, "source": source,
                               "category": r.get("error"), "status": r.get("apiErrorStatus"),
                               "text": _flat(text)})
                continue
            last_ok = i
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    uses[b["id"]] = {"name": b.get("name"),
                                     "input": b.get("input") if isinstance(b.get("input"), dict)
                                     else {}}
        elif t == "user":
            pd = r.get("permissionDecision") if isinstance(r.get("permissionDecision"), dict) else {}
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                use = uses.get(b.get("tool_use_id")) or {}
                ev = _result_event(use, _flat(_text_of(b)), bool(b.get("is_error")), pd)
                if ev:
                    ev.update({"ts": ts, "order": i, "source": source})
                    events.append(ev)
            # a failed background agent is reported to the main session as a task notification
            blob = msg.get("content") if isinstance(msg.get("content"), str) else ""
            if "<status>failed</status>" in blob:
                for m in re.finditer(r"<summary>(.*?)</summary>", blob, re.S):
                    if not m.group(1).startswith("Background command"):
                        events.append({"kind": "agent-failed", "ts": ts, "order": i,
                                       "source": source, "text": _flat(m.group(1))})
        elif t == "system" and r.get("subtype") == "api_error":
            e = r.get("error") if isinstance(r.get("error"), dict) else {}
            events.append({"kind": "api-error", "ts": ts, "order": i, "source": source,
                           "category": None, "status": e.get("status"), "retry": True,
                           "text": _flat(e.get("formatted") or e.get("message") or "")})
    # a retried failure that a later answer followed was survived; a final failure is kept, and
    # says whether the session went on afterwards
    kept = []
    for e in events:
        if e["kind"] == "api-error":
            e["recovered"] = e["order"] < last_ok
            if e.get("retry") and (e["recovered"] or e.get("status") not in (401, 402, 403, 429)):
                continue
        kept.append(e)
    return kept, mode


def _result_event(use, text, is_error, pd):
    """One tool_result -> an event, or None when it is not a refusal or a prompt."""
    name = use.get("name")
    inp = use.get("input") or {}
    keep = {k: inp[k] for k in ("command", "file_path", "path", "notebook_path", "pattern")
            if isinstance(inp.get(k), str)}
    base = {"tool": name, "input": keep}
    reject = pd.get("decision") == "reject"
    if is_error or reject:
        m = CORE_BLOCK.search(text)
        if m:
            return dict(base, kind="core-rule", rule=m.group(1))
        low = text.lower()
        if "auto mode classifier" in low:
            r = re.search(r"Reason: \[([^\]]{1,80})\]", text)
            return dict(base, kind="classifier", reason=r.group(1) if r else None)
        if "built-in claude code safety check" in low or pd.get("reasonType") == "safetyCheck":
            return dict(base, kind="safety-check")
        if low.startswith("the user doesn't want to proceed"):
            return None                                   # you said no: not a gate
        if FENCE_TEXT.search(text):
            return dict(base, kind="blocked-read")
        if DENY_TEXT.search(text) or (reject and pd.get("reasonType") == "rule"):
            return dict(base, kind="deny-rule")
        if "hook error" in low and "blocked" in low or (reject and pd.get("reasonType") == "hook"):
            return dict(base, kind="hook-block")
        return None
    if pd.get("decision") == "accept" and str(pd.get("source") or "").startswith("user"):
        return dict(base, kind="prompt", answered=pd.get("source"))
    return None


def last_events(root, projects_dir=None, h=None, transcript=None):
    """-> (events oldest-first, files read, session permission mode)."""
    files = transcript_files(root, projects_dir, h, transcript)
    events, mode = [], None
    for n, p in enumerate(files):
        rows = tail_rows(p)
        ev, m = extract_events(rows, os.path.basename(p))
        if n == 0 and m:
            mode = m
        events += ev
    events.sort(key=lambda e: (e["ts"] if e["ts"] is not None else 0.0, e["order"]))
    prov = [e for e in events if e["kind"] in ("api-error", "agent-failed")]
    rest = [e for e in events if e["kind"] not in ("api-error", "agent-failed")]
    keep = rest[-WINDOW:] + prov[-10:]               # a pile of prompts must not hide a spend cap
    keep.sort(key=lambda e: (e["ts"] if e["ts"] is not None else 0.0, e["order"]))
    return keep, files, mode


# --------------------------------------------------------------------- rule matching
def _grant():
    import gt_grant
    return gt_grant


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n")


def _without_heredocs(text):
    """The command with each heredoc body cut out: its lines are text, not commands."""
    out, pos = [], 0
    while True:
        m = _HEREDOC.search(text, pos)
        if not m:
            out.append(text[pos:])
            return "".join(out)
        out.append(text[pos:m.end()])
        end = re.search(r"(?m)^[ \t]*%s[ \t]*$" % re.escape(m.group(2)), text[m.end():])
        pos = m.end() + end.end() if end else len(text)


def _simple_commands(command):
    """A chained command as its simple commands (split on && || ; | and newlines, outside quotes)."""
    text, parts, cur, q, i = _without_heredocs(command or ""), [], [], None, 0
    while i < len(text):
        c = text[i]
        if q:
            cur.append(c)
            if c == q:
                q = None
        elif c in "\"'":
            q = c
            cur.append(c)
        elif c in "\n;|" or (c == "&" and text[i + 1:i + 2] == "&"):
            if c in "&|" and text[i + 1:i + 2] == c:
                i += 1
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


# Commands Claude Code runs without asking whatever the rules say, so they are never the part
# that no allow rule covers.
BUILTIN_OK = ("cd", "pwd", "echo", "true", "false", "sleep", "exit", ":", "test", "[")


def _tokens(part):
    try:
        return shlex.split(part)
    except ValueError:
        return part.split()


def _drop_env(toks):
    i = 0
    while i < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[i]):
        i += 1
    return toks[:i], toks[i:]


def bash_rule_text(rule):
    """'Bash(node --test:*)' -> ('prefix', 'node --test'); None for a rule of another tool."""
    import gt_lockdown as ld
    base, pat = ld._split_rule(rule)
    if base != "Bash":
        return None
    if pat is None:
        return ("any", "")
    if pat.endswith(":*"):
        return ("prefix", pat[:-2])
    if pat.endswith(" *"):
        return ("prefix", pat[:-2])
    return ("glob" if "*" in pat else "exact", pat)


def covered_by(rules, tool, tool_input):
    """The first rule of `rules` ([(rule, scope)]) that covers the call, or None. Uses gt_grant's
    matcher -- the one a grant is judged by -- so doctor and grants cannot disagree."""
    g = _grant()
    for rule, scope in rules:
        try:
            if g.rule_matches(rule, tool, tool_input):
                return rule, scope
        except Exception:                                  # noqa: BLE001 -- a rule it cannot read
            continue
    return None


def blame_part(command, allow):
    """The simple command no allow rule covers: -> (part, all parts). A chain is judged part by
    part, because Claude Code asks about the part nothing covers."""
    parts = _simple_commands(command)
    for p in parts:
        toks = _drop_env(_tokens(p))[1]
        if not toks or toks[0] in BUILTIN_OK:
            continue                        # a bare NAME=value, or a command that never asks
        if not covered_by(allow, "Bash", {"command": p}):
            return p, parts
    return None, parts


def nearest_rule(part, allow):
    """The allow rule closest to a command it does not cover: same first word, longest shared
    leading words. -> (rule, scope, kind, text, shared) or None."""
    toks = _tokens(part)
    best = None
    for rule, scope in allow:
        rt = bash_rule_text(rule)
        if not rt or rt[0] == "any":
            continue
        rtoks = _tokens(rt[1].replace("*", ""))
        if not rtoks:
            continue
        shared = 0
        for a, b in zip(toks, rtoks):
            if a != b:
                break
            shared += 1
        # the same program is what makes a rule "near": `node --test` and `node x.mjs`
        env_cmd = _drop_env(toks)[1]
        same_prog = bool(env_cmd) and env_cmd[0] == rtoks[0]
        if shared >= 1 or same_prog:
            if best is None or shared > best[4]:
                best = (rule, scope, rt[0], rt[1], shared)
    return best


def _prefix_len(toks):
    """How many leading words name the program and what it runs: `node scripts/x.mjs`,
    `python3 -m pytest`, `cargo test`, `pytest`."""
    first = toks[0]
    if first in sp.SHELLS or first in sp.INTERPRETERS:
        return 3 if len(toks) >= 3 and toks[1] == "-m" else 2
    if len(toks) >= 2 and re.match(r"^[a-z][a-z0-9_-]*$", toks[1]):
        return 2
    return 1


def suggest_prefix(part, root):
    """A runner prefix that would cover `part` if Claude ran it relative to the project.
    -> (prefix or None, why not, grant pattern or None). The prefix is judged by the profile's own
    runner checks; the grant pattern is the same words as typed (absolute path and all), for the
    one-off route when gt will not pre-approve them. It names a program AND what it runs, never a
    whole program such as `ssh *`, and is None if any of the words looks secret."""
    env, toks = _drop_env(_tokens(part))
    if not toks:
        return None, "there is no command in it", None
    grant = None
    words = toks[:_prefix_len(toks)]
    if not env and len(words) >= 2 and words[0] not in sp.NEVER_CMD \
            and not any(_SECRET_TOKEN.search(w) for w in words):
        grant = " ".join(words) + " *"       # never a whole program (`curl *`, `ssh *`)
    if env:
        names = [e.split("=", 1)[0] for e in env]
        bad = [n for n in names if n not in sp.ENV_OK]
        if bad:
            return None, "it starts with the variable %s, which gt will not pre-approve" % bad[0], None
    rel = []
    for t in toks:
        if os.path.isabs(t):
            try:
                r = os.path.relpath(t, root).replace(os.sep, "/")
            except ValueError:                             # another drive on Windows
                r = "../" + t
            rel.append(r if not r.startswith("..") else t)
        else:
            rel.append(t)
    cand = " ".join(list(env) + rel[:_prefix_len(rel)])
    try:
        return sp.check_runner(cand), "", grant
    except sp.ProfileError as e:
        return None, str(e).replace("refused: ", "", 1), grant


# --------------------------------------------------------------------- the findings
def _fix(fid, summary, command, extra=None):
    d = {"id": fid, "summary": summary, "command": command,
         "scope": "this project only (.claude/settings.local.json)",
         "one_confirm": True, "reversible": "gt_testing_profile.py off"}
    if extra:
        d.update(extra)
    return d


def _dir_to_add(path):
    """The folder worth adding for an outside path: the repo or project root above it, else its
    own folder. A guess the person can widen; judged again by add-dir's own refusals."""
    p = os.path.realpath(path)
    cur = p if os.path.isdir(p) else os.path.dirname(p)
    start = cur
    for _ in range(8):
        if any(os.path.exists(os.path.join(cur, m)) for m in
               (".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod")):
            return cur
        up = os.path.dirname(cur)
        if up == cur:
            break
        cur = up
    return start


PLAIN = {
    "read-fence": "This fence stops Claude reading files outside your project folders. Your test "
                  "folder is not one yet: add it as a working folder and reads there stop being "
                  "refused.",
    "outside-working-dir": "Claude Code asks before it touches files outside your project folders. "
                           "Adding the folder as a working folder makes it one of them, so it stops "
                           "asking.",
    "prompt": "Claude Code asks before it runs a command that no allow rule covers. A rule matches "
              "the START of the command text -- not the folder it runs in.",
    "edit-mode": "In the default permission mode Claude Code asks before every file edit. The "
                 "accept-edits mode lets edits inside this project through and still asks about "
                 "commands.",
    "deny-rule": "A deny rule in your settings refuses this on purpose. This doctor never removes "
                 "a deny rule.",
    "classifier": "Claude Code's auto-mode safety classifier refused this action. It is not a "
                  "permission rule, so an allow rule or a fix here would not change it.",
    "safety-check": "A built-in Claude Code safety check refused this (it guards against commands "
                    "that can delete far more than intended). It is not configurable.",
    "core-rule": "This is one of Golden Thread's Core-rule gates. They protect your vault and your "
                 "secrets, run whatever your permission settings say, and this doctor never "
                 "loosens them.",
    "hook-block": "A hook blocked this, not a permission rule. This doctor does not touch hooks.",
    "tool-prompt": "Claude Code asks before it uses a tool no allow rule covers.",
}


def _core_rule_sentence(name):
    """What the Core rule says it protects, from the rule file gt ships."""
    p = os.path.join(os.path.dirname(HERE), "templates", "core-rules", "%s.md" % name)
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("description:"):
                    d = line.split(":", 1)[1].strip().strip('"')
                    return re.sub(r"^CORE rule\s*[-—–]+\s*", "", d)
    except OSError:
        pass
    return None


def analyse_prompt(ev, cfg, allow, ask, mode, h=None):
    """One answered prompt or one refusal of a tool call -> a finding dict, or None."""
    tool, inp = ev.get("tool"), ev.get("input") or {}
    if not tool or tool in ASKS_BY_DESIGN:
        return None            # no tool call (older than the part read), or a question not a check
    root = cfg["root"]
    roots = working_set(cfg, h)
    fence, fence_scope = effective(cfg, "blockReadsOutsideWorkingDirectories", True)
    fence_on = _truthy(fence)
    path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
    f = {"tool": tool, "when": ev.get("ts")}
    if tool in READ_TOOLS + EDIT_TOOLS and path:
        if not os.path.isabs(path):
            path = os.path.join(root, path)
        if not inside_any(path, roots):
            target = _dir_to_add(path)
            f.update(gate="read-fence" if (fence_on and tool in READ_TOOLS) else "outside-working-dir",
                     path=path, add_dir=target)
            if f["gate"] == "read-fence":
                f["title"] = "The read fence refused a file outside your working folders"
                f["why"] = ("blockReadsOutsideWorkingDirectories is ON (%s settings) and %s is not "
                            "inside the project or any additional working folder."
                            % (fence_scope, short_path(path, h)))
            else:
                f["title"] = "Claude Code asked about a file outside your working folders"
                f["why"] = ("%s is not inside the project or any additional working folder, so "
                            "Claude Code asks each time." % short_path(path, h))
            f["plain"] = PLAIN[f["gate"]]
            f["fixes"] = _add_dir_fixes(target, root, h)
            f["never"] = ["relaxing the read fence itself: it protects every project, so the cure "
                          "is to add the folder, never to lower the fence"]
            return f
    if tool in READ_TOOLS and path and ev.get("kind") == "blocked-read":
        f.update(gate="fence-restart", path=path,
                 title="Already fixed in your settings: restart Claude Code to apply it",
                 why="%s is inside a working folder in your settings now, but the refusal was "
                     "recorded before Claude Code read them: it reads settings when it starts."
                     % short_path(path, h),
                 plain=PLAIN["read-fence"], fixes=[],
                 manual="restart Claude Code (or open a new session) in this project")
        return f
    if tool in EDIT_TOOLS:
        eff = mode or effective(cfg, "defaultMode")[0] or "default"
        if eff in LOOSER_MODES:
            return None
        f.update(gate="edit-mode", title="Edits ask because the permission mode is default",
                 why="Claude Code is in %r mode, which asks before every file edit." % eff,
                 plain=PLAIN["edit-mode"])
        f["fixes"] = [_fix("accept-edits", "set defaultMode to acceptEdits for this project",
                           "%s fix accept-edits --project-dir %s" % (self_command(h),
                                                                    _quote_path(root, h)))]
        f["alternative"] = ("narrower: a testing profile with --sandbox <folder> allows edits "
                            "in that one folder only (fix profile --sandbox <folder>)")
        return f
    if tool == "Bash":
        cmd = inp.get("command") or ""
        part, parts = blame_part(cmd, allow)
        shape = safe_shape(part or cmd)
        f["command"] = shape
        asked = covered_by(ask, "Bash", {"command": part or cmd})
        if part is None:
            f.update(gate="prompt-rule-not-applied",
                     title="A prompt appeared although an allow rule covers the command",
                     plain=PLAIN["prompt"])
            if asked:
                f["why"] = ("an `ask` rule (%s, %s settings) also matches `%s`, and an ask rule "
                            "beats an allow rule." % (asked[0], asked[1], shape))
                f["fixes"] = []
                f["manual"] = "remove or narrow that ask rule yourself; the doctor does not edit rules you wrote"
            else:
                hit = covered_by(allow, "Bash", {"command": cmd})
                f["why"] = ("`%s` is covered by %s (%s settings), so the prompt most likely came "
                            "before the rule was added: Claude Code reads settings when it starts."
                            % (shape, hit[0] if hit else "an allow rule", hit[1] if hit else "?"))
                f["fixes"] = []
                f["manual"] = "restart Claude Code (or open a new session) so it reads the rule"
            return f
        near = nearest_rule(part, allow)
        sugg, why_not, grant = suggest_prefix(part, root)
        if len(parts) > 1:
            f["chain"] = "%d commands chained; this is the one nothing covers" % len(parts)
        if near:
            rule, scope, rkind, rtext, _shared = near
            f.update(gate="prompt-prefix-mismatch",
                     title="A permission prompt: a rule exists, but its prefix does not match",
                     plain=PLAIN["prompt"],
                     near_rule=rule)
            if rkind == "prefix":
                f["why"] = ("you have %s (%s settings), but Claude Code matches a Bash rule on "
                            "the start of the command text, and `%s` does not start with `%s`. "
                            "The folder it runs in is not the issue." % (rule, scope, shape, rtext))
            elif rkind == "exact":
                f["why"] = ("you have %s (%s settings), which matches only that exact command, "
                            "and `%s` is not it." % (rule, scope, shape))
            else:
                f["why"] = ("you have %s (%s settings), but its pattern does not match `%s`; "
                            "Claude Code compares the command text, not the folder it runs in."
                            % (rule, scope, shape))
        else:
            f.update(gate="prompt-missing-rule",
                     title="A permission prompt: no allow rule covers this command",
                     plain=PLAIN["prompt"])
            f["why"] = ("none of your %d Bash allow rule(s) starts like `%s`, so Claude Code has "
                        "to ask." % (len([1 for r, _s in allow if bash_rule_text(r)]), shape))
        fixes = []
        default_hit = sugg and any(sugg == d or sugg.startswith(d + " ") for d in sp.DEFAULT_RUNNERS)
        if sugg:
            fixes.append(_fix("allow", "allow `%s` (one rule: Bash(%s:*))" % (sugg, sugg),
                              "%s fix allow %s --project-dir %s"
                              % (self_command(h), shlex.quote(sugg), _quote_path(root, h)),
                              {"rule": "Bash(%s:*)" % sugg}))
            if sugg != " ".join(_tokens(part)[:len(_tokens(sugg))]):
                f["note"] = ("Claude must run it in that form (relative to the project) for the rule "
                             "to match: tell it to use `%s`." % sugg)
        if default_hit:
            fixes.append(_fix("profile", "or turn on the whole testing profile (pytest, npm test, "
                              "node --test, gt's own runners) for this project",
                              "%s fix profile --project-dir %s" % (self_command(h),
                                                                  _quote_path(root, h))))
        f["fixes"] = fixes
        if not sugg:
            f["manual"] = ("gt will not pre-approve this command (%s). For a one-off, a "
                           "time-limited grant is the route: /gt:gt-grant." % why_not)
            if grant:
                f["grant"] = ("python3 %s add %s 1h" % (_quote_path(os.path.join(HERE, "gt_grant.py"), h),
                                                         shlex.quote(grant)))
                f["grant_note"] = ("a grant is the user's to give, in a terminal; it covers every "
                                   "project on this machine for that hour, then ends by itself")
        return f
    f.update(gate="tool-prompt", title="A permission prompt for %s" % tool, plain=PLAIN["tool-prompt"],
             why="no allow rule in your settings covers a %s call." % tool, fixes=[],
             manual="gt does not pre-approve this tool automatically; /gt:gt-grant lets one "
                    "call through for a while")
    return f


def _add_dir_fixes(target, root, h=None):
    try:
        real = sp.check_directory(target, h)
    except sp.ProfileError as e:
        return [{"id": "add-dir-refused", "summary": "gt will not add %s: %s"
                 % (short_path(target, h), str(e).replace("refused", "refused", 1)),
                 "command": None}]
    return [_fix("add-dir", "add %s as a working folder" % short_path(real, h),
                 "%s fix add-dir %s --project-dir %s" % (self_command(h), _quote_path(real, h),
                                                        _quote_path(root, h)),
                 {"dir": real, "session_only": "/add-dir %s" % real})]


# --------------------------------------------------------------------- the provider's errors
def _hm(secs):
    secs = max(0, int(secs))
    h, m = secs // 3600, (secs % 3600) // 60
    return "%dh %02dm" % (h, m) if h else "%dm" % m


def _ago(secs):
    return "under a minute ago" if secs < 60 else _hm(secs) + " ago"


def _clock(t):
    return time.strftime("%H:%M %Z", time.localtime(t)).strip()


def next_utc_midnight(now):
    return now - (now % 86400) + 86400


def parse_reset(text, now):
    """'resets 5:10pm (America/Chicago)' -> (epoch, 'tz name') or (None, text-or-None)."""
    m = re.search(r"resets? (?:at )?(\d{1,2})(?::(\d\d))?\s*([ap]m)\b(?:\s*\(([A-Za-z_]+/[A-Za-z_]+|UTC)\))?",
                  text or "", re.I)
    if not m:
        return None, None
    hour, minute = int(m.group(1)) % 12, int(m.group(2) or 0)
    if m.group(3).lower() == "pm":
        hour += 12
    label = "%d:%02d%s" % (int(m.group(1)), minute, m.group(3).lower()) + \
        ((" " + m.group(4)) if m.group(4) else "")
    tzname = m.group(4)
    try:
        import datetime
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            return None, label
        tz = ZoneInfo(tzname) if tzname else None
        if tz is None:
            return None, label
        cur = datetime.datetime.fromtimestamp(now, tz)
        tgt = cur.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if tgt <= cur:
            tgt += datetime.timedelta(days=1)
        return tgt.timestamp(), label
    except Exception:                                      # noqa: BLE001 -- no tz database
        return None, label


def classify_provider(ev, cfg, now):
    """A provider error -> a finding dict (gate provider-*), in plain words with the cure and the
    reset time. The text it sees is the error's own; only a host name and credential NAMES are
    added from the configuration -- never a value."""
    text = ev.get("text") or ""
    low = text.lower()
    status = ev.get("status")
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    m = re.match(r"^(?:API Error: )?(\d{3})\b", text)
    if status is None and m:
        status = int(m.group(1))
    cat = ev.get("category")
    host, host_from = base_url(cfg)
    amounts = re.search(r"([€$£]\s?\d[\d.,]*)\s*(?:of|/)\s*([€$£]?\s?\d[\d.,]*)", text)
    spent = "%s of a %s limit" % (amounts.group(1), amounts.group(2).strip()) if amounts else None
    f = {"tool": None, "when": ev.get("ts"), "evidence": safe_error(text, 240),
         "recovered": bool(ev.get("recovered")),
         "agent": ev["kind"] == "agent-failed", "provider": host or "Anthropic (direct)",
         "fixes": [], "never": []}
    died = "A background agent died on this. " if f["agent"] else ""

    if status == 402 or re.search(r"spend(ing)? (limit|cap)|budget (exceeded|reached)|credit balance|"
                                  r"insufficient (credit|fund)|payment required|usage-credits", low):
        f.update(gate="provider-spend-cap",
                 title="The model provider refused requests: spending limit reached")
        f["plain"] = (died + "Your %s has used up its spending limit%s, so every request is refused "
                      "until the limit resets or is raised -- your settings and rules are not the "
                      "problem." % ("proxy account" if host else "account",
                                    (" (%s)" % spent) if spent else ""))
        if host:
            f["resolution"] = ("Raise the limit in the dashboard of %s (your ANTHROPIC_BASE_URL, "
                               "from %s), or ask whoever runs it; Claude Code cannot change it."
                               % (host, host_from))
        else:
            f["resolution"] = ("Run /usage-credits to raise the limit or add credit, or switch "
                               "model with /model; Claude Code is talking to Anthropic directly.")
        if "daily" in low:
            nxt = next_utc_midnight(now)
            f["reset"] = ("%s UTC (%s local), in %s, if it resets at UTC midnight as daily limits "
                          "normally do; confirm with whoever runs it."
                          % ("00:00", _clock(nxt), _hm(nxt - now)))
            f["reset_epoch"] = nxt
        elif "monthly" in low or "month" in low:
            f["reset"] = ("the start of your next billing month (normally the 1st, 00:00 UTC); "
                          "/usage-credits or your provider's billing page shows the date.")
        else:
            rt, label = parse_reset(text, now)
            f["reset"] = ("%s (in %s)" % (label, _hm(rt - now)) if rt else label) or \
                "the error does not say; ask whoever runs the limit"
    elif status == 429 or cat == "rate_limit" or re.search(
            r"session limit|rate.?limit|too many requests|usage limit", low):
        f.update(gate="provider-rate-limit", title="The model provider refused requests: rate limit")
        f["plain"] = (died + "You have hit a usage window or rate limit, so requests are refused "
                      "until it resets -- your settings and rules are not the problem.")
        rt, label = parse_reset(text, now)
        f["reset"] = ("%s (in %s)" % (label, _hm(rt - now)) if rt else label) or \
            "the error does not say; it clears on its own"
        if rt:
            f["reset_epoch"] = rt
        f["resolution"] = ("Wait for the reset, or run /usage-credits to keep going, or switch "
                           "model with /model.")
    elif status in (401, 403) or cat == "authentication_failed" or re.search(
            r"authenticat|not logged in|please run /login|login expired|invalid.{0,20}api.?key|"
            r"unauthori[sz]ed|forbidden", low):
        f.update(gate="provider-auth", title="The model provider refused the login")
        f["plain"] = (died + "Claude Code could not sign in to its provider, so every request is "
                      "refused -- your settings and rules are not the problem.")
        names = credential_names(cfg)
        if host:
            f["resolution"] = ("%s rejected the credential. Check %s for %s (set in %s); get a new "
                               "key from whoever runs it."
                               % (host, " / ".join(names) or "ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY",
                                  host, host_from))
        elif names:
            f["resolution"] = ("An API key variable (%s) is set and was rejected: check it, or "
                               "unset it to use your login instead." % ", ".join(names))
        else:
            f["resolution"] = "Run /login to sign in again; an expired login does not renew itself."
        f["reset"] = "none: a login does not expire on a clock; it works again once you sign in"
    elif cat == "invalid_request" or "prompt is too long" in low or "context length" in low:
        f.update(gate="provider-context", title="The conversation is too long for the model")
        f["plain"] = (died + "The conversation grew past what the model can read in one go, so the "
                      "request was refused -- your settings and rules are not the problem.")
        f["resolution"] = ("Run /compact (or /clear and carry on from a handoff); a long test run "
                           "that reads a lot of output hits this.")
        f["reset"] = "none; it works again once the conversation is shorter"
    elif status in (500, 502, 503, 504, 529) or re.search(
            r"overloaded|server error|internal server|service unavailable", low):
        f.update(gate="provider-overloaded", title="The model provider was overloaded or failing")
        f["plain"] = (died + "The provider's servers (or your proxy) failed after Claude Code "
                      "retried. Nothing in your settings caused it.")
        f["resolution"] = "Nothing to fix: re-run it. A background agent that died this way can be started again."
        f["reset"] = "none; this usually clears within minutes"
    elif re.search(r"can't reach|unable to connect|connection (refused|error|reset)|enotfound|"
                   r"econn|etimedout|went to sleep|firewall|proxy", low):
        f.update(gate="provider-network", title="Claude Code could not reach the model provider")
        f["plain"] = (died + "The connection to the provider failed (network, DNS, a firewall or "
                      "proxy, or the computer slept). Nothing in your settings caused it.")
        f["resolution"] = ("Check the network, VPN and proxy%s, then re-run it."
                           % ((" (%s)" % host) if host else ""))
        f["reset"] = "none; it works again when the connection does"
    else:
        f.update(gate="provider-other", title="The model provider returned an error",
                 plain=died + "The provider returned an error that this doctor does not "
                 "recognise; the words are below.",
                 resolution="Read the message; if it is a limit or a login, its owner can fix it.",
                 reset="the error does not say")
    f["why"] = "the provider answered: %s" % (safe_error(text) or "(no text)")
    return f


def safe_error(text, n=200):
    """An error message for display: no URLs beyond their host, no long token-like strings."""
    t = re.sub(r"https?://[^\s\"']+", lambda m: "https://" + (host_of(m.group(0)) or "?") + "/...", text)
    t = re.sub(r"\b[A-Za-z0-9_\-]{32,}\b", "<hidden>", t)
    return t[:n]


# --------------------------------------------------------------------- the report
def diagnose(project_dir=None, projects_dir=None, transcript=None, limit=6, h=None, now=None,
             managed=None):
    """-> the report dict: posture, what was read, and the blockers found (newest first)."""
    t = sp._now(now)
    cfg = load_config(project_dir, h, managed)
    root = cfg["root"]
    events, files, session_mode = last_events(root, projects_dir, h, transcript)
    allow, ask, deny = rules_of(cfg, "allow"), rules_of(cfg, "ask"), rules_of(cfg, "deny")
    set_mode, mode_scope = effective(cfg, "defaultMode")
    fence, fence_scope = effective(cfg, "blockReadsOutsideWorkingDirectories", True)
    st = sp.load_state(h)
    profiles = [r for r in st["profiles"].values()
                if r.get("project_dir") and os.path.realpath(r["project_dir"]) == os.path.realpath(root)
                or r.get("scope") == "user"]
    host, host_from = base_url(cfg)
    posture = {
        "project": root,
        "settings": [{"scope": e["scope"], "path": e["path"],
                      "state": "error: " + e["error"] if e["error"] else
                      ("read" if e["doc"] is not None else "absent")} for e in cfg["docs"]],
        "defaultMode": set_mode or "default", "defaultMode_from": mode_scope,
        "session_mode": session_mode,
        "blockReadsOutsideWorkingDirectories": _truthy(fence), "fence_from": fence_scope,
        "working_folders": working_set(cfg, h)[1:],
        "allow_rules": len(allow), "ask_rules": len(ask), "deny_rules": len(deny),
        "testing_profile": [sp.summary_line(r) for r in profiles],
        "provider": host or "Anthropic (direct)",
    }
    findings, seen = [], {}
    for ev in reversed(events):                              # newest first
        k = ev["kind"]
        f = None
        if k in ("api-error", "agent-failed"):
            f = classify_provider(ev, cfg, t)
            if k == "agent-failed" and f["gate"] == "provider-other":
                f = None                     # an agent that failed for a reason that is not the provider's
        elif k == "core-rule":
            f = {"gate": "core-rule", "title": "A Core-rule gate blocked it: %s" % ev.get("rule"),
                 "plain": PLAIN["core-rule"], "tool": ev.get("tool"), "when": ev.get("ts"),
                 "rule": ev.get("rule"), "fixes": [],
                 "why": _core_rule_sentence(ev.get("rule")) or "see the rule file in your vault",
                 "never": ["loosening this gate: it is not a permission, it is the protection"],
                 "manual": "the block message names the supported route (for example --vault <path> "
                           "or the write queue); follow that, not a setting"}
        elif k in ("classifier", "safety-check", "deny-rule", "hook-block"):
            f = {"gate": k, "title": {"classifier": "Claude Code's auto-mode classifier refused it",
                                      "safety-check": "A built-in safety check refused it",
                                      "deny-rule": "A deny rule refused it",
                                      "hook-block": "A hook blocked it"}[k],
                 "plain": PLAIN[k], "tool": ev.get("tool"), "when": ev.get("ts"), "fixes": [],
                 "why": ("the classifier's stated reason: %s" % ev["reason"]) if ev.get("reason")
                 else "see the message Claude Code showed at the time",
                 "never": ["changing this from a setting"]}
            if ev.get("input", {}).get("command"):
                f["command"] = safe_shape(ev["input"]["command"])
            if k == "deny-rule":
                hit = covered_by(deny, ev.get("tool") or "Bash", ev.get("input") or {})
                if hit:
                    f["why"] = "%s (%s settings) matches it." % hit
                f["manual"] = "if that deny is no longer wanted, remove it yourself; the doctor never does"
        elif k == "blocked-read":
            f = analyse_prompt(dict(ev, kind="blocked-read"), cfg, allow, ask, session_mode, h)
            if f and f.get("gate") not in ("read-fence", "outside-working-dir", "fence-restart"):
                f = None
        elif k == "prompt":
            f = analyse_prompt(ev, cfg, allow, ask, session_mode, h)
        if not f:
            continue
        sig = (f["gate"], f.get("command") or f.get("path") or f.get("rule") or f.get("evidence"))
        if sig in seen:
            seen[sig]["count"] += 1
            continue
        f["count"] = 1
        f["id"] = "b%d" % (len(findings) + 1)
        seen[sig] = f
        findings.append(f)
        if len(findings) >= limit:
            break
    for f in findings:
        if f.get("when"):
            f["ago"] = _ago(t - f["when"]) if t >= f["when"] else "just now"
    return {"posture": posture, "transcripts": [os.path.basename(p) for p in files],
            "transcripts_dir": project_transcript_dir(root, projects_dir, h)
            if files and not transcript else None,
            "events_read": len(events), "findings": findings, "now": t}


OFFER_GATES = ("read-fence", "outside-working-dir", "fence-restart", "prompt", "edit-mode",
               "tool-prompt")                    # testing blockers; never core-rule, deny, hook, provider
OFFER_SKIP = re.compile(r"(?i)secret|credential|\.env\b|\.aws|\.ssh|\.gnupg|token|password|keychain|"
                        r"vault|obsidian|golden-thread|\.claude")
OFFER = ("Or run it in a throwaway VM or container: /gt:gt-disposable "
         "(gt_disposable.py plan --tree . -- <command>)")


def _backends():
    import gt_disposable as gd                      # a sibling; never probes (runs nothing)
    return gd.detect(probe=False)


def offer_line(rep, backends=None):
    """ONE line offering the disposable runner, or "" -- text only, runs and queues nothing. Only for
    a testing blocker the runner can sidestep; never for secrets, credentials or the vault (those must
    not be worked around), and silent (fails open) when no backend exists or the probe fails."""
    try:
        fs = [f for f in rep.get("findings") or [] if f.get("gate") in OFFER_GATES
              and not OFFER_SKIP.search(" ".join(str(f.get(k) or "") for k in ("command", "path", "title")))]
        if not fs:
            return ""
        rows = backends() if callable(backends) else (_backends() if backends is None else backends)
        return OFFER if any(r.get("usable") for r in rows) else ""
    except Exception:                                # noqa: BLE001
        return ""


def render(rep, h=None, backends=None):
    """The report as text a person can act on."""
    po = rep["posture"]
    out = ["GOLDEN THREAD doctor testing -- why is my test run blocked?",
           "project   %s" % short_path(po["project"], h)]
    for s in po["settings"]:
        out.append("settings  %-8s %s (%s)" % (s["scope"], short_path(s["path"], h), s["state"]))
    out.append("mode      defaultMode %s%s%s" % (
        po["defaultMode"], (" (from %s)" % po["defaultMode_from"]) if po["defaultMode_from"] else "",
        ("; this session ran in %s" % po["session_mode"]) if po["session_mode"] else ""))
    out.append("fence     blockReadsOutsideWorkingDirectories is %s%s" % (
        "ON" if po["blockReadsOutsideWorkingDirectories"] else "off",
        (" (from %s)" % po["fence_from"]) if po["fence_from"] else ""))
    out.append("folders   %d extra working folder(s); %d allow, %d ask, %d deny rule(s)"
               % (len(po["working_folders"]), po["allow_rules"], po["ask_rules"], po["deny_rules"]))
    out.append("profile   %s" % ("; ".join(po["testing_profile"]) if po["testing_profile"]
                                 else "testing profile: off"))
    out.append("provider  %s" % po["provider"])
    if rep["transcripts"]:
        out.append("read      %d recent event(s) in %d transcript(s)%s"
                   % (rep["events_read"], len(rep["transcripts"]),
                      (" under " + short_path(rep["transcripts_dir"], h)) if rep["transcripts_dir"]
                      else " (named with --transcript)"))
    else:
        out.append("read      no session transcript found for this project (looked in "
                   "~/.claude/projects); only the live settings above were checked")
    fs = rep["findings"]
    out.append("")
    if not fs:
        out.append("No blocker found in the recent record.%s" % (
            "" if rep["transcripts"] else " That is not the same as none: with no transcript there "
            "was no denial to read -- run it again after the next block, or pass --transcript."))
        out.append("Nothing was changed. Start a run, and if it stops, ask again.")
        return "\n".join(out)
    out.append("%d blocker%s, newest first:" % (len(fs), "" if len(fs) == 1 else "s"))
    for n, f in enumerate(fs, 1):
        out.append("")
        out.append(wrap("%d. [%s] %s" % (n, f["gate"], f["title"]), "   ", first=""))
        if f.get("ago"):
            out.append("   seen %s%s" % (f["ago"], (", %d times" % f["count"]) if f["count"] > 1 else ""))
        if f.get("command"):
            out.append("   command   %s" % f["command"])
        if f.get("path"):
            out.append("   path      %s" % short_path(f["path"], h))
        if f.get("chain"):
            out.append(wrap("chain     %s" % f["chain"], "             ", first="   "))
        out.append(wrap("%s: %s" % ("what happened" if f["gate"].startswith("provider-")
                                    else "what it protects", f["plain"]), "      ", first="   "))
        if f.get("recovered"):
            out.append("   (the session carried on afterwards, so it may have cleared by itself)")
        out.append(wrap("why here: %s" % f["why"], "      ", first="   "))
        if f.get("note"):
            out.append(wrap("note: %s" % f["note"], "      ", first="   "))
        if f.get("resolution"):
            out.append(wrap("what clears it: %s" % f["resolution"], "      ", first="   "))
        if f.get("reset"):
            out.append(wrap("resets: %s" % f["reset"], "      ", first="   "))
        for fx in f.get("fixes") or []:
            if fx["command"]:
                out.append(wrap("fix: %s -- one confirm, %s, undone by `gt_testing_profile.py off`:"
                                % (fx["summary"], fx["scope"]), "      ", first="   "))
                out.append("        %s" % fx["command"])
                if fx.get("session_only"):
                    out.append("      or for this session only, inside Claude Code: %s"
                               % fx["session_only"])
            else:
                out.append(wrap("no fix offered: %s" % fx["summary"], "      ", first="   "))
        if f.get("alternative"):
            out.append(wrap("or %s" % f["alternative"], "      ", first="   "))
        if f.get("manual"):
            out.append(wrap("you: %s" % f["manual"], "      ", first="   "))
        if f.get("grant"):
            out.append(wrap("for one hour, in your terminal (%s):" % f["grant_note"], "      ",
                            first="   "))
            out.append("        %s" % f["grant"])
        for nv in f.get("never") or []:
            out.append(wrap("never offered: %s" % nv, "      ", first="   "))
    offer = offer_line(rep, backends)
    if offer:
        out.append("")
        out.append(offer)
    out.append("")
    out.append(wrap("Nothing was changed. A fix writes only after you run its command in a terminal "
                    "and answer yes. Undo everything with: gt_testing_profile.py off", ""))
    return "\n".join(out)


# --------------------------------------------------------------------- the commands
def _need_arg(a, what):
    if not a.arg:
        raise sp.UsageError("fix %s needs %s" % (a.what, what))


def run_fix(a, h=None):
    rerun = "%s fix %s%s --project-dir %s" % (self_command(h), a.what,
                                              (" " + shlex.quote(a.arg)) if a.arg else "",
                                              _quote_path(sp.project_root(a.project_dir), h))
    if a.what == "profile":
        return _fix_profile(a, rerun, h)
    if a.sandbox or a.runners:
        raise sp.UsageError("--sandbox and --runners belong to `fix profile`")
    if a.what in ("add-dir", "allow"):
        _need_arg(a, "a folder" if a.what == "add-dir" else "a command prefix")
    elif a.arg:
        raise sp.UsageError("fix accept-edits takes no argument")
    return sp.apply_fix(a.what, a.arg, a.project_dir, a.until, a.dry_run, a.yes, rerun=rerun, h=h)


def _fix_profile(a, rerun, h=None):
    """The whole testing profile, into the local file. The profile's own turn_on does the work and
    the checks; this only adds the one explicit yes, after showing the diff."""
    if a.arg:
        raise sp.UsageError("fix profile takes no argument (see --sandbox and --runners)")
    kw = dict(scope="local", project_dir=a.project_dir, runners=a.runners, sandbox=a.sandbox,
              until=a.until, h=h)
    root = sp.project_root(a.project_dir)
    why = sp._vault_conflict(root, h)
    if why:
        raise sp.ProfileError("refused (vault): this project folder %s (%s); run the fix for the "
                              "code repository with --project-dir." % (why, root))
    print("fix: turn on the testing profile for this project only (settings.local.json)")
    sp.turn_on(dry_run=True, dry_label="the change to %s (nothing is written yet):"
               % sp.settings_for("local", a.project_dir, h), **kw)
    if a.dry_run:
        return 0
    sp._require_terminal(rerun)
    if not sp._ask_yes("Apply this change?", a.yes):
        print("declined: nothing changed.")
        return 1
    return sp.turn_on(dry_run=False, **kw)


def run_off(a, h=None):
    return sp.turn_off(a.scope, a.project_dir, h)


def _windows_utf8():
    if os.name != "nt":
        return
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def build_parser():
    ap = argparse.ArgumentParser(
        prog="gt_testing_doctor.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Why is my test run blocked? Names the gate that fired (from your live "
                    "settings and the last denial in your session transcripts) and offers the "
                    "one scoped fix. `fix` and `off` change settings.local.json; diagnose never "
                    "writes.",
        epilog="Also: gt_doctor.py testing ...  The Core-rule gates are never loosened.")
    # The diagnose flags are on the top parser (so `gt_testing_doctor.py --json` is the default
    # verb) and on `diagnose` itself. SUPPRESS there: a subparser default would overwrite a
    # value given before the verb.
    def diag_flags(parser, sub):
        d = argparse.SUPPRESS if sub else None
        parser.add_argument("--project-dir", default=d,
                            help="the project (default: $CLAUDE_PROJECT_DIR, else the current folder)")
        parser.add_argument("--json", action="store_true", default=d if sub else False,
                            help="machine-readable output")
        parser.add_argument("--transcript", default=d,
                            help="read this one session transcript instead of the project's newest")
        parser.add_argument("--projects-dir", default=d,
                            help="Claude Code's projects folder (default ~/.claude/projects)")
        parser.add_argument("--limit", type=int, default=d if sub else 6,
                            help="at most this many blockers (default 6)")
    diag_flags(ap, False)
    diag = argparse.ArgumentParser(add_help=False)
    diag_flags(diag, True)
    sub = ap.add_subparsers(dest="cmd", metavar="{diagnose,fix,off}")
    sub.add_parser("diagnose", parents=[diag],
                   help="name the blocking gate and the fix (the default; read-only)")
    fx = sub.add_parser("fix", help="make one scoped, confirmed, reversible change",
                        description="One change to this project's .claude/settings.local.json: "
                                    "add-dir DIR | allow PREFIX | accept-edits | profile. Shows "
                                    "the diff, needs a terminal and one yes.")
    fx.add_argument("what", choices=("add-dir", "allow", "accept-edits", "profile"),
                    help="add-dir DIR, allow PREFIX, accept-edits, or profile")
    fx.add_argument("arg", nargs="?", default=None, help="the folder (add-dir) or prefix (allow)")
    fx.add_argument("--project-dir", default=argparse.SUPPRESS,
                    help="the project (default: $CLAUDE_PROJECT_DIR, else the current folder)")
    fx.add_argument("--until", default=None,
                    help="end it by itself after 30m / 4h (at most 7 days); one limit covers "
                         "everything gt added to that settings file")
    fx.add_argument("--dry-run", action="store_true", help="print the diff and write nothing")
    fx.add_argument("--yes", action="store_true",
                    help="skip the y/N question (a terminal is still required)")
    fx.add_argument("--sandbox", default=None, help="profile only: the one folder edits are allowed in")
    fx.add_argument("--runners", default=None, help="profile only: your own comma list of runners")
    off = sub.add_parser("off", help="undo every change gt made to this project's settings, show the diff")
    off.add_argument("--project-dir", default=argparse.SUPPRESS,
                     help="the project (default: $CLAUDE_PROJECT_DIR, else the current folder)")
    off.add_argument("--scope", choices=sp.SCOPES, default=None,
                     help="only this settings file (default: this project's two files)")
    return ap


def main(argv=None, h=None):
    _windows_utf8()
    ap = build_parser()
    a = ap.parse_args(argv)
    cmd = a.cmd or "diagnose"
    try:
        if cmd == "fix":
            return run_fix(a, h)
        if cmd == "off":
            return run_off(a, h)
        rep = diagnose(a.project_dir, a.projects_dir, a.transcript, a.limit, h)
        if a.json:
            print(json.dumps(rep, indent=2, sort_keys=True, default=str))
        else:
            print(render(rep, h))
        return 1 if rep["findings"] else 0
    except sp.ProfileError as e:
        print(str(e))
        return 1
    except sp.UsageError as e:
        print("usage: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
