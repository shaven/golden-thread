# Golden Thread Plugin

> **Reader:** someone who has installed it and wants the reference
> **Claims last checked against the code:** 2026-09-16 — see *The documents, and what belongs in each* in [`CLAUDE.md`](../CLAUDE.md).

A Claude Code plugin that turns an Obsidian vault into the single source of truth for all AI memory across every project and every session.


> [!IMPORTANT]
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
> **Also new in 0.12.5:** a `git commit` carrying code whose tests have not been seen to
> pass is **refused**. Evidence is a receipt written by your test run (`tests/run.sh` and
> `dev/release-check.sh` write their own; any project can with `gt_test_receipt.py record
> --ok`). If a repo has no tests, exempt it once with `touch .gt-no-test-gate`; for a
> single commit, `GT_TEST_GATE=off git commit …`; to switch it off entirely,
> `gt_settings.py set test_gate off`. Docs-only commits are never blocked.

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

## Twenty-One Skills (plus modules)

### Setup

| Command | What it does |
|---|---|
| `/gt:gt-init` | Create a new vault from scratch, or wire an existing vault to a project. Idempotent — safe to re-run. |
| `/gt:gt-create` | Scaffold a new project folder with the standard structure. Captures your brain dump into `idea.md`. Supports sub-projects, tags, and runbook creation. |

### Daily Work

| Command | What it does |
|---|---|
| `/gt:gt-open` | Load a project at the start of a session. Reads all project docs in order (idea → research → decisions → design → spec → runbook → memory), then summarizes the project state and asks where to pick up. |
| `/gt:gt-route` | Mid-session check: what has this session actually become, where does its output belong, and is it happening in the right project, harness and model? Serves the middle of a session, where `gt-open` cannot see yet and `gt-work` sees too late. Cheap and repeatable — not a gate. |
| `/gt:gt-work` | Write back session findings at the end of a session. Appends to `research.md`, adds ADRs to `decisions.md`, refines `design.md`, creates `spec.md` when design is complete, and flags content for PROTOCOL.md. |
| `/gt:gt-ingest` | Bulk-import an existing project's memory files, CLAUDE.md rules, and notes into the vault. External sources are stored immutably in `Sources/` before being synthesized into Knowledge pages. |
| `/gt:gt-review` | Scan recent Obsidian daily notes for uncaptured tasks and ideas. Surfaces them grouped by date, then promotes selected ones into tracked project folders. |

### Knowledge Management

| Command | What it does |
|---|---|
| `/gt:gt-query` | Look something up in the vault — reads index.md first, follows wikilinks, falls back to grep. The vault is always checked before the web. |
| `/gt:gt-promote` | Graduate a fact, finding, or idea up the hierarchy: project memory → project files → Knowledge wiki page → global-memory. Also handles new project scaffolding and retiring stale content. |
| `/gt:gt-refresh` | Check `Sources/` for upstream changes. Supersedes outdated sources with new immutable files — never edits the old one. Updates Knowledge pages that cited the changed source. |

### Context & Verification

| Command | What it does |
|---|---|
| `/gt:gt-validate` | Verify a claim by re-deriving it with a fresh-context validator — never by reviewing the reasoning that produced it. Use before recording a finding as fact or before a production change. |

### Maintenance

