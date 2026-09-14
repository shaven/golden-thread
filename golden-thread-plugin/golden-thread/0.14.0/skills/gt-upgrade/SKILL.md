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

**Step 6 — Report what needs a person.** Exit 1 means a step needed an owner; `needs a
person:` lines are printed by `status` and `run` on every call until they are dealt with.
`install.sh` prints them too but never acts on them.

| What you will see | What it means |
|---|---|
| `REFUSED: … duplicate ADR number(s)` | two decisions share a number. The owner picks which keeps it; renumbering breaks inbound references, so never do it unasked |
| `CONFLICT` on a document | the merge conflicted: the merged text with markers is beside the document as `<doc>.merge-conflict`; the document itself is untouched |
| `needs a person: no merge base` | gt never recorded a base for this document, so it cannot tell your edits from the release's changes. It is left untouched and never merged — see **No merge base** below |
| `needs a person: conflict awaiting you: <path>.merge-conflict` | a conflict gt already wrote, for this exact base, template and document. It is not redone (no merge, no stamp, no rewrite) until something changes — see **Conflict awaiting you** below |

Nothing else is skipped when one step needs a person. Re-running after they resolve it
continues from there.

### No merge base

Merging without a base would either overwrite the owner's edits or ignore the release,
so gt waits for a person. With the user:

1. Show the difference between their document and the shipped template (both paths are
   in the message):

   ```bash
   git diff --no-index "<vault>/Projects/PROTOCOL.md" "<template path from the message>"
   ```

2. Walk through it and let the user decide, section by section, what to keep from their
   version and what to take from the template. Make the edits they choose in the vault
   document. Never decide for them, and never replace the document wholesale unasked.
3. When the document is what they want, record the base:

   ```bash
   python3 <base_dir>/../../scripts/gt_upgrade.py run --vault "<vault>" --record-base PROTOCOL.md
   ```

   (`CONVENTIONS.md` likewise.) This records the **shipped template** as the base,
   meaning "reviewed against this release", so later releases merge only their own
   changes — unattended, including from `install.sh`. Only this command records a base;
   the installer never does.

### Conflict awaiting you

1. Open `<doc>.merge-conflict` and resolve each `<<<<<<<` / `=======` / `>>>>>>>` block
   with the user: their text is on one side, the release's on the other.
2. Put the resolved text into the document itself (`Projects/PROTOCOL.md`), without any
   markers, and delete the `.merge-conflict` file.
3. Record that the document is now reviewed against this release, then re-run Step 2:

   ```bash
   python3 <base_dir>/../../scripts/gt_upgrade.py run --vault "<vault>" --allow-dirty --record-base PROTOCOL.md
   ```

   After a conflict gt wrote, `--record-base` replaces the old base with the shipped
   template (it refuses while the `.merge-conflict` file still exists). Without it the
   resolved document would be merged against the old base again and conflict again
   wherever the user kept their own text. `--allow-dirty` is only because the resolution
   is uncommitted; have the user commit afterwards.

## What it will not do

- Renumber anything, or resolve a conflict by guessing.
- Merge, overwrite or record a base for a document that has no merge base, unless told
  to with `--record-base`.
- Redo a conflict it has already written for the same inputs.
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
