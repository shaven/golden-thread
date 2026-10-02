# Golden Thread Plugin

> **Reader:** someone who has installed it and wants the reference
> **Claims last checked against the code:** 2026-10-01 (gt 0.18.0) — see *The documents, and what belongs in each* in [`CLAUDE.md`](../CLAUDE.md).

A Claude Code plugin that turns an Obsidian vault into the single source of truth for all AI memory across every project and every session.


> [!IMPORTANT]
> **0.18.0 has ten themes**; what changed and why is in the CHANGELOG, how to use each in the
> MANUAL. **One verb per action:** `gt-create`, `gt-open`, `gt-list`, `gt-handle` and the new
> `gt-close` take the artifact (project, task, handoff) as their argument; the six old task and
> handoff skills still work through 0.18.x as deprecated aliases. **A coding loop:** `gt-plan`
> writes a phased plan and waits for approval; `gt-implement` runs it test-first and stops on red.
> **Decisions you can trace:** ADR expiry and supersession fields, `gt_adr.py lineage`, and
> `gt-brief` to draft a repo's `CLAUDE.md`. **Subtraction:** `gt-optimize` gains a `session` member
> (prompt-cache cost), `--cost`, `--archive` and `--supersede`; `gt-minimize` prunes a session
> before you cut it. **Write-back that checks itself:** contradictions, promotion candidates and a
> research digest. **Returning after time away:** a catch-up brief in `gt-open` and *What Changed
> This Session* in the handoff. **Install health and guards:** doctor rows `repo-target` and
> `hooks-schema`, and a guard that refuses commits in another machine's checkout. **Checks modules
> contribute:** `gt_check.py` hosts module checkers (`cannot-check` never passes) and `gt_apply.py`
> is the only writer of their fixes. **Model intent:** skills declare `fast`/`balanced`/`deep`,
> resolved by `gt_model.py`. **Across machines and outside sessions:** `gt-sync`, and reminders by
> macOS notification, SMS/Discord or email (each off by default). Scans and ingests **resume** after
> an interruption, from any session. After installing, restart and run `/gt:gt-upgrade`.
>
> Earlier: **0.17.11** made Core rule 1 queue-first (every vault write goes through the write
> queue) and added the optional `gt-lotr` module; **0.17.2** added surfacing at session start and
> the task and handoff skills. Every release is in the [CHANGELOG](../CHANGELOG.md).

> [!NOTE]
> **0.16.2: the Core rules are re-asserted after a compaction.** Hook-added context is
> *summarised* during a compaction rather than re-injected from disk, so
> `inject_core_rules.sh` is now registered on `SessionStart` with matcher `compact` as well
> as on `UserPromptSubmit`. Scope it honestly: what was exposed was the **remainder of the
> turn** in which an auto-compaction fired — `UserPromptSubmit` has no compaction exception,
> so the next user prompt always restored the rules verbatim — and the **mechanical tier was
> never affected**, because the `PreToolUse` guards and the `Stop` validator are
> event-driven commands, not context. The full registration table is under *Install*.
>
> Also in 0.16.2:
>
> - **A module hook can declare a `timeout`** — whole seconds, 1 to 600. `SessionEnd` hooks
>   share a **1.5-second budget**, and a hook that runs over is cancelled **with its output
>   discarded**, which looks exactly like a hook that had nothing to say. The report card
>   declares 15 on `SessionEnd`.
> - **The pack-submission licence check knows the current SPDX spellings.**
>   `GPL-3.0-or-later` — the identifier any modern licence scanner emits — is now refused
>   **as copyleft**, with that reason, instead of as an unrecognised identifier.
> - **`/gt:gt-context`'s envelope is documented as *not* a security control.** It provides
>   identifiability, not protection — see *Packs and the registry* below.

## What It Does

Instead of scattered `.claude/memory/` files and CLAUDE.md snippets that live and die per-session, Golden Thread gives every fact a permanent home in a structured vault. Knowledge flows up a hierarchy from session notes into the vault, and the right facts are always in scope when you need them.

```
session conversation
      ↓  gt-work / gt-promote
project memory  (memory/*.md)
      ↓  gt-promote
project files   (research.md / decisions.md / design.md / spec.md / runbook.md)
      ↓  gt-promote
Knowledge/      (cross-project wiki pages, backed by immutable Sources/)
      ↓  gt-promote
global-memory/  (loaded in every session, all projects)
```

Facts move up the hierarchy as they prove themselves general. They never move back down, and they are never silently deleted.

---

## Thirty-Six Skills (plus modules)

### Setup

| Command | What it does |
|---|---|
| `/gt:gt-init` | Create a new vault from scratch, or wire an existing vault to a project. Idempotent — safe to re-run. |
| `/gt:gt-create` | The create verb: `project <slug>` (or a bare slug) scaffolds a project and captures your brain dump into `idea.md` — sub-projects, tags, runbook; `task <text>` adds a task line; `handoff` writes a handoff. Since 0.18.0 a sub-project's `CLAUDE.md` names its real folder. |

### Daily Work

| Command | What it does |
|---|---|
| `/gt:gt-open` | Load a project at the start of a session. Reads all project docs in order (idea → research → decisions → design → spec → runbook → memory), then summarizes the project state and asks where to pick up. Names that project's waiting handoffs without reading them. Since 0.18.0: leads with a generated catch-up brief after `brief_absence_days` away (`--brief` / `--no-brief`, `gt_catchup.py`), reads `research-digest.md` when it is current, says which repo a repo-scoped command will hit, and opens one handoff or task (`handoff [id]`, `task <id>`). |
| `/gt:gt-route` | Mid-session check: what has this session actually become, where does its output belong, and is it happening in the right project, harness and model? Serves the middle of a session, where `gt-open` cannot see yet and `gt-work` sees too late. Cheap and repeatable — not a gate. |
| `/gt:gt-work` | Write back session findings at the end of a session. Appends to `research.md`, adds ADRs to `decisions.md`, refines `design.md`, creates `spec.md` when design is complete, and flags content for PROTOCOL.md. Offers a handoff for what did not reach a file — which the next session is now shown at start. Since 0.18.0 it asks about ADR supersession and expiry and memory entities, regenerates `research-digest.md` (`gt_digest.py`), flags contradicting memory notes (`gt_memory_check.py`) and promotion candidates (`gt_promote_detect.py`), and ends with a *Learn* step for reusable patterns. |
| `/gt:gt-ingest` | Bulk-import an existing project's memory files, CLAUDE.md rules, and notes into the vault. External sources are stored immutably in `Sources/` before being synthesized into Knowledge pages. Since 0.18.0 an interrupted ingest resumes from any session (`gt_checkpoint.py`). |
| `/gt:gt-review` | Empty the inbox. Reads `<vault>/INBOX.md` first — the capture point any session drops a line into — and then Obsidian daily notes, but only if the vault is configured for them. Routes each captured item into a tracked project. |
| `/gt:gt-sync` | Keep the vault in step with its git remote across machines (0.18.0, `gt_sync.py`): `status` (ahead, behind, uncommitted), `pull` (fast-forward only — stops on divergence, never merges), `push` (only after the push check passes, and refused while behind). Setting `sync_check` adds a "vault is behind" line at session start. |

