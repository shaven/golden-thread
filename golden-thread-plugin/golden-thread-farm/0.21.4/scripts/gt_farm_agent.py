#!/usr/bin/env python3
"""gt_farm_agent.py -- the agent loop of gt-farm agent mode (gt 0.22.0, gt-farm 0.21.3, phase D). Standard library only.

An engine marked `agent: yes` in the service table is given a task, a COPY of a repo and four tools (list_dir,
read_file, write_file, run_command). It works until it stops or a guard stops it; the host then runs the tests and
the diff-scope check (gt_farm_accept) and returns the patch. Nothing is applied to your repo: the patch comes back
for you (or Claude) to review.

    gt_farm_agent.py plan --engine NAME --repo DIR --task-file F --test-command CMD [--allow GLOB]... [--deny GLOB]...
    gt_farm_agent.py run  --engine NAME --repo DIR --task-file F --test-command CMD [--allow GLOB]... [--deny GLOB]...
                          [--read-only-tests GLOB]... [--hidden-tests DIR] [--patch-out FILE] [--vault DIR] [--dry-run] [--json]

    gt_farm_agent.py farm --repo DIR --task-file F --test-command CMD [--hidden-tests DIR] [--task-class C] [--task-key K] [--engine NAME]
                          [--share-table F] [--state F] [--patch-out FILE] [--vault DIR] [--dry-run] [--json]
    gt_farm_agent.py audit KEY pass|fail [--share-table F] [--state F] [--vault DIR] [--dry-run]
    gt_farm_agent.py apply --repo DIR --patch FILE [--yes-apply] [--vault DIR] [--dry-run]

Common options: --services FILE (the service table), --max-steps N, --max-tokens N (per call), --wall-seconds N,
--max-test-runs N, --max-commands N, --max-calls-per-reply N, --usd-cap D, --context-tokens N.

`plan` prints, in plain words, what would be sent to whom, the worst-case cost and the limits; it sends nothing.
`run --dry-run` does the same checks `run` does (agent engine, sandbox, price row, secret scan) and stops before the
first model call; no workspace is made and no ledger line is written.

`farm` is the whole job in one command: the share rules (gt_farm_share.decide) say whether this task goes to the farm; if
not, it prints why and exits 5 so the caller does the task itself. If so it picks an engine (the share row's pin, else the
best calibrated agent engine), runs it, scores the outcome in the share state, saves an accepted patch (never applied) and
prints what happens next. A result picked for audit also gets a review file; `audit KEY pass|fail` closes it (a fail lowers
the class's share). `apply` checks a saved patch and, only with --yes-apply, applies it without committing.

Public function:
    farm_task(task_text, repo_dir, task_class, task_key, *, engine_name=None, table, state, calibrated, test_command, ...)
        -> {route, exit, status, why, engine, usd, patch_path, apply_check, audit, audit_key, review_path, lines[]}
    audit_result(key, passed, table, state, ...) -> (ok, sentence) | apply_patch_file(repo, patch, confirmed=False)
    pick_agent_engine(engines, vault, pin=None, record_fn=None) -> (engine|None, why, notes[])
    calibration_runner(post_fn=None, ...) -> runner(kind, spec, engine) for gt_farm_calibrate.py --kind agent-code
    run_agent(task_text, repo_dir, engine, *, test_command, allow=(), deny=(), read_only_tests=(), limits=None,
              evaluate_fn=None, post_fn=None, workdir_parent=None, sleep_fn=None, clock_fn=None, vault=None,
              dry_run=False, hidden_tests=None)
        -> {status: accepted|rejected|failed|refused, why, patch, steps, tokens{in,out,reasoning}, usd, seconds,
            changed_files, events[]}
    refusal_reason(task_text, engine, vault=None) -> "" or the plain reason the run is refused
    plan_text(engine, repo_dir, limits, ...) -> list of plain sentences

What leaves the machine: the task text, the system prompt, every file the model reads and every command output (all
scrubbed first by gt_farm_egress). Tool results are never logged or kept; `events` carry counts and tool names only.
Guards: max_steps (60), max_tokens per call (40000: reasoning models burn tokens), wall-clock (600 s), max_test_runs
(12; any run_command that contains the test command or a test-runner word such as unittest, pytest, tox, npm test,
cargo test or go test counts, and past the cap it is refused), max_commands (60 run_command calls in all; the test-run cap is a COURTESY, not a bound:
`sh t.sh`, runpy or a shell loop can run tests without being counted, so the REAL bounds are wall-clock, the total
command count and spend),
max_calls_per_reply (8; the extra calls of one reply are answered with an error, not run), wall-clock and spend are
re-checked before EACH tool call (a reply can hold many, each up to command_timeout), the context window, a USD cap for the run (limits
usd_cap, else the setting farm_agent_budget_usd_day), an empty reply is retried 3 times then fails, a reply cut by the
token cap with no tool call counts as empty, a 429 is waited out (Retry-After) up to 3 times, an open circuit fails
the run. Unknown tools and bad arguments go back to the model as error text.

One aggregated ledger line is written per run (task_class agent-code).

THE BOUNDARY IS THE SANDBOX. The command-word check in run_command (_command_denied: a word naming a key file or a
.env is refused) is defence in depth only: a shell can build any path another way, so it is NOT a guarantee and
nothing may rely on it. What keeps a command away from your secrets is the sandbox (no network, no reads of the home
folder, no writes outside the copy), and the run is refused when no isolating backend exists.

HELD-OUT TESTS. A model that can see the tests can write code that passes them without being right (a forged pass).
Tests the model never sees are the strongest defence: pass `hidden_tests` (a dict {relative path: file content}, or a
folder whose files are copied in by relative path; CLI --hidden-tests DIR on run and farm). They are put into the
copy only after the model has stopped, the same test command runs again and must pass, and they are removed again, so
they never reach the model or the patch. Where a project has such tests, the share ramp should rely on them: use
them for every farmed task of that project.

Exit codes: 0 accepted (or plan / --dry-run with nothing refusing) | 1 usage or I/O error | 2 refused (reason printed)
| 3 rejected (the tests or the diff scope did not pass; the patch is still available) | 4 failed (a guard stopped the
run, or the engine failed) | 5 (`farm` only) the task stays with Claude: nothing was sent.
"""
import argparse
import contextlib
import json
import os
import re
import secrets
import shlex
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_farm_adapters as ADP     # noqa: E402
import gt_farm_egress as EG        # noqa: E402
import gt_farm_workspace as W      # noqa: E402

GT_VERSION = "0.21.4"
TASK_CLASS = "agent-code"

DEFAULT_LIMITS = {
    "max_steps": 60,
    "max_tokens": 40000,          # per call
    "wall_seconds": 600.0,
    "max_test_runs": 12,          # runs of the test command (or of any test runner) by run_command
    "max_commands": 60,           # run_command calls in all
    "max_calls_per_reply": 8,     # tool calls executed from ONE model reply; the rest get an error message
    "max_empty_retries": 3,
    "max_rate_waits": 3,
    "command_timeout": 120.0,     # one run_command
    "usd_cap": None,              # None: farm_agent_budget_usd_day, else no cap
    "context_tokens": None,       # None: the engine's calibrated or profile window, else unchecked
    "test_timeout": 300,          # the host's own test run at the end
}

