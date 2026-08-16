---
name: gt-refresh
description: "Check vault sources for upstream changes and supersede outdated ones. Use when the user says: refresh the vault, check sources for updates, is the wiki still current, update from upstream. Sources are immutable — updating means superseding with a new file, never editing the old one."
---

# Golden Thread Refresh

Check Sources/ for upstream changes. Sources are immutable: "updating" means creating a new source file that supersedes the old one — the old file stays as the historical record forever.

## Vault location

Read `~/.claude/vault-config.json` for `vault_path`. If missing → tell the user to run `/gt:gt-init` first.

## Immutability rule

**`Sources/` files are never modified after creation.** This is absolute. If a source has changed upstream:
1. Fetch the new version
2. Store it as a NEW file: `Sources/YYYY-MM-DD <title>.md`
3. Set `supersedes: ["Sources/YYYY-MM-DD <old-title>.md"]` in the new file's frontmatter
4. Update Knowledge pages to cite the new source
5. The old source file stays on disk unchanged as the historical record

## Source file format

Every file in `Sources/` has frontmatter:
```yaml
---
title: <human-readable title>
url: <original URL, if web source>
local_path: <original file path, if local source>
fetched: <YYYY-MM-DD>
supersedes: []   # list any source filenames this replaces
---
```

## Steps

**Step 1 — Select scope**

List source files in `<vault>/Sources/` that have a `url:` or `local_path:` field in their frontmatter (grep for `^url:` or `^local_path:`). Present the list and ask which to check this run. Default: all sources with a remote URL.

**Step 2 — Fetch and compare**

For each in-scope source:
- Web URL: fetch the current version with WebFetch
- Local path: read the current file from disk
- Compare against the stored source content

**Unchanged**: note it briefly and move on.

**Changed**: proceed to Step 3.

**Step 3 — Supersede on change**

For a changed source:

1. Store the new version as a fresh immutable file:
   ```
   Sources/YYYY-MM-DD <title>.md
   ```
   Frontmatter:
   ```yaml
   ---
   title: <title>
   url: <url>
   fetched: <today>
   supersedes: ["<old-source-filename>"]
   ---
   ```
   Followed by the full raw content.

2. Find all Knowledge pages that cite the old source:
   ```bash
   grep -rl "<old-source-filename>" "<vault>/Knowledge/"
   ```

3. For each citing Knowledge page:
   - Review what changed between old and new source
   - Propose content updates where the facts changed
   - Update `sources:` frontmatter to point at the new file
   - If content was revised, set `status: growing`
   - **Owner approves** each update before writing

4. The old source file is left exactly as-is.

**Step 4 — Log**

Append to `<vault>/log.md`:
```
<today> [refresh] <N> sources checked, <M> unchanged, <P> superseded, <Q> Knowledge pages updated
```

For each superseded source, add a line:
```
<today> [supersede] Sources/<old>.md → Sources/<new>.md
```

## Rules

- Never edit a file in `Sources/` — not even a typo fix. Corrections go in a new superseding file.
- Supersession is tracked in ONE place: the new source's `supersedes:` field. This is how `gt-lint` detects stale Knowledge pages (superseded-cited check).
- Only fetch sources that are in scope for this run. Never fetch things the user didn't ask about.
- Owner approves every Knowledge page update before it's written.
