---
name: gt-doctor
description: "Check the health of this Golden Thread install in one command: plugin version, component drift, hook wiring, module states, pending vault migrations, stray workers, unpushed commits, publish-destination drift and a lint summary. Use when the user says: is everything healthy, check my install, gt doctor, health check, what state is golden thread in, is anything broken, why is a hook not firing."
---

# Golden Thread Doctor

One report covering every automatic check this plugin makes, plus the ones that only
ever ran at session start where a user could miss them.

## Steps

**Step 1 — Locate the vault.** Use `$GT_VAULT` if set; otherwise read `vault_path`
from `~/.claude/vault-config.json`. If neither gives a vault, the doctor still runs —
it reports `vault: no vault configured` and the other checks stand.

**Step 2 — Run it.**

```bash
python3 <base_dir>/../../scripts/gt_doctor.py
```

Add `--vault <path>` to check a vault other than the configured one, `--only <check>`
to run just one, and `--json` when another tool will read the result.

**Step 3 — Read the first line out loud.** It names the release every other answer is
relative to. A clean report from a check pinned to an old release looks exactly like a
healthy install — that is how 0.9.4 sat uninstalled beside a component check reporting
clean on 2026-08-30. Say the version, then the findings.

**Step 4 — Report each finding with its fix.** Every row that is not `ok` carries a
`fix:` line. Offer to run the safe ones; do not run them unasked.

**Step 5 — Offer `--fix` only for wiring.** `--fix` re-registers hooks that are
declared but missing from `settings.json`. It adds entries and removes none, so no
user configuration can be lost. Nothing else is repaired automatically: a pending
migration, an unpushed commit, a stray worker and a module whose plugin state
contradicts its `module.json` each need a person to decide.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | all clear — every check ran and none of them is `WARN` or `FAIL` |
| 1 | something needs attention — at least one `WARN` or `FAIL` |
| 2 | a check could not run (`?`) and nothing outright failed — **not** the same as clean |

Exit 2 matters: "could not check" reported as "clean" is the failure this whole tier
exists to prevent. It is the state of the **worst** row, ranked `FAIL`, then `?`, then
`WARN` — so a run with both a failure and a check that could not run exits **1**, and
the `?` row is still sitting there unanswered. Never read exit 1 or 0 as "everything
was checked": read the marks. Any `?` row means that question has no answer yet — say
which check could not run, and why, before summarising anything else.

A `-` row is neither: it is a check that does not apply to this machine because nothing
configured it (no publish destination, for instance). It never raises the exit code,
and it is not "clean" either.

## What each check answers

| Check | Question |
|---|---|
| `version` | is the newest release the one installed? |
| `components` | do the installed files match that release? |
| `wiring` | is every hook the release declares actually in `settings.json`? |
| `core-rules` | does each Core rule that claims a mechanism actually have THAT mechanism wired? |
| `modules` | which modules are on or off and why, does each admit this gt release, and is every **on** module's plugin both registered in `installed_plugins.json` and enabled in `settings.json`? (read-only) |
| `vault` | is the vault reachable, and are `log.md`/`decisions.md` migrated? |
| `workers` | are background processes running that nobody declared? |
| `push` | do this machine's commits exist anywhere else? |
| `gt-src` | does the publish destination still hold only what was published? |
| `lint` | what does the vault linter say, in one line? |

Those ten are the whole list — `--only` accepts exactly these names.

**`core-rules` is not the same question as `wiring`, and the difference is the point.**
`wiring` asks whether every hook the RELEASE declares is present in `settings.json`.
`core-rules` starts from the other end: it reads each `level: core` rule in the vault and
checks that the specific script implementing its declared `enforcement` is wired — a
`reminder` rule needs a `UserPromptSubmit` hook running `inject_core_rules.sh`, a
`validated` rule needs a `Stop` hook running `validate_response.sh` — and that the script
exists on disk. It tolerates extra arguments and is strict about which script.

It was added in 0.16.5 because **nothing verified this**. The docstring had advertised the
check for releases while `CHECKS` did not contain it, and the one place that looked —
`gt_lint`'s `core-unenforced` — only asked whether SOME hook occupied the event, so a vault
with any third-party `UserPromptSubmit` hook and `inject_core_rules.sh` absent reported the
Core tier enforced. Both are fixed; this is the check that answers the question directly.

A failure here means a rule is **stored but not enforced** — present in the vault, read by
every session, and backed by nothing. The fix line is
`vault_init.py install-core-rules --vault <vault>` — named explicitly, per Core rule 2,
because a mutating tool must never infer its target.

Scope: it looks only inside `core-rules/`. A `level: core` file somewhere else is
`gt_lint`'s `core-misplaced`. The `enforcement` vocabulary is only `reminder` and
`validated`, so the `PreToolUse` guards some rules describe in prose are covered by
`wiring`, not here.

`modules` is read-only and never repairs anything. A module that is **off** is a choice
and is reported as "not installed by choice", never as drift — what it flags is a
contradiction: an off module whose plugin is still installed or enabled, an on module
whose plugin is missing, an invalid `module.json`, or a module that does not admit the
running gt release.
