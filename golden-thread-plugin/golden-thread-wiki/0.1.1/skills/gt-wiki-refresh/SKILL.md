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
`--all`: every source with a `local:` or `url:` field. Refresh is the ONE
sanctioned reason to fetch external resources; fetch only the sources in
scope, nothing else.

## Workflow

1. **Select scope** — list candidate sources (those with `local:`/`url:`)
   and confirm the subset with the user.

2. **Detect changes** — run `wiki_refresh.py` for the selected scope:
   ```
   python3 <plugin>/scripts/wiki_refresh.py <vault> --all [--no-fetch] [--json]
   # or scope by page: --page "Page Title"
   # or scope by repo: --repo reponame
   # or scope by topic: --topic keyword
   ```
   `<plugin>` is the `Base directory for this skill:` path two levels up
   (`../../`). The script prints `[unchanged]` / `[changed]` / `[fetch-needed]`
   per source. Exit 0 = nothing changed; 1 = changes found; 2 = error.
   For changed sources, re-run with `--json` to get the full diff. For
   `[fetch-needed]` (web-only sources), fetch the URL with WebFetch and
   compare manually against the stored source body.

3. **Supersede on change** — for a changed source:
   - Ingest the new version as a NEW file `Sources/YYYY-MM-DD <title>.md`
     with a `supersedes:` frontmatter list naming the old source file(s);
     for a local source also set `upstream_sha:` to the `head` SHA the script
     reported, so the next refresh diffs from exactly this point
   - NEVER edit the old source — it stays as the historical record
   - Update every Knowledge page citing the old source: revise content that
     changed, repoint `sources:` at the new file, set status per the change
     (content revised → `growing`)
   - The lint script detects any citing pages that were missed
     (superseded-cited check)

4. **Cross-linking** — apply the same cross-link rules as ingest: update
   bidirectional links on any page you touch.

5. **Log** — record the run with `wiki_log.py`:
   ```
   python3 <plugin>/scripts/wiki_log.py <vault> log refresh "<scope>" \
     --line "checked: N, unchanged: N" --line "superseded: <files>" \
     --line "pages updated: <titles>"
   ```

## Convention summary

Supersession is tracked in ONE place: the new source's `supersedes:` field.
Old sources are never touched; lint and refresh derive superseded status by
scanning `supersedes:` fields.
