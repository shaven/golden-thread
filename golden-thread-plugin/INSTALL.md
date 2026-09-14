# Golden Thread Plugin — Install Guide

**Requirements:** Python 3.8+, Claude Code (any version).

---

## Option A — Install from GitHub (recommended)

The plugin lives in the `golden-thread-plugin/` subdirectory of the
`golden-thread` repo.

```bash
git clone git@github.com:shaven/golden-thread.git
cd golden-thread/golden-thread-plugin
bash install.sh --vault ~/Documents/GoldenThread
```

`--vault` is what makes this one step instead of two. A **vault** is a plain folder of
markdown where your memory lives, and the Core-rule enforcement hooks are wired
*against* a vault — so an install with no vault leaves those hooks present but inert.
Pass the path and the installer creates it (or connects an existing one) and wires
everything.

| You have | Run |
|---|---|
| no vault yet | `bash install.sh --vault ~/Documents/GoldenThread` |
| an existing vault or Obsidian folder | `bash install.sh --vault /path/to/it` — nothing existing is overwritten |
| no idea yet | `bash install.sh` — at a terminal it asks; answer or skip |
| a deliberate reason for none | `bash install.sh --no-vault` |

**If something else is running the installer** — an agent, a script, CI — and no vault
is configured, it stops with **exit 4** and says what it needs rather than inventing a
directory and claiming `~/.claude/vault-config.json`. That is a question for a person:
where should the vault live? Re-run with `--vault` once you know.

`install.sh` resolves its own paths, so it can be run from anywhere. Run
`bash install.sh --help` for every option.

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

**What gets installed.** gt itself, plus each **module** — an optional plugin that ships
beside gt and is versioned with it. Today there are two, both on by default:

| Module | Plugin | What it adds |
|---|---|---|
| `wiki` | `gt-wiki` | five `/gt-wiki:*` skills for an LLM wiki (ingest, lint, refresh, init) |
| `demo` | `gt-demo` | `/gt-demo:gt-demo`, the guided PizzaBot 3000 tour in its own throwaway vault |

You do not install modules separately — one `install.sh` run installs gt and every module
that is on. To choose:

```bash
bash install.sh --list-modules        # each module, whether it is on, and why
bash install.sh --without demo        # remove it completely; the choice is remembered
bash install.sh --with demo           # bring it back
```

The choice lives in `~/.claude/golden-thread/install-choices.json` and later installs keep it.
A module that is off leaves nothing behind — no plugin cache, marketplace entry, enabled
flag, hooks or hook scripts. gt itself cannot be removed this way.

It copies each plugin into Claude Code's plugin cache — gt's looks like this:

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

Re-running `install.sh` is safe, and **an upgrade from any older release ends where a fresh
install of the newest would**, so skipping releases is fine. On each run it:

1. removes superseded caches and anything an older release installed that this one no
   longer ships (`retired.json`, after a backup) — files it does not recognise are reported
   and left alone;
2. applies one-time **machine migrations** (changes under `~/.claude/` a skipped release would
   have made); the first failure stops the install with `INSTALL INCOMPLETE`, nothing is
   rolled back, and re-running is safe;
3. applies pending **vault upgrades** itself when the vault had no uncommitted changes before
   the install touched it (after a backup; the results are left uncommitted for you to
   review). If the vault holds your own uncommitted work it only reports, and prints the
   command to run. A `PROTOCOL.md` or `CONVENTIONS.md` you edited is never merged
   unattended — it is listed as *needs a person* for `/gt:gt-upgrade`.

`install.sh` does **not** choose or create a vault. The vault path is written to
`~/.claude/vault-config.json` by `/gt:gt-init` (below), which runs `vault_init.py` in
`fresh` or `connect` mode; that step also seeds the vault tools, the inbox, the git
hooks and the first `TASKS.md`. If a vault is already configured when `install.sh`
runs, it refreshes that vault's tools to the installed templates.

`install.sh` registers the nine hooks it owns in `~/.claude/settings.json` (the other five,
the Core-rule enforcement hooks, are wired against the vault) and then
verifies them, printing either `Verified hook wiring → every hook this installer owns
is connected` or a list of what will never run. Read that line: **a file being
installed and a file being wired are different things**, and until 0.9.13 nothing
reported the difference.

