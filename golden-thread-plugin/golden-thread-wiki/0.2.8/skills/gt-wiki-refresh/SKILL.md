---
name: gt-wiki-refresh
description: "Check the user's LLM Wiki sources for upstream changes and supersede outdated ones. Use when the user says: refresh the wiki, check sources for updates, is the wiki still current, update from upstream. Runs on a user-selected subset of sources by default. Vault path via ~/.claude/vault-config.json."
model_intent: balanced
---

# LLM Wiki — Refresh

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `wiki_refresh.py <vault> --all …` | `vault_report` `{report: wiki_refresh}` (no fetch); a run that fetches upstream is **terminal** |
| a new `Sources/YYYY-MM-DD <title>.md` | `vault_source_store` `{name, content}` — write-once |
| `<gt-scripts>/gt_write_queue.py …` | `vault_queue_write` — the same `path`, `op`, `section`, `key` and `hint`, with the text itself as `content` (no `--session`: the server stamps its own). The broker decides at once and the result says apply, held or escalate. `design.md` and `global-memory/` are refused (owner only) |
| `<gt-scripts>/gt_broker.py drain` | nothing to run after `vault_queue_write`; `vault_queue_drain` if anything was queued from the shell |
| `wiki_log.py <vault> log …` (it runs the vault's `gt_log.py`) | `vault_log_add` `{line}`: the line it would write, `YYYY-MM-DD HH:MM TZ [<kind>] <subject> — <detail>` |

A **terminal** row is a step sandbox mode refuses on purpose — it changes settings or install state, or rewrites the vault wholesale. Give the user the exact command to run from a terminal, and carry on with the rest.

Sources are immutable, so "updating" a source means superseding it: a new
source file replaces the old one's role while the old file stays untouched
on record. Vault path: `$GT_VAULT` if set, else `vault_path` in `~/.claude/vault-config.json`.

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
   - Page updates go through the write queue, never a direct Write/Edit
     (Core rule 1; the gt guard denies it — the new `Sources/` file above is
     exempt and written directly). With gt's scripts at
     `~/.claude/plugins/cache/golden-thread-plugin/gt/<newest>/scripts/`:
     ```
     # revised content: one per changed section, body in a scratch file outside the vault
     python3 <gt-scripts>/gt_write_queue.py --vault "<vault>" \
       --path "Knowledge/<Page Title>.md" --op replace-section --section "<heading>" \
       --content-file <scratch file> --session <session id> --hint "refresh: <source>"
     # frontmatter: one key per request, no content file
     python3 <gt-scripts>/gt_write_queue.py --vault "<vault>" \
       --path "Knowledge/<Page Title>.md" --op set-property --key status --value growing \
       --session <session id> --hint "refresh: <source>"
     ```
     `replace-section` replaces the whole body of `## <heading>`, so the file
     carries everything you keep. `set-property` rewrites ONE `key: value`
     line: use it for `status:` and `updated:`, and for `sources:` only when
     the page declares it inline (`sources: ["[[Sources/a.md]]"]` →
     `--value '["[[Sources/<new>.md]]"]'`). A block-list `sources:` (one
     `- ` item per line, as in the template), or any frontmatter change
     set-property cannot express, is rewritten whole (the queue refuses
     set-property on a block-form key): read the page, rebuild its full text
     with the repointed list (and any revised content), and queue
     `--op replace-file --content-file <scratch file>` (no section). The base
     hash is recorded at queue time, so the broker escalates rather than
     overwrite a page changed in between. Queue either one replace-file or
     section/property requests for a page, not both.
   - The lint script detects any citing pages that were missed
     (superseded-cited check)

4. **Cross-linking** — apply the same cross-link rules as ingest: update
   bidirectional links on any page you touch, queued the same way (a link
   back is `--op append --section "Related"`). Then drain once, for all of
   steps 3–4:
   ```
   python3 <gt-scripts>/gt_broker.py drain --vault "<vault>"
   ```
   and report it in one line: applied N, held N, escalated N.

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
