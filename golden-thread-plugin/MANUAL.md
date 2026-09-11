# Golden Thread — User Manual

Complete reference for all seventeen skills. Written against **gt v0.10.0**.

---

## The model

Claude forgets everything between sessions. The vault doesn't. Golden Thread's
job is to move what you learn *out* of a session into a file whose **location**
tells you how widely it applies — then load only the slice the next session needs.

```
session conversation
      ↓  gt-work
Projects/<slug>/memory/*.md      this project, this detail
      ↓  gt-promote
research.md                      dated findings — `unverified` allowed, if labelled
      ↓  gt-promote + gt-validate
decisions.md · design.md         settled choices — independently verified
      ↓  gt-promote + gt-validate
Knowledge/                       true for several projects — independently verified
      ↓  gt-promote + gt-validate
global-memory/                   true everywhere — independently verified
```

Facts move **up** as they prove general. They never move back down, and nothing
is silently deleted.

**Verification gates the climb — it is not a step at the end.** `research.md` holds
dated findings and accepts an `unverified` figure so long as it is labelled. Every
level above it *reads as settled*, so a figure entering `decisions.md`, `design.md`,
`Knowledge/` or `global-memory/` — or driving a production change — must be
**independently verified**: re-derived by a validator that never saw the reasoning.
A second pass by the same session is only `self-verified`; it shares the assumptions
that produced the error, so it confirms rather than checks. Run `/gt:gt-validate`
*before* the promotion.

> This rung was added after an unverified analysis reached `decisions.md` as an ADR
> recommending a four-ticker production change. An independent validator refuted three
> of the four — two of which would have deepened portfolio drawdown while appearing to
> improve their own ticker's. The ladder carried the error upward because nothing on it
> asked how the number was checked.

**The discipline:** write a fact at the narrowest scope that is honest, and
promote it only when a second project proves it general. A fact promoted too
early becomes a rule you must remember to disbelieve.

### Loading is lazy, by design

`gt-open` reads the project's core docs and then reads **`memory/MEMORY.md` only**
— an index of one line per file. The memory files themselves are read on demand.

A project with 70 notes costs ~80 lines to open instead of ~2,000. This is why
`MEMORY.md` descriptions matter: they are the entire basis for deciding whether a
file is worth opening.

---

## Vault layout

```
<vault>/
  CLAUDE.md              vault-wide conventions and page schema
  index.md               navigational index of Knowledge pages
  log.md                 audit trail
  review-queue.md        items flagged for owner review
  lint-declines.md       suppressions, each with its reason

  Sources/               IMMUTABLE raw originals
  Knowledge/             cross-project wiki pages
  global-memory/         loaded in every session
    MEMORY.md            the index — this is what actually gets read

  Projects/
    README.md            master list + Dataview views
    CONVENTIONS.md       stages, properties, domain taxonomy, file roles
    PROTOCOL.md          cross-project process rules
    INFRASTRUCTURE.md    the server fleet, defined ONCE

    <slug>/
      README.md          status board + property frontmatter
      source.md          where the code lives + deploy plan
      idea.md            original brain dump — IMMUTABLE
      research.md        append-only dated findings
      decisions.md       append-only numbered ADRs
      design.md          current architecture
      spec.md            handoff artifact (when design is complete)
      runbook.md         operational procedures (optional)
      memory/
        MEMORY.md        index — the only thing gt-open reads
        <topic>.md       loaded on demand
```

### Project properties

Every project `README.md` carries frontmatter. These are real Obsidian
properties — they drive the tag pane, search, and the Dataview views.

```yaml
---
type: project
slug: my-project          # must match the folder name
domain: trading           # coarse grouping
stage: active             # idea|research|design|active|complete|archived
topology: bastion-direct  # local|remote|bastion-jump|bastion-direct|n/a
tags: [trading, platform, live]
parent: other-slug        # sub-projects only
---
```

