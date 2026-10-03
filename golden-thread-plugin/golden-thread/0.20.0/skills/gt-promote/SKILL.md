---
name: gt-promote
description: "Graduate a fact, finding, or idea up the knowledge hierarchy: project memory → decisions/research → Knowledge wiki page → global-memory."
model_intent: deep
---

# Golden Thread Promote

Move knowledge up the hierarchy so it's available in the right scope.

## The Hierarchy

```
session conversation
      ↓ graduate
project memory (memory/*.md)
      ↓ graduate
project files (decisions.md / research.md / design.md)
      ↓ graduate
Knowledge/ wiki page (applies beyond this project)
      ↓ graduate
global-memory/ (loaded in every session, all projects)
```

An idea that doesn't fit in any existing project can become a new project scaffold.

## Steps

**Step 1 — Identify what to promote**

Ask: "What would you like to promote? You can describe it, paste the content, or give me a filename."

Also accept: "I want to review promotion candidates" → scan recent entries in research.md and decisions.md for items that have `→ promote` or `candidate` notes, plus any `status: seed` Knowledge pages that might be ready to graduate to `growing`.

**Step 2 — Determine destination**

Ask (if not obvious from context):
1. "Does this apply only to `<project-slug>`, or to other projects too?"
   - Project-only → goes into decisions.md or research.md (if not already there)
   - Cross-project → goes into `Knowledge/` as a wiki page
2. "Is this something every Claude Code session should know about, regardless of project?"
   - Yes → goes into `global-memory/`

**Step 3 — Execute**

Vault content is written only through the write queue (Core rule 1), never with Write, Edit or a shell redirect. Write each piece of content to a scratch file outside the vault and queue it — `append` adds to the end of a `## ` section (or the file), `create` makes a new page, `set-property` changes one top-level frontmatter key, `replace-file` replaces a whole file you have read (for a nested `metadata:` key, or anything above the first `## `):

```bash
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
  --path "<vault-relative .md>" --op append [--section "<heading>"] \
  --content-file <scratch>/<item>.md --hint "promote: <source> → <destination>"
```

When the move's writes are all queued, apply them once:

```bash
python3 <base_dir>/../../scripts/gt_broker.py drain --vault "<vault>"
```

and tell the user in one line what the broker applied, held (another live session has the file; the next drain applies it) or escalated to the owner as a task.

