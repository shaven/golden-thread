---
name: gt-review
description: "Scan daily notes for uncaptured ideas and tasks and promote them into tracked projects. Use when the user says: review my daily notes, what's uncaptured, project review, what have I jotted down, turn my notes into projects. Vault path via ~/.claude/vault-config.json."
---

# Golden Thread Review

Surface uncaptured work from daily notes and promote selected items into the Projects/ folder.

## Vault location

Read `~/.claude/vault-config.json` for `vault_path`. If missing → tell the user to run `/gt:gt-init` first.

Daily notes location: check `<vault>/CLAUDE.md` for a `daily_notes_path` config entry. If not set, default to `<vault>/Daily Notes/`. If that folder doesn't exist either, ask the user where their daily notes live and offer to save it.

## Steps

**Step 1 — Scan daily notes for uncaptured items**

Read recent daily notes (last 2–4 weeks unless the user specifies a range). Look for:

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

**Step 6 — Optionally link back to the daily note**

Ask: "Want me to update the daily note to link to the new project?" If yes, replace or annotate the original task line with a `[[slug]]` wikilink so it shows as captured.

**Step 7 — Report**

```
Review complete.

  Scanned: N daily notes (YYYY-MM-DD to YYYY-MM-DD)
  Uncaptured items found: X
  Promoted to new projects: Y
  Added to existing projects: Z
  Skipped: W
```

## Rules

- Never summarize or lose content — `idea.md` should capture everything the user said
- Don't over-ask — if the user gives enough context, create without interrogating
- One-off tasks that clearly don't need a project folder can go in a shared `ideas.md` at `<vault>/Projects/ideas.md` if the user prefers
- Follow CONVENTIONS.md for tags — don't invent new taxonomy
