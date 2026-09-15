# Changelog

Releases of the `gt` and `gt-wiki` plugins. `install.sh` always installs the newest
version directory present, so upgrading is `bash install.sh` and a restart of Claude Code.

Announcements for recent releases are posted under
[Discussions → Announcements](../../discussions/categories/announcements).

Entries from 0.9.13 onward are written from the change itself. Earlier entries are the
release's own summary line, kept short rather than reconstructed after the fact.

---

## gt 0.15.0 · gt-demo 0.15.0 · gt-wiki 0.2.1 · gt-watch · gt-report-card · gt-farm · gt-flow 0.15.0 — 2026-09-14

**Three parts of gt become modules, and one new module draws the vault's history.** gt now
has sixteen skills; everything optional ships as one of six modules, each removable with
`bash install.sh --without <name>`.

- **`watch`** (plugin `gt-watch`, on): the upstream watch. The command is now
  `/gt-watch:gt-watch` (was `/gt:gt-watch`); its SessionStart hook, `gt_watch.py` and the
  `watch` setting moved with it. `--without watch` also removes the crontab line it
  installed — only the line tagged for it, after a backup.
- **`report-card`** (plugin `gt-report-card`, on): the session report card and the
  close-out question. No command; its PreCompact, SessionEnd and SessionStart hooks and the
  `report_card` and `closeout_check` settings moved with it.
- **`farm`** (plugin `gt-farm`): work packets for an external AI service, now
  `/gt-farm:gt-farm` (was `/gt:gt-farm`). **Off for a fresh install; kept on for upgraders** —
  a machine migration records `farm` as on for a machine upgrading from a gt that shipped
  `/gt:gt-farm`, unless a choice is already recorded. It names no particular AI service and
  its packet directory is configurable (`farm_packet_dir`).
- **`flow`** (plugin `gt-flow`, on, new): `/gt-flow:gt-flow` renders `events.jsonl` as one
  self-contained, offline HTML file — a lane per project, an arrow each time an item climbed
  a level. `--redact` replaces every name with a per-render salted hash and writes nothing if
  its self-check fails; `task.*` events are hidden until clicked or `--tasks`; `--project`,
  `--since`; an `--out` inside the vault is refused.

**Upgrading from 0.14.0 converges.** One install over 0.14.0 ends where a fresh 0.15.0
install made with the same module choices would: no SessionStart, PreCompact or SessionEnd
entry is left pointing at a gt path, your `watch`, `report_card` and `closeout_check` values
are kept, and rolling back to 0.14.0 removes the module plugins it does not know, so it is
not left with two copies of a skill.

**Module format extensions.**
- A module may declare **reporter hooks** and **hook-directory scripts** in `module.json`;
  they are tagged with the module's name, wired only while it is on, and removed when it is
  switched off. A Core-rule enforcement hook can still never belong to a module.
- **Module settings** may carry a long `detail`, printed by `gt_settings.py explain`. A value
  set for a module that is later switched off is kept and shown as not in effect.
- A module's skills may name another module's script; the demo resolves it at run time with
  `gt_demo.sh module-scripts NAME` and skips the act when that module is absent. The watch
  act moved from the core tour into the watch module: nine core acts, plus the wiki, watch
  and flow acts with the default modules.
- `gt_components.py module-states --detail` explains each module's state.
- `dev/check_retired.py` treats a hook script now provided by a module beside the release as
  **moved, not retired**, so install never deletes a live module file, and reports a
  `retired.json` entry naming such a file.

**Operations record movement events as they happen.** `gt_events.py` shipped in 0.13.0 with
nothing calling it; now:
- `gt_log.py add "<line>" --event <kind> --item <path> …` spools a log line and its event in
  one command (`/gt:gt-promote`, `/gt:gt-refresh`); `/gt:gt-review`, `/gt:gt-work` and
  `/gt:gt-ingest` call `gt_events.py emit`.
- `gt_adr.py allocate` emits `adr`; `vault_init.py` emits `create`, `rename`, `merge` and
  `archive` when the operation does something (never under `--dry-run`). `archive` comes only
  from `archive-project` — `gt_closeout.py answer <slug> yes` records a decision, not a move.
- `gt_tasks.py` emits `task.open` / `task.done` for changed checkboxes once the stream has
  been seeded.
- An event can never fail the operation it describes.
- **`gt_events.py backfill`** rebuilds a vault's history from git and `log.md` as
  `actor: backfill` events. `--dry-run` previews with samples; a second run adds nothing.

**Core rule `core_parallel_when_beneficial` reworded:** independent units run concurrently
*within one machine-wide budget shared by every session*, not a budget per session.

**Fixed**
- **Every vault-writing command in a shipped skill names its vault**, so following a skill
  never runs into the vault-write guard's denial. A test feeds every command in every shipped
  `SKILL.md` to the guard itself.
- The release gate's skill-reference step crashed on `golden-thread/0.1.0-archive.zip` and,
  with the exit code unread, printed ok. It now fails when it cannot run.
- The README and MANUAL examples for `gt_log.py` and `gt_adr.py` now pass `--vault`.
- **`--with <module>` puts back the crontab lines `--without` removed.** Turning `watch` off
  and on again reinstalled the module but never its hourly fetch, so the watch report stayed
  on and never changed, silently. The removed lines are now kept (mode 600, under
  `~/.claude/golden-thread/module-cron/`) and restored verbatim once the module is on, after
  a crontab backup. The watch hook also says so when watches exist but no gt-watch cron entry
  does.
- **Rolling back keeps gt-wiki and gt-demo.** `./install.sh 0.14.0` from a 0.15.0 tree skipped
  them, because only each plugin's newest release (built for 0.15.0) was considered. Each
  plugin now installs its newest release whose `requires_gt` admits the gt being installed,
  and the install banner names the release it chose and why.