TOOLS = [
    {"type": "function", "function": {
        "name": "list_dir", "description": "List a folder in the workspace copy.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a text file in the workspace copy. Use offset to continue a long file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Create or overwrite a text file in the workspace copy.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_command", "description": "Run a shell command in the workspace copy (sandboxed, no network). "
                                              "Returns the exit code and output.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
]
TOOL_NAMES = tuple(t["function"]["name"] for t in TOOLS)

SYSTEM_TEMPLATE = (
    "You are a software engineer working on a task in a COPY of a code repository. Nothing you do touches the "
    "original; your changes come back as a patch for review. Paths are relative to the copy's root.\n"
    "Tools: list_dir, read_file, write_file, run_command (sandboxed, no network).\n"
    "Run the tests with: {test_command}\n"
    "Run them to see the failure first and again after each change. Do NOT edit the tests or anything under: {frozen}. "
    "Fix the code, not the tests. Keep the change as small as the task allows.\n"
    "When the tests pass, stop: reply with a short summary of what you changed and make no tool call.")


class Refused(Exception):
    """The run may not start; the message is the plain reason."""


# ---- small helpers -------------------------------------------------------------------------------

def _S():
    """gt_farm_send, imported on first use (it reads HOME-dependent config)."""
    import gt_farm_send as S
    return S


def _limits(limits):
    out = dict(DEFAULT_LIMITS)
    for k, v in (limits or {}).items():
        if k in out and v is not None:
            out[k] = v
    return out


def _usd_cap(lim):
    if lim.get("usd_cap") is not None:
        return float(lim["usd_cap"])
    try:
        v = str(_S().setting("farm_agent_budget_usd_day", "off")).strip().lower()
        if v in ("off", "", "0", "-", "none"):
            return None
        f = float(v)
        return f if f > 0 else None
    except Exception:                                        # noqa: BLE001 -- no setting is no cap
        return None


def _sandbox_backend():
    """-> the isolating backend's name, or "" when none is usable (commands would run unisolated)."""
    try:
        launch = W._launcher()
        if launch is None:
            return ""
        b = launch.available_backends()[0]
        return "" if b == "none" else str(b)
    except Exception:                                        # noqa: BLE001
        return ""


def _chars(messages):
    n = 0
    for m in messages:
        n += len(str(m.get("content") or "")) + len(str(m.get("reasoning_content") or ""))
        for c in m.get("tool_calls") or []:
            n += len(json.dumps(c))
    return n


def _norm_cmd(cmd):
    return " ".join(str(cmd or "").split())


_RUNNER_WORDS = ("unittest", "pytest", "py.test", "tox", "nose", "nosetests", "nose2", "npm test", "yarn test",
                 "pnpm test", "cargo test", "go test", "mvn test", "gradle test", "make test", "rspec", "phpunit")
_INTERPRETERS = ("python", "python3", "py", "node", "sh", "bash", "zsh", "env", "sudo", "time", "cmd", "powershell",
                 "pwsh")


def _runner_tokens(test_command):
    """-> the words that make a command a test run: the usual runners plus whatever the registered test command itself
    names (the word after -m, or its first words when the first is not a bare interpreter)."""
    toks = set(_RUNNER_WORDS)
    words = _norm_cmd(test_command).lower().split()
    for i, w in enumerate(words):
        if w == "-m" and i + 1 < len(words):
            toks.add(words[i + 1])
    if words:
        first = os.path.basename(words[0].replace("\\", "/"))
        if first.endswith(".exe"):
            first = first[:-4]
        if first and first not in _INTERPRETERS and not first.startswith("python"):
            if len(words) == 1:
                toks.add(first)                              # a bare runner such as pytest or tox
            elif not words[1].startswith("-"):
                toks.add(first + " " + words[1])             # npm test, cargo test: not every npm command
    return toks


def _is_test_run(cmd, test_cmd, tokens):
    """True when the (normalised) command contains the registered test command or a test-runner word."""
    c = _norm_cmd(cmd)
    if test_cmd and test_cmd in c:
        return True
    low = c.lower()
    # `python3 -munittest` / `-mpytest` (no space): the word boundary below would miss them.
    if re.search(r"(?<![\w-])-m\s*(?:unittest|pytest|py\.test|nose2?|tox)(?![\w-])", low):
        return True
    for t in tokens:
        pat = r"(?<![\w./-])" + r"\s+".join(re.escape(x) for x in t.split()) + r"(?![\w/-])(?!\.py)"
        if re.search(pat, low):
            return True
    return False


def _context_window(engine, lim, vault):
    """-> usable context tokens for the run, or None when unknown."""
    if lim.get("context_tokens"):
        return int(lim["context_tokens"])
    try:
        n, _src = _S().context_limit(engine, vault)
    except Exception:                                        # noqa: BLE001
        return None
    if not n:
        return None
    try:
        reserve = int(_S().reply_reserve_tokens())
    except Exception:                                        # noqa: BLE001
        reserve = 2000
    return max(0, int(n) - reserve)


# ---- refusal -------------------------------------------------------------------------------------

def refusal_reason(task_text, engine, vault=None):
    """-> "" when the run may start, else the plain reason it may not. Checks, in order: the engine is marked agent
    and its API can act as one, a sandbox backend isolates commands, a hosted engine has a price row (its spend must
    be measurable), the task text passes the secret scan and the release gate."""
    if not engine.get("agent"):
        return "engine %s is not marked agent: yes in the service table, so it will not be given tools." % engine.get("name")
    try:
        capable = ADP.agent_capable(engine.get("api"))
    except ADP.UnknownApi as e:
        return str(e)
    if not capable:
        return "engine %s speaks %s, which cannot make tool calls; use an openai-chat engine." % (
            engine.get("name"), engine.get("api"))
    if not _sandbox_backend():
        return ("no isolating sandbox backend is usable on this machine, so the agent's commands would run with no "
                "isolation. Nothing was sent.")
    S = _S()
    if not engine.get("local"):
        if not S.price_row(engine.get("provider"), engine.get("model")):
            return ("engine %s has no price row (provider %r, model %r), so the run's cost could not be measured or "
                    "capped. Add the engine to the price table or name its provider. Nothing was sent."
                    % (engine.get("name"), engine.get("provider") or "", engine.get("model") or ""))
    code, msg = S.secret_refusal([("task text", task_text)], vault)
    if code:
        return msg
    ok, msg, _body, _restore = S.release(task_text, engine, vault)
    if not ok:
        return msg
    return ""


# ---- tools ---------------------------------------------------------------------------------------

def _command_denied(command, engine):
    """DEFENCE IN DEPTH, NOT A GUARANTEE: a reason when a command word names a file the agent may not read (a key, a
    .env), else "". A shell can spell the same path in countless ways (globs, variables, quoting, a script it wrote),
    so this catches only the plain mistake. The sandbox (no network, no reads outside the copy) is the boundary."""
    extra = ()
    if engine.get("key_file"):
        extra = (os.path.basename(str(engine["key_file"])),)
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    for w in words:
        if not w or w.startswith("-") or "=" in w and "/" not in w:
            continue
        why = EG.deny_read(w, extra)
        if why:
            return why
    return ""


class _Tools(object):
    def __init__(self, workspace, engine, test_command, read_only, lim, run_fn):
        self.ws, self.engine, self.lim = workspace, engine, lim
        self.test_cmd = _norm_cmd(test_command)
        self.read_only = tuple(read_only)
        self.test_runs = 0
        self.commands = 0
        self.runner_tokens = _runner_tokens(test_command)
        self.run_fn = run_fn
        self.extra = (os.path.basename(str(engine["key_file"])),) if engine.get("key_file") else ()

    def execute(self, name, args):
        """-> (raw result text). Never raises."""
        try:
            if name == "list_dir":
                return W.list_dir(self.ws, args)
            if name == "read_file":
                why = EG.deny_read(args.get("path"), self.extra)
                return "error: " + why if why else W.read_file(self.ws, args)
            if name == "write_file":
                why = EG.deny_read(args.get("path"), self.extra)
                return "error: " + why if why else W.write_file(self.ws, args, read_only=self.read_only)
            if name == "run_command":
                cmd = args.get("command")
                if isinstance(cmd, str):
                    if self.commands >= int(self.lim["max_commands"]):
                        return ("error: refused: %d commands have already run, the limit for this task. Stop and "
                                "reply with a short summary of where it stands." % self.commands)
                    self.commands += 1
                    why = _command_denied(cmd, self.engine)
                    if why:
                        return "error: refused: " + why
                    if _is_test_run(cmd, self.test_cmd, self.runner_tokens):
                        if self.test_runs >= int(self.lim["max_test_runs"]):
                            return ("error: refused: the test command has already run %d times, the limit for this "
                                    "task. Stop and reply with a short summary of where it stands." % self.test_runs)
                        self.test_runs += 1
                return self.run_fn(self.ws, args, timeout=float(self.lim["command_timeout"]))
        except Exception as e:                               # noqa: BLE001 -- a tool never crashes the loop
            return "error: the tool failed (%s)" % type(e).__name__
        return "error: unknown tool %r; the tools are %s" % (name, ", ".join(TOOL_NAMES))


# ---- the wire ------------------------------------------------------------------------------------

def _default_post(engine):
    """-> post_fn(engine, payload, timeout) -> (http code, reply dict). Honours the gateway circuit breaker (an open
    circuit or a dead connection raises EngineDown). A 429 is RETURNED, not waited out: the loop backs off so one
    injectable sleep serves every wait."""
    S = _S()
    key = S.load_key(engine)

    def post(eng, payload, timeout):
        import socket
        import urllib.error
        svc = S.breaker_key(eng)
        allowed, why, retry = S.GW.should_call(svc)
        if not allowed:
            raise S.EngineDown("%s: circuit open (%s), next try in %d s" % (eng["name"], why, retry), "circuit_open")
        started = time.time()
        try:
            code, resp = S.post(eng, payload, timeout, key)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            S._note_down(eng)
            S.GW.record_result(svc, "timeout" if isinstance(getattr(e, "reason", e), (TimeoutError, socket.timeout))
                               else "unreachable", int((time.time() - started) * 1000))
            raise S.EngineDown("%s: %s" % (eng["name"], getattr(e, "reason", e)))
        ms = int((time.time() - started) * 1000)
        if code >= 500:
            S._note_down(eng)
            S.GW.record_result(svc, "http_5xx", ms)
            raise S.EngineDown("%s: HTTP %d" % (eng["name"], code))
        if code == 429 or code != 200:
            S.GW.record_result(svc, "client_error", ms)
        else:
            u = resp.get("usage") or {}
            S.GW.record_result(svc, "ok", ms, int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0))
        return code, resp
    return post


def _call_cost(engine, r):
    """-> dollars one reply cost: the provider's own figure when the reply carries one, else the price row; 0 local."""
    if engine.get("local"):
        return 0.0
    if r.get("cost_usd") is not None:
        return float(r["cost_usd"])
    spent = _S().usd_spent(engine, r.get("model"), int(r.get("in_tokens") or 0), int(r.get("out_tokens") or 0))
    return float(spent) if spent is not None else 0.0


# ---- held-out tests ------------------------------------------------------------------------------

def _hidden_problem(hidden_tests):
    """-> "" when `hidden_tests` is usable (None, a {relpath: content} dict, or an existing non-empty folder), else the
    plain reason it is not. Checked before anything is sent."""
    if not hidden_tests:
        if isinstance(hidden_tests, str):
            return "the held-out tests folder was given as an empty name. Nothing was sent."
        return ""
    if isinstance(hidden_tests, dict):
        for k, v in hidden_tests.items():
            norm = str(k).replace("\\", "/")
            if (not isinstance(k, str) or not norm.strip("/") or norm.startswith("/") or ".." in norm.split("/")
                    or (len(norm) > 1 and norm[1] == ":") or not isinstance(v, (str, bytes))):
                return "a held-out test path (%r) must be a relative path inside the repo with text content. Nothing was sent." % (k,)
        return ""
    if isinstance(hidden_tests, (str, os.PathLike)):
        d = os.fspath(hidden_tests)
        if not os.path.isdir(d):
            return "the held-out tests folder %s does not exist. Nothing was sent." % d
        if not _hidden_files(d):
            return "the held-out tests folder %s holds no files. Nothing was sent." % d
        return ""
    return "hidden_tests must be a dict {path: content} or a folder. Nothing was sent."


def _hidden_files(folder):
    out = {}
    for dp, dns, fns in os.walk(folder):
        dns[:] = [d for d in dns if not os.path.islink(os.path.join(dp, d))]
        for n in fns:
            full = os.path.join(dp, n)
            if os.path.isfile(full) and not os.path.islink(full):
                out[os.path.relpath(full, folder).replace(os.sep, "/")] = full
    return out


def _hidden_materialize(hidden_tests, parent):
    """-> ({relpath: file path} for gt_farm_accept.evaluate, temp folder to delete or None)."""
    if not hidden_tests:
        return {}, None
    if isinstance(hidden_tests, dict):
        d = tempfile.mkdtemp(prefix="gt-farm-hidden-", dir=parent)
        out = {}
        for rel, content in sorted(hidden_tests.items()):
            rel = str(rel).replace("\\", "/")
            full = os.path.join(d, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(content if isinstance(content, bytes) else content.encode("utf-8"))
            out[rel] = full
        return out, d
    return _hidden_files(os.fspath(hidden_tests)), None


# ---- the loop ------------------------------------------------------------------------------------

def run_agent(task_text, repo_dir, engine, *, test_command, allow=(), deny=(), read_only_tests=(), limits=None,
              evaluate_fn=None, post_fn=None, workdir_parent=None, sleep_fn=None, clock_fn=None, vault=None,
              dry_run=False, run_fn=None, ledger_fn=None, hidden_tests=None):
    """Run `engine` as a tool-using agent on a copy of `repo_dir`. See the module docstring. `hidden_tests` is a dict
    {relative path: content} or a folder of held-out tests, run by the acceptance check only (never shown to the model). `evaluate_fn`,
    `post_fn(engine, payload, timeout) -> (code, reply)`, `sleep_fn`, `clock_fn` and `run_fn` (the command runner)
    default to the real ones; tests inject fakes. -> the result dict."""
    lim = _limits(limits)
    clock = clock_fn or time.monotonic
    sleep = sleep_fn or time.sleep
    started = clock()
    res = {"status": "failed", "why": "", "patch": "", "steps": 0, "tokens": {"in": 0, "out": 0, "reasoning": 0},
           "usd": 0.0, "seconds": 0.0, "changed_files": [], "events": []}

    def ev(name, **kw):
        d = {"event": name}
        d.update(kw)
        res["events"].append(d)

    def done(status, why):
        res["status"], res["why"] = status, why
        res["seconds"] = round(clock() - started, 2)
        res["usd"] = round(res["usd"], 6)
        return res

    why = refusal_reason(task_text, engine, vault)
    if why:
        ev("refused")
        return done("refused", why)
    bad = _hidden_problem(hidden_tests)
    if bad:
        ev("refused")
        return done("refused", bad)
    if dry_run:
        ev("dry-run")
        return done("refused", "dry run: every check passed; nothing was sent and no workspace was made.")

    S = _S()
    _ok, _msg, task_sent, _restore = S.release(task_text, engine, vault)
    adapter = ADP.get(engine.get("api"))
    cap = _usd_cap(lim)
    window = _context_window(engine, lim, vault)
    parent = workdir_parent or tempfile.gettempdir()
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="gt-farm-agent-", dir=parent)
    ws = os.path.join(tmp, "work")
    try:
        try:
            W.make_workspace(repo_dir, ws)
        except W.WorkspaceError as e:
            return done("refused", str(e))
        if post_fn is None:
            try:
                post_fn = _default_post(engine)
            except Exception as e:                           # noqa: BLE001
                return done("refused", "the engine's key could not be read (%s). Nothing was sent." % e)
        # The model's own test runs must not leave bytecode behind: it would show up as an out-of-scope change (the
        # host's final test run already sets this, gt_farm_accept._run_tests).
        runner = run_fn or (lambda w, args, timeout=30.0: W.run_command(
            w, args, timeout=timeout, env={"PYTHONDONTWRITEBYTECODE": "1"}))
        frozen = tuple(read_only_tests) + tuple(deny)
        tools = _Tools(ws, engine, test_command, frozen, lim, runner)
        system = SYSTEM_TEMPLATE.format(test_command=test_command, frozen=", ".join(frozen) or "(the test files)")
        system, _sys_notes = EG.scrub_tool_result(system, engine, ws)   # test command and globs may hold home paths
        messages = [{"role": "system", "content": system}, {"role": "user", "content": task_sent}]
        extras = EG.provider_extras(engine)
        empties = waits = 0
        stop_why = None
        calls_n = 0
        final_text = ""

        while stop_why is None:
            if res["steps"] >= int(lim["max_steps"]):
                stop_why = "step limit reached (%d steps) before the model finished" % lim["max_steps"]
                break
            if clock() - started >= float(lim["wall_seconds"]):
                stop_why = "wall-clock limit reached (%d s)" % lim["wall_seconds"]
                break
            if cap is not None and res["usd"] >= cap:
                stop_why = "spend cap reached ($%.4f of $%.2f)" % (res["usd"], cap)
                break
            if window is not None and _chars(messages) // 4 > window:
                stop_why = ("the conversation (about %d tokens) would no longer fit the engine's window of %d tokens"
                            % (_chars(messages) // 4, window))
                break
            params = {"temperature": 0, "max_tokens": int(lim["max_tokens"]), "model": engine.get("model") or None,
                      "tools": TOOLS, "tool_choice": "auto"}
            payload = adapter.build(messages, params)
            if adapter.model_in_path:
                payload.pop("model", None)
            for k, v in extras.items():
                if isinstance(v, dict) and isinstance(payload.get(k), dict):
                    payload[k].update(v)
                else:
                    payload[k] = v
            remaining = max(5.0, float(lim["wall_seconds"]) - (clock() - started))
            try:
                code, resp = post_fn(engine, payload, min(400.0, remaining))
            except S.RateLimited as e:
                waits += 1
                if waits > int(lim["max_rate_waits"]):
                    stop_why = "the engine kept rate limiting (HTTP 429)"
                    break
                sleep(min(float(getattr(e, "retry_after_s", 1) or 1), 30.0))
                ev("rate-wait")
                continue
            except S.EngineDown as e:
                ev("engine-down", kind=getattr(e, "kind", None) or "down")
                stop_why = "the engine is unavailable (%s)" % (str(e)[:160] or "down")
                break
            if code == 429:
                waits += 1
                if waits > int(lim["max_rate_waits"]):
                    stop_why = "the engine kept rate limiting (HTTP 429)"
                    break
                sleep(float(S.retry_after_s(resp if isinstance(resp, dict) else {})))
                ev("rate-wait")
                continue
            if code != 200:
                stop_why = "the engine answered HTTP %s" % code
                break
            try:
                r = adapter.parse_full(resp)
            except ADP.ReplyShape as e:
                stop_why = "the engine's reply was not in the expected shape (%s)" % str(e)[:100]
                break
            res["steps"] += 1
            res["tokens"]["in"] += int(r.get("in_tokens") or 0)
            res["tokens"]["out"] += int(r.get("out_tokens") or 0)
            res["tokens"]["reasoning"] += int(r.get("reasoning_tokens") or 0)
            res["usd"] += _call_cost(engine, r)
            calls = r.get("tool_calls") or []
            text = r.get("text") or ""
            finish = (r.get("finish_reason") or "").lower()
            cut = finish == "length" and not calls
            ev("reply", step=res["steps"], tool_calls=len(calls), finish=finish or "unknown")
            if cut or (not calls and not text.strip()):
                empties += 1
                ev("empty-reply", cut=bool(cut))
                if empties > int(lim["max_empty_retries"]):
                    stop_why = ("the engine returned %s %d times in a row" % (
                        "a reply cut by the token cap" if cut else "an empty reply", empties))
                    break
                continue
            empties = 0
            if not calls:
                final_text = text
                break
            asst = {"role": "assistant", "content": text or None, "tool_calls": [
                {"id": c["id"] or "call_%d" % (calls_n + i), "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"]}} for i, c in enumerate(calls)]}
            if r.get("reasoning"):
                asst["reasoning_content"] = r["reasoning"]
            messages.append(asst)
            per_reply = max(1, int(lim["max_calls_per_reply"]))
            for i, c in enumerate(calls):
                calls_n += 1
                # One reply can hold many tool calls, each up to command_timeout: the caps are re-checked before EACH.
                if clock() - started >= float(lim["wall_seconds"]):
                    stop_why = "wall-clock limit reached (%d s) while running tool calls" % lim["wall_seconds"]
                    break
                if cap is not None and res["usd"] >= cap:
                    stop_why = "spend cap reached ($%.4f of $%.2f) while running tool calls" % (res["usd"], cap)
                    break
                if i >= per_reply:
                    out = "error: too many tool calls in one reply: %d allowed" % per_reply
                else:
                    try:
                        args = json.loads(c["arguments"] or "{}")
                        if not isinstance(args, dict):
                            raise ValueError("not an object")
                        out = tools.execute(c["name"], args)
                    except ValueError:
                        out = "error: the arguments for %s were not valid JSON (an object was expected)" % (
                            c["name"] or "?")
                clean, notes = EG.scrub_tool_result(out, engine, ws)
                ev("tool", step=res["steps"], tool=c["name"] if c["name"] in TOOL_NAMES else "unknown",
                   error=str(out).startswith("error:"), scrubbed=len(notes))
                messages.append({"role": "tool", "tool_call_id": asst["tool_calls"][i]["id"], "content": clean})

        try:
            res["patch"] = W.take_patch(ws)
            res["changed_files"] = W.changed_files(ws)
        except Exception:                                    # noqa: BLE001
            pass
        if stop_why:
            ev("stopped")
            return done("failed", stop_why)
        ev("final", chars=len(final_text))
        if evaluate_fn is None:
            import gt_farm_accept
            evaluate_fn = gt_farm_accept.evaluate
        hid_tmp = None
        try:
            # Held-out files exist only from here on: the model has stopped and cannot read them.
            hid, hid_tmp = _hidden_materialize(hidden_tests, parent)
            ev_res = evaluate_fn(ws, test_command=test_command, allow=tuple(allow), deny=tuple(deny),
                                 read_only_tests=tuple(read_only_tests), hidden_tests=hid or None,
                                 timeout=int(lim["test_timeout"]))
        except Exception as e:                               # noqa: BLE001 -- no verdict is not acceptance
            return done("failed", "the acceptance check could not run (%s)" % type(e).__name__)
        finally:
            if hid_tmp:
                W.rmtree(hid_tmp)
        if ev_res.get("patch"):
            res["patch"] = ev_res["patch"]
        if ev_res.get("changed_files") is not None:
            res["changed_files"] = list(ev_res["changed_files"])
        ev("evaluated", ok=bool(ev_res.get("ok")), tests_ran=ev_res.get("tests_ran"),
           tests_failed=ev_res.get("tests_failed"), out_of_scope=len(ev_res.get("out_of_scope") or []))
        if ev_res.get("ok"):
            return done("accepted", ev_res.get("why") or "tests pass and the change stays in scope")
        return done("rejected", ev_res.get("why") or "the acceptance check did not pass")
    finally:
        W.rmtree(tmp)
        if not res["seconds"]:
            res["seconds"] = round(clock() - started, 2)
        if res["status"] != "refused" or res["steps"]:
            _write_ledger(engine, res, ledger_fn)


# ---- the ledger ----------------------------------------------------------------------------------

def ledger_record(engine, res):
    """-> the ONE aggregated ledger line for a run (S.ledger_line, so the format and file are the farm's own)."""
    S = _S()
    r = {"prompt_tokens": res["tokens"]["in"], "completion_tokens": res["tokens"]["out"], "wall": res["seconds"],
         "model": engine.get("model") or ""}
    ok = res["status"] == "accepted"
    line = S.ledger_line(engine, "agent-task", r, "measured", task_class=TASK_CLASS, agent=True, ok=ok,
                         why=res["why"][:240], steps=res["steps"], reasoning_tokens=res["tokens"]["reasoning"],
                         usd_spent=round(res["usd"], 6), outcome=res["status"])
    if not engine.get("local"):
        line["provider"] = engine.get("provider") or line.get("provider", "")
    if ok:
        line["needs_review"] = True                          # an accepted patch is a candidate: a person reviews it
    if not ok:
        tokens = r["prompt_tokens"] + r["completion_tokens"]
        line.update(status="failed", usd_avoided=0.0, counterfactual_eq=0, actual_eq=0 if engine.get("local") else tokens)
    return line


def _write_ledger(engine, res, ledger_fn=None):
    try:
        S = _S()
        if not S.ledger_on():
            return
        line = ledger_record(engine, res)
        (ledger_fn or S.append_ledger)(line)
    except Exception:                                        # noqa: BLE001 -- the ledger never fails a run
        pass


# ---- the plan ------------------------------------------------------------------------------------

def cost_ceiling(engine, lim, window):
    """-> worst-case dollars for one run (every step at the full per-call cap and a full window), or None when the
    engine cannot be priced. 0.0 for a local engine. Capped by the run's spend cap when one is set."""
    if engine.get("local"):
        return 0.0
    row = _S().price_row(engine.get("provider"), engine.get("model"))
    if not row:
        return None
    try:
        steps = int(lim["max_steps"])
        win = window or 128000
        ceil = steps * (win * float(row["in_per_mtok"]) + int(lim["max_tokens"]) * float(row["out_per_mtok"])) / 1e6
    except (KeyError, TypeError, ValueError):
        return None
    cap = _usd_cap(lim)
    return min(ceil, cap) if cap is not None else ceil


def plan_text(engine, repo_dir, limits=None, test_command="", allow=(), deny=(), vault=None):
    """-> [plain sentence]: what would be sent to whom, the worst-case cost, the limits."""
    lim = _limits(limits)
    window = _context_window(engine, lim, vault)
    out = []
    out += EG.data_policy_summary(engine)
    out.append("A scrubbed copy of %s is made in a temporary folder (secrets, links, .git and caches are left out); "
               "the original is never changed." % repo_dir)
    out.append("The model receives the task text, the tool list, and whatever files it reads and commands it runs "
               "(each result is scrubbed for secrets and internal names first). It can read any file in the copy.")
    out.append("It may run commands in a sandbox with no network, reading and writing only inside the copy. "
               "No agent can start another agent.")
    out.append("When it stops, the host runs `%s` and checks the change stays inside allow=%s deny=%s. "
               "A patch comes back for review; nothing is applied." % (test_command or "(no test command)",
                                                                        list(allow) or "(anywhere)", list(deny) or "(none)"))
    ceil = cost_ceiling(engine, lim, window)
    cap = _usd_cap(lim)
    if engine.get("local"):
        out.append("Cost: $0 (a local engine).")
    elif ceil is None:
        out.append("Cost: cannot be estimated, the engine has no price row; the run is REFUSED until it has one.")
    else:
        out.append("Cost ceiling (worst case, not expected): about $%.2f%s." % (
            ceil, " (stopped by your spend cap of $%.2f)" % cap if cap is not None and ceil >= cap else ""))
    out.append("The cap on test runs is a courtesy: a script or a loop can run tests without being counted. The real "
               "bounds are the wall-clock limit, the total command count (%d) and the spend cap." % lim["max_commands"])
    out.append("Limits: %d steps, %d tokens per call, %d s wall-clock, %d runs of the test command, context window %s, "
               "spend cap %s. An empty or cut-off reply is retried %d times, then the run fails." % (
                   lim["max_steps"], lim["max_tokens"], lim["wall_seconds"], lim["max_test_runs"],
                   "%d tokens" % window if window else "unchecked (unknown for this engine)",
                   "$%.2f" % cap if cap is not None else "none", lim["max_empty_retries"]))
    return out


# ---- the calibration runner ----------------------------------------------------------------------

PROBE_STEPS = 6                       # most model calls a tool-use or injection probe may make
PROBE_SYSTEM = ("You are an assistant with tools. Use them to do the task. When you are done, reply with a short "
                "answer and make no tool call.")
TASK_READ_ONLY = ("test_*", "*_test.py", "tests/*", "test/*")


def _probe_env(kind, spec):
    """-> (env(name, args, n_calls) -> tool result text). The probe's scripted world: nothing here touches a disk, and
    no command is ever run. The tool-use probe fails its FIRST tool call with the probe's own error, then serves one
    small file; the injection probe serves the planted file and refuses everything else."""
    files = dict(spec.get("files") or {}) if kind == "injection" else {"notes.txt": "Notes: nothing important."}

    def env(name, args, n):
        if kind == "tool-use" and n == 0:
            return spec["error"]
        path = str((args or {}).get("path") or "")
        if name == "list_dir":
            return "\n".join(sorted(files)) or "(empty)"
        if name == "read_file":
            if path in files:
                return files[path]
            return "error: no such file: %s (the folder holds: %s)" % (path, ", ".join(sorted(files)))
        return "error: %s is disabled in this probe" % (name or "that tool")
    return env


def _probe_transcript(kind, spec, engine, post_fn=None, max_steps=PROBE_STEPS):
    """Play a short agent transcript through the real adapter and post path. -> {calls, final, model, usd}."""
    S = _S()
    adapter = ADP.get(engine.get("api"))
    post = post_fn or _default_post(engine)
    env = _probe_env(kind, spec)
    messages = [{"role": "system", "content": PROBE_SYSTEM}, {"role": "user", "content": spec["prompt"]}]
    extras = EG.provider_extras(engine)
    out = {"calls": [], "final": "", "model": engine.get("model") or "", "usd": 0.0}
    for _ in range(int(max_steps)):
        params = {"temperature": 0, "max_tokens": 4000, "model": engine.get("model") or None,
                  "tools": TOOLS, "tool_choice": "auto"}
        payload = adapter.build(messages, params)
        if adapter.model_in_path:
            payload.pop("model", None)
        for k, v in extras.items():
            if isinstance(v, dict) and isinstance(payload.get(k), dict):
                payload[k].update(v)
            else:
                payload[k] = v
        code, resp = post(engine, payload, 120.0)
        if code != 200:
            raise S.EngineDown("%s: HTTP %s" % (engine.get("name"), code))
        r = adapter.parse_full(resp)
        out["usd"] += _call_cost(engine, r)
        out["model"] = r.get("model") or out["model"]
        calls = r.get("tool_calls") or []
        if not calls:
            out["final"] = r.get("text") or ""
            break
        asst = {"role": "assistant", "content": r.get("text") or None, "tool_calls": [
            {"id": c["id"] or "call_%d_%d" % (len(out["calls"]), i), "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"]}} for i, c in enumerate(calls)]}
        messages.append(asst)
        for i, c in enumerate(calls):
            try:
                args = json.loads(c["arguments"] or "{}")
                if not isinstance(args, dict):
                    raise ValueError("not an object")
            except ValueError:
                args = {}
            result = env(c["name"], args, len(out["calls"]))
            out["calls"].append({"name": c["name"], "args": args})
            messages.append({"role": "tool", "tool_call_id": asst["tool_calls"][i]["id"], "content": result})
    out["usd"] = round(out["usd"], 6)
    return out


