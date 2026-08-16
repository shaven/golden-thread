---
name: core_rule_priority_model
description: "The tiering model for all Golden Thread rules — three scope levels (Core/Context/Generic) × two enforcement strengths (Reminder/Validated). Defines what makes a rule un-removable and how rules are promoted/demoted."
metadata:
  node_type: memory
  type: core
  level: core
  enforcement: reminder
---

# Rule Priority Model (Golden Thread)

The system that decides **which rules survive** a long session, a context switch, or
a memory compaction — and which can yield. Every Golden Thread rule should carry a
`level` and an `enforcement` in its frontmatter, per this model.

## The insight this is built on

**"In context" ≠ "being applied."** Rules almost never fail because they were deleted
from context — they fail because a salient immediate task crowds them out of
*attention*. (A rule can sit in `CLAUDE.md` the whole time and still be dropped for
many turns.) Therefore a rule is only as durable as the
**mechanism that re-asserts it**. Priority is defined by enforcement mechanism, not by
position in a list.

## Two axes

Every rule has a **scope** (how universally it applies) and an **enforcement**
(what guarantees it holds). They are independent.

### Axis 1 — Scope (3 levels)

| Level | Name | Definition | Guarantee | Mechanism |
|---|---|---|---|---|
| **1** | **Core** | Applies in *every* session, *every* turn, *every* project. Cannot be removed no matter what else loads. | Present and salient on every turn | Re-injected each turn by a `UserPromptSubmit` hook (not merely stored in a file) |
| **2** | **Context** | Applies only while a specific context is active (a project, a directory, a skill, a task mode). Un-removable *while that context is active*. | Present whenever the context is | Directory-scoped `CLAUDE.md`, a skill that loads its rules on activation, or a conditional hook keyed to a context marker |
| **3** | **Generic** | Best-effort working convention. May be summarized away or overridden under space pressure. | Loaded when space allows | Ordinary memory files / `CLAUDE.md` body, subject to compaction |

Default for an unlabeled rule = **Generic**. Most existing `feedback_*` memories are
Generic or Context.

### Axis 2 — Enforcement (2 strengths)

| Strength | What it is | Depends on model discipline? |
|---|---|---|
| **Reminder** | The rule is re-stated (ideally as an imperative) so it stays in attention. | Yes — still relies on the model applying it |
| **Validated** | A mechanism *checks the output* and blocks/flags a violation (e.g. a stop-hook that rejects a reply missing its timestamp). | **No** — the only truly unbreakable form |

A rule that must *never* break = **Core + Validated**. A rule that's important but not
mechanically checkable = **Core + Reminder** (strong, but not ironclad).

## The grid (scope × enforcement)

```
                 Reminder                         Validated
        ┌───────────────────────────┬───────────────────────────┐
 Core   │ re-injected every turn     │ re-injected + output-checked│  ← truly un-losable
        │ (e.g. memory-load policy)  │ (e.g. timestamp)            │
        ├───────────────────────────┼───────────────────────────┤
Context │ loaded while context active│ checked while context active│
        │ (e.g. project trade rules) │ (e.g. no-lookahead in backtests)
        ├───────────────────────────┼───────────────────────────┤
Generic │ loaded when space allows   │ (rare — validation implies  │
        │ (most feedback_* memories) │  it deserves a higher scope) │
        └───────────────────────────┴───────────────────────────┘
```

## Implementation in this environment

- **Core / Reminder** → add the rule's imperative to the `UserPromptSubmit` hook text
  so it is re-emitted every turn. Storing it in a file is *not* enough; the file can
  stay unread. Phrase as a command ("Begin your reply with…"), not a description.
- **Core / Validated** → additionally add a `Stop`/output hook that inspects the draft
  reply and rejects/annotates it if the rule was violated. Only use for mechanically
  checkable rules (a timestamp prefix, a required file-list, a forbidden token).
- **Context** → put the rule in the relevant scope: a directory-scoped `CLAUDE.md`, a
  `/gt:*` skill that loads the project's rules, or a hook that fires only when a
  context marker is present. It is un-removable *for the duration of that context*.
- **Generic** → an ordinary `feedback_*` memory indexed in `MEMORY.md`.

## Authoring guidelines

1. **Keep the Core tier small.** The fewer Core rules, the more reliably each survives.
   Promote to Core only what genuinely must hold on every turn everywhere.
2. **One rule per statement**, phrased as an imperative.
3. **Prefer Validated for anything cheap to check** — it removes reliance on discipline.
4. **Don't inflate scope to signal importance.** A hard trading constraint that only
   matters inside a backtest is Context, not Core — even though it's critical.
5. **Every promoted rule keeps its `Why:` and `How to apply:`** so the tier is
   justified, not asserted.

## Governance — promotion & demotion

- A rule earns **Core** when it has drifted/failed despite being stored, *or* the user
  designates it as always-on. Promotion means: set `level: core`, wire the enforcement
  mechanism (hook), and note it here.
- A rule drops to **Generic** when it proves situational or is superseded.
- Record every promotion/demotion in the rule file's body and re-index `MEMORY.md`.
- This model file is itself **Core** — the system for rules must be as durable as the
  rules.

## Frontmatter contract (add to every Golden Thread rule)

```yaml
metadata:
  type: core | feedback | user | reference
  level: core | context | generic
  enforcement: validated | reminder      # omit for generic
```
