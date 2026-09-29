---
name: gt-optimize
description: "Find vault content that costs context and earns nothing back — duplicated facts across memory files, dead index rows, relative dates that rot, memory files over budget. Reporting never writes; the one write action is `--demote`, which moves a note to a cheaper tier and leaves a pointer behind."
---

# Golden Thread Optimize

Memory is read into every session. A duplicated fact is paid for on every turn, in every
project, forever — this finds that waste. It never removes anything: the only thing it writes
is a demotion.

## What it is not

This is not `/gt:gt-lint`. Lint asks *is the vault structurally correct* — broken links, missing
index rows. This asks *is the vault paying for words it does not need*. Different question,
different answer, run both.

## The two classes are labels, not permissions

The report is printed in two sections. Both are **reported and never applied** — the class says
how confident the finding is, not what may be done with it.

| class | printed as | what it is |
|---|---|---|
| mechanical | `MECHANICAL — decidable from the text` | a blank-line run, a `MEMORY.md` row pointing at a file that is not there |
| judgement | `JUDGEMENT — reported, never applied` | two facts saying the same thing, a memory file over budget, a relative date, a repeated line |

A repeated line is judgement, **not** mechanical. Measured against a real vault, the examples
found were an ADR's required `- **Rejected alternatives**:` section label and a line shared by
two Dataview queries. In prose a repeated line is waste; in a document built from repeated
structure it *is* the structure, and no script can tell which it is looking at.

## There is no mechanically-safe `--apply`

`--apply` **without** `--demote` is a usage error (exit 2), and there is no flag that applies the
mechanical class.

One existed, for about an hour on 2026-09-16. It was restricted to a "SAFE" class said to be
decidable from the text alone, and an independent validation took it apart: `dead-index-row`
deleted **live** rows, because the link target was parsed with `[^)]+` and used raw, so
percent-encoded names, titled links, angle-bracket links and `mailto:`/`obsidian://` links all
read as missing files — seven of the eight rows in the fixture were live, and a deleted row
carries the note's one-line description, which no diff brings back. Blank-run collapsing was not
fence-aware and rewrote the inside of code blocks. Findings computed from one read were applied
to another by line index. CRLF files were rewritten to LF and mode `0600` became `0644`.
`--project` was joined to the vault unvalidated and edited files outside it entirely.

It was **removed rather than repaired**. Each failure was a case the author had not thought of,
which is the point: the "safe" classification was a claim about the world, and the world kept
producing exceptions. The findings are still worth having — and the valuable ones were always
the judgement class, which needed a reader from the start.

## The ladder is about cost, not maturity

`/gt:gt-promote` moves knowledge **up** by how settled it is. `--demote` moves it **down** by how
often it is paid for:

| tier | read | cost |
|---|---|---|
| `global-memory/` | every session of every project | most expensive |
| `Projects/<slug>/memory/` | every session of one project | |
| `Knowledge/<page>.md` | when someone asks for it | cheapest |

A fact in `global-memory/` that only one project needs is charged to every session forever.
Moving it does not make it less true — it makes it cost what it is worth.

## Steps

**Step 1 — Locate the vault**

Use `$GT_VAULT` if set; otherwise `~/.claude/vault-config.json`. If neither → `/gt:gt-init`.
The vault is **always named explicitly** on the command line — never inferred (Core rule 2).

**Step 2 — Report. Reporting never writes.**

```bash
python3 <base_dir>/../../scripts/gt_optimize.py --vault "<vault>" [--project SLUG] [--json]
```

`--project` takes a **slug, not a path**: a value containing `/` or `\`, or that is `.` or `..`,
or that resolves outside the vault, is a usage error. Each section prints its first 40 findings and then
a count of the rest.

Read the report with the user. Most findings are for a person to act on by editing the file.

**Step 3 — Work the judgement list with the user**

These are the valuable ones and they need a person:

- `duplicate-fact` — the same sentence in two context-loaded files. Decide which one owns it and
  link to it from the other. Usually the answer is "the more specific scope wins".
- `memory-bloat` — a `global-memory/` file over 30 non-blank lines. The message names the
  `--demote` command for it. Remember Core rule 6: global-memory is only for what EVERY project
  needs.
- `relative-date` — "last week" in a file injected into a session months later. Replace with the
  absolute date. Only flagged in silently-loaded files, where it genuinely misleads.
- `duplicate-line` — check whether it is structure before removing it.

**Step 4 — Demote, when a note belongs somewhere cheaper**

```bash
# preview — nothing moves
python3 <base_dir>/../../scripts/gt_optimize.py --vault "<vault>" \
    --demote global-memory/<note>.md --to project-memory --project <slug>