### Coding Work

| Command | What it does |
|---|---|
| `/gt:gt-plan` | Plan before any code: restate the requirement and confirm it, read the design, spec and ADRs it touches, list risks marked verified or unverified, and write a phased, test-first plan to `.claude/gt-plan-current.md` in the code repo. Ends with "Approve this plan?" and writes no code. |
| `/gt:gt-implement` | Run the approved plan one phase at a time: the failing test first, then the code, then `gt_allin.py --only tests,scan` naming the tests that ran. Stops on any failure or any check that could not run; commits only through `gt-allin-commit` and only on your yes; never pushes. Refuses without an approved plan. |

### Context

| Command | What it does |
|---|---|
| `/gt:gt-minimize` | Prune a heavy session before cutting it: measure its billed context and how long its prompt cache stays warm (`gt_minimize.py`), keep the few things worth keeping (to `Knowledge/` or `INBOX.md`), drop the rest, and say "/compact or /clear now". Writes no in-flight note — anything mid-flight goes to `/gt:gt-create handoff`. |

### Tasks and Handoffs

One skill per verb, the artifact as its argument (0.18.0). Seeing never loads a project, and
nothing is deleted.

| Command | What it does |
|---|---|
| `/gt:gt-create task` · `/gt:gt-create handoff` | Create a task the way a developer drops a TODO into code — one well-formed line in the project README's `## Tasks`, written by the vault tool `gt_task.py`, optionally tied to a `[[page]]`, wiki page or source with `[ref:: …]` (which must resolve). Or write a handoff: facts labelled by source and verification state, *What Changed This Session* from the vault's git history, and a narrative left for the person who did the work. |
| `/gt:gt-open task <id>` · `/gt:gt-open handoff [id]` | Read-only: one task's full detail, or one handoff plus only the task lines citing it. |
| `/gt:gt-list` | Read-only: `tasks [filter]` (`<slug>`, `p1`/`p2`/`p3`, `mine`, `overdue`, `stale`, `deferred`, `ref:<text>`), `handoffs` (open, or whose deferral date has come), or bare for a summary of both. |
| `/gt:gt-handle` | Work through a set one at a time. Tasks: close, drop (reason required), defer to a future date, move to another project, or keep; IDs are refused if the line changed since it was listed. Handoffs: settle each item, then close as handled or defer to a date. Bare, it asks which. |
| `/gt:gt-close` | Close a **project** (every open task and handoff decided — close, drop, move, or keep shelved at `p:: 7` — then graduation offered through gt-promote, then archived in place; `--move` relocates it to `Archive/`), a **task**, or a **handoff** (refused while a citing task is open). `gt_close.py`. |
| `/gt:gt-task` · `/gt:gt-handoff` · `/gt:gt-task-list` · `/gt:gt-handoff-list` · `/gt:gt-task-handle` · `/gt:gt-handoff-handle` | **Deprecated aliases**, working through 0.18.x: each says which verb replaced it and then does exactly what that verb does. |

### Knowledge Management

| Command | What it does |
|---|---|
| `/gt:gt-query` | Look something up in the vault — reads index.md first, follows wikilinks, falls back to grep. The vault is always checked before the web. Since 0.18.0: `--lineage <topic>` traces how a decision came to be (`gt_adr.py lineage`), and `--entity <name>` loads only the memory notes declaring it (`gt_entities.py`). |
| `/gt:gt-promote` | Graduate a fact, finding, or idea up the hierarchy: project memory → project files → Knowledge wiki page → global-memory. Also handles new project scaffolding and retiring stale content. |
| `/gt:gt-brief` | Draft a self-contained `CLAUDE.md` section for a project's code repo from the vault — what it is, the standing decisions, where it runs, what not to do — printed for review, never written (`gt_brief.py`). On your yes the skill places it through the write queue. |
| `/gt:gt-refresh` | Check `Sources/` for upstream changes. Supersedes outdated sources with new immutable files — never edits the old one. Updates Knowledge pages that cited the changed source. |

### Context & Verification

| Command | What it does |
|---|---|
| `/gt:gt-validate` | Verify a claim by re-deriving it with a fresh-context validator — never by reviewing the reasoning that produced it. Use before recording a finding as fact or before a production change. Since 0.18.0 it asks `gt_check.py list --for` first, so an installed checker settles a mechanical claim, and it declares `model_intent: deep` (`gt_model.py`). |

### Maintenance

