---
name: gt-settings
description: "View and change what gt does on its own: every optional automatic behaviour, gt's and each module's, in one place (`show` lists them). `lotr` shows LOTR connections and whether each works; `models` is the Models and cost page (model, effort, cost and calibration per agent type). Use when the user says: change a gt setting, show settings."
plain:
  - cmd: "/gt:gt-settings"
    does: "Shows and changes what Golden Thread does on its own."
    how: "`show` lists every setting with its value and default; `set` changes one and records it in your config file. Some changes need a restart of Claude Code."
    detail: "reads and writes ~/.claude/vault-config.json; `models` shows per-agent cost, `lotr` shows gateway connections."
    when: "You want a behaviour turned on, off or tuned."
    next: "/gt:gt-doctor"
model_intent: fast
---

# Golden Thread Settings

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_settings.py show` / `explain` / `get`, `gt_sandbox.py status`, `gt_model_policy.py show`, `gt_integrity.py status` / `verify` | unchanged: they read `~/.claude`, which stays readable |
| `gt_settings.py models` (the page) | unchanged: it only reads |
| the gt-farm calibrate command (gt-farm module) | **terminal**: it reads the service table from the vault, which sandbox mode refuses the shell, and queues the calibration record. Run it from a terminal |
| `<farm-scripts>/gt_gateway.py …` (gt-farm module) | **terminal**: the gateway breaker keeps its state files outside the vault, which sandbox mode refuses the shell |
| `gt_broker.py drain` (what `queue_autodrain` does for you) | `vault_queue_drain` — applies the queued vault writes outside the sandbox, with the same decisions as the script |
| `gt_settings.py set …`, `gt_settings.py models set` / `reset` / `profile` / `apply-calibration`, `gt_model_policy.py choose` / `set` / `clear` / `apply`, `gt_integrity.py unlock` / `lock` / `relock`, `gt_telemetry.py set` / `reset-id` | **terminal**: sandbox mode write-protects `~/.claude` on purpose — a setting that could switch a guard off must never be changed from inside the fence |

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
sign-in  -- details: /gt:gt-settings lotr`, or `LOTR: not configured` when the gateway has
no configuration yet. Read that line back to the user.

`show` lists the model settings together under **Models and cost** — `model_profile`,
`agent_models`, and the calibration settings — beside the others, not after the modules.

**If the argument is `models`** (`/gt:gt-settings models`), or the user asks what model an agent
runs on, what the agents cost, or whether the models are right, show the Models and cost page:

```bash
python3 <plugin>/scripts/gt_settings.py models
```

