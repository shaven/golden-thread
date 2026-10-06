---
name: gt-handle
description: "Work through what is waiting, one item at a time — handoffs (close each open item, keep it as a task, or defer the whole handoff to a date) or tasks (close, drop with a reason, defer, move, or keep) — with a filter to choose which. Use when the user says: handle the handoffs, deal with the handoff, clear the handoffs, defer that handoff, work through the handoffs, handle my tasks, work through the tasks, clear the backlog, triage tasks, get rid of old tasks. To only SEE them, use gt-list."
model_intent: balanced
---

# Golden Thread — Handle

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_handoff_status.py list …` | `vault_report` `{report: handoffs, project, all}` |
| `gt_task.py list …` | `vault_report` `{report: tasks, filters}` |
| `gt_close.py task <ID> --vault …` (`gt_task.py done` + the rollup) | `vault_task` `{action: close, id, reason}` |
| `gt_task.py drop` / `defer` / `move` / `add` | `vault_task` `{action: drop \| defer \| move \| add, …}` |
| `gt_write_queue.py …` | `vault_queue_write` — the same `path`, `op`, `section`, `key` and `hint`, with the text itself as `content` (no `--session`: the server stamps its own). The broker decides at once and the result says apply, held or escalate. `design.md` and `global-memory/` are refused (owner only) |
| `gt_broker.py drain` | nothing to run after `vault_queue_write`; `vault_queue_drain` if anything was queued from the shell |
| `gt_close.py handoff --vault …` | `vault_handoff` `{action: close, file, reason}` |
| `gt_handoff_status.py mark …` | `vault_handoff` `{action: mark, file, status, until, reason}` |
| `gt_tasks.py` | `vault_tasks_regen` |
| `gt_session.py register` / claims | skipped: the session is not recorded, so a sandboxed session holds no claim of its own and other sessions' file tools are not stopped from editing what it works on (the file tools cannot edit the vault under sandbox mode anyway); the server stamps its own session on every write, which the broker checks against other live sessions' claims |

One verb for working through what is waiting. The artifact is the first argument:

| invoked as | works through |
|---|---|
| `/gt:gt-handle handoff` or `handoffs` (optionally `<id>` / a project) | waiting handoffs — section **Handoffs** below |
| `/gt:gt-handle task` or `tasks` (optionally a filter) | open tasks — section **Tasks** below |
| `/gt:gt-handle` with no argument | see **Which one?** |

Replaces `gt-handoff-handle` and `gt-task-handle` (0.18.1). Those still work for one release as
deprecated aliases that follow the sections here unchanged.

Vault: `$GT_VAULT` if set, else `vault_path` from `~/.claude/vault-config.json`. Pass `--vault` on
every call (Core rule 2). `<scripts>` is `~/.claude/golden-thread/hooks` (where install.sh puts
`gt_handoff_status.py`); `<tool>` is `<vault>/Projects/golden-thread/tools/gt_task.py`;
`<close>` is `<base_dir>/../../scripts/gt_close.py` — the same close logic `/gt:gt-close` runs.

## Which one?

Match the first word only: `handoff`/`handoffs` → Handoffs; `task`/`tasks` → Tasks. Anything else
is a task filter (`p1`, `mine`, a project slug …) → Tasks with that filter.

No argument: count both, without opening anything —

```bash
python3 <scripts>/gt_handoff_status.py list --vault "<vault>" --json
python3 <tool> list --vault "<vault>" --json
```

- Both non-empty → **ask** which to work through ("N handoffs and M tasks are waiting — handoffs
  or tasks?"). Never pick one silently.
- Only one non-empty → say so in one line and go to that section.
- Neither → say nothing is waiting, and stop.

## Handoffs

A handoff exists because a session could **not** capture everything. Since 0.17.2 each one has a
status — `open`, `deferred` (to a date) or `handled` — and an open one keeps being shown at
session start (setting `handoff_surface`) until someone deals with it. This is where it gets
dealt with.

**Showing a handoff costs no context; handling one does.** So nothing is loaded until the user
has picked ONE handoff, and then only that handoff and its project's task lines — not a full
`/gt:gt-open`.

### Step H1 — List what is waiting

```bash
python3 <scripts>/gt_handoff_status.py list --vault "<vault>"
```

One line each: path, project, age, open items, and why it counts as waiting (never marked,
deferral ended). Show the list as a numbered menu, oldest first, and ask which to handle. If the
user named a project, pass `--project <slug>`; if they named a handoff, skip the menu. **Do not
open any handoff before they choose.**

Nothing waiting → say so in one line and stop.

### Step H2 — Load only the chosen one

Read the chosen handoff, then the `## Tasks` section of its project's `README.md` — only the
lines that cite this handoff's filename. That is the whole context. If an item genuinely cannot
be decided without more (a `design.md`, a `source.md` before touching a host), read that one file
and say why.