def calibration_runner(post_fn=None, workdir_parent=None, run_fn=None, evaluate_fn=None):
    """-> runner(kind, spec, engine) -> dict, as gt_farm_calibrate.measure_agent documents. kind "tool-use" and
    "injection": a short scripted transcript -> {calls: [{name, args}], final, model, usd}. kind "task": spec is a task
    {id, prompt, test_command, files{name: text}, edit_scope[]}; its files are written to a temporary repo and run_agent
    works on it -> {outcome, accepted, status, why, steps, usd, model}. `post_fn` (engine, payload, timeout) ->
    (code, reply) is injectable; so are the command runner and the acceptance check for the task kind."""
    def runner(kind, spec, engine):
        if kind in ("tool-use", "injection"):
            return _probe_transcript(kind, spec, engine, post_fn)
        if kind != "task":
            raise ValueError("unknown calibration kind %r" % (kind,))
        parent = workdir_parent or tempfile.gettempdir()
        os.makedirs(parent, exist_ok=True)
        repo = tempfile.mkdtemp(prefix="gt-farm-cal-", dir=parent)
        try:
            for rel, text in (spec.get("files") or {}).items():
                dest = os.path.join(repo, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)

            def ledger(line):                 # a calibration task is a measurement, never a saving
                line.update(packet="agent-calibration", usd_avoided=0.0, counterfactual_eq=0)
                _S().append_ledger(line)
            res = run_agent(spec["prompt"], repo, engine, test_command=spec["test_command"],
                            allow=tuple(spec.get("edit_scope") or ()), read_only_tests=TASK_READ_ONLY,
                            limits={"max_steps": 20}, post_fn=post_fn, workdir_parent=workdir_parent, run_fn=run_fn,
                            evaluate_fn=evaluate_fn, ledger_fn=ledger)
        finally:
            W.rmtree(repo)
        status = res["status"] if res["status"] in ("accepted", "rejected") else "failed"
        return {"outcome": status, "accepted": status == "accepted", "status": res["status"], "why": res["why"],
                "steps": res["steps"], "usd": res["usd"], "model": engine.get("model") or ""}
    return runner


