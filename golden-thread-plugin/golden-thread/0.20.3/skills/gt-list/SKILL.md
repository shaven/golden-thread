---
name: gt-list
description: "Show what is waiting without opening anything — handoffs (open, or whose deferral has ended) and open tasks filtered by project, priority, mine, overdue, stale, deferred or ref. Read-only. Use when the user says: list the handoffs, what handoffs are waiting, show handoffs, any handoffs pending, which handoffs are deferred, list tasks, show my tasks, what tasks are open, what's overdue, show p1 tasks, what is waiting."
model_intent: fast
---

# Golden Thread — List

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_handoff_status.py list …` | `vault_report` `{report: handoffs, project, all}` |
| `gt_task.py list …` | `vault_report` `{report: tasks, filters}` |
| `gt_task.py count` | `vault_report` `{report: task_count}` |

Read-only. Nothing is changed and no project is opened. The artifact is the first argument:

| invoked as | shows |
|---|---|
| `/gt:gt-list handoffs` (or `handoff`) | waiting handoffs — section **Handoffs** |
| `/gt:gt-list tasks [filter ...]` (or `task`) | open tasks — section **Tasks** |
| `/gt:gt-list` with no argument | both, summarised — section **Both** |

Replaces `gt-handoff-list` and `gt-task-list` (0.18.1). Those still work for one release as
deprecated aliases that follow the sections here unchanged. Dispatch is keyword matching on the
first word only; anything else is a task filter (`p1`, `mine`, a slug …) → **Tasks**.

Vault: `$GT_VAULT` if set, else `vault_path` from `~/.claude/vault-config.json`.

## Handoffs

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

End with one line: how many are waiting, and that `/gt:gt-handle handoff` works through them.

## Tasks

```bash
python3 <vault>/Projects/golden-thread/tools/gt_task.py list --vault "<vault>" [FILTER ...]
```

Turn what the user asked into filters (a task must match all of them):

| user says | filter |
|---|---|
| a project name | `<slug>` (sub-project `parent/child`), or `inbox` |
| urgent / p1, normal / p2, someday / p3 | `p1` `p2` `p3` |
| mine, waiting on me | `mine` |
| overdue, late | `overdue` |
| stale, sitting too long | `stale` (p1 open over 7 days) |
| deferred, snoozed | `deferred` (hidden otherwise) |
| about a page or source | `ref:<text>` |

No filter lists every open task, most urgent first — which can be long; if it is over ~40 lines,
say how many and offer a narrower filter rather than dumping them all. Shelved tasks (`p:: 7`+)
never show unless a `p7`-style filter asks.

Show the lines as printed. Each starts with an ID (`slug:LINE:HASH`) that
`/gt:gt-handle task`, `/gt:gt-open task <id>` and `/gt:gt-close task <id>` accept. End with the
count and the handle command.

## Both

No argument: run both reads, print nothing but counts and the top of each —

```bash
python3 ~/.claude/golden-thread/hooks/gt_handoff_status.py list --vault "<vault>"
python3 <vault>/Projects/golden-thread/tools/gt_task.py count --vault "<vault>"
python3 <vault>/Projects/golden-thread/tools/gt_task.py list --vault "<vault>" p1
```

Summarise as:

```
Handoffs waiting: N   (oldest: <path>, <age>d)
Tasks: M p:: 1 waiting on you (X overdue, Y stale); first few:
  <up to 5 p1 lines as printed>
```

Then one line: `/gt:gt-list handoffs` or `/gt:gt-list tasks <filter>` for the full lists,
`/gt:gt-handle` to work through them.
