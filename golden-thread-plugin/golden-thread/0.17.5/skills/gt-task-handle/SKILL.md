---
name: gt-task-handle
description: "Work through open tasks one at a time — close, drop with a reason, defer to a date, or keep — with a filter to choose which (a project, p1, mine, overdue, stale, ref). The way to clear a backlog. Use when the user says: /gt-task-handle, handle my tasks, work through the tasks, clear the backlog, triage tasks, get rid of old tasks."
---

# Golden Thread — Handle Tasks

Tool: `<vault>/Projects/golden-thread/tools/gt_task.py`. Vault from `$GT_VAULT` or
`~/.claude/vault-config.json`. Pass `--vault` on every call (Core rule 2).

## Step 1 — Choose the set

Map the user's parameter to filters exactly as `/gt:gt-task-list` does (`<slug>`, `p1`, `mine`,
`overdue`, `stale`, `deferred`, `ref:<text>`), then:

```bash
python3 <tool> list --vault "<vault>" <filters>
```

Say how many there are. With no parameter and a long list, propose a first cut (`stale`,
`overdue`, or one project) rather than starting at the top of everything.

## Step 2 — One task at a time

Show the task line. **Load context only if deciding needs it**: the file its `ref::` points at, or
one named file — never a full `/gt:gt-open`. Then ask the user for ONE of:

| decision | command |
|---|---|
| **done** | `python3 <tool> done <ID> --vault "<vault>" --reason "<what settled it>"` |
| **drop** | `python3 <tool> drop <ID> --vault "<vault>" --reason "<why it no longer matters>"` |
| **defer** | `python3 <tool> defer <ID> --vault "<vault>" --until YYYY-MM-DD --reason "<why then>"` |
| **keep** | nothing — move on |

- Nothing is deleted: done and drop check the box and record the reason on the line. Drop needs a
  reason; that is what makes clearing a backlog safe.
- A deferral needs a future date and hides the task — from lists, the TASKS.md ranking and
  escalation — until then. "Later, some time" is a drop, said as one.
- If the tool says the ID changed (someone edited the file), list again and use the new ID; never
  edit the README by hand to get around it.
- If a decision belongs in `decisions.md` or `research.md`, write it there too — a task line is not
  where a decision lives.

Register the session and claim each README before its first write (Core rule 1); the tool refuses
if another live session holds it.

## Step 3 — Stop cleanly

When the set is done or the user stops: one line with the counts (closed, dropped, deferred, kept),
then regenerate the rollup so "what's next" is current:

```bash
python3 <vault>/Projects/golden-thread/tools/gt_tasks.py --vault "<vault>"
```