One row per agent type, in three groups — pipeline stages (with their job types), module agents
(`gt-lotr:*`, `gt:disk-reader`) and skills by intent (fast, balanced, deep, with the number of skills
in each). Each row has its model · effort, **why** (override, profile, task tier or inherit), the last
7 days' runs, tokens and cost (an **estimate**: tokens at list price), and the last calibration
verdict with its date and agreement, or *not calibrated* / *not enough runs*. The header gives the
profile, the session model (Claude Code's `/model`; gt never changes it) and the calibration
settings. Read the page back, then offer the changes it prints at the foot:

```bash
python3 <plugin>/scripts/gt_settings.py models set <agent-type> <model> [<effort>]   # one row; --skill/--plugin for those
python3 <plugin>/scripts/gt_settings.py models reset <agent-type>                    # back to the profile or tier
python3 <plugin>/scripts/gt_settings.py models profile average|very-high|inherit     # applied at once
python3 <plugin>/scripts/gt_settings.py models apply-calibration [<agent-type>|all]  # stored verdicts only
```

`explicit_model_policy` (`leave` default, `warn`, `downgrade`) governs a spawn that NAMES a model:
`warn` adds one advice line (and an escalate-to-Opus line for a security-class spawn on a cheaper model);
`downgrade` also lowers an Opus spawn to the cheaper tier, only where the class table and a measured advice
row (30+ judged runs, Opus needed in 10% or less) agree. Never a security class; the shipped table is empty.

An effort the model does not accept is refused, naming the allowed values (Haiku has none), never
lowered quietly. `apply-calibration` prints each stored verdict and then applies only the ones
asked for; **run it only when the user asks**. To produce fresh verdicts, `/gt:gt-calibrate` runs the
calibration pass now, whatever `model_calibration` says (the budget and weekly-allowance guards
still apply). All of these change settings, so under sandbox mode they are terminal commands.

**Local engines (0.21.4).** Under the tables the page shows each local engine of the gt-farm service table: its
current model, whether a calibration for **that model** exists (from which date and machine) and the verdicts, the
model history, and the routine work the user's persona can hand to a local engine. `python3 <plugin>/scripts/gt_settings.py
farm` prints only that part. An engine whose model has no calibration gets no automatic work: offer
the gt-farm module's calibrate command for that engine (`/gt-farm:gt-farm`, "calibrate my engine"; a dry run first states the Claude cost). The setting
`farm_first` (off, suggest, yes) decides how much gt does for the user; turning it on with no local engine prints how to
get one. Reading the page changes nothing.

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

**If the argument is `gateway`** (`/gt:gt-settings gateway`), or the user asks why gt stopped calling
an engine, show the gateway's settings and each service's circuit:

```bash
python3 <plugin>/scripts/gt_settings.py show            # gateway, gateway_failure_threshold, gateway_cooldown_seconds, gateway_call_timeout_seconds
python3 <farm-scripts>/gt_gateway.py status             # every circuit and the last audit lines (gt-farm)
```

`gateway` (on|off) is the breaker, `gateway_failure_threshold` the consecutive failures that open a
circuit, `gateway_cooldown_seconds` how long it stays open before one probe call, and
`gateway_call_timeout_seconds` the hard timeout the farm applies to a call. The state is advisory and
holds no content. `gt_gateway.py reset SERVICE` closes one circuit by hand. Show the output as it is.

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
| `scope_gate` | `off` · `warn` · `block` | `warn` |
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

**`vault_hints`** (0.19.1, default `on` since 0.21.0) — when `on`, a UserPromptSubmit hook matches each
prompt against `index.md` and adds at most three `- <title> — <path>` lines naming pages that
may already hold the answer. It reads one file and never a page body; nothing is added below
the match threshold or past its 0.8 s budget. Off by default because it costs a little on every
turn. Turn on with `python3 <base_dir>/../../scripts/gt_settings.py set vault_hints on`.

**`lockdown`** (0.20.5, default `strict`) — how much Claude may run without asking.
`strict` · `trust-local` · `trust-network` · `insecure`; each level is a set of allow rules
`gt_lockdown.py` merges into `~/.claude/settings.json` (gt's own rules only, recorded, settings
backed up first; credential files stay denied to the Read tool above strict). The installer
asks for it; `explain lockdown` prints the chart. It reduces prompts and is **not** a security
boundary. A security key: with gt unlock on, changing it needs a fresh confirmation. Restart
Claude Code after a change. `python3 <base_dir>/../../scripts/gt_settings.py set lockdown trust-local`.

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

**`paranoid`** (0.21.0, default `off`) — protect gt's own installed code from being changed
behind your back. A group: the master `paranoid` (`off` | `on`) and one switch per part —
`paranoid.os_lock` (the OS locks gt's hooks, packs, plugin cache and marketplace copies),
`paranoid.code_write_gate` (a presence check before they are unlocked: Touch ID with gt unlock,
else a typed confirmation at a real terminal), `paranoid.bash_guard` (a shell command that writes
to that code stops and asks), `paranoid.signed_manifest` (each hook checks its own files before it
runs; the list is signed with the unlock key, or pinned — weaker — without it),
`paranoid.plugin_dirs` (the plugin cache and marketplace become protected paths) and
`paranoid.require_unlock` (refuse to turn it on until gt unlock is set up). A part left unset
follows the master; set one `on` / `off` to decide it yourself (`paranoid (partial)` then names
what is off). Every key is a security key. **Do not turn any of them off for the user, and do not
try to**: lowering one needs a person at a terminal (or the unlock confirmation), which an
assistant's shell does not have — give the user the command. `explain paranoid` says, in plain
words, who each part is for, what it protects against and what it costs; `gt_integrity.py status`
shows what is locked and sealed, `gt_integrity.py verify` re-checks it, and an upgrade is
`gt_integrity.py unlock --for-upgrade` then the installer (or just the installer).

**`model_profile`** (0.19.1; a normal setting from 0.21.0) — the model and effort each skill runs at, written into the
installed copy of every SKILL.md (never the release source). `average` (a new install's
default): fast skills haiku with no effort setting, balanced sonnet · medium, deep opus · high.
`very-high`: opus · xhigh everywhere. `inherit`: nothing written, every skill runs on the
session's model and effort. It lives with the install choices
(`~/.claude/golden-thread/install-choices.json`), and since 0.21.0 `show` lists it under
**Models and cost** and `set model_profile average` changes it: it records the choice and
re-applies it at once (the same as the policy tool's `choose` then `apply`), no reinstall. The
policy tool does the rest:

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

**Subagents outside gt's stages (0.21.4).** A subagent Claude spawns on its own (an Explore
search, a general-purpose agent) runs on the session's model unless something says otherwise,
so on an opus session a file search costs opus. Two lines in the user's own Claude Code config
fix that; gt does not write either for you:

- `~/.claude/settings.json`: `"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}` — the default
  model for subagents that name none.
- `~/.claude/agents/explore.md`: a user-level Explore agent (`name: Explore`, `model: haiku`
  in its frontmatter), so searches that only locate code run on the smallest model.

Precedence: a model passed on the Agent call wins, then the agent definition's frontmatter, then
`CLAUDE_CODE_SUBAGENT_MODEL`, then the session's model. gt's stage agents pass their model or
carry it in frontmatter, so they keep their tiers either way.

**`context_nudge`** (0.21.4, default `40`) — `off`, or a percent 20-90. When this session's
context reaches it, the per-turn hook shows one line: every reply re-sends the whole session, so
close the thread (the `gt-close` command) and start a fresh one, or `/compact`; Claude is asked to suggest
it once, at a break. Said again only every 15 points above it (40, 55, 70, 85), never every
turn, and it never blocks a prompt. It needs the usage module's context reading.
`python3 <base_dir>/../../scripts/gt_settings.py set context_nudge off` turns it off.

**`cut_handoff`** and **`cut_kind`** (0.22.0) — two separate choices for every cut gt makes (the cut
`auto_compact` makes once the person opts in with `auto`, a percent or a token count, and
`/gt:gt-minimize compact`; `warn`, the default, never cuts). `cut_handoff`: `facts` (default) saves the
session's new findings to the vault and writes a handoff with its key facts; `handoff` writes the handoff
only; `none` is a plain cut with nothing carried over. `cut_kind`: `compact` (default) compacts in place;
`clear` clears the session and puts "Read the handoff at ..." in the fresh session's prompt box (not yet
seen in a live Claude Code window: say so if asked). `clear` with `none` is refused by
`gt_settings.py set`; tell the person why (a clear that carries nothing over discards the session) and
offer `cut_handoff handoff` or `cut_kind compact` instead.

