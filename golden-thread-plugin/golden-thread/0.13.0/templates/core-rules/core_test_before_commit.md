---
name: core_test_before_commit
description: "CORE rule — code is not committed until its tests have been seen to pass, and the reply says which ran. Enforced by a PreToolUse guard on `git commit`, with a per-repo opt-out for repos that have no tests."
metadata:
  node_type: memory
  type: core
  level: core
  enforcement: validated
  promoted: 2026-09-12
imperative: "Run the tests before you commit code, and say which ran — never commit code whose tests you have not seen pass."
gated_by: test_gate
---

# See it pass, then commit

**Run the tests before you commit code, and say which ran — never commit code whose
tests you have not seen pass.**

Not "the tests exist". Not "the tests passed earlier". Seen to pass, on the tree you
are about to commit.

## The incident

2026-09-12, in this repository, in the release that introduced the parallel Core rule.
The release gate was run, everything reported ok, several more edits followed, and the
commit went out with a **stale `MANIFEST.json`** — the one file whose entire purpose is
to detect drift between what ships and what is checked in. The next full gate run caught
it in the step named `manifest`.

Nothing about the discipline was wrong. The gate existed, it was thorough, it was
*run* — and then the tree changed underneath it. **The discipline had no mechanism**,
so the question "has this exact tree passed?" was answered from memory, and memory
answered about a different tree.

That is the same shape as every other Core rule here: a rule written down is not a rule
enforced. This one closes the last unenforced step of the release path.

## What satisfies it

A **receipt** — evidence, not a claim:

```bash
tests/run.sh                 # writes its own receipt when it passes
dev/release-check.sh         # likewise, for the whole gate
gt_test_receipt.py record --repo . --what "pytest -q" --ok     # any project
```

A receipt covers a file only if it is **newer** than that file. Editing something after
the run invalidates the receipt for that file automatically — there is nothing to
remember and nothing to recompute, which is precisely what the stale manifest needed.

## Turning it off, per repo — this is expected, not a failure

Most repositories are not test-bearing. A vault of notes, a scratch repo, a docs site:
demanding a test run there produces a gate that is wrong every day, and a gate that is
wrong every day gets switched off for everything. So there are three escapes, and the
first is the one to reach for:

| Scope | How |
|---|---|
| **This repo, permanently** | `touch .gt-no-test-gate` in the repo root. Committed, so it is visible to everyone and needs no explaining twice. |
| This one commit | `GT_TEST_GATE=off git commit …` — said out loud, in the transcript. |
| This machine | `gt_settings.py set test_gate off` — also stops the rule being injected. |

The setting has four positions: `off`, `warn` (allow but say so), `auto` (**default** —
block where the repo has a discoverable test command, warn where it does not), and
`block` (hold every repo to it).

`auto` is the important one. A repo with no test entry point is **not asked to have
one**; a repo that has one is held to it. The gate only fires where it can be satisfied.

## What is never blocked

- **Docs-only commits.** Markdown, HTML, PDFs, images, LICENSE, CHANGELOG.
- A repo carrying `.gt-no-test-gate`.
- Anything the guard cannot parse with certainty — it **fails open**, every time. It sits
  in front of every Bash call in every session, so a wrong deny costs far more than a
  missed one. This is the same reasoning as the other two `PreToolUse` guards, and the
  reason there are three narrow guards rather than one that must understand everything.

**Tier:** Core (see [[core_rule_priority_model]])

**Why designated:** the user designated it, asking for exactly this shape — a rule, with
a per-project disable. It also passes the gate on its own: an untested commit is a wrong
change shipped (question 1) and a backout (question 2).

**Why `validated` rather than `reminder`:** "did the tests pass on this tree" is
mechanically checkable — a timestamp against a file mtime — which is the whole test for
this tier. Unlike [[core_parallel_when_beneficial]], there is no judgement to make here,
so a reminder would be a choice to enforce less than is possible.

**How to apply:** run the project's tests or its gate as the **last** action before
committing, not the first; name what ran and what passed in the reply; and if the repo
has no tests, exempt it once with `.gt-no-test-gate` rather than working around the gate
each time. Related: [[core_verification_state]] — the same argument, applied to figures
rather than code.
