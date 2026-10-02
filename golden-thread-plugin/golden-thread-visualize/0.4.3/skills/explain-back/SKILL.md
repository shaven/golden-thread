---
name: explain-back
description: "Opt-in only: ask the user 3-5 questions about the seams of a gt-visualize explainer (behaviour, change impact, rationale, failure), one at a time with a prediction pause, and compare each answer with the expected answer copied from the story and the vault. --diff scopes the questions to the scenes whose parts a diff touches. A wrong answer offers a trace of that seam. Never part of any automatic or check-all flow. Use when the user says: explain-back, quiz me on the explainer, test my understanding of the explainer, ask me about the explainer seams, check I understood the walkthrough."
---

# Explain-back: the user explains the system back

Command: `/gt-visualize:explain-back [--diff <ref>]`.

**Opt-in only.** Run this only when the user asks for it by name or by one of the phrases
above. It is never run by another skill, a hook, a schedule, a check-all or any automatic
flow, and never suggested as a gate on anything.

Script: `<base>/../../scripts/gt_visualize.py` (`<base>` is this skill's base directory).

Find the story as the tour skill does (named by the user, else
`Projects/<slug>/visualize/story.json` in the vault). A story with problems is refused; relay
them and stop.

## Scope

- No `--diff`: all of the story.
- `--diff <ref>`: `git diff --name-only <ref>` in the code repository. Map each changed file to
  the parts it belongs to, using only the parts' labels and notes, the scenes' bodies and the
  project's vault pages (`design.md`, `source.md`). Say which parts you mapped and why. No part
  matched → say the diff touches nothing the story draws, and stop.

## Ask

`python3 <script> story questions <story> [--parts a,b]` — 3–5 questions, each with a `seam`,
the `expected` answer (strings copied verbatim from the story) and `from` (where in the story
each came from).

For each question, one at a time:

1. **Prediction pause.** Ask the question and stop. Wait for the user's answer. Never show the
   expected answer in the same message as the question.
2. **Compare** the answer with `expected` (and, where it helps, the vault page the story was
   drawn from). Judge the meaning, not the wording. Say what matched and what was missing,
   quoting the expected text and naming where it came from.
3. **Wrong or partial:** offer `/gt-visualize:trace <scene or part>` for that seam, so the
   user sees it hop by hop. Do not run it unasked.

End with a one-line summary of the seams covered. No score, no grade, no record: nothing is
saved and nothing is reported anywhere.

## Rules

- Expected answers come only from the story and the vault. A question the story cannot answer
  is dropped, not answered from memory.
- Read-only: this skill writes nothing.
