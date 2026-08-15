---
name: gt-wiki-refresh
description: "Check the user's LLM Wiki sources for upstream changes and supersede outdated ones. Use when the user says: refresh the wiki, check sources for updates, is the wiki still current, update from upstream. Runs on a user-selected subset of sources by default. Vault path via ~/.claude/vault-config.json."
---

# LLM Wiki — Refresh

Sources are immutable, so "updating" a source means superseding it: a new
source file replaces the old one's role while the old file stays untouched
on record. Vault path from `~/.claude/vault-config.json`.

## Page format

Read `<vault>/Knowledge/_template.md` before updating any Knowledge page.
It is the single source of truth for frontmatter fields, categories, status
values, and conventions.

## Scope

Default: ask the user which sources (or topic area) to refresh this run.
`--all`: every source with a `remote:` or `url:` field. Refresh is the ONE
sanctioned reason to fetch external resources; fetch only the sources in
scope, nothing else.

## Workflow

1. **Select scope** — list candidate sources (those with `remote:`/`url:`)
   and confirm the subset with the user.

2. **Fetch and compare** — retrieve each in-scope source's upstream version
   and compare against the stored copy. Unchanged: note it and move on.

3. **Supersede on change** — for a changed source:
   - Ingest the new version as a NEW file `Sources/YYYY-MM-DD <title>.md`
     with a `supersedes:` frontmatter list naming the old source file(s)
   - NEVER edit the old source — it stays as the historical record
   - Update every Knowledge page citing the old source: revise content that
     changed, repoint `sources:` at the new file, set status per the change
     (content revised → `growing`)
   - The lint script detects any citing pages that were missed
     (superseded-cited check)

4. **Cross-linking** — apply the same cross-link rules as ingest: update
   bidirectional links on any page you touch.

5. **Log** — append a `refresh` entry to `log.md`: sources checked,
   unchanged, superseded, pages updated.

## Convention summary

Supersession is tracked in ONE place: the new source's `supersedes:` field.
Old sources are never touched; lint and refresh derive superseded status by
scanning `supersedes:` fields.
