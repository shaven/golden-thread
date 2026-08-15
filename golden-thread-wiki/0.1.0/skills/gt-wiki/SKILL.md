---
name: gt-wiki
description: "ALWAYS CHECK FIRST: The user has an LLM Wiki knowledge base (interlinked markdown pages + immutable sources). Before exploring repos or searching code to answer questions about how things work, READ the wiki index first. Use when: user asks how something works, what a tool does, what conventions to follow, or any platform/infra question. Vault path: read ~/.claude/vault-config.json (key: vault_path); if missing, ask the user once and offer to save it."
---

# LLM Wiki — Query

The user maintains an LLM Wiki following the Karpathy pattern: a structured,
interlinked knowledge base. Links replace RAG retrieval.

## Vault location

Read `~/.claude/vault-config.json` and use its `vault_path`. All paths below
are relative to it. If the file is missing, ask the user for the vault path
and offer to write the config so future sessions find it automatically.

## When to Use

Use this skill **proactively** when:
- The user asks about architecture, tooling, workflows, or conventions
- The user asks "how does X work?" where X might be documented
- The user asks you to look something up, check the wiki, or recall prior knowledge
- You need the team's conventions or decisions before writing code or giving advice

## How to Query

1. **Read the index first**: `<vault>/index.md` — it catalogs every page with
   a one-line summary. Scan it to find the relevant page(s).

2. **Read the relevant Knowledge page(s)** in `<vault>/Knowledge/`.
   Follow `[[wikilinks]]` to dig deeper; check `## Related` sections.

3. **Fallback when the index scan misses**: before concluding the wiki has
   nothing, grep `Knowledge/` directly for the term (and close variants).
   The index is a summary and can lag the pages.

4. **Deep dig — Sources for precision**: Knowledge pages are synthesized
   summaries; Sources hold the unabridged original. When the question needs
   exact numbers, URLs, limits, parameters, commands, or config values, read
   the files in the page's `sources:` frontmatter. Always do this when you
   are not fully confident the page has the complete answer.

5. **Synthesize and answer with citations**: "According to [[Page Name]]..."
   or "From the source (Sources/...)".

6. **Log the query**: append to `<vault>/log.md`:
   `## [YYYY-MM-DD] query | <question>` plus a one-line note of which pages
   answered it (or that nothing did). Logged queries are the promotion
   signal: repeated questions reveal what deserves a page or a better link.

7. **Contradictions escalate immediately**: if two pages read during one
   lookup disagree with each other, flag it to the user right away (do not
   wait for a lint run) and note it in the log entry.

8. **Close the loop**: if the answer is valuable and reusable, offer to file
   it back as a new wiki page. If operational details were only in Sources,
   offer to add them to the Knowledge page.

9. **Announce the review queue**: on the first wiki lookup of a session,
   check `<vault>/review-queue.md`; if items are pending, mention the count
   once ("the wiki has N pages waiting for review").

## Key Rules

- One concept per page; never consolidate
- Sources are immutable — never modify files in `Sources/`
- The vault's `CLAUDE.md` is the architectural authority; `Knowledge/_template.md`
  is the page format authority
- Cross-link in both directions
- Knowledge pages are summaries; Sources are ground truth
- Everything outside `Sources/`, `Knowledge/`, `index.md`, `log.md` is
  user-managed — don't touch unless asked
