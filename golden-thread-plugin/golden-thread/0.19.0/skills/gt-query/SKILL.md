---
name: gt-query
description: "Look up a topic in the Golden Thread vault — reads index.md first, follows wiki links, falls back to grep across Knowledge and global-memory."
model_intent: fast
---

# Golden Thread Query

Look something up in the vault. The vault is the single source of truth — always start here before searching the web or guessing.

## Steps

**Step 1 — Read vault-config.json**

Use `$GT_VAULT` if it is set (a session pinned to one vault, such as the demo); otherwise get the vault path from `~/.claude/vault-config.json`. If missing → tell user to run `/gt-init`.

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

`log.md` is generated — never append to it directly (the PreToolUse guard denies it). Add the
line through its own tool:
```bash
python3 "<vault>/Projects/golden-thread/tools/gt_log.py" --vault "<vault>" add \
    '<today> [query] "<topic>" → <found|not found> — <page name or "no match">'
```

**Step 8 — Stamp the pages you used (after the answer, never before)**

Once the answer is in front of the user, record which Knowledge pages it was drawn from, so the
wiki lint's `review-due` check measures when a page was last READ rather than last edited:
```bash
python3 <base_dir>/../../scripts/gt_review_stamp.py --vault "<vault>" "Knowledge/<Page>.md" ...
```
Only pages you actually read and used — not every page a grep matched. It writes
`last_reviewed: <today>` through the write queue (never edit the frontmatter yourself), skips a
page already stamped today, and does nothing when the `review_stamp` setting is `off`. If the
query found nothing, or the read failed, stamp nothing.

## Notes

- Always read the full Knowledge page, not just the preview — the detail is in the content
- If a page has `status: stale`, mention it: "Note: this page is marked stale — it may be outdated"
- If a page has a `sources:` frontmatter field, those are the authoritative references

## Decision lineage — "what is the history of decisions about X?" (0.18.1)

Asked how a decision came to be, or `--lineage <topic>`: trace the supersession chain in the
project's ADRs instead of reading `decisions.md` top to bottom.

```bash
python3 "<vault>/Projects/golden-thread/tools/gt_adr.py" --vault "<vault>" lineage <project> "<topic>"
```

It finds every ADR whose title or body mentions the topic, follows `Supersedes:` fields both
ways, and prints each chain oldest first: number, date, title, the reason each was superseded
(the superseding ADR's Context), and the non-superseded one labelled **current** with its
rejected alternatives. "No decisions found for topic '<topic>'" means none mention it — say so;
do not fall back to guessing. One project at a time (a sub-project's bare slug works). Chains
are only as good as the `Supersedes:` fields: an ADR that replaced another in prose alone shows
as its own one-entry chain — offer to record the link in a new ADR.

## Entity lookup — "what do we know about X?" (0.18.1)

Asked about a thing (a service, a host, a component) or `--entity <name>`: load only the
memory files that declare it in `entities:` frontmatter, not every memory file.

```bash
python3 <base_dir>/../../scripts/gt_entities.py --vault "<vault>" lookup "<name>" --project <project>
```

`<base_dir>` is the path in the `Base directory for this skill:` header. Matching is
case-insensitive substring on the declared names. Each hit prints its MEMORY.md description
and the file. "No memory files tagged with entity '<name>'" — then continue with the steps
above (index, grep), and mention that tagging the relevant memory files with `entities:` would
make this lookup work next time. `list --project <project>` shows every declared entity.