| Command | What it does |
|---|---|
| `/gt:gt-upgrade` | Bring an existing vault up to the installed release: run the migrations it has not had, take the release's changes into `PROTOCOL.md` and `CONVENTIONS.md` without losing local edits, add newly shipped Core rules, stamp the vault. Rehearse with `--dry-run`; it refuses a dirty tree and backs up before applying. |
| `/gt:gt-doctor` | One report for the whole install: plugin version, component drift, hook wiring, pending vault migrations, stray workers, unpushed commits, publish-destination drift and a lint summary. Exit 2 means a check *could not run*, which is deliberately distinct from clean. |
| `/gt:gt-scan` | Scan code against the language definitions in effect on this machine — naming conventions and encoding, per language, all of it from definition packs rather than from the script, so a contributed language pack teaches it a new language with no code change. An aggregator over leaf scanners: it reports how many members RAN alongside what they found. |
| `/gt:gt-optimize` | Find vault content that costs context and earns nothing back: a fact duplicated across memory files, an index row pointing at a file that is gone, a `global-memory/` file over budget, a relative date in a file that will be read months later. Reports only — it never edits your notes. |
| `/gt:gt-allin` | Every check in one command — scan, lint, the optimize report, install health — reporting how many members actually ran, not just what they found. Never pushes and never applies a change. |
| `/gt:gt-allin-commit` | The separate, deliberate act: commit once the checks pass and a test receipt covers every staged file. Refuses on the default branch, refuses without evidence, and never pushes — a commit is reversible, a push is not. |
| `/gt:gt-handoff` | Write the next session a handoff it can trust: facts gathered from the project and repository, each labelled with its source and whether it was actually verified, plus the questions a script cannot answer. The design narrative is left for the person who did the work. |
| `/gt:gt-lint` | Audit the vault for structural problems: broken wikilinks, orphaned pages, missing index entries, unlisted memory files, Knowledge pages citing superseded sources, stale pages, and Core rules that are stored but not enforced. Applies fixes with your approval. |
| `/gt:gt-runbook-lint` | Scan all project `runbook.md` files for content that has drifted into multiple runbooks. Classifies duplicated content by type and routes it to the right shared layer (PROTOCOL.md, Knowledge page, or repo CLAUDE.md) via `gt-promote`. |
| `/gt:gt-settings` | View and change what Golden Thread does automatically: component drift checking at session start, and every setting an installed module adds (the report card, the upstream watch). Every automatic behaviour can be switched off. |

### Modules

Optional parts of Golden Thread, each a separate plugin in the same marketplace, installed
by `install.sh` while it is on. Since 0.15.0 there are six.

