# Golden Thread — plugin repo

The `gt` and `gt-wiki` Claude Code plugins, plus the templates and hook scripts that
establish a Golden Thread vault. This repo is its own root — it is not part of the
main working repo and shares no rules with it.

## Layout

| Path | What it is |
|---|---|
| `golden-thread-plugin/golden-thread/<ver>/` | The `gt` plugin: skills, scripts, templates, hooks |
| `golden-thread-plugin/golden-thread-wiki/<ver>/` | The `gt-wiki` plugin |
| `golden-thread-plugin/install.sh` | Installs both, wires the hooks, registers the marketplace |
| `.../scripts/vault_init.py` | Vault scaffolding and lifecycle operations |
| `.../scripts/gt_lint.py` | The vault health checks |
| `.../scripts/gt_paths.py` | Vault + core-rules resolution, used by the hooks at run time |

## The rule that governs this repo

**A Core rule is only as durable as the mechanism that re-asserts it.** Storing a
rule in a file is not enforcement — the file may never be read. Anything claiming to
be Core must be wired to a hook and *verified to fire*.

## Landmines

**Hooks must never be referenced from inside the vault.** `settings.json` addresses
them by absolute path, so a path running through a project slug breaks silently on a
rename. They install to `~/.claude/golden-thread/hooks/` and locate the rules at run
time via `gt_paths.py`. This has been re-broken by documentation four times; if you
find a doc pointing at `core-rules/hooks/`, it is wrong.

**Never copy rule text into a hook script.** The injector reads the rule files at run
time. Duplicating a rule into the script creates two copies that drift.

**Bump the version and re-run `install.sh` after changing plugin content.** The
version is the directory name, `install.sh`'s `VERSION`, and `plugin.json` — all
three. Otherwise installed caches keep serving the old content.

**Do not pipe `install.sh` to `head`.** `set -o pipefail` aborts it partway, leaving
the cache updated and registration not done.

**Templates and the vault drift.** `templates/core-rules/*` is meant to stay
byte-identical to a live vault's `core-rules/`. Diff them before assuming either is
current.

**Verify a hook by running it**, not by reading it:

```bash
echo '{}' | ~/.claude/golden-thread/hooks/inject_core_rules.sh
```

**Writing a `Stop`-hook validator is safety-critical.** One that blocks wrongly makes
every session unusable. Fail open on any parse failure, honour `stop_hook_active` so
a block cannot loop, and test the allow cases before the block case.

## Checks

```bash
python3 <plugin>/scripts/gt_lint.py <vault>
```

Thirteen checks. `core-unenforced` is the important one — it catches a rule that is
stored but never re-asserted, which is the exact failure this system exists to close.

## Deeper context, if this machine has the vault

If `~/.claude/vault-config.json` exists, read `vault_path` from it; the notes are at
`Projects/golden-thread/`. **If it is absent, skip this — everything above stands
alone.**

| Question | File |
|---|---|
| Where the pieces live and how they install | `source.md` |
| Current architecture | `design.md` |
| Why, and what was rejected | `decisions.md` |
| Dated findings | `research.md` |
| The Core rules themselves | `core-rules/` |
