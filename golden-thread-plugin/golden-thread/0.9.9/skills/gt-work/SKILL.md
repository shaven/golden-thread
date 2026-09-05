---
name: gt-work
description: "Capture session findings into the vault at the end of a work session — append to research.md, add ADRs to decisions.md, refine design.md, create spec.md when design is complete, update PROTOCOL.md for cross-project process rules."
---

# Golden Thread Work

Write back this session's findings into the vault. Run at the end of any meaningful work session.

## Context

Read `~/.claude/vault-config.json` for vault path. If missing → tell user to run `/gt:gt-init`.

Ask if not obvious: "Which project are we writing back to?" (show available project slugs from `<vault>/Projects/`)

## Scope Classification

Before writing anything, classify each finding:

| Scope | Where to write | Test |
|---|---|---|
| Session-only | Don't write | Won't need it again |
| This project | `research.md` / `decisions.md` / `design.md` / `runbook.md` | Specific to this codebase, team, or service |
| Cross-project | `Knowledge/` wiki page | Platform constraint, infra fact, or tool truth that applies beyond this project |
| Every session | `global-memory/` | Constant needed in ALL projects, regardless of codebase |

**Rule:** when in doubt, write to the project first. Facts earn their way up the hierarchy by recurring. A single incident is not enough for `global-memory/` — flag it in `research.md` and promote after it applies in a second unrelated project.

## What Gets Written Where

### research.md — append-only findings

New discoveries, gotchas, measured behaviors, or anything surprising goes here.

```markdown
## YYYY-MM-DD: <short title>
<finding in plain language — what you learned, what broke, what the fix was>
```

Rules:
- Append only — never edit or remove existing entries
- One entry per finding, date-stamped
- If a finding supersedes an earlier one, note it: "Supersedes 2026-01-15 entry"

### decisions.md — append-only ADRs

New or revised technical decisions.

```markdown
## ADR-N: <title>
- **Decision**: What was decided
- **Context**: Why — the constraint, incident, or requirement that drove this
- **Rejected alternatives**: What else was considered and why it lost
```

Rules:
- Append only, sequential numbering
- To reverse a decision, add a new ADR that supersedes it — never edit the old one
- Only write here if the decision is stable and won't change next session

### design.md — iteratively refineable

Current architecture, component relationships, data flow. Unlike the others, this file is updated in place as the design evolves.

