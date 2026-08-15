# Golden Thread — User Manual

Complete reference for all eleven skills. Written against **gt v0.3.0**.

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
research.md · decisions.md       settled findings and choices
      ↓  gt-promote
Knowledge/                       true for several projects
      ↓  gt-promote
global-memory/                   true everywhere
```

Facts move **up** as they prove general. They never move back down, and nothing
is silently deleted.

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
  CLAUDE.md              Knowledge page conventions and schema
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

## Setup

### `/gt:gt-init`

First run, or wiring a new machine. Writes `~/.claude/vault-config.json` (the
pointer every other skill reads), scaffolds the vault, and adds a Golden Thread
section to `~/.claude/CLAUDE.md`. Idempotent.

> `CLAUDE.md` is read at **session start**. A rule added mid-session does not
> apply until you restart — that looks like the rule being ignored.

### `/gt:gt-create`

Scaffolds a project. Gathers slug, title, tags, **domain**, sub-project parent,
runbook, and **topology**; runs the script; then fills `idea.md` from what you
actually said. `idea.md` is immutable afterwards — it is the traceable "why".

---

## Daily work

### `/gt:gt-open <slug>`

Reads `source.md` → `idea.md` → `research.md` → `decisions.md` → `design.md` →
`spec.md` → `runbook.md`, then the linked fleet page, then `memory/MEMORY.md` —
and **stops**. Individual memory files load on demand.

Summarises stage, topology and hosts, next action, blockers, and what memory
exists but is unloaded.

### `/gt:gt-work`

End of session. The step people skip, and skipping it is what makes the vault decay.

| Goes to | What belongs there |
|---|---|
| `research.md` | Dated findings and gotchas. **New discoveries only.** |
| `decisions.md` | Numbered ADR: decision, context, rejected alternatives. **Stable choices only.** |
| `design.md` | Rewritten in place if architecture changed. Describes *now*. |
| `memory/*.md` | Session state, updated in place. |
| `spec.md` | Created when design is complete enough to hand off. |

Also updates the `stage:` property when the project changes phase.

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

Scans Obsidian daily notes for uncaptured tasks and ideas, then promotes selected
ones into tracked projects.

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

### `/gt:gt-refresh`

Checks `Sources/` for upstream changes. Supersedes with a **new** immutable file
carrying `supersedes:` — never edits the old one.

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

Declines go in `lint-declines.md` as `suppress:` **with the reason**.

Triage findings into three piles: *you broke it* (fix now), *already broken*
(record in `review-queue.md` — don't guess at a target), *false positive*
(suppress with reason).

### `/gt:gt-runbook-lint`

Finds procedures duplicated across runbooks and routes them to the right shared
layer — `PROTOCOL.md`, a Knowledge page, or a repo `CLAUDE.md`.

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

---

## Script reference

```bash
SCRIPTS=~/.claude/plugins/cache/golden-thread-plugin/gt/0.3.0/scripts

python3 $SCRIPTS/vault_init.py fresh --vault ~/my-vault --domain "My Team"

python3 $SCRIPTS/vault_init.py create-project --vault ~/my-vault \
  --name my-project --title "My Project" --tags "backend,api" \
  --domain platform --topology bastion-direct \
  --repo-url git@github.com:me/my-project.git --runbook

python3 $SCRIPTS/vault_init.py create-project --vault ~/my-vault \
  --name sub-feature --parent my-project --domain platform

python3 $SCRIPTS/vault_init.py connect --vault ~/existing-vault

python3 $SCRIPTS/gt_ingest.py ~/Projects/my-project --json

python3 $SCRIPTS/gt_lint.py ~/my-vault --queue ~/my-vault/review-queue.md
```

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
