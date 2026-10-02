---
name: gt-work
description: "Capture session findings into the vault at the end of a work session — append to research.md, add ADRs to decisions.md, refine design.md, create spec.md when design is complete, update PROTOCOL.md for cross-project process rules."
---

# Golden Thread Work

Write back this session's findings into the vault. Run at the end of any meaningful work session.

## Context

Use `$GT_VAULT` if it is set (a session pinned to one vault, such as the demo); otherwise read `~/.claude/vault-config.json` for the vault path. If neither gives a vault → tell user to run `/gt:gt-init`.

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

## Every write goes through the queue (Core rule 1)

Nothing in this skill edits a vault file directly. Each write below is **queued**, and the queue
is **drained once**, at the end. Since 0.17.11 the PreToolUse guard denies a direct Write/Edit (or
shell redirect) to vault content, so a direct edit is refused, not just discouraged.

For each write: put the text in a scratch file, then

```bash
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" --path <vault-relative .md> \
    --op append|replace-section|create [--section "<## heading text>"] --content-file <file> \
    --session <session id> --hint "<one line: why>"
```

- `append`: a new research entry, a runbook procedure, an INBOX line, a new task line.
  `--section` names the `##` heading to append under; without it, the text goes at the end of
  the file.
- `replace-section`: a design.md section that changed, or a README `## Tasks` block with a
  box ticked. The broker refuses it (escalates) if that section changed since you queued.
- `create`: a new file (a memory note, a spec).
- `set-property`: one frontmatter key, such as README `stage:`.

Then, once, after the last write:

```bash
python3 <base_dir>/../../scripts/gt_broker.py drain --vault "<vault>"
```

Tell the user in one line what the broker did: `N applied, N held, N escalated`.
- **Held:** another live session claims that file. The write waits in the queue, and the next
  drain applies it. Never work around a hold.
- **Escalated:** every `design.md` and `global-memory/` write, by design, and any conflicting
  replacement. It becomes a task for the owner; say so, and name the conflict file it points to.

Generated files keep their own tools and never go through the queue: `log.md` (`gt_log.py
add`), `decisions.md` (`gt_adr.py allocate` + `merge`), events (`gt_events.py emit`).

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

#### Skeptic pass (only when `skeptic_pass` is on)

`skeptic_pass` alone decides this; it is independent of `agent_specialization`, which
governs only the ingest and validation hand-off. Both settings default to `off`, and with
`skeptic_pass` off there is no skeptic pass: write the entries as above. Before the first `research.md` entry lands, ask the resolver (`<base_dir>` is the
`Base directory for this skill:` header):
```bash
python3 <base_dir>/../../scripts/gt_agent_spec.py resolve --skill gt-work --vault "<vault>"
```
`action: inline` → no pass; write as normal (show a `notice:` line in one line if there is
one). `action: spawn` → write the drafted entries, verbatim, to a file under your
scratchpad, then:
1. `gt_agent_spec.py render skeptic --input-file additions=<file> --vault "<vault>"` and
   spawn one subagent with exactly that output — nothing else from this session.
2. Record its result at the path `gt_agent_spec.py spool-path skeptic --session <id> --vault "<vault>"`
   prints, as `{"job_type": "skeptic", "session_id": ..., "created": ..., "result": <its JSON>}`,
   then run `gt_agent_spec.py check-output skeptic "<record>" --vault "<vault>"`.
3. Show the user the flags. For each, they fix the entry, keep it as written, or drop it.
   The pass is advice, never a gate: write-back completes whatever is decided, and a
   skeptic that fails to run is reported in one line, not retried.

#### Research digest (after the research entries land)

Once this session's `research.md` entries are applied (drained), regenerate the project's
digest — a cheap first read beside the append-only file:
```bash
python3 <base_dir>/../../scripts/gt_digest.py write --vault "<vault>" --project <slug>
```
It rewrites `Projects/<slug>/research-digest.md` through the write queue: the last 20 `## `
sections newest first, one line each (heading + first line, nothing inferred), plus up to 5
sections whose heading carries `[pinned]`. The first time, it also indexes the digest in
`memory/MEMORY.md`. It never touches `research.md`. A `held` or `escalated` result is reported
in one line, like any other queued write. To pin a finding, the user adds `[pinned]` to its
heading — the one edit to an existing research entry that is allowed.

