# Project Conventions

Standards for all projects in this vault.

## Project Stages

| Stage | Meaning |
|---|---|
| `idea` | Captured concept, not yet started |
| `research` | Investigating feasibility |
| `design` | Architecture and approach decided |
| `active` | In development |
| `complete` | Shipped, no active development |
| `archived` | Retired or replaced |

## Project lifecycle

Projects get redefined, combined and retired. All three are supported operations —
a hand-done rename leaves links pointing at nothing.

```bash
vault_init.py rename-project  --vault <v> --from <old> --to <new>
vault_init.py merge-project   --vault <v> --from <slug> --into <slug>
vault_init.py archive-project --vault <v> --slug <slug> --reason "<why>"
```

**Nothing is deleted.** `archived` is the stage; `retire` is the log verb. A merge
leaves the source as a tombstone README recording where it went, so links written
before the merge still resolve.

Merge combines what can be combined without judgement and **refuses to guess** at the
rest: `design.md` and `source.md` are appended under a NEEDS REVIEW banner (two
architectures cannot be merged mechanically), `idea.md` is preserved verbatim because
it is immutable, and ADR ids are renumbered to avoid collision. Everything needing a
human decision is written to `review-queue.md`.

## Core rules

Core rules are defined **and enforced** from `Projects/golden-thread/core-rules/`
and apply to every project. They are the top of the promotion hierarchy, above
`global-memory/`.

A Core rule is not a rule that is *written down more emphatically* — it is one backed
by a mechanism that re-asserts it every turn. Two axes:

| Axis | Values |
|---|---|
| **Scope** | `core` (every session, turn, project) · `context` (while a context is active) · `generic` (best effort) |
| **Enforcement** | `reminder` (re-injected each turn) · `validated` (output-checked — the only unbreakable form) |

Enforcement is wired at **user-global** `~/.claude/settings.json`:
`UserPromptSubmit` → `~/.claude/golden-thread/hooks/inject_core_rules.sh` (Reminder),
`Stop` → `~/.claude/golden-thread/hooks/validate_response.sh` (Validated). Per-project
wiring scopes a rule to that project, which is the Context tier.

The hooks live **outside the vault**, installed by `install.sh`, and are deliberately
not stored in `core-rules/`. `settings.json` references them by absolute path, so
keeping them in the vault meant a project rename or a vault move silently broke
enforcement. The rules stay here as content; the machinery ships with the plugin and
locates the rules at run time (`gt_paths.py`).

Every rule carries `level` and `enforcement` in its frontmatter — see
`core-rules/core_rule_priority_model.md`.

### Two ways into Core

| Path | Gate |
|---|---|
| **Designation** (primary) | None. The user names it Core and it is Core, from that moment. No prior incident required. |
| **Promotion from levels 1–5** | Must pass the three-question test — see [[PROTOCOL]]. |

The gate asks, *if this rule were **not** enforced*: would it cause **incorrect code or
a change implemented wrongly** · would it cause **rework or a backout** · would it
**cascade** into lower-level rules being misrepresented or unfollowable. **Any single
YES qualifies.**

Observed drift is **not** an entry requirement — a rule that drifts was mis-tiered.
Drift is a diagnostic, not a qualification.

**Canary rules are designated, never tested.** A canary is kept because its absence is
*visible*, not because it is *costly* — small, cheap, and seen on every reply, so the
moment it stops appearing you know enforcement itself has broken. Every other Core rule
fails silently; a canary fails loudly. `core_timestamp_every_message` is the canonical
one: it answers NO to all three gate questions and is Core regardless. **Never demote a
canary for failing the gate.**

## Project Properties

Every project's `README.md` carries YAML frontmatter. These are real Obsidian
properties — they drive the tag pane, search, and the Dataview views in
`Projects/README.md`. Categorisation lives here, **not in folder nesting**:
nesting a second level under `Projects/` silently disables `gt-lint`'s
`memory-unlisted` check, which uses a single-level `iterdir()`.

```yaml
---
type: project              # always "project" — what the Dataview queries select on
slug: my-project           # must match the folder name
domain: trading            # the top-level grouping
stage: active              # see Project Stages above
topology: bastion-direct   # local | remote | bastion-jump | bastion-direct | n/a
tags: [trading, platform, live]
parent: other-slug         # sub-projects only
---
```

### Domain taxonomy

