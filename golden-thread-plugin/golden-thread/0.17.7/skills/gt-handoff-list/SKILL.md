---
name: gt-handoff-list
description: "Show the handoffs still waiting — open, or whose deferral has ended — across every project or one, without opening any of them. Read-only. Use when the user says: list the handoffs, what handoffs are waiting, show handoffs, any handoffs pending, which handoffs are deferred."
---

# Golden Thread — List Handoffs

Read-only. Shows what is waiting and changes nothing; dealing with one is
`/gt:gt-handoff-handle`.

## Run it

Vault: `$GT_VAULT` if set, else `vault_path` from `~/.claude/vault-config.json`.

```bash
python3 ~/.claude/golden-thread/hooks/gt_handoff_status.py list --vault "<vault>" [--project <slug>] [--all]
```

- No argument: every handoff waiting now (open, or deferral ended), oldest first.
- A project named by the user → `--project <slug>` (sub-project as `parent/child`).
- "All", "deferred", "including handled" → `--all`, which prints each handoff with its state
  (`open`, `deferred` with its date, `handled`, `history`).

Show the output as it is — one line each: path, project, age, open items. **Do not read any
handoff's body.** Listing must cost no project context; that is the reason this is separate from
handling (owner, 2026-09-28).

End with one line: how many are waiting, and that `/gt:gt-handoff-handle` works through them.
