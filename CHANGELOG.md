# Changelog

Releases of the `gt` and `gt-wiki` plugins. `install.sh` always installs the newest
version directory present, so upgrading is `bash install.sh` and a restart of Claude Code.

Announcements for recent releases are posted under
[Discussions → Announcements](../../discussions/categories/announcements).

Entries from 0.9.13 onward are written from the change itself. Earlier entries are the
release's own summary line, kept short rather than reconstructed after the fact.

---

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
