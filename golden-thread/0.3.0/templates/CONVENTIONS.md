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

## Project Properties

Every project's `README.md` carries YAML frontmatter. These are real Obsidian
properties — they drive the tag pane, search, and the Dataview views in
`Projects/README.md`.

```yaml
---
type: project              # always "project" — what the Dataview queries select on
slug: my-project           # must match the folder name
domain: <grouping>         # the top-level grouping; see taxonomy below
stage: active              # see Project Stages above
topology: bastion-direct   # local | remote | bastion-jump | bastion-direct | n/a
tags: [domain, more, tags]
parent: other-slug         # sub-projects only
---
```

### Categorise with properties, not folders

**Do not add a second folder level under `Projects/`.** `gt_lint`'s
`memory-unlisted` and `global-scope-leak` checks walk `Projects/` one level deep,
so a project nested under a category folder has its unlisted memory files
silently ignored — the check that catches "Claude will never load this file"
stops working.

`domain` + `tags` give the same grouping with none of that cost, and Obsidian
resolves `[[wikilinks]]` by filename regardless of folder anyway.

The one legitimate second level is a genuine **sub-project** created with
`--parent`, where the nesting expresses ownership rather than category.

### Domain taxonomy

Define your own. Keep it short — `domain` is the coarse axis, `tags` are the
fine-grained multi-value one. A project has exactly one `domain` and any number
of `tags`. If two projects land in a catch-all domain, that is the signal to add
a real one.

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
| Session scratch notes | `memory/<file>.md` |

## Append-Only vs Iterative

**Append-only** (never edit old entries):
- `decisions.md` — each ADR is permanent; supersede with a new ADR, never edit the old one
- `research.md` — each finding is permanent; note supersession inline

**Iterative** (update in place):
- `design.md` — always describes NOW; history belongs in research.md
- `memory/*.md` — updated each session
- `Knowledge/*.md` — refine as knowledge matures; update `updated:` and `status:` in frontmatter
