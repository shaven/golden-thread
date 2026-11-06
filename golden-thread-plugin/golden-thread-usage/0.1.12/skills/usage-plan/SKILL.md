---
name: usage-plan
description: "Set gt's token-saving plan (off, min, max), see what it changed, or hold cuts for this session. Use when the user says: usage plan, save tokens, turn on token saving, what did the plan change, hold cuts for this session."
plain:
  - cmd: "/gt-usage:usage-plan"
    does: "Turns gt's token-saving plan on or off in one step and shows what it changed."
    how: "min sets the safe few savings settings, max sets them all, off puts back the values from before; changes lists each setting with its value before the plan. hold stops all four automatic cuts for this session only; farm calibrates your own engine and turns farm_share on only if it passes."
    when: "You want gt to spend fewer tokens without choosing each setting yourself."
    next: "/gt-usage:gt-usage-savings"
    hear: "usage plan|save tokens|turn on token saving|what did the plan change|hold cuts for this session"
model_intent: balanced
---

# Golden Thread Usage: the usage plan

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell cannot write `~/.claude` or the vault. The steps then take the route in this table. With sandbox mode off (the default) nothing changes: every step runs as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_usage_plan.py show` / `changes` / `hold status` | unchanged: they read `~/.claude`, which stays readable |
| `gt_usage_plan.py off` / `min` / `max` / `hold on` / `hold off` | **terminal**: they change settings, which sandbox mode write-protects on purpose |
| `gt_usage_plan.py farm …` (it runs `gt_farm_calibrate.py`, `gt_farm_setup.py --apply` and `gt_settings.py set farm_share on`) | **terminal**: calibration reads the service table and queues its record in the vault; the other two change settings |

A **terminal** row is a step sandbox mode refuses on purpose. Give the user the exact command to run from a terminal, and carry on with the rest.

One command for gt's token-saving settings. The plan itself lives in gt core
(`gt_settings.py usage-plan`); this command prints core's output unchanged, so it and
`/gt:gt-settings` never disagree.

## Run it

`<verb>` is what the user asked for; no verb means `show`.

```bash
python3 ~/.claude/golden-thread/hooks/gt_usage_plan.py <verb>
```

| Verb | What it does |
|---|---|
| `show` | the plan in effect (`off`, `min`, `max` or `custom`) and each setting it covers, now and by default |
| `changes` | what the plan changed: each setting with its value before the plan |
| `min` | the safe few: cuts carry a facts handoff and clear, the cut advisor and expiry guard on, the context nudge at 30% |
| `max` | all of them: also the minimum token profile, farm-first, the farm ladder and share, auto-compact at 250,000 tokens, auto-clear scoring |
| `off` | the values from before the plan come back |
| `hold [on\|off\|status]` | this session only: `on` (a bare `hold` is `on`) blocks all four automatic cuts (auto-clear, auto-compact, the expiry guard's idle cut and the open-session path) until `off`; other sessions are untouched |
| `farm --base-url URL [--model NAME] [--name NAME]` | calibrate the engine at URL; only when it passes, make gt-farm's safe fixes and turn `farm_share` on |

Show the output verbatim. After `min` or `max`, add one sentence naming the way back:
`/gt-usage:usage-plan off`. A setting whose module is off is skipped and listed, never an
error. Changing any one of the plan's settings by hand makes the plan `custom`.

`hold` takes this session's id from Claude Code's environment; pass `--session ID` when the
user names another. It needs gt 0.22.1 core; an older core says it has no hold.

## farm: put the plan's farm settings onto an engine you run

`farm` is glue over gt-farm, never a new routing path, and needs the gt-farm module. Finding
an engine by itself is not done: the user gives the URL (an OpenAI-style endpoint, such as
`http://127.0.0.1:8080/v1`).

1. Run it with `--dry-run` first: it prints the calls and the estimated Claude cost of the
   calibration and sends nothing. Show that, and run without `--dry-run` only on the user's yes.
2. The run calibrates the engine (`gt_farm_calibrate.py --base-url URL`). It passes when at
   least one task class is `can-take`.
3. On a pass it runs `gt_farm_setup.py --apply` (farm_ladder and pipeline_engine on, the
   ladder table copied; it never writes the service table) and `gt_settings.py set farm_share on`.
4. A refusal (a budget or allowance guard, no runner, no engine answering) or a calibration with
   no `can-take` class changes nothing: `farm_share` stays as it was and the reason is printed.

The engine still needs a `local: yes` row in the farm service table before routine work goes to
it; the setup step names that gap with its fix when it is missing. Say so when it does.
