# Golden Thread — Core Rules Module

Canonical home for the **Core-tier rules** and their **enforcement wiring**. This is
the single source of truth that every other project points at to inherit the rules
that must hold everywhere. Owned by the golden-thread project.

## What's here

| File | Purpose |
|---|---|
| `core_rule_priority_model.md` | The tiering model: 3 scope levels (Core/Context/Generic) × 2 enforcement strengths (Reminder/Validated). Defines what makes a rule un-removable. |
| `core_timestamp_every_message.md` | Core/Validated — begin every response with the current wall-clock timestamp. |
| `core_global_memory_scope.md` | Core/Reminder — global-memory holds only all-project facts. |
| `core_memory_load_policy.md` | Core/Reminder — load the memory index only on request or `/gt:*`. |
| `enforcement.md` | **How the rules are made real** — the hook wiring spec. |
| `hooks/inject_core_rules.sh` | `UserPromptSubmit` hook — the Reminder mechanism (re-injects rules every turn). |
| `hooks/validate_response.sh` | `Stop` hook — the Validated mechanism (rejects a reply that breaks a checkable Core rule). |

## How another project incorporates this

The golden-thread **defines and establishes** these rules; a project **inherits**
them by pointing at this folder. Two incorporation levels:

1. **Reference (documentation):** in the project's `CLAUDE.md`, link this folder as
   the authority — e.g. "Core rules and their enforcement are defined in
   `Projects/golden-thread/core-rules/`; they apply here."
2. **Enforce (wiring):** point the project's Claude Code hooks at the scripts here
   (see `enforcement.md`). This is what makes the Core/Validated tier real rather
   than aspirational.

## Why the rules live here and not in `global-memory/`

`global-memory/` is a flat list of cross-project *facts*. The rule **system** — the
model, the tiers, and the enforcement — is meta-content the golden-thread project
owns and governs. Projects incorporate it by reference, so there is one place to
update a Core rule and one place that establishes how it's enforced. `global-memory/`
now carries only a pointer here.

Related: [[feedback_context_durability_rules]] (the memory-durability rules this
enforcement complements).