### Step H3 — Walk the open items with the user

For each thing the handoff leaves unresolved (its uncaptured list, and each task citing it), ask
the user and apply ONE of:

- **Done (close)** — close the task with the shared close logic. If the decision belongs in
  `decisions.md` or `research.md`, write it there through the usual `/gt:gt-work` path; the task
  line is not where a decision lives.
- **Keep** — leave the task open. It is already `p:: 1` with a `since::` date, so it escalates the
  project on its own if it sits for a week.
- **Drop** — check it off with the reason. Dropping is a decision, and it is recorded, never
  silently deleted.
- **Move** — it belongs to another project: `gt_task.py move` writes it there first, then checks
  this line off with where it went.

An item with no task yet: raise one in the gt-work shape
(`- [ ] <item> — context in <handoff file> [p:: 1] [waiting:: user] [since:: YYYY-MM-DD]`) or
settle it now.

**How these reach `README.md` (Core rule 1).** Never edit the README directly — the PreToolUse
guard denies a Write/Edit to vault content. There are two sanctioned paths:

- **Close / Drop / Move / a new task — the tools first.** They change exactly one line, re-check
  that line's hash, and refuse if another live session claims the README:

  ```bash
  python3 <tool> list --vault "<vault>" <slug>            # find the citing task's ID
  python3 <close> task <ID> --vault "<vault>" --reason "<what settled it>"
  python3 <tool> drop <ID> --vault "<vault>" --reason "<why>"
  python3 <tool> move <ID> --vault "<vault>" --to <other-slug> --reason "<why it belongs there>"
  python3 <tool> add "<item> — context in <handoff file>" --vault "<vault>" --project <slug> --p 1 --waiting user
  ```

- **Anything the tools cannot address** (a line they do not parse as a task, an edit that is not
  close/drop/move/add) goes through the write queue as a `replace-section` of `## Tasks`. Write the
  **whole** new section body — every line as it is now, with only the decided lines changed — to a
  scratch file and queue ONE replacement carrying all of this handoff's changes (two replacements
  of one section in a pass conflict with each other):

  ```bash
  python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
      --path Projects/<slug>/README.md --op replace-section --section "Tasks" \
      --content-file <scratch file> --session <session id> --hint "handoff <file>: <what changed>"
  python3 <base_dir>/../../scripts/gt_broker.py drain --vault "<vault>"
  ```

  The broker records the section as it was when you queued and **escalates** (a task for the
  owner, not a write) if anyone changed it in between — re-read and queue again rather than
  forcing it. Report the drain in one line: `N applied, N held, N escalated`. **Held** means
  another live session claims the README; it waits for the next drain — never work around it,
  and do not mark the handoff `handled` until it is applied.

A decision for `decisions.md` or `research.md` goes through `/gt:gt-work`, which queues it.

### Step H4 — Set the handoff's status

