---
name: gt-implement
description: "Carry out an approved gt-plan phase by phase, test first; stop on any failure, name the tests that ran, commit only on the user's yes. Use when the user says: implement the plan, build it now, execute the plan, run the approved plan, next phase."
plain:
  - cmd: "/gt:gt-implement"
    does: "Carries out an approved plan one phase at a time, tests first."
    how: "Writes the failing test, makes it pass, runs the checks and names which tests ran. It stops on any failure, commits only on your yes, and refuses without an approved plan."
    when: "A plan from /gt:gt-plan is approved."
    next: "/gt:gt-allin"
    hear: "implement the plan|build it now|execute the plan|run the approved plan"
model_intent: deep
---

# Golden Thread — Implement

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_allin.py …`, `gt_allin_commit.py …` | **terminal**: the tests member writes its receipt into `~/.claude/golden-thread/` and the vault members read the vault — both refused inside the sandbox. Meanwhile `vault_report` (`lint`, `runbook_lint`, `optimize`, `doctor`) shows the vault members' findings |
| the plan file (`<scope> plan --path`) | unchanged: it is in the repo's git directory |
| `gt_settings.py models advise` | **terminal**: it reads the advice pack and settings under `~/.claude`, which sandbox mode fences. Run it from a terminal and relay the answer. |

A **terminal** row is a step sandbox mode refuses on purpose — it changes settings or install state, or rewrites the vault wholesale. Give the user the exact command to run from a terminal, and carry on with the rest.

**When `.git` is not writable** (the sandbox blocks writes to it, so `git add` or `git commit` fails with `Operation not permitted` or "Unable to create '.git/index.lock'"): do not retry, and do not work around it with another route. Once `<commit>` has passed and the user has said yes, print the commands for them to run, each naming the repo, and carry on with the rest:

```
run this from a terminal:
git -C "<repo>" add <the staged paths>
git -C "<repo>" commit -m "<the message>"
```

Print them only on the user's yes. Never print a push; pushing stays the user's act.

Runs the plan `/gt:gt-plan` wrote and the user approved. It owns the *discipline* — phase by
phase, test first, stop on red — and **reuses** the checks and the commit gate gt already has
(`/gt:gt-allin`, `/gt:gt-allin-commit`) rather than carrying a second copy of either.

`<allin>` is `<base_dir>/../../scripts/gt_allin.py`, `<commit>` is
`<base_dir>/../../scripts/gt_allin_commit.py`, `<scope>` is `<base_dir>/../../scripts/gt_scope.py`.
Vault from `$GT_VAULT` or `~/.claude/vault-config.json`; `<repo>` is the code repository.

## Step 0 — Refuse without an approved plan

Ask where the plan is and what state it is in — the script also puts a plan whose scope block was
edited after approval back to draft, so the answer is the one to trust:

```bash
python3 <scope> plan --repo "<repo>"
```

It prints `plan:` (the file), `state:` (`missing`, `draft` or `approved`) and the `branch:` the
plan's scope applies to. Then read the plan file it names.

- **`state: missing`** → stop. Say: *"No plan: this repository has none (it would be written to
  <the `where:` path>; a plan at the old `.claude/gt-plan-current.md` is read too). Run
  /gt:gt-plan first."* Write no code.
- **`state: draft`** (not approved, or approval removed by an edit — a `note:` line says when the
  scope block was edited) → stop. Say: *"The plan in <path> is not approved (status: <value>).
  Approve it in /gt:gt-plan first."* Write no code.
- Approved → say which phase is next (the first one not marked `- [x] done`), and which branch the
  plan's scope applies to. If you are on another branch, say so: the scope check will not apply
  there. Then go on.

## Step 1 — One phase at a time

For the current phase, in this order — the order is the point:

1. **RED.** Write the phase's test first, run it, and show it **failing** for the reason the plan
   gives. A test that passes before the change proves nothing about the change. If the phase
   genuinely cannot have a test (pure docs, a rename), say so and why, and go on.
2. **GREEN.** Write the least implementation that makes it pass.

   A test run that prints a long output (a hundred lines or more) is read once and then carried by every
   later turn. Run it as `python3 <base_dir>/../../scripts/gt_spill.py run -- <the test command>`
   instead: it prints the first and last lines and the lines that look like failures, keeps the whole
   output in an owner-only file (read or grep that for more), and exits with the command's own status,
   so a red run still reads as red. A short output is printed whole.
3. **REFACTOR.** Tidy what you just wrote, with the test still green. Nothing outside the phase.
4. **Docs.** If the phase changed a documented interface (a CLI flag, a config key, a public
   function), update the doc that describes it in the same phase.

Before you spawn a builder or a reviewer for a phase, run `gt_settings.py models advise` (the Opus-or-Sonnet advisor, `gt_model_advise.py`) and read the row for the work class: use a deep-tier (Opus) verifier on Sonnet-built work only when all four hold (high stakes if wrong, new logic and not a mechanical change, its tests already pass, and no exhaustive deterministic gate already covers it), at most one per risk cluster per release, always a different model than the builder. The rows are examples until measured, and the model is still your choice.

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
- All green → run the scope check below, then mark the phase `- [x] done <date>` in the plan file,
  summarise it in two lines, and **ask before moving to the next phase.**

**Scope check, at the end of every phase.** The plan's `scope:` block is a contract; this shows
the user how the phase measured up to it before they say yes to anything:

```bash
python3 <scope> check --repo "<repo>" --worktree
```

It lists every changed file outside the allowed files, and every new direct dependency in
`package.json`, a requirements file, `pyproject.toml`, `go.mod` or `Cargo.toml` that
`new_dependencies` does not name. Each is `JUSTIFIED` (the plan's `## Deviations` has a line for
it, shown beside it) or `UNJUSTIFIED`. A plan that is not approved for this branch, or has no scope
block, prints `not applicable` and needs nothing.

