---
name: gt-refresh
description: "Check a Golden Thread vault's Sources/ for upstream changes and supersede outdated ones. Use when the user says: refresh the vault, check the vault's sources, are my project sources still current, supersede a stale source. For a standalone LLM Wiki vault use gt-wiki-refresh instead. Sources are immutable — updating means superseding with a new file, never editing the old one."
model_intent: balanced
---

# Golden Thread Refresh

**Under gt sandbox mode (preview)** (the session-start line says SANDBOX MODE): this skill's shell steps are refused — each refused tool prints one line with the exact command. Instead, read with `vault_read` / `vault_search` / `vault_list` and write through `vault_queue_write`; ask the user to run the `gt_log` entry from a terminal. With sandbox mode off (the default) nothing changes.

Check Sources/ for upstream changes. Sources are immutable: "updating" means creating a new source file that supersedes the old one — the old file stays as the historical record forever.

A standalone LLM Wiki vault is refreshed by `/gt-wiki:gt-wiki-refresh`, from the optional
**gt-wiki** module. If the gt-wiki plugin is not installed (its `gt-wiki:*` skills are not
listed), say so and name `bash install.sh --with wiki` instead of invoking it.

## Vault location

Use `$GT_VAULT` if it is set (a session pinned to one vault, such as the demo); otherwise read `~/.claude/vault-config.json` for `vault_path`. If missing → tell the user to run `/gt:gt-init` first.

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

   A Knowledge page is vault content, so each approved update goes through the write
   queue (Core rule 1), never Write, Edit or a shell redirect. Revised content: write the
   section's new body to a scratch file outside the vault and queue it with
   `--op replace-section`. Frontmatter: one `--op set-property` per key.
   ```bash
   python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
     --path "Knowledge/<page>.md" --op replace-section --section "<heading>" \
     --content-file <scratch>/<page>-<heading>.md --hint "refresh: <new-source-filename>"
   python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
     --path "Knowledge/<page>.md" --op set-property --key sources \
     --value '["Sources/<new-source-filename>"]'
   python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
     --path "Knowledge/<page>.md" --op set-property --key status --value growing
   ```
   When every approved page is queued, apply them once and tell the user in one line
   what the broker applied, held or escalated:
   ```bash
   python3 <base_dir>/../../scripts/gt_broker.py drain --vault "<vault>"
   ```
   The new `Sources/` file is not queued — `Sources/` sits outside the queue, and is
   written once, as above, and never again.

4. The old source file is left exactly as-is.

**Step 4 — Log**

Record the summary with `gt_log.py` (below), in this shape:
```
<today> [refresh] <N> sources checked, <M> unchanged, <P> superseded, <Q> Knowledge pages updated
```

For each superseded source, add a line **and its event in the same command**, right
after the new source file is written:
```bash
python3 "<vault>/Projects/golden-thread/tools/gt_log.py" --vault "<vault>" add \
  "<today> [supersede] Sources/<old>.md → Sources/<new>.md" \
  --event source.supersede --item "Sources/<new>.md" \
  --from "Sources/<old>.md" --to "Sources/<new>.md"
```
Use `gt_log.py --vault "<vault>" add` for the `[refresh]` summary line too (no `--event`); never append
to `log.md` by hand.

## Rules

- Never edit a file in `Sources/` — not even a typo fix. Corrections go in a new superseding file.
- Supersession is tracked in ONE place: the new source's `supersedes:` field. This is how `gt-lint` detects stale Knowledge pages (superseded-cited check).
- Only fetch sources that are in scope for this run. Never fetch things the user didn't ask about.
- Owner approves every Knowledge page update before it's written.
