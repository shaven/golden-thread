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

There are **two ways into Core**, and only one of them is gated.

### Path 1 — Designation (primary)

The user designates a rule as Core. It is Core from that moment: no test applies, and
**no prior incident is required or wanted**. A rule that must be immutable is Core as
soon as that is known. Requiring it to fail first means accepting the failure, which
defeats the one tier whose entire purpose is that it never breaks.

### Path 2 — Promotion from a lower level (gated)

When an existing item at levels 1–5 is put forward for Core, answer all three
questions explicitly. Each is asked in the negative — *if this rule were **not**
enforced*:

1. **Correctness** — would it cause a misunderstanding by Claude that leads to code
   written incorrectly, or a change implemented wrongly, not at all, or in a way that
   is not allowed?
2. **Cost** — would it cause more work, or force backing out a solution already
   implemented?
3. **Cascade** — would it cause a cascade in which rules at the lower five levels are
   misrepresented, or are written such that they cannot or should not be followed?

**Any single YES qualifies the item for Core.** Three NOs means it stays where it is —
it may still be a sound rule, but it belongs at its current level.

Record the three answers in the rule file's body, so the tier is justified rather than
asserted.

The two paths are not redundant, and the current Core set proves it:

| Rule | Q1 | Q2 | Q3 | Entered by |
|---|---|---|---|---|
| `core_timestamp_every_message` | no | no | no | **Designation** — fails the gate, is Core anyway |
| `core_global_memory_scope` | no | no | **yes** | Either path |
| `core_memory_load_policy` | no | **yes** | no | Either path |

The gate governs *promotion*, not the tier. A rule the user requires is Core whether
or not it would ever have qualified on its own.

### Standing rules

- Promotion by either path means: set `level: core` and `enforcement`, move the file
  into `core-rules/`, wire the mechanism, and **verify it actually fires**.
- Observed drift is **not** an entry requirement. A rule that drifts was mis-tiered —
  drift is a diagnostic, not a qualification.
- A rule drops to **Generic** when it proves situational or is superseded. **Unwire
  the mechanism first**, then move the file and change the frontmatter; the reverse
  order leaves a hook pointing at a rule that no longer exists.
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
