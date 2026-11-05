# golden-thread-hermes

A Hermes memory-provider plugin and Observer Hooks guardrail plugin for [Golden Thread](https://github.com/shaven/golden-thread).

## What it does

- **Memory provider** (`golden-thread`) exposes 52 Golden Thread commands as Hermes tools, backed by a local markdown vault.
  - All 39 canonical `/gt:gt-*` commands from the Claude Code plugin are available as `gt_*` Hermes tools.
  - The remaining tools are Hermes-specific helpers, aliases, or stubs for UI-only / external-subsystem commands that have no direct Hermes equivalent.
  - Automatic context injection from the vault's `Core.md` and the active project's notes.
- **Observer Hooks guardrails** (`golden-thread-guardrails`) enforces selected Golden Thread rules without requiring explicit tool calls.

## Hermes limitations

Hermes does not provide a general tool-level button or side-pane API. Commands that in Claude Code render UI (e.g. `gt-pane-buttons`, interactive visualizations) are exposed as guidance-only tools that return plain-text instructions or filesystem artifacts.

## Install

```bash
cd /path/to/golden-thread/plugins/hermes
pip install -e .
```

Then activate the memory provider in Hermes:

```yaml
memory:
  provider: golden-thread

plugins:
  golden-thread:
    vault_path: /mnt/obsidian-vaults/eman-mobile/golden-vault
    active_project: hermes-golden-thread
```

## Configuration

The provider reads `plugins.golden-thread` from `config.yaml`:

- `vault_path` — absolute path to the Golden Thread vault directory.
- `active_project` — slug of the project to open by default.
- `core_rules_max_lines` — max lines from `Core.md` to inject (default: 200).
- `project_context_max_lines` — max lines from project docs to inject (default: 400).

## Tools

All tools return JSON strings for Hermes. A complete list is available by calling `gt_help()`.

### Canonical Golden Thread commands

`gt_allin`, `gt_allin_commit`, `gt_brief`, `gt_calibrate`, `gt_close`, `gt_complete`, `gt_context`, `gt_create`, `gt_digest`, `gt_doctor`, `gt_farm`, `gt_forget`, `gt_grant`, `gt_handle`, `gt_help`, `gt_implement`, `gt_ingest`, `gt_init`, `gt_lint`, `gt_list`, `gt_minimize`, `gt_open`, `gt_optimize`, `gt_plan`, `gt_promote`, `gt_query`, `gt_refresh`, `gt_review`, `gt_route`, `gt_runbook_lint`, `gt_scan`, `gt_settings`, `gt_setup`, `gt_sync`, `gt_upgrade`, `gt_validate`, `gt_validation`, `gt_watch`, `gt_work`.

### Hermes-specific helpers / stubs

`gt_decide`, `gt_demo`, `gt_demote`, `gt_flow`, `gt_lotr`, `gt_lotr_research`, `gt_usage`, `gt_visualize`, `gt_wiki_ingest`, `gt_wiki_init`, `gt_wiki_lint`, `gt_wiki_refresh`, `gt_workflow`.

- `gt_decide` is a Hermes alias for recording an ADR under the active project.
- `gt_demo` returns a guided tour as text commands.
- `gt_visualize` returns guidance because Hermes has no tool-side visualization pane.
- `gt_usage` returns guidance because Hermes does not expose a Claude-style usage plan API.
- `gt_lotr*` and `gt_wiki*` stubs record requests or create markdown files for external subsystems that are not wired into Hermes.

## Tests

```bash
cd /path/to/golden-thread/plugins/hermes
python3 -m pytest -q
```

There is also a 52-tool Hermes smoke test in the `golden-thread-hermes-integration` skill references.