| Command | What it does |
|---|---|
| `/gt:gt-upgrade` | Bring an existing vault up to the installed release: run the migrations it has not had, take the release's changes into `PROTOCOL.md` and `CONVENTIONS.md` without losing local edits, add newly shipped Core rules, stamp the vault. Rehearse with `--dry-run`; it refuses a dirty tree and backs up before applying. |
| `/gt:gt-doctor` | One report for the whole install: plugin version, component drift, hook wiring, the vault's release stamp and pending migrations, the scheduled jobs' last exits, stray workers, unpushed commits, publish-destination drift and a lint summary. Since 0.18.0, `repo-target` (which repo a repo-scoped command will hit — a note, never a finding) and `hooks-schema` (hook entries naming an event, tool or file Claude Code does not have). Exit 2 means a check *could not run*, which is deliberately distinct from clean. |
| `/gt:gt-scan` | Scan code against the language definitions in effect on this machine — naming conventions and encoding, per language, all of it from definition packs rather than from the script, so a contributed language pack teaches it a new language with no code change. An aggregator over leaf scanners: it reports how many members RAN alongside what they found. Since 0.18.0 an interrupted scan resumes where it stopped, from any session. |
| `/gt:gt-optimize` | Find what costs context and earns nothing back, as two members: `vault` — a fact duplicated across memory files, a dead index row, a `global-memory/` file over budget or naming one project, a relative date, a Knowledge page nobody reads, what each project costs to open (`--cost`); and `session` — prompt-cache writes lost to expiry or a changed prefix (`gt_optimize_session.py`). Reporting never writes; `--demote`, `--archive` and `--supersede` write only with `--apply`, through the queue, and delete nothing. |
| `/gt:gt-allin` | Every check in one command — scan, lint, the optimize report, install health, the credential scan, the runbook lint, and the wiki lint while gt-wiki is installed — reporting how many members actually ran, not just what they found. Never pushes and never applies a change. |
| `/gt:gt-allin-commit` | The separate, deliberate act: commit once the checks pass and a test receipt covers every staged file. Refuses on the default branch, refuses without evidence, and never pushes — a commit is reversible, a push is not. |
| `/gt:gt-context` | Render the vault's model-reachable definitions for a session to read, inside an explicit untrusted-data envelope. The Tier D slots' first consumer. The envelope is **not a security control** — it provides identifiability, not protection; see *Packs and the registry* below. |
| `/gt:gt-validation` | Record what a validation established, and what it could not determine, stamped with the file's content hash so the definition expires when the file changes. |
| `/gt:gt-lint` | Audit the vault for structural problems: broken wikilinks, orphaned pages, missing index entries, unlisted memory files, Knowledge pages citing superseded sources, stale pages, and Core rules that are stored but not enforced. Since 0.18.0 four checks file questions to the review queue: `adr-expires`, `bundled-concept`, `decision-candidate`, `memory-entity-orphan`. Applies fixes with your approval. |
| `/gt:gt-runbook-lint` | Scan all project `runbook.md` files for content that has drifted into multiple runbooks. Classifies duplicated content by type and routes it to the right shared layer (PROTOCOL.md, Knowledge page, or repo CLAUDE.md) via `gt-promote`. |
| `/gt:gt-settings` | View and change what Golden Thread does automatically. Thirty-three settings ship with gt — `component_updates`, `version_check`, `orphan_check`, `push_check`, `surface`, `daily_comms_content`, `release_announce`, `agent_specialization`, `skeptic_pass`, `handoff_surface`, `task_surface`, `test_gate`, `parallel_work`, `parallel_max`, `protected_paths`, and new in 0.18.0 `brief_absence_days`, `foreign_checkout_guard`, `decision_signals`, `knowledge_access_log`, `optimize_session_days`, `optimize_avoidable_pct`, `memory_contradiction_check`, `promotion_candidates`, `promotion_overlap`, `review_stamp`, `sync_check`, `reminder_days`, `reminder_macos`, `reminder_relay`, `reminder_email`, `commit_checks`, `addon_fixes`, `addon_fix_size_limit` — plus every setting an installed module adds (`report_card`, `closeout_check`, `usage_meter`, `usage_alert`, `visualize_publish`, `visualize_publish_visibility`, `watch`). Every automatic behaviour can be switched off. |

### Modules

Optional parts of Golden Thread, each a separate plugin in the same marketplace, installed
by `install.sh` while it is on. There are nine; `lotr` joined in 0.17.11.

| Module (plugin) | Default | Command | What it does |
|---|---|---|---|
| `wiki` (`gt-wiki`) | on | `/gt-wiki:gt-wiki` and four more | A standalone LLM wiki: immutable sources, interlinked Knowledge pages, ingest, lint and refresh loops. |
| `demo` (`gt-demo`) | on | `/gt-demo:gt-demo` | A guided tour on the fictional PizzaBot 3000 project — nine core acts plus one for each installed module that ships one (wiki, watch, flow) — in its own throwaway vault. `start`, `tour`, `end`, `clean`, `remove`, `status`. |
| `watch` (`gt-watch`) | on | `/gt-watch:gt-watch` | Watch any git repo you depend on. A cron fetch classifies each change by rules, and the next session opens with a **P0** when a watched repo ships a security fix (CVE/GHSA ids, security releases, advisories). Reporting stays off until the `watch` setting is `report`. Was `/gt:gt-watch`. |
| `report-card` (`gt-report-card`) | on | none | The session report card at `/compact` and session end, shown at the next session start, and the project close-out question. Settings `report_card`, `closeout_check`. |
| `farm` (`gt-farm`) | off (kept on when upgrading from a gt that had it) | `/gt-farm:gt-farm` | Route bulk, mechanical, or second-opinion tasks to an external AI service as a self-contained work packet. All four gates (Stateless, Self-contained, Checkable, Releasable) must pass before a task leaves. Results come back unverified. Was `/gt:gt-farm`. |
| `flow` (`gt-flow`) | on | `/gt-flow:gt-flow` | Render the vault's event stream as one offline HTML page: a lane per project, an arrow each time knowledge climbed a level. Add `--redact` before sharing it. |
| `visualize` (`gt-visualize`) | on | `/gt-visualize:gt-visualize` | Explain a codebase as a scroll-driven 3D walkthrough of how its parts work together, or render it as a 3D code city — one offline HTML page (three.js inlined): directories are districts, files are buildings, height is lines, colour is language or git churn. `--redact` before sharing. |
| `lotr` (`gt-lotr`) | off | `/gt-lotr:gt-lotr` | LOTR, also called gt MCP: one gateway to rule them all. A fixed four-tool MCP surface (`find`, `call_read`, `call_write`, `call_consent`) in front of any number of downstream connections (GitHub, Jira, Microsoft Graph, other REST APIs), with a registry of who talks to whom, as whom, from which machine. The gateway, not the assistant, sets each operation's tier; consent operations are confirmed in a dialog the daemon raises. Credentials are references (keychain, store, file), never values. Turn on with `./install.sh --with lotr`. |
| `usage` (`gt-usage`) | on | `/gt-usage:gt-usage` | Where this account stands against its Claude plan allowance — the 5-hour, weekly and monthly-spend windows. Records a reading a minute and says nothing until one is worth acting on; `usage_alert always` keeps it on screen. |

#### What flow shows

