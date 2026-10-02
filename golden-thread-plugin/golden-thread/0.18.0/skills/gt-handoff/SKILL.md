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

Replace the placeholder under `## The design, in the author's words` with what only this
session knows:

- **What was decided**, and what was rejected — rejected options are the ones the next session
  will otherwise re-propose and re-discover the hard way.
- **What is built vs. designed.** Be blunt. "Designed, not written" and "written, not tested"
  are different states and the next session cannot tell them apart from a file listing.
- **What to do first**, and what would make it wrong.
- **What is uncertain.** An open question written down is worth more than a confident guess.

**Step 4 — Answer the checklist at the top**

The script writes questions it cannot answer: whether the tests pass, whether the decisions were
overtaken later in the session, whether anything agreed verbally never reached a file. Answer
them in the document (under `## What the next session must not assume`), or say plainly that they
are unanswered.

**How Steps 3 and 4 reach the file (Core rule 1).** The handoff is vault content, so you never
edit it directly — the PreToolUse guard denies a Write/Edit to it. The script created the file;
you change it only through the write queue. Write each new section body to a scratch file and
queue a `replace-section` for it:

```bash
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
    --path Projects/<slug>/handoff/<date>-handoff.md --op replace-section \
    --section "The design, in the author's words" --content-file <scratch file> \
    --session <session id> --hint "handoff design narrative"
```

and likewise with `--section "What the next session must not assume"` for the answered
checklist. Each body replaces the **whole** section, so it must carry everything you keep — for
the design section, that includes the `Repository:` line the script put under it. Drain once,
after Step 6's README link is queued too (below).

**Step 5 — Verification labels are not decoration**

If the handoff says the tests pass, say **who ran them, when, and which ones**. "The tests pass"
with no attribution becomes folklore the moment it is written down in something formal-looking.
Anything you did not personally observe this session is `unverified`.

**Step 6 — Record**

Log in `log.md` with `work` through its own tool —
`python3 "<vault>/Projects/golden-thread/tools/gt_log.py" --vault "<vault>" add "<date time tz> [work] <slug> — handoff written"`
— and link the handoff from the project's `README.md` so the next session finds it without being
told it exists. The link is a queued `append` (one line in a scratch file; add
`--section "<heading>"` if the README has a section that lists handoffs, otherwise it goes at the
end of the file):

```bash
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
    --path Projects/<slug>/README.md --op append --content-file <scratch file> \
    --session <session id> --hint "link the handoff"
```

Then drain once, for all of this skill's writes:

```bash
python3 <base_dir>/../../scripts/gt_broker.py drain --vault "<vault>"
```

Report it in one line: `N applied, N held, N escalated`. **Held** means another live session
claims that file — the request waits and the next drain applies it; never work around a hold.
**Escalated** means the broker could not decide (for a `replace-section`, the section changed
since you queued it) and made the owner a task pointing at the conflict file — say so. Until the
design section is applied, the handoff still reads as incomplete; say that too.

## Rules

- **Never let the script's output stand alone.** A handoff with the placeholder still in it is
  incomplete, and the next session should say so rather than infer the design from a file list.
- **Never claim verification you do not have.** This document is exactly where an unverified
  claim gets promoted to settled fact.
- **Write down what was rejected.** It is the most expensive thing to rediscover.
- A handoff is not a status report. It exists to let someone else continue, not to summarise.