```bash
# every item settled (done, dropped, moved, or kept as a task that now carries it) -- the same
# close /gt:gt-close handoff runs; it refuses while a task citing the handoff is still open:
python3 <close> handoff <handoff> --vault "<vault>" --reason "<one line>"

# not now -- the whole handoff comes back on that date:
python3 <scripts>/gt_handoff_status.py mark <handoff> --vault "<vault>" --status deferred --until YYYY-MM-DD --reason "<why>"
```

- A deferral **must** carry a date; the tool refuses one without. "Later, some time" is a decision
  to drop it — close it as handled and say so in the reason.
- A handoff whose citing tasks are ALL checked off counts as handled automatically; closing it is
  still worth doing, because the reason lands in its status log.
- Never delete a handoff. It is someone's record of a session; its status says it is dealt with.

Then offer the next waiting handoff, or stop.

### Handoff rules

- Never read a handoff the user did not choose.
- Never mark a handoff handled while an item on it is undecided — defer it instead.

## Tasks

### Step T1 — Choose the set

Map the user's parameter to filters exactly as `/gt:gt-list tasks` does (`<slug>`, `p1`, `mine`,
`overdue`, `stale`, `deferred`, `ref:<text>`), then:

```bash
python3 <tool> list --vault "<vault>" <filters>
```

Say how many there are. With no parameter and a long list, propose a first cut (`stale`,
`overdue`, or one project) rather than starting at the top of everything.

### Step T2 — One task at a time

Show the task line. **Load context only if deciding needs it**: the file its `ref::` points at, or
one named file — never a full `/gt:gt-open`. Then ask the user for ONE of:

| decision | command |
|---|---|
| **done (close)** | `python3 <close> task <ID> --vault "<vault>" --reason "<what settled it>"` |
| **drop** | `python3 <tool> drop <ID> --vault "<vault>" --reason "<why it no longer matters>"` |
| **defer** | `python3 <tool> defer <ID> --vault "<vault>" --until YYYY-MM-DD --reason "<why then>"` |
| **move** | `python3 <tool> move <ID> --vault "<vault>" --to <slug> --reason "<why it belongs there>"` |
| **keep** | nothing — move on |

- Close is `/gt:gt-close task`'s logic: `gt_task.py done`, then the TASKS.md rollup, which records
  the `task.done` event.
- Nothing is deleted: done, drop and move check the box and record the reason on the line. Drop
  needs a reason; that is what makes clearing a backlog safe.
- A deferral needs a future date and hides the task — from lists, the TASKS.md ranking and
  escalation — until then. "Later, some time" is a drop, said as one.
- If the tool says the ID changed (someone edited the file), list again and use the new ID; never
  edit the README by hand to get around it.
- If a decision belongs in `decisions.md` or `research.md`, record it there too — a task line is not
  where a decision lives. That write goes through `/gt:gt-work` (an ADR through `gt_adr.py`, a
  research entry through the write queue), never a direct edit: the PreToolUse guard denies a
  Write/Edit to vault content (Core rule 1).

Register the session and claim each README before its first write (Core rule 1); the tool refuses
if another live session holds it. The tool is the write path for task lines — it re-checks the
line's hash itself and goes through the write queue.

### Step T3 — Stop cleanly

When the set is done or the user stops: one line with the counts (closed, dropped, deferred,
moved, kept), then regenerate the rollup so "what's next" is current:

```bash
python3 <vault>/Projects/golden-thread/tools/gt_tasks.py --vault "<vault>"
```

## A #conflict task on `design.md` or `global-memory/`

The broker escalates a write to those two (it does not apply it), and since 0.20.2 no gt MCP tool
does either: they are changed by the **owner, in an editor**. A `#conflict` task from the broker
points (`ref::`) at its conflict file in `Projects/golden-thread/spool/broker/conflicts/`, which
holds every version in full and the target as it stood. Offer to read that file with the user and
help them decide which version stands, or merge them in a scratch copy — then they make the edit
themselves and close the task here (`/gt:gt-handle task`). Never apply it for them, and never try
to route around the refusal.
