---
name: gt-task
description: "Create a task the way a developer drops a TODO into code — one line, well-formed, optionally tied to a vault page, wiki page or source. Use when the user says: /gt-task, add a task, make a task for, remind me to, todo:, track this as a task."
---

# Golden Thread — Create a Task

The user gives the task in their own words; this writes it through a tool so the line is always
well-formed (`[p::] [waiting::] [since::]`) and ranks in `TASKS.md` exactly like a hand-written
one. Tasks live in the project README's `## Tasks` — one store, nothing new.

Vault: `$GT_VAULT` if set, else `vault_path` from `~/.claude/vault-config.json`.
Tool: `<vault>/Projects/golden-thread/tools/gt_task.py`.

## Decide, don't ask, where you can

- **Project** — the one this session is working in. None open, or the user names none → ask once,
  offering `--inbox` (unfiled; `/gt:gt-review` routes it later).
- **Priority** — `--p 2` unless the user says urgent (`1`) or someday (`3`). Never `p:: 0`: it
  does not exist for tasks.
- **Waiting** — `user` unless the task is plainly work for the assistant (`agent`) or someone else
  (`external`).
- **Ref** — when the user ties it to something: `--ref "[[Wiki Page]]"`, or a vault path
  (`Sources/2026-09-28 …md`, `Projects/x/design.md`). The tool refuses a ref that resolves to
  nothing; if it does, tell the user what it looked for.
- **Due** — only if the user gave a date.

## Run it

```bash
python3 <vault>/Projects/golden-thread/tools/gt_task.py add "<the task, one line>" \
  --vault "<vault>" --project <slug> [--ref "<ref>"] [--p N] [--waiting W] [--due YYYY-MM-DD]
```

Register the session and claim the README first (Core rule 1); the tool also refuses if another
live session holds it. Echo the line it wrote and its ID. Nothing else — do not start the task.
