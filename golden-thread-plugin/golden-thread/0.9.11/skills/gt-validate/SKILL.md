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

Read `~/.claude/vault-config.json` for `vault_path`. If missing → tell the user to run
`/gt:gt-init` first.

## Steps

**Step 1 — Identify what is being validated**

From the user's request, or from what this session just produced. Reduce it to a
**single falsifiable assertion**. "The analysis is sound" is not validatable.
"Widening NQ's target to 1.25 improves risk-adjusted return" is.

If there are several claims, validate them **separately**. A bundled claim returns a
bundled verdict, which hides which part failed.

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

Log in `log.md` with `work`.

## Rules

- **Never paste your reasoning into the packet.** This is the whole skill.
- **Never hand the validator your conclusion first.** Reproduce, then compare.
- **Validate the artifact, not the write-up.** Prose can be internally consistent and
  still wrong about the world.
- **Vantage errors need a different position, not more care from the same one.**
- **Report scope reductions.** If 3 of 5 claims were checked, say so.
- A validator disagreeing is a **result**, not a failure. Investigate before defending.