- **Nothing outside the contract, or only justified items** → show them in your two-line summary
  and go on.
- **An UNJUSTIFIED item** → stop and show it. The user decides, and there are two honest answers:
  take the change out, or keep it and write the reason. For the second, **propose** the line for
  `## Deviations` — `` `path or glob` — why it was needed — the simpler alternative rejected, and
  why `` — and add it only once the user agrees. Never write a Deviations line to make the check
  pass without showing it; the written reason is the point of the check, and `--allow-findings`
  does not accept a deviation. Widening `scope:` itself is a plan amendment (see the Rules): it
  puts the plan back to draft for the user's approval.

## Step 3 — Commit, on a yes

When the user wants a commit (after a phase or at the end), stage what the phase changed and run
the commit gate, rehearsal first:

```bash
python3 <commit> --repo "<repo>" --vault "<vault>" -m "<message>" --dry-run
```

Show the staged files and the verdict — including the `--- scope ---` block, which prints every
deviation and its written reason — then **ask: "Commit this?"** Only on an explicit yes:

```bash
python3 <commit> --repo "<repo>" --vault "<vault>" -m "<message>"
```

It refuses without a test receipt covering every staged file, on the default branch, or with
findings — read its refusals to the user; they are the feature. With `scope_gate` set to `block` it
also refuses a commit that holds an UNJUSTIFIED deviation, and tells you the fix (a line in the
plan's `## Deviations`); under the default `warn` it prints the same and the commit can go ahead.
It never pushes, and neither do you.

A plain `git commit` in a Bash call is checked against the same contract: the PreToolUse guard
`guard_scope_contract` denies it under `block` (naming the files and the `## Deviations` fix) and adds
the same report as context under `warn`, so going around `<commit>` does not go around the contract.
It is a backstop, not the route: it fails open on a command line it cannot analyse, so use `<commit>`.

## Step 4 — Finish

After the last phase: all-in once more over everything, and the scope check over the whole branch —

```bash
python3 <scope> check --repo "<repo>" --base auto
```

— then tell the user what was built, which tests prove it, every deviation from the scope and the
reason written for it, and what the plan said was out of scope. Offer `/gt:gt-work` to capture what the
session learned — including its **learn** step, which looks for reusable patterns.

## Rules

- No approved plan, no code — Step 0 is not skippable.
- Test before implementation, in every phase that can have one.
- A red check stops the run. Never start phase N+1 on a failing phase N.
- Never commit without asking, never pass `--allow-findings` or `--allow-default-branch` on the
  user's behalf, never push.
- The plan is the scope. Something the plan did not cover → stop and propose amending the plan
  (which returns it to `status: draft` for approval) rather than building it unasked. Editing the
  `scope:` block does this by itself, because the approval carries a hash of it; a line in
  `## Deviations` does not, and is for the thing the plan could not have known — a fourth file
  nobody saw at planning time — written with its reason, in the user's sight.