**memory/*.md → decisions.md or research.md**
- If it's a stable rule/constraint → an ADR: `decisions.md` is generated, so allocate it with `python3 "<vault>/Projects/golden-thread/tools/gt_adr.py" --vault "<vault>" allocate <slug> --title "<title>"`, write the body into the slot it names, then `gt_adr.py --vault "<vault>" merge <slug>`
- If it's a finding/gotcha → queue a dated entry, `--path "Projects/<slug>/research.md" --op append`
- Log: `relocate`

**decisions/research → Knowledge/<page>.md**
- Queue the page as a new file, `--path "Knowledge/<page>.md" --op create`, with proper frontmatter:
  ```yaml
  ---
  title: <descriptive title>
  category: <runbook|decision|reference|concept>
  tags: [<relevant tags>]
  sources: []
  created: <today>
  updated: <today>
  status: seed
  ---
  ```
- Queue a one-line entry to `index.md` under the appropriate category heading (`--op append --section "<category>"`)
- Queue `[[<page title>]]` cross-links into any related Knowledge pages (`--op append`)
- Log: `graduate`

**Knowledge/<page>.md → global-memory/**
- **Global-scope check — ask before writing:**
  > "This will be loaded in EVERY session for EVERY project. Does it contain zero project-specific facts — no project slugs, no service URLs specific to one project, no team-specific process? And has it proven useful in at least 2 unrelated projects?"
  - If no → keep in `Knowledge/` and suppress the promotion
- Keep the file under 30 lines. If more is needed, the detail belongs in a `Knowledge/` page that `global-memory/` points to.
- Queue `--path "global-memory/<slug>.md" --op create`
- Queue its entry, `--path "global-memory/MEMORY.md" --op append`
- The broker does not apply either: every `global-memory/` write goes to the owner for review as a task, which is intended. The promotion is done when the owner accepts it
- Log: `graduate`

**Any level → new project scaffold**
- If the item is an idea for a separate project, run:
  ```bash
  python3 <base_dir>/../../scripts/vault_init.py create-project --vault "<vault>" --name "<new-slug>"
  ```
- Queue the idea into the new project's `idea.md` (`--op append`; the script already wrote its heading)
- Add to `<vault>/Projects/README.md` (the script registers it)
- Log: `graduate`

**Retiring (removing from active use)**
- When a page is superseded or no longer accurate: queue `--op set-property --key status --value stale` for it
- Never delete — just mark stale and note what superseded it (`--op append`)
- Log: `retire`

## Log Entry and event

Record every move **at the moment the destination write is queued**, with one command
that spools the `log.md` line and the structured event together. Never append to
`log.md` by hand and never write `events.jsonl` yourself — both are generated:

```bash
python3 "<vault>/Projects/golden-thread/tools/gt_log.py" --vault "<vault>" add \
  "<today> [graduate] <source> → <destination>: <one-line description>" \
  --event promote --item "<destination>" --from "<source>" --to "<destination>" \
  --level-from <L> --level-to <L> --project <slug>
```

| Move | log verb | `--event` | levels |
|---|---|---|---|
| memory → decisions/research | `relocate` | `promote` | `--level-from 2 --level-to 3` |
| decisions/research → Knowledge | `graduate` | `promote` | `--level-from 3 --level-to 4` |
| Knowledge → global-memory | `graduate` | `promote` | `--level-from 4 --level-to 5` |
| anything → Core | `graduate` | `promote` | `--level-from <L>`, no `--level-to`; `--note "to Core: <rule>"` |
| reclassified sideways or down | `relocate` | `relocate` | from/to levels as they are |
| page marked stale | `retire` | `retire` | `--item Knowledge/<page>.md`, no `--to` |

Levels are the ladder: 2 `memory/`, 3 `decisions.md`/`research.md`/`design.md`,
4 `Knowledge/`, 5 `global-memory/`. Paths are vault-relative. Omit `--project` for a
move that belongs to no project. A new project scaffold needs no event from you:
`vault_init.py create-project --vault "<vault>"` emits `create` itself. If the event is refused the log
line is still written — read the stderr line, fix the flag, and run
`gt_events.py --vault "<vault>" emit --kind ...` for the event alone.

The log line shapes:
```
<today> [graduate] <source> → <destination>: <one-line description>
<today> [retire] Knowledge/<page>.md: superseded by <new-page>
<today> [relocate] <source> → <dest>: reclassified
```

---

## Promoting to Core (the top tier)

`core-rules/` is the top of the hierarchy, above
`global-memory/`. Promote here only when a rule must hold on **every turn, in every
project**.

There are **two ways in**, and only one is gated.

**Path 1 — Designation (primary).** The user names the rule Core; it is Core from that
moment. No test, and **no prior incident is required or wanted** — a rule that must be
immutable is Core as soon as that is known. Requiring it to fail first means accepting
the failure, which defeats the one tier whose purpose is that it never breaks.

*Canary rules are designated too.* A canary is kept because its absence is **visible**,
not because it is **costly** — small, cheap, and present on every reply, so the moment
it stops appearing you know enforcement itself has broken. Every other Core rule fails
silently; a canary fails loudly. `core_timestamp_every_message` is the canonical one.
**Never demote a canary for failing the gate below** — it was never meant to take it.

**Path 2 — Promotion from levels 1–5 (gated).** When an existing item moves up, answer
all three, each asked as *if this rule were **not** enforced*:

1. **Correctness** — would it cause a misunderstanding that leads to code written
   incorrectly, or a change implemented wrongly, not at all, or in a way not allowed?
2. **Cost** — would it cause more work, or force backing out an implemented solution?
3. **Cascade** — would it cause a cascade in which rules at the lower five levels are
   misrepresented, or written such that they cannot or should not be followed?

**Any single YES qualifies.** Three NOs means it stays where it is. Record the three
answers in the rule file's body so the tier is justified rather than asserted.

Observed drift is **not** an entry requirement — a rule that drifts was mis-tiered.
Importance alone is not the test either: a critical trading constraint that only
matters inside a backtest is Context, not Core.

### Promotion is not a move — it is a wiring job

Moving the file is the easy part and, on its own, achieves nothing. The 2026-08-16
incident had the timestamp rule sitting in `global-memory` while silently not being
applied for dozens of turns. **A Core rule that is only stored is not Core.**

Steps, in order:

1. **Choose the enforcement**, and be honest about it:
   - `validated` — the rule is mechanically checkable (a required prefix, a forbidden
     token, a required file list). This is the only unbreakable form.
   - `reminder` — a judgement call that can only be re-asserted, not checked.
   Prefer `validated` for anything cheap to check.
2. **Set the frontmatter:**
   ```yaml
   metadata:
     type: core
     level: core
     enforcement: validated | reminder
     promoted: <today>
     supersedes: <old-filename-if-any>
   ```
3. **Move the file** to `core-rules/`, renamed `core_<topic>.md`. `core-rules/` is
   outside the write queue, so write the new file there directly. The old path is vault
   content: queue the one-line pointer that replaces it (step 5) as `--op replace-file`.
4. **Wire or confirm the mechanism.** The hooks live at
   `~/.claude/golden-thread/hooks/` — **outside the vault**, so the absolute path in
   `settings.json` survives project renames and vault moves.
   - `reminder` → **nothing to edit.** `inject_core_rules.sh` reads the rule files at
     run time, so a correctly-placed rule is picked up automatically. Its injected
     text comes from the rule's imperative, which is why the rule body must be phrased
     as a command ("Begin your reply with…"), never a description. **Do not copy rule
     text into the script** — duplicating it is how the two copies drift apart.
   - `validated` → add a check to `validate_response.sh` that inspects the finished
     reply and blocks on violation. Keep it cheap and unambiguous.
   - Confirm both hooks are wired in `~/.claude/settings.json` (user-global = Core).
     If not: `vault_init.py install-core-rules --vault <vault>`.
   - **Verify it actually fires** — an unverified Core rule is an assumption:
     ```bash
     echo '{}' | ~/.claude/golden-thread/hooks/inject_core_rules.sh
     ```
     The new rule must appear in the output.
5. **Update the pointer** in `global-memory/MEMORY.md` — it points at `core-rules/`,
   it does not hold Core rules inline. Queue it (`--op replace-section` or `append`);
   as a `global-memory/` write it goes to the owner for review. Leave a one-line note where the old file was
   (`--op replace-file` on the old path; under `global-memory/` that also goes to the owner).
6. **Verify** with `/gt:gt-lint` — `core-unenforced` must not fire for the new rule.
   That check exists precisely to catch step 4 being skipped.
7. **Log** with the `graduate` verb and `--event promote`, as in *Log Entry and event*
   above (no `--level-to`; `--note "to Core: core_<topic>"`).

> **Writing a validator is a safety-critical act.** A `Stop` hook that blocks wrongly
> makes every session unusable. Fail open on any parse failure, honour
> `stop_hook_active` so a block can never loop, and test the allow cases before the
> block case.

### Demotion (Core → generic)

When a rule proves situational or is superseded, reverse it in this order:
**remove the enforcement first**, then move the file. Leaving a hook behind that
enforces a rule no longer in `core-rules/` is worse than either state.

1. Remove any `validated` check for it from
   `~/.claude/golden-thread/hooks/validate_response.sh`. For a `reminder`-tier rule
   there is nothing to unwire in the script — the injector reads `core-rules/` at run
   time, so the rule stops being injected the moment step 3 moves the file out.
2. Set `level: generic` and drop `enforcement` (directly — the file is still in
   `core-rules/`, which is outside the queue).
3. Move to `global-memory/` (cross-project) or the owning project's `memory/`: queue
   the whole file at its destination with `--op create` (a `global-memory/` destination
   goes to the owner for review). Removing it from `core-rules/` is a deletion, which is
   not a queue op; delete it directly only once the destination exists.
4. Re-index `MEMORY.md` through the queue (a `global-memory/` entry goes to the owner for review) and log with `retire` or `relocate` — with `--event relocate`
   (`--to` the new file, `--level-to` its level) or `--event retire`.

**Keep the Core tier small.** A bloated always-on tier dilutes attention on every rule
in it — which is the failure mode Core exists to prevent.

---

## Project lifecycle: rename, merge, archive

Projects get redefined, combined and retired. These are supported operations, not
manual sweeps — a half-finished rename leaves links pointing at nothing.

```bash
vault_init.py rename-project  --vault <v> --from <old> --to <new>
vault_init.py merge-project   --vault <v> --from <slug> --into <slug>
vault_init.py archive-project --vault <v> --slug <slug> --reason "<why>"
```

Each emits its own `rename` / `merge` / `archive` event when it succeeds (never under
`--dry-run`). Do not emit a second one by hand.

### Nothing is ever deleted

`archived` is the **stage** (`CONVENTIONS.md`: *"Retired or replaced"*); `retire` is
the **log verb** for `log.md`. Archiving keeps every note, decision and link intact —
it changes the project's status, not its contents.

A merge leaves the source as a **tombstone**: a README recording where it went. Notes
and links written before the merge still lead somewhere.

### What merge does and does not do

| Content | Handling |
|---|---|
| `memory/*.md` | Moved, filenames preserved so `[[wikilinks]]` keep resolving. A clash becomes `<name>__from_<slug>.md` and is flagged |
| `idea.md` | **Immutable — never concatenated.** Preserved verbatim as `<dst>/memory/idea_<src>.md` |
| `research.md` | Appended (dated and append-only, so interleaving is safe) |
| `decisions.md` | Appended with ADR ids **renumbered** to avoid collision; the original id is kept in the heading |
| `runbook.md`, `spec.md` | Appended |
| `design.md`, `source.md` | **Appended under a NEEDS REVIEW banner, not merged.** Two architectures or two topologies cannot be combined mechanically — a silent concatenation would describe neither system |
| frontmatter (`domain`, `tags`) | Destination's kept; parked in `review-queue.md` for you to confirm |
| open `- [ ]` tasks | Added to the end of the destination's `## Tasks`, so the rollup keeps counting them; a review item asks you to re-prioritise |
| everything else — the old README (as `README.pre-merge.md`), `CLAUDE.md`, the memory index, unrecognised files and folders | **Moved untouched** into `<dst>/merged-<src>/`. Merge deletes nothing it has not already appended |

Everything requiring judgement lands in `review-queue.md`. Work through it before
calling the merge done, and re-run `/gt:gt-lint` — expect `memory-unlisted` until
`MEMORY.md` is tidied, and `project-missing` if anything still points at the old slug.

## Promote as a staged pipeline (reviewing several candidates)

When the user asks to review promotion candidates — several at once, as in Step 1's "review promotion candidates" — run them through the staged pipeline instead of one pass that does everything. **Promotions stay human-approved:** the pipeline ends in a proposal, and nothing is queued until the owner says yes to it.

| Stage | Who runs it | Parallel |
|---|---|---|
| scan | this session collects the candidates; `gt_ingest_pipeline.py promote-scan` records them | one |
| verify | a zero-context Claude subagent per candidate, spec `verify` (the old `validate`) | **per candidate** |
| generalize | a subagent per confirmed candidate, spec `generalize` | per candidate |
| place | a subagent per generalized candidate, spec `place` (overlap, links, index entry) | per candidate |
| owner approves | the owner, here | — |

Every stage reads only the previous stage's packet in `<vault>/Projects/golden-thread/spool/pipeline/<run>/`; every agent stage is a Claude subagent spawned here, never gt-farm or any non-Claude service. The tool is `<base_dir>/../../scripts/gt_ingest_pipeline.py` (`<tool>`); every command takes `--vault "<vault>"` and `--dry-run`. The specs render only when `agent_specialization` is on (`gt_agent_spec.py resolve --skill gt-promote`); with it off, do each stage yourself in the same order — the verify stage then becomes a `/gt:gt-validate` run per candidate — and still stop at the owner.

**Which agent (every spawned agent).** The task sets the model and effort, not the session: before spawning a job type, run `python3 <base_dir>/../../scripts/gt_agent_spec.py model <job type> --vault "<vault>"` and say its lines once per job type. If it names `agent type: gt:<stage>` **and** your Agent tool offers that type, spawn with `subagent_type: "gt:<stage>"` and **no** `model` — the definition carries the model and effort (verify and reconcile also start without the CLAUDE.md files). Otherwise (`agent type: none`, or a Claude Code that does not offer the type) spawn as before, passing the alias on its first line as the Agent tool's `model` (`session` means pass none). Never pick a model name yourself. The prompt is exactly what `render` printed, on either route.

**Workflow route (a Claude Code with workflows).** When your tools include the Workflow tool and `python3 <base_dir>/../../scripts/gt_agent_spec.py features` does not show workflows off, a stage with several units may run as the `gt:pipeline-stage` workflow instead of one Agent call per unit. `<tool> workflow-args <run> --stage <stage> --json` prints its args: for extract it renders every clean unit itself, through the intake scan (promote has no extract stage); every other stage takes `--prompt <unit>=<file>` per unit, each file the output of `render`. Run the workflow with those args, verbatim; each agent's output is checked against the stage schema as it returns. Save the JSON it returns to a scratch file and run `<tool> packets <run> --stage <stage> --results-file <file>`, which checks every result again and writes the packets; exit codes and stops are those of `packet`. A unit that came back empty is listed `INCOMPLETE`: run `workflow-args` again and it hands out only the units still without a packet. A workflow cannot ask anything, so every stop is still handled here, after it returns. Without the Workflow tool, the per-agent steps below are the same pipeline.

1. **Scan.** Write the candidates as a JSON list `[{"claim", "origin": "<vault file › section>", "evidence", "level": "knowledge|global_memory"}]` to a scratch file; `<tool> promote-scan --candidates-file <file> --vault "<vault>"` prints the run id and the candidate ids (`c01`, …).
2. **Verify, in parallel.** Per candidate: `gt_agent_spec.py render verify --input-file claim=<f> --input-file rules=<f> --input-file artifact=<f> --vault "<vault>"` — it loads no vault context, and you must add none. Spawn all of them in one message. Save each result and run `<tool> packet <run> --stage verify --unit <id> --result-file <file>`. A `refuted` or `cannot-verify` candidate is dropped from the run, with its reason shown.
3. **Generalize.** Per confirmed candidate: `render generalize --input-file claim=<f> --input-file evidence=<the verify packet's derivation and evidence>`; `packet <run> --stage generalize --unit <id> …`. `generalizes: false` drops it — it stays where it is.
4. **Place.** Per generalized candidate: `render place --input-file statement=<f> --input level=<level>`; `packet <run> --stage place --unit <id> …`.
5. **Owner approves.** `<tool> promote-plan <run>` folds the packets into proposals (from → target, action, the general statement, overlaps, links, index entry) and ends `OWNER APPROVAL REQUIRED -- nothing has been written`. There is no command that applies a plan. Show each proposal and ask; for each one the owner approves, do Step 3's writes for that move, then its log entry and event (*Log Entry and event*, above). A proposal the owner declines is simply not written.