- **An install no longer overwrites your vault's git hooks, tool edits or `core.hooksPath`.**
  A fresh-context validation reproduced each loss against vaults built by older releases.
  `.githooks/` was copied over on every install, so an uncommitted hook edit was gone with no
  backup; now a hook gt shipped is updated, an uncommitted edit is kept, and a committed one is
  backed up first. A vault tool was replaced when its file was *older on disk* than the
  release's copy — an owner's edit older than a freshly unpacked release was "stale", a
  pristine copy seeded later was "AHEAD". Tools are now compared by content against every
  text gt has shipped (`templates/shipped-hashes.json`, from `dev/shipped_hashes.py`): gt's
  own text is updated, anything else is kept as a local modification. `core.hooksPath` is set
  only when unset. The logic moved from `install.sh` into `scripts/vault_refresh.py`.
- **The install backs up the vault files it may change before its first write.** The only
  backup was gt_upgrade's, taken after the core-rule, CLAUDE.md, hook and tool refreshes, so
  it never held what they replaced. `install-vault-files-<stamp>.tar.gz` is kept only when
  the install changed something in it.
- **What an install puts back or converts is named.** A re-created Core rule, the re-inserted
  CLAUDE.md enforcement section and each converted `log.md` / `decisions.md` (with notes such
  as trailing blank lines dropped) are listed instead of "migrated 1 project(s)". A rule or
  the section listed in `.gt-removed` beside `core-rules/` is never re-created, by
  `install-core-rules` or by the upgrade's core-rules step.
- **A vault upgrade never removes lines from your PROTOCOL.md or CONVENTIONS.md unasked.**
  Releases before 0.14.0 recorded the owner's own document as its merge base; with base ==
  ours a "clean" 3-way merge *is* the template, so an install on a clean vault would have
  replaced the document — dropping owner sections and wikilinks while the dry run said
  "would merge cleanly (-16 lines)" (and "+0 lines" for six overwritten ones). A merge that
  would remove any line of the owner's document is now held: never a pending step, never
  applied by `install.sh`, reported as `needs a person: merge held` with the lines it would
  remove. `run --record-base <doc>` keeps the document (backing up the old base under
  `~/.claude/golden-thread/backups/`); `run --accept-merge <doc>` takes the merge. Dry runs
  report lines added and removed, not a net count.
- **The worker check no longer calls another live session's shells orphans.** It judged a
  worker by CPU and declaration alone, so a second session's SessionStart reported two wait
  loops of a live session as `ORPHAN` and offered `reap`, which would have killed them. It now
  walks each shell's parent chain: a shell with a live `claude` above it belongs to that
  session, is listed as information (session id, claude pid, uptime, and `WAITING on: <what
  it polls>` for a poll loop or a shell whose only children are `sleep`), and is never
  reaped — `reap` refuses it even if classification were wrong. `ORPHAN` now means no
  `claude` remains above it. This session's own undeclared workers are still raised.
- **Migrating or merging `log.md` and `decisions.md` no longer rewrites bytes that are not
  UTF-8.** `gt_log.py`, `gt_adr.py` and `gt_spool.py` read them with `errors="replace"`, so a
  latin-1 `é` in an old log became U+FFFD in both the frozen baseline and the generated file —
  and the round-trip gate compared two already-replaced copies, so it passed. Every read that
  feeds a write now uses `surrogateescape`, which carries any undecodable byte through
  unchanged; the baseline is written from the file's raw bytes and checked against them.
- **Rolling back to a gt from before modules ends where that release's own installer did.**
  `./install.sh 0.12.8` or `0.13.0` from a 0.15.0 tree removed gt-wiki: the releases those
  gts shipped with (0.1.2, 0.1.3) carry no `module.json`, so nothing admitted them. The
  installer now carries which plugin releases each gt release shipped with (`SHIPPED_WITH`,
  checked against git history by a test) and installs exactly those on a rollback. The same
  rollback also ignored `install_demo=no`: those gts carry the demo inside gt, and 0.15.0 had
  dropped the stripping. A `--without demo` this run, a recorded `demo: off`, or
  `install_demo=no` now leaves `/gt:gt-demo` out of that gt, as 0.13.0 did.
- **An upgrade says where moved commands went.** `/gt:gt-watch`, `/gt:gt-farm` and (from
  before 0.14.0) `/gt:gt-demo` simply stopped resolving after the upgrade. The installer
  now prints `Moved: /gt:gt-watch → /gt-watch:gt-watch` once for each skill the previous gt
  had and the new gt left to a module, naming the module to turn on when it is off.
- **An upgrade wires hooks in the same order as a fresh install.** Each writer appended its
  own entry, so an upgrade from 0.13.0/0.14.0 left `guard_protected_paths.sh` last in
  PreToolUse where a fresh install put it first. install.sh and `vault_init.py
  install-core-rules` now both apply one canonical order per event: blocks that are not
  purely gt's (your own hooks, or a block mixing yours with ours) first, in the order you
  had them; then gt's in `HOOK_REGISTRATIONS` order, module hooks after; then any other entry
  pointing into the gt hooks dir. Entries only move — nothing is added or removed by it.
- **File modes are set, not inherited.** `cp` gives a new file the source's mode and keeps an
  existing file's, so a fresh install from an untracked 0700 checkout left hooks at 0711 and
  plugin files at 0700 while an upgrade kept 0755/0644. Everything install.sh copies into the
  plugin cache, the marketplace and the hooks dir is now 0755 for directories, `*.sh` and
  `*.py`, and 0644 for every other file. A file of your own in the hooks dir is not touched.
- **`install-choices.json` is written one way.** `record-choice` wrote it 0644 with sorted
  keys, the machine migration 0600 with unsorted ones. Both now write 0600 — the mode of
  every other state file the installer keeps under `~/.claude` — with sorted keys.
- **`install.sh --vault` keeps an owner's `core.hooksPath` too.** The refresh step already
  did, but the `--vault` connect path (`vault_init.py` seeding) ran `git config core.hooksPath
  .githooks` first, so a custom hooks directory was still replaced. Both paths now set it only
  when it is unset; the chaining hint prints once.
