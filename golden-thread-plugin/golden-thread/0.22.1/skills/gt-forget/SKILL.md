---
name: gt-forget
description: "Forget a fact that turned out wrong: find every vault note that states it and retract the ones the owner picks, each marked with reason, date and where the right fact lives, deleting nothing. Use when the user says: forget that, that fact is wrong, retract this note, the vault says X but it is no longer true, unlearn this."
plain:
  - cmd: "/gt:gt-forget"
    does: "Retracts a fact that turned out to be wrong, everywhere the vault states it."
    how: "Finds every note that states it, shows each one, and marks the ones you choose as retracted with the reason and date. Nothing is deleted."
    when: "You found out something the vault says is wrong."
    next: "/gt:gt-lint"
model_intent: balanced
---

# Golden Thread Forget

A wrong fact is rarely in one place. A host moved, a figure was mismeasured, a decision was
reversed — and every note that repeated it in passing goes on being read as current. This finds
them all and **retracts** the ones the owner picks. Nothing is deleted: a retracted note stays on
disk and readable; gt-open stops listing it, gt-query ranks it last with `[RETRACTED: <reason>]`,
and gt-lint reports a retraction whose replacement is missing.

**Under gt sandbox mode** (`gt_settings.py get sandbox_mode` is `on`): the shell cannot run the
vault scripts. Each step below takes the route in this table; with sandbox mode off nothing changes.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_forget.py search …` | `vault_search` — search with the gt-vault MCP tool; read a match with `vault_read` |
| `gt_forget.py retract …` | `vault_queue_write` — queue each marker (the formats are below); say that you did |

## Steps

**Step 1 — Get the vault and the statement.** Use `$GT_VAULT` if set, else `vault_path` from
`~/.claude/vault-config.json`. Ask for the wrong statement in a few words if the user has not
given it, and what is true instead (and which note says so), if they know.

**Step 2 — Search. This writes nothing.**
```bash
python3 <base_dir>/../../scripts/gt_forget.py search "<the wrong statement>" --vault "<vault>"
```
Each match names its file, line and `## ` section (`exact`: the statement itself; `words`: every
significant word of it in one paragraph). Show the owner the numbered list as printed.

**Step 3 — The owner chooses.** Ask which to retract — by number, all, or none — and for each,
whether the WHOLE note is wrong or only its section. Never retract a match the owner did not
choose, and never infer that other facts depend on this one.

**Step 4 — Retract each chosen note**, one command per note:
```bash
python3 <base_dir>/../../scripts/gt_forget.py retract <path> --reason "<why it is wrong>" \
    [--section "<## heading>"] [--by <note that holds the right fact>] --vault "<vault>"
```
It queues one write through the write queue (Core rule 1) and drains it. The marker is one
frontmatter key, `retracted: "<date> | reason: … | replaced_by: …"`, on a note that has
frontmatter, or the line `> retracted: <date> | reason: … | replaced_by: …` as the note's first
line (no frontmatter) or right under the section heading (`--section`). Everything else in the
note stays byte for byte. A path outside the vault, or one the queue refuses (Sources/, Core
rules, generated files), is refused with exit 2 — report it; do not work around it.

**Step 5 — Report** what was retracted, what was refused or held, and suggest writing the right
fact down (gt-work or gt-promote) when no replacement note exists yet.