Rules:
- Rewrite sections that changed this session
- Keep it current — it should always describe NOW, not history
- History goes in research.md or decisions.md, not here
- Mark open questions clearly; move resolved ones to a "Resolved" section (don't delete)

### spec.md — handoff artifact (create when design is complete)

A spec is a self-contained implementation document designed to be handed to another session, developer, or agent with zero prior context. Create it when all open design questions in `design.md` are resolved.

Ask: "Is the design settled enough to write a spec?" If yes:

```markdown
# <Project> Implementation Spec

## What to change
<exact files, functions, or systems to modify>

## Expected behavior
<precise description of what the code/system should do when done>

## Tests to write
<what tests to add and what they must verify>

## Acceptance criteria
- [ ] <specific, verifiable condition>
- [ ] <specific, verifiable condition>
```

Rules:
- A spec must be implementable by someone reading ONLY the spec — no assumed prior context
- Reference research.md and decisions.md for "why" — don't duplicate their content
- Update a spec only when scope changes; completed items get checked off, not deleted
- Once all acceptance criteria are checked, the project stage is "done"

### runbook.md — operational procedures (if it exists)

Add project-specific operational steps, environment setup, or deployment notes. This file is for project-specific HOW-TO — process rules that apply across projects go in PROTOCOL.md instead.

Rules:
- Append new procedures; update existing ones in place if they changed
- If a procedure also applies to other projects, flag it for `/gt:gt-runbook-lint`

### memory/ files — update in place

Session memory files (feedback.md, project-state.md, etc.) are updated in place. These are the most frequently changing files.

### PROTOCOL.md — cross-project process rules

`<vault>/Projects/PROTOCOL.md` holds rules that apply across ALL projects — not project-specific facts, not one-time incidents.

Write here when a ruling from this session will apply to future sessions and has been proven across more than one context (one incident is not enough — flag it in research.md and let it recur once before promoting).

Format: short imperative rules, grouped by concern. Strip incident-specific details — those stay where they originated.

Rules:
- Never add project-specific facts to PROTOCOL.md — those go in decisions.md or runbook.md
- PROTOCOL.md is not a CLAUDE.md — no tool or platform facts
- When in doubt, leave it in the project and flag for `/gt:gt-promote` after it recurs

## Promotion Candidates

After writing, check: does any finding apply beyond this project?

- A platform constraint (Kubernetes, auth, infra) → candidate for `Knowledge/`
- A cross-project tool or config fact → candidate for `global-memory/`
- An idea for a separate project → candidate for a new project scaffold
- A rule that has now applied in multiple projects → candidate for `PROTOCOL.md`

Ask: "This looks like it applies beyond `<project-slug>`. Should I add it now, or flag for `/gt:gt-promote` later?"

## Log Entry

Append to `<vault>/log.md`:
```
<today> [work] <project-slug> — wrote N finding(s), M ADR(s), updated design[, created spec]
```

## Ask whether the project is finished

Closing a project is a decision nobody makes unless asked. Two moments call for it:

1. **You just checked off a task** and it was the last open one at `p:: 2` or better,
   or the last task with a due date.
2. **The close-out probe names this project.** Run it — it is cheap:
   ```bash
   python3 <vault>/Projects/golden-thread/tools/gt_closeout.py signals <slug>
   ```
   If any rule fires (`R1` most tasks past due, `R2` most tasks done and nothing
   urgent, `R3` three quiet weeks, `R4` nothing open), record that you are asking and
   then ask:
   ```bash
   python3 <vault>/Projects/golden-thread/tools/gt_closeout.py ask <slug> gt-work
   ```
   > "`<slug>` looks finished: <reasons>. Close it? **yes** / **no** / **later** — and why?"

Record the answer, whatever it is; the record is how the thresholds get tuned to the
user's own pattern rather than a guess:
```bash
python3 <vault>/Projects/golden-thread/tools/gt_closeout.py answer <slug> yes|no|later "<their words>"
```

**If yes, closing means:** `stage: complete` in `README.md` frontmatter (`archived`
only once nothing at all remains); every leftover open task moved to `[p:: 7]` so
it stays in the README as a record but leaves the rollup and stops escalating the
project; `pp:` lowered to `3`; a final `research.md` entry saying what shipped; then
the promotion check below, because a finished project is where the vault's most
general lessons usually are.

**Never delete a task to close a project.** Shelve it (`p:: 7`) or check it off with
a pointer to what superseded it.

## Keep the project's properties current

`README.md` frontmatter drives the vault's index views. When a session changes
the project's reality, update the property too — not just the prose:

| If this changed | Update |
|---|---|
| The project moved phase (idea → research → design → active → complete) | `stage:` |
| Where the code lives, or a new host was added | `topology:` and `source.md` |
| The project's grouping | `domain:` |

The `## Stage` heading in the body and the `stage:` property must agree. If they
disagree, the property is what the Dataview views show, so fix the property and
make the prose match it.

## Tier every rule you write

Memory files carry `level` and `enforcement` in frontmatter so a rule's durability is
explicit from the moment it is written:

```yaml
metadata:
  type: core | feedback | user | reference
  level: core | context | generic      # default generic
  enforcement: validated | reminder    # required iff level: core
```

Default to `generic`. Do **not** set `level: core` here — Core is a promotion, and it
requires wiring an enforcement hook, which is `/gt:gt-promote`'s job. A file marked
`level: core` without that wiring is exactly what `gt-lint`'s `core-unenforced` check
exists to catch.

Template: `templates/memory-file.md`.
