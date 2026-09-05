---
name: gt-review
description: "Sweep INBOX.md (and daily notes, if configured) for captured-but-unfiled thoughts and route each one into a tracked project. Use when the user says: review the inbox, review my daily notes, what's uncaptured, what have I jotted down, file my inbox, turn my notes into projects. Vault path via ~/.claude/vault-config.json."
---

# Golden Thread Review

Surface captured-but-unfiled work and route each item into the Projects/ folder.

## Vault location

Read `~/.claude/vault-config.json` for `vault_path`. If missing → tell the user to run `/gt:gt-init` first.

## Sources, in order

1. **`<vault>/INBOX.md`** — the primary inbox. Any session, in any project, drops a
   checkbox line here the moment a thought arrives. The rollup shows unchecked lines
   at the top of `TASKS.md` under *Inbox*; this skill is what empties it.
2. **Daily notes**, only if `<vault>/CLAUDE.md` carries a `daily_notes_path` entry or
   `<vault>/Daily Notes/` exists. If neither, skip silently — do not ask the user to
   create one; INBOX.md is the inbox.

## Steps

**Step 1 — Read INBOX.md, then any daily notes**

Every unchecked `- [ ]` line in INBOX.md is an uncaptured item by definition. A
`[project:: slug]` field on the line is the user's hint about where it belongs;
honour it unless it is clearly wrong. In daily notes (last 2–4 weeks unless the user
specifies a range), look for:

- Open `- [ ]` checkbox tasks that don't contain a `[[wikilink]]` to a project
- Bullet points under notes or brain-dump sections that aren't linked to existing projects
- Text that looks like an idea, task, or piece of work not yet tracked

Skip:
- Completed tasks (`- [x]`)
- Tasks that already contain a `[[wikilink]]` (they're already captured)
- Dataview queries and template sections
- Pure reference items (links to PRs/issues that are just tracking)

**Step 2 — Cross-reference with existing projects**

Read `<vault>/Projects/README.md` to see all current projects. Check each uncaptured item against existing project names and slugs to avoid duplicates.

**Step 3 — Present findings**

Show uncaptured items grouped by daily note date. For each item, note whether it looks like:
- A new standalone project (substantial, independent work)
- A sub-project or task under an existing project
- A one-off task (worth tracking but not its own project)
- A Knowledge page candidate (platform or domain insight worth preserving)

Ask: "Which of these would you like to promote?"

**Step 4 — For each selected item, flesh it out**

Ask 2–3 targeted questions to build the idea:
- "What's the goal — one sentence?"
- "Which domain/tags does it belong to?" (per CONVENTIONS.md)
- "Is this under an existing project, or standalone?"

Then create the project using `/gt:gt-create` mechanics:
- Run `vault_init.py create-project` with the gathered info
- Fill `idea.md` with the original note text expanded by the user's answers — never summarize, capture everything

**Step 5 — Register in the master index**

Add the new project to `<vault>/Projects/README.md` (the script handles this).

**Step 6 — Mark the source line as filed**

For an INBOX.md line: check it off and append `→ [[slug]]` so the line reads as
history (`- [x] the thought → [[slug]]`). Never delete the line; the user prunes
checked lines when they choose. Register the session and claim `INBOX.md` before
editing it, like any shared vault file.

For a daily note: ask "Want me to update the daily note to link to the new project?"
If yes, annotate the original task line with a `[[slug]]` wikilink.

When the item becomes a task rather than a project, write it under the target
project's `## Tasks` with `[since:: <today>]` and the priority the user gave — an
idea gets `p:: 3` and **no due date**; a due date on an idea makes the deadline rule
escalate it above real work.

**Step 7 — Report**

```
Review complete.

  Inbox lines: I filed, J left
  Scanned: N daily notes (YYYY-MM-DD to YYYY-MM-DD)   (omit if none)
  Uncaptured items found: X
  Promoted to new projects: Y
  Added to existing projects: Z
  Skipped: W
```

**Step 8 — Regenerate the rollup**

```bash
python3 <vault>/Projects/golden-thread/tools/gt_tasks.py
```
so the *Inbox* section empties and the filed tasks appear under their projects.

## Rules

- Never summarize or lose content — `idea.md` should capture everything the user said
- Don't over-ask — if the user gives enough context, create without interrogating
- One-off tasks that clearly don't need a project folder can go in a shared `ideas.md` at `<vault>/Projects/ideas.md` if the user prefers
- Follow CONVENTIONS.md for tags — don't invent new taxonomy
