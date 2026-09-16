---
name: gt-optimize
description: "Find vault content that costs context and earns nothing back — duplicated facts across memory files, dead index rows, relative dates that rot, memory files over budget. Applies only the mechanically safe cases."
---

# Golden Thread Optimize

Memory is read into every session. A duplicated fact is paid for on every turn, in every
project, forever — this finds that waste.

## What it is not

This is not `/gt:gt-lint`. Lint asks *is the vault structurally correct* — broken links, missing
index rows. This asks *is the vault paying for words it does not need*. Different question,
different answer, run both.

## The two classes, which are the whole design

| class | what it is | `--apply` |
|---|---|---|
| **SAFE** | decidable from the text alone, no judgement: a blank-line run, an index row pointing at a file that does not exist | applied |
| **JUDGEMENT** | two facts saying the same thing, a memory file over budget, a relative date, a repeated line | **never applied** — reported with evidence |

A repeated line is judgement, **not** safe. Measured against a real vault, the examples found
were an ADR's required `- **Rejected alternatives**:` section label and a line shared by two
Dataview queries. In prose a repeated line is waste; in a document built from repeated structure
it *is* the structure, and no script can tell which it is looking at.

## Steps

**Step 1 — Locate the vault**

Use `$GT_VAULT` if set; otherwise `~/.claude/vault-config.json`. If neither → `/gt:gt-init`.
The vault is **always named explicitly** on the command line — never inferred (Core rule 2).

**Step 2 — Report first, always**

```bash
python3 <base_dir>/../../scripts/gt_optimize.py --vault "<vault>" [--project SLUG] [--json]
```

Without `--apply` **not a byte changes**. Read the report with the user before touching anything.

**Step 3 — Apply the safe class, if the user wants it**

```bash
python3 <base_dir>/../../scripts/gt_optimize.py --vault "<vault>" --apply
```

`core-rules/` is **never** written by this script, at all, under any flag. `global-memory/`
requires `--include-global-memory`. That refusal lives in the script itself because
`guard_protected_paths` is a PreToolUse hook: it sees the model's Write tool and never sees a
script writing a file, so script-driven edits would otherwise walk straight past the guard.

**Step 4 — Work the judgement list with the user**

These are the valuable ones and they need a person:

- `duplicate-fact` — the same sentence in two context-loaded files. Decide which one owns it and
  link to it from the other. Usually the answer is "the more specific scope wins".
- `memory-bloat` — a `global-memory/` file over budget. Move the detail to `Knowledge/`, leave
  the fact. Remember Core rule 6: global-memory is only for what EVERY project needs.
- `relative-date` — "last week" in a file injected into a session months later. Replace with the
  absolute date. Only flagged in silently-loaded files, where it genuinely misleads.
- `duplicate-line` — check whether it is structure before removing it.

**Step 5 — Record**

Log the run in `log.md` with `work`. If facts were merged or moved, note where they went in the
project's `research.md`, so the next session does not go looking for the copy that was removed.

## Rules

- **Report before apply, always.** A tool that reports and edits in one breath is one people
  stop running.
- **Never apply a judgement finding**, however obvious it looks. Deleting knowledge is not
  reversible by reading a diff.
- **Never edit `core-rules/` with this.** A Core rule is re-asserted into every turn of every
  session; it is changed deliberately, by a person, with the reasoning recorded.
- Only files this tool is *for* are touched — memory, global-memory, core-rules (read-only),
  CLAUDE.md and the living project docs. Recorded artifacts and `Sources/` are never proposed.
