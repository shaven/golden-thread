---
name: core_concurrent_session_claim
description: "CORE rule — more than one session writes this vault at once. Register the session, claim a file before writing it, and never write a file another live session holds. Enforced by a PreToolUse hook that denies the write."
metadata:
  node_type: memory
  type: core
  level: core
  enforcement: validated
  promoted: 2026-08-28
imperative: "Register your session and claim a vault file before writing it; never write a file another live session has claimed."
---

# One writer per file

**Register your session and claim a vault file before writing it; never write a file
another live session has claimed.**

```bash
gt_session.py register --task "..."     # once, at the start of substantive work
gt_session.py claim <path>              # before writing a shared file
gt_session.py list                      # who else is live, and what they hold
gt_session.py release                   # when your writes are done
```

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

## Enforcement — `validated`, and genuinely so

A `PreToolUse` hook (`guard_session_claims.sh`) inspects each `Write`/`Edit` against
the vault and **denies** it when another *live* session holds a claim on that path.
Unlike `core_verification_state`, this rule has a mechanically detectable shape: a
file path, a claim file, and a running pid. There is no judgement to make.

**Liveness is a fact, not a timeout.** Each session file records `pid` + `host`;
when the host matches, `os.kill(pid, 0)` settles whether that process still exists.
A session busy for hours stays `LIVE`; one that died seconds ago is `STALE` and its
claims are ignored. Heartbeat age is only the fallback when pid cannot be checked.

**The hook fails open.** Any parse failure, missing vault, or unreadable session
directory allows the write. A guard that blocks wrongly would make every session
unusable, which is a worse failure than the one it prevents — the same reasoning
that governs the `Stop` validator.

### What it deliberately does not cover

- **`Bash` writes** (`>`, `sed -i`, `tee`, `cp`). The hook does not parse shell, so a
  redirect can still clobber a claimed file. Detecting this reliably means parsing
  arbitrary shell, which fails open so often it would train the guard to be ignored.
- **Files outside the vault.**
- **Stale claims**, by design — see liveness above.

So this closes the common case (an agent editing a file another agent is editing),
not every case. It is a guard, not a lock.

## Related

`PROTOCOL.md` → "Concurrent sessions" — the process half, and the `pending/`
staging convention for when a file *is* claimed.
`Projects/golden-thread/tools/gt_session.py` — the tool.
`Projects/golden-thread/sessions/` — live registrations.
