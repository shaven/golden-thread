# Golden Thread Plugin — Install Guide

**Requirements:** Python 3.8+, Claude Code (any version).

---

## Option A — Install from GitHub (recommended)

```bash
git clone https://github.com/YOUR_USERNAME/golden-thread-plugin
cd golden-thread-plugin
bash install.sh
```

Or as a one-liner:

```bash
git clone https://github.com/YOUR_USERNAME/golden-thread-plugin && bash golden-thread-plugin/install.sh
```

---

## Option B — Install from zip

Download `golden-thread-plugin.zip` from the GitHub releases page (or the repo root), then:

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

`install.sh` copies only the four directories the plugin needs into Claude Code's plugin cache:

```
~/.claude/plugins/cache/golden-thread-plugin/golden-thread/0.1.0/
  .claude-plugin/   ← plugin metadata
  skills/           ← six /gt-* skill definitions
  scripts/          ← Python scripts (vault_init.py, gt_ingest.py, gt_lint.py)
  templates/        ← vault scaffold templates
```

Re-running `install.sh` is safe — it overwrites only those directories.

---

## First run

Open any Claude Code session and type:

```
/gt-init
```

Claude will walk you through:
1. Choosing a vault path (default: `~/Documents/Obsidian/GoldenThreadVault`)
2. Entering your domain/team name
3. Wiring the vault to your current project

The skills `/gt-ingest`, `/gt-work`, `/gt-promote`, `/gt-lint`, and `/gt-query` are then ready to use.

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

Run `/gt-init` and enter the vault path when asked. The `vault_init.py` script uses `ensure_file` throughout — it only creates files that don't exist, so your existing content is safe.

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
/gt-init      # wire the vault to this project
/gt-ingest    # scan and import existing memory
```

`/gt-ingest` copies, never moves. Your existing files stay untouched.

---

## Updating

Run `bash install.sh` again from the repo (after `git pull`) or from a new zip. The versioned directory means newer versions install alongside older ones without conflict.

---

## Uninstall

```bash
rm -rf ~/.claude/plugins/cache/golden-thread-plugin
```

Your vault and `~/.claude/vault-config.json` are unaffected — the vault is yours, not the plugin's.