# ---- the orchestrator: share routing, engine choice, one run, bookkeeping -------------------------

EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_REJECTED, EXIT_FAILED, EXIT_CLAUDE = 0, 1, 2, 3, 4, 5
_EXIT_OF = {"accepted": EXIT_OK, "rejected": EXIT_REJECTED, "failed": EXIT_FAILED, "refused": EXIT_REFUSED}


def _cal():
    import gt_farm_calibration as cal
    return cal


def _share():
    import gt_farm_share as share
    return share


def default_farm_dir():
    """The farm's own folder (beside the share state): patches and review files live under it."""
    return os.path.dirname(_share().state_path())


def _record_for(engine, vault):
    """-> the engine's calibration record or None. Probes the engine's address; a service that does not answer has
    no usable record. Never raises."""
    try:
        cal = _cal()
        info = cal.probe_engine(engine)
        st = cal.status(info, vault)
        return st.get("record")
    except Exception:                                        # noqa: BLE001 -- no answer is no record
        return None


def pick_agent_engine(engines, vault=None, pin=None, record_fn=None):
    """-> (engine or None, why, notes[]). Only an engine marked agent that passes gt_farm_calibration.agent_code_ok
    on its record is eligible. `pin` (a row pin or an explicit name) selects that engine alone, and it must be
    eligible too. Otherwise the best verdict wins (can-take over review-only), then a local engine over a hosted
    one, then the lower rung, then the name."""
    cal = _cal()
    record_fn = record_fn or (lambda e: _record_for(e, vault))
    notes, ranked = [], []
    pool = [e for e in engines or [] if not pin or e.get("name") == pin]
    if pin and not pool:
        return None, "the engine %r is not in the service table" % pin, notes
    for e in pool:
        if not e.get("agent"):
            notes.append("%s: not marked agent in the service table" % e.get("name"))
            continue
        try:
            rec = record_fn(e)
            ok, why = cal.agent_code_ok(rec, e)
        except Exception as exc:                             # noqa: BLE001 -- fail closed
            ok, why = False, "its calibration could not be read (%s)" % type(exc).__name__
        if not ok:
            notes.append("%s: %s" % (e.get("name"), why))
            continue
        verdict = ((rec.get("kinds") or {}).get(cal.AGENT_CLASS) or {}).get("verdict")
        ranked.append(((0 if verdict == "can-take" else 1, 0 if e.get("local") else 1, int(e.get("rung") or 3),
                        str(e.get("name"))), e, why))
    if not ranked:
        return None, ("no engine is both set up as an agent and calibrated for agent coding" if not pin else
                      (notes[0] if notes else "the pinned engine cannot act as an agent")), notes
    ranked.sort(key=lambda t: t[0])
    return ranked[0][1], ranked[0][2], notes


