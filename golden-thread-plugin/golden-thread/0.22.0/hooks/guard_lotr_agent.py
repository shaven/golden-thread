#!/usr/bin/env python3
"""Confine an agent made by `lotr onboard` to its one script (gt 0.22.0, plan phase 41). PreToolUse hook, Bash.

    guard_lotr_agent.py        # the hook: the payload is on stdin

`lotr onboard agent ID` writes a thin agent whose only job is to run one script, `lotr_agent_call.py`, which
builds the call, submits it through the gateway and shortens a large result. The agent file cannot confine its own
Bash tool (measured 2026-10-21 on Claude Code 2.1.306: a `tools: Bash(pattern)` line and a hook in the agent's own
frontmatter both let a second command through), but a PreToolUse hook sees WHO is calling (`agent_type`). So this hook
is the confinement.

WHO IT ACTS ON. A Bash call whose `agent_type` starts with `lotr-`. Every other agent, and the main session, gets no
output at all: the hook is silent and Claude Code's normal permission flow decides.

WHAT IS ALLOWED for those agents, and nothing else:

    python3 <that agent's lotr_agent_call.py> [--home <its home>] <its connection> <operation>
            [--args '<json>'] [--select '<paths>']

Outside single quotes only letters, digits, space and  / . _ - : @ = , + ~  may appear: no `;`, `|`, `&`, `<`, `>`,
`$`, backtick, backslash, parenthesis, brace, double quote or newline. Inside single quotes anything goes except a
single quote (a single quote in the JSON is written \\u0027), because nothing inside them reaches the shell.
The interpreter, the script (after realpath), the home and the connection must be the ones recorded for this agent in
`<zone home>/onboard/agents.json`, which only `lotr onboard agent` writes.

FAILS CLOSED for those agents (and only those): an agent not in the registry, a missing or unreadable registry, or any
error while deciding is a denial. For anything else it fails open, like its siblings. What it cannot do: if the
interpreter itself cannot start this file, nothing runs and the call goes through; that is the residual risk.
"""
import glob
import json
import os
import re
import shlex
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True          # the hooks folder is signed: importing a sibling must not leave a __pycache__ in it

PREFIX = "lotr-"
INTERPRETER = re.compile(r"^(?:python3?(?:\.\d+)?|py)(?:\.exe)?$", re.I)
NAME_OK = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
OUTSIDE_OK = re.compile(r"^[A-Za-z0-9 /._:@=,+~-]*$")

try:
    import gt_refusals as _gtr
except Exception:                                            # noqa: BLE001
    _gtr = None


def _annotated(reason):
    try:
        return _gtr.annotate('guard_lotr_agent', reason) if _gtr else reason
    except Exception:                                        # noqa: BLE001
        return reason


def _use_session(payload):
    try:
        if _gtr:
            _gtr.use_session(payload)
    except Exception:                                        # noqa: BLE001
        pass


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": _annotated(reason),
    }}))
    sys.exit(0)


def registries():
    home = os.path.expanduser("~")
    paths = []
    if os.environ.get("LOTR_HOME"):
        paths.append(os.path.join(os.path.expanduser(os.environ["LOTR_HOME"]), "onboard", "agents.json"))
    paths += sorted(glob.glob(os.path.join(home, ".config", "gt-lotr", "*", "onboard", "agents.json")))
    return paths


def entry_for(agent):
    """-> the registry entry for `agent`, or None. A corrupt file is skipped, so it can only make this return None."""
    for p in registries():
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        agents = doc.get("agents") if isinstance(doc, dict) else None
        e = agents.get(agent) if isinstance(agents, dict) else None
        if isinstance(e, dict) and all(isinstance(e.get(k), str) and e.get(k) for k in ("connection", "script", "home")):
            return e
    return None


def allowed_form(e):
    # quoted as `lotr onboard` writes it: unquoted, a Windows path's backslashes are shell syntax this guard denies
    return ("python3 %s [--home %s] %s <operation> [--args '<json>'] [--select '<paths>']"
            % (shlex.quote(e["script"]), shlex.quote(e["home"]), e["connection"]))


def clean_outside_quotes(command):
    """True when nothing outside single quotes could reach the shell as syntax."""
    outside, quoted = [], False
    for ch in command:
        if ch == "'":
            quoted = not quoted
            outside.append("'")
        elif not quoted:
            outside.append(ch)
    return not quoted and OUTSIDE_OK.match("".join(outside).replace("'", "")) is not None


def judge(command, e):
    """-> None when allowed, else the reason."""
    form = "this agent may run only: " + allowed_form(e)
    if not isinstance(command, str) or not command.strip():
        return form
    if "\n" in command or "\r" in command or not clean_outside_quotes(command):
        return "that command has shell syntax outside single quotes; " + form
    try:
        tokens = shlex.split(command)
    except ValueError:
        return form
    if len(tokens) < 4 or not INTERPRETER.match(os.path.basename(tokens[0])):
        return form
    if os.path.realpath(tokens[1]) != os.path.realpath(e["script"]):
        return form
    rest, home_ok = tokens[2:], True
    if rest and rest[0] == "--home":
        if len(rest) < 2 or os.path.realpath(os.path.expanduser(rest[1])) != os.path.realpath(e["home"]):
            return form
        rest = rest[2:]
    if len(rest) < 2 or rest[0] != e["connection"] or not NAME_OK.match(rest[1]):
        return form
    flags = rest[2:]
    i = 0
    seen = set()
    while i < len(flags):
        f = flags[i]
        if f not in ("--args", "--select") or f in seen or i + 1 >= len(flags):
            return form
        seen.add(f)
        i += 2
    return None


def hook():
    try:
        payload = json.load(sys.stdin)
    except Exception:                                        # noqa: BLE001 - cannot tell who is calling: no objection
        sys.exit(0)
    _use_session(payload)
    agent = payload.get("agent_type") if isinstance(payload, dict) else None
    if not isinstance(agent, str) or not agent.startswith(PREFIX):
        sys.exit(0)
    try:
        if payload.get("tool_name") != "Bash":
            sys.exit(0)
        e = entry_for(agent)
        if e is None:
            deny("this agent is not registered with gt-lotr, so it may not run commands (lotr onboard agent registers it)")
        command = (payload.get("tool_input") or {}).get("command")
        why = judge(command, e)
        if why:
            deny(why)
    except SystemExit:
        raise
    except Exception:                                        # noqa: BLE001 - fail closed for these agents
        deny("the check on this agent's command could not finish, so the command was not run")
    sys.exit(0)


if __name__ == "__main__":
    hook()
