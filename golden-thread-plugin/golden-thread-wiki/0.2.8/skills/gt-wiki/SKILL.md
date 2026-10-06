---
name: gt-wiki
description: "ALWAYS CHECK FIRST: The user has an LLM Wiki knowledge base (interlinked markdown pages + immutable sources). Before exploring repos or searching code to answer questions about how things work, READ the wiki index first. Use when: user asks how something works, what a tool does, what conventions to follow, or any platform/infra question. Vault path: read ~/.claude/vault-config.json (key: vault_path); if missing, ask the user once and offer to save it."
model_intent: balanced
---

# LLM Wiki — Query

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| reading `index.md`, `Knowledge/`, `Sources/`, `review-queue.md` | `vault_search`, `vault_read`, `vault_list` |
| `wiki_log.py <vault> log …` (it runs the vault's `gt_log.py`) | `vault_log_add` `{line}`: the line it would write, `YYYY-MM-DD HH:MM TZ [<kind>] <subject> — <detail>` |
| `<gt-scripts>/gt_write_queue.py …` | `vault_queue_write` — the same `path`, `op`, `section`, `key` and `hint`, with the text itself as `content` (no `--session`: the server stamps its own). The broker decides at once and the result says apply, held or escalate. `design.md` and `global-memory/` are refused (owner only) |
| `<gt-scripts>/gt_broker.py drain` | nothing to run after `vault_queue_write`; `vault_queue_drain` if anything was queued from the shell |
| a new `Sources/YYYY-MM-DD <title>.md` | `vault_source_store` `{name, content}` — write-once |
| writing `~/.claude/vault-config.json` | **terminal** |

A **terminal** row is a step sandbox mode refuses on purpose — it changes settings or install state, or rewrites the vault wholesale. Give the user the exact command to run from a terminal, and carry on with the rest.

The user maintains an LLM Wiki following the Karpathy pattern: a structured,
interlinked knowledge base. Links replace RAG retrieval.

## Vault location

Use `$GT_VAULT` if it is set; otherwise read `~/.claude/vault-config.json` and use its `vault_path`. All paths below
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

6. **Log the query**: never append to `log.md` by hand — in a Golden Thread
   vault it is a generated file. Record it with the bundled script, naming
   which pages answered it (or that nothing did):
   ```
   python3 <plugin>/scripts/wiki_log.py <vault> log query "<question>" \
     --line "answered by [[Page]], [[Page]] (or: nothing)"
   ```
   `<plugin>` is the `Base directory for this skill:` path two levels up
   (`../../`). In a gt vault the script hands the line to gt's
   `Projects/golden-thread/tools/gt_log.py --vault "<vault>" add`; in a
   standalone wiki it appends to `log.md` itself.
   Logged queries are the promotion signal: repeated questions reveal what
   deserves a page or a better link.

7. **Contradictions escalate immediately**: if two pages read during one
   lookup disagree with each other, flag it to the user right away (do not
   wait for a lint run) and note it in the log entry.

8. **Close the loop**: if the answer is valuable and reusable, offer to file
   it back as a new wiki page. If operational details were only in Sources,
   offer to add them to the Knowledge page. On the user's yes, write through
   the queue, never directly (Core rule 1; the gt guard denies a direct
   Write/Edit): the page or section body goes in a scratch file outside the
   vault, then, with gt's scripts at
   `~/.claude/plugins/cache/golden-thread-plugin/gt/<newest>/scripts/`:
   ```
   python3 <gt-scripts>/gt_write_queue.py --vault "<vault>" \
     --path "Knowledge/<Page Title>.md" --op create --content-file <scratch file> \
     --session <session id> --hint "file back a wiki answer"
   python3 <gt-scripts>/gt_broker.py drain --vault "<vault>"
   ```
   (`--op replace-section --section "<heading>"` to add to an existing page;
   if its `sources:` list must change too, rebuild the whole page and queue
   `--op replace-file` instead.)
   Report the drain in one line: applied N, held N, escalated N. A new page
   still needs its `Sources/` conversation file and index entry — follow
   gt-wiki-ingest's "Conversation-derived pages".

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