| Domain | Meaning |
|---|---|
| `trading` | The product itself and everything feeding it — rename to your own primary domain |
| `infrastructure` | The tooling that supports the work — vault, source control, deploy |
| `home` | The physical house — cameras, network, HomeKit. Added retroactively 2026-08-19: it was already in use by three projects but had never been written into this table. |
| `personal` | Personal-life automation that is not the house and not the platform — mail, calendar, documents. Added 2026-08-19 with `gmail-parser`. |
| `misc` | Genuinely uncategorised. If two projects land here, it needs a new domain. |

`domain` is the coarse grouping; `tags` are the fine-grained, multi-value axis.
A project has exactly one `domain` and any number of `tags`.

## Priority

Two levels, composed. A project carries **PP** (project priority); a task carries
**P**. Neither is written twice — the composite `PP1-P1` is *rendered* by the
rollup, never typed into a file. Typing it onto task lines would mean editing every
task in a project whenever its PP changed, and they would go stale silently.

### Project priority — `pp`

**PP answers one question: how bad is it if this project gets no attention this
week?** Not how interesting it is, and not purely how many things depend on it —
real money, security exposure and irreversibility raise cost-of-inaction just as
dependents do.

| PP | Meaning |
|---|---|
| `0` | Active harm accruing right now. **Nothing sits here permanently** — reached only by escalation |
| `1` | Blocks other projects, or a costly/unrecoverable failure is plausible soon |
| `2` | Real work, contained blast radius |
| `3` | Ideas and exploration; nothing downstream is waiting |

```yaml
pp: 1
pp_escalate: "Mon-Fri 07:00-15:15 America/Chicago -> 0"   # optional
```

### Task priority — `p`

Tasks are checkbox lines under `## Tasks` in the project's `README.md` — **not a
separate file.** The README is the file that actually gets maintained; the
`runbook.md` incubator is empty in every project, which is what a new file would
become.

```markdown
- [ ] Deploy the missing DNS record to the Gold [p:: 1] [waiting:: agent] [due:: 2026-08-22]
- [ ] Decide whether ADR-2 overrides the runbook [p:: 1] [waiting:: user] [since:: 2026-08-21]
```

The line is deliberately **both** valid Dataview *and* plain greppable markdown, so
Obsidian and a raw-markdown reader (`gt-open`, `grep`) see the same truth with no
sync step.

| Field | Values | Required |
|---|---|---|
| `p` | `1` urgent · `2` normal · `3` someday · `7`+ **shelved** | yes |
| `waiting` | `user` · `agent` · `external` · `parked` | yes |
| `due` | `YYYY-MM-DD` | no |
| `since` | `YYYY-MM-DD` — when it was raised, drives staleness | no |
| `blocks` | another project's slug | no |

`waiting` is the higher-signal field in practice. `waiting:: user` sorted by
priority is the human's list; `waiting:: agent` is the session's. `parked` means
deliberately not being chased — it is not a backlog.

**Shelved (`p:: 7` or higher).** The task stays in the README as a record and leaves
the rollup entirely: no section, no escalation, counted under *Project standing*
only. This is how a finished project's leftover tasks stop outranking live work
without anything being deleted. Added 2026-09-05, when 25 rehearsal tasks for a talk
already delivered held the top of `TASKS.md` for two days.

**Inbox.** A thought that arrives in the wrong project goes on one line in
`INBOX.md` at the vault root — no project, no priority, no date. The rollup shows
unchecked inbox lines at the top so they cannot be lost, and nowhere else so they
do not rank. `/gt:gt-review` files each one and checks it off with a `→ [[slug]]`
pointer. An idea filed as a task gets `p:: 3` and **no due date**: a due date on an
idea makes the deadline rule rank it above real work.

### Escalation — stored as rules, never as values

Situational priority is **computed at the moment of asking**, never written to disk.
A stored "current" priority is stale the second it is written; a stored *rule* is
correct forever. Four rules raise a project's effective PP:

| Rule | Trigger |
|---|---|
| Time window | `pp_escalate` window is currently open |
| Stale P1 | A `p:: 1` older than 7 days (by `since`) — escalates one level |
| Deadline | Any task `due` within 3 days |
| Blocking | A task carries `blocks:: <slug>` |

None of the four applies to a shelved task or to a project whose `stage` is
`complete` or `archived`. A closed project's leftover due dates are history, not
deadlines.

