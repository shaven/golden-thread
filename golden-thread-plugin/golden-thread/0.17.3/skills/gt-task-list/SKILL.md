---
name: gt-task-list
description: "Show open tasks, filtered — by project, priority, mine, overdue, stale, deferred or ref — without loading any project's context. Read-only. Use when the user says: /gt-task-list, list tasks, show my tasks, what tasks are open, what's overdue, show p1 tasks."
---

# Golden Thread — List Tasks

Read-only. Nothing is changed and no project is opened.

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
`/gt:gt-task-handle` accepts. End with the count and the handle command.