> **Categorise with properties, not folders.** Do not add a category level under
> `Projects/`. `gt-lint` walks projects to find `memory/` directories; a category
> folder is fine for *that* check since v0.3.0, but folders also fragment
> `gt-open`'s project lookup and buy nothing Obsidian doesn't already give you
> through properties. The one legitimate second level is a real sub-project
> created with `--parent`.

---

## Source topology

`source.md` records **where the code actually lives**. It is read first by
`gt-open`, because acting without it is how you edit the wrong box.

| Topology | Meaning |
|---|---|
| `local` | A folder on this machine. No deploy step. |
| `remote` | One repo, one server. Repo mirrors the prod structure; the server pulls from GitHub. |
| `bastion-jump` | Several servers, all reached through one gateway host. |
| `bastion-direct` | Several servers, each reachable independently. |

Addressing is **role × env → host + path**, because one machine can serve several
vhosts and the same role exists in more than one environment.

Bastion topologies also carry a **file plan**, marking each deployed file:

- **static** — byte-identical on every target. Deploy from one canonical copy.
- **unique** — a per-server variant. Never copy one host's version over another's.

That distinction is the whole point. Two files with the same name on two hosts
may be the same file that must not drift, or different files that must not be
merged — and nothing but the file plan tells you which.

The fleet itself is defined **once** in `Projects/INFRASTRUCTURE.md` and linked
from each `source.md`. Never copy the host table into a project.

---

## Typical use cases

Recipes for the situations that actually recur. Each names the command; the
per-command detail follows in the sections below.

### Starting something new

```
/gt:gt-create   →   work   →   /gt:gt-work
```

`gt-create` gathers slug, domain, topology and tags, scaffolds the folder, and
writes `idea.md` from what you actually said. **`idea.md` is immutable after
that** — it is the traceable "why", and the one file you never rewrite when the
plan changes. Close the session with `gt-work` or the session's findings die with it.

### Picking up a project after time away

```
/gt:gt-open <slug>
```

Reads the project docs in dependency order and stops at `memory/MEMORY.md`. Skim the
returned summary for **stage**, **hosts** and **blockers** before touching anything —
a stale host entry is cheapest to correct before work starts. Then pull individual
memory notes by name as the work needs them.

### "What should I work on?"

```bash
python3 <vault>/Projects/golden-thread/tools/gt_tasks.py   # then read TASKS.md
```

**Regenerate first, always.** Effective priority is computed against the clock — a
`p:: 1` older than seven days escalates, a `due` inside three days escalates, an open
`pp_escalate` window escalates. Reading a stale `TASKS.md` gives you last week's
ranking with this week's confidence.

`waiting:: user` is your list; `waiting:: agent` is the session's.

Two sections sit above the ranked lists. **Inbox** shows every unchecked line in
`INBOX.md` — thoughts captured from anywhere and not yet filed — so nothing is lost
and nothing unfiled ranks. **Review** shows projects the close-out probe thinks may
be finished, with the reasons, so closing a project is asked about rather than
forgotten (see *Closing a project*).

Tasks at `p:: 7` or higher are **shelved**: kept in the README as a record, excluded
from every section and every escalation rule, counted in *Project standing* only.

### Bringing an existing project into the vault

```
/gt:gt-ingest
```

Copies — never moves or deletes. If the notes cross-reference each other by
`[[wikilink]]`, **count the links before renaming anything**; Obsidian resolves links
by filename, so a tidy-up destroys the graph. Prefer keeping filenames.

### You just learned something — where does it go?

```
/gt:gt-work          (end of session)
/gt:gt-promote       (when it proves general)
```

File at the **narrowest scope that is honest**. A finding that stopped changing
graduates from `memory/` to `research.md`; a truth a *second* project hit graduates to
`Knowledge/`. See "Where does this fact go?" below. Promotion is cheap; demotion is not.

### A number is about to be recorded as fact

```
/gt:gt-validate
```

Before it lands in `decisions.md`, `design.md`, `Knowledge/` or `global-memory/`, and
before any production change. The validator gets the claim, the rules and the
artifact — never your reasoning. Budget for the possibility that it comes back
**refuted**; that is the skill working, not failing.

### Answering "how does this work?"

