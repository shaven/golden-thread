---
name: gt-wiki-ingest
description: "Add material to the user's LLM Wiki. Use when the user says: add this to the wiki, ingest this article / doc / README / transcript, capture this into the wiki, save this source, remember this, wiki this. Takes a URL, a file path, or pasted text; stores it immutably in Sources/ and starts the ingest discussion. Vault path via ~/.claude/vault-config.json."
---

# LLM Wiki — Ingest

Fetch or receive source material, store it immutably, and run the ingest
conversation. Vault path from `~/.claude/vault-config.json` (`vault_path`).

## Arguments

`$ARGUMENTS` is a URL, a local file path, or the literal text "paste".

## Page format

Read `<vault>/Knowledge/_template.md` before writing any Knowledge page.
It is the single source of truth for frontmatter fields, categories, status
values, and conventions.

## Workflow

1. **Fetch** — URL: retrieve with WebFetch. File path: read it. "paste" or
   empty: ask the user to paste the material.

2. **Duplicate check** — before storing, check BOTH:
   - `index.md` for pages already covering this topic
   - `Sources/` frontmatter for this URL or local path (grep for it)
   If the source already exists, say so and switch to updating instead.

3. **Store** — save raw content to `Sources/YYYY-MM-DD <source-title>.md`.
   Frontmatter fields per the vault's `CLAUDE.md` (distinguishes repo-file
   sources from web-only sources). Never modify a file in `Sources/` after
   creation.

4. **Read current state** — read `index.md` to know what already exists.

5. **Discuss** — present key findings: main concepts, which existing pages
   would update, what new pages this suggests. Ask what to emphasize.

6. **Wait for approval** — do NOT write pages until the user confirms.

7. **Write** — create/update pages in `Knowledge/` per `Knowledge/_template.md`.
   One concept per page.

8. **Cross-link** — the most important step. For every new or updated page
   add upstream, downstream, hub, and cross-domain links, and update
   existing pages to link back. Do not rely on the index alone: grep
   `Knowledge/` for the new page's title and key terms — pages that mention
   them are link candidates the index summary would not surface. Test
   mentally: from the index, is every piece of surrounding context reachable
   in 1-2 hops?

9. **Update navigation** — update `index.md`; append an `ingest` entry to
   `log.md`.

10. **Summarize** — report what was created, updated, and cross-linked.

## Conventions

- File names: Title Case with spaces — `Some Topic.md`
- Tags: lowercase, hyphenated
- One concept per page — split pages that cover two topics
- **Never consolidate.** Hub pages link to detail pages, never replace them
- Every page links to at least one other page (no orphans)
- When new content contradicts existing pages: flag it, add an inline note
  citing both sources, set status to `growing`

## Cross-Linking (Critical)

Links are the **primary navigation mechanism** — they replace RAG retrieval.
The LLM follows `[[wikilinks]]` hop by hop from `index.md`.

- **Link bidirectionally** — if A links to B, B links back to A
- **Link for traversal, not decoration**
- **Five link types** (where applicable): upstream, downstream, sibling,
  hub, cross-domain
- **Cross-domain links are the most commonly missed and the most valuable**
  — they enable multi-hop questions across areas

## Conversation-derived pages

A page created from a Q&A session still needs a source. Create a lightweight
`Sources/YYYY-MM-DD Conversation - <topic>.md` capturing the question, the
substance of the answer, and where the knowledge came from. This keeps the
page → source chain uniform: `sources:` is required on every page.

## Important

- Update existing pages when possible; create new ones only for new concepts.
- The user decides what matters, not the LLM. Keep it conversational.
