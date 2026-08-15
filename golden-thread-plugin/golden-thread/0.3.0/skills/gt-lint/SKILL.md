---
name: gt-lint
description: "Audit the Golden Thread vault for broken links, orphaned pages, missing index entries, unlisted memory files, bloated global-memory files, project-specific facts in global scope, and Knowledge pages citing superseded sources."
---

# Golden Thread Lint

Health check for the vault. Run periodically to catch structural drift.

## Steps

**Step 1 — Locate vault**

Read `~/.claude/vault-config.json`. If missing → tell user to run `/gt:gt-init`.

**Step 2 — Run gt_lint.py**

The script is at `<base_dir>/../../scripts/gt_lint.py`, where `<base_dir>` is the path shown in the `Base directory for this skill:` header. Run:
```bash
python3 <base_dir>/../../scripts/gt_lint.py "<vault>" --queue "<vault>/review-queue.md"
```

Parse the output.

**Step 3 — Interpret findings**

For each finding category, explain what it means and propose a fix:

**`index-gap`** — A Knowledge page exists but has no entry in `index.md`.
> "`Knowledge/<page>.md` isn't in the index. It won't be found by `/gt:gt-query`."
> Fix: show the line to add. Ask "Add it now?"

**`broken-link`** — A `[[wikilink]]` doesn't resolve to any file.
> "File X has a link to `[[Y]]` but that page doesn't exist. Either the page was renamed, or the link is wrong."
> Fix: show the link + surrounding context. Ask "Remove the link, or create the target page?"

**`orphan`** — A Knowledge page isn't reachable from index.md or any other page.
> "`Knowledge/<page>.md` isn't linked from anywhere — it's an island."
> Fix: add it to index.md or link it from a related page.

**`memory-unlisted`** — A file in `Projects/*/memory/` isn't referenced in that project's `MEMORY.md`.
> "`Projects/<project>/memory/<file>.md` isn't in the MEMORY.md index. Claude won't load it at session start."
> Fix: show the line to add. Ask "Add it now?"

**`global-gap`** — A file in `global-memory/` isn't reachable from `MEMORY.md`.
> "`global-memory/<file>.md` isn't in the global MEMORY.md index. It won't be loaded cross-project."
> Fix: show the line to add. Ask "Add it now?"

**`memory-bloat`** — A `global-memory/` file exceeds 30 lines.
> "`global-memory/<file>.md` has N lines. Global-memory files should stay under 30 lines — full reference tables belong in `Knowledge/`."
> Fix: propose moving detailed content to a new Knowledge page and replacing it with a pointer + 3-5 essential constants. Ask "Trim it now?"

**`global-scope-leak`** — A `global-memory/` file references a project slug.
> "`global-memory/<file>.md` mentions project slug `<slug>`. Global-memory should contain only cross-project facts."
> Fix: propose moving the project-specific content to `Projects/<slug>/memory/` or `Projects/<slug>/decisions.md`. Ask "Move it now?"

**`superseded-cited`** — A Knowledge page's `sources:` field cites a Source file that has been superseded.
> "`Knowledge/<page>.md` still cites `Sources/<old>.md`, but that source was superseded by `Sources/<new>.md`."
> This means the Knowledge page may contain outdated information.
> Fix: review what changed between old and new source, update the Knowledge page content if needed, then update `sources:` to point at the new file. Ask "Review and update now?"
> **This is the only finding that may justify setting `status: stale`** — if the source changed significantly and the page content can't be confidently updated this session.

**`stale`** — A Knowledge page has `status: stale`.
> "`Knowledge/<page>.md` is marked stale. Either update it or retire it via `/gt:gt-promote`."

**Step 4 — Approval loop**

For each proposed fix:
- Present the change
- Ask yes/no
- If yes → apply it immediately
- If no → ask "Add to lint-declines.md to suppress this in future?" and record if confirmed

Suppression format in `<vault>/lint-declines.md`:
```
suppress: Knowledge/page.md
suppress: Knowledge/page.md:[[BrokenLink]]
```

**Step 5 — Summary**

```
Lint complete.

  Checked: N Knowledge pages, M memory files, P wiki links, Q sources
  Found:   X issues
  Fixed:   Y (with your approval)
  Suppressed: Z (added to lint-declines.md)
  Remaining: W (need manual attention)
```

If the vault is clean: "✓ Vault is healthy — no issues found."
