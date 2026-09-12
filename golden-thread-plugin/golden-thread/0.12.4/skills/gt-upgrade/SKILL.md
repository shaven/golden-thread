---
name: gt-upgrade
description: "Bring an existing vault up to the installed plugin release: run the migrations it has not had, take the release's changes into PROTOCOL.md and CONVENTIONS.md without losing local edits, add newly shipped Core rules, and stamp the vault. Use when the user says: upgrade the vault, is my vault up to date, I just installed a new version, what do I need to run after installing, migrate the vault."
---

# Upgrade a vault to the installed release

`install.sh` updates the **plugin**. This updates the **vault** — the migrations,
documents and rules that live in it.

## Steps

**Step 1 — Locate the vault.** `$GT_VAULT` if set, else `vault_path` from
`~/.claude/vault-config.json`. If neither, tell the user to run `/gt:gt-init`.

**Step 2 — Say what is pending, before anything else.**

```bash
python3 <base_dir>/../../scripts/gt_upgrade.py status --vault "<vault>"
```

Read the result out: the release, the vault's stamp, and every pending step with its
reason. A vault with no stamp is treated as **old**, not current — every migration is
offered, and each one detects its own work, so a step that already happened is a no-op.

**Step 3 — Rehearse. Always.**

```bash
python3 <base_dir>/../../scripts/gt_upgrade.py run --vault "<vault>" --dry-run
```

This writes nothing — no stamp, no backup, no files. Show the user what it would do
and get their go-ahead before applying. This step exists because on 2026-09-11 a
rehearsal run by hand wrote the live vault instead of the copy.

**Step 4 — Commit the vault first.** `run` refuses a dirty tree, and rightly: the
upgrade should be one `git checkout` away from undone. If the user has uncommitted
work, commit it or stash it. `--allow-dirty` exists but is not the default answer.

**Step 5 — Apply.**

```bash
python3 <base_dir>/../../scripts/gt_upgrade.py run --vault "<vault>"
```

It backs the vault up to `~/.claude/golden-thread/backups/` first, applies each step,
stamps the vault and writes a line to `log.md` through `gt_log.py`.

**Step 6 — Report what needs a person.** Exit 1 means some step needs an owner:

| What you will see | What it means |
|---|---|
| `REFUSED: … duplicate ADR number(s)` | two decisions share a number. The owner picks which keeps it; renumbering breaks inbound references, so never do it unasked |
| `CONFLICT` on a document | the merged text with markers is beside the document as `.merge-conflict`; the document itself is untouched |
| `no merge base recorded` | the document was left alone and a base captured, so the *next* upgrade can merge it |

Nothing else is skipped when one step needs a person. Re-running after they resolve it
continues from there.

## What it will not do

- Renumber anything, or resolve a conflict by guessing.
- Overwrite a document the owner has edited when it has no merge base.
- Overwrite a Core rule that already exists — new rules are added, existing ones left.
- Touch a vault whose git tree is dirty, unless explicitly told to.

## Where the pieces live

| Path | What |
|---|---|
| `Projects/golden-thread/.vault-version.json` | the stamp: which release this vault's files came from |
| `Projects/golden-thread/.templates/` | the merge base — the template text the vault was last seeded or upgraded from |
| `~/.claude/golden-thread/backups/vault-*.tar.gz` | the pre-upgrade backup |

The base lives **in the vault** on purpose: only the current and previous releases stay
on disk, so a base kept in the plugin would disappear exactly when an old vault needed
it most.