After installing, **restart Claude Code** — plugins and hooks load at session start.
Then confirm enforcement is actually live, because a rule that is not wired is not a rule:

```bash
# Are the Core rules being asserted? (the UserPromptSubmit hook)
echo '{}' | ~/.claude/golden-thread/hooks/inject_core_rules.sh

# Are ALL declared hooks wired? (14 in 0.14.0)
python3 ~/.claude/golden-thread/hooks/gt_components.py wiring \
  "<plugin-repo>/golden-thread/<version>"
```

The Core rules should print. Silence or an error means they are not being asserted.

The second command should print `all 14 declared hooks are wired`. It is the broader
check of the two: the first proves one hook answers, while this one compares live
settings against the list of every hook the release declares — so it can report the
case the first cannot, which is a hook that was never registered at all. Anything it
lists as `unwired` never runs, and `badpath` means it is registered against a path that
does not exist on this machine (a version directory removed by a later bump, most often).

Both are worth running on a **second machine** in particular. Files sync; a
`settings.json` on another box does not.

Two more checks after an upgrade:

```bash
python3 ~/.claude/golden-thread/hooks/gt_doctor.py      # includes a modules row: on/off, installed, enabled
python3 "<plugin-repo>/golden-thread/<version>/scripts/gt_upgrade.py" --vault <vault> status
```

---

## Using it with Obsidian (optional)

Nothing requires Obsidian — the vault is plain markdown and git, and Claude Code reads
and writes it with nothing else installed. Obsidian is for *you*, to read and follow
links by hand.

There is no import step: an Obsidian vault **is** a folder.

1. Install Obsidian from <https://obsidian.md> (free).
2. **Open folder as vault**, and select your vault folder.
3. Trust the folder when asked — it is your own.

Every vault this plugin creates carries an `OPEN-IN-OBSIDIAN.md` at its root with the
path filled in, so the instructions are there when you are standing in the folder.

**Plugins that matter for this vault**, in order:

| Plugin | Why |
|---|---|
| **Dataview** (community) | Tasks carry inline fields — `[p:: 1] [waiting:: agent]`. That is Dataview syntax; with it you can sort and query by priority, owner and due date |
| **Obsidian Git** (community) | The vault is a git repo. Commits and pushes on a timer, so your edits do not sit uncommitted while a Claude session works the same tree |
| Backlinks, Graph view (core) | Already on. The `[[wikilinks]]` between pages are the structure |

Templater, Tasks and Kanban are fine plugins this vault does not assume.

**One caution before editing in Obsidian:** `TASKS.md`, `log.md` and every
`decisions.md` are **generated** from per-session files under
`Projects/golden-thread/spool/`. An edit typed into them is lost at the next merge —
add decisions with `tools/gt_adr.py` and log lines with `tools/gt_log.py`. Every other
file is yours.

---

## First run

If you installed with `--vault`, your vault already exists and every hook is wired;
skip to creating a project. Otherwise, open any Claude Code session and type:

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

Run `bash install.sh --vault <vault>` again from the repo (after `git pull`), then restart
Claude Code. It installs the newest release of gt and of each module that is on, removes
superseded caches and retired files, applies machine migrations and — if your vault is
committed — vault upgrades. Exactly one version of each plugin is live at a time. Commit your
vault first so the install can apply its upgrades rather than only report them.

**Rollback:** the repo (and a gt-src copy) keeps the previous release, so
`bash install.sh <previous version> --vault <vault>` reinstalls it; your module choices and
vault are kept.

---

## Uninstall

```bash
rm -rf ~/.claude/plugins/cache/golden-thread-plugin \
       ~/.claude/plugins/marketplaces/golden-thread-plugin \
       ~/.claude/golden-thread/hooks
```

Then remove the `golden-thread-plugin` entries from `~/.claude/plugins/installed_plugins.json`
and `~/.claude/plugins/known_marketplaces.json`, the `*@golden-thread-plugin` keys under
`enabledPlugins` in `~/.claude/settings.json`, and every hook whose command points into
`~/.claude/golden-thread/hooks/` — otherwise Claude Code keeps calling hooks that no longer
exist. To drop only an optional part, use `bash install.sh --without <module>` instead.

Your vault and `~/.claude/vault-config.json` are unaffected — the vault is yours, not the plugin's.
