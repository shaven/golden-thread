---
name: gt-close
description: "Close a project, a task or a handoff the guided way. A project: review every open task and handoff (close, drop, move to another project, or keep shelved), offer to graduate what it learned, then archive it in place — or move it to Archive/ on request. A task: mark it done. A handoff: mark it handled. Use when the user says: close the project, close out X, we're done with this project, wrap up the project, retire this project, close this task, mark that handoff handled, close the handoff."
model_intent: balanced
---

# Golden Thread — Close

The close verb, with the artifact as its argument (0.18.1):

| invoked as | closes |
|---|---|
| `/gt:gt-close project <slug>` (or `/gt:gt-close <slug>`) | a project — section **Project** |
| `/gt:gt-close task <id>` | one task — section **Task** |
| `/gt:gt-close handoff [<id>]` | one handoff — section **Handoff** |

Dispatch on the first word: `project`, `task`, `handoff`; an ID shaped `slug:LINE:HASH` is a task;
a path under `handoff/` is a handoff; any other word is a project slug.

**No argument:** if this session has a project open (it ran `/gt:gt-open <slug>`), propose closing
*that* project and wait for a yes. Otherwise — or if the context fits more than one thing — **ask
what to close.** Never guess: closing is the one verb here whose mistakes are expensive to undo.

Every step is propose-then-confirm: nothing is closed, moved or archived without the user's yes
for that item.

Vault: `$GT_VAULT` if set, else `vault_path` from `~/.claude/vault-config.json`; pass `--vault` on
every call (Core rule 2). `<close>` is `<base_dir>/../../scripts/gt_close.py`, `<tool>` is
`<vault>/Projects/golden-thread/tools/gt_task.py`. Every write below is a tool's, and the tools go
through the write queue — never Write/Edit a vault file from here (Core rule 1). Register the
session and claim the project's README before the first write.

## Project

### Step P1 — What stands in the way

```bash
python3 <close> project <slug> --vault "<vault>"
```

It lists every open task in the project and its sub-projects — deferred ones too, since a deferral
comes back — every handoff still open or deferred, and how many `research.md` entries there are.
Exit 1 means **something is undecided, and the close halts there** until each item has a
disposition. Show the list; say the count.

### Step P2 — One item at a time

For each **task**, ask for ONE of:

| decision | command |
|---|---|
| **close** — it is done | `python3 <close> task <ID> --vault "<vault>" --reason "<what settled it>"` |
| **drop** — it no longer matters | `python3 <tool> drop <ID> --vault "<vault>" --reason "<why>"` |
| **move** — it belongs to another project | `python3 <tool> move <ID> --vault "<vault>" --to <slug> --reason "<why>"` |
| **keep** — as a record, never ranked again | `python3 <tool> shelve <ID> --vault "<vault>" --reason "<why kept>"` (sets `p:: 7`) |

For each **handoff**: walk its open items exactly as `/gt:gt-handle handoff` does (read that
skill's **Handoffs** section — `<base_dir>/../gt-handle/SKILL.md`), then close it as in **Handoff**
below. A handoff cannot be "kept" in a project being archived: it would surface at every session
start forever. Its items move (as tasks) or close.

If the user will not decide an item, **stop the close** and say what is left. Do not archive a
project with an undecided item; that is the gate, and `<close>` enforces it.

Re-run Step P1 until it exits 0.

### Step P3 — Graduate what it learned

A closing project is the last cheap moment to lift findings out of it. Read `research.md`'s `##`
headings (Step P1 printed the count; for a long file read headings, not the whole file). Present
the candidates **one at a time** — a finding that would help another project, a convention, a
platform fact — and for each ask: **promote** (run `/gt:gt-promote` for that item: its mechanics,
its checks, its write path), **defer** (leave it, it stays readable in the archive), or **drop**.
Skip entries already promoted (they link a `[[Knowledge page]]`). Decisions in `decisions.md` that
apply beyond this project are candidates too. Nothing is lost by skipping: archiving is not
deleting.

### Step P4 — Archive

Ask for a one-line reason, then:

```bash
python3 <close> project <slug> --vault "<vault>" --archive --reason "<one line>" --dry-run
python3 <close> project <slug> --vault "<vault>" --archive --reason "<one line>"
```

**In place, by default** (owner decision, 2026-10-01): vault_init's `archive-project` sets
`stage: archived` and `archived: <date>` in the README, adds an *Archived* banner, marks the
project's row in `Projects/README.md` as `archived`, and records an `archive` event. Nothing moves,
every `[[wikilink]]` still resolves, and the project drops out of `TASKS.md` (its open tasks were
all decided in Step P2).

**`--move`, only when the user asks for it:** after archiving in place, the folder is relocated to
`Archive/<slug>/`, and every path reference to `Projects/<slug>/` elsewhere in the vault is
re-pointed through the write queue (drained at once; an edit made in between is escalated, never
overwritten). It refuses while any live session claims a file in the project. Files the queue may
not write — `log.md`, `decisions.md`, `Sources/`, spools — keep the old path as history, and the
count is reported. A `relocate` event is recorded.

Report the JSON rows in a few lines: what was updated, anything `skipped` or `held`.

### Step P5 — Record

```bash
python3 "<vault>/Projects/golden-thread/tools/gt_log.py" --vault "<vault>" add "<date time tz> [retire] <slug> — closed: <reason>"
python3 "<vault>/Projects/golden-thread/tools/gt_tasks.py" --vault "<vault>"
```

## Task

```bash
python3 <close> task <ID> --vault "<vault>" --reason "<what settled it>"
```

`gt_task.py done` checks the line off in the project README (through the write queue; it refuses
an ID whose line changed since it was listed — list again and use the new ID), then the `TASKS.md`
rollup runs, which records the `task.done` event. Echo what it printed. If it says the README is
held by another live session, the close is queued, not lost; say so.

No ID given → list the session's project's open tasks (`/gt:gt-list tasks <slug>`) and ask which.

## Handoff

```bash
python3 ~/.claude/golden-thread/hooks/gt_handoff_status.py list --vault "<vault>"   # no id given: pick one
python3 <close> handoff <path> --vault "<vault>" --reason "<one line>"
```

Marks the handoff `handled` (status and a status-log line, through the write queue) and records a
`retire` event whose note starts `handoff.close`. **It refuses while any task citing the handoff
is still open** — settle those first (`/gt:gt-handle handoff` walks them). Never delete a handoff;
its status says it is dealt with.

## Rules

- **Ask, never guess, what to close.** No argument and no single obvious candidate → ask.
- **Halt on an undecided item.** A project with an open task or handoff nobody has decided on is
  not closed; say what is left.
- **Archive in place unless the user asked for `--move`.**
- Every vault write is a tool's, through the write queue. No Write/Edit on a vault path.
