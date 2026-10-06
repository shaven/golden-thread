---
name: gt-usage
description: "Show where this account stands against its Claude plan allowance — the 5-hour, weekly and monthly-spend windows — and what ending or cutting a session would save. Use when the user says: how much have I used, am I near my limit, what is my usage, how close am I to the cap, show my allowance, what is this session costing."
model_intent: fast
---

# Golden Thread Usage

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_usage.py --now` / `--history` | unchanged: they read `~/.claude/golden-thread/usage/`, which stays readable |
| adding the `statusLine` to `settings.json` | **terminal** |

A **terminal** row is a step sandbox mode refuses on purpose — it changes settings or install state, or rewrites the vault wholesale. Give the user the exact command to run from a terminal, and carry on with the rest.

Where the plan allowance stands, and what a person can do about it.

## Run it

```bash
python3 ~/.claude/golden-thread/hooks/gt_usage.py --now
```

Show the output verbatim, then add **one sentence** saying whether anything needs
attention or plainly that nothing does. Do not restate the numbers in prose.

For the readings behind it:

```bash
python3 ~/.claude/golden-thread/hooks/gt_usage.py --history 20
```

## Rules when reporting any of this

- **A window the plan does not report is absent, never zero.** The readout prints
  `not reported on this plan`; keep that distinction in anything you say about it.
- **If the newest reading is stale, say how old it is** rather than quoting it as
  current. The readout marks it; do not smooth that over.
- **Any dollar figure is a list-price equivalent, not a bill.** A subscription is
  not billed per token. Say so whenever one is quoted.
- **Do not convert tokens into a percentage of the allowance.** Whether cache
  tokens touch a subscription allowance is undocumented, and inventing the
  conversion would be a guess wearing the clothes of a measurement.

## What it is not

It is not a cost saver. Measured on one machine over 180 days, 65% of cache-write
volume was avoidable — and the weekly allowance still sat at 13%. The waste was
real; the scarcity was not. Say that when someone asks whether it is worth acting on.

## Turning it down

Both settings live in gt's own registry, so `/gt:gt-settings show` lists them:

- `usage_meter` — `off`, `status`, `login`, `both` (default `both`)
- `usage_alert` — `early`, `normal`, `late` (default `normal`)

The status line needs one entry in `settings.json` that no installer writes for you,
because the display is the user's own:

```json
"statusLine": {"type": "command",
               "command": "python3 ~/.claude/golden-thread/hooks/gt_usage.py"}
```
