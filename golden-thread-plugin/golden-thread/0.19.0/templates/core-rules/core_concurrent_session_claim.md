---
name: core_concurrent_session_claim
description: "CORE rule -- more than one session and agent writes this vault at once. Vault content is written only through the write queue (gt_write_queue.py) and applied by the broker (gt_broker.py drain), which honours live claims, removes duplicates and sends conflicts to the owner. Enforced by a PreToolUse hook that denies direct writes."
metadata:
  node_type: memory
  type: core
  level: core
  enforcement: validated
  promoted: 2026-08-28
  revised: 2026-10-01
imperative: "Write vault content only through the write queue (gt_write_queue.py), then apply it with gt_broker.py drain; never edit a vault file directly, and never write one another live session has claimed."
---

# One way in: the write queue

**Write vault content only through the write queue (`gt_write_queue.py`), then apply it with
`gt_broker.py drain`; never edit a vault file directly, and never write one another live
session has claimed.**

```bash
gt_session.py register --task "..."                       # once, at the start of substantive work
gt_write_queue.py --vault V --path REL --op append \
    [--section "<heading>"] --content-file F               # every vault write: append,
gt_write_queue.py --vault V --path REL --op replace-section --section H --content-file F
gt_write_queue.py --vault V --path REL --op create --content-file F
gt_broker.py drain --vault V                              # apply: in order, once each
gt_session.py release                                     # when your writes are done
```

Generated files keep their own tools: `log.md` → `gt_log.py add`, `decisions.md` →
`gt_adr.py allocate`, `TASKS.md` → `gt_tasks.py`. Scaffolding (`vault_init.py`, `/gt:gt-create`)
and migrations (`/gt:gt-upgrade`) write through their scripts.

## Revised 2026-10-01: from "claim, then write" to "queue, then drain"

Owner: *"the agents write to the queues rather than directly to the files"* -- and, asked
whether `design.md` should be the exception, *"queue everything"*.

The claim rule closed the common collision but depended on every writer remembering to
claim, and on the write depending on the claim. On 2026-10-01 it failed twice in one hour:
- a Bash append went through a live claim (the guard never read Bash);
- a refused `claim` printed CONFLICT, but the next command was not chained to it, and wrote anyway.

The queue makes the safe path the only path. A request never touches its target. The broker
applies requests one at a time, holds any whose target a live session has claimed, removes
duplicates, and escalates what a check cannot decide -- conflicting replacements, a section
changed since it was queued, and **every write to a `design.md` or `global-memory/`** -- to the
owner as a task. Claims still exist: they are what the broker honours.

## The incident

2026-08-28. Two Claude Code sessions worked this vault simultaneously. One built
`claudebox` and wrote `index.md`, `INFRASTRUCTURE.md`, `log.md` and a memory file.
The other ran MSv6/NVDA work and wrote `research.md`, `TASKS.md`, `log.md` and a
project `README.md` — growing `research.md` from +73 to +124 lines inside a few
minutes. **Neither could see the other.**

Nothing was lost, and only by luck: one session ran `git status` before committing,
saw files it had never touched, and stopped. Had it committed, or had it run
`git checkout` to revert its own edit, the other session's uncommitted work would
have been destroyed with no error and no trace.

## Why git does not cover this

Every session shares **one working tree**. There are no branches to collide, so
there is no merge, no conflict marker, and no rejected push — just last-writer-wins.
The failure is silent at the moment it happens and only discoverable afterwards, by
noticing an absence. That is the worst possible shape for a data-loss bug.

## The three-question gate

1. **Correctness — yes.** A concurrent overwrite silently discards work that was
   already reasoned about and written. The vault then holds a *partial* record, which
   is worse than an empty one: later sessions read it as complete.
2. **Cost — yes.** The lost work is unrecoverable — uncommitted, so not in git, and
   the session that produced it has usually moved on or ended. It must be re-derived
   from scratch, if anyone even notices it is gone.
3. **Cascade — yes.** This vault is the promotion ladder. A clobbered `research.md`
   or `decisions.md` propagates upward into Knowledge pages and `global-memory/`,
   and every rule that says "the vault is the single source of truth" becomes false
   without announcing it.

Any single yes qualifies. All three do.

## Why this is Core rather than Context

It is not specific to a project, a directory or a task mode. It applies to every
write, in every project, in every session, and its cost is highest exactly when
attention is elsewhere — which is when a rule that depends on remembering fails.

## Enforcement — `validated` for Write/Edit; Bash in part

A `PreToolUse` hook (`guard_session_claims.sh`) does two things, in order:

1. **Queue first.** A `Write`/`Edit`/`MultiEdit` to vault content -- any `.md` outside `Sources/`,
   `core-rules/`, `.obsidian/`, `.git/`, `.gt/` and gt's own `spool/`, `sessions/`, `tools/` -- is
   **denied**, claimed or not, with the exact queue command in the reason. A `Bash` command is
   denied when it visibly writes vault content: a redirect (`>`, `>>`), `tee`, `sed -i`, or a
   `cp`/`mv`/`install` whose destination is a vault `.md`.
2. **Claims.** A write to any other vault file is denied when another *live* session holds a
   claim on it. Liveness is a fact (pid on this machine), not a timeout.

**It fails open.** Any parse failure, missing vault or unreadable session directory raises no
objection: a guard that blocks wrongly makes every session unusable.

### What it does not cover, said plainly

- **A script that opens a file itself** (`python3 -c "open(p,'w')…"`, a tool written for the
  purpose). A shell guard cannot read intent inside an interpreter. This part of the rule is
  `reminder`, not `validated`.
- **The owner's own edits in Obsidian.** No hook sees them, and none should.
- **Files outside the vault.**

## Related

`PROTOCOL.md` → "Concurrent sessions" -- the process half.
`gt_write_queue.py`, `gt_broker.py` -- the queue and the broker (gt scripts).
`Projects/golden-thread/spool/queue/` -- waiting requests; `spool/broker/` -- the decisions log.
`Projects/golden-thread/sessions/` -- live registrations and claims.
