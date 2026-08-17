# {{DOMAIN}} Knowledge Wiki

This vault is the single source of truth for AI memory across all projects.

## How to Read This

- `Knowledge/` — wiki pages. Each page is self-contained. Follow `[[wikilinks]]` for related topics.
- `Sources/` — immutable raw input (meeting notes, research dumps). Never edit these.
- `global-memory/` — facts loaded in every Claude Code session, regardless of project.
- `Projects/<slug>/` — per-project files. See `Projects/CONVENTIONS.md` for structure.
- `log.md` — audit trail of all Golden Thread operations.
- `index.md` — navigational index of all Knowledge pages.

## Knowledge Page Schema

Every page in `Knowledge/` must have this frontmatter:

```yaml
---
title: <descriptive title>
category: runbook | decision | reference | concept
tags: [comma, separated]
sources: []
created: YYYY-MM-DD
updated: YYYY-MM-DD
status: seed | growing | mature | stale
---
```

**Status vocabulary:**
- `seed` — newly created, may be incomplete
- `growing` — actively being refined, trust but verify
- `mature` — stable, well-sourced, high confidence
- `stale` — superseded or no longer accurate; do not use without verification

## Promotion Workflow

Knowledge moves up this hierarchy:

| Level | Location | Contents |
|---|---|---|
| 1 | session conversation | ephemeral — captured with `/gt-work` |
| 2 | `Projects/<slug>/memory/` | project-scoped session notes |
| 3 | `Projects/<slug>/decisions.md` | stable ADRs |
| 3 | `Projects/<slug>/research.md` | dated findings |
| 3 | `Projects/<slug>/design.md` | current architecture |
| 4 | `Knowledge/<page>.md` | cross-project platform knowledge |
| 5 | `global-memory/` | loaded in every session |
| **6** | **`core-rules/`** | **rules enforced by hooks on every turn, every project** |

Levels 1–5 hold **facts** and are read on demand. Level 6 holds **rules** and is
pushed into every turn by a hook.

Use `/gt-promote` to graduate items between levels.
Use `/gt-lint` to check for gaps, broken links, and stale pages.

## Golden Thread Commands

| Command | Purpose |
|---|---|
| `/gt-init` | Set up vault and wire to a project |
| `/gt-ingest` | Import an existing project's memory |
| `/gt-work` | Write back session findings |
| `/gt-promote` | Graduate facts up the hierarchy |
| `/gt-lint` | Audit vault health |
| `/gt-query` | Look up a topic |

## Core rules

Core rules live in `Projects/golden-thread/core-rules/` and are enforced by hooks on
every turn, in every project — the top of the promotion hierarchy, above
`global-memory/`. Each carries `level` and `enforcement` in its frontmatter.

Do not duplicate Core rules elsewhere; point at that folder. See
`core-rules/core_rule_priority_model.md` for the model and `core-rules/enforcement.md`
for the hook wiring.
