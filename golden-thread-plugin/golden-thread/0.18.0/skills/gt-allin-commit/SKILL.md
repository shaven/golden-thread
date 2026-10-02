---
name: gt-allin-commit
description: "Commit once the checks pass and a test receipt covers every staged file. Refuses without evidence, refuses on the default branch, and never pushes."
---

# Golden Thread All-In Commit

The deliberate act, kept separate from the sweep.

## Why it is its own command

`/gt:gt-allin` reports and changes nothing, so it can be run freely by anyone at any time. This
one changes the repository. Keeping them apart means a routine check is never also a write, and
committing stays something a person chose.

## Why it commits but will not push

A commit is local and reversible — `git reset` undoes it and nobody else ever saw it. A push is
outward-facing and effectively permanent: other people fetch it, CI acts on it, and a bad commit
becomes a public fact. That asymmetry is the whole line, and there is no `--push` flag.

## Why it checks the receipt itself

`guard_test_before_commit` already denies a commit with no passing test receipt — but it is a
**PreToolUse hook**. It sees the model running `git commit` in a Bash call and never sees a
*script* running it through subprocess. A script that shelled out to git would walk straight
past the one mechanism enforcing `core_test_before_commit`, so the same check is made here,
directly, against the same ledger. Protection cannot depend on who is holding the pen.

**Cite Core rules by name, never by number.** The numbers in the injected list are positions,
computed each turn from the rules that are gated ON — switch `test_gate` off and every rule
below it shifts up one. This file said "Core rule 4" and "Core rules 4 and 10"; both happened
to be right, and both would have gone quietly wrong the moment a setting changed.

## Steps

**Step 1 — Stage what you mean to commit**

It commits the index, not the working tree. Nothing staged is a refusal, not a no-op.

**Step 2 — Run it**

```bash
python3 <base_dir>/../../scripts/gt_allin_commit.py \
    --repo "<repo>" --vault "<vault>" -m "your message"
```

Add `--dry-run` to run every check and commit nothing — the safe first move, always.

**Step 3 — Read the refusals**

It prints the staged file list *before* the verdict, then refuses for any of:

- **a check could not run** — fatal, and `--allow-findings` cannot wave it through. An unknown
  is not a finding someone can accept.
- **findings exist** — pass `--allow-findings` once you have decided they are acceptable.
- **no passing test receipt covers every staged file** — run the tests. Editing a file after a
  run invalidates the receipt, which is the point.
- **you are on `main`/`master`** — branch first, or pass `--allow-default-branch`.

**Step 4 — Push yourself**

It prints the command. Run it when you mean to.

## Rules

- **Never pass `--allow-findings` on the user's behalf.** Accepting a finding is a judgement
  about this specific change; it is theirs.
- **Never work around a missing receipt.** Run the tests instead. The receipt exists precisely
  because "I ran them" is the claim the rule was built to stop trusting.
- **Say which tests the receipt covers** when reporting a successful commit —
  `core_test_before_commit` and `core_verification_state` together mean naming the evidence,
  not just asserting it.
- If the user asks you to "just commit it", state what is refusing and let them decide. The
  refusals are the feature.
