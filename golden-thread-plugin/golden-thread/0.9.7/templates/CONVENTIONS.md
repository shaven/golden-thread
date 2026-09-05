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

## Core rules — the top of the hierarchy

Core rules are defined **and enforced** from `Projects/golden-thread/core-rules/`
and apply to every project. They sit above `global-memory/`.

A Core rule is not one written down more emphatically — it is one backed by a
mechanism that re-asserts it every turn. **"In context" does not mean "applied":**
rules fail because a salient task crowds them out of attention, not because they were
deleted. So a rule is only as durable as the mechanism that re-asserts it.

### Two independent axes

| Scope | Meaning |
|---|---|
| `core` | Every session, every turn, every project. Un-removable. |
| `context` | While a project / directory / skill context is active. |
| `generic` | Best-effort convention; yields under space pressure. Default. |

| Enforcement | Meaning | Depends on model discipline? |
|---|---|---|
| `reminder` | Re-injected each turn as an imperative | Yes |
| `validated` | Output is checked and a violation is blocked | **No** — the only unbreakable form |

### Frontmatter contract

```yaml
metadata:
  type: core | feedback | user | reference
  level: core | context | generic
  enforcement: validated | reminder    # omit for generic
```

### Enforcement wiring

| Tier | Hook | Script |
|---|---|---|
| Reminder | `UserPromptSubmit` | `~/.claude/golden-thread/hooks/inject_core_rules.sh` |
| Validated | `Stop` | `~/.claude/golden-thread/hooks/validate_response.sh` |

Wire at **user-global** `~/.claude/settings.json` for Core (applies everywhere).
Per-project wiring scopes a rule to that project — that is the Context tier.

The hooks live **outside the vault** — installed by `install.sh`, never stored in
`core-rules/`. `settings.json` references them by absolute path, so keeping them in
the vault meant a project rename or vault move silently broke enforcement. The scripts
locate the rules at run time, so editing a `core_*.md` changes what gets injected.

### Two ways into Core

| Path | Gate |
|---|---|
| **Designation** (primary) | None. The user names it Core and it is Core. No prior incident required. |
| **Promotion from levels 1–5** | Must pass the three-question test — see [[PROTOCOL]]. |

The gate asks, *if this rule were **not** enforced*: would it cause **incorrect code
or a change implemented wrongly** · would it cause **rework or a backout** · would it
**cascade** into lower-level rules being misrepresented or unfollowable. Any single
YES qualifies.

**Canary rules are designated, never tested.** A canary is kept because its absence is
*visible*, not because it is *costly* — small, cheap, and seen on every reply, so the
moment it stops appearing you know enforcement itself has broken. Every other Core
rule fails silently; a canary fails loudly. Never demote one for failing the gate.

**Keep the Core tier small.** The fewer Core rules, the more reliably each survives;
a bloated always-on tier dilutes attention on every rule in it.

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

## Memory file frontmatter

New memory files start from `templates/memory-file.md`, which carries `level` and
`enforcement` so every rule is tiered from birth. Unlabeled = `generic`.

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
| **Rules enforced on every turn** | `Projects/golden-thread/core-rules/<rule>.md` |

The last row is the only one holding **rules** rather than **facts**, and the only one
pushed into every turn by a hook rather than read on demand.

## Append-Only vs Iterative

**Append-only** (never edit old entries):
- `decisions.md` — each ADR is permanent; supersede with a new ADR, never edit the old one
- `research.md` — each finding is permanent; note supersession inline

**Iterative** (update in place):
- `design.md` — always describes NOW; history belongs in research.md
- `memory/*.md` — updated each session
- `Knowledge/*.md` — refine as knowledge matures; update `updated:` and `status:` in frontmatter
