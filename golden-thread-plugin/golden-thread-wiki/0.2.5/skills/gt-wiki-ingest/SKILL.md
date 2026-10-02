---
name: gt-wiki-ingest
description: "Add material to the user's LLM Wiki. Use when the user says: add this to the wiki, ingest this article / doc / README / transcript, capture this into the wiki, save this source, remember this, wiki this. Takes a URL, a file path, or pasted text; stores it immutably in Sources/ and starts the ingest discussion. Vault path via ~/.claude/vault-config.json."
---

# LLM Wiki — Ingest

Fetch or receive source material, store it immutably, and run the ingest
conversation. Vault path: `$GT_VAULT` if set, else `vault_path` in `~/.claude/vault-config.json`.

## Arguments

`$ARGUMENTS` is a URL, a local file path, or the literal text "paste".

## Page format

Read `<vault>/Knowledge/_template.md` before writing any Knowledge page.
It is the single source of truth for frontmatter fields, categories, status
values, and conventions.

## Writing to the vault (Core rule 1)

Knowledge pages are vault content: never Write/Edit them directly — the gt PreToolUse guard
denies it. Put each page body or section body in a scratch file outside the vault and queue it
with gt's `gt_write_queue.py` (installed at
`~/.claude/plugins/cache/golden-thread-plugin/gt/<newest>/scripts/`):

```bash
python3 <gt-scripts>/gt_write_queue.py --vault "<vault>" --path "Knowledge/<Page Title>.md" \
    --op create|replace-section|append|replace-file [--section "<heading>"] --content-file <scratch file> \
    --session <session id> --hint "<why>"
```

A section is a level-2 heading (`## <heading>`); `replace-section` replaces its **whole**
body, so the scratch file carries everything you keep. `Sources/` is exempt: a source file is
written directly (step 3). Nothing reaches a page until `gt_broker.py drain` (same directory)
runs — once, at the end of the write phase (step 8). This holds in every Golden Thread
vault (one with `Projects/golden-thread/`); only a standalone wiki with no gt installed has no
queue, and there the pages are written directly. The same applies to the other gt-wiki skills.

## Workflow

1. **Fetch** — URL: retrieve with WebFetch. File path: read it. "paste" or
   empty: ask the user to paste the material.

2. **Duplicate check** — before storing, check BOTH:
   - `index.md` for pages already covering this topic
   - `Sources/` frontmatter for this URL or local path (grep for it)
   If the source already exists, say so and switch to updating instead.

3. **Store** — save raw content to `Sources/YYYY-MM-DD <source-title>.md`.
   Frontmatter fields per the vault's `CLAUDE.md` (distinguishes repo-file
   sources from web-only sources). For a file inside a git repo, also record
   `upstream_sha:` — the output of `git -C <dir> log -1 --format=%H -- <file>`
   — so `/gt:gt-wiki-refresh` can later diff from exactly this point. Never
   modify a file in `Sources/` after creation.

4. **Read current state** — read `index.md` to know what already exists.

5. **Discuss** — present key findings: main concepts, which existing pages
   would update, what new pages this suggests. Ask what to emphasize.

6. **Wait for approval** — do NOT write pages until the user confirms.

7. **Write** — create/update pages in `Knowledge/` per `Knowledge/_template.md`.
   One concept per page. Through the queue: a new page is `--op create` with
   the whole page (frontmatter included) in the scratch file; an existing page
   is `--op replace-section --section "<heading>"` per section you revise.
   Frontmatter sits above the first heading, where sections cannot reach:
   change one key per request with `--op set-property --key <k> --value <v>`
   (no content file) — `status:`, `updated:`, or an inline list such as
   `sources: [...]`. A block-list key (`sources:` with one `- ` item per line)
   or any other frontmatter change set-property cannot express (the queue
   refuses set-property on a block-form key): read the page, rebuild its full
   text with the change made, and queue `--op replace-file --content-file
   <scratch file>` (no section). The base hash is recorded at queue time, so
   the broker escalates rather than overwrite a page changed in between.

8. **Cross-link** — the most important step. For every new or updated page
   add upstream, downstream, hub, and cross-domain links, and update
   existing pages to link back. Do not rely on the index alone: grep
   `Knowledge/` for the new page's title and key terms — pages that mention
   them are link candidates the index summary would not surface. Test
   mentally: from the index, is every piece of surrounding context reachable
   in 1-2 hops? A link back on an existing page is a queued
   `--op append --section "Related"` (one line, `- [[<Page Title>]] — <why>`),
   or a `replace-section` when the link belongs inside revised prose.
   Then drain once, for all of steps 7–8:
   ```
   python3 <gt-scripts>/gt_broker.py drain --vault "<vault>"
   ```
   and report it in one line: applied N, held N, escalated N. A held write
   waits for the next drain; an escalated one is now a `#conflict` task for
   the owner — name it, do not retry it by hand.

9. **Update navigation** — call `wiki_log.py` twice:
   ```
   python3 <plugin>/scripts/wiki_log.py <vault> log ingest "<Page Title>" \
     --line "created: <Page Title>" --line "source: <Sources/file.md>"
   python3 <plugin>/scripts/wiki_log.py <vault> index "<Page Title>" \
     "<one-line summary>" [--section "<Section Name>"]
   ```
   `<plugin>` is the `Base directory for this skill:` path two levels up
   (`../../`). If the page already exists in `index.md` the `index` command
   replaces its entry in place. In a Golden Thread vault the script writes
   neither file itself: `log` goes through gt's `gt_log.py --vault <vault> add` (log.md is
   generated) and `index` is queued and drained through the broker — it
   prints the decision (`apply`, `held`, …); relay anything but `apply`.

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
page → source chain uniform: `sources:` is required on every page. The
source file is written directly (`Sources/` is exempt from the queue); the
page itself goes through the queue like any other (steps 7–8).

## Important

- Update existing pages when possible; create new ones only for new concepts.
- The user decides what matters, not the LLM. Keep it conversational.
