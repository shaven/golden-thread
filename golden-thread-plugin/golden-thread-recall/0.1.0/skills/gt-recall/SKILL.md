---
name: gt-recall
description: "Rank the vault's pages for a question by BM25 (page name, headings and index line count above body text) and say which pages to open first. Use when the user says: find the page about X, which note covers X, search my vault for X, recall X from the vault, gt-query found nothing."
plain:
  - cmd: "/gt-recall:gt-recall"
    does: "Finds the vault pages most likely to answer a question."
    how: "Scores every page under Knowledge, Projects and global-memory on your words, with a page's name and headings counting more than its body, and lists the best first."
    when: "A question's page exists but a plain lookup did not surface it."
    next: "/gt:gt-query"
    hear: "which note covers|search my vault for|find the page about"
model_intent: fast
---

# Golden Thread Recall

Read-only. It ranks pages; it never writes the vault, builds an index, or uses the network.

Script: `<root>/scripts/gt_recall.py`, where `<root>` is the folder two levels above this skill's base directory. Write it as a plain absolute path, with no `..` and no `~`: the lockdown allow rules name that exact path.
Vault: `$GT_VAULT` if set, else `vault_path` in `~/.claude/vault-config.json`.

Under gt sandbox mode the shell cannot read the vault: use `vault_search` (the core keyword path) instead, or ask the user to run the command in a terminal.

## Use

1. Run `python3 <script> "<the user's question, in their words>" --vault <vault> [--k 10] [--json]`.
2. It prints each hit as `score  path`, best first. Open the top three with the normal read tools (and `index.md` first, as gt-query does), then answer from them and cite the page.
3. `--json` adds `hits` (path, level, score, method) and any `locked` folders. A locked folder is never read; say it was skipped.
4. No hits means no page shares a word with the question: say so, and try the user's other words before concluding the vault has nothing.

## Rules

- `method` is `bm25` today. A hit never claims a method that did not find it.
- Stage 2 (embeddings or an external search tool) is not part of this module: it is added only if the recall benchmark (`gt_bench.py recall`) shows this stage below target on the owner's real vault.
