---
name: gt-allin-commit
description: "Commit once checks pass and a test receipt covers every staged file. Refuses without evidence or on the default branch; never pushes. Use when the user says: commit it, commit once checks pass."
plain:
  - cmd: "/gt:gt-allin-commit"
    does: "Commits your staged work once the checks pass."
    how: "Refuses unless a passing test receipt covers every staged file, refuses on the main branch, and never pushes."
    detail: "reads the test receipts under ~/.claude/golden-thread; runs git commit only on your yes."
    when: "After /gt:gt-allin is clean and you want the commit made."
    next: "/gt:gt-sync"
model_intent: balanced
---

# Golden Thread All-In Commit

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_allin.py …`, `gt_allin_commit.py …` | **terminal**: the tests member writes its receipt into `~/.claude/golden-thread/` and the vault members read the vault — both refused inside the sandbox. Meanwhile `vault_report` (`lint`, `runbook_lint`, `optimize`, `doctor`) shows the vault members' findings |

A **terminal** row is a step sandbox mode refuses on purpose — it changes settings or install state, or rewrites the vault wholesale. Give the user the exact command to run from a terminal, and carry on with the rest.

**When `.git` is not writable** (the sandbox blocks writes to it, so `git add` or `git commit` fails with `Operation not permitted` or "Unable to create '.git/index.lock'"): do not retry, and do not work around it with another route. Once the receipt and the checks have passed and the user has said yes, print the commands for them to run, each naming the repo, and carry on with the rest:

```
run this from a terminal:
git -C "<repo>" add <the staged paths>
git -C "<repo>" commit -m "<the message>"
```

Print them only after the user's yes. Never print a push; Step 4 stays the user's act.

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
  Since 0.21.0 findings from `lint`, `optimize`, `runbooks`, `wiki` and `doctor` — which judge
  the vault or the install, not the staged code — are printed as **ADVISORY** and do not block
  a commit to a code repository (they still block a commit in the vault itself). Tell the user
  they exist; do not fix the vault as part of the commit.
- **no passing test receipt covers every staged file** — run the tests. Editing a file after a
  run invalidates the receipt, which is the point.
- **you are on `main`/`master`** — branch first, or pass `--allow-default-branch`.
- **the change is outside the plan's scope contract** (0.21.4, only with `scope_gate` set to
  `block`) — when the repository has an approved plan for this branch with a `scope:` block, the
  run prints a `--- scope ---` block listing every staged file or new dependency outside it,
  `JUSTIFIED` (the plan's `## Deviations` has a line, shown beside it) or `UNJUSTIFIED`. Under the
  default `warn` it only prints them. `--allow-findings` does **not** accept an unjustified
  deviation: show the user the items and let them choose — take the change out, or write the
  reason in `## Deviations` (propose the line; never add one to make the check pass). No plan, a
  draft, or a plan for another branch prints `not applicable` and never refuses. A plain `git commit`
  in a Bash call meets the same contract in the PreToolUse guard `guard_scope_contract` (denied under
  `block`, reported under `warn`); neither `GT_TEST_GATE=off` nor `.gt-no-test-gate` applies to it.

**A local second opinion (0.21.4, opt-in).** With `commit_local_review` on (or unset under `farm_first` auto with a
calibrated local engine), the run prints a `--- local review ---` block: the staged diff, scanned for credentials and
prompt injection first (any hit skips it, naming the kind), was read by a calibrated `local: yes` engine. It is
**advisory and never blocks**; `--allow-findings` has nothing to do with it. Its findings are **unverified
suggestions**: in the trial behind it 3 of 10 were real defects no test had caught and 7 needed Claude to dismiss.
Check each one against the diff before you tell the user it is real, and say which you could not confirm. An engine
with no calibration, or one the calibration marks not suitable for code review, is sent nothing.

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

## The release pipeline's pre-commit steps (0.18.1)

In a repo with a release pipeline (`release-pipeline.tsv`), the checks include the pipeline's
steps before the owner gate. A FAILED step refuses the commit and `--allow-findings` cannot wave
it through — a gate that did not pass is not a finding someone may accept. Fix the step (or, for
a default gate that does not apply, `gt_pipeline.py remove <id> --reason "..."`, which is
recorded), then run this again.

On a feature branch the test-receipt check accepts a **scoped** receipt -- from a test run
limited to the tests the change needs (`--affected`) -- for the files it covered (setting `scoped_receipts`); the default branch
and every release gate still need a full-suite receipt.