The **stale-P1 rule matters most.** This vault's demonstrated failure is not
misprioritisation — it is decisions correctly made and then silently never executed
(in the vault this was built in: DNS records flagged as dangling and unactioned for
13 days; a certificate migration scheduled twice and never run; a sizing decision
made and still unimplemented three weeks later). Ranking cannot catch those. Ageing can.

### The rollup

`TASKS.md` at the vault root is **generated**, never hand-edited — run
`python3 Projects/golden-thread/tools/gt_tasks.py`. It carries live Dataview queries
for Obsidian plus a static table for every other reader, the same dual-reader
pattern `Projects/README.md` already uses. Because it is derived, holding tasks in
two places is a projection rather than a duplicate.

It is the one file to act from. Above the ranked lists it renders two more sources:
**Inbox** (unchecked lines in `INBOX.md`) and **Review** (projects that
`gt_closeout.py` thinks may be finished, with the reasons).

### Closing a project

Delivery is not closure; closure is a decision, and it is asked for rather than
assumed. `Projects/golden-thread/tools/gt_closeout.py candidates` names projects
whose signals say they may be done — most open tasks past due, most tasks checked
with nothing urgent left, three quiet weeks, or nothing open. The same probe runs at
`/compact` and session end, and `gt-work` asks after the last urgent task is checked.

Closing means `stage: complete`, `pp: 3`, leftover tasks shelved at `p:: 7`, and a
final `research.md` entry. Every ask and every answer is appended to
`Projects/golden-thread/closeout-signals.jsonl` with the signal values at the time;
`gt_closeout.py history` reads it back so the thresholds can be tuned to how the user
actually closes things, not to how a script imagined it.

## Validation rule packs

Every project may carry `validation-rules.md` — the standing invariants a **validation
agent** must enforce when operating on that project, whether or not the request
mentions them. See `golden-thread/validation-agents`.

This exists because a requester who has already made a domain error will not think to
ask the validator to check for it. The pack encodes what the *domain* requires, not
what the requester remembered.

```markdown
## R1 — <INVARIANT, stated as an absolute>
**Invariant:** <the property that must hold>
### Concrete checks
1. <mechanically checkable step>
### Trap
<the way this rule is violated while appearing satisfied>
<!-- PROVENANCE-START -->
<the incident that earned it a rule, with its figures>
<!-- PROVENANCE-END -->
```

**The provenance markers are load-bearing, not decoration.** Packs are copied into
validation packets **verbatim**, and the incident narrative names the very numbers a
claim may be about — R1's own text contained "a real ~$21k to a fantasy $344k". A
validator handed that has been told the answer and can no longer re-derive it; it grades
the argument instead, which is the single failure `/gt:gt-validate` exists to prevent.

So every rule splits in two:

| Half | Contains | Goes in a packet? |
|---|---|---|
| **Operational** — invariant, concrete checks, trap | what must hold and how to check it | **yes, verbatim** |
| **Provenance** — between the markers | the incident, dates, and figures that earned the rule | **never** |

Keep figures, incident dates and outcomes strictly inside the markers. If a check
genuinely needs a number to be mechanical (a cache version, a cutoff timestamp), that
number is operational — state it in the check, not in the story.

**A rule is only useful if it is operational.** Prose like "no look-ahead" is
unenforceable; it needs the invariant, the concrete checks, and the known traps in
*this* codebase. Anything less produces a validator that agrees with whatever it sees.

Packs are **inherited by sub-projects** and **merged** with a request's own rules; on
conflict the pack wins. First pack:
`Projects/automated-trading-system/validation-rules.md` (R1 no-look-ahead → R7).

## File Naming

- Project slugs: kebab-case (`my-project`, not `MyProject` or `my_project`)
- Memory files: kebab-case (`feedback.md`, `project-state.md`)
- Knowledge pages: descriptive title case (`Hyperspace LLM Connectivity.md`)

## What Goes Where

| Content type | File |
|---|---|
| Stable rules and constraints | `decisions.md` |
| Dated findings and gotchas | `research.md` |
| Current architecture | `design.md` |
| Platform-level knowledge | `Knowledge/<page>.md` |
| Cross-project facts | `global-memory/<file>.md` |
| Durable notes that are not yet ADRs, findings, or architecture | `memory/<file>.md` |
| Invariants a validation agent must enforce | `validation-rules.md` |
| **Rules enforced on every turn** | `Projects/golden-thread/core-rules/<rule>.md` |

