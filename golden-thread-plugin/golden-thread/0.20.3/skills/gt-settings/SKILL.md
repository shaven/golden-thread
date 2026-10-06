---
name: gt-settings
description: "View and change what Golden Thread does on its own — component drift checking at session start, the session report card at compact, the upstream watch, and whatever an installed module adds. Every optional automatic behaviour is registered in one place, and `show` is the authoritative list. `/gt:gt-settings lotr` shows what is connected to LOTR and whether each connection works."
model_intent: fast
---

# Golden Thread Settings

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_settings.py show` / `explain` / `get`, `gt_sandbox.py status`, `gt_model_policy.py show` | unchanged: they read `~/.claude`, which stays readable |
| `gt_settings.py set …`, `gt_model_policy.py choose` / `set` / `clear` / `apply` | **terminal**: sandbox mode write-protects `~/.claude` on purpose — a setting that could switch a guard off must never be changed from inside the fence |

A **terminal** row is a step sandbox mode refuses on purpose — it changes settings or install state, or rewrites the vault wholesale. Give the user the exact command to run from a terminal, and carry on with the rest.

Everything this system does without being asked that is *optional* is registered in one
place and can be turned off. A mechanism that acts on its own and cannot be inspected or
disabled is not trustworthy — which is the same argument the Core rules make about
enforcement, pointed back at the tooling itself.

The exception is deliberate and worth saying out loud. The four hooks that carry and
backstop the Core rules — `inject_core_rules.sh`, `validate_response.sh`,
`guard_session_claims.sh`, `guard_vault_writes.sh` — have no entry, because enforcement
the enforced session can switch off is not enforcement. They are still *visible*:
`install.sh` names every hook it wires and `/gt:gt-doctor` reports whether each is
registered and current. Removing one is an uninstall, not a setting. `gt_settings.py`'s
own docstring carries the full reasoning, including why `test_gate` and `protected_paths`
are registered when these are not.

Settings live in `~/.claude/vault-config.json`, beside `vault_path`. They are per machine: `$GT_VAULT` changes which vault a session works on, never where settings are read.

## Steps

**Step 1 — Locate the script**

`<plugin>/scripts/gt_settings.py`. The plugin cache is
`~/.claude/plugins/cache/golden-thread-plugin/gt/<version>/`.

**Step 2 — Show the current state**

```bash
python3 <plugin>/scripts/gt_settings.py show
```

Prints every setting, its current value, whether that value is set explicitly or
is the default, and the options. Do this before changing anything so the user sees
what they have.

When LOTR is installed, `show` ends with one line such as `LOTR: 3 connected, 1 needs
sign-in  -- details: /gt:gt-settings lotr`. Read that line back to the user.

**If the argument is `lotr`** (`/gt:gt-settings lotr`), or the user asks what is connected
to LOTR or whether a connection works, show the connection table instead:

```bash
python3 <plugin>/scripts/gt_settings.py lotr            # every connection and its state
python3 <plugin>/scripts/gt_settings.py lotr --check    # test every MCP connection now
```

Each line is `✓ connected`, `⚠ needs sign-in`, `✗ error` (with the error code and when),
`○ not used yet` or `– disabled`, and a connection that needs attention carries the fix
(for example `run: lotr login jira@personal`). Show the table as it is; do not run the fix
for the user. A sign-in is theirs to do at a terminal.

**Step 3 — Explain, if the user is choosing**

```bash
python3 <plugin>/scripts/gt_settings.py explain <name>
```

Each setting carries the reasoning for its default, including the failure that
motivated it. Read that back rather than paraphrasing — the defaults are chosen
against specific incidents, and the incident is the argument.

**Step 4 — Change it**

```bash
python3 <plugin>/scripts/gt_settings.py set <name> <value>
```

Validated against the registry; an invalid value is refused with the valid ones
listed. Reports what changed, from and to.

## The settings

**`show` is the authority, not this table.** Run it first — a module that is not installed
registers nothing, so what is actually available on this machine is a fact about this
machine. What gt itself ships:

| Setting | Values | Default |
|---|---|---|
| `component_updates` | `off` · `report` · `confirm` · `auto` | `report` |
| `version_check` | `off` · `report` | `report` |
| `orphan_check` | `off` · `report` · `reap` | `report` |
| `push_check` | `off` · `report` | `report` |
| `test_gate` | `off` · `warn` · `auto` · `block` | `auto` |
| `parallel_work` | `off` · `on` | `on` |
| `parallel_max` | `auto`, or a positive integer | `auto` |
| `protected_paths` | `off` · `ask` | `ask` |

And what the shipped modules add when installed:

| Setting | Module | Values | Default |
|---|---|---|---|
| `report_card` | report-card | `off` · `minimal` · `full` | `minimal` |
| `closeout_check` | report-card | `off` · `ask` | `ask` |
| `watch` | watch | `off` · `report` | `off` |

`show` lists every setting gt itself registers, then each installed module's settings
under `module <name>`. Since 0.15.0 `report_card` and `closeout_check` come from the
**report-card** module (`gt-report-card`, no command of its own) and `watch` from the
**watch** module (`/gt-watch:gt-watch`); a module that is not installed registers nothing.
A value you set for a module that is later switched off is kept and listed as
**OFF — settings kept, not in effect**; it takes effect again after
`bash install.sh --with <module>`. Switching a setting off never uninstalls a module, and
`install.sh --without <module>` never rewrites a setting.

**`component_updates`** — at session start, compares INSTALLED hooks and scripts
against what is checked into the plugin. Found live on 2026-08-29 that
`guard_session_claims.sh` was installed but absent from the plugin source, and
`validate_response.sh` had drifted: two of three enforcement mechanisms existed on
one machine only, and a fresh install would have had the Core rules as documents
with nothing re-asserting them.

Note what `auto` does **not** do: it never overwrites a file whose installed copy
is newer than the source, and never deletes one the source lacks. That is not
caution for its own sake — the real drift ran exactly that direction, so a naive
"source is truth" updater would have reverted the timestamp validator and deleted
the claim guard, silently, on every session start.

**`report_card`** (report-card module) — a pass at `/compact`, auto-compact and session end. `minimal`
reports hygiene from the session just done; `full` adds vault features that are
available and unused. It fires on `PreCompact` so it is written while there is
still context to write it in.

**`vault_hints`** (0.19.1, default `off`) — when `on`, a UserPromptSubmit hook matches each
prompt against `index.md` and adds at most three `- <title> — <path>` lines naming pages that
may already hold the answer. It reads one file and never a page body; nothing is added below
the match threshold or past its 0.8 s budget. Off by default because it costs a little on every
turn. Turn on with `python3 <base_dir>/../../scripts/gt_settings.py set vault_hints on`.

**`sandbox_mode`** (0.20.1, default `off`) — gt sandbox mode. `on` has `gt_sandbox.py` merge
Claude Code settings into `~/.claude/settings.json` (recording exactly what it added, so `off`
removes only that): the OS sandbox (`sandbox.enabled`, `allowUnsandboxedCommands: false`,
`failIfUnavailable: true`) with the vault, `~/.claude/golden-thread`, the unlock home, LOTR and
the plugin dirs write-denied and the unlock state / LOTR credentials / locked vault folders
read-denied, `~/.gt-inbox` as the one writable spot, and matching `Read(...)`/`Edit(...)` deny
rules for Claude's file tools. The vault is then read through the gt-vault MCP tools and written
through `vault_queue_write` (or `gt_write_queue.py`, which drops its request in the inbox).
Native Windows has no Claude Code sandbox: there it writes the permission rules only —
friction, not a boundary. Linux/WSL2 refuse it without `bubblewrap` and `socat`. Restart Claude
Code after switching; switching it **off** must be done from a terminal (the sandbox
write-protects `~/.claude`). `sandbox_vault_reads` (`deny` default / `allow`) says whether the
shell and file tools may still read the vault; `vault_mcp` (`auto` default / `on` / `off`)
whether the vault MCP server offers its tools. Status and drift:
`python3 <base_dir>/../../scripts/gt_sandbox.py status`; SECURITY.md, "gt sandbox mode".

**`model_profile`** (0.19.1) — the model and effort each skill runs at, written into the
installed copy of every SKILL.md (never the release source). `average` (a new install's
default): fast skills haiku with no effort setting, balanced sonnet · medium, deep opus · high.
`very-high`: opus · xhigh everywhere. `inherit`: nothing written, every skill runs on the
session's model and effort. It lives with the install choices
(`~/.claude/golden-thread/install-choices.json`), so `show` displays it but `set` here does
not change it — use the policy tool, which re-applies at once, no reinstall:

```bash
python3 <base_dir>/../../scripts/gt_model_policy.py show              # every skill: model, effort, why
python3 <base_dir>/../../scripts/gt_model_policy.py choose very-high  # then: ... apply
python3 <base_dir>/../../scripts/gt_model_policy.py set --skill gt-plan --model opus --effort max
python3 <base_dir>/../../scripts/gt_model_policy.py set --plugin gt-wiki --model sonnet --effort low
python3 <base_dir>/../../scripts/gt_model_policy.py clear --skill gt-plan
```

Precedence: per-skill override, then per-plugin override, then the skill's `model_intent`
through the profile, then inherit. An effort the model does not accept is refused when set,
naming the allowed values (Haiku has none) — never lowered quietly. Choices survive a
reinstall. Hooks have no model or effort in Claude Code, so they are not covered.

**Specialist agents are set by the task, not the profile.** Each agent stage runs at its
spec's tier through the intent pack — classify and draft haiku, extract and place sonnet,
reconcile, verify and generalize opus — even on a very-high machine ("doc reading doesn't
need opus"). **`agent_models`** (default `task`) is the switch: `session` passes no model and
every agent runs on the session's model (`gt_settings.py set agent_models session`). The Agent
tool takes a model alias and no effort, so an agent runs at the session's effort and an agent
override (used when `agent_models` is `task`) is a model only, by stage or job type (the job
type wins):

```bash
python3 <base_dir>/../../scripts/gt_model_policy.py set --agent extract --model haiku
python3 <base_dir>/../../scripts/gt_model_policy.py set --agent extract-code --model opus
python3 <base_dir>/../../scripts/gt_model_policy.py clear --agent extract
```

## Adding a setting later

Append one entry to `SETTINGS` in `gt_settings.py` with `default`, `values`,
`summary` and `detail` — or, for a setting that belongs to a module, one entry in that
module's `module.json` `settings` list (`key`, `default`, `values`, `summary`, optional
`detail`). This skill and the validation pick it up with no other
change. Anything automatic that is *not* in that registry is an undocumented
behaviour, which is the thing this exists to prevent — so register it there rather
than reading a config key directly.

## Rules

- **Show before setting.** The user should see the current state and the options
  before choosing, not after.
- **Never write the config without `vault_path`.** A partial file leaves the hooks
  unable to find the vault, which breaks enforcement rather than extending it. The
  script refuses; do not work around it — run `/gt:gt-init`.
- **Do not read these keys directly from other scripts.** Use
  `gt_settings.get(name)` so the default is applied consistently.