Every move through the vault — a capture, a promotion, an ADR, a source ingested or
superseded — is recorded as an event in `Projects/golden-thread/events.jsonl` by the skill
that made it. `/gt-flow:gt-flow` (or *"show how knowledge moved"*) draws that stream as one
offline HTML file: a lane per project, time left to right, height in the lane the ladder
level. Dots arrive, triangles move, squares leave, and an arrow joins the level an item left
to the level it reached.

![Flow view of three fictional demo projects, with findings climbing from memory to research to Knowledge and on to a Core rule](docs/flow-example.png)

The picture is [`docs/flow-example.html`](docs/flow-example.html), rendered from the
sample stream [`docs/flow-example-events.jsonl`](docs/flow-example-events.jsonl). Open
the HTML to filter by project or kind and click any mark for its details.

```bash
# Resolve the newest installed gt-flow, so this line does not go stale with the version
FLOW=$(ls -d ~/.claude/plugins/cache/golden-thread-plugin/gt-flow/*/scripts | sort -V | tail -1)
python3 $FLOW/gt_flow.py render --vault <vault> [--project <slug>] [--since YYYY-MM-DD] [--tasks] [--redact]
```

A vault older than 0.15.0 starts empty: `gt_events.py backfill --dry-run` shows the history
it would recover from git and `log.md`. Use `--redact` before the page leaves your screen.

---

## Packs and the registry (0.16.1)

Pluggable definitions — naming conventions, ignore sets, secret shapes, vocabularies — live in
**packs**: one JSON file of data each, shipped in `packs/core/` and `packs/community/` and
hash-verified in `MANIFEST.json`. `scripts/gt_registry.py` resolves them into one answer per key
and names the source.

```bash
gt_registry.py show <slot> [--lang X]   # what is in effect, and where it came from
gt_registry.py sources                  # every pack found, in precedence order
gt_registry.py slots                    # the slot table
```

Precedence is **community < core < local**: a merged contribution extends coverage but never
silently redefines a core default, and a user's own packs under
`<vault>/Projects/golden-thread/packs/` always win. A shadowed entry is reported, not dropped.

Union slots are additive — nothing can replace an entry, which is what stops a contributed pack
retiring a core definition. To switch one off, a pack **in your own vault** carries a `retract`
list: `{"retract": [{"lang": "go"}]}` turns off every Go definition in that slot, and is
reported as `RETRACTED` rather than hidden. Only vault packs may retract. `gt_registry.py slots`
also marks the slots no shipped tool reads yet, so a pack for one of them is a considered
choice rather than a surprise. Contributing a pack: `../SUBMISSIONS.md`.

**The `/gt:gt-context` envelope is not a security control (0.16.2).** It wraps rendered
definitions in a marked block saying *data, not instructions*, and no in-context framing of that
kind holds: *Adaptive Attacks Break Defenses Against Indirect Prompt Injection Attacks on LLM
Agents* (NAACL 2025 Findings, arXiv:2503.00061) attacked eight published defences and broke all
eight, with attack success above 50% in every case — delimiter schemes like this one and trained
detectors alike. What the envelope provides is **identifiability**: a line is legible as
someone's definition rather than as the system speaking. What protects a session is that packs
are **reviewed before merge**, and that a pack can only reach a session already trusting the
vault.

## Vault Structure

```
<vault>/
  CLAUDE.md                   ← Knowledge page conventions and schema
  index.md                    ← navigational index of all Knowledge pages
  log.md                      ← audit trail: every create, ingest, promote, retire
  review-queue.md             ← items flagged for owner review (written by gt-lint)
  INBOX.md                    ← the capture point: one checkbox line, from any session
  TASKS.md                    ← GENERATED cross-project task rollup (gt_tasks.py)

  Sources/                    ← IMMUTABLE raw originals
    YYYY-MM-DD <title>.md     ← never modified after creation; superseded by new files

  Knowledge/                  ← cross-project wiki pages (synthesized from Sources/)
    <Page Title>.md

  global-memory/              ← facts loaded in every session, all projects
    MEMORY.md                 ← index; read automatically via CLAUDE.md pointer
    <topic>.md

  Projects/
    README.md                 ← master project list + Dataview views
    CONVENTIONS.md            ← lifecycle phases, property schema, domain taxonomy
    PROTOCOL.md               ← recurring process rules across all projects
    INFRASTRUCTURE.md         ← the server fleet, defined ONCE and linked from each project

    golden-thread/
      core-rules/             ← the Core tier the hooks re-assert
      tools/                  ← gt_log.py, gt_adr.py, gt_events.py, gt_tasks.py, …
      events.jsonl            ← the event stream gt-flow draws
      packs/                  ← your own packs; highest precedence in the registry

    <project-slug>/
      README.md               ← status board + YAML property frontmatter
      source.md               ← where the code lives, which hosts, deploy file plan
      idea.md                 ← original brain dump — IMMUTABLE after creation
      research.md             ← append-only findings and gotchas
      decisions.md            ← append-only ADRs
      design.md               ← iteratively updated architecture
      spec.md                 ← handoff artifact with acceptance criteria (created when design is complete)
      runbook.md              ← operational procedures (optional, created with --runbook flag)
      memory/
        MEMORY.md             ← session memory index for this project
        <topic>.md            ← session memory files
```

---

## Immutability Model

Two file types are permanently immutable once created:

**`Sources/`** — Raw ingested content is stored here verbatim before being synthesized into Knowledge pages. Sources are never edited. When upstream content changes, `gt-refresh` creates a new `Sources/YYYY-MM-DD <title>.md` with `supersedes: [old-file]` in its frontmatter — the old file stays on disk as the historical record. `gt-lint` detects Knowledge pages that still cite a superseded source (`superseded-cited` check).

**`idea.md`** — Every project's origin story. Captured from the conversation at project creation and never changed. It is the traceable "why" for everything that follows.

Additionally, `research.md` and `decisions.md` are **append-only** — history is never rewritten, only extended or superseded by new entries.

---

## Key Files Explained

