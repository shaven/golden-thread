---
name: gt-wiki-lint
description: "Health-check the user's LLM Wiki. Use when the user says: lint the wiki, check the wiki, wiki health, garden the wiki, find broken links or orphans or stale pages, or on a periodic maintenance request. Runs the bundled deterministic script, then interprets the report and proposes fixes. Vault path via ~/.claude/vault-config.json."
---

# LLM Wiki — Lint

Deterministic checks run in code; judgment stays with the LLM; edits stay
with the owner. Vault path from `~/.claude/vault-config.json`.

## Workflow

1. **Run the script**:
   `python3 <plugin>/scripts/wiki_lint.py <vault_path> --queue <vault_path>/review-queue.md`
   (add `--json` for machine-readable output, `--days N` to change the
   review-due window, default 90).

2. **Interpret the report.** For each finding category, propose a concrete
   fix and group them by effort:
   - broken-links: fix the link target or create the missing page
   - orphans: add inbound links from related pages or the index
   - missing-reciprocal: add the return link
   - unsourced: locate the real source and cite it, or ingest one
   - superseded-cited: update the page to cite the superseding source
     (this is the ONLY finding that justifies proposing `status: stale`)
   - review-due: list for the owner as a review queue. Age alone is a
     review signal, NOT staleness — never propose `stale` from age
   - index-mismatch: add or remove index entries
   - status-schema: propose the correct status value
   - unlinked-mention: judge each — if genuinely related, propose the link
     in BOTH directions; if coincidental wording, note as false positive

3. **Owner approves** — apply only the fixes the user picks. Never bulk-edit
   without approval.

   **Declines are recorded, not forgotten**: when the user rejects a
   finding, append it to `<vault>/lint-declines.md` as
   `- <finding text> | <one-line rationale>`. The script suppresses ledger
   entries from every future run. To reinstate, delete its line.

4. **Log** — append a `lint` entry to `log.md` with finding counts and
   what was fixed.

## Status semantics

`stale` means "probably wrong", not "old". Set only when a page's source
has been superseded, or when the owner rules it stale during review.
Old-but-correct pages stay `mature`. This keeps the flag trustworthy.