- **Git hooks follow the vault-tool rule.** A `.githooks/` file is replaced only when its bytes
  match a hook some release shipped (`shipped-hashes.json`); an owner's edit, committed or not,
  is kept and reported as `MODIFIED LOCALLY`. Committed edits used to be backed up and replaced.
- **Hand-written lines in a generated `log.md` are kept.** Any render (`gt_log.py add`,
  including the receipt an install writes) rebuilt the file from the spool and deleted lines
  typed into it, with no word. They are now captured into `spool/log/hand-edits-<time>.md` and
  announced; a line starting with a date sorts by that date, an undated line after the
  baseline. `decisions.md` has no slot to guess, so `gt_adr.py merge` refuses and names the
  lines instead.
- **A merge base copied from the owner's own file is never merged against.** A pre-0.14 base
  identical to the document let a merge that only ADDED lines through, re-inserting a section
  the owner had deleted. Such a merge is now always held for a person, whatever it adds or
  removes. Merges also keep the document's bytes and line endings (CRLF stayed CRLF only by
  luck of the text-mode pipeline; it is now bytes end to end).
- **Upgrade and fresh install write the same JSON key order.** The hook EVENT keys in
  `settings.json`, `enabledPlugins` and the plugins in `installed_plugins.json` kept an older
  release's insertion order. Your own events and other marketplaces' plugins stay first, in
  the order you had them; gt's follow in one fixed order.
- **No bytecode is left behind.** Python run by the installer wrote `__pycache__` into the
  plugin cache, the marketplace copy and, on the `--vault` path, the vault's own tools
  directory, differently each run. The installer now runs with `PYTHONDONTWRITEBYTECODE=1` and
  strips `__pycache__` from what it installs; `marketplace.json` gets its mode set like every
  other installed file.
- **The Moved notice reflects where the install ends.** It was computed before the machine
  migrations and said `module farm is off` for an upgrader whose farm a migration then turned
  on. The off/on tail is now added from the final module state.
- **A rollback removes what a newer release wired.** `./install.sh 0.12.8` from a newer tree
  left the newer `guard_protected_paths` hook wired and its files in the hooks dir, which
  0.12.8 reported as drift every session. Hooks-dir files byte-identical to a copy a newer
  release (or module) in the tree ships, and entries for scripts those releases register, are
  removed when the older gt does not ship them; files of your own are left alone.

## gt 0.14.0 · gt-demo 0.14.0 · gt-wiki 0.2.0 — 2026-09-14

**An upgrade now finishes the job without you.** 0.13.0 removed what older releases left
behind; this release adds the other two pieces an upgrade from any older release needs.

- **Machine migrations** (`gt_machine_migrate.py`, run by `install.sh`): one-time changes
  under `~/.claude/` that a skipped release would have made. Each is applied once, judged
  from the machine's actual state rather than a record, stops the install at the first
  failure (`INSTALL INCOMPLETE`, exit 7), and backs up what it changes. The first one records
  your demo choice in `install-choices.json`, ready for optional modules.
- **Vault upgrades are applied by the install** when the vault had no uncommitted changes
  before the install touched it (after a backup; results left uncommitted for review). A
  vault the same install created gets an initial commit. Your own uncommitted work is never
  touched — the install prints the command instead.
- **Your edits are never merged away.** A PROTOCOL.md or CONVENTIONS.md with no base gt
  recorded is never merged unattended: it is reported as *needs a person* until you review
  it and run `gt_upgrade.py run --record-base <doc>`. Connecting an existing vault no longer
  records a base for an edited document. A merge conflict is reported, not re-run on every
  install.
- **`install.sh` installs every plugin the release ships**, discovered by the same rule as the
  release gate, instead of naming gt and gt-wiki. `--list-plugins` shows what it found.

**Optional parts are modules you can decline.**
- A module is a separate plugin in the same marketplace, declared by `module.json` and
  versioned with gt. **`wiki`** (gt-wiki 0.2.0) and **`demo`** (gt-demo 0.14.0) ship as modules,
  both on by default.
- `bash install.sh --without demo` removes a module completely — plugin cache, marketplace
  entry, enabled flag, hooks and hook scripts — and remembers the choice; `--with demo` brings
  it back; `--list-modules` shows each module's state and why.
- **The demo moved out of gt.** It is now `/gt-demo:gt-demo` (was `/gt:gt-demo`). `remove`
  deletes the demo vault and points to `install.sh --without demo`. If you had
  `install_demo: no`, the upgrade keeps the demo off.
- Modules may declare hooks, tagged with the module name and wired only while the module is
  on; a module can never claim a Core-rule enforcement hook. Module settings appear in
  `gt_settings.py show` under the module's name; gt's `install_demo` setting is gone.
- gt skills that point at the wiki say how to install it when it is off, and the version
  check reports a declined module as "not installed by choice".
- `/gt:gt-doctor` gains a modules check; the release gate validates every `module.json` and
  that its `requires_gt` admits the gt being released.

**The demo shows what you have installed.** `/gt-demo:gt-demo` assembles its tour when it
runs: gt's core acts plus one act from each installed module that ships one
(`module.json` → `demo`), placed before the closing act so its work appears in the receipt.
The wiki act now lives in the wiki module — install it and the act appears; remove it and
it is gone. Third-party extensions will not supply act text of their own.

**Fixed, found by this release's regression**
- **New vaults and new projects start current.** Since 0.11.0 `vault_init fresh` created
  `log.md`, and `create-project` created `decisions.md`, in the pre-spool layout, so every
  new vault and project was born one upgrade behind. Now that installs apply upgrades, a
  fresh install would have ended with changes to review on a vault created seconds earlier.
- **`merge-project` no longer loses decisions.** It wrote the source project's ADRs into the
  destination's generated `decisions.md`, and the next re-render deleted them. They now go
  into the destination's spool, the ADR count is verified before anything is removed, and a
  failure part-way leaves both projects as they were.
- **`rename-project` moves the decisions spool** with the project, so the renamed project
  does not read as unmigrated and its next ADR does not restart at 1.
