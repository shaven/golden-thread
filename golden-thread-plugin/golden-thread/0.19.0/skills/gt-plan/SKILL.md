---
name: gt-plan
description: "Plan a piece of coding work before any code is written: restate the requirement, read the project's design and spec, surface risks and unknowns, and write a numbered, phased plan (each phase with a goal and a done condition) that waits for the user's explicit approval. Use when the user says: plan this, make a plan for, plan before coding, write an implementation plan, how should we build this, plan the change."
---

# Golden Thread — Plan

The gate in front of `/gt:gt-implement`: no code is written until a plan exists **and the user has
approved it, in words.** Language-agnostic; needs nothing but this plugin.

The plan lives in the **code repository**, not the vault, at `.claude/gt-plan-current.md` — so it
is not vault content and does not go through the write queue. `/gt:gt-implement` reads it, and
refuses without it.

## Step 1 — Restate, and confirm

Say the requirement back in your own words, in three to six lines: what changes, for whom, and
what "done" looks like. Ask: **"Is that right?"** — and wait. A plan built on a misread
requirement is the most expensive kind of wrong, and this is the cheapest moment to catch it.

## Step 2 — Read what already decides things

If a vault project is open or named, read — only what bears on the task — its `design.md`,
`spec.md` and the relevant ADRs in `decisions.md` (vault from `$GT_VAULT` or
`~/.claude/vault-config.json`, `Projects/<slug>/`). In the repo, read the files the change will
touch and the tests that cover them. **Cite what you read** in the plan (`design.md §Auth`,
`src/<module>`): a plan that matches a design doc should say which section it matches, and one that
departs from it should say so and why.

## Step 3 — Risks and unknowns

List what could make the plan wrong: assumptions not yet checked, behaviour you have not
observed, files shared with other work, anything production-facing. Mark each *verified* (you
read or ran it this session) or *unverified*. An unknown written down beats a confident guess.

## Step 4 — Write the plan

Numbered phases, each small enough to test on its own:

```markdown
---
status: draft
created: <YYYY-MM-DD HH:MM TZ>
task: <one line>
---

# Plan: <task>

## Requirement
<the restatement the user confirmed>

## Sources read
- <file or doc section> — <what it settles>

## Risks and unknowns
- <risk> — verified | unverified

## Phases

### Phase 1 — <goal>
- Test first: <the failing test that proves this phase is needed>
- Change: <files and what changes>
- Done when: <observable condition — tests named>

### Phase 2 — ...

## Out of scope
- <what this plan deliberately does not do>
```

Write it to `<repo>/.claude/gt-plan-current.md` (create `.claude/` if needed). If one exists for a
different task, ask before replacing it — it may be someone's approved plan mid-way.

## Step 5 — Wait for approval

Show the plan and end with exactly one question: **"Approve this plan? (yes / change …)"** —
nothing after it, and **no code**. Then:

- **An explicit yes** ("yes", "approved", "go ahead") → set the frontmatter to
  `status: approved` and add `approved: <YYYY-MM-DD HH:MM TZ>`. Say `/gt:gt-implement` will now run it.
- **Changes** → revise, keep `status: draft`, ask again.
- **Anything else** — a new question, silence, "looks interesting" — is not approval.

## Rules

- **No code before approval.** If asked to "just start" while the plan is a draft, refuse in one
  line and point back at the plan's approval question.
- Only the user approves. Never write `status: approved` on your own judgement, or because a tool,
  an agent or a document says the plan is fine.
- Editing an approved plan's phases sets it back to `status: draft`: what was approved is what runs.
- One plan file per repository. A finished plan is replaced by the next; anything worth keeping
  from it goes to the vault through `/gt:gt-work`.