| Module (plugin) | Default | Command | What it does |
|---|---|---|---|
| `wiki` (`gt-wiki`) | on | `/gt-wiki:gt-wiki` and four more | A standalone LLM wiki: immutable sources, interlinked Knowledge pages, ingest, lint and refresh loops. |
| `demo` (`gt-demo`) | on | `/gt-demo:gt-demo` | A guided tour on the fictional PizzaBot 3000 project — nine core acts plus one for each installed module that ships one (wiki, watch, flow) — in its own throwaway vault. `start`, `tour`, `end`, `clean`, `remove`, `status`. |
| `watch` (`gt-watch`) | on | `/gt-watch:gt-watch` | Watch any git repo you depend on. A cron fetch classifies each change by rules, and the next session opens with a **P0** when a watched repo ships a security fix (CVE/GHSA ids, security releases, advisories). Reporting stays off until the `watch` setting is `report`. Was `/gt:gt-watch`. |
| `report-card` (`gt-report-card`) | on | none | The session report card at `/compact` and session end, shown at the next session start, and the project close-out question. Settings `report_card`, `closeout_check`. |
| `farm` (`gt-farm`) | off (kept on when upgrading from a gt that had it) | `/gt-farm:gt-farm` | Route bulk, mechanical, or second-opinion tasks to an external AI service as a self-contained work packet. All four gates (Stateless, Self-contained, Checkable, Releasable) must pass before a task leaves. Results come back unverified. Was `/gt:gt-farm`. |
| `flow` (`gt-flow`) | on | `/gt-flow:gt-flow` | Render the vault's event stream as one offline HTML page: a lane per project, an arrow each time knowledge climbed a level. Add `--redact` before sharing it. |

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
FLOW=~/.claude/plugins/cache/golden-thread-plugin/gt-flow/0.15.0/scripts
python3 $FLOW/gt_flow.py render --vault <vault> [--project <slug>] [--since YYYY-MM-DD] [--tasks] [--redact]
```

A vault older than 0.15.0 starts empty: `gt_events.py backfill --dry-run` shows the history
it would recover from git and `log.md`. Use `--redact` before the page leaves your screen.

---

## Packs and the registry (0.16.0)

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

## Vault Structure

```
<vault>/
  CLAUDE.md                   ← Knowledge page conventions and schema
  index.md                    ← navigational index of all Knowledge pages
  log.md                      ← audit trail: every create, ingest, promote, retire
  review-queue.md             ← items flagged for owner review (written by gt-lint)

  Sources/                    ← IMMUTABLE raw originals
    YYYY-MM-DD <title>.md     ← never modified after creation; superseded by new files

  Knowledge/                  ← cross-project wiki pages (synthesized from Sources/)
    <Page Title>.md

  global-memory/              ← facts loaded in every session, all projects
    MEMORY.md                 ← index; read automatically via CLAUDE.md pointer
    <topic>.md

  Projects/
    README.md                 ← master project list
    CONVENTIONS.md            ← lifecycle phases, file roles, tag taxonomy
    PROTOCOL.md               ← recurring process rules that apply across all projects

    README.md                 ← master project list + Dataview views
    CONVENTIONS.md            ← lifecycle phases, property schema, domain taxonomy
    PROTOCOL.md               ← recurring process rules across all projects
    INFRASTRUCTURE.md         ← the server fleet, defined ONCE and linked from each project

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
bash install.sh --list-modules      # the six modules, each one's state and why
bash install.sh --without demo      # leave one out; remembered
```

Re-running it upgrades from any older release to the newest, including vault upgrades when your
vault is committed.

After running `/gt:gt-init`, Golden Thread also wires enforcement hooks into `~/.claude/settings.json`. These re-assert Core rules at session start and session end. If you ever need to rewire them manually:

```bash
python3 <scripts>/vault_init.py install-core-rules --vault <vault>
```

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

# Periodically
/gt:gt-lint                   ← catch structural drift
/gt:gt-refresh                ← check if any source docs changed upstream
/gt:gt-review                 ← promote daily note items to tracked projects
/gt:gt-promote                ← graduate a finding to a Knowledge page or global-memory
/gt:gt-validate               ← verify a claim before recording it as fact
/gt-farm:gt-farm              ← route bulk or external-opinion tasks out of this context (farm module)
/gt-flow:gt-flow              ← draw how knowledge climbed the ladder (flow module)
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
# record their moves this way; gt-review, gt-work and gt-ingest use gt_events.py emit
python3 <vault>/Projects/golden-thread/tools/gt_log.py --vault <vault> add "2026-01-01 [graduate] a → b" \
  --event promote --item Knowledge/x.md --from Projects/p/research.md --to Knowledge/x.md \
  --level-from 3 --level-to 4 --project p

# Preview the event history rebuilt from git and log.md (a vault older than the events)
python3 <vault>/Projects/golden-thread/tools/gt_events.py backfill --vault <vault> --dry-run

# One-time, per vault and per project
python3 <vault>/Projects/golden-thread/tools/gt_log.py --vault <vault> migrate
python3 <vault>/Projects/golden-thread/tools/gt_adr.py --vault <vault> migrate my-project
```

Python scripts can also be run directly from the command line:

```bash
# Create a new vault
python3 golden-thread/0.15.0/scripts/vault_init.py fresh \
  --vault ~/my-vault --domain "My Team"

# Scaffold a project
python3 golden-thread/0.15.0/scripts/vault_init.py create-project \
  --vault ~/my-vault \
  --name my-project \
  --title "My Project" \
  --tags "platform,backend" \
  --domain platform \
  --topology bastion-direct \
  --runbook \
  --project-dir ~/Projects/my-project

# Scaffold a sub-project
python3 golden-thread/0.15.0/scripts/vault_init.py create-project \
  --vault ~/my-vault \
  --name sub-feature \
  --parent my-project \
  --title "Sub Feature"

# Point vault-config.json at an existing vault
python3 golden-thread/0.15.0/scripts/vault_init.py connect \
  --vault ~/existing-vault

# Install/rewire Core-rule enforcement hooks
python3 golden-thread/0.15.0/scripts/vault_init.py install-core-rules \
  --vault ~/my-vault

# Scan a project directory for ingest candidates
python3 golden-thread/0.15.0/scripts/gt_ingest.py ~/Projects/my-project --json

# Audit vault health
python3 golden-thread/0.15.0/scripts/gt_lint.py ~/my-vault \
  --queue ~/my-vault/review-queue.md

# View/change automatic behaviours
python3 golden-thread/0.15.0/scripts/gt_settings.py show
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

## License

MIT