| File | Mutability | Purpose |
|---|---|---|
| `Sources/*.md` | Immutable | Raw originals; never edited after creation |
| `idea.md` | Immutable | Original brain dump; the project's "why" |
| `research.md` | Append-only | Dated findings, gotchas, measured behaviors |
| `decisions.md` | Append-only | Numbered ADRs; reversed by adding a new ADR, never by editing |
| `design.md` | Mutable | Current architecture; always describes NOW |
| `spec.md` | Mutable (scope changes only) | Self-contained handoff doc with acceptance criteria |
| `runbook.md` | Mutable | Operational HOW-TO for this project specifically |
| `PROTOCOL.md` | Append-only | Cross-project process rules proven across multiple projects |
| `Knowledge/*.md` | Mutable | Synthesized summaries; cite `Sources/` |
| `memory/*.md` | Mutable | Session state; updated in place. Frontmatter carries `level` (core/context/generic) and `enforcement` (validated/reminder) fields that declare how durable a rule is. |
| `source.md` | Mutable | Where the code lives, per role × env, plus the deploy file plan |
| `INFRASTRUCTURE.md` | Mutable | The server fleet — defined once, never copied into a project |


---

## Source Topology

Every project records **where its code actually lives** in `source.md`, which
`gt-open` reads first — acting without it is how you edit the wrong box.

| Topology | Meaning |
|---|---|
| `local` | A folder on this machine. No deploy step. |
| `remote` | One repo, one server; the server pulls from GitHub. |
| `bastion-jump` | Several servers, all reached through one gateway host. |
| `bastion-direct` | Several servers, each reachable independently. |

Addressing is **role × env → host + path** — one machine can serve several
vhosts, and the same role exists in more than one environment.

Bastion projects also carry a **file plan** marking each deployed file **static**
(byte-identical everywhere — deploy from one copy) or **unique** (a per-server
variant that must never be cross-deployed). Two files sharing a name across hosts
are either the same file that must not drift or different files that must not be
merged; only the file plan says which.

## Lazy Loading

`gt-open` reads a project's core docs, then reads `memory/MEMORY.md` — an index
of one line per file — and **stops**. Memory files load on demand.

A project with 70 notes costs about 80 lines to open instead of ~2,000. This is
why `MEMORY.md` descriptions matter: they are the whole basis for deciding
whether a file is worth opening.

## Project Properties

Project `README.md` files carry YAML frontmatter — real Obsidian properties that
drive the tag pane, search, and the Dataview views in `Projects/README.md`:

```yaml
---
type: project
slug: my-project          # must match the folder name
domain: trading           # coarse grouping
stage: active
topology: bastion-direct
tags: [trading, platform, live]
---
```

**Categorise with properties, not folders.** Grouping lives in `domain` and
`tags`; Obsidian resolves `[[wikilinks]]` by filename regardless of folder, so a
category directory buys nothing and fragments project lookup. The only valid
second level is a genuine sub-project created with `--parent`.

---

## Install

See [INSTALL.md](INSTALL.md) for step-by-step instructions, including how to install from a GitHub release.

```bash
bash install.sh --vault <vault>     # gt plus every module that is on; then restart Claude Code
bash install.sh --list-modules      # the nine modules, each one's state and why
bash install.sh --without demo      # leave one out; remembered
```

Re-running it upgrades from any older release to the newest, including vault upgrades when your
vault is committed.

