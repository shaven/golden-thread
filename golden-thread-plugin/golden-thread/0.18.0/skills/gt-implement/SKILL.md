---
name: gt-implement
description: "Carry out an approved gt-plan one phase at a time, test first: write the failing test, make it pass, run the checks and name which tests ran, stop on any failure, and commit only on the user's yes. Refuses without an approved plan. Use when the user says: implement the plan, build it now, execute the plan, start implementing, run the approved plan, next phase."
---

# Golden Thread — Implement

Runs the plan `/gt:gt-plan` wrote and the user approved. It owns the *discipline* — phase by
phase, test first, stop on red — and **reuses** the checks and the commit gate gt already has
(`/gt:gt-allin`, `/gt:gt-allin-commit`) rather than carrying a second copy of either.

`<allin>` is `<base_dir>/../../scripts/gt_allin.py`, `<commit>` is
`<base_dir>/../../scripts/gt_allin_commit.py`. Vault from `$GT_VAULT` or
`~/.claude/vault-config.json`; `<repo>` is the code repository.

## Step 0 — Refuse without an approved plan

Read `<repo>/.claude/gt-plan-current.md`.

- **Missing** → stop. Say: *"No plan: `.claude/gt-plan-current.md` does not exist in this
  repository. Run /gt:gt-plan first."* Write no code.
- **Frontmatter not `status: approved`** (a draft, or approval removed by an edit) → stop. Say:
  *"The plan in `.claude/gt-plan-current.md` is not approved (status: <value>). Approve it in
  /gt:gt-plan first."* Write no code.
- Approved → say which phase is next (the first one not marked `- [x] done`) and go on.

## Step 1 — One phase at a time

For the current phase, in this order — the order is the point:

1. **RED.** Write the phase's test first, run it, and show it **failing** for the reason the plan
   gives. A test that passes before the change proves nothing about the change. If the phase
   genuinely cannot have a test (pure docs, a rename), say so and why, and go on.
2. **GREEN.** Write the least implementation that makes it pass.
3. **REFACTOR.** Tidy what you just wrote, with the test still green. Nothing outside the phase.
4. **Docs.** If the phase changed a documented interface (a CLI flag, a config key, a public
   function), update the doc that describes it in the same phase.

## Step 2 — Check, and name what ran

Run the repo's checks through all-in — it finds the test suite the way the commit guard does,
records the receipt `/gt:gt-allin-commit` needs, and reports how many members actually ran:

```bash
python3 <allin> --vault "<vault>" --repo "<repo>" --only tests,scan
```

Report **which tests ran** (the command, the count passed and failed) — never just "tests pass".
Then:

- **Any failure, or a member that could not run** → **stop**. Do not start the next phase. Show the
  failure and ask how to proceed. "N of M ran" with M−N missing is a stop too.
- All green → mark the phase `- [x] done <date>` in the plan file, summarise it in two lines, and
  **ask before moving to the next phase.**

## Step 3 — Commit, on a yes

When the user wants a commit (after a phase or at the end), stage what the phase changed and run
the commit gate, rehearsal first:

```bash
python3 <commit> --repo "<repo>" --vault "<vault>" -m "<message>" --dry-run
```

Show the staged files and the verdict, then **ask: "Commit this?"** Only on an explicit yes:

```bash
python3 <commit> --repo "<repo>" --vault "<vault>" -m "<message>"
```

It refuses without a test receipt covering every staged file, on the default branch, or with
findings — read its refusals to the user; they are the feature. It never pushes, and neither do you.

## Step 4 — Finish

After the last phase: all-in once more over everything, then tell the user what was built, which
tests prove it, and what the plan said was out of scope. Offer `/gt:gt-work` to capture what the
session learned — including its **learn** step, which looks for reusable patterns.

## Rules

- No approved plan, no code — Step 0 is not skippable.
- Test before implementation, in every phase that can have one.
- A red check stops the run. Never start phase N+1 on a failing phase N.
- Never commit without asking, never pass `--allow-findings` or `--allow-default-branch` on the
  user's behalf, never push.
- The plan is the scope. Something the plan did not cover → stop and propose amending the plan
  (which returns it to `status: draft` for approval) rather than building it unasked.
