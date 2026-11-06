---
name: gt-usage
description: "Show this account against its Claude plan allowance (5-hour, weekly, monthly spend) and what ending or cutting a session would save. Use when the user says: how much have I used, am I near my limit, what is my usage, show my allowance, what is this session costing."
plain:
  - cmd: "/gt-usage:gt-usage"
    does: "Shows where you stand against your Claude plan allowance."
    how: "Reads the 5-hour, weekly and monthly-spend windows and says what ending or cutting a session would save. Read-only."
    when: "You wonder whether you are close to a limit."
    next: "/gt:gt-minimize"
    hear: "how much have i used|am i near my limit|what is my usage|show my allowance"
model_intent: fast
---

# Golden Thread Usage

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_usage.py --now` / `--history` / `--context` | unchanged: they read `~/.claude/golden-thread/usage/` (and `--context` the transcripts under `~/.claude/projects/`), which stay readable; `--context` then only skips caching its result |
| adding the `statusLine` to `settings.json` | **terminal** |

A **terminal** row is a step sandbox mode refuses on purpose — it changes settings or install state, or rewrites the vault wholesale. Give the user the exact command to run from a terminal, and carry on with the rest.

Where the plan allowance stands, and what a person can do about it.

## Run it

```bash
python3 ~/.claude/golden-thread/hooks/gt_usage.py --now
```

Show the output verbatim, then add **one sentence** saying whether anything needs
attention or plainly that nothing does. Do not restate the numbers in prose.

When gt-farm has recorded packets in the last 7 days, the readout has a `work kept off
Claude` section: packets, tokens, the share on the user's own engines, and the same work
as Claude token-equivalents (estimated). It is tokens, not allowance: keep it beside the
percentages, never subtracted from them. `/gt-usage:gt-usage-savings` has the breakdown.

The `where output went` section (0.1.10) splits the last 7 days of output tokens by model and
by the context each reply carried (input + cache read + cache write: <32k, 32-128k, 128-400k,
>400k), measured from the local transcripts. Output written while carrying a large context is
the share that closing threads sooner would cut; say which band holds most of it, in one clause.
`--now` reuses a result under a day old; `gt_usage.py --context <days>` measures another window.

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

The status line needs one entry in `settings.json`. Since gt 0.21.0 install.sh offers to add
it at a terminal (default no), and `python3 ~/.claude/golden-thread/hooks/gt_usage.py
--setup-statusline` adds it at any time — only when no status line is configured, because the
display is the user's own (`--check` says what it would write). The entry is:

```json
"statusLine": {"type": "command",
               "command": "python3 ~/.claude/golden-thread/hooks/gt_usage.py"}
```
