---
name: gt-grant
description: "Let one command through once, for a while or forever, without lowering the lockdown level; list and revoke grants. Use when the user says: grant this, allow this once, allow it for 30 minutes, stop asking for this command, list my grants, revoke a grant, why was my command refused."
plain:
  - cmd: "/gt:gt-grant"
    does: "Lets one command through for a while without lowering your whole security level."
    how: "Writes a time-limited permission into Claude Code's settings and removes it when the time is up. Also lists live grants and revokes one."
    detail: "a permission rule written with an expiry; a session-start sweep and `revoke` remove it. It never adds an allow by hook."
    when: "Claude keeps asking for the same safe command."
    next: "/gt:gt-settings"
model_intent: balanced
---

# Grants: let one command through, with a lifetime

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_grant.py list` | **terminal**: it reads `~/.claude/golden-thread/lockdown/`, which sandbox mode fences |
| `gt_grant.py add` / `revoke` | **terminal**: a grant writes `~/.claude/settings.json`, which sandbox mode write-protects on purpose — and a grant is the user's to give, so it is always run by them |

A **terminal** row is a step sandbox mode refuses on purpose. Give the user the exact command to run from a terminal, and carry on with the rest.

## What this is

Claude Code refuses a command it has no permission rule for, and its refusal says the cure: "the user can add a permission rule". A hand-written rule lasts forever, which is the wrong cure for a one-off. A **grant** is that rule with a lifetime:

| Duration | What happens |
|---|---|
| `once` | the first matching command runs, then the rule is taken out (and it lapses after an hour if unused) |
| `30m`, `2h` | the rule stands for that long, then gt takes it out. A command after it ends asks again |
| `forever` | a plain allow rule that survives gt being uninstalled. Backed up first |

The broad switch for most users is the lockdown level (`/gt:gt-settings lockdown`). A grant is for the one command a level does not cover — for example a ship script.

## Who runs it

**The user, in a terminal.** `gt_grant.py add` refuses to run without one, and asks gt unlock for a fresh confirmation when unlock is on. Do not run it, and do not try to reach the same effect another way: an assistant granting itself permission is exactly what the refusal is there to stop. When a command was refused, give the user the line to run and wait.

```bash
python3 ~/.claude/golden-thread/hooks/gt_grant.py add "make deploy" 30m
python3 ~/.claude/golden-thread/hooks/gt_grant.py add "scp *" once
python3 ~/.claude/golden-thread/hooks/gt_grant.py list
python3 ~/.claude/golden-thread/hooks/gt_grant.py revoke <id>
```

`PATTERN` is the command, exact (`make deploy`), or a prefix ending in `*` (`scp *`), or a Claude Code rule you write yourself (`Bash(...)`, `Edit(./docs/**)`).

## What it will not grant

- A whole tool (`Bash`, `Edit`): that is the `insecure` lockdown level, chosen on purpose.
- A LOTR consent-tier operation (send mail, merge, delete): those ask at every level.
- A credential path core denies (`~/.ssh`, `.env`, cloud logins).

## Honest limits

A grant is a permission rule Claude Code reads from `settings.json`; it applies when Claude Code reloads its settings (restart it if a command still asks). Ending a grant mid-session is done by a PreToolUse check that makes Claude Code **ask** again once the clock has passed it — gt's guards only ever tighten, none says "allow". The matching gt does for that (and for spending a `once`) is an approximation of Claude Code's own; the rule in `settings.json` is what Claude Code enforces. Every use is one line in `~/.claude/golden-thread/lockdown/log.jsonl` (the grant, its rule, a hash of the command — never the command).

## When a command was refused

gt's PermissionDenied hook prints the cure: the narrow one first (a grant, with the line to run), then the lowest lockdown level whose rules cover the command, if any. Relay it to the user as it is. Do not suggest raising the level for a one-off.