# do it
python3 <base_dir>/../../scripts/gt_optimize.py --vault "<vault>" \
    --demote global-memory/<note>.md --to project-memory --project <slug> --apply
```

`--to` is `knowledge` or `project-memory`; omitted, it is one tier cheaper than where the note
sits. `--to project-memory` requires `--project <slug>`. The path is relative to the vault. The
work is done by `gt_demote.py`, which `gt_optimize.py` delegates to so the order below has one
implementation and one set of tests.

**The order is the safety property:**

1. **write** the destination, with a provenance comment naming where it came from;
2. **verify** it by re-reading it from disk and comparing digests — then re-read both sides again
   immediately before the source is touched, because the invariant is not "it was there" but "it
   is there now";
3. **only then** replace the source with a small pointer to where it went.

A failure at any step leaves **duplication** — the same content in two places, which a reader can
see and resolve. The old `--apply` failed by **deletion**, which no diff brings back. That
asymmetry is the entire reason demotion exists as a separate command rather than as another
`--apply`.

A `MEMORY.md` row naming the moved file is **not** rewritten. The run says so, before and after:
those rows now point at the pointer, which is correct and worth tidying by hand.

**Step 5 — Record**

Log the run in `log.md` with `work`. If facts were merged or moved, note where they went in the
project's `research.md`, so the next session does not go looking for the copy that was moved.

## What a demotion refuses

Refusals live in the script because `guard_protected_paths` is a PreToolUse hook: it sees the
model's Write tool and never sees a script writing a file, so script-driven edits would otherwise
walk straight past the guard. These are the ones the code performs:

- any path segment resolving to **`core-rules/`** or **`Sources/`** — compared on the resolved
  path, casefolded and NFC-normalised, so `sources/` and `CORE-RULES/` do not slip past on a
  case-insensitive volume;
- anything **outside the three tiers** — `global-memory/`, `Projects/<slug>/memory/` and
  `Knowledge/` are the only places it moves notes between;
- a note that **is, or sits under, a symlink**, and any path that resolves **outside the vault**;
- a note already at the **cheapest tier** with no `--to`: there is nowhere further down;
- `--to project-memory` with **no `--project`**, or a `--project` that is not a plain slug
  (`[A-Za-z0-9._-]`, 1–64 characters);
- a **destination that already exists** or is a symlink — merging two notes is a judgement a
  person makes;
- a source that is **not valid UTF-8**: it will not rewrite bytes it cannot read;
- a note **another live session has claimed** (Core rule 1) — read from the vault's session
  registry, ignoring this session's own claim and any released or stale one, and reported in a
  dry run as well as under `--apply`, because learning at apply time that someone else has the
  note open is learning one step too late. **A session file that cannot be read also refuses**:
  whether anyone holds the note is then unknown, and unknown is not clear.

  This last refusal was documented in `gt_demote.py` from 0.16.1 while no code performed it, and
  was implemented in 0.16.2 once a documentation sweep read the file against its own source. It
  is worth knowing as a worked example rather than hidden: prose in a file is not evidence about
  that file.

## Exit codes

| Code | Reporting | With `--demote` |
|---|---|---|
| 0 | nothing to report | done, or a preview printed (no `--apply`) |
| 1 | findings reported | refused, and the reason is printed |
| 2 | usage — bad vault, bad `--project`, or `--apply` without `--demote` | usage — traversal in the path, or no such note |
| 3 | — | an error mid-move; nothing was removed, so the content exists in both places |

## Rules

- **Report before demote, always.** A tool that reports and edits in one breath is one people
  stop running.
- **Never act on a finding on the tool's say-so.** The class is a confidence label. Deleting
  knowledge is not reversible by reading a diff.
- **Never edit `core-rules/` with this.** It is read, and reported on, and never written. A Core
  rule is re-asserted into every turn of every session; it is changed deliberately, by a person,
  with the reasoning recorded.
- Only files this tool is *for* are looked at — memory, global-memory, core-rules, CLAUDE.md and
  the living project docs. Generated files, `Sources/` and recorded artifacts are never reported.
