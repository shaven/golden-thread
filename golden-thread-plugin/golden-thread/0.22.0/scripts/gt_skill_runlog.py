#!/usr/bin/env python3
"""gt_skill_runlog.py -- a local record of how each gt skill ran, so a skill can be calibrated (0.21.4).

    gt_skill_runlog.py catch-up [--hook] [--days N] [--projects-dir DIR] [--max-seconds S]
    gt_skill_runlog.py status   [--json]
    gt_skill_runlog.py runs     [--skill NAME] [--json]

The calibration pass (gt_model_calibrate.py) grades a pipeline STAGE because a stage keeps the
prompt it was given, so a sample can be re-run on a neighbouring model and judged. A skill keeps
no prompt record, and roughly forty skills run on a model and effort chosen by INTENT, never by
measurement. This captures the evidence that was missing. It is OFF by default (`skill_runlog`).

WHERE A RUN COMES FROM. Claude Code's own transcripts (~/.claude/projects/**/*.jsonl) already say
everything about a skill run, and nothing has to be installed on the invocation path:
  * the user types `/gt:gt-open ...`: a user row holding `<command-name>/gt:gt-open</command-name>`
    and `<command-args>`; or
  * the model calls the Skill tool: an assistant `tool_use` named `Skill` with `input.skill` and
    `input.args`.
The run is every assistant turn after that row up to the next human prompt (or the next skill
invoked); each turn carries `message.model`, `effort` and `message.usage`, read with the same
rules gt_savings.py reads them (one request is one turn however many streamed rows repeat it; a
half-written last line is skipped). Wall clock is first row to last row of the run.

WHY A CATCH-UP AND NOT A HOOK ON THE INVOCATION. A hook on skill invocation sees only the start:
the tokens, the wall clock and how it ended are not known until later, so a second hook (Stop,
SessionEnd) would be needed, a Skill-tool call and a typed `/gt:...` are two different events,
and a session that is killed (a closed terminal fires no SessionEnd) loses everything. The
transcript holds all of it, written by Claude Code whatever happens to the session. So this does
what the savings ledger does: ONE SessionStart hook reads the transcripts that changed since last
time. A dropped session contributes its runs at the next session start. It adds no model call and
prints nothing; with `skill_runlog` off it returns before opening a transcript.

WHAT IS STORED, in ~/.claude/golden-thread/skill-runlog/runs.jsonl (append-only, newest line per
key wins on read, local only, never written into a vault or sent anywhere): per run the skill id,
how it was invoked, the model alias and effort it ran at, tokens (input, output, cache read, cache
write) and the same price-weighted figure the savings ledger uses, wall seconds, turns, an outcome
(completed | interrupted | error | abandoned | open) and a DIGEST of the task: the skill's
arguments, and for a Skill-tool call the human prompt that led to it, each cut short and
REDACTED (anything shaped like a credential is replaced). The digest is not the prompt: it is
what is needed to re-pose the task to a neighbouring model, and no more.

THE INTAKE SCAN. Every new digest is scanned by gt_intake_scan.py (credentials, prompt injection)
in one batch before anything is written. A digest that fails is EXCLUDED: not written, counted in
state.json (the rule that matched, never the text), and so never reachable by the calibration pass.
When the scan cannot run, nothing is written (a record the scan could not vouch for is not
written) and the next catch-up tries again.

Exit: 0 ok (a hook always exits 0) | 2 usage.
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))

SCHEMA = 1
CATCHUP_DAYS = 30                 # transcripts older than this are not opened (Claude Code prunes them)
MAX_SECONDS = 8.0                 # the most one catch-up spends reading transcripts
ARGS_CHARS, CONTEXT_CHARS = 600, 600
IDLE_S = 1800                     # an unfinished run in a file idle this long is "abandoned", not "open"
RETAIN_DAYS = 180                 # records older than this are dropped when the log is compacted
COMPACT_BYTES = 2 * 1024 * 1024
LOCK_STALE_S = 120
SCAN_TIMEOUT = 120
HOOK_SCAN_TIMEOUT = 7             # inside the hook's 10 s: a scan that is slower fails closed and is retried
# The same weights gt_savings.py applies (tests/test_gt_skill_runlog.py keeps the two equal): a
# request's tokens are weighted by what each kind costs, at a price of 1 per input token.
W_IN, W_OUT, W_READ, W_5M, W_1H = 1.0, 5.0, 0.1, 1.25, 2.0
OUTCOMES = ("completed", "interrupted", "error", "abandoned", "open")


# -- settings and paths ------------------------------------------------------------------------
def home():
    return os.path.expanduser("~")


def _cfg():
    try:
        with open(os.path.join(home(), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                                    # noqa: BLE001 - a hook never fails
        return {}


def enabled():
    """The skill_runlog setting, read from the one config file gt_settings keeps (so this works in
    a hook without importing the registry); anything but "on" is off."""
    v = _cfg().get("skill_runlog")
    return v is True or str(v or "off").strip().lower() == "on"


def min_runs(default=5, floor=2):
    """Fewer recorded, completed runs than this and a skill is "not calibrated" (setting)."""
    try:
        return max(floor, int(_cfg().get("skill_runlog_min_runs") or default))
    except (TypeError, ValueError):
        return default


def base_dir():
    return os.path.join(home(), ".claude", "golden-thread", "skill-runlog")


def log_path():
    return os.path.join(base_dir(), "runs.jsonl")


def state_path():
    return os.path.join(base_dir(), "state.json")


def lock_path():
    return os.path.join(base_dir(), "catchup.lock")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _opaque(s, n=12):
    return hashlib.sha256(str(s).encode("utf-8", "replace")).hexdigest()[:n]


# -- redaction ---------------------------------------------------------------------------------
_MIXED = r"(?=[A-Za-z0-9+_=-]*\d)(?=[A-Za-z0-9+_=-]*[A-Za-z])"
# (pattern, replacement): a replacement keeps the label of a `name = value` pair and drops the value
REDACTIONS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
     "[redacted]"),
    (re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|github_pat|glpat|xox[abpr])[-_][A-Za-z0-9_-]{8,}",
                re.I), "[redacted]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{30,}"), "[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"), "[redacted]"),
    (re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), "[redacted]"),
    (re.compile(r"(?i)\b([a-z0-9_.-]*(?:api[_-]?key|token|secret|passw(?:or)?d|authorization|"
                r"credential)[a-z0-9_.-]*\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|\S+)"), r"\1[redacted]"),
    (re.compile(r"(?i)(://[^/\s:@]+:)[^@\s/]+(@)"), r"\1[redacted]\2"),
    (re.compile(r"(?<![\w.+/=-])" + _MIXED + r"[A-Za-z0-9+_=-]{32,}(?![\w.+/=-])"), "[redacted]"),
)
REDACTED = "[redacted]"


def redact(text):
    """-> (text with anything shaped like a credential replaced, how many were replaced)."""
    n = 0
    text = str(text or "")
    for rx, repl in REDACTIONS:
        text, k = rx.subn(repl, text)
        n += k
    return text, n


def squash(text, limit):
    """Whitespace collapsed to single spaces and cut to `limit` characters."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def make_digest(args, context):
    """-> (digest text, redaction count). What is needed to re-pose the task, redacted and short."""
    parts, n = [], 0
    a, k = redact(squash(args, ARGS_CHARS))
    n += k
    if a:
        parts.append("arguments: " + a)
    c, k = redact(squash(context, CONTEXT_CHARS))
    n += k
    if c:
        parts.append("request: " + c)
    return "\n".join(parts), n


