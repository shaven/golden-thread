---
name: gt-handoff-handle
description: "Deal with handoffs that are still waiting: list the ones not yet handled across every project, then work through one — close each open item, keep it as a task, or defer the whole handoff to a date. Use when the user says: handle the handoffs, deal with the handoff, clear the handoffs, defer that handoff, work through the handoffs. To only SEE them, use gt-handoff-list."
---

# Golden Thread — Handle a Handoff

A handoff exists because a session could **not** capture everything. Since 0.17.2 each one has a
status — `open`, `deferred` (to a date) or `handled` — and an open one keeps being shown at
session start (setting `handoff_surface`) until someone deals with it. This is where it gets
dealt with.

**Showing a handoff costs no context; handling one does.** So this skill loads nothing until the
user has picked ONE handoff, and then only that handoff and its project's task lines — not a
full `/gt:gt-open`.

## Vault location

Use `$GT_VAULT` if set; otherwise `vault_path` from `~/.claude/vault-config.json`.
`<scripts>` is `~/.claude/golden-thread/hooks` (where install.sh puts `gt_handoff_status.py`).

## Step 1 — List what is waiting

```bash
python3 <scripts>/gt_handoff_status.py list --vault "<vault>"
```

One line each: path, project, age, open items, and why it counts as waiting (never marked,
deferral ended). Show the list as a numbered menu, oldest first, and ask which to handle. If the
user named a project, pass `--project <slug>`. **Do not open any handoff before they choose.**

Nothing waiting → say so in one line and stop.

## Step 2 — Load only the chosen one

Read the chosen handoff, then the `## Tasks` section of its project's `README.md` — only the
lines that cite this handoff's filename. That is the whole context. If an item genuinely cannot
be decided without more (a `design.md`, a `source.md` before touching a host), read that one file
and say why.

## Step 3 — Walk the open items with the user

For each thing the handoff leaves unresolved (its uncaptured list, and each task citing it), ask
the user and apply ONE of:

- **Done** — check the task off (`- [x]`) with a one-line note of what settled it. If the
  decision belongs in `decisions.md` or `research.md`, write it there through the usual
  `/gt:gt-work` path; the task line is not where a decision lives.
- **Keep** — leave the task open. It is already `p:: 1` with a `since::` date, so it escalates the
  project on its own if it sits for a week.
- **Drop** — check it off with the reason (`— dropped: <why>`). Dropping is a decision, and it is
  recorded, never silently deleted.

An item with no task yet: raise one in the gt-work shape
(`- [ ] <item> — context in <handoff file> [p:: 1] [waiting:: user] [since:: YYYY-MM-DD]`) or
settle it now.

**How these reach `README.md` (Core rule 1).** Never edit the README directly — the PreToolUse
guard denies a Write/Edit to vault content. There are two sanctioned paths:

- **Done / Drop / a new task — the task tool first.** `<tool>` =
  `<vault>/Projects/golden-thread/tools/gt_task.py` changes exactly one line, re-checks that line's hash,
  and refuses if another live session claims the README — the same path `/gt:gt-task-handle` and
  `/gt:gt-task` use:

  ```bash
  python3 <tool> list --vault "<vault>" <slug>            # find the citing task's ID
  python3 <tool> done <ID> --vault "<vault>" --reason "<what settled it>"
  python3 <tool> drop <ID> --vault "<vault>" --reason "<why>"
  python3 <tool> add "<item> — context in <handoff file>" --vault "<vault>" --project <slug> --p 1 --waiting user
  ```

- **Anything the tool cannot address** (a line it does not parse as a task, an edit that is not
  done/drop/add) goes through the write queue as a `replace-section` of `## Tasks`. Write the
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

## Step 4 — Set the handoff's status

```bash
# every item settled (done, dropped, or kept as a task that now carries it):
python3 <scripts>/gt_handoff_status.py mark <handoff> --vault "<vault>" --status handled --reason "<one line>"

# not now — the whole handoff comes back on that date:
python3 <scripts>/gt_handoff_status.py mark <handoff> --vault "<vault>" --status deferred --until YYYY-MM-DD --reason "<why>"
```

- A deferral **must** carry a date; the tool refuses one without. "Later, some time" is a decision
  to drop it — mark it `handled` and say so in the reason.
- A handoff whose citing tasks are ALL checked off counts as handled automatically; marking it is
  still worth doing, because the reason lands in its status log.
- Never delete a handoff. It is someone's record of a session; its status says it is dealt with.

Then offer the next waiting handoff, or stop.

## Rules

- Never read a handoff the user did not choose.
- Never mark a handoff handled while an item on it is undecided — defer it instead.
- Pass `--vault` on every call (Core rule 2).