```
/gt:gt-query <topic>
```

Reads `index.md`, follows wikilinks into `Knowledge/`, then falls back to grep and
project memory. If a page comes back `status: stale`, verify before acting on it.

### Keeping the vault honest

```
/gt:gt-lint            # broken links, orphans, unlisted memory, scope leaks
/gt:gt-runbook-lint    # facts duplicated across runbooks
/gt:gt-refresh         # upstream changes to Sources/
```

Run `gt-lint` after any structural change. Triage into three piles: *you broke it*
(fix now), *already broken* (record in `review-queue.md`), *false positive* (suppress
**with the reason**). The check that matters most is `memory-unlisted` — a memory file
missing from `MEMORY.md` is a file Claude will never load.

### Capturing a thought without leaving what you are doing

Add one checkbox line to `<vault>/INBOX.md`, from any session, in any project:

```
- [ ] Backtesting: try the nq_pullback set against a cold cache
```

No project, no priority, no date. Then go back to work. Later:

```
/gt:gt-review
```

reads the inbox (and daily notes, if you keep them), asks where each line belongs,
files it as a task or a project, and checks the inbox line off with a `→ [[slug]]`
pointer. An idea filed as a task gets `p:: 3` and no due date — a due date on an
idea makes the deadline rule rank it above real work.

### Closing a project

Delivery is not closure. A shipped project whose tasks stay open keeps escalating
through its past due dates and outranks live work; on 2026-09-05 a talk delivered
two days earlier held 24 of the top rows in the rollup.

```bash
python3 <vault>/Projects/golden-thread/tools/gt_closeout.py candidates
```

names projects whose signals say they may be finished: most open tasks past due,
most tasks checked with nothing urgent left, three quiet weeks, or nothing open. The
same probe runs at `/compact` and session end (setting `closeout_check`), and
`gt-work` asks after the last urgent task is checked. The question is always put to
you, never acted on.

Closing means: `stage: complete`, leftover tasks to `p:: 7` (shelved, not deleted),
`pp: 3`, a last `research.md` entry, and a look at what to promote. Every ask and
every answer is recorded in `Projects/golden-thread/closeout-signals.jsonl`;
`gt_closeout.py history` shows what "ready" has actually looked like for you, which
is what the thresholds get tuned against.

---

## Setup

### `/gt:gt-init`

First run, or wiring a new machine. Writes `~/.claude/vault-config.json` (the
pointer every other skill reads), scaffolds the vault, and adds a Golden Thread
section to `~/.claude/CLAUDE.md`. Also installs Core-rule enforcement hooks into
`~/.claude/settings.json` (`UserPromptSubmit` + `Stop` hooks) so that Core rules
are actively asserted, not merely stored. Idempotent.

For a vault that predates the Core-rule tier, run the install step separately:

```bash
python3 $SCRIPTS/vault_init.py install-core-rules --vault ~/my-vault
```

> `CLAUDE.md` is read at **session start**. A rule added mid-session does not
> apply until you restart — that looks like the rule being ignored.

### `/gt:gt-create`

Scaffolds a project. Gathers slug, title, tags, **domain**, sub-project parent,
runbook, and **topology**; runs the script; then fills `idea.md` from what you
actually said. `idea.md` is immutable afterwards — it is the traceable "why".

---

## Daily work

### `/gt:gt-open <slug>`

Loads a project and stops. It is **read-only** — nothing is written during loading.

Reads, in order: `CONVENTIONS.md` and `PROTOCOL.md` (once per session), the project
`README.md` — frontmatter first, since `domain`/`stage`/`topology`/`tags` is the
fastest read of where the project stands — then `source.md` → `idea.md` →
`research.md` → `decisions.md` → `design.md` → `spec.md` → `runbook.md`, the linked
fleet page, and finally `memory/MEMORY.md` — **the index only**. Individual memory
files load on demand.

**`source.md` leads the project docs deliberately.** It says which box serves which
role in which environment. Acting before reading it is how you edit the wrong host,
or overwrite a file that exists on three machines.

