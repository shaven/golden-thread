---
name: gt-settings
description: "View and change what Golden Thread does on its own — component drift checking at session start, the session report card at compact, the upstream watch, and whatever an installed module adds. Every optional automatic behaviour is registered in one place, and `show` is the authoritative list."
---

# Golden Thread Settings

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