**`voice_prompting`** (0.22.0, default `off`; with `voice_auto_submit` and `voice_key`) — `off`, `hold` or `tap`. Claude Code already has `/voice [hold|tap|off]`, which turns voice on and checks the microphone; this setting adds what people get stuck on: a push-to-talk key of their choice, a backup of `settings.json` and `keybindings.json` before any edit, an exact undo (`off` removes only what gt added), and `python3 <base_dir>/../../scripts/gt_settings.py voice --check`, a read-only preflight. Voice needs a claude.ai sign-in (an API key or a cloud provider makes `/voice` an unknown command) and a local microphone; the preflight says it cannot verify the microphone permission from a script, and that is the person's step (`/voice`, then Allow). Never write the `voiceEnabled` key by hand.

**`telemetry`** (0.22.0, default `off`) — `off` or `local`. gt can keep simple counts of how it is used on this machine (which features ran, which checks passed or failed, which error codes appeared) and write one small payload a day to `~/.claude/golden-thread/telemetry/local.jsonl`, so the person can see exactly what sharing would contain. **Nothing is sent anywhere in this release**: there is no sending code and no destination. A path, a prompt, a note, a name or a credential has no field in it. `share` is not a value; `gt_telemetry.py set share` is refused with `telemetry_no_destination`. It is asked once at install in plain words and defaults to off. Never turn it on for the user: set `local` only when the user asks, with `python3 <base_dir>/../../scripts/gt_settings.py set telemetry local`. To show what it holds, run `python3 <base_dir>/../../scripts/gt_telemetry.py preview` (prints the exact next payload; read-only) or `gt_telemetry.py status`; `gt_telemetry.py reset-id` replaces the random install id. Never claim gt has "no telemetry": say it is off by default, local only, and nothing leaves the machine.

**`rules_set`** (0.22.0, default `full`) — `full` or `lite`. Which Core rules gt injects into every turn. `full` is all ten, with the Stop hook holding a reply to the timestamp rule and the secrets rule. `lite` is four: vault writes go through the queue, no secret value in the session, tests before a commit, and a label on every derived figure; there is no timestamp rule and no timestamp check, the secrets check stays, and the text is under 200 tokens. The starter install (`install.sh --starter`) sets `lite`. `python3 <base_dir>/../../scripts/gt_settings.py set rules_set full` switches back; the rule files in the vault are never changed.

**`queue_autodrain`** (0.22.0, default `on`) — `off` or `on`. When `on`, a vault write queued through `gt_write_queue.py` is applied by the broker at the next safe point, the end of the turn (Stop) or the start of the next session (SessionStart), without anyone running `gt_broker.py drain`. The broker decides exactly as a manual drain does: a claimed file stays queued, a contradiction is escalated, a repeat is not written, and two drains at once apply each request once. `off` leaves the queue for a manual drain, and session start names any request that has waited more than a day (the report card says so too).

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