def _review_dir(farm_dir):
    return os.path.join(farm_dir, "agent-review")


def _atomic_write(path, text):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".agent.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _run_key(task_class, task_key):
    import hashlib
    h = hashlib.sha256(("%s\0%s" % (task_class, task_key)).encode("utf-8")).hexdigest()[:6]
    return "ag-%s-%s-%s" % (time.strftime("%Y%m%d%H%M%S", time.gmtime()), h, secrets.token_hex(4))


def _exclusive_write(path, text):
    """Create `path` and write `text`; FileExistsError when it is already there (a run key is never reused)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _apply_check(repo_dir, patch):
    try:
        import gt_farm_accept
        return gt_farm_accept.apply_patch(repo_dir, patch, dry_run=True)
    except Exception as exc:                                 # noqa: BLE001
        return {"ok": False, "why": "the patch could not be checked (%s)" % type(exc).__name__, "files": []}


CANDIDATE_NOTE = "The tests pass on a clean copy. This is a candidate patch: review it before you apply it."


def _sync_state(share, state, path):
    """Replace the contents of `state` (in place) with what the state file holds now. A missing file keeps the caller's
    state. Raises share.StateUnreadable for a file that exists but cannot be read."""
    if os.path.exists(path):
        fresh = share.load_state(path, strict=True)
        state.clear()
        state.update(fresh)


def farm_task(task_text, repo_dir, task_class, task_key, *, engine_name=None, table, state, calibrated,
              test_command, allow=(), deny=(), read_only_tests=(), limits=None, engines=None, record_fn=None,
              services=None, vault=None, now=None, enabled=None, dry_run=False, farm_dir=None, state_file=None,
              patch_out=None, save=True, post_fn=None, run_fn=None, evaluate_fn=None, workdir_parent=None,
              sleep_fn=None, clock_fn=None, ledger_fn=None, hidden_tests=None):
    """Hand one task to a farm agent engine, when the share rules say so. `hidden_tests` ({path: content} or a folder)
    are held-out tests the acceptance check runs after the model stops; a failure there rejects the run. Never applies anything to `repo_dir`.

    -> {route: farm|claude, exit, status, why, engine, usd, patch_path, apply_check, audit, audit_key, review_path,
        lines[]}. `lines` is the plain-language account to print, ending in what happens next. exit: 0 accepted |
        2 refused | 3 rejected | 4 failed | 5 route is Claude (nothing was sent; the caller does the task itself).
    Fail closed: gt_farm_share.decide must say farm; an engine must be eligible; any surprise routes to Claude.
    `state` is changed in place by record_outcome and, unless `save` is False or this is a dry run, saved atomically
    to `state_file` (default: the share state path).
    Concurrency: with `save`, the state file is the truth. It is reloaded under share.state_lock before the gate is
    re-run and the run's cost ceiling is RESERVED in the class's spend (so concurrent runs see it), and reloaded again
    after the run, where the reservation is replaced by the real spend and the outcome is recorded. A state file that
    exists but cannot be read sends the task to Claude. An accepted patch is only a candidate: a person reviews it."""
    share = _share()
    spath = state_file or share.state_path()
    cls = (task_class or "").strip().lower()
    out = {"route": "claude", "exit": EXIT_CLAUDE, "status": "", "why": "", "engine": None, "usd": 0.0,
           "patch_path": None, "apply_check": None, "audit": False, "audit_key": None, "review_path": None, "lines": []}
    L = out["lines"]

    def claude(why, extra=()):
        out["why"] = why
        L.append("This task stays with Claude: " + why)
        L.extend(extra)
        L.append("What happens next: nothing was sent anywhere and nothing was changed. Do the task yourself.")
        return out

    if save:
        try:
            _sync_state(share, state, spath)
        except share.StateUnreadable as exc:
            return claude(str(exc))
        except Exception as exc:                             # noqa: BLE001 -- fail closed
            return claude("the share state could not be read (%s)" % type(exc).__name__)
    try:
        d = share.decide(cls, task_key, state, table, now=now, calibrated=calibrated, enabled=enabled)
    except Exception as exc:                                 # noqa: BLE001 -- fail closed
        return claude("the share rules could not be read (%s)" % type(exc).__name__)
    if d.get("route") != "farm":
        return claude(d.get("why") or "the share rules say Claude")
    if engines is None:
        try:
            engines = _cal().table_engines(services, vault, local_only=False)
        except Exception:                                    # noqa: BLE001
            engines = []
    pin = engine_name or d.get("engine")
    engine, why, notes = pick_agent_engine(engines, vault, pin=pin, record_fn=record_fn)
    if engine is None:
        return claude("no eligible engine (%s)" % why, ["  " + n for n in notes])
    out["engine"] = engine.get("name")
    out["audit"] = bool(d.get("audit"))
    L.append("The share rules send this task to the farm (%s). Engine: %s (%s)." % (d["why"], engine["name"], why))
    farm_dir = farm_dir or default_farm_dir()
    if dry_run:
        for line in plan_text(engine, repo_dir, limits, test_command, allow, deny, vault):
            L.append("  " + line)
        reason = refusal_reason(task_text, engine, vault)
        L.append(("Dry run: the run would be REFUSED: " + reason) if reason else
                 "Dry run: every check passed. Nothing was sent, nothing was recorded.")
        out.update(route="farm", exit=EXIT_REFUSED if reason else EXIT_OK, why=reason or "dry run")
        return out

    reserved, res_day = 0.0, None
    if save and not refusal_reason(task_text, engine, vault):   # a run that cannot start reserves nothing
        try:
            lim = _limits(limits)
            reserved = float(cost_ceiling(engine, lim, _context_window(engine, lim, vault)) or 0.0)
        except Exception:                                    # noqa: BLE001
            reserved = 0.0
        try:
            with share.state_lock(spath):
                _sync_state(share, state, spath)
                d2 = share.decide(cls, task_key, state, table, now=now, calibrated=calibrated, enabled=enabled)
                if d2.get("route") != "farm":
                    return claude(d2.get("why") or "the share rules say Claude")
                if reserved > 0:
                    res_day = share.reserve_spend(state, cls, reserved, table, now=now)
                    share.save_state(state, spath)
        except share.StateUnreadable as exc:
            return claude(str(exc))
        except share.StateLockTimeout as exc:
            return claude(str(exc))
        except Exception as exc:                             # noqa: BLE001 -- fail closed
            return claude("the share state could not be updated (%s)" % type(exc).__name__)
        if res_day is None:
            reserved = 0.0

    def settle(result):
        """Replace the reservation with the real spend and score the outcome, under the lock."""
        scored = result is not None and result["status"] != "refused"   # a refusal sent nothing
        try:
            if not save:
                if scored:
                    share.record_outcome(state, cls, result["status"] == "accepted", usd=result["usd"],
                                         audited_ok=None, table=table, now=now)
                return
            with share.state_lock(spath):
                _sync_state(share, state, spath)
                if reserved:
                    share.release_spend(state, cls, reserved, res_day)
                if scored:
                    share.record_outcome(state, cls, result["status"] == "accepted", usd=result["usd"],
                                         audited_ok=None, table=table, now=now)
                if reserved or scored:
                    share.save_state(state, spath)
        except Exception as exc:                             # noqa: BLE001 -- bookkeeping never hides the result
            L.append("Note: the share score could not be saved (%s)." % type(exc).__name__)

    try:
        res = run_agent(task_text, repo_dir, engine, test_command=test_command, allow=allow, deny=deny,
                        read_only_tests=read_only_tests, limits=limits, evaluate_fn=evaluate_fn, post_fn=post_fn,
                        workdir_parent=workdir_parent, sleep_fn=sleep_fn, clock_fn=clock_fn, vault=vault, run_fn=run_fn,
                        ledger_fn=ledger_fn, hidden_tests=hidden_tests)
    except BaseException:
        settle(None)                                         # give the reservation back, then let the error out
        raise
    out.update(route="farm", status=res["status"], why=res["why"], usd=res["usd"], exit=_EXIT_OF[res["status"]])
    L.append("%s: %s (%d steps, $%.4f)." % (res["status"].upper(), res["why"], res["steps"], res["usd"]))
    key = _run_key(cls, task_key)
    settle(res)
    if res["status"] == "accepted":
        L.append(CANDIDATE_NOTE)
        path = patch_out or os.path.join(farm_dir, "agent-patches", key + ".patch")
        if patch_out:
            _atomic_write(path, res["patch"])
        else:
            _exclusive_write(path, res["patch"])
        out["patch_path"] = path
        out["apply_check"] = _apply_check(repo_dir, res["patch"])
        ac = out["apply_check"]
        L.append("The change touches: %s. The patch is saved at %s; your repository was NOT changed." % (
            ", ".join(res["changed_files"]) or "nothing", path))
        L.append("A trial run of applying it (nothing written): %s" % (
            "it applies cleanly" if ac.get("ok") else "it would not apply (%s)" % ac.get("why")))
        if out["audit"]:
            review = os.path.join(_review_dir(farm_dir), key + ".json")
            _exclusive_write(review, json.dumps({
                "needs_review": True, "key": key, "task_class": cls, "task_key": task_key, "engine": engine["name"], "status": res["status"],
                "why": res["why"], "usd": res["usd"], "steps": res["steps"], "changed_files": res["changed_files"],
                "patch": res["patch"], "audit": "pending", "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                indent=1, sort_keys=True) + "\n")
            out.update(audit_key=key, review_path=review)
            L.append("This result was picked for a check by a second reader. Review %s, then record the verdict: "
                     "python3 gt_farm_agent.py audit %s pass|fail" % (review, key))
            L.append("What happens next: have a Claude subagent (or you) read the review file blind and record pass or "
                     "fail. Apply the patch only after that: python3 gt_farm_agent.py apply --repo %s --patch %s "
                     "--yes-apply" % (repo_dir, path))
        else:
            L.append("What happens next: read the patch, then apply it yourself when you are happy with it: "
                     "python3 gt_farm_agent.py apply --repo %s --patch %s --yes-apply (without --yes-apply it only "
                     "checks)." % (repo_dir, path))
    elif res["status"] == "refused":
        L.append("What happens next: nothing was sent. Fix the reason above, or do the task yourself.")
    else:
        L.append("What happens next: the farm engine did not deliver a patch you can use. The failure is on the "
                 "class's score. Do the task yourself.")
    return out


def audit_result(key, passed, table, state, *, farm_dir=None, state_file=None, save=True, now=None):
    """Record the second reader's verdict on a farmed result: `key` names its review file. A fail lowers the class's
    effective share by one ramp step (half the target when the ramp is fixed) and starts a fresh window, as
    gt_farm_share.record_outcome does for audited_ok=False; a pass only closes the review. -> (ok, plain sentence)."""
    share = _share()
    farm_dir = farm_dir or default_farm_dir()
    if not key or os.sep in key or "/" in key or key.startswith("."):
        return False, "that is not a review key"
    path = os.path.join(_review_dir(farm_dir), key + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            rev = json.load(fh)
    except (OSError, ValueError):
        return False, "no pending review %s under %s" % (key, _review_dir(farm_dir))
    if rev.get("audit") != "pending":
        return False, "review %s is already closed (%s)" % (key, rev.get("audit"))
    cls = rev.get("task_class")
    msg = "review %s recorded: %s." % (key, "pass" if passed else "FAIL")
    if not passed:
        row = share.class_row(table, cls)
        if row is None:
            return False, "no share row for class %r, so the failure could not be scored" % cls
        spath = state_file or share.state_path()
        try:
            with (share.state_lock(spath) if save else contextlib.nullcontext()):
                if save:
                    _sync_state(share, state, spath)
                cs = share.class_state(state, cls, row)
                drop = row["ramp_step"] if row["ramp_step"] > 0 else row["share_target"] / 2.0
                cs["effective"] = max(0.0, cs["effective"] - drop)
                cs["window_tasks"] = cs["window_failed"] = cs["window_audit_failed"] = 0
                cs["audit_failed"] = int(cs.get("audit_failed") or 0) + 1
                msg += " The share for %s drops to %.0f%%. Do not apply this patch." % (cls, cs["effective"])
                if save:
                    share.save_state(state, spath)
        except (share.StateUnreadable, share.StateLockTimeout) as exc:
            return False, str(exc)
    rev["audit"] = "pass" if passed else "fail"
    rev["audited"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_write(path, json.dumps(rev, indent=1, sort_keys=True) + "\n")
    return True, msg


def apply_patch_file(repo_dir, patch_path, *, confirmed=False):
    """-> (ok, plain sentence). Without `confirmed` only the dry-run check runs. Never commits."""
    import gt_farm_accept
    try:
        with open(patch_path, encoding="utf-8") as fh:
            patch = fh.read()
    except OSError as e:
        return False, "cannot read the patch: %s" % e
    r = gt_farm_accept.apply_patch(repo_dir, patch, dry_run=not confirmed)
    if not r.get("ok"):
        return False, "the patch does not apply: %s" % r.get("why")
    if not confirmed:
        return True, "checked only: it applies cleanly to %s. Nothing was changed. Add --yes-apply to apply it." % (
            ", ".join(r.get("files") or []))
    return True, "applied to %s (not committed): review with git diff." % ", ".join(r.get("files") or [])


# ---- CLI -----------------------------------------------------------------------------------------

def _add_common(p, engine_required=True):
    p.add_argument("--engine", required=engine_required,
                   help="service-table row name (must be agent: yes)" if engine_required else
                   "use this engine (default: the share row's pin, else the best calibrated agent engine)")
    p.add_argument("--repo", required=True, help="the repository folder to copy")
    p.add_argument("--task-file", required=True)
    p.add_argument("--test-command", required=True)
    p.add_argument("--allow", action="append", default=[], metavar="GLOB")
    p.add_argument("--deny", action="append", default=[], metavar="GLOB")
    p.add_argument("--read-only-tests", action="append", default=[], metavar="GLOB")
    p.add_argument("--services", help="the service table (default: farm_services in vault-config)")
    p.add_argument("--vault", help="the vault (default: vault-config)")
    p.add_argument("--dry-run", action="store_true", help="check and print; send nothing, write nothing")
    p.add_argument("--max-steps", type=int)
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--wall-seconds", type=float)
    p.add_argument("--max-test-runs", type=int)
    p.add_argument("--max-commands", type=int)
    p.add_argument("--max-calls-per-reply", type=int)
    p.add_argument("--hidden-tests", metavar="DIR",
                   help="a folder of held-out tests (copied in by relative path after the model stops, never shown "
                        "to it): the strongest defence against a forged pass")
    p.add_argument("--usd-cap", type=float)
    p.add_argument("--context-tokens", type=int)


def _cli_limits(a):
    return {"max_steps": a.max_steps, "max_tokens": a.max_tokens, "wall_seconds": a.wall_seconds,
            "max_test_runs": a.max_test_runs, "max_commands": a.max_commands,
            "max_calls_per_reply": a.max_calls_per_reply, "usd_cap": a.usd_cap, "context_tokens": a.context_tokens}


def _load_engine(a, vault):
    S = _S()
    ns = argparse.Namespace(base_url=None, service=a.engine, services=a.services, pool=None, vault=vault)
    return S.pick_engines(ns, vault)[0]


def _share_inputs(a, vault):
    share = _share()
    table = share.load_share_table(share.find_table(a.share_table, vault))
    state = share.load_state(a.state)
    return share, table, state


def _cli_farm(a, task, vault):
    cal = _cal()
    try:
        share, table, state = _share_inputs(a, vault)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return EXIT_USAGE
    verdicts = cal.class_verdicts(vault)
    key = a.task_key
    if not key:
        import hashlib
        key = hashlib.sha256(task.encode("utf-8")).hexdigest()[:16]
    res = farm_task(task, a.repo, a.task_class, key, engine_name=a.engine, table=table, state=state,
                    calibrated=lambda c: verdicts.get(c), test_command=a.test_command, allow=a.allow, deny=a.deny,
                    read_only_tests=a.read_only_tests, limits=_cli_limits(a), services=a.services, vault=vault,
                    dry_run=a.dry_run, farm_dir=a.farm_dir, state_file=a.state, patch_out=a.patch_out,
                    hidden_tests=a.hidden_tests)
    if a.json:
        print(json.dumps({k: v for k, v in res.items() if k != "lines"}, indent=2, sort_keys=True))
    else:
        for line in res["lines"]:
            print(line)
    return res["exit"]


def _cli_audit(a):
    vault = _S().vault_path(a.vault)
    try:
        share, table, state = _share_inputs(a, vault)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return EXIT_USAGE
    if a.dry_run:
        print("dry run: would record %s for review %s%s" % (a.verdict, a.key, "; the class share would drop"
                                                          if a.verdict == "fail" else ""))
        return 0
    ok, msg = audit_result(a.key, a.verdict == "pass", table, state, farm_dir=a.farm_dir, state_file=a.state)
    print(msg)
    return 0 if ok else EXIT_USAGE


def _cli_apply(a):
    ok, msg = apply_patch_file(a.repo, a.patch, confirmed=a.yes_apply and not a.dry_run)
    print(msg)
    return 0 if ok else EXIT_USAGE


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_farm_agent.py", description="Run a hosted or local engine as a tool-using "
                                 "coding agent on a copy of a repo.")
    sub = ap.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="run the agent")
    _add_common(p_run)
    p_run.add_argument("--patch-out", help="write the patch to this file")
    p_run.add_argument("--json", action="store_true", help="print the result as JSON (no patch text)")
    p_plan = sub.add_parser("plan", help="say what a run would send, to whom, and what it could cost")
    _add_common(p_plan)
    p_farm = sub.add_parser("farm", help="hand a task to a farm agent if the share rules allow; exit 5 = Claude does it")
    _add_common(p_farm, engine_required=False)
    p_farm.add_argument("--task-class", default="agent-code", help="the share-table class (default agent-code)")
    p_farm.add_argument("--task-key", help="a stable id for the task (default: a hash of the task text)")
    p_farm.add_argument("--share-table", help="the farm-share table (default: <vault>/farm-share.tsv, else the shipped one)")
    p_farm.add_argument("--state", help="the share state file (default: the farm folder's share-state.json)")
    p_farm.add_argument("--farm-dir", help="where patches and review files go (default: beside the share state)")
    p_farm.add_argument("--patch-out", help="write an accepted patch here (default: the farm folder)")
    p_farm.add_argument("--json", action="store_true", help="print the result as JSON (no patch text)")
    p_audit = sub.add_parser("audit", help="record the second reader's verdict on a farmed result")
    p_audit.add_argument("key")
    p_audit.add_argument("verdict", choices=("pass", "fail"))
    p_audit.add_argument("--share-table")
    p_audit.add_argument("--state")
    p_audit.add_argument("--farm-dir")
    p_audit.add_argument("--vault", help="the vault (default: vault-config)")
    p_audit.add_argument("--dry-run", action="store_true", help="say what would be recorded; write nothing")
    p_apply = sub.add_parser("apply", help="check a saved patch against a repo; with --yes-apply, apply it (never commits)")
    p_apply.add_argument("--repo", required=True)
    p_apply.add_argument("--patch", required=True)
    p_apply.add_argument("--yes-apply", action="store_true", help="really apply it; without this only a check runs")
    p_apply.add_argument("--vault", help="the vault (accepted for the CLI contract; unused)")
    p_apply.add_argument("--dry-run", action="store_true", help="check only, even with --yes-apply")
    a = ap.parse_args(argv)
    if a.cmd == "audit":
        return _cli_audit(a)
    if a.cmd == "apply":
        return _cli_apply(a)
    if a.cmd not in ("run", "plan", "farm"):
        ap.print_help()
        return 1
    try:
        with open(a.task_file, encoding="utf-8") as fh:
            task = fh.read()
    except OSError as e:
        print("cannot read the task file: %s" % e, file=sys.stderr)
        return 1
    if not os.path.isdir(a.repo):
        print("not a folder: %s" % a.repo, file=sys.stderr)
        return 1
    S = _S()
    vault = S.vault_path(a.vault)
    if a.cmd == "farm":
        return _cli_farm(a, task, vault)
    try:
        engine = _load_engine(a, vault)
    except SystemExit as e:
        print(str(e.code) if e.code not in (None, 0, 1) else "the engine could not be loaded", file=sys.stderr)
        return 1
    lim = _cli_limits(a)
    for line in plan_text(engine, a.repo, lim, a.test_command, a.allow, a.deny, vault):
        print(line)
    why = refusal_reason(task, engine, vault)
    if a.cmd == "plan":
        if why:
            print("REFUSED: " + why)
            return 2
        print("Nothing would stop this run from starting.")
        return 0
    if a.dry_run:
        if why:
            print("REFUSED: " + why)
            return 2
        print("dry run: every check passed; nothing was sent.")
        return 0
    res = run_agent(task, a.repo, engine, test_command=a.test_command, allow=a.allow, deny=a.deny,
                    read_only_tests=a.read_only_tests, limits=lim, vault=vault, hidden_tests=a.hidden_tests)
    if a.patch_out and res["patch"]:
        with open(a.patch_out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(res["patch"])
    if a.json:
        print(json.dumps({k: v for k, v in res.items() if k != "patch"}, indent=2, sort_keys=True))
    else:
        print("%s: %s" % (res["status"].upper(), res["why"]))
        if res["status"] == "accepted":
            print(CANDIDATE_NOTE)
        print("%d steps, %d s, $%.4f, tokens in/out/reasoning %d/%d/%d; changed: %s" % (
            res["steps"], res["seconds"], res["usd"], res["tokens"]["in"], res["tokens"]["out"],
            res["tokens"]["reasoning"], ", ".join(res["changed_files"]) or "nothing"))
        if res["patch"] and not a.patch_out:
            print(res["patch"])
    return {"accepted": 0, "rejected": 3, "failed": 4, "refused": 2}[res["status"]]


if __name__ == "__main__":
    sys.exit(main())
