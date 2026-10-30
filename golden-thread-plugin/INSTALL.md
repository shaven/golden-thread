# Golden Thread Plugin — Install Guide

> **Reader:** installing for the first time, or verifying a fork
> **Stamped for gt 0.22.0:** the counts and version numbers in this document are checked against the code at every release; the prose is re-read when the document is edited, not at every release.

**Requirements:** Python 3.8+, Claude Code (any version; 2.1.136 or newer uses the plugin hook route, 2.1.290 or newer is needed for the gt pane; below it the pane is neither asked about nor installed).

---

## Option A — From the Claude prompt (recommended)

No terminal and no clone beyond updating Claude Code. **First, update Claude Code**, so the install and
the cost-saving features that need a recent version all work: in a terminal run `claude update`, then
`claude --version`. It should print **2.1.300 or newer** (gt installs from 2.1.292, but the in-session
compact and the `auto_compact` busy check need 2.1.300). Then, in Claude Code:

`<github repo>` stands for the GitHub address that holds the gt repository; replace it in every command below. For the public build that is the address of the project's GitHub account; a team that keeps its own mirror uses its mirror's address instead.

```
/plugin marketplace add <github repo>/golden-thread.git
/plugin install gt-all@golden-thread-plugin
```

`gt-all` is a bundle: Claude Code installs gt and **every** module as dependencies, so there is nothing
to pick plugin by plugin: gt-wiki, gt-flow, gt-usage, gt-visualize, gt-report-card, gt-watch, gt-demo,
gt-pane, gt-lotr, gt-farm, gt-workflow and gt-recall. Installing a module turns none of its behaviour on (LOTR does
nothing until `lotr init`, farm needs an engine you set up). `gt@golden-thread-plugin` alone is the minimal
install.

Restart Claude Code. The first session shows `gt: Golden Thread is installed but not set up on this
machine`; run **`/gt:gt-setup`**. It asks, in the prompt:

1. **Install everything (recommended)**, choose modules, or core only;
2. the security level and the other install questions (the same words as `install.sh --questions`);
3. where your vault lives (or none yet);

then shows one review screen and runs the installer for you with those answers. That is the whole
install; restart once more when it says so.