**`research.md` over ~200 lines is read by its headings**, plus the entries relevant
to the work at hand and the most recent few. It is append-only and grows without
bound; reading it whole crowds out the context the actual work needs.

Sub-projects load only when you name one, or the status board shows it as the active
item.

Announces the `review-queue.md` count once, then summarises stage, **topology and
hosts**, next action, blockers, and what memory exists but is unloaded — the host
list so you can correct a stale entry *before* work starts rather than after.

### `/gt:gt-route`

The middle of a session. `gt-open` runs before the session knows what it is; `gt-work`
runs after it is over. Between them sits the part where the work actually changes shape,
and nothing served it.

Run it whenever the ground has shifted, or whenever you cannot name what you are doing.
It reads only the conversation already in context — never project files, because reading
`research.md` to decide where a note goes costs more than the note — and answers four things:

1. **What this has become.** Restated in a sentence, with the drift named explicitly if
   the session no longer resembles what it opened with. This is most of the value: the
   drift is usually felt before it is articulated.
2. **Where the output belongs** — the [Where does this fact go?](#where-does-this-fact-go)
   table, applied to the material actually in hand, naming more than one destination when
   more than one is true.
3. **Whether the work is in the right place** — project loaded, harness (a real PTY belongs
   in a terminal; vault context belongs in Claudian), and whether the model matches the
   difficulty. It reports mismatches; it changes nothing.
4. **One next action.** A skill to invoke or a file to append to — not a menu.

It **writes nothing**, with a single exception: one `INBOX.md` checkbox line, which is
what `INBOX.md` is for. Everything else is handed to the skill that owns the destination,
which does the writing under its own claim.

**"Nothing, keep going" is a valid answer** and the skill is built to give it. A check that
always finds work is a check nobody runs twice.

Use it when a session opened as one thing and became another; when a finding might belong
to a project other than the one loaded; when you are about to write something down and are
not sure which file; or when you simply feel lost. Being wrong costs one re-run.

### `/gt:gt-work`

End of session. The step people skip, and skipping it is what makes the vault decay.

| Goes to | What belongs there |
|---|---|
| `research.md` | Dated findings and gotchas. **New discoveries only.** |
| `decisions.md` | Numbered ADR: decision, context, rejected alternatives. **Stable choices only.** |
| `design.md` | Rewritten in place if architecture changed. Describes *now*. |
| `memory/*.md` | Session state, updated in place. |
| `spec.md` | Created when design is complete enough to hand off. |

Memory files carry `level` and `enforcement` in frontmatter to make a rule's
durability explicit:

```yaml
metadata:
  type: core | feedback | user | reference
  level: core | context | generic      # default: generic
  enforcement: validated | reminder    # required only for level: core
```

Default to `generic`. Do **not** set `level: core` here — Core is a promotion,
and it requires wiring an enforcement hook, which is `gt-promote`'s job.

After writing, `gt-work` actively asks whether any finding applies beyond this
project and should go to `Knowledge/`, `global-memory/`, or a new project.

Also updates the `stage:`, `topology:`, and `domain:` properties in `README.md`
when the project's reality changes.

**Not in `decisions.md`:** anything you might change next session. An ADR you
reverse next week teaches the vault to lie to you.

### `/gt:gt-query`

Reads `index.md`, follows wikilinks 2–3 hops into `Knowledge/`, falls back to
grep, then project memory. Be specific. If a result is `status: stale`, verify it.

### `/gt:gt-ingest`

Bulk-imports an existing project. Copies — never moves or deletes.

**The trap:** if the notes cross-reference each other by `[[wikilink]]`, renaming
or merging files destroys that graph, because Obsidian resolves links by
filename. Measure first:

```bash
grep -roh '\[\[[^]]*\]\]' --include='*.md' . | wc -l
```

Prefer keeping filenames. If you must repoint, match the *whole* link —
`[[name]]` and `[[name|alias]]` — never a substring, or `[[foo]]` will corrupt
`[[foo_bar]]`. Never repoint links inside `Sources/`.

### `/gt:gt-review`

Empties `INBOX.md`: reads every unchecked line (and daily notes, if configured),
asks where each belongs, files it as a task or project, and checks the inbox line
off with a pointer. Regenerates the rollup at the end.

---

## Context management

### `/gt:gt-farm`

Routes work out of this context to an external AI service when work is bulk,
mechanical, or genuinely benefits from a non-Claude second opinion.

The unit is a **work packet**: self-contained, with a strict return contract
(`FINDINGS` + `GAPS` blocks). The transport is swappable — the user pastes it
into a web UI today; a script sends it to an API later. The packet is identical
either way.

**All four gates must pass before a task leaves.** Any failure means the work
stays here.

| Gate | Question |
|---|---|
| **Stateless** | Answerable with no vault or repo state? |
| **Self-contained** | Fits in a paragraph plus a list of URLs? |
| **Checkable** | Verifiable without redoing the work? |
| **Releasable** | Every input safe to hand a third party? |

**Releasable is default-deny.** Never include: account identifiers, balances,
hostnames, internal IPs, credential locations, non-public source code, or
personal identifiers.

Key rules:
- One packet, one task. A packet asking three questions returns three half-answers.
- Supply a vocabulary block with exact model IDs, product names, and version
  numbers read from the system — never from memory.
- Results come back as `unverified`. Promote a finding only after opening its
  `source:` URL and confirming the claim.
- `GAPS` is mandatory and `GAPS: None` must be justified, not asserted.
- Cite the specific page carrying the claim, never a homepage.

Packets are saved to `<vault>/Projects/external-ai-tools/packets/<YYYY-MM-DD>-<slug>.md`.

---

## Knowledge management

### `/gt:gt-promote`

The judgement call: **has a second project proved this general?**

| From → To | When |
|---|---|
| `memory/` → `research`/`decisions` | The finding stopped changing session to session |
| project → `Knowledge/` | A second project hit the same truth |
| `Knowledge/` → `global-memory/` | Needed in *every* session, *every* project |
| anywhere → new project | The idea doesn't belong where it is |

Nothing is deleted. Retiring sets `status: stale` and records what superseded it.

Promoting a rule to Core (`level: core`) also wires the enforcement hook —
`gt-promote` handles this; `gt-work` does not.

### `/gt:gt-refresh`

Checks `Sources/` for upstream changes. Supersedes with a **new** immutable file
carrying `supersedes:` — never edits the old one.

---

## Verification

### `/gt:gt-validate`

Verify a claim by **re-deriving** it, not by reviewing it. Use before a finding is
recorded as fact, before a production change, or whenever a number matters.

**The one rule: the validator never receives the reasoning that produced the claim.**
Give it the reasoning and it grades the argument — inheriting the same blind spot.
Give it only the claim, the rules and the artifact, and it has to go back to primary
sources. That is the only thing that catches a wrong premise.

Reduce the work to a **single falsifiable assertion**. "The analysis is sound" is not
validatable; "widening NQ's target to 1.25 improves risk-adjusted return" is. Validate
several claims **separately** — a bundled claim returns a bundled verdict, which hides
which part failed.

Loads the project's `validation-rules.md`, and the parent's too, since packs are
inherited. Those are standing invariants the validator enforces whether or not the
request mentions them — because *a requester who has already made a domain error will
not think to ask the validator to check for it.* On conflict, the pack wins.

| Class | Use when the risk is |
|---|---|
| `empirical` | A number, measurement or result could be wrong |
| `vantage` | The measuring position may not be able to observe the answer |
| `rule-compliance` | Work must satisfy `core-rules/` or `CONVENTIONS.md` |
| `code` | Code may not do what its name, comment or docs claim |

Pick every class whose risk is present. **Do not substitute one generic reviewer** —
that produces agreement, not verification.

The packet is exactly three fields — `claim`, `rules`, `artifact` — then re-read it
and strip your conclusions, your numbers, your confidence, and any adjective implying
the expected answer. "Confirm that X" becomes "determine whether X". Withholding
*reasoning* is isolation; withholding *access* is just a broken validator, so keep the
hostnames, API shapes and filters it needs to reach the artifact alone.

Run the validators **independently**, and read their derivation *before* re-reading
your own — reading yours first turns the exercise back into review.

Three verdicts: **confirmed**, **refuted** (quantify the divergence), and
**cannot-verify** (name what was missing).

Append the verdict to `research.md`. A refutation supersedes the original finding.

> **`cannot-verify` is never a pass.** A validator that could not check something and
> stayed quiet manufactures false assurance — worse than no validator at all.

**You are the worst possible author of this packet**, because you already know the
answer and your framing leaks. Treat building it as an adversarial exercise against
yourself. A validator disagreeing is a *result*, not a failure.

---

## Maintenance

### `/gt:gt-lint`

| Check | Catches |
|---|---|
| `index-gap` | Knowledge page not in `index.md` — `gt-query` won't find it |
| `broken-link` | `[[wikilink]]` resolving to nothing |
| `orphan` | Knowledge page linked from nowhere |
| `memory-unlisted` | `memory/*.md` not in `MEMORY.md` — **Claude will never load it** |
| `global-gap` | `global-memory/*.md` not in its index |
| `memory-bloat` | `global-memory` file over 30 lines |
| `global-scope-leak` | Project-specific content in `global-memory/` |
| `superseded-cited` | Knowledge page citing a superseded source |
| `stale` | Knowledge page marked `status: stale` |
| `source-todo` | `source.md` with no topology or deployment targets |
| `frontmatter` | Project README missing properties, or slug ≠ folder |
| `core-misplaced` | A rule declares `level: core` but lives outside `core-rules/` |
| `core-no-enforcement` | `level: core` with no `enforcement` field declared |
| `core-unenforced` | The declared enforcement hook is not actually wired |

`core-unenforced` is the critical one — it is the machine-checkable form of "rule
stored but never asserted". Treat it as a real defect. **Do not suppress it** —
suppressing `core-unenforced` recreates the original bug with a paper trail saying
it was fine. Fix: `vault_init.py install-core-rules --vault <vault>`.

Declines go in `lint-declines.md` as `suppress:` **with the reason**.

Triage findings into three piles: *you broke it* (fix now), *already broken*
(record in `review-queue.md` — don't guess at a target), *false positive*
(suppress with reason).

### `/gt:gt-settings`

View and change what Golden Thread does automatically. Every automatic behaviour
is registered here and can be switched off.

| Setting | Values | Default | What it does |
|---|---|---|---|
| `component_updates` | `off` · `report` · `confirm` · `auto` | `report` | At session start, compares installed hooks/scripts against plugin source and reports drift |
| `version_check` | `off` · `report` | `report` | At session start, reports when a newer plugin version is checked in than the one installed |
| `orphan_check` | `off` · `report` · `reap` | `report` | At session start, looks for abandoned background Claude workers; `reap` stops them |
| `push_check` | `off` · `report` | `report` | At session start, reports vault commits not yet pushed |
| `report_card` | `off` · `minimal` · `full` | `minimal` | At `/compact`, summarises session hygiene |
| `watch` | `off` · `report` | `off` | `/gt:gt-watch`: the hourly fetch and the session-start report of repo changes |
| `install_demo` | `yes` · `no` | `yes` | Whether `install.sh` installs `/gt:gt-demo`, its script and the PizzaBot template |

```bash
python3 $SCRIPTS/gt_settings.py show            # current state of all settings
python3 $SCRIPTS/gt_settings.py explain <name>  # reasoning behind the default
python3 $SCRIPTS/gt_settings.py set <name> <value>
```

`component_updates` at `auto` will not overwrite an installed file that is newer
than the source, and will not delete a file the source lacks — real drift ran
that direction, so a naive "source is truth" updater would silently break
enforcement on every session start.

### `/gt:gt-runbook-lint`

Finds procedures duplicated across runbooks and routes them to the right shared
layer — `PROTOCOL.md`, a Knowledge page, or a repo `CLAUDE.md`.

---

### `/gt:gt-watch`

Watch any git repo you depend on, and hear about it when it changes in a way you said
you care about — raised as a **P0** when it ships a security fix.

```
/gt:gt-watch add <git-url>   — create a watch (any git URL: GitHub, GitLab, self-hosted, file://)
/gt:gt-watch list            — every watch and its last change
/gt:gt-watch check           — fetch now instead of waiting for cron
/gt:gt-watch show <slug>     — Claude reads the new commits and explains them
/gt:gt-watch ack [<slug>]    — mark changes seen
/gt:gt-watch remove <slug>   — stop watching
```

**A watch is a note** — `Projects/golden-thread/watches/<slug>.md`, editable in Obsidian's
Properties panel. Its frontmatter says what to watch: `url`, `track` (commits, tags,
releases), `branch`, `watch_paths`, `current_version`, your own `p0_when` patterns, and
`starred`.

**How it runs.** A cron job (`gt_watch.py install-cron --every 1h`) fetches every watch
without Claude, classifies each change, and queues it. At session start a hook reads the
queue — P0s first, spelled out; review items one line each; routine changes as a count. It
never fetches. Machine state lives in `~/.claude/golden-thread/watch/`, outside the vault.

**When it is a P0** — decided by rules, never by a model reading commit prose: a new
security advisory for the repo; a new tag or release naming a CVE or GHSA id, or saying
"security fix/release/update" or "vulnerability"; a commit message carrying a CVE or GHSA
id; a change to `SECURITY.md` or your `watch_paths` that says the same; or anything
matching your own `p0_when`. A new release, a major-version jump, a change under
`watch_paths`, or "BREAKING CHANGE" is a **review** item. Ordinary words like "auth" or
"security" in a commit message are not enough — a P0 that cries wolf stops being read.

Off until you switch it on: setting `watch` (`off` · `report`). `add` offers to switch it
on and install the cron entry.

---

### `/gt:gt-demo`

An eleven-act guided tour of Golden Thread on PizzaBot 3000, a fictional pizza-ordering
project. It runs in its **own throwaway vault**, so nothing it does can reach yours.

```
/gt:gt-demo start    — build the demo vault and print the command that opens it
/gt:gt-demo tour     — (in the demo session) run the tour; you only click Next
/gt:gt-demo end      — every commit and file the tour produced
/gt:gt-demo clean    — delete the demo vault and build a fresh one
/gt:gt-demo remove   — delete the demo and switch it off (install_demo = no)
/gt:gt-demo status   — is there a demo vault, and how old is it
```

**Running it:** `start` builds the vault at `~/.claude/golden-thread/demo-vault` and prints
`cd <demo vault> && GT_VAULT=<demo vault> GT_WATCH=report GT_WATCH_STATE=<demo vault>/.demo/watch claude`
— the extra variables let the watch act run without touching this machine's watch
settings or state. Run that in a new terminal and type
`/gt:gt-demo tour`. For each act Claude says one line to the audience, does the work with
the real skill, says what just happened, and offers buttons — **Next**, **Repeat this
act**, **Skip ahead**, **End tour**.

**The acts:** Core rules enforced (a prepared reply carrying a key is blocked) · open a
project · "what's next?" from the task rollup · capture a finding and an ADR mid-session ·
ingest a source and query the wiki · route a stray idea · validate a wrong claim ·
lint finds a planted broken link · promote a finding to a Knowledge page (which fixes the
link) · watch an upstream library and see its security release open as a P0 · file the inbox, close the session, and show the receipt. The acts are plain text
in `templates/demo-pizzabot/tour.md` — reorder or reword them there.

**Why its own vault:** a fuller tour writes to shared files (`INBOX.md`, `TASKS.md`,
`log.md`, `Knowledge/`) that other sessions also write. Undoing that in a real vault
means rewinding history everyone shares. In a throwaway vault, `clean` just rebuilds it.
The demo never writes your `vault-config.json`: the demo session is pinned with
`GT_VAULT`, which every skill and hook honors. `clean` and `remove` delete a directory
only if it carries the demo's marker file.

---

## Where does this fact go?

| The fact | Goes to |
|---|---|
| "This preset is wrong right now" | `memory/` |
| "This API rejects plural keys" | `research.md` |
| "We use POST for all mutations, and why" | `decisions.md` |
| "The system is structured like this now" | `design.md` |
| "This platform behaviour bit two projects" | `Knowledge/` |
| "Always diff before overwriting prod" | `global-memory/` |
| "How to restart the prod backtest" | `runbook.md` |
| "Never scp without backing up" | `PROTOCOL.md` |
| "This code lives on host X at path Y" | `source.md` |
| "Which box serves which role" | `INFRASTRUCTURE.md` |

Two failure modes, opposite directions: **too narrow** buries a platform truth in
one project, so the next rediscovers it the hard way; **too wide** promotes a
quirk into `global-memory/`, where it loads into unrelated sessions as a rule
that isn't true there. When unsure, file narrow — promotion is cheap, demotion isn't.

**`/gt:gt-route` applies this table for you**, against the material actually in hand,
without your having to hold the table in your head mid-session. Reach for it when the
answer is not obvious — including when the honest answer is that the fact belongs to a
project other than the one loaded.

---

## Script reference

```bash
SCRIPTS=~/.claude/plugins/cache/golden-thread-plugin/gt/0.10.0/scripts

python3 $SCRIPTS/vault_init.py fresh --vault ~/my-vault --domain "My Team"

python3 $SCRIPTS/vault_init.py create-project --vault ~/my-vault \
  --name my-project --title "My Project" --tags "backend,api" \
  --domain platform --topology bastion-direct \
  --repo-url git@github.com:me/my-project.git --runbook

python3 $SCRIPTS/vault_init.py create-project --vault ~/my-vault \
  --name sub-feature --parent my-project --domain platform

python3 $SCRIPTS/vault_init.py connect --vault ~/existing-vault

python3 $SCRIPTS/vault_init.py install-core-rules --vault ~/my-vault

python3 $SCRIPTS/gt_ingest.py ~/Projects/my-project --json

python3 $SCRIPTS/gt_lint.py ~/my-vault --queue ~/my-vault/review-queue.md

python3 $SCRIPTS/gt_settings.py show
```

`gt_tasks.py` runs **from the vault**, at `Projects/golden-thread/tools/`, beside
`gt_closeout.py`, `gt_session.py`, `gt_edits.py` and `safe_write.py`. All five ship as
templates and are seeded there by `vault_init.py fresh` or `connect` (what
`/gt:gt-init` runs); `install.sh` refreshes copies that predate the installed
template. It regenerates `TASKS.md` from every project's `## Tasks` section.

```bash
python3 ~/my-vault/Projects/golden-thread/tools/gt_tasks.py
```

It infers the vault from its own location, so it needs no arguments; `--vault` is
there to override that. **Re-run it before reading `TASKS.md`** — project priority is
computed against the clock (stale-P1 ageing, deadline windows, `pp_escalate`), so the
ranking changes with time even when no file has changed. `TASKS.md` is generated and
must never be hand-edited.

---

## Troubleshooting

**"vault-config.json not found"** — run `/gt:gt-init`.

**A rule in `CLAUDE.md` seems ignored** — it is read at session start. Added
mid-session, it applies from the *next* session. Same for hooks in
`settings.json`: open `/hooks` once or restart.

**Memory files aren't being read** — that is intended. `gt-open` reads
`MEMORY.md` and loads files on demand. Ask for one by name.

**A memory file is never offered** — run `/gt:gt-lint`; it is probably
`memory-unlisted`, so nothing knows it exists.

**`/gt:gt-query` can't find a page you added** — likely `index-gap`.

**Lint suppression isn't working** — paths are matched case-insensitively against
the finding path; make sure you used the path as the finding reports it.

**"conflict: vault-config.json points to a different vault"** — run
`vault_init.py connect --vault <new-path>`.

**`core-unenforced` finding in lint** — Core rules are stored but not being
asserted. Run `vault_init.py install-core-rules --vault <vault>` or wire the
hooks by hand. Do not suppress this finding.
