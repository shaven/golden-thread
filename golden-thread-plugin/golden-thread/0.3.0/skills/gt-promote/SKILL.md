---
name: gt-promote
description: "Graduate a fact, finding, or idea up the knowledge hierarchy: project memory → decisions/research → Knowledge wiki page → global-memory."
---

# Golden Thread Promote

Move knowledge up the hierarchy so it's available in the right scope.

## The Hierarchy

```
session conversation
      ↓ graduate
project memory (memory/*.md)
      ↓ graduate
project files (decisions.md / research.md / design.md)
      ↓ graduate
Knowledge/ wiki page (applies beyond this project)
      ↓ graduate
global-memory/ (loaded in every session, all projects)
```

An idea that doesn't fit in any existing project can become a new project scaffold.

## Steps

**Step 1 — Identify what to promote**

Ask: "What would you like to promote? You can describe it, paste the content, or give me a filename."

Also accept: "I want to review promotion candidates" → scan recent entries in research.md and decisions.md for items that have `→ promote` or `candidate` notes, plus any `status: seed` Knowledge pages that might be ready to graduate to `growing`.

**Step 2 — Determine destination**

Ask (if not obvious from context):
1. "Does this apply only to `<project-slug>`, or to other projects too?"
   - Project-only → goes into decisions.md or research.md (if not already there)
   - Cross-project → goes into `Knowledge/` as a wiki page
2. "Is this something every Claude Code session should know about, regardless of project?"
   - Yes → goes into `global-memory/`

**Step 3 — Execute**

**memory/*.md → decisions.md or research.md**
- If it's a stable rule/constraint → append as an ADR to decisions.md
- If it's a finding/gotcha → append as a dated entry to research.md
- Log: `relocate`

**decisions/research → Knowledge/<page>.md**
- Write the page with proper frontmatter:
  ```yaml
  ---
  title: <descriptive title>
  category: <runbook|decision|reference|concept>
  tags: [<relevant tags>]
  sources: []
  created: <today>
  updated: <today>
  status: seed
  ---
  ```
- Add a one-line entry to `<vault>/index.md` under the appropriate category heading
- Add `[[<page title>]]` cross-links from any related Knowledge pages
- Log: `graduate`

**Knowledge/<page>.md → global-memory/**
- **Global-scope check — ask before writing:**
  > "This will be loaded in EVERY session for EVERY project. Does it contain zero project-specific facts — no project slugs, no service URLs specific to one project, no team-specific process? And has it proven useful in at least 2 unrelated projects?"
  - If no → keep in `Knowledge/` and suppress the promotion
- Keep the file under 30 lines. If more is needed, the detail belongs in a `Knowledge/` page that `global-memory/` points to.
- Write to `<vault>/global-memory/<slug>.md`
- Add entry to `<vault>/global-memory/MEMORY.md`
- Log: `graduate`

**Any level → new project scaffold**
- If the item is an idea for a separate project, run:
  ```bash
  python3 <base_dir>/../../scripts/vault_init.py create-project --vault "<vault>" --name "<new-slug>"
  ```
- Write the idea into the new project's `idea.md`
- Add to `<vault>/Projects/README.md`
- Log: `graduate`

**Retiring (removing from active use)**
- When a page is superseded or no longer accurate: update its `status:` to `stale`
- Never delete — just mark stale and note what superseded it
- Log: `retire`

## Log Entry

Always append to `<vault>/log.md` with the appropriate verb:
```
<today> [graduate] <source> → <destination>: <one-line description>
<today> [retire] Knowledge/<page>.md: superseded by <new-page>
<today> [relocate] <source> → <dest>: reclassified
```