### decisions.md — generated from atomically allocated ADRs

**Allocate the number before writing the ADR.** Two sessions that both read "the
highest is 5" will both write ADR-6; allocation makes that impossible. The command
prints the number it reserved, and reserving it creates the file that holds it:

```bash
python3 <vault>/Projects/golden-thread/tools/gt_adr.py --vault "<vault>" allocate <project> --title "<title>"
```

Write the body into the slot it names, then `gt_adr.py --vault "<vault>" merge <project>`. Never pick a
number by reading `decisions.md` and adding one. `allocate` emits the `adr` event
itself — do not emit another.


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

Current architecture, component relationships, data flow. Unlike the others, this file changes section by section as the design evolves: queue a `replace-section` for each section that changed. **Every design.md write is escalated to the owner for review** (owner's choice, 2026-10-01). Tell the user the design change is waiting for them; it is not applied until they accept it.

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

Session memory files (feedback.md, project-state.md, etc.) change most often. Queue a `create` for a new note and a `replace-section` (or `append`) for a change to an existing one, plus an `append` of its index line to `memory/MEMORY.md`.

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

### Two mechanical passes after the writes land (0.18.0)

Once the queue has been drained, run both. Each is read-only, prints **nothing** when it finds
nothing (say nothing then either), and honours its setting under `--work`
(`memory_contradiction_check`, `promotion_candidates`):

```bash
# memory notes you wrote or changed this session, against their same-topic neighbours
python3 <base_dir>/../../scripts/gt_memory_check.py --vault "<vault>" --project <slug> --work \
    [--changed memory/<note>.md ...]
# promotion candidates a script can see
python3 <base_dir>/../../scripts/gt_promote_detect.py --vault "<vault>" --project <slug> --work
```

- **Contradictions**: pass the memory notes you wrote with `--changed` (without it, the notes
  git reports as modified are used). Show each pair it prints — both paths, the conflicting
  sentences — and ask the question it asks: *same fact? which is current? update the older or
  link it to the newer?* Change nothing unless the user decides, and then through the queue.
- **Promotion candidates**: a memory note edited in 3 of the last 5 memory commits
  (→ research.md / decisions.md), research.md sections overlapping another project's by the
  `promotion_overlap` setting (→ Knowledge/), and global-memory notes naming a project
  (gt-lint's `global-scope-leak` → demotion). Show each with its one-line justification and
  destination; the user accepts, skips or defers each. Nothing is promoted without a yes —
  accepted ones go through `/gt:gt-promote` or `gt_optimize.py --demote`.

## Log Entry

Add the line with the tool — **never append to `log.md` by hand.** It is generated
from per-session spool files, so a hand append is lost at the next merge:

```bash
python3 <vault>/Projects/golden-thread/tools/gt_log.py --vault "<vault>" add "<the line>"
```

The line it records:
```
<YYYY-MM-DD HH:MM TZ> [work] <project-slug>[, <other-slug>...] — wrote N finding(s), M ADR(s), updated design[, created spec]
```

### Events for what this session captured

`allocate` already emitted each ADR's event, and `gt_tasks.py` emits task events from
the README. What only you know is that a finding left the conversation. Right after
writing each `research.md` entry (level 3) or new `memory/` note (level 2), run:
```bash
python3 "<vault>/Projects/golden-thread/tools/gt_events.py" --vault "<vault>" emit \
  --kind capture --item "Projects/<slug>/research.md" --to "Projects/<slug>/research.md" \
  --level-from 1 --level-to 3 --project <slug> --note "<entry title>"
```
(`--item`/`--to` the memory file and `--level-to 2` for a memory note.) A refused
event is one stderr line; the write it describes stands.

`gt_closeout.py` reads this line to date each project's last write-back, so keep its
shape: the date first (time and zone after it are optional), then `[work]`, then the
exact slug — several comma-separated when the work spans projects — then ` — `.

## Ask whether anything is leaving with you

You have just classified every finding and written the ones that belong in a file. Some
will not have made it: a decision that needs the user, something mid-flight, a question
raised and never answered. Those live only in this conversation, and this conversation is
about to end.

`gt_handoff.py` exists for exactly that and is reached by one caller — the user typing
`/gt:gt-handoff`. Which means it is offered at the moment a person happens to think of it,
rather than the moment it is needed.

**So state the list, then ask.** As you write the sections above, keep the items you
identified and did *not* write, each with the reason. At the end:

- **The list is empty** — say so in one line and stop. Do not ask. An offer made every
  session is one that is always declined, which is how `gt_closeout` taught the same lesson:
  it asked every session until the answer stopped being read. Nothing uncaptured, no question.
- **The list is not empty** — name the items, then ask:

  > "These did not reach a file: <item>, <item>. Write a handoff so the next session has
  > them? **yes** / **no**"

**Only on yes:**

```bash
python3 <scripts>/gt_handoff.py --vault "<vault>" --project <slug> [--repo <path>]
```

It gathers the facts and labels each one `verified` / `unverified` / `unknown`. **You write
the narrative** — what was decided, why, and what to do next. The script deliberately will
not: a document that reads finished when it is not hands the next session false confidence,
which is worse than handing it none. Put the uncaptured items in that narrative; they are
the reason the file exists.

An existing handoff is **refused, not overwritten** — it is someone's record of a session.
If one exists for today, say so and ask before passing `--force`.

### Then make sure it gets picked up

**A handoff nobody is told to read is write-only.** The file is not the mechanism; the task
is. So after writing one, add **one task per unresolved item** to the project's `## Tasks` in
`README.md`:

```markdown
- [ ] <the unresolved thing, as a decision or an action> — context in <handoff file> [p:: 1] [waiting:: user] [since:: YYYY-MM-DD]
```

Three things about that line are deliberate:

- **`p:: 1`, not `p:: 0`.** There is no `p:: 0`. Task priority is `1` urgent, `2` normal,
  `3` someday, `7`+ shelved; `0` exists only at PROJECT level (`pp:`), where
  `Projects/CONVENTIONS.md` defines it as *"active harm accruing right now — nothing sits here
  permanently."* A handoff task parked there for ever would make that tier a place things live.
- **`since:: <today>` is what makes it escalate.** The stale-P1 rule raises the project one
  level once a `p:: 1` is seven days old, computed against the clock. Read the handoff tomorrow
  and nothing happens; ignore it for a week and the project climbs on its own. That is stronger
  than a fixed top priority, because it decays correctly and needs no cleanup.
- **One task per item, naming the item.** Never a single "review the handoff" line: that
  competes with real work while saying nothing, and if five things are hanging it hides four of
  them. The handoff is the context; the task is the thing to do.

`waiting:: user` puts them on the human's list, which is correct — these are the items that
needed a person, which is why they did not reach a file.

**The next session is told, without anyone asking** (since 0.17.2). `gt_handoff.py` writes
`status: open`, and an open handoff is shown — one line, never its body — at every session start
or when its project is opened (setting `handoff_surface`) until it is handled or deferred to a
date through `/gt:gt-handoff-handle`. It also counts as handled once every task citing it is
checked off, so **the task must name the handoff's filename** — that is how the two are joined.

**Never write one on the user's behalf after a no.** They have just told you these items
are not worth a file, and that is theirs to decide.

## Ask whether the project is finished

Closing a project is a decision nobody makes unless asked. Two moments call for it:

1. **You just checked off a task** and it was the last open one at `p:: 2` or better,
   or the last task with a due date.
2. **The close-out probe names this project.** Run it — it is cheap:
   ```bash
   python3 <vault>/Projects/golden-thread/tools/gt_closeout.py --vault "<vault>" signals <slug>
   ```
   If any rule fires (`R1` most tasks past due, `R2` most tasks done and nothing
   urgent, `R3` three quiet weeks, `R4` nothing open), record that you are asking and
   then ask:
   ```bash
   python3 <vault>/Projects/golden-thread/tools/gt_closeout.py --vault "<vault>" ask <slug> gt-work
   ```
   > "`<slug>` looks finished: <reasons>. Close it? **yes** / **no** / **later** — and why?"

Record the answer, whatever it is; the record is how the thresholds get tuned to the
user's own pattern rather than a guess:
```bash
python3 <vault>/Projects/golden-thread/tools/gt_closeout.py --vault "<vault>" answer <slug> yes|no|later "<their words>"
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
the project's reality, update the property too, not just the prose, by queuing a
`set-property` (`--key stage --value active`), and the prose with a `replace-section`:

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

## Learn: patterns worth keeping (0.18.0)

Run this step **after Promotion Candidates and before the Log Entry** — it is where a separate
`gt-learn` would have gone, folded in here so there is one capture path, not two (owner,
2026-10-01). The findings above are *what happened*; this looks for *how the work was done* that
should be done the same way next time.

Scan this session's conversation for:

- **A pattern applied more than once** — the same fix, check, command sequence or workaround used
  twice or more.
- **A decision that is not yet written down** — made in conversation, absent from the project's
  `decisions.md` (search it first).
- **A reusable technique** — something that could become a convention, a runbook step, or a
  skill.

**Quality gate before showing anything:** skip a candidate that is already captured (search the
project's `decisions.md`, `research.md`, `runbook.md` and `memory/`, and the wiki index), that
happened once with no reason to expect a repeat, or that is only true of today's state. Fewer,
real candidates beat a long list the user learns to wave through.

Present the survivors **one at a time**, one line each, and ask where it goes:

| answer | where | how |
|---|---|---|
| **project convention** | an ADR in `Projects/<slug>/decisions.md` | allocate with `gt_adr.py`, then the body through the write queue — exactly as *decisions.md* above |
| **runbook step** | `Projects/<slug>/runbook.md` | a queued `append` (*Every write goes through the queue* above) |
| **memory note** | `Projects/<slug>/memory/<name>.md` + its `MEMORY.md` line | queued `create` and `append` |
| **beyond this project** | `Knowledge/` or `global-memory/` | `/gt:gt-promote` — not written from here |
| **skip** | nowhere | — |

Every save goes through `gt_write_queue.py` + `gt_broker.py drain` (or `gt_adr.py` for an ADR) —
never Write/Edit on a vault path (Core rule 1). Count what was saved in the Log Entry line
(`… , L pattern(s) kept`). Nothing found is a fine result; say so in one line and move on.

## ADR fields and memory entities (0.18.0)

**When allocating an ADR, ask two questions** and pass the answers as flags — the fields are
what `gt-lint`, `gt-brief` and `gt-query`'s lineage mode read:

- *Does this replace an earlier decision?* → `--supersedes <N>` (repeatable). Writes
  `- **Supersedes**: ADR-N`. Never edit the old ADR; this link is how the chain is traced.
- *Does this decision have a known expiry condition?* ("until the SDK is upgraded", "while we
  are under 10k requests/day") → `--expires-when "<condition>"`; a date → `--expires YYYY-MM-DD`.
  Lint surfaces a condition in the review queue at any age and a date once it has passed, and
  `gt-brief` keeps an expiring decision out of the repo's stable constraints.

```bash
python3 <vault>/Projects/golden-thread/tools/gt_adr.py --vault "<vault>" allocate <project> \
    --title "<title>" [--supersedes N] [--expires-when "<condition>"] [--expires YYYY-MM-DD]
```

Both are optional; an ADR with neither is unchanged. Written by hand into a body, the lines
`- **Supersedes**: ADR-N`, `- **Expires when**: <prose>` and `- **Expires**: YYYY-MM-DD` (or
`expires_when:` in the Knowledge-page spelling) read the same.

**When creating a memory file, ask:** *What entities (services, hosts, components) does this
memory file cover? List them to enable entity-based lookup.* They go in its frontmatter so
`/gt:gt-query --entity <name>` finds it:

```yaml
entities:
  - auth-service
  - TOKENSVC
```

Optional — skip it when the note is not about a nameable thing. Never add entities the user
did not confirm.