- **`--dry-run` means nothing changes** for `merge-project` (it moved notes and deleted files)
  and `rename-project` (it renamed the folder).

## gt 0.13.0 · gt-wiki 0.1.3 — 2026-09-14

**Upgrading from any older release now lands you on the same install a fresh one would.**
`install.sh` installs only the newest release — it never steps through the ones in
between — but until now it only ever *added*: a hook entry or hook-directory file an older
release installed stayed forever once a newer one stopped shipping it. This release makes
convergence a requirement and the first mechanism behind it.

- **`retired.json` ships in every release** and records everything earlier releases
  installed into `~/.claude/golden-thread/hooks/` or wired into `settings.json` that this
  one no longer does. `install.sh` removes exactly those, after a backup, and prints each
  removal. Anything it does not recognise is reported and left in place — it never guesses.
- **A new release-gate step, `check_retired.py`,** fails any release that stops installing
  or registering something without recording it, so an unrecorded removal cannot ship.
- **Old gt-wiki cache versions are pruned**, as gt's already were.
- **Pending vault upgrades are shown at the end of every install**, with the command to
  apply them. They are reported, not yet applied automatically — see below.
- A test installs 0.12.8 with a vault, upgrades it to 0.13.0, and asserts the hooks,
  hook-directory files and plugin caches match a fresh 0.13.0 install, with the user's own
  hooks and files intact.

**The vault-write guard reads commands, not strings.** It blocked a `git commit` whose
*message* mentioned `gt_log.py add`, and read-only commands that merely named a tool —
`grep`, `sed -n`, `diff`, `cat`, and `--help`. It now objects only when a vault tool is the
program actually being run, including through `python3`, `env`, `sudo`, `bash -c` and
`$(…)`. Every existing denial still holds.

**New and extended tools**
- **`gt_events.py`** (vault tool): structured movement events — `emit`, `merge`,
  `validate`, `list` — on the same per-session spool pattern as the log. Schema v1 is
  validated before anything is written. Other tools start emitting in a later release.
- **`gt_tasks.py --json`** prints the ranked rollup without writing `TASKS.md`. Deadline
  windows can **span days** (`Fri 16:00 → Sun 16:44 America/Chicago`), including spans that
  wrap the week.
- **`gt_lint.py --json`**, and **`gt_lint.py --runbooks`**, which reports lines duplicated
  across projects' runbooks. `/gt:gt-runbook-lint` said a script did the detection; now one
  does.

**The public release no longer assumes one person's machine.**
- `gt_doctor`'s gt-src check reads `gt_src` from `vault-config.json` (or `$GT_SRC`) and
  reports *not configured* instead of checking a hard-coded folder.
- The weekly lint report goes to `lint_report_dir`, else the folder earlier releases used
  if it already exists, else `<vault>/.gt/lint`; it says so when gt-wiki is not installed.
- `/gt:gt-farm` no longer depends on a project only one vault had.
- The demo tour resolves its vault and scripts instead of hard-coding them.
- The announcement check reads Discussion bodies as well as titles, and `0.12.1` no longer
  matches `0.12.10`.

**Release machinery**
- Every build tool discovers plugins through one rule (`dev/plugins.py`) instead of naming
  gt and gt-wiki, so a future module is gated, packaged and published automatically.
- **gt-wiki 0.1.3 ships a `MANIFEST.json`**, and the gate fails any plugin without one.

**Not yet:** applying vault upgrades during install. `gt_upgrade run` refuses a vault with
uncommitted changes, and a vault created moments earlier by the same install is always
uncommitted — so turning it on needs that settled first.

## gt 0.12.9 — 2026-09-13

**The guards were approving tool calls they meant to ignore.** Upgrade promptly.

All three PreToolUse guards — the session-claim guard, the vault-target guard and the
commit-test guard — answered `permissionDecision: "allow"` for every tool call they did not
block, and they run on every tool call. Claude Code's hooks reference defines `allow` as
"skip the interactive permission prompt"; the neutral answer is to exit 0 and print
nothing. So a guard written to stay out of the way was instead waving calls past the
permission prompt you would otherwise have seen. Deny and ask rules in your settings still
applied, which is why nothing looked wrong.

- **Every guard now prints nothing when it has no objection**, including when it fails
  open on an error. Denials are unchanged. A regression test per guard asserts empty
  output for a harmless call, and those tests fail against 0.12.8.
- The commit guard's warn-mode note is still delivered, as context with no decision.

**Protected paths now need a person to see the write.** Until this release nothing but
skill prose stopped a session's Write or Edit tool from changing the files every session
depends on. A new guard, `guard_protected_paths`, and a new setting, `protected_paths`
(`ask` by default, or `off`):

- A Write or Edit to the vault's `core-rules/` or `global-memory/`, to
  `~/.claude/golden-thread/`, or to `~/.claude/settings.json` always shows the permission
  prompt, in any permission mode. Approving it is fine when the change is intended.
- Editing or overwriting an **existing** file in `Sources/` is refused — supersede it with
  a new file. Creating a new source is unaffected.
- Paths are resolved first, so `..` and symlinks do not get around it. It does **not** see
  shell commands that write the same files.

**The report card was never shown.** It runs at `/compact`, automatic compaction and
session end, and output at those events reaches neither you nor the assistant. It now
saves the card, and a new session-start step shows it at the start of your next session —
including the close-out question, which the assistant is told to put to you. Its
docstring also stopped claiming it never writes to the vault: its close-out step appends
`closeout-signals.jsonl`, and `gt_closeout.py ask` / `answer` are now covered by the
vault-target guard.

**The release gate scrubs everything a push publishes.** It used to scan a list of plugin
directories, so files at the repository root were published unscanned — and one that
named internal systems did reach the public repository before it was removed.
`scrub_check.py --repo` scans tracked files plus untracked files that are not ignored, and
the gate now uses it.

Hooks wired by `install.sh` go from 7 to 9: the session-start report-card step and the
protected-path guard. Re-run `bash install.sh` and restart Claude Code.