Once installed, code in your own projects passes four gates by default before it reaches its
repository — tests seen to pass, `/gt:gt-allin`, `/gt:gt-allin-commit` and the automatic commit
guard — and gt never pushes. See the [MANUAL](MANUAL.md#default-release-gates-what-your-project-passes-before-it-reaches-its-repository).

![Default release gates: tests seen to pass, gt-allin, gt-allin-commit and the commit guard, then your own push](docs/release-process.svg)

A full install writes **sixteen hook registrations across fourteen scripts** into
`~/.claude/settings.json`, from one declaration (`HOOK_REGISTRATIONS` in `gt_components.py`)
that the installer registers from and the drift check compares against — so they cannot
disagree about what "wired" means. `install.sh` owns ten of them: the five `SessionStart`
reporters (`gt_components.py`, `gt_workers.py`, `gt_version_check.py`, `gt_push_check.py`
and, since 0.17.2, `gt_surface.py`), `gt_state.py` twice (`UserPromptSubmit` and
`PreCompact`), the `guard_protected_paths.sh` and — since 0.18.0 — `guard_foreign_checkout.sh`
`PreToolUse` guards, which are installer-owned precisely because they are tied to no Core rule
and need no vault to be wired, and — since 0.18.0 — `log_knowledge_read.sh` on `PostToolUse`
(matcher `Read`), the Knowledge read log.

The other six are the **enforcement** tier, wired by `/gt:gt-init` — **six registrations
across five scripts**, because since 0.16.2 `inject_core_rules.sh` is registered twice and
the two counts differ:

| Event | Script | What it is for |
|---|---|---|
| `UserPromptSubmit` | `inject_core_rules.sh` | re-asserts the Core rules on every turn |
| `SessionStart` (matcher `compact`) | `inject_core_rules.sh` | **new in 0.16.2** — re-asserts them after a compaction |
| `Stop` | `validate_response.sh` | mechanical: validates the reply |
| `PreToolUse` | `guard_session_claims.sh` | mechanical |
| `PreToolUse` | `guard_vault_writes.sh` | mechanical |
| `PreToolUse` | `guard_test_before_commit.sh` | mechanical |

`inject_core_rules.sh` is wired twice on purpose. Hook-added context is *summarised* during a
compaction rather than re-injected from disk, so before 0.16.2 the Core rules survived a
compaction only as whatever the summariser chose to keep. The exposure was the **remainder of the
turn** in which an auto-compaction fired — `UserPromptSubmit` has no compaction exception, so the
next user prompt always restored them verbatim. The mechanical tier was never affected: the
`PreToolUse` guards and the `Stop` validator are event-driven commands, not context.

If you ever need to rewire them manually:

```bash
python3 <scripts>/vault_init.py install-core-rules --vault <vault>
```

### Standing behaviour: parallel work, and the gates

These shipped earlier and are all on by default. They surprise people the
first time, so they are written down here rather than only in the changelog.

> [!NOTE]
> **Since 0.12.4, divisible work runs in parallel by default.** A Core rule asks for work
> that splits into independent units to be spread across your processors instead of
> crawling through one core, and the tools here honour it — `tests/run.sh` and
> `dev/render-pdfs.sh` fan out, and so will anything Claude writes while the rule is
> active. **The first sign is usually the fans**: many processes at once and CPU well
> above 100%, all of it gone when the run ends. That is the feature, not a runaway job.
> Measured on this suite: 509s to 108s, 4.7x.
>
> You decide how much of the machine it may use:
>
> ```bash
> gt_settings.py set parallel_max 4      # never more than 4 workers
> gt_settings.py set parallel_work off   # serial, and the rule stops being asserted
> gt_settings.py show                    # what is allowed right now
> ```
>
> **Since 0.12.5:** a `git commit` carrying code whose tests have not been seen to
> pass is **refused**. Evidence is a receipt written by your test run (`tests/run.sh` and
> `dev/release-check.sh` write their own; any project can with `gt_test_receipt.py record
> --what <suite> --ok`). If a repo has no tests, exempt it once with `touch .gt-no-test-gate`; for a
> single commit, `GT_TEST_GATE=off git commit …`; to switch it off entirely,
> `gt_settings.py set test_gate off`. Docs-only commits are never blocked.
>
> **Since 0.17.0 the scanners run on three cadences, and only two of them block:**
>
> | | What it looks at | What it does |
> |---|---|---|
> | `tests/run.sh` | the file set the scanners define | fails the run — and writes no receipt, so the test gate above will not license the commit either |
> | the commit gate | the staged diff only | denies the commit |
> | `gt_sweep.py` | the whole tree | **reports**, never blocks |
>
> The narrow two are narrow deliberately: a commit gate that scanned the whole tree would
> punish you for someone else's old code. But that means nothing ever re-examines what
> nobody is touching — a rule added today never sees a file no one edits, and a credential
> committed before the gate existed stays committed. The sweep is the answer to "what is
> true about the tree as a whole", and because it can surface old debt it must never block:
> a report you read on a Monday, not a wall in front of a commit.
>
> **A commit is also checked for credentials.** `/gt:gt-init` seeds `.githooks/` in the
> vault and points `core.hooksPath` at it; the `pre-commit` hook there runs `gt_secrets.py
> --staged` over the staged blobs — the index, not the worktree, because `git add`-ing a
> credential and then tidying the file in your editor leaves a clean worktree and a dirty
> commit. It is the only hook gt ships that **fails closed**: "could not run" refuses the
> commit, because a credential in git history is in every clone, fork and backup
> immediately and the only real remedy is rotating it. A finding is `path:line`, a rule id
> and a **length** — never the value. If that gate passes, `gt_scan_code.py --staged` runs
> after it; there a real finding blocks, but a rule skipped because this machine lacks an
> optional evaluator tier is reported and allowed through, since blocking on it would
> refuse every commit on every machine without `ast_grep_py`.
>
> Findings you have examined and accepted belong in a baseline —
> `.gt/secrets-baseline.json` and `.gt/code-baseline.json`, which the hook picks up if they
> exist — not in an escape. The escapes are `git commit --no-verify` for one commit, and
> `git config gt.secretsgate off` for this clone, which prints that it is off on every
> commit: a gate you have forgotten you disabled is indistinguishable from one that works.
> Recording each commit's verdict into the vault is opt-in per clone
> (`git config gt.checkreport on`), because a hook that writes to the vault on every commit
> in every repo on the machine is a side effect nobody asked for.

---

## Typical Session Pattern

```
# Start of session
/gt:gt-open my-project        ← loads all project docs, summarizes state

# During session
(work happens — Claude Code keeps context)
/gt:gt-route                  ← "where is this going?" — names the drift, says where the
                                output belongs and whether you are in the right project,
                                harness and model. Run it whenever the ground has shifted.

# End of session
/gt:gt-work                   ← writes findings, ADRs, updates design, creates spec if ready

# Writing to the shared files — always through the tool, never by hand
tools/gt_log.py add "<line>"            ← your session's spool; log.md is generated
tools/gt_adr.py allocate <project>      ← reserves the next ADR number atomically
gt_write_queue.py … && gt_broker.py drain ← every other vault write (Core rule 1, 0.17.11)

# Periodically
/gt:gt-lint                   ← catch structural drift
/gt:gt-refresh                ← check if any source docs changed upstream
/gt:gt-review                 ← promote daily note items to tracked projects
/gt:gt-promote                ← graduate a finding to a Knowledge page or global-memory
/gt:gt-validate               ← verify a claim before recording it as fact
/gt-farm:gt-farm              ← route bulk or external-opinion tasks out of this context (farm module)
/gt-flow:gt-flow              ← draw how knowledge climbed the ladder (flow module)
/gt-visualize:gt-visualize    ← explain a codebase in 3D, or draw it as a code city (visualize module)
```

---

## Script Reference

Two of these you will run by hand often. `log.md` and `decisions.md` are **generated**
from per-session spool files, so nothing writes them directly:

```bash
# Record a log entry -- writes only YOUR session's spool file
python3 <vault>/Projects/golden-thread/tools/gt_log.py --vault <vault> add "2026-01-01 10:00 CST [work] my-project — what happened"

# Reserve the next ADR number before writing the decision
python3 <vault>/Projects/golden-thread/tools/gt_adr.py --vault <vault> allocate my-project --title "The choice"

# Regenerate either file (idempotent)
python3 <vault>/Projects/golden-thread/tools/gt_log.py --vault <vault> merge
python3 <vault>/Projects/golden-thread/tools/gt_adr.py --vault <vault> merge my-project

# A log line plus one structured event (what moved where) -- gt-promote and gt-refresh
# record their moves this way; gt-review, gt-work and gt-ingest call gt_events.py emit,
# and gt-create's scaffold script emits its own create event
python3 <vault>/Projects/golden-thread/tools/gt_log.py --vault <vault> add "2026-01-01 [graduate] a → b" \
  --event promote --item Knowledge/x.md --from Projects/p/research.md --to Knowledge/x.md \
  --level-from 3 --level-to 4 --project p

# Preview the event history rebuilt from git and log.md (a vault older than the events)
python3 <vault>/Projects/golden-thread/tools/gt_events.py backfill --vault <vault> --dry-run

# Every other vault write goes through the write queue (Core rule 1, since 0.17.11): queue it,
# then drain. Ops: append, replace-section, create, set-property (one frontmatter key) and
# replace-file (the whole file, guarded by the hash of what was read). The skills already do this;
# the guard's denial message prints the exact command.
H=~/.claude/golden-thread/hooks
python3 $H/gt_write_queue.py --vault <vault> --path Projects/p/research.md --op append \
  --section Findings --content-file note.md
python3 $H/gt_write_queue.py --vault <vault> --path Projects/p/README.md --op set-property \
  --key status --value active
python3 $H/gt_broker.py drain --vault <vault>        # apply, oldest first, then exit
python3 $H/gt_broker.py status --vault <vault>       # how many are waiting
python3 $H/gt_broker.py audit --vault <vault> --since 24   # .md files changed NOT by the broker

# One-time, per vault and per project
python3 <vault>/Projects/golden-thread/tools/gt_log.py --vault <vault> migrate
python3 <vault>/Projects/golden-thread/tools/gt_adr.py --vault <vault> migrate my-project
```

Python scripts can also be run directly from the command line. One variable, so a version
bump does not strand eight copied paths — from the release tree, or from the install:

```bash
GT=golden-thread/0.17.11/scripts
# installed instead:  GT=$(ls -d ~/.claude/plugins/cache/golden-thread-plugin/gt/*/scripts | sort -V | tail -1)

# Create a new vault
python3 $GT/vault_init.py fresh \
  --vault ~/my-vault --domain "My Team"

# Scaffold a project
python3 $GT/vault_init.py create-project \
  --vault ~/my-vault \
  --name my-project \
  --title "My Project" \
  --tags "platform,backend" \
  --domain platform \
  --topology bastion-direct \
  --runbook \
  --project-dir ~/Projects/my-project

# Scaffold a sub-project
python3 $GT/vault_init.py create-project \
  --vault ~/my-vault \
  --name sub-feature \
  --parent my-project \
  --title "Sub Feature"

# Point vault-config.json at an existing vault
python3 $GT/vault_init.py connect \
  --vault ~/existing-vault

# Install/rewire Core-rule enforcement hooks
python3 $GT/vault_init.py install-core-rules \
  --vault ~/my-vault

# Scan a project directory for ingest candidates
python3 $GT/gt_ingest.py ~/Projects/my-project --json

# Audit vault health
python3 $GT/gt_lint.py ~/my-vault \
  --queue ~/my-vault/review-queue.md

# View/change automatic behaviours
python3 $GT/gt_settings.py show

# Find credentials in the wrong place. A separate tool from every other scanner because
# its output rule is the opposite of theirs: nothing here ever prints matched source
# text, and no other check runs beside it. A finding is path:line, a rule id and a
# LENGTH -- never an excerpt, a prefix or a hash. --staged reads the index, which is
# what the commit gate uses. Exit 0 clean / 1 found something / 2 could not run.
python3 $GT/gt_secrets.py ~/Projects/my-project \
  --exclude 'vendor/**' --baseline .gt/secrets-baseline.json
python3 $GT/gt_secrets.py . --write-baseline .gt/secrets-baseline.json

# Source validation: check code against the `lint` rules in effect. A leaf of gt_scan.py
# alongside gt_scan_language.py. Rules are DATA from lint packs, in a documented subset
# of ast-grep's rule schema; a rule whose evaluator tier is absent here is reported
# SKIPPED, never silently passed. Unlike gt_secrets it DOES print source text -- there
# the text is the finding -- which is why the two may never share a process.
python3 $GT/gt_scan_code.py ~/Projects/my-project --sarif findings.sarif
python3 $GT/gt_scan_code.py --rules          # the rules in effect, and which tier each needs

# The weekly cadence: the whole tree, REPORTS and never blocks. The other two cadences
# are narrow on purpose, so nothing re-examines code nobody is touching; this is what
# answers "what is true about the tree as a whole". Files its verdict in the vault.
python3 $GT/gt_sweep.py --vault ~/my-vault --path ~/Projects/my-project --only secrets,code

# File a check's result into the vault, so a gate leaves a record. A verdict, a count, a
# scope and a ref -- never a finding's CONTENT, because the vault is committed and pushed.
# It answers the three questions an exit code cannot: is the check running at all, is it
# getting noisier, did anyone look.
python3 $GT/gt_check_report.py record --vault ~/my-vault \
  --check secrets --verdict clean --count 0 --scope "staged diff" --ref HEAD
python3 $GT/gt_check_report.py show --vault ~/my-vault --check secrets

# The deterministic framework for code review -- gt does the planning, the mechanical
# rejection and the record; the judgement is yours. gt ships NO dimensions deliberately:
# the opinions are the user's or the company's, added as a `review` pack in their vault.
# Zero dimensions configured exits 3, never 0 -- "nothing was reviewed" is not "clean".
python3 $GT/gt_code_review.py dimensions --vault ~/my-vault
python3 $GT/gt_code_review.py plan ~/Projects/my-project --vault ~/my-vault --staged
python3 $GT/gt_code_review.py validate findings.json --root ~/Projects/my-project
python3 $GT/gt_code_review.py report findings.json --root ~/Projects/my-project \
  --vault ~/my-vault --ledger ~/my-vault/Projects/my-project/review-ledger.jsonl

# Install, verify and remove gt's scheduled (launchd) jobs -- `daily`, `lint-weekly` and
# `sweep` (0.17.2; the sweep's pre-flight currently refuses: the registry cannot find its
# packs from the hooks dir). `/gt:gt-doctor`'s `schedule` check reads each installed job's
# last exit.
# `install` does not stop at writing a plist: it bootstraps the job, kickstarts it and
# reads launchd's own exit code, because a job that works in a terminal can still fail
# under launchd. `remove` is the rollback, which is what makes install safe to re-run.
python3 $GT/gt_schedule.py list
python3 $GT/gt_schedule.py install daily --vault ~/my-vault \
  --repo ~/Projects/my-project --hour 22 --minute 0
python3 $GT/gt_schedule.py check daily
python3 $GT/gt_schedule.py remove daily
python3 $GT/gt_schedule.py install sweep --vault ~/my-vault --repo ~/Projects/my-project

# Write the day's FACTS into Daily Notes/<date>.md: tasks closed, commits per repo, event
# counts, wiki item counts, an active span per project. Terse by design -- it does not
# explain or interpret, because the meaning of the day is the owner's to write. Each fact
# goes in a marked block under its heading (Did, Decided, Open at end of day), replaced
# whole each run, with the counts in a footer; it never touches `## Noticed`.
python3 $GT/gt_daily.py --vault ~/my-vault --repo ~/Projects/my-project --dry-run
python3 $GT/gt_daily.py --vault ~/my-vault --check

# Write the session's state BEFORE the context runs out. The signal is `ctx_pct` --
# context fill -- and never the rate-limit meter: a real reading was 5% of the five-hour
# allowance at 91% context, so triggering on the allowance would have fired at the wrong
# moment and looked right doing it. PreCompact is the backstop, not the primary.
python3 $GT/gt_state.py check --margin 5 --json
python3 $GT/gt_state.py write --reason "handing over"
python3 $GT/gt_state.py show

# Put what the last session left behind in front of this one (SessionStart, 0.17.2): the
# MUST DO block from <vault>/deadlines.md every session, new handoffs and state files once
# each. Never writes the vault. --dry-run leaves the "already shown" ledger untouched.
python3 $GT/gt_surface.py check --dry-run
python3 $GT/gt_surface.py must-do --vault ~/my-vault
python3 $GT/gt_surface.py handoffs --project my-project --vault ~/my-vault   # what /gt:gt-open runs

# Is a handoff still waiting on someone? open / deferred (to a DATE) / handled / history.
# `list` never loads a handoff's body; `mark` appends to the handoff's status log.
python3 $GT/gt_handoff_status.py list --vault ~/my-vault [--project my-project] [--all]
python3 $GT/gt_handoff_status.py mark Projects/my-project/handoff/2026-09-28-handoff.md \
  --vault ~/my-vault --status deferred --until 2026-10-12 --reason "after the migration"

# Move a note DOWN the cost ladder without deleting it. /gt:gt-promote moves knowledge up
# by how SETTLED it is; this moves it down by how often it is PAID FOR -- global-memory/
# (read in every session of every project) -> Projects/<slug>/memory/ (every session of
# one project) -> Knowledge/<page>.md (read only when asked). Without --apply it is a
# preview; with it, the destination is written and verified before the source is removed.
python3 $GT/gt_demote.py --vault ~/my-vault \
  --file global-memory/one-project-fact.md --to project-memory --project my-project
python3 $GT/gt_demote.py --vault ~/my-vault \
  --file global-memory/one-project-fact.md --to knowledge --apply
```

---

## Obsidian Setup

Golden Thread works with any folder, but it is designed to be read in Obsidian, and a few
plugins carry real weight. This is the set the reference vault runs, verified against its
`.obsidian/` configuration on 2026-09-08 (plugin IDs in parentheses are what Obsidian's
settings and `community-plugins.json` use).

**Required**

| Plugin | Why |
|---|---|
| **Dataview** (`dataview`) | `Projects/README.md` and `TASKS.md` render their live tables from Dataview queries, and every task line carries inline fields (`[p:: ]`, `[waiting:: ]`, `[since:: ]`, `[due:: ]`) that Dataview indexes. Without it those pages are static text. |

**Core plugins to turn on** (built in): Files, Search, Quick switcher, Graph view, Backlinks,
Outgoing links, Tags view, Properties, Daily notes, Templates, Command palette, Outline,
File recovery, Bases. Backlinks and Graph are how the wiki is navigated; Daily notes is what
`/gt:gt-review` sweeps when a daily-notes folder exists; File recovery is the safety net beside
git; Bases can replace Dataview tables if you prefer.

**Recommended community plugins**

| Plugin | Role in the workflow |
|---|---|
| **Terminal** (`terminal`) | Run Claude Code in a pane inside the vault, so the session and the notes share a window. |
| **Calendar** (`calendar`) | Navigate daily notes; pairs with the Daily notes core plugin. |
| **Text Extractor** (`text-extractor`) | OCR for PDFs and images you ingest into `Sources/`. |
| **Advanced Tables** (`table-editor-obsidian`) | Editing the task, source and inventory tables by hand. |
| **Templater** (`templater-obsidian`) | Optional scripting for note templates; the vault scaffolds projects by script, so this is convenience. |
| **Excalidraw** (`obsidian-excalidraw-plugin`) | Design sketches next to `design.md`. |
| **Auto Card Link** (`auto-card-link`), **URL Formatter** (`url-formatter`) | Paste a URL and get a card or a Markdown link; useful when capturing a source. |
| **Recent Files**, **Home tab**, **Iconize**, **Style Settings**, **Trash Explorer**, **BRAT** | Quality of life: recent-file list, browser-style start tab, icons, theme variables, `.trash` recovery, beta-plugin installs. |

Full detail, including which plugins are installed but off and the core plugins to leave off,
is in [OBSIDIAN-WORKFLOW.md](OBSIDIAN-WORKFLOW.md#plugins). One caution: never run Obsidian Sync and another sync engine (Dropbox, OneDrive) on the same vault folder.

## Requirements

- Python 3.8+
- Claude Code (any version)
- Obsidian (optional — the vault is plain markdown files; Obsidian is just the GUI layer)

---

## Acknowledgments

Golden Thread builds on **Jonathan Tucci**'s llm-wiki and project-flow Claude Code
plugins. The core wiki pattern — immutable `Sources/`, synthesized `Knowledge/`,
wikilink-hop navigation — originates with Jonathan's design. Golden Thread extends it
with a hook architecture, Core-rule enforcement, a session report card, a separate
`gt-wiki` plugin, and a deeper project-memory hierarchy. Jonathan also identified key
drawbacks in Golden Thread's own original design — particularly around lazy loading and
the promotion discipline — and his insights directly informed the fixes that made it
practical to use at scale.

Two scripts in `golden-thread-wiki` come directly from Jonathan's work:

- **`scripts/wiki_log.py`** — deterministic writes to `log.md` and `index.md`, with a
  closed vocabulary of operations (ingest, query, lint, refresh, graduate, retire,
  relocate).
- **`scripts/wiki_refresh.py`** — git-native change detection for local sources:
  `git fetch` + `git diff base..head`, with `upstream_sha:` frontmatter for a precise
  diff baseline; web-only sources are flagged for the LLM to compare.

**Sergey Kryvets** recommended adding a visualization tool to Golden Thread — a great idea that became **gt-visualize**: the 3D code city, the scroll-driven explainers that show how a system's parts work together, publishing, and the guided walkthrough. Thank you, Sergey.

## License

MIT
