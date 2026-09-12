# Changelog

Releases of the `gt` and `gt-wiki` plugins. `install.sh` always installs the newest
version directory present, so upgrading is `bash install.sh` and a restart of Claude Code.

Announcements for recent releases are posted under
[Discussions → Announcements](../../discussions/categories/announcements).

Entries from 0.9.13 onward are written from the change itself. Earlier entries are the
release's own summary line, kept short rather than reconstructed after the fact.

---

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
