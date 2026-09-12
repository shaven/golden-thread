---
name: gt-doctor
description: "Check the health of this Golden Thread install in one command: plugin version, component drift, hook wiring, pending vault migrations, stray workers, unpushed commits, publish-destination drift and a lint summary. Use when the user says: is everything healthy, check my install, gt doctor, health check, what state is golden thread in, is anything broken, why is a hook not firing."
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
migration, an unpushed commit and a stray worker each need a person to decide.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | all clear |
| 1 | something needs attention |
| 2 | a check could not run — **not** the same as clean |

Exit 2 matters: "could not check" reported as "clean" is the failure this whole tier
exists to prevent. If you see it, say which check could not run and why.

## What each check answers

| Check | Question |
|---|---|
| `version` | is the newest release the one installed? |
| `components` | do the installed files match that release? |
| `wiring` | is every hook the release declares actually in `settings.json`? |
| `vault` | is the vault reachable, and are `log.md`/`decisions.md` migrated? |
| `workers` | are background processes running that nobody declared? |
| `push` | do this machine's commits exist anywhere else? |
| `gt-src` | does the publish destination still hold only what was published? |
| `lint` | what does the vault linter say, in one line? |
