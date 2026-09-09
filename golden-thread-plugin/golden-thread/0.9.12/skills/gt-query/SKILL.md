---
name: gt-query
description: "Look up a topic in the Golden Thread vault — reads index.md first, follows wiki links, falls back to grep across Knowledge and global-memory."
---

# Golden Thread Query

Look something up in the vault. The vault is the single source of truth — always start here before searching the web or guessing.

## Steps

**Step 1 — Read vault-config.json**

Get vault path from `~/.claude/vault-config.json`. If missing → tell user to run `/gt-init`.

**Step 2 — Read index.md**

Read `<vault>/index.md`. Scan for a matching entry by topic keyword.

If found → follow the `[[wikilink]]` and read the Knowledge page. Summarize the relevant sections for the user.

**Step 3 — Follow links**

While reading a Knowledge page, follow any `[[wikilinks]]` that seem relevant to the query. A chain of 2-3 hops is normal — follow them.

**Step 4 — Grep fallback**

If index.md has no match: search `Knowledge/` and `global-memory/` by keyword:
```bash
grep -ril "<keyword>" "<vault>/Knowledge/" "<vault>/global-memory/" 2>/dev/null
```

Read any matching files and summarize relevant content.

**Step 5 — Project memory fallback**

If still nothing: check `<vault>/Projects/<current-project>/memory/MEMORY.md` and follow its links.

**Step 6 — Not found**

If the vault has nothing on the topic:
> "The vault doesn't have anything on `<topic>` yet. Options:
> 1. Run `/gt-ingest` to pull in existing notes or memory files on this topic
> 2. I can add what I know about it right now as a `status: seed` Knowledge page — run `/gt-promote` to write it"

**Step 7 — Log the query**

Append to `<vault>/log.md`:
```
<today> [query] "<topic>" → <found|not found> — <page name or "no match">
```

## Notes

- Always read the full Knowledge page, not just the preview — the detail is in the content
- If a page has `status: stale`, mention it: "Note: this page is marked stale — it may be outdated"
- If a page has a `sources:` frontmatter field, those are the authoritative references