## gt 0.12.8 — 2026-09-12

**`gt_paths.py` shipped twice, and one of the copies was three releases stale.** If you
have ever seen a drift report you could not clear, this is why.

Two requests arrived from another machine. One asked to promote its installed
`gt_paths.py` into the plugin, on the reading that the installed copy was the richer one
and the source lacked the `gated_by` / `budget_from` keys. Half right, and the diagnosis
inverted:

- `scripts/gt_paths.py` has had those keys since **0.12.4**. The installed copy was
  byte-identical to it.
- `hooks/gt_paths.py` — a **second copy in the same release** — was the 0.12.2-era file
  without them, and had been stale in every release from 0.9.13 through 0.12.7.

Both install to the same destination. `install.sh` copies `hooks/*` first and then
overwrites with `scripts/gt_paths.py`, so the correct file won — **by ordering, not by
design** — while `MANIFEST.json` kept a hash for each path. The drift check therefore had
to disagree with one of them on every machine, forever, and no user action could clear it.

It got worse in 0.12.7. Resolving direction by identity made that row read `stale`, which
is the **auto-appliable** state: with `component_updates=auto` the check would have copied
the 0.12.2 file **over** the correct one, silently disabling the keys the parallel Core
rule reads to know which setting governs it. A phantom that became destructive.

- **`hooks/gt_paths.py` is deleted.** One file, one home: `scripts/`, installed to the
  hooks directory by `install.sh`, exactly as the other hook-dir scripts are.
- **`gt_components.duplicate_destinations()`** reports any destination claimed by more
  than one shipped file, using the same mapper the drift check uses so the two cannot
  disagree about where a file goes. Wired into the release gate: pointed at 0.12.7 it
  names the defect, at 0.12.8 it reads clean.
- Two end-to-end tests make the original report impossible to reproduce: the installed
  copy carries both keys, and a freshly installed tree reports no drift on `gt_paths.py`.

### `gt_upgrade.py status` no longer reports success as failure

Pending migration steps exited 1, so every interactive `/gt:gt-upgrade` rendered as a tool
error — training the reader to ignore a check whose whole job is to be read. `status`
succeeded: it looked, and found pending steps. It now exits 0. Nothing branched on the
code (`gt_doctor` computes pending itself, the skill reads the printed output), verified
before changing it, and the printed output is unchanged and asserted so.

**The test written for that request then found a second bug**: `status --vault
<missing-path>` exited **0** and reported on the nonexistent path as an un-upgraded vault
— "never stamped, every migration is offered" is an alarming thing to print about a typo.
`cmd_run` had always refused a missing vault; `status` never did. It now exits 2, which
also keeps the command able to fail at all now that pending is 0.

Two assertions pinning the old behaviours were **inverted rather than deleted**, each with
its original reasoning recorded, so neither can return from the argument that produced it.

## gt 0.12.7 — 2026-09-12

**The installer checks that what it is about to install matches its manifest, and the
drift check stops claiming a direction it cannot prove.** Two halves of the same failure,
both reported the same day.

### install.sh verifies the source against MANIFEST.json

Requested 2026-09-11 (`install-refuse-stale-manifest`), and the refuse-or-warn question
it existed to settle was decided by the repo owner on 2026-09-12:

- **Refuse** when a file that **executes** (`hooks/`, `scripts/`) disagrees with the
  manifest — exit 6, the file named, the regenerate command named, and **nothing
  installed**.
- **Warn** when only copied files (`templates/`, `skills/`) disagree; the install
  completes.
- **Exempt** a developer's uncommitted or untracked edit, per git, downgrading it to a
  warning. Editing a script and installing to test it is the normal loop here, and a gate
  that fires on the normal loop gets overridden by reflex and then ignored.
- `--force-manifest-mismatch` installs anyway, and says so.

It matters on a machine that is not the one running the release gate: since 0.12.6 the
installer no longer regenerates the manifest, so a second machine installing from `gt-src`
could install files the manifest does not describe, be told nothing, and then report drift
at every session start for a mismatch the installer could have caught once. This earned
its own release the same day it was wanted: 0.12.4 shipped with a stale `MANIFEST.json`,
and only the gate saw it.

### `ahead` no longer means "we guessed from an mtime"

Another machine reported `hooks/gt_paths.py` as `ahead` of the plugin source at every
session start, with the advice *"installed is NEWER — the plugin source needs updating from
it"*. The installed file turned out to be **byte-identical** to what the plugin ships, and
the direction had never been established: `install.sh` copies with plain `cp`, so every
installed file carries the install-time mtime and is **always** newer than its source. Any
content mismatch therefore read as `ahead`, which is never auto-applied — a permanent
warning, pointing the wrong way, with no way out.

- Direction is now decided by **identity first**: a copy whose hash matches the same file
  in another release on disk, or in the plugin cache, is a leftover from that install and
  is `stale` — reportable and applicable.
- When nothing local can establish direction, the state is **`differs`**, which says so
  plainly and prints the three resolutions with real paths: diff them, capture the
  installed copy into the plugin, or delete it and re-install. It is never auto-applied,
  exactly as `ahead` never was.
- `ahead` is still recognised, so nothing that consumed it breaks.

**Also:** the test fixture now regenerates its own manifest, so a working tree mid-edit
does not fail every install test for a reason unrelated to the test. And a test that pinned
"a stale manifest still installs" was **inverted rather than deleted**, with the original
intent recorded in it, so the old assertion cannot come back from the reasoning that
produced it.

## gt 0.12.6 — 2026-09-12

**The install-time machine measurement actually happens now.** Take this if you have
0.12.5.

0.12.5 introduced `parallel_profile`, measured at install so `parallel_max: auto` means
the machine in front of you. It ran as step 6b of `install.sh` — **before** `setup_vault`
creates `vault-config.json`. On a machine that already had a config it worked, which is
every machine the author tested on. On a **fresh** install there was nothing to write into
and the step skipped silently, so the profile appeared only where one already existed and
`auto` fell back to reading the machine live on every call.

