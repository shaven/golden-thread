# Golden Thread Plugin — Install Guide

**Requirements:** Python 3.8+, Claude Code (any version).

---

## Option A — Install from GitHub (recommended)

The plugin lives in the `golden-thread-plugin/` subdirectory of the
`golden-thread` repo.

```bash
git clone git@github.com:shaven/golden-thread.git
cd golden-thread/golden-thread-plugin
bash install.sh
```

Or as a one-liner:

```bash
git clone git@github.com:shaven/golden-thread.git && bash golden-thread/golden-thread-plugin/install.sh
```

`install.sh` resolves its own paths, so it can be run from anywhere.

---

## Option B — Install from zip

Build it with `bash package.sh`, or download it from a GitHub release. It is **not** in
the repo — `*.zip` is gitignored — so a fresh clone will not contain one.

```bash
unzip golden-thread-plugin.zip
cd golden-thread-plugin
bash install.sh
```

---

## Option C — One-liner (no git required)

```bash
curl -fsSL https://github.com/shaven/golden-thread/archive/refs/heads/main.tar.gz | tar -xz -C /tmp \
  && bash /tmp/golden-thread-main/golden-thread-plugin/install.sh
```

---

## What the installer does

`install.sh` installs the **newest version directory present** — it detects the version
rather than carrying a hardcoded one, so this guide does not name a version either. To
pin an older release deliberately: `bash install.sh 0.9.3`.

It copies into Claude Code's plugin cache:

```
~/.claude/plugins/cache/golden-thread-plugin/gt/<version>/
  .claude-plugin/   ← plugin metadata
  skills/           ← the /gt:gt-* skill definitions
  scripts/          ← Python scripts (vault_init, gt_lint, gt_ingest, gt_settings,
                      gt_components, gt_version_check, gt_workers, gt_push_check, …)
  templates/        ← vault scaffold templates, Core rules, git hooks, vault tools
  hooks/            ← Core-rule enforcement
```

The enforcement hooks are **also** copied outside the cache, to
`~/.claude/golden-thread/hooks/`, and `~/.claude/settings.json` references them by
absolute path — so the path survives a project rename, a vault move, or a version bump.

Re-running `install.sh` is safe. Note it **removes superseded caches** for the plugin
rather than leaving them beside the new one.

`install.sh` does **not** choose or create a vault. The vault path is written to
`~/.claude/vault-config.json` by `/gt:gt-init` (below), which runs `vault_init.py` in
`fresh` or `connect` mode; that step also seeds the vault tools, the inbox, the git
hooks and the first `TASKS.md`. If a vault is already configured when `install.sh`
runs, it refreshes that vault's tools to the installed templates.

`install.sh` registers the six hooks it owns in `~/.claude/settings.json` and then
verifies them, printing either `Verified hook wiring → every hook this installer owns
is connected` or a list of what will never run. Read that line: **a file being
installed and a file being wired are different things**, and until 0.9.13 nothing
reported the difference.

After installing, **restart Claude Code** — plugins and hooks load at session start.
Then confirm enforcement is actually live, because a rule that is not wired is not a rule:

```bash
# Are the Core rules being asserted? (the UserPromptSubmit hook)
echo '{}' | ~/.claude/golden-thread/hooks/inject_core_rules.sh

# Are ALL NINE declared hooks wired? (0.9.13+)
python3 ~/.claude/golden-thread/hooks/gt_components.py wiring \
  "<plugin-repo>/golden-thread/<version>"
```

The Core rules should print. Silence or an error means they are not being asserted.

The second command should print `all 9 declared hooks are wired`. It is the broader
check of the two: the first proves one hook answers, while this one compares live
settings against the list of every hook the release declares — so it can report the
case the first cannot, which is a hook that was never registered at all. Anything it
lists as `unwired` never runs, and `badpath` means it is registered against a path that
does not exist on this machine (a version directory removed by a later bump, most often).

Both are worth running on a **second machine** in particular. Files sync; a
`settings.json` on another box does not.

---

## First run

Open any Claude Code session and type:

```
/gt:gt-init
```

Claude will walk you through:
1. Choosing a vault path (default: `~/Documents/Obsidian/GoldenThreadVault`)
2. Entering your domain/team name
3. Wiring the vault to your current project

The rest of the `/gt:gt-*` skills are then ready to use — see
[`MANUAL.md`](MANUAL.md) for the full reference, or run `/gt:gt-settings` to see
everything the plugin does on its own.

---

## Verifying an install, or a fork

```bash
bash golden-thread-plugin/selftest.sh
```

Runs the whole sequence above — `install.sh`, then `fresh`, then `create-project` —
inside a throwaway home and checks that every file the manual names exists, that the
hooks answer, and that the new vault lints clean. Nothing on your machine is touched.
Run it before publishing a fork; to publish a versioned zip, run `bash package.sh` and
attach `golden-thread-plugin.zip` to a release.

---

## Connecting to an Existing Obsidian Vault

Run `/gt:gt-init` and enter the vault path when asked. The `vault_init.py` script uses `ensure_file` throughout — it only creates files that don't exist, so your existing content is safe.

Golden Thread adds these files/directories only if missing:
- `CLAUDE.md` — how a session reads the vault, and the enforcement check
- `index.md` — Knowledge index stub
- `log.md` — activity log stub
- `INBOX.md` — the capture point; rendered at the top of `TASKS.md`
- `TASKS.md` — generated rollup of every project's tasks
- `global-memory/MEMORY.md` — global memory index
- `Projects/CONVENTIONS.md`, `Projects/PROTOCOL.md`, `Projects/INFRASTRUCTURE.md`
- `Projects/golden-thread/` — the Core rules, the vault tools (`gt_tasks.py`,
  `gt_closeout.py`, `gt_session.py`, `gt_edits.py`, `safe_write.py`), and the
  session and pending directories the tools create
- `.githooks/` and `core.hooksPath` — per-edit attribution, and `git init` if the
  vault is not yet a repo

---

## Migrating an Existing Project

If your project already has `.claude/memory/` files or CLAUDE.md constraints:

```
/gt:gt-init      # wire the vault to this project
/gt:gt-ingest    # scan and import existing memory
```

`/gt:gt-ingest` copies, never moves. Your existing files stay untouched.

---

## Updating

Run `bash install.sh` again from the repo (after `git pull`), then restart Claude Code.
It installs the newest version directory it finds and removes superseded caches for the
plugin, so exactly one version is live at a time.

---

## Uninstall

```bash
rm -rf ~/.claude/plugins/cache/golden-thread-plugin
```

Your vault and `~/.claude/vault-config.json` are unaffected — the vault is yours, not the plugin's.