The last row is the only one holding **rules** rather than **facts**, and the only one
pushed into every turn by a hook rather than read on demand. See "Core rules" above and
[[PROTOCOL]] for how something gets there.

## Sub-projects, and the two meanings of "project"

The word **project** names two different things in this vault. They are unrelated,
and conflating them has already caused a real misreading — 60 files named
`project_*.md` in one folder read as a list of projects that had gone missing.

**1. A project (or sub-project) is a folder.** It has `README.md`, `idea.md`,
`research.md`, `decisions.md`, `design.md`, `source.md`, `runbook.md`, `CLAUDE.md`
and its own `memory/`. Its `README.md` frontmatter carries `type: project` and, for a
sub-project, `parent: <parent-slug>`. It appears in `TASKS.md` as
`parent-slug/child-slug` and inherits the parent's validation rule packs. Create one
only with:

```bash
python3 <plugin>/scripts/vault_init.py create-project \
  --vault <vault> --name <slug> --parent <parent-slug> --domain <domain>
```

**2. `type: project` is a memory-file category.** Inside any `memory/` folder, each
note's frontmatter carries `type:` from the memory schema — `user`, `feedback`,
`project`, or `reference`. `type: project` means "ongoing work, goals, or constraints"
as opposed to a preference or a reference fact. The `project_` filename prefix is
just that type spelled out in the name. **It confers no structure whatsoever.**

| | A sub-project | A `project_*` memory note |
|---|---|---|
| Is | a folder | a single file |
| Lives at | `Projects/<parent>/<child>/` | `Projects/<parent>/memory/<name>.md` |
| Declared by | `parent:` in README frontmatter | `type: project` in the note's frontmatter |
| Shows in `TASKS.md` | yes, with its own tasks | no |
| Created by | `vault_init.py create-project --parent` | writing a file |

Neither replaced the other. Both are current: `shome-security` has three sub-projects,
and `golden-thread/validation-agents` is one — while every project's `memory/` keeps
using `project_`-prefixed notes.

## When a memory note becomes a sub-project

Sub-projects are not free. Each one adds eight files, a `TASKS.md` grouping, and a
place for facts to be split across. Create one only when the alternative is worse.

**A cluster in `memory/` earns a sub-project when it meets ALL of:**

1. **Three or more notes on one bounded subject**, where the subject is a *thing* —
   a component, a service, a model, a host group — not a time period or an incident.
2. **It has its own decisions.** There is at least one choice that applies to this
   subject and not to its siblings. A cluster with no decisions of its own is a topic,
   not a project.
3. **It has its own work.** Open tasks that would be ranked and worked on separately
   from the parent's. If nothing is outstanding, `memory/` is sufficient.
4. **Someone could work on it without loading the parent.** If every session on it
   needs the parent's full context anyway, splitting only adds a hop.

**And ANY of these makes it urgent rather than merely allowed:**

- The parent's `memory/` exceeds ~40 notes, so its `MEMORY.md` index no longer fits
  in a reasonable read.
- The cluster already has a hand-written pointer index (an `index_*.md` note) — that
  index is a sub-project's `MEMORY.md` in everything but name.
- The parent's hub note describes the cluster as a "sub-thread" or similar. That is
  the author having already made the judgement without the structure to record it.

**Do NOT create a sub-project for:**

- A single incident, however large. Incidents are `research.md` entries.
- A cluster whose real owner is a **different** top-level project. Route it there
  instead — see below.
- A subject with no open work, kept only for history. Leave it in `memory/`.
- Symmetry. Three siblings existing does not oblige a fourth.

**Route to an existing project, don't sub-divide, when** the subject already has a
top-level project. Check `Projects/README.md` first, every time. A sub-project that
duplicates a top-level project is worse than no sub-project: it splits one subject
across two owners, which is the drift this vault exists to prevent.

**When promoting, move the whole cluster** — notes, and any `index_*.md` that points
at them — and update every wiki-link that referenced them. Record the promotion
in the parent's `research.md`, and leave the parent's hub note pointing at the new
sub-project rather than at the individual notes.

## Append-Only vs Iterative

**Append-only** (never edit old entries):
- `decisions.md` — each ADR is permanent; supersede with a new ADR, never edit the old one
- `research.md` — each finding is permanent; note supersession inline

**Iterative** (update in place):
- `design.md` — always describes NOW; history belongs in research.md
- `memory/*.md` — updated each session
- `Knowledge/*.md` — refine as knowledge matures; update `updated:` and `status:` in frontmatter
