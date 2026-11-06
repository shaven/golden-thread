---
name: core_explicit_vault_target
description: "CORE rule — a tool that writes a vault must be told which vault. Pass --vault or --dry-run on every mutating run; never let the target be inferred from vault-config.json. Enforced by a PreToolUse hook that denies the command."
metadata:
  node_type: memory
  type: core
  level: core
  enforcement: validated
  promoted: 2026-09-11
imperative: "Name the vault on every mutating tool run — --vault or --dry-run — never let the target be inferred."
---

# Say which vault you mean

**Name the vault on every mutating tool run — `--vault` or `--dry-run` — never let the
target be inferred.**

```bash
gt_adr.py merge myproj --vault "$REHEARSAL"   # the copy, explicitly
gt_log.py migrate --dry-run                   # show me, write nothing
gt_tasks.py --vault "$LIVE"                   # the real vault, said out loud
```

## The incident

2026-09-11. A session was migrating this vault to the 0.11.0 spool model and did the
careful thing first: `rsync` the vault to a scratch directory, run every migration
there, compare the output, and only then touch anything real. It ran `gt_log.py`
with `--vault <copy>`, and that half behaved exactly as intended.

`gt_adr.py migrate` takes no `--vault`. It resolves the vault from
`~/.claude/vault-config.json`. So the loop over 43 projects — a loop whose entire
purpose was to avoid touching the live vault — migrated the live vault's 42
`decisions.md` files instead of the copy's.

Nothing was lost. The content survived byte-for-byte, the tree was clean, and one
`git checkout` would have reverted it. That is luck, not design: the same shape with
`rename-project` or `merge-project` moves files the copy was supposed to absorb.

## Why a flag, and not care

The session did not skip a precaution. It *took* the precaution — a rehearsal on a
copy — and the precaution silently did not apply to one of the tools it used. No
output said "live vault"; the command that hit production looked exactly like the
one that did not.

A default that resolves to the most valuable thing on the machine is the wrong
default, and no amount of attention fixes it, because the attentive version of the
command is indistinguishable from the careless one. So the requirement moves into
the command line, where it is visible in the transcript and checkable by a hook.

## The two halves

**This rule** covers the caller: say the target, every time.

**A release gate** covers the tool: `dev/check_cli_contract.py` fails the build if a
vault tool grows a mutating subcommand without `--vault` and `--dry-run`. A rule that
depends on a flag the tool does not offer is unfollowable, so the flag is not
optional either.

**Tier:** Core (see [[core_rule_priority_model]])

**Why:** a tool that infers its target writes to whatever the config happens to say,
which on a developer's machine is always the live vault. The one time it matters is
the one time you were trying to be careful.

**How to apply:** pass `--vault` whenever you run a mutating vault tool, even when
the vault you mean *is* the live one — the flag is also how the next reader knows you
meant it. Pass `--dry-run` first when you are unsure. `GT_VAULT` in the environment
satisfies the rule too: a session opened against one vault has already said which.
Related: [[core_concurrent_session_claim]]
