---
name: gt-validate
description: "Independently verify a claim using a fresh-context validation agent. The validator receives only the claim, the rules and the artifact — never the reasoning that produced them — so it re-derives the answer instead of grading an argument. Use when a finding is about to be recorded as fact, before a production change, or when a number matters."
---

# Golden Thread Validate

Verify a claim by re-deriving it, not by reviewing it.

## The rule this skill exists to enforce

**A validator must never receive the reasoning that produced the claim.**

Give it the reasoning and it grades the argument — and inherits the same blind spot.
Give it only the claim, the rules and the artifact, and it must go back to primary
sources. That is the only thing that catches a wrong premise.

**You are the worst possible author of this packet**, because you already know the
answer and your framing leaks. Treat packet construction as an adversarial exercise
against yourself.

## Vault location

Use `$GT_VAULT` if it is set (a session pinned to one vault, such as the demo); otherwise read `~/.claude/vault-config.json` for `vault_path`. If missing → tell the user to run
`/gt:gt-init` first.

## Steps

**Step 1 — Identify what is being validated**

From the user's request, or from what this session just produced. Reduce it to a
**single falsifiable assertion**. "The analysis is sound" is not validatable.
"Widening NQ's target to 1.25 improves risk-adjusted return" is.

If there are several claims, validate them **separately**. A bundled claim returns a
bundled verdict, which hides which part failed.

**Step 1b — Can an installed checker decide it?**

Some claims are mechanical: "this page is valid HTML", "this file's content matches its
MIME type", "every input on this form has a label". A module may have installed a
**checker** that decides exactly that, deterministically and without a model call. Ask:

```bash
python3 <base_dir>/../../scripts/gt_check.py list --for <the artifact file(s)>
```

If a listed checker decides the claim **as stated**, run it instead of an agent:

```bash
python3 <base_dir>/../../scripts/gt_check.py run <the artifact file(s)> --no-receipt
```

Report its verdict as the validation result — `pass` → **confirmed**, `fail` → **refuted**
(name each finding), and `cannot-check` → **cannot-verify**, never a pass. Then go to Step 7.
Use the fresh-context agent (Steps 2–6) only for a claim no checker covers, or for the part
of a claim a checker does not decide — and say which part went where.

**Step 2 — Load the project's rule pack**

Read `<vault>/Projects/<slug>/validation-rules.md` if it exists. For a sub-project,
read the **parent's pack too** — packs are inherited.

These are standing invariants the validator must enforce **whether or not the request
mentions them**. This matters because *a requester who has already made a domain error
will not think to ask the validator to check for it.*

Merge with any claim-specific rules. **On conflict, the pack wins.**

**Step 3 — Choose the validator class**

| Class | Use when the risk is |
|---|---|
| `empirical` | A number, measurement or result could be wrong |
| `vantage` | The measurement position may not be able to observe the answer |
| `rule-compliance` | Work must satisfy `core-rules/` or `CONVENTIONS.md` |
| `code` | Code may not do what its name, comment or docs claim |

Pick more than one when more than one risk is present. **Do not substitute a single
generic reviewer** — it produces agreement, not verification.

**Step 4 — Build the packet**

Exactly three fields:

```
claim:    <the single falsifiable assertion>
rules:    <merged pack + claim-specific constraints, stated operationally>
artifact: <file path / endpoint / dataset / command — where to look>
```

Then **re-read it and strip**: your conclusions, your numbers, your confidence, the
transcript, prior results, and any adjective implying the expected answer. If the
packet says "confirm that X", rewrite it as "determine whether X".

Include enough operational detail that the validator can reach the artifact
independently — hostnames, API shapes, required filters. Withholding *access* is not
isolation; withholding *reasoning* is.

**Step 5 — Dispatch**

Launch a subagent per class with the matching prompt from `prompts/`, plus the packet.
Run them in the background; they are slow by design because they redo the work.

**Specialist spec (only when `agent_specialization` is on).** The setting defaults to
`off`, and then this paragraph changes nothing. Otherwise first run:
```bash
python3 <base_dir>/../../scripts/gt_agent_spec.py resolve --skill gt-validate --vault "<vault>"
```
`action: inline` → dispatch as above (show any `notice:` line in one line). `action: spawn`
→ build each validator's prompt from the `validate` spec instead of by hand. Write the
packet's three fields to files under your scratchpad, then per class:
```bash
python3 <base_dir>/../../scripts/gt_agent_spec.py render validate --vault "<vault>" \
  --input-file claim=<f> --input-file rules=<f> --input-file artifact=<f> \
  --input-file method=<base_dir>/prompts/<class>.md
```
Spawn the subagent with exactly that output — the spec loads **no** vault context, and you
must not add any. For each result, write the record to the path
`gt_agent_spec.py spool-path validate --session <id> --vault "<vault>"` prints, as
`{"job_type": "validate", "session_id": ..., "created": ..., "result": <its JSON>}`, run
`gt_agent_spec.py check-output validate "<record>" --vault "<vault>"`, and take the
verdict from its `verdict` field in Step 6. Show the user the summary lines; the full
derivation stays in the record.

**Step 6 — Compare, in that order**

Read the validator's independent derivation **before** re-reading your own. Then report:

- **confirmed** — independently re-derived, matches
- **refuted** — independently re-derived, does not match. Quantify the divergence
- **cannot-verify** — inputs insufficient. **Name what was missing**

**`cannot-verify` must never be reported as a pass.** A validator that could not check
something and stayed quiet manufactures false assurance, which is worse than no
validator at all.

**Step 7 — Record**

Append the verdict to the project's `research.md` with the date, the claim, the class
used, and the outcome. A refutation supersedes the original finding — mark it inline.

Both go through the write queue, never a direct edit (Core rule 1; the PreToolUse guard denies
a Write/Edit to vault content). Put each body in a scratch file:

```bash
# the verdict: a new dated entry
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
    --path Projects/<slug>/research.md --op append --content-file <verdict file> \
    --session <session id> --hint "validation verdict: <claim>"
# a refutation's inline mark: the WHOLE body of the original finding's `##` section, as it is
# now, with only the supersession mark added
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
    --path Projects/<slug>/research.md --op replace-section \
    --section "<the original finding's heading text>" --content-file <section file> \
    --session <session id> --hint "refuted by validation <date>"
python3 <base_dir>/../../scripts/gt_broker.py drain --vault "<vault>"
```

Report the drain in one line: `N applied, N held, N escalated`. **Held**: another live session
claims `research.md`; the next drain applies it — never work around it. **Escalated**: the
original section changed since you queued the mark, so the owner gets a task pointing at the
conflict file; say so, because until it is resolved the refuted finding still reads as standing.

Log in `log.md` with `work`, through its own tool:
`python3 "<vault>/Projects/golden-thread/tools/gt_log.py" --vault "<vault>" add "<date time tz> [work] <slug> — validated: <claim> → <verdict>"`.

## Rules

- **Never paste your reasoning into the packet.** This is the whole skill.
- **Never hand the validator your conclusion first.** Reproduce, then compare.
- **Validate the artifact, not the write-up.** Prose can be internally consistent and
  still wrong about the world.
- **Vantage errors need a different position, not more care from the same one.**
- **Report scope reductions.** If 3 of 5 claims were checked, say so.
- A validator disagreeing is a **result**, not a failure. Investigate before defending.
