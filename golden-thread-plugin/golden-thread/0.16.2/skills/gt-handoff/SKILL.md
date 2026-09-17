---
name: gt-handoff
description: "Write a handoff document for the next session — the facts gathered and labelled by source and verification state, and the design narrative written by the session that actually did the work."
---

# Golden Thread Handoff

The session that *designs* something is rarely the session that *builds* it. This writes down
what the next one needs, and marks what it must not assume.

## Why the script only does half

`gt_handoff.py` gathers **facts**: the project's stated goal, open tasks, recent decisions, the
observed state of the code repository. Each one carries where it came from and a verification
label, per Core rule 10.

It deliberately does **not** write the design narrative. A script that invents "what we decided
and why" produces a document that *reads* finished and is not — and the next session inherits
false confidence rather than no confidence. That section is yours.

## Steps

**Step 1 — Locate the vault**

`$GT_VAULT`, else `~/.claude/vault-config.json`. Named explicitly on the command line, never
inferred (Core rule 2).

**Step 2 — Gather the facts**

```bash
python3 <base_dir>/../../scripts/gt_handoff.py \
    --vault "<vault>" --project <slug> [--repo <code-path>]
```

Writes `Projects/<slug>/handoff/<date>-handoff.md`. Use `--json` to read the facts without
writing a file.

**Step 3 — Write the design section yourself**

Replace the placeholder with what only this session knows:

- **What was decided**, and what was rejected — rejected options are the ones the next session
  will otherwise re-propose and re-discover the hard way.
- **What is built vs. designed.** Be blunt. "Designed, not written" and "written, not tested"
  are different states and the next session cannot tell them apart from a file listing.
- **What to do first**, and what would make it wrong.
- **What is uncertain.** An open question written down is worth more than a confident guess.

**Step 4 — Answer the checklist at the top**

The script writes questions it cannot answer: whether the tests pass, whether the decisions were
overtaken later in the session, whether anything agreed verbally never reached a file. Answer
them in the document, or say plainly that they are unanswered.

**Step 5 — Verification labels are not decoration**

If the handoff says the tests pass, say **who ran them, when, and which ones**. "The tests pass"
with no attribution becomes folklore the moment it is written down in something formal-looking.
Anything you did not personally observe this session is `unverified`.

**Step 6 — Record**

Log in `log.md` with `work`, and link the handoff from the project's `README.md` so the next
session finds it without being told it exists.

## Rules

- **Never let the script's output stand alone.** A handoff with the placeholder still in it is
  incomplete, and the next session should say so rather than infer the design from a file list.
- **Never claim verification you do not have.** This document is exactly where an unverified
  claim gets promoted to settled fact.
- **Write down what was rejected.** It is the most expensive thing to rediscover.
- A handoff is not a status report. It exists to let someone else continue, not to summarise.