# -- reading one transcript --------------------------------------------------------------------
_PLUGIN = r"gt(?:-[a-z0-9]+)*"
SKILL_REF = re.compile(r"^(?:(?P<plugin>%s):(?P<qname>[a-z0-9][a-z0-9-]*)|(?P<bare>gt-[a-z0-9-]+))$"
                       % _PLUGIN)
NOT_HUMAN = ("task_notification", "task-notification", "peer")
COMMAND = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)


def skill_of(ref):
    """-> (plugin or None, skill name) for an id of a gt-family skill, else None. `gt:gt-open`,
    `gt-usage:gt-usage`, a bare `gt-open`; another plugin's skill (`pdf`, `code-review`) is not ours."""
    m = SKILL_REF.match(str(ref or "").strip().lstrip("/"))
    if not m:
        return None
    if m.group("bare"):
        return None, m.group("bare")
    return m.group("plugin"), m.group("qname")


def _ts(raw):
    try:
        return datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _num(d, k):
    try:
        return max(0, int(d.get(k) or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _is_tool_result(content):
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def alias_of(model):
    """haiku / sonnet / opus / fable, from a model id; the id itself when it is none of them."""
    m = str(model or "").lower()
    for a in ("haiku", "sonnet", "opus", "fable"):
        if a in m:
            return a
    return m or None


def weighted(u):
    return (u["input"] * W_IN + u["output"] * W_OUT + u["cache_read"] * W_READ
            + u["cache_write_5m"] * W_5M + u["cache_write_1h"] * W_1H)


def _new_run(kind, plugin, skill, row, args, context, ref):
    return {"via": kind, "plugin": plugin, "skill": skill, "ref": ref,
            "key": "%s:%s" % ("t" if kind == "tool" else "s", _opaque(ref)),
            "ts": row.get("timestamp"), "start": _ts(row.get("timestamp")), "args": args,
            "context": context, "models": {}, "efforts": {}, "turns": 0, "last": None,
            "usage": dict.fromkeys(("input", "output", "cache_read", "cache_write_5m",
                                    "cache_write_1h"), 0),
            "stop": None, "launch_error": False, "interrupted": False, "closed": None,
            "launch_id": ref if kind == "tool" else None}


def runs_from_file(path, since=None):
    """-> [run] for a transcript file: every gt skill run in it, in order. Pure: no log, no clock.
    A run's `closed` is "prompt" (a human prompt followed), "skill" (another skill was invoked) or
    None (the file ends inside it)."""
    runs, seen = [], set()
    cur = None
    context = ""                         # the latest human prompt: what led the model to a Skill call
    session_model = ""                   # the model of the latest turn outside any run (0.22.0)
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return runs

    def close(why):
        nonlocal cur
        if cur is not None:
            cur["closed"] = why
            runs.append(cur)
            cur = None

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue                         # a half-written last line
            if not isinstance(row, dict):
                continue
            if row.get("isSidechain") is True:
                continue                         # a subagent's turns are the agent's, not the skill's
            ts = _ts(row.get("timestamp"))
            if since is not None and ts is not None and ts < since:
                continue
            msg = row.get("message") if isinstance(row.get("message"), dict) else {}
            kind = row.get("type")
            if kind == "user":
                content = msg.get("content")
                if _is_tool_result(content):
                    if cur is not None:
                        cur["last"] = ts or cur["last"]
                        for b in content:
                            if isinstance(b, dict) and b.get("tool_use_id") == cur["launch_id"] \
                                    and b.get("is_error"):
                                cur["launch_error"] = True
                    continue
                if row.get("isMeta"):
                    continue                     # the skill's own text, caveats, reminders
                origin = row.get("origin")
                if (isinstance(origin, dict) and origin.get("kind") in NOT_HUMAN) \
                        or row.get("turnOrigin") in NOT_HUMAN:
                    continue                     # a background-task notification is not the human
                text = _text_of(content).strip()
                if not text or text.startswith(("<local-command-", "<bash-input>", "<bash-stdout>",
                                                "<bash-stderr>")):
                    continue
                if text.startswith("[Request interrupted by user"):
                    if cur is not None:
                        cur["interrupted"] = True
                    continue
                m = COMMAND.search(text)
                close("prompt")
                if m:
                    ref = skill_of(m.group(1))
                    if ref:
                        a = COMMAND_ARGS.search(text)
                        args = (a.group(1) if a else "").strip()
                        cur = _new_run("slash", ref[0], ref[1], row, args, "",
                                       row.get("uuid") or "%s:%s" % (path, row.get("timestamp")))
                        cur["session_model"] = session_model
                        cur["last"] = ts
                        context = args           # a Skill call made inside it is about this task
                else:
                    context = text
                continue
            if kind != "assistant":
                continue
            content = msg.get("content") if isinstance(msg.get("content"), list) else []
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            counted = False
            if usage:
                dedup = (row.get("requestId"), msg.get("id"))
                if dedup == (None, None):
                    dedup = ("uuid", row.get("uuid"))
                counted = dedup not in seen
                seen.add(dedup)
            # a Skill-tool call: the turn that made it belongs to the run before it (if any); the
            # skill's own turns are the ones after
            invoked = None
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    ref = skill_of(inp.get("skill"))
                    if ref:
                        invoked = (ref, str(inp.get("args") or ""), b.get("id") or row.get("uuid"))
                        break
            if cur is None and counted and msg.get("model"):
                session_model = msg.get("model")
            if cur is not None:
                cur["last"] = ts or cur["last"]
                if counted:
                    u = cur["usage"]
                    cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"),
                                                                    dict) else {}
                    w1h, w5m = _num(cc, "ephemeral_1h_input_tokens"), \
                        _num(cc, "ephemeral_5m_input_tokens")
                    write = _num(usage, "cache_creation_input_tokens")
                    if write and not (w1h or w5m):
                        w5m = write
                    u["input"] += _num(usage, "input_tokens")
                    u["output"] += _num(usage, "output_tokens")
                    u["cache_read"] += _num(usage, "cache_read_input_tokens")
                    u["cache_write_5m"] += w5m
                    u["cache_write_1h"] += w1h
                    cur["turns"] += 1
                    mdl = msg.get("model") or ""
                    if mdl:
                        cur["models"][mdl] = cur["models"].get(mdl, 0) + 1
                    eff = row.get("effort") or row.get("perTurnEffort")
                    if eff:
                        cur["efforts"][str(eff)] = cur["efforts"].get(str(eff), 0) + 1
                if msg.get("stop_reason"):          # the last streamed row of a message carries it
                    cur["stop"] = msg.get("stop_reason")
            if invoked:
                close("skill")
                cur = _new_run("tool", invoked[0][0], invoked[0][1], row, invoked[1], context,
                               invoked[2])
                cur["session_model"] = session_model
                cur["last"] = ts
    if cur is not None:
        runs.append(cur)
    return runs


def _modal(d):
    return max(d, key=d.get) if d else None


def outcome_of(run, file_idle):
    """completed | interrupted | error | abandoned | open."""
    if run["launch_error"]:
        return "error"
    if run["interrupted"]:
        return "interrupted"
    if run["stop"] == "end_turn" or run["closed"] == "skill":
        return "completed"                # a skill that hands over to another skill is done
    if run["closed"]:                    # the human moved on while the model was still mid-work
        return "error" if run["turns"] == 0 else "interrupted"
    return "abandoned" if file_idle else "open"


def record_of(run, session, project, file_idle):
    """-> the log record for a run, digest included (not yet scanned)."""
    digest, n = make_digest(run["args"], run["context"] if run["via"] == "tool" else "")
    mid = _modal(run["models"])
    wall = None
    if run["start"] is not None and run["last"] is not None:
        wall = max(0.0, (run["last"] - run["start"]).total_seconds())
    u = run["usage"]
    ts = run["start"].astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") \
        if run["start"] else (run["ts"] or "")
    return {"v": SCHEMA, "key": run["key"], "ts": ts, "skill": run["skill"],
            "plugin": run["plugin"], "via": run["via"], "model": alias_of(mid), "model_id": mid,
            "session_model": alias_of(run.get("session_model")),
            "effort": _modal(run["efforts"]), "turns": run["turns"],
            "tokens": dict(u), "tokens_total": sum(u.values()), "eq": round(weighted(u)),
            "wall_s": None if wall is None else round(wall, 1),
            "outcome": outcome_of(run, file_idle), "digest": digest, "redactions": n,
            "session": session, "project": project}


# -- the intake scan ---------------------------------------------------------------------------
def _tool(name):
    """gt_intake_scan.py beside this file (the hooks dir), else this tree, else the plugin cache."""
    cands = [os.path.join(HERE, name)]
    root = os.path.join(home(), ".claude", "plugins", "cache", "golden-thread-plugin", "gt")
    try:
        vs = sorted((d for d in os.listdir(root) if d.replace(".", "").isdigit()),
                    key=lambda s: tuple(int(x) for x in s.split(".")), reverse=True)
    except OSError:
        vs = []
    cands += [os.path.join(root, v, "scripts", name) for v in vs]
    return next((c for c in cands if os.path.isfile(c)), None)


def intake_scan(texts, timeout=SCAN_TIMEOUT):
    """-> (flagged {id: [rule]}, ran). ONE gt_intake_scan.py run over a folder holding each digest as
    a file. `ran` is False when the scan did not complete -- a crash, a credential scan that could
    not run, a unit it called incomplete -- and then nothing may be treated as clean."""
    if not texts:
        return {}, True
    tool = _tool("gt_intake_scan.py")
    if not tool:
        return {}, False
    d = tempfile.mkdtemp(prefix="gt-runlog-")
    try:
        names = {}
        for i, (key, text) in enumerate(sorted(texts.items())):
            names["d%05d.md" % i] = key
            with open(os.path.join(d, "d%05d.md" % i), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        argv = [sys.executable, "-B", tool, d, "--json"]
        vault = _cfg().get("vault_path")
        if vault and os.path.isdir(os.path.expanduser(str(vault))):
            argv += ["--vault", os.path.expanduser(str(vault))]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
            rep = json.loads(p.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return {}, False
        checks = rep.get("checks") or {}
        if p.returncode not in (0, 1) or not checks.get("credential") \
                or checks.get("credential_problem"):
            return {}, False
        flagged = {}
        for unit in rep.get("units") or []:
            if unit.get("incomplete") and not unit.get("findings"):
                return {}, False
            for f in unit.get("findings") or []:
                key = names.get(os.path.basename(str(f.get("file") or "")))
                if key is not None:
                    flagged.setdefault(key, []).append(str(f.get("rule") or f.get("kind") or "?"))
        return flagged, True
    finally:
        shutil.rmtree(d, ignore_errors=True)


# -- the log -----------------------------------------------------------------------------------
def read_log(path=None):
    """-> {key: newest record}. Lines that are not a JSON object with a key are skipped."""
    out = {}
    try:
        with open(path or log_path(), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("key"):
                    out[r["key"]] = r
    except OSError:
        pass
    return out


def read_runs(skill=None, path=None):
    """-> [record] newest first (by start time), optionally one skill's."""
    runs = [r for r in read_log(path).values() if skill is None or r.get("skill") == skill]
    runs.sort(key=lambda r: (str(r.get("ts") or ""), str(r.get("key"))), reverse=True)
    return runs


def by_skill(runs=None):
    out = {}
    for r in (read_runs() if runs is None else runs):
        out.setdefault(r.get("skill"), []).append(r)
    return out


def load_state():
    try:
        with open(state_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(d):
    try:
        os.makedirs(base_dir(), exist_ok=True)
        tmp = state_path() + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(d, fh, sort_keys=True)
        os.replace(tmp, state_path())
    except OSError:
        pass


def record_profile_savings(records):
    """0.22.0: one `model-profile` line in the savings ledger for each FINISHED run that ran on a different
    model than the session was on: its tokens at its own model's list price against the same tokens at the
    session model's price, `estimated`. The skill name is a gt skill's; no argument or text goes in. A run
    still open, one on the session model, or one with no usage records nothing. Never raises."""
    try:
        import gt_expiry_guard
        import gt_ledger
    except Exception:                                    # noqa: BLE001
        return 0
    n = 0
    for r in records:
        try:
            run_m, ses_m = r.get("model"), r.get("session_model")
            if r.get("outcome") == "open" or not run_m or not ses_m or run_m == ses_m or not r.get("eq"):
                continue
            ratio = gt_expiry_guard.price_for(run_m) / gt_expiry_guard.price_for(ses_m)
            if gt_ledger.record_event(
                    "model-profile", round(r["eq"] * ratio), r["eq"], "estimated",
                    note="skill %s ran on %s while the session was on %s; tokens at the list-price ratio"
                         % (str(r.get("skill"))[:40], run_m, ses_m),
                    key="model-profile:%s" % r["key"], session=str(r.get("session") or "")[:8]):
                n += 1
        except Exception:                                # noqa: BLE001
            continue
    return n


def _append(records):
    if not records:
        return 0
    os.makedirs(base_dir(), exist_ok=True)
    with open(log_path(), "a", encoding="utf-8", newline="\n") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
    return len(records)


def compact():
    """Keep the newest line per key inside RETAIN_DAYS. Only when the log has grown past
    COMPACT_BYTES, and only under the catch-up's lock."""
    try:
        if os.path.getsize(log_path()) < COMPACT_BYTES:
            return False
    except OSError:
        return False
    floor = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=RETAIN_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    keep = [r for r in read_log().values() if str(r.get("ts") or "") >= floor]
    keep.sort(key=lambda r: (str(r.get("ts") or ""), str(r.get("key"))))
    tmp = log_path() + ".%d.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for r in keep:
            fh.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
    os.replace(tmp, log_path())
    return True


def _take_lock():
    path = lock_path()
    os.makedirs(base_dir(), exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) < LOCK_STALE_S:
                    return False
                os.remove(path)
            except OSError:
                return False
        except OSError:
            return False
    return False


def _drop_lock():
    try:
        os.remove(lock_path())
    except OSError:
        pass


# -- the catch-up ------------------------------------------------------------------------------
def transcript_files(projects_dir, since_epoch):
    """-> transcripts written at or after `since_epoch`, newest first. A subagent's transcript is
    not a skill run (its rows are sidechain rows), so those folders are not opened."""
    out = []
    for p in glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True):
        if os.sep + "subagents" + os.sep in p:
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_mtime >= since_epoch:
            out.append((st.st_mtime, st.st_size, p))
    return sorted(out, reverse=True)


def catch_up(projects_dir=None, days=CATCHUP_DAYS, max_seconds=MAX_SECONDS, now=None,
             scan_timeout=SCAN_TIMEOUT):
    """Read the transcripts that changed since last time and append what they add. -> a summary dict.
    Does nothing (and says why) when the setting is off. Never raises."""
    out = {"enabled": enabled(), "files": 0, "new": 0, "updated": 0, "excluded": 0, "scan": "n/a",
           "busy": False}
    if not out["enabled"]:
        return out
    if not _take_lock():
        out["busy"] = True
        return out
    try:
        projects = projects_dir or os.path.join(home(), ".claude", "projects")
        now = time.time() if now is None else now
        since = datetime.datetime.fromtimestamp(now - days * 86400, datetime.timezone.utc)
        deadline = time.monotonic() + max_seconds
        state = load_state()
        files = state.get("files") if isinstance(state.get("files"), dict) else {}
        excluded = state.get("excluded") if isinstance(state.get("excluded"), dict) else {}
        have = read_log()
        cand, sigs = {}, {}
        for n, (mtime, size, path) in enumerate(transcript_files(projects, now - days * 86400)):
            if n and time.monotonic() > deadline:
                break
            fid = _opaque(os.path.relpath(path, projects).replace(os.sep, "/"))
            sig = [size, int(mtime * 1000)]
            if files.get(fid) == sig:
                continue
            out["files"] += 1
            idle = now - mtime > IDLE_S
            unfinished = False
            for run in runs_from_file(path, since):
                sid = os.path.splitext(os.path.basename(path))[0][:8]
                rec = record_of(run, sid, _opaque(os.path.relpath(path, projects).split(os.sep)[0], 8),
                                idle)
                unfinished = unfinished or rec["outcome"] == "open"
                if rec["key"] in excluded:
                    continue
                prev = cand.get(rec["key"])
                if prev is None or (rec["tokens_total"], rec["turns"]) >= \
                        (prev["tokens_total"], prev["turns"]):
                    cand[rec["key"]] = rec
            if not unfinished:                # a run still open is read again until it ends or goes idle
                sigs[fid] = sig
        fresh = {}
        for key, rec in cand.items():
            old = have.get(key)
            if old and all(old.get(k) == rec.get(k) for k in
                           ("eq", "turns", "outcome", "model", "effort", "digest")):
                continue
            if old and (old.get("turns") or 0) > rec["turns"]:
                continue                     # a run only grows: a shorter copy (a fork) is not newer
            fresh[key] = rec
        scan_texts = {k: r["digest"] for k, r in fresh.items()
                      if r["digest"] and not (have.get(k) and have[k].get("digest") == r["digest"])}
        flagged, ran = intake_scan(scan_texts, scan_timeout)
        out["scan"] = "ok" if ran else "could not run"
        if not ran and scan_texts:
            # nothing the scan could not vouch for is written, and the files are not marked read,
            # so the next catch-up tries them again
            sigs = {}
            fresh = {k: r for k, r in fresh.items() if k not in scan_texts}
        write = []
        for key, rec in sorted(fresh.items(), key=lambda kv: kv[1]["ts"]):
            if key in flagged:
                excluded[key] = {"ts": rec["ts"] or now_iso(), "rules": sorted(set(flagged[key]))}
                out["excluded"] += 1
                continue
            write.append(rec)
            out["updated" if key in have else "new"] += 1
        _append(write)
        record_profile_savings(write)
        files.update(sigs)
        floor = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=RETAIN_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        excluded = {k: v for k, v in excluded.items() if str(v.get("ts") or "") >= floor}
        state.update(v=SCHEMA, files=files, excluded=excluded, updated=now_iso(),
                     scan=out["scan"])
        save_state(state)
        compact()
    except Exception as exc:                             # noqa: BLE001 - a hook never fails
        out["error"] = exc.__class__.__name__
    finally:
        _drop_lock()
    return out


def excluded_count():
    """How many digests the intake scan has kept out of the log (rules only are held, never text)."""
    ex = load_state().get("excluded")
    return len(ex) if isinstance(ex, dict) else 0


def status():
    runs = read_runs()
    per = {}
    for r in runs:
        e = per.setdefault(r.get("skill"), {"runs": 0, "completed": 0, "last": None})
        e["runs"] += 1
        e["completed"] += r.get("outcome") == "completed"
        e["last"] = e["last"] or r.get("ts")
    st = load_state()
    return {"enabled": enabled(), "log": log_path(), "records": len(runs), "skills": per,
            "excluded_intake": excluded_count(), "last_catch_up": st.get("updated"),
            "scan": st.get("scan"), "min_runs": min_runs()}


# -- command line ------------------------------------------------------------------------------
def cmd_catch_up(a):
    if a.hook:
        try:
            payload = json.load(sys.stdin)
        except Exception:                                # noqa: BLE001
            payload = {}
        if isinstance(payload, dict) and payload.get("source") in ("compact", "clear"):
            return 0
    r = catch_up(a.projects_dir, a.days, a.max_seconds,
                 scan_timeout=HOOK_SCAN_TIMEOUT if a.hook else SCAN_TIMEOUT)
    if a.hook:
        return 0
    if not r["enabled"]:
        print("skill_runlog is off (gt_settings.py set skill_runlog on): nothing was read or recorded")
    elif r["busy"]:
        print("another catch-up is running; nothing done")
    else:
        print("skill run log: %d new, %d updated, %d excluded by the intake scan, from %d changed "
              "transcript(s); intake scan %s" % (r["new"], r["updated"], r["excluded"], r["files"],
                                                 r["scan"]))
    return 0


def cmd_status(a):
    s = status()
    if a.json:
        print(json.dumps(s, indent=1, sort_keys=True))
        return 0
    print("skill_runlog: %s   log: %s" % ("on" if s["enabled"] else "off", s["log"]))
    print("%d record(s) for %d skill(s); %d digest(s) excluded by the intake scan; last catch-up %s"
          % (s["records"], len(s["skills"]), s["excluded_intake"], s["last_catch_up"] or "never"))
    for name, e in sorted(s["skills"].items(), key=lambda kv: -kv[1]["runs"]):
        print("  %-24s %3d run(s), %3d completed   (%d needed to calibrate)"
              % (name, e["runs"], e["completed"], s["min_runs"]))
    return 0


def cmd_runs(a):
    runs = read_runs(a.skill)
    if a.json:
        print(json.dumps(runs, indent=1, sort_keys=True))
        return 0
    for r in runs:
        print("%s  %-22s %-7s %-7s %-9s %6s tok  %s" % (
            r.get("ts"), r.get("skill"), r.get("model") or "-", r.get("effort") or "-",
            r.get("outcome"), r.get("tokens_total"), (r.get("digest") or "").split("\n")[0][:60]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_skill_runlog.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("catch-up", help="record the skill runs the transcripts hold that the log lacks")
    p.add_argument("--hook", action="store_true", help="SessionStart: silent, always exit 0")
    p.add_argument("--days", type=int, default=CATCHUP_DAYS)
    p.add_argument("--projects-dir")
    p.add_argument("--max-seconds", type=float, default=MAX_SECONDS)
    p.set_defaults(fn=cmd_catch_up)
    p = sub.add_parser("status", help="what the log holds")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("runs", help="the recorded runs, newest first")
    p.add_argument("--skill")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_runs)
    try:
        a = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if "--hook" in (argv if argv is not None else sys.argv[1:]) else int(exc.code or 0)
    try:
        return a.fn(a)
    except Exception:                                    # noqa: BLE001
        if getattr(a, "hook", False):
            return 0
        raise


if __name__ == "__main__":
    sys.exit(main())