**The context nudge needs a status line.** The context nudge (default 40%) reads the session size from the gt-usage status line, and a default install sets none, so the nudge never speaks. Turn it on with `--statusline yes` (or by answering yes to the installer's status-line question), or later with `python3 ~/.claude/golden-thread/hooks/gt_usage.py --setup-statusline` in a terminal; it never replaces a status line you already have.

The installer it runs is the one in the marketplace's
checkout, and it leaves the marketplace as Claude Code manages it.

**To leave modules out:** answer "Choose modules" (or "Core only") in the first question, or run
`/gt:gt-setup` again at any time to add or remove one; from a terminal, `install.sh --without lotr`
removes a module and keeps it off, and `install.sh --with lotr` adds it back (the choice is remembered).

A short replay walkthrough of this route (a replay of recorded text, not a screen recording; start to finish about two minutes) has its script in [docs/install-video-script.md](docs/install-video-script.md) and its captions in [docs/install-video.srt](docs/install-video.srt).

To test the prompt route before the repository is published, or from a fork, use a path: `/plugin
marketplace add /path/to/checkout`, the folder holding `.claude-plugin/marketplace.json`.

---

## Option B — Install from GitHub with a terminal

The plugin lives in the `golden-thread-plugin/` subdirectory of the
`golden-thread` repo.

```bash
git clone <github repo>/golden-thread.git
cd golden-thread/golden-thread-plugin
bash install.sh --vault ~/GoldenThread
```

`--vault` is what makes this one step instead of two. A **vault** is a plain folder of
markdown where your memory lives, and the Core-rule enforcement hooks are wired
*against* a vault — so an install with no vault leaves those hooks present but inert.
Pass the path and the installer creates it (or connects an existing one) and wires
everything.

| You have | Run |
|---|---|
| no vault yet | `bash install.sh --vault ~/GoldenThread` |
| an existing vault or Obsidian folder | `bash install.sh --vault /path/to/it` — nothing existing is overwritten |
| no idea yet | `bash install.sh` — at a terminal it asks; headless it uses `~/GoldenThread` |
| a deliberate reason for none | `bash install.sh --no-vault` |

**Where the vault lives (0.21.4).** Keep it in a plain local folder: `~/GoldenThread`
(`%USERPROFILE%\GoldenThread` on Windows). gt writes into its vault constantly, and two kinds
of place go wrong once the vault is large: the **Documents** folder, which macOS (folder-access
prompts, iCloud Desktop & Documents) and Windows (Controlled Folder Access, OneDrive folder
backup) guard, and a **synced or shared drive** (iCloud Drive, Dropbox, OneDrive, Google
Drive, Box, an SMB/AFP/NFS share, a network drive), where sync conflicts corrupt it. The
installer, `/gt:gt-setup` and `/gt:gt-init` warn about such a path in plain words and recommend
`~/GoldenThread`; at a terminal you confirm or change it. It is a warning, not a refusal: you
can keep the path. `gt_doctor` shows the same as a `vault-location` WARN (it never changes the
exit code). Check any path yourself:
`python3 ~/.claude/golden-thread/hooks/gt_vault_location.py check <path>`.

**If something else is running the installer** — an agent, a script, CI — and no vault
is configured, it sets the vault up at the default, `~/GoldenThread`
(`%USERPROFILE%\GoldenThread` on Windows), and prints the path on stderr (0.21.4; it used to
stop with exit 4). Exit 4 is now only for a default that could not be set up. Pass `--vault`
for another place, or `--no-vault` for none.

At a terminal a new install first asks which modules you want (everything, let me choose, or core
only). `--all-modules` and `--core-only` answer that question without a terminal, and remembered
choices are kept like `--with`/`--without`.

`install.sh` resolves its own paths, so it can be run from anywhere. Run
`bash install.sh --help` for every option.

**One module, not the whole install (0.21.4).** Once gt is installed, `bash install.sh --only NAME`
adds or updates a single module (NAME: the module, its plugin, or one of its skills) without the
questions, the vault work or touching anything else, and ends with a self-proof of that module
(`--only NAME --check` says what it would do and changes nothing). A later full install ends
exactly where a fresh one would. See MANUAL.md, *Modules*.

**On Windows** (0.19.2; complete in 0.20.1) run the same installer from Git Bash, or run `install.cmd` from
cmd.exe, PowerShell or Explorer — it finds Git Bash and hands `install.sh` every argument
unchanged (`install.cmd --vault C:\Users\you\GoldenThread`; `/uninstall`, `/check`
and `/?` are understood too). Both need
[Git for Windows](https://git-scm.com/download/win), which Claude Code on Windows needs anyway,
and Python 3.8+ from [python.org](https://www.python.org/downloads/) with *Add python.exe to
PATH* ticked. See *Windows* below.

**A failed install is rolled back** (0.20.1). If the installer exits non-zero after it has
started writing — any exit but 4, which means "installed, now choose a vault", and 9 under
`--force-manifest-mismatch` — it puts back
`~/.claude/settings.json`, the plugin registrations, the golden-thread-plugin marketplace and
cache, `~/.claude/golden-thread` (its `backups/` are kept) and `vault-config.json` exactly as they
were, and says `ROLLED BACK`. The vault is not rolled back; it has its own pre-write backup.

---

## Option C — Install from zip

Build it with `bash package.sh`, or download it from a GitHub release. It is **not** in
the repo — `*.zip` is gitignored — so a fresh clone will not contain one.

```bash
unzip golden-thread-plugin.zip
cd golden-thread-plugin
bash install.sh
```

---

## Option D — One-liner (no git required)

```bash
curl -fsSL <github repo>/golden-thread/archive/refs/heads/main.tar.gz | tar -xz -C /tmp \
  && bash /tmp/golden-thread-main/golden-thread-plugin/install.sh
```

The source folder may be deleted afterwards (a reboot clears `/tmp`). Everything keeps working
from the installed copy: `gt_doctor.py` and its post-install gate check the plugin cache
instead, and the doctor's `version` row says only that it cannot tell whether a newer release
exists (0.20.1). Re-run the one-liner to upgrade.

---

## What the installer does

`install.sh` installs the **newest version directory present** — it detects the version
rather than carrying a hardcoded one, so this guide does not name a version either. To
pin an older release deliberately: `bash install.sh 0.14.0`.

**What gets installed.** gt itself, plus each **module** — an optional plugin that ships
beside gt and is versioned with it. `bash install.sh --list-modules` prints the modules in the
tree you have, with each one's state and why; at the time of writing they are:

| Module | Plugin | Default | What it adds |
|---|---|---|---|
| `wiki` | `gt-wiki` | on | five `/gt-wiki:*` skills for an LLM wiki (query, ingest, lint, refresh, init) |
| `demo` | `gt-demo` | on | `/gt-demo:gt-demo`, the guided PizzaBot 3000 tour in its own throwaway vault |
| `watch` | `gt-watch` | on | `/gt-watch:gt-watch` and its session-start report; fetches nothing until the `watch` setting is `report` |
| `report-card` | `gt-report-card` | on | the session report card and close-out question (hooks, no command) |
| `flow` | `gt-flow` | on | `/gt-flow:gt-flow`, an offline HTML timeline of knowledge moving up the ladder |
| `visualize` | `gt-visualize` | on | `/gt-visualize:gt-visualize`, a codebase in 3D — how-it-works walkthrough or code city (three.js inlined) |
| `usage` | `gt-usage` | on | `/gt-usage:gt-usage`, the plan-allowance meter (5-hour, weekly and spend windows) |
| `pane` | `gt-pane` | asked (0.21.0) | `/gt-pane:gt-pane` and `/gt-pane-buttons`: a 28-column pane of 12 plain-words buttons you can reorder, rename, add and remove (Claude Code 2.1.296 or newer; below it the module is off and the question is not asked) |
| `farm` | `gt-farm` | **off** | `/gt-farm:gt-farm`, work packets for an external AI service |
| `lotr` | `gt-lotr` | **off** | `/gt-lotr:gt-lotr`, one MCP gateway in front of GitHub, Jira, Microsoft 365 and others |
| `workflow` | `gt-workflow` | **off** | `/gt-workflow:gt-workflow` (short form `/gt-workflow NAME`), saved chains of steps you own and run by name: gt tools, LOTR calls, a local engine, and the steps that wait for you or Claude |

`farm` is off for a fresh install. A machine upgrading from a gt that shipped `/gt:gt-farm`
keeps it on: a one-time machine migration records that choice, so nobody loses a command
they had because it moved into a module.

You do not install modules separately — one `install.sh` run installs gt and every module
that is on. To choose:

```bash
bash install.sh --list-modules        # each module, whether it is on, and why
bash install.sh --without demo        # remove it completely; the choice is remembered
bash install.sh --with farm           # add one that is off (workflow and lotr are off too)
```

The choice lives in `~/.claude/golden-thread/install-choices.json` and later installs keep it.
A module that is off leaves nothing behind — no plugin cache, marketplace entry, enabled
flag, hooks or hook scripts — and for `watch`, the crontab line it installed (only the line
tagged `# gt-watch`, after a backup). Those lines are saved under
`~/.claude/golden-thread/module-cron/`, and `--with watch` puts exactly them back (crontab
backed up first, every other line untouched). gt itself cannot be removed this way. A setting
you gave a module is kept while it is off and applies again when you turn it back on.

It copies each plugin into Claude Code's plugin cache — gt's looks like this:

```
~/.claude/plugins/cache/golden-thread-plugin/gt/<version>/
  .claude-plugin/   ← plugin metadata
  skills/           ← the /gt:gt-* skill definitions
  scripts/          ← Python scripts (vault_init, gt_lint, gt_ingest, gt_settings,
                      gt_components, gt_version_check, gt_workers, gt_push_check, …)
  templates/        ← vault scaffold templates, Core rules, git hooks, vault tools
  hooks/            ← Core-rule enforcement
  agents/           ← the pipeline stage agents gt:extract … gt:place (0.20.1)
  workflows/        ← the gt:pipeline-stage workflow (0.20.1)
```

The enforcement hooks are **also** copied outside the cache, to
`~/.claude/golden-thread/hooks/`. On the settings route `~/.claude/settings.json` references them by
absolute path — so the path survives a project rename, a vault move, or a version bump; on the default
plugin route `settings.json` holds only `enabledPlugins` and the plugins' `hooks/hooks.json` run the
commands wired under `~/.claude/golden-thread/wired/` (see *Where the hooks live: two routes* below).

Re-running `install.sh` is safe, and **an upgrade from any older release ends where a fresh
install of the newest would**, so skipping releases is fine. On each run it:

1. removes superseded caches and anything an older release installed that this one no
   longer ships (`retired.json`, after a backup) — files it does not recognise are reported
   and left alone;
2. applies one-time **machine migrations** (changes under `~/.claude/` a skipped release would
   have made); the first failure stops the install with `INSTALL INCOMPLETE` and the install
   is rolled back (see *A failed install is rolled back* above); re-running is safe;
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

`install.sh` registers the hooks it owns — gt's, plus each module's while it is on — in each
plugin's `hooks/hooks.json` on the default plugin route, or in `~/.claude/settings.json` on the
older settings route, and prints how many, and how many more (the Core-rule enforcement
hooks) the vault step wires against the vault. It then verifies them, printing either
`Verified hook wiring → every hook this installer owns is connected` or a list of what will
never run. Read that line: **a file being installed and a file being wired are different
things**, and until 0.9.13 nothing reported the difference. The counts are not repeated here
because they change with the release and your module choices; the installer's own lines are
the ones to read.

After installing, **restart Claude Code** — plugins and hooks load at session start.
Then confirm enforcement is actually live, because a rule that is not wired is not a rule.
One command answers it — the release gate the installer already ran, now against the
restarted machine:

```bash
# macOS / Linux / Git Bash
python3 ~/.claude/golden-thread/hooks/gt_doctor.py post-install --vault <vault>
```

```bat
:: Windows cmd.exe
py -3 "%USERPROFILE%\.claude\golden-thread\hooks\gt_doctor.py" post-install --vault <vault>
```

```powershell
# Windows PowerShell
py -3 "$env:USERPROFILE\.claude\golden-thread\hooks\gt_doctor.py" post-install --vault <vault>
```

(No `py`? Use the full path of the Python the installer printed as `Python: ...`.) Every row
should be PASS or INFO. `wiring` compares live settings against every hook the release
declares, so it reports the case nothing else can — a hook that was never registered at all;
`smoke-rules` runs the installed Core-rule injector and checks every shipped rule comes out.
Anything `unwired` never runs; `badpath` means it is registered against a path that does not
exist on this machine.

It is worth running on a **second machine** in particular. Files sync; a `settings.json` on
another box does not. For the whole health picture, run the doctor without `post-install`
(`/gt:gt-doctor` in Claude Code does the same): it adds the modules, vault, scheduled jobs,
workers and push rows. On a healthy fresh install it reports no WARN — a vault with no git
remote is an `i` (information) row saying there is nothing to push, and on Windows the
`workers` row says it is not supported there.

After an upgrade, `gt_upgrade.py ... status` (in the release's `scripts/`) lists vault
migrations still pending; `/gt:gt-upgrade` applies them.

---

## Lockdown level: asked at install (0.20.5)

How much Claude may run without asking. Most of the friction in a session is approval prompts; a lockdown level is a set of Claude Code allow rules that gt writes into `~/.claude/settings.json` for the level **you** choose, so routine work stops asking. The installer asks for it (`./install.sh --lockdown <level>` to answer in advance; with no terminal a new install stays strict and an upgrade keeps its level). Change it any time with `/gt:gt-settings lockdown <level>` (`gt_settings.py set lockdown <level>`), then restart Claude Code. gt writes and removes only its own rules, records them, backs `settings.json` up first and logs every change (`~/.claude/golden-thread/lockdown/`); uninstall takes them out. With gt unlock on, changing the level needs a fresh confirmation. gt never changes the level by itself.

<!-- lockdown-chart:start -->
| Level | Runs without a prompt | Still asks |
|---|---|---|
| **strict** | nothing added -- exactly what Claude Code asks today (default) | everything Claude Code asks today |
| **trust local** | read-only shell (ls, cat, grep, wc, ...) and git (status, log, diff, show, branch); edits in the project; running tests; git add/commit | network, remote hosts, pushes, installs, deletes, anything not listed |
| **trust network** | trust local, plus ssh/scp/rsync, git push/pull/fetch, web fetch; curl/wget and npm/pip/brew install while install_guard is on | any other shell command or edit outside the project |
| **insecure** | trust network, plus any shell command, any edit, web search | LOTR send/merge/delete still ask for consent |

At every level above strict, Claude's Read tool is denied credential files (ssh keys, cloud and gh credentials, Claude Code's login, .env). That guards the Read tool only: from trust local up a shell command can still print a file, so Core rule 3 (no secret's value in the session) remains the line. These levels reduce prompts; they are not a security boundary.
<!-- lockdown-chart:end -->

## What an install asks, in order (0.21.0)

`bash install.sh --questions [--json]` prints every question and installs nothing. Security comes
first. Each answer is a flag, or a key in `--answers FILE` (the JSON the listing prints):

| # | Question | Flag | Default |
|---|---|---|---|
| 1 | How much may Claude do without asking? | `--lockdown strict\|trust-local\|trust-network\|insecure\|paranoid` | strict |
| 2 | Fence Claude's shell off the vault and gt's files? | `--sandbox-mode off\|on` | off |
| 3 | Lock gt's installed files? Not asked: the `paranoid` level answers it; the flag still combines it with a lower level | `--paranoid off\|on` | off |
| 4 | Quality against token cost | `--token-profile minimum\|standard\|maximum` | standard |
| 5 | Which model size for gt's skills? | `--model-profile average\|very-high\|inherit` | average |
| 6 | Your role | `--persona` | none |
| 7 | Shape drafts and summaries for that role? | `--persona-aligned on\|off` | on |
| 8 | How long should replies be? | `--reply-length normal\|brief\|terse` | normal |
| 9 | Compact idle sessions while the cache is warm? | `--expiry-guard auto\|warn-only\|off` | set by the token profile (auto when standard) |
| 10 | gt-usage's status line? | `--statusline` | no |
| 11 | ast-grep for code search? | `--ast-grep` | no |
| 12 | Add the gt button pane? | `--with pane` / `--without pane` | no (yes for plain-mode roles) |
| 13 | Open the guide? | `--open-guide` | yes at a terminal; skipped in CI, SSH and headless runs |

**Not at a terminal**, a *new* install with a security question (1-3) unanswered **exits 5** before
installing and names the flags to pass. `--accept-defaults` takes the defaults on purpose (CI); the
summary lists each choice as yours, chosen for you, or NOT applied. An upgrade keeps your current values.
Through Claude, `/gt:gt-init` asks each choice, security first, shows one review screen, then installs; on
exit 5 it asks and never retries with the defaults. The sandbox mode, paranoid, `--hook-mode` and
`--accept-defaults` flags are listed in `./install.sh --help`.

## Where the hooks live: two routes (0.21.0)

| Route | When | Registered in |
|---|---|---|
| **plugin** | Claude Code **2.1.136 or newer**, the default | each plugin's `hooks/hooks.json` (gt, gt-watch, gt-report-card, gt-usage, gt-pane); `settings.json` is not written |
| **settings** | older Claude Code, or a version the installer cannot read | `~/.claude/settings.json`, by absolute path into `~/.claude/golden-thread/hooks/` |

Every plugin-route entry runs the command the installer wired for this machine, from the **wired
directory**, `~/.claude/golden-thread/wired/`. `--hook-mode plugin|settings` (or `GT_HOOK_MODE`) overrides
the choice. A hook is wired once under either route. **Upgrading from 0.20.5** moves gt's own
`settings.json` hook entries out after a backup; hooks that are yours stay. Restart Claude Code afterwards.
`python3 ~/.claude/golden-thread/hooks/gt_components.py` and `gt_doctor.py` report on whichever route is used.

## gt unlock: security, off by default (0.20.1)

The installer does not turn it on; it ends by saying it is off and how to turn it on. What it
touches when you do:

* `~/.claude/golden-thread/hooks/gt_unlock*.py`, `gt_unlockd*.py`, `gt_ipc.py`, `gt_tty.py`, `gt_vault_location.py`, `gt_lock.py`,
  `qrcodegen.py` (+ its MIT licence) — copied on every install, run only when unlock is on or
  you call them. Two hook registrations (`gt_unlock.py hook session-start|session-end`) return
  at once when unlock is off.
* **macOS:** `~/.claude/golden-thread/bin/gt-presence`, the Touch ID helper, built from the
  shipped Swift source with the Xcode Command Line Tools and ad-hoc signed (skipped, and said
  so, without them; a Developer ID signed release binary with a `gt-presence.release` marker
  beside it is never rebuilt over).
* **Windows:** nothing extra; Windows Hello is reached through `gt_unlock_hello.ps1`, run by
  Windows PowerShell 5.1 with `-ExecutionPolicy Bypass` for its own process only.
* State, once you enrol: `~/.claude/golden-thread/unlock/` (700; files 600). An administrator
  floor, if your organisation sets one, lives in `/Library/Application Support/gt/`,
  `%ProgramData%\gt\` (or `HKLM\SOFTWARE\Policies\gt`) or `/etc/gt/`.

Turn it on with `gt_unlock.py enroll …` then `gt_unlock.py policy enable`; verify with
`gt_unlock.py verify` or `/gt:gt-doctor` (rows `unlock` and `security`). Step-by-step per
platform, and what each level does and does not protect: [`SECURITY.md`](SECURITY.md).

## A fingerprint for pushes and commits: off by default (0.20.4)

Three settings, all off, for the repos you name in `push_fingerprint_repos`: `push_fingerprint`
(Touch ID or Windows Hello before every push), `push_fingerprint_seal_token` (the push token
sealed behind it, so a plain `--no-verify` push still asks; limits in SECURITY.md §5.5a) and `commit_fingerprint` (macOS: a Secure
Enclave key signs every commit). They need gt unlock with one enrolled factor; turning one on
installs a `pre-push` hook or sets the repo's git signing config, and `off` takes exactly that
away. What it touches:

* `~/.claude/golden-thread/hooks/gt_push_guard.py` and `gt_sign.py`, copied on every install, run
  only when a setting is on or you call them.
* In each named repo: a marked `pre-push` hook (a hook of your own is left alone, and `on` refuses),
  local `credential.https://<host>.helper` entries with token sealing, and local `gpg.*`,
  `commit.gpgsign`, `tag.gpgsign`, `user.signingkey` with commit signing. The earlier values are saved
  and put back by `off`.
* State: `~/.claude/golden-thread/push-guard.json`, `commit-sign*.json`, `commit-sign.pub`,
  `commit-sign.allowed_signers`, `bin/gt-sign`.

Setup, proof and back-out, step by step: [`SECURITY.md`](SECURITY.md) §5.5b.

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
1. Choosing a vault path (default: `~/GoldenThread`)
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

### macOS: "Operation not permitted" writing the vault

macOS can refuse one Python writes it allows another: a file carrying `com.apple.provenance`, or a privacy-protected folder.
Seen 2026-10-03: Homebrew's `python3.9` -- the default `python3` on that Mac -- could not replace
the vault's `log.md`, while `/usr/bin/python3` could. Since 0.20.1 gt words this as one line
naming the interpreter and the fix (`gt_log.py` exits `5`; the broker holds the request; nothing
is half-written), and `install.sh` picks ONE interpreter for hooks, tools and scheduled jobs by
an actual write probe -- create, write, `os.replace`, remove in the vault and in
`~/.claude/golden-thread`, and open `log.md` for append -- preferring `/usr/bin/python3` when it
is a working 3.8+. It is recorded in `~/.claude/golden-thread/interpreter.json` and
`~/.claude/golden-thread/python`; the hook wrappers and the `.py` hook commands in
`settings.json` use it instead of bare `python3`. Since 0.20.1, when the `python3` on your PATH
is one the probe REFUSED, Claude's own shell gets the same remedy Windows has: a `python3` shim
in `~/.claude/golden-thread/bin/` (put first on PATH for Claude's Bash commands at session
start, through `$CLAUDE_ENV_FILE`) runs the chosen interpreter, and the gt-vault MCP server is
started by that interpreter's full path. A `python3` that passes the probe is never overridden.
To see where you stand:

```bash
python3 ~/.claude/golden-thread/hooks/gt_write_probe.py probe     # PASS/FAIL per interpreter
/gt:gt-doctor                                                    # the write-probe row
```

Fix: re-run `install.sh` (it re-probes and re-records), run gt's tools with the interpreter it
names, or give that Python Full Disk Access (System Settings > Privacy & Security). A recorded
interpreter that later disappears (a `brew upgrade`) shows as `badpath` in the wiring check
rather than letting every hook fail open. In your own terminal `python3` is untouched.

See *Troubleshooting* in [`MANUAL.md`](MANUAL.md) for the same note.

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
  `gt_closeout.py`, `gt_session.py`, `gt_edits.py`, `gt_log.py`, `gt_adr.py`,
  `gt_events.py`, `gt_spool.py`, `safe_write.py`), and the session, spool and pending
  directories the tools create
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

**Upgrading from 0.14.0.** `/gt:gt-watch`, the report card and `/gt:gt-farm` moved out of gt
into the `watch`, `report-card` and `farm` modules; the commands are now `/gt-watch:gt-watch`
and `/gt-farm:gt-farm`. The install prints `Moved: /gt:gt-watch → /gt-watch:gt-watch` once for
each command you had, adding `(module <name> is off: ./install.sh --with <name>)` when that
module ends up off. One install over 0.14.0 ends where a fresh 0.15.0 install with the
same module choices would, and your hook-dir files and your own hooks are left in place.

**Installing from a published copy (0.17.3+):** the published copy has the repository's layout, so run
`install.sh` from its `golden-thread-plugin/` folder. Before copying anything, `install.sh` checks
every file against the `SHA256SUMS` at the repository root and names any file that is missing,
changed or not where the list puts it. **By default it then installs anyway, marked unverified**
— someone who simply downloaded the repository is never blocked. `--require-checksum` (or
`GT_REQUIRE_CHECKSUM=1`) turns a mismatch into a refusal (exit 8, nothing copied); use it on the
machine receiving a publish. A tree with no `SHA256SUMS` installs as it stands.

**The shared transfer copy is not installed from (0.20.2).** The shared transfer copy passes code between machines;
`install.sh` run inside it (it sees `SOURCE.json` at the root) refuses with exit 9 before anything is
copied. Copy it into your repository with `copygt.sh` and run `install.sh` there. To install from the
shared copy on purpose: `GT_INSTALL_FROM_SHARED=1 bash install.sh`.

**Windows** (native since 0.19.2, complete in 0.20.1; tested on Windows 11 with Git for Windows
2.56 and Python 3.12).
`install.sh` and the hooks are bash scripts and run under Git Bash; `install.cmd` is only a
launcher for it. The installer resolves a real Python once — `python3`, then `python`, then
`py -3` — and **the Microsoft Store Python does not count**: neither the "Python was not found"
stub Windows puts on PATH as `python3`, nor a real Store install (its virtualised AppData makes
writes under your profile unreliable). With no other Python 3.8+ it stops before installing
anything and says what to install. The interpreter it found is written into the hook commands
and to `~/.claude/golden-thread/python` for the hook wrappers.

*`python3` in Claude's shell (0.20.1).* Skills tell Claude to run `python3 <tool>.py`, and in
Git Bash `python3` is the Store stub. The installer writes a small `python3` shim — the Python
it resolved, in UTF-8 mode, with `\r` stripped from piped output — to
`~/.claude/golden-thread/bin/python3` and to `~/bin/python3` (Git for Windows puts `~/bin` first
on PATH in every Git Bash login shell). At session start gt's component check adds the first
to PATH and sets `PYTHONUTF8=1` through `$CLAUDE_ENV_FILE`, which Claude Code sources before every
Bash command. A `~/bin/python3` that is not gt's is left alone. Claude Code's PowerShell tool
does not read `$CLAUDE_ENV_FILE`; there `python` (python.org's name) works as it is.

*Scheduled jobs (0.20.1).* Jobs are labelled `io.goldenthread.gt-<job>` (0.20.1; an install
moves a job still under the old vendor-prefixed label). A job is reported installed
only when the scheduler accepted it: a refused registration puts the files back and says NOT
INSTALLED (0.20.1). On Linux a job is a systemd user timer, or a tagged crontab line where
there is no user systemd. On Windows `gt_schedule.py install|check|remove|list|reconcile` use Task
Scheduler (`schtasks`), per user and without admin: the task runs
`~/.claude/golden-thread/jobs/gt-<job>.cmd`, which appends to the same `<job>.out` / `.err` logs
the macOS jobs write. `install` runs the task once and reads Task Scheduler's Last Result, as it
reads launchd's exit code on macOS. Like a macOS job, a task runs only while you are logged on
at the desktop — from an SSH session `install` registers it and says it is NOT PROVEN; prove it
later with `gt_schedule.py check <job>`.

*gt-lotr on Windows (0.20.1).* gt-lotr 0.3.0 serves its local front door on a named pipe
(user-SID-only DACL, remote clients rejected, first instance only), so it installs on Windows;
gt-lotr 0.2.0 and older stay off there. Its `plugin.json` starts the MCP server with `python3`,
which on Windows is the Microsoft Store stub, so the installer rewrites the INSTALLED copies of
that manifest (plugin cache and marketplace) to the interpreter it resolved, as an absolute path
with `PYTHONUTF8=1` (`gt_components.py localize-mcp`). Claude Code then starts the server
directly, so it stays a direct child of `claude`, which gt unlock's shim registration checks.
The post-install gate's `smoke-lotr` row starts exactly that configured command and asks it for
its tools. macOS and Linux keep the manifest as shipped. *What does not run on Windows, and says so (0.20.1).* gt-lotr before 0.3.0 (a Unix-domain-socket gateway) is
off on Windows whatever is chosen; `gt-watch`'s hourly cron fetch (`install-cron`) is POSIX-only —
run `gt_watch.py fetch` by hand or from Task Scheduler; the doctor's `workers` row says it is not
supported on Windows (there is no POSIX process table); task priority windows need a time-zone database —
`python -m pip install tzdata`. The repository's `.gitattributes` forces LF on checkout (0.17.3) — except `*.cmd`,
which stays CRLF — so a Windows clone neither breaks the shell scripts nor fails the checksum
check. Not yet exercised: the hooks as Claude Code for Windows itself runs them (they have been
run directly, with the same payloads). WSL is Linux, and installs as Linux does.

**Rollback:** the repo (and a published copy) keeps the previous release, so
`bash install.sh <previous version> --vault <vault>` reinstalls it; your recorded module
choices and vault are kept. **If you turned on sandbox mode or gt unlock, turn them off
first** — a release from before them cannot undo their settings, so the vault could be left
unreachable:

```bash
python3 ~/.claude/golden-thread/hooks/gt_settings.py set sandbox_mode off
python3 ~/.claude/golden-thread/hooks/gt_unlock.py policy disable
python3 ~/.claude/golden-thread/hooks/gt_unlock.py daemon stop
```

A rollback is judged against the release it installed, not the newest in the tree (0.20.1):
the post-install gate and the doctor compare against the installed release, the vault's
gt-shipped tools and git hooks go back to that release's copies (they are not reported as
local edits), and an installer verb the older release lacks is skipped rather than printing
a usage error. Rolling back to 0.14.0 removes the module plugins it does not
know (`gt-watch`, `gt-report-card`, `gt-farm`, `gt-flow`), so the older gt — which carries
watch, the report card and farm itself — is not left with two copies of the same skill.
Each other plugin installs the release that gt shipped with (gt-wiki 0.1.2 for 0.12.x, 0.1.3
for 0.13.0, 0.2.0 for 0.14.0), and a demo choice of off — `install_demo=no` or
`--without demo` — leaves the demo out of a gt that still carries it inside.

An upgrade prints `Moved: /gt:<skill> → /<plugin>:<skill>` for every command that left gt
for a module since the gt you had.

---

## Uninstall

One command removes everything gt put on this machine, shows you first, and checks afterwards
that it is gone (0.20.1):

```bash
bash install.sh --uninstall --check     # list what would be removed; nothing changes
bash install.sh --uninstall             # remove it (asks once; --yes when not at a terminal)
```

On Windows: `install.cmd /uninstall /check`, then `install.cmd /uninstall`. If the plugin source
is gone, run the installed copy: `python3 ~/.claude/golden-thread/hooks/gt_uninstall.py --check`
(cmd.exe: `py -3 "%USERPROFILE%\.claude\golden-thread\hooks\gt_uninstall.py" --check`).

It removes:

- gt's scheduled jobs — launchd, Task Scheduler, systemd timers or cron lines — under both the
  current `io.goldenthread.gt-*` and the old vendor-prefixed labels;
- the sandbox entries gt added to `settings.json` (through `gt_sandbox.py remove`);
- the unlock daemon and its home. **Your enrolled factors, recovery codes and sealed secrets are
  destroyed**; a git credential helper pointing at `gt_unlock.py` goes too;
- gt's hook entries and the `*@golden-thread-plugin` keys in `~/.claude/settings.json` (your own
  hooks stay), and the plugin registrations, cache and marketplace;
- `~/.claude/golden-thread`, keeping its `backups/` unless `--purge-backups`;
- gt's `python3` shims (a `~/bin/python3` that is not gt's is kept);
- `~/.gt-inbox` and `~/.gt-scratch`, module crontab lines, and stale gt socket directories in
  `/tmp`;
- `~/.claude/vault-config.json` (keep it with `--keep-vault-config`).

It refuses while `~/.gt-inbox` holds vault writes that were never applied — drain them first
(`python3 ~/.claude/golden-thread/hooks/gt_broker.py drain --vault <vault>`) or pass
`--discard-queued`. Every configuration file it edits is copied first to
`~/.claude/gt-uninstall-backup-<stamp>/`, and the summary prints the commands to copy them back.
Exit 0 means the re-check found nothing of gt's left. Restart Claude Code afterwards.

**Your vault is never touched** — it is yours, not the plugin's. To detach its git hooks:
`git -C <vault> config --unset core.hooksPath`. The LOTR gateway home (`~/.config/gt-lotr`) is
kept unless `--purge-lotr`. To drop only an optional part, use `bash install.sh --without
<module>` instead.
