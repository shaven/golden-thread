---
name: gt-open
description: "Load a project from the vault at the start of a session. Use when the user says: open project X, load project X, work on X, continue X, start on X. Reads the core project docs in order — source, idea, research, decisions, design — indexes memory files without loading them, then summarizes the project state and asks where to pick up."
---

# Golden Thread Open

Load a project and build full context before proceeding.

## Vault location

Read `~/.claude/vault-config.json` for `vault_path`. If missing → tell the user to run `/gt:gt-init` first.

## Steps

**Step 1 — Find the project**

List folders in `<vault>/Projects/`. Match the user's name against folder names (fuzzy match is fine for ambiguous input). If multiple match, list them and ask. If none match, say so and offer to create it with `/gt:gt-create`.

**Step 2 — Read CONVENTIONS.md and PROTOCOL.md (once per session)**

If not already in context this session, read:
- `<vault>/Projects/CONVENTIONS.md` — tag taxonomy, lifecycle phases, file roles
- `<vault>/Projects/PROTOCOL.md` — execution protocol and cross-project process rules

**Step 3 — Read the project README**

Read `<vault>/Projects/<slug>/README.md`. Its YAML frontmatter (`domain`,
`stage`, `topology`, `tags`) is the fastest read of where this project stands —
take it before the prose. Note:
- One-line vision
- Status board (sub-projects, phases, next actions)
- Stage (idea / researching / designing / implementing / done)
- Tags and related links

**Step 4 — Read documents in order**

Read all existing files in this sequence (skip any that don't exist):
1. `source.md` — **read this before anything else that might lead to touching code.** It says where the code actually lives, which hosts serve which role in which environment, and which files are shared across servers. Acting without it is how you edit the wrong box or overwrite a file that exists on three machines.
2. `idea.md` — original brain dump, the "why"
3. `research.md` or `research/` folder — findings, codebase analysis, gotchas
4. `decisions.md` — ADRs, architectural choices made
5. `design.md` — current implementation approach
6. `spec.md` — implementation spec ready for handoff (if exists)
7. `runbook.md` — operational procedures (if exists)

If `source.md` links a fleet page (`**Fleet:** [[INFRASTRUCTURE]]`), read that page too — it holds the host table the project deliberately does not duplicate.

**On `research.md`:** it is append-only and grows without bound. If it exceeds ~200 lines, do not read it whole — read its `##` headings to learn what is covered, then read only the entries relevant to what the user is about to do, plus the most recent few.

Then index the memory files — **do not read them all**:

8. `memory/MEMORY.md` — read this index only. It is one line per file (`- [Title](file.md) — description`), which is enough to know what exists and what each file covers.
9. **Do not follow the links yet.** Read an individual `memory/*.md` file only when the current task touches its subject, or the user asks for it.

This keeps session startup cheap. A project with 40 memory files costs ~45 lines to open instead of ~2,000, and the detail is still one read away the moment it is needed. Loading everything up front buys nothing and crowds out the context the actual work needs.

**Step 5 — Load sub-projects (if any)**

If the README status board references sub-projects (subfolders within the project), read their READMEs. Only go deeper into a sub-project if:
- The user named it specifically, OR
- The status board shows it as the active/next item

For sub-projects, follow the same read order (idea → research → decisions → design).

**Step 6 — Announce review queue**

Check `<vault>/review-queue.md`. If it has pending items, mention the count once:
> "The vault has N items waiting for review."

**Step 7 — Summarize and ask**

After loading, briefly tell the user:
- Current stage
- **Topology and targets** — the topology type and which hosts this project touches, so the user can correct a stale entry before work starts rather than after
- What the next action is (from the status board)
- Any blockers or open questions noted in the docs
- **What's available but not loaded** — how many memory files exist and roughly what they cover, so the user knows the depth is there to ask for

Then ask: "Where do you want to pick up?"

## Rules

- **Never modify project files during loading** — this is read-only context gathering
- **Load lazily.** `MEMORY.md` is an index, not a manifest to expand. Read a memory file when the work needs it, not because it is listed
- **Don't re-read files already in context** from this session
- **For large projects** with many sub-projects, read only the top-level README first and ask which sub-project to dive into
- If `idea.md` is missing, the project is uninitialized — offer to run `/gt:gt-create` to scaffold it properly