Found by installing into a throwaway `HOME` and looking for the value, rather than
trusting the installer's output — which said nothing either way. Measured before and after
on an otherwise empty home: `parallel_profile` ABSENT with the 0.12.5 ordering, and
`{cores: 16, cpu_max: 16, io_max: 32}` with this one (`self-verified`).

- The measurement moved to **after** the vault is configured, and the installer now prints
  what it recorded.
- Two tests assert the **value**, not the delivery: a fresh install records a profile with
  every field, and re-running the installer does **not** overwrite a `parallel_max` or
  `parallel_work` the user chose. Every existing check passed through this bug — the files
  arrived, the hooks wired, the gate was green — because they all asked whether things were
  *installed*, and none asked whether the number was *there*.

Nothing else changed; the 0.12.5 payload is otherwise identical.

## gt 0.12.5 — 2026-09-12

**A tenth Core rule: code is not committed until its tests have been seen to pass. Plus
`auto` now measures your machine instead of guessing, and `publish` is one command.**

- **`core_test_before_commit`** (Core/**Validated**) — enforced by a third `PreToolUse`
  guard, on `git commit`. Evidence is a **receipt**: `tests/run.sh` and
  `dev/release-check.sh` write one when they pass, any project writes one with
  `gt_test_receipt.py record --ok`, and a receipt covers a file only if it is **newer**
  than that file — so editing something after the run invalidates it automatically, with
  nothing to remember. Setting `test_gate`: `off`, `warn`, `auto` (**default** — block
  where the repo has a discoverable test command, warn where it does not), `block`.
  **Per-repo opt-out:** `touch .gt-no-test-gate`. Per-commit: `GT_TEST_GATE=off`. Never
  blocked: docs-only commits, and anything the guard cannot parse — it fails open.

  It exists because 0.12.4 was committed and pushed with a **stale `MANIFEST.json`**. The
  gate that catches exactly that had been run, then several more edits followed. The
  discipline was fine; it had no mechanism.

- **`parallel_max: auto` is now measured, not assumed.** `install.sh` records a
  `parallel_profile` for the machine — cores, physical cores, memory, and the resulting
  `cpu_max` / `io_max` — and re-measures at every upgrade, which is exactly when the
  hardware may have changed. It never touches `parallel_work` or `parallel_max`: the
  profile records the hardware, those record what you will allow. On a 16-core/128 GB
  machine `io_max` comes out at **32**, where 0.12.4 hardcoded 20 — a number that was
  right for the laptop it was chosen on and arbitrary everywhere else.

- **A ceiling you set is checked against the machine.** `set parallel_max 40` on a
  16-core box is refused, with the numbers; a value above core count is accepted with a
  note that it helps I/O-bound work and does nothing for CPU-bound work. `--force`
  covers the case the profile cannot see, like a fan-out bounded by the network.

- **`dev/publish.sh` — publishing is one command.** Gate, committed, pushed, gt-src,
  announced, logged, in order, stopping at the first failure. The requirements are data
  at the top of the script and `--list` prints them; there is no `--skip`, because a step
  you may skip is not a requirement. 0.12.4 shipped with gt-src left a release behind,
  which is invisible from this repo: everything here was correct and only the copy the
  other machine reads was stale.

- **The hook-wiring list had a second copy, and now does not.** `vault_init.py` kept its
  own hand-maintained list of which hooks to register — the same duplication that shipped
  `guard_session_claims.sh` unwired in 0.9.5. It now reads
  `gt_components.HOOK_REGISTRATIONS`, the one declaration `install.sh` already used.

**Two bugs caught by the new rule's own tests**, both worth naming because each would
have shipped silently: the commit guard's heredoc regex referenced a capture group that
did not exist, so the guard threw on every call and the wrapper failed open — an inert
guard that reported nothing; and an import failure in the guard now **announces** that it
is degraded rather than quietly allowing, the same principle as
`inject_core_rules.sh`'s ENFORCEMENT DEGRADED banner.

## gt 0.12.4 — 2026-09-12

**A ninth Core rule: parallel execution is the default for divisible work, in every
project — and a setting that caps it.**

The serial default was never a decision. A loop, a sweep over 43 projects, a backtest and
a test suite all run on one core unless someone says otherwise, and nothing reports the
waste: the work completes, correctly, slowly, and its output is identical to the fast
version. Measured here on the `gt` suite itself — **509s serial against 108s parallel,
4.7x, CPU from roughly one core to 404%**, the same 672 tests passing both ways
(`self-verified`). That speedup had been available on every previous run.

- **`core_parallel_when_beneficial`** (Core/Reminder, designated by the repo owner) —
  work that splits into independent units runs concurrently, up to the configured
  budget; serial execution must be justified rather than assumed. It governs both halves
  of the job: what gets **run** (builds, suites, sweeps, migrations, host fan-out, in any
  language on any host) and what gets **written** — a tool authored for divisible work
  gets a `--jobs` flag defaulting to the budget, because a script that can only run
  serially makes the mistake permanent for everyone who runs it later. Repetition is the
  strongest trigger: the cost is paid on every iteration, and each run still looks fine.
  It also names where parallelism is *forbidden* — shared-file writes, order-dependent
  steps, rate-limited remotes, production changes, and any job whose partial failure
  leaves a state nobody can reason about.
- **Two settings, `parallel_work` and `parallel_max`.** `parallel_work=off` runs
  everything serially *and stops the rule being injected* — a rule the user has switched
  off must stop being asserted, or the registry is decoration. `parallel_max` is `auto`
  (as many as the machine allows) or a worker ceiling; `auto` is deliberately not a
  number, since a stored count is wrong on the next machine and this config syncs
  between them. `gt_settings.parallel_jobs()` is the single place the two turn into a
  worker count, so no caller invents its own policy.
- **The mechanism is generic, not hardcoded.** A rule file may now declare `gated_by:`
  (the setting that can switch it off) and `budget_from:` (the setting whose value is
  appended to the injected line). `inject_core_rules.sh` reads both from the rule, so the
  hook never learns a rule's name — the same reason rule *text* has never lived in a
  script. The injected line carries the current ceiling, because a budget the model
  cannot see is a budget it cannot honour.
- `tests/prun.py` now takes its default worker count from those settings instead of
  keeping a private policy, with `-j` still winning, then `GT_TEST_JOBS`, then the
  settings, then the old I/O-bound fallback for a clone with no `~/.claude` at all.

**What you will notice after upgrading.** Runs that used to occupy one core now occupy
all of them: many processes at once, CPU above 100%, audible fans. Nothing is runaway —
the processes end when the run does. If you would rather it stayed modest,
`gt_settings.py set parallel_max 4` caps it and `set parallel_work off` turns it off
entirely, rule included.

**Two stale counts fixed on the way past.** The docs had said "seven Core rules" since
0.9.10 while eight shipped — `core_explicit_vault_target` was added in 0.12.0 and no
count moved — and the repo-root README's own table listed six of them. Both now state
nine, four validated, counted from the rule files.

## gt 0.12.3 — 2026-09-12

**A name for the installer fix, and a gate so the next one cannot go unnamed.**

The plugin payload is byte-identical to 0.12.2. This release exists because 0.12.2's
installer bug was fixed and committed *without* a version bump, so two published states
both called themselves 0.12.2: one whose `install.sh` wires the Core-rule enforcement
hooks and one whose `install.sh` does not. "Which version wires correctly?" had no
answer. If you have 0.12.2, take this.

- `dev/check_installer_version.py` fails the release gate when `install.sh` or
  `selftest.sh` has changed since the newest version directory was cut — committed or
  still dirty. `MANIFEST.json` hashes only what lives inside a version directory, so the
  file a user actually runs had nothing covering it at all. It decides from git history
  rather than a recorded hash, because regenerating a manifest after editing the
  installer would quietly bless the edit — the very move that caused this.

## gt 0.12.2 — 2026-09-12

**Reported from a second machine: `guard_vault_writes.sh` still unwired.** It was right,
and the bug had survived two fixes because each was confirmed from the one vantage point
where it already worked.

- 0.12.1 put the wiring call inside the block gated on the vault being a **git repo**. A
  vault that is not a repo installed with the enforcement hooks inert — and was then
  reported as no vault at all. Git decides whether the *attribution* hooks can be wired;
  it has nothing to do with an entry in `settings.json`. The wiring now depends only on a
  vault existing.
- **`dev/check_wiring_coverage.py` is a new release gate**, and it is the durable answer:
  it installs into a throwaway home — including the upgrade path, where a vault already
  exists — and then asks every shipped hook, hook-dir script, skill, script, vault tool
  and Core rule whether it reached its destination. Nothing is listed by hand; each set is
  read from the release, so a file shipped tomorrow is covered tomorrow. A hook that ships
  but is registered nowhere is itself a finding.
  Every other check reported "clean" through all three releases: the manifest matched, the
  files were present, the registration list agreed with itself. Only doing the install
  catches an inert hook.

### Earlier in 0.12.2



**A vault is part of the install, not a thing to remember afterwards.**

The enforcement hooks are wired against a vault, so an install without one ends with
them present and inert — and the next session start reports them unwired, which reads
as a broken install.

- `install.sh --vault <path>` creates or connects a vault and wires everything, in one
  command. `GT_VAULT` does the same. `--no-vault` is the deliberate opt-out.
- With no vault and no flag: at a terminal it asks where the vault should go; anywhere
  else — an agent, a pipe, CI — it stops with **exit 4** and says what it needs. It
  never invents a directory or claims `~/.claude/vault-config.json` unasked, because
  where your memory lives is not an installer's decision.
- Every new vault carries `OPEN-IN-OBSIDIAN.md` with its own path filled in: how to
  open a folder as a vault, which plugins the conventions actually rely on (Dataview,
  because `[p:: 1]` inline fields are its syntax; Obsidian Git, because the vault is a
  repo), and which generated files must not be hand-edited.
- `--help` prints real usage. It had been extracting a nearby comment block, and
  printed the rollback instructions instead.

## gt 0.12.1 — 2026-09-12

**A hook that ships inert is worse than one that does not ship.**

0.12.0 added a fourth enforcement hook. `install.sh` wires only the seven hooks it owns;
the enforcement hooks belong to `vault_init.py`, which ran them only when a vault was
*created*. So every machine that already had a vault installed the new hook and never
registered it, and reported it unwired at session start with no instruction that would
fix it — while the component check called the install clean.

- `install.sh` now wires the enforcement hooks whenever a vault is already configured,
  so an upgrade registers a newly shipped hook instead of copying it and stopping. The
  call is idempotent; a second install says "already wired".
- The vault-write guard no longer inspects heredoc bodies. It denied a command that was
  *writing* a script containing a tool call — a guard that fires on a quoted mention is
  one people switch off, and then it guards nothing.
- Only the current and previous releases stay on disk, so 0.11.0 is now in git only.

## gt 0.12.0 — 2026-09-11

**A vault tool must be told which vault it means.**

A session about to migrate a vault copied it to a scratch directory first, to see the diff
before touching anything real. One tool took `--vault`, so that half stayed in the copy;
another resolved the vault from config instead, so the same rehearsal migrated every
`decisions.md` in the live vault. Nothing was lost, and the intent was the exact opposite of
what happened.

- A `PreToolUse` guard denies a vault-mutating tool run that names no target — no `--vault`,
  no `--dry-run`, no `GT_VAULT`. Read-only subcommands are never denied, and anything the
  guard cannot parse with certainty is allowed: it fails open, because a guard that blocks
  wrongly makes every session unusable.
- The release gate enforces that every shipped vault tool accepts `--vault` and `--dry-run`,
  so the gap cannot reopen.
- `install.sh` no longer rewrites `MANIFEST.json` in the tree it installs from. Every run
  used to change its timestamp, dirtying git and silently modifying any shared copy someone
  installed from. The drift protection moved to the release gate, which verifies the manifest
  against the tree on every release.
- The end-of-install skill summary is derived from what was actually installed. It had been a
  hardcoded list, and told new users that a shipped skill did not exist.

**Two new commands, and thirteen fewer release directories.**

- `/gt:gt-upgrade` updates the VAULT after `install.sh` updates the plugin — the step that did
  not exist, which is why adopting 0.11.0 meant reading source for the migrations and running
  them by hand across 43 projects. It records a version stamp, keeps the merge base for
  `PROTOCOL.md` and `CONVENTIONS.md` inside the vault, rehearses with `--dry-run`, refuses a
  dirty tree, backs up before applying, and reports the steps that need a person (a duplicate
  ADR number, a document conflict) rather than guessing at them.
- `/gt:gt-doctor` answers "is this install healthy?" in one command: version, component drift,
  hook wiring, pending migrations, stray workers, unpushed commits, publish-destination drift,
  lint. Exit 2 means a check *could not run*, which is deliberately not the same as clean.
- `sync-gt-src.sh` names the files in the publish destination that it did not write, before
  the backup and the delete. A flat 0.9.13-era `scripts/` and `templates/` had appeared there
  and were removed inside forty lines of rsync output where nobody could see it.
- The migration round-trip gate compared both sides with `rstrip`, so a trailing blank line
  could be dropped while the tool reported a byte-identical round trip. It now compares
  exactly and says so when the rendering differs.
- Only the current and previous releases stay on disk. Earlier ones are in git; `install.sh`
  documents the two-step rollback.

## gt 0.11.0 — 2026-09-11

**`log.md` and `decisions.md` become generated files, so concurrent sessions stop colliding.**

More than one session works a vault at a time and they share one working tree. There are no
branches to collide, so there is no merge and no conflict marker — just a last writer who
wins silently. One session appended a line to `log.md`, committed it, and carried nine more
written by earlier sessions that happened to be uncommitted.

- Each session appends to a spool file only it writes; the shared file is rendered from those
  spools, the same relationship `TASKS.md` already had. New tools: `gt_log.py`, `gt_adr.py`,
  and the shared `gt_spool.py`.
- ADR numbers are allocated with a single atomic exclusive create, so two sessions cannot both
  take ADR-6 — which had already happened. A read-then-write counter does not fix this: that
  is two operations, so both sessions read 5 and both write 6. Verified under contention at
  100 racing process pairs, 200 allocations, zero duplicates.
- Migration (`gt_log.py migrate`, `gt_adr.py migrate <project>`) freezes what is already there
  as a baseline that sorts first — **no history is rewritten and no ADR is renumbered** — and
  refuses unless the merge reproduces the original file byte for byte. It refuses outright on
  a project whose ADR numbers already collide.
- `gt-lint` gains `adr-collision` (which ignores a deliberate `## ADR-6 amendment:`) and
  `generated-hand-edited`.
- The release scrub can extract PDF text on another host, so the machine that builds releases
  no longer needs a PDF library installed to scan for leaked strings.

## gt 0.10.0 — 2026-09-11

**`gt-watch`: watch any git repo, and open the next session with a P0 when it ships a
security fix.**

## gt 0.9.14 / gt-wiki 0.1.2 — 2026-09-11

**The 38 defects the test harness found, and a demo that never touches your vault.**

The first release after a test harness was written for every shipped tool. `gt-demo` runs a
guided tour against its own throwaway vault.

## gt 0.9.13 — 2026-09-10

**The component check verifies that hooks are *wired*, not merely installed.**

A machine had every file installed and the component check reporting clean, while no
session-start hook had ever run — its settings had no entries at all. The manifest recorded
only files, so "components clean" truthfully meant "the files are present" while nothing was
connected to anything.

- `MANIFEST.json` declares the hook entries each release expects, and the check compares them
  against live settings. Two states: `unwired` (nothing references the script) and `badpath`
  (wired, but naming a path that does not exist on this machine).
- `install.sh` registers from that same declaration and verifies what it wrote, so the
  installer and the checker cannot disagree about what "wired" means.
- The install summary moved to the end of the run; hook registration used to print below
  "Restart Claude Code", past what reads as the end of the output.

## gt 0.9.12 — 2026-09-09

**`gt-route` — a routing check for the middle of a session.**

## gt 0.9.11 / gt-wiki 0.1.1 — 2026-09-08

**The linters see the vault the way Obsidian does, and a weekly lint runs itself.**

## gt 0.9.10 — 2026-09-07

**A seventh Core rule: secrets rest only in the store.**

## gt 0.9.9 — 2026-09-05

**The plugin stands on its own, and `selftest.sh` proves it.**

## gt 0.9.8 — 2026-09-05

**An inbox the rollup renders, and a close-out question that learns.**

## gt 0.9.7 — 2026-09-05

**The six findings from the 2026-09-05 review of the public repo.**

## gt 0.9.6 — 2026-09-02

**Two fixes that had lived on one machine since 0.8.0, and two truncation paths in
`safe_write`.**

`write(mode="a")` applied the mode to a temporary file rather than the target, so an append
replaced the file with only the new bytes. An independent validator then found that the first
fix left a second path open: `os.path.exists()` answers False for "I cannot tell" as well as
"not there".

## gt 0.9.5 — 2026-08-31

**The `PreToolUse` guard was copied to every machine and registered on one.**

## gt 0.9.4 — 2026-08-29

**The enforcement layer could not tell what it had installed.**

## gt 0.9.3 — 2026-08-22

**The scaffold now emits a `## Tasks` section.**

## gt 0.9.2 — 2026-08-22

**The secrets validator blocked correct work and missed real credentials.**

## gt 0.9.1 — 2026-08-22

**`gt_lint` could never resolve a project link.**

## gt 0.9.0 — 2026-08-22

**Ships `gt-validate`, and documents how the plugin is actually used.**

## gt 0.6.0 — 2026-08-16

**The two-path Core model, canary rationale, and hook-path corrections.**
