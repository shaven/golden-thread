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
curl -fsSL https://github.com/YOUR_USERNAME/golden-thread-plugin/archive/refs/heads/main.tar.gz \
  | tar -xz --strip-components=1 -C /tmp/golden-thread-plugin --mkdir \
  && bash /tmp/golden-thread-plugin/install.sh
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

After installing, **restart Claude Code** — plugins and hooks load at session start.
Then confirm enforcement is actually live, because a rule that is not wired is not a rule:

```bash
echo '{}' | ~/.claude/golden-thread/hooks/inject_core_rules.sh
```

The Core rules should print. Silence or an error means they are not being asserted.

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

## Hosting on GitHub

1. Create a new GitHub repo (public or private).
2. Push this directory to it:
   ```bash
   cd golden-thread-plugin
   git init
   git add .
   git commit -m "Initial release"
   git remote add origin https://github.com/YOUR_USERNAME/golden-thread-plugin
   git push -u origin main
   ```
3. Replace `YOUR_USERNAME` in the install commands above with your GitHub username.
4. To publish a versioned zip: run `bash package.sh` and attach `golden-thread-plugin.zip` to a GitHub release.

---

## Connecting to an Existing Obsidian Vault

Run `/gt:gt-init` and enter the vault path when asked. The `vault_init.py` script uses `ensure_file` throughout — it only creates files that don't exist, so your existing content is safe.

Golden Thread adds these files/directories only if missing:
- `CLAUDE.md` — knowledge page schema
- `index.md` — Knowledge index stub
- `log.md` — activity log stub
- `global-memory/MEMORY.md` — global memory index
- `Projects/CONVENTIONS.md` and `Projects/PROTOCOL.md`

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
