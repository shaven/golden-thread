---
name: gt-wiki-lint
description: "Health-check the user's LLM Wiki. Use when the user says: lint the wiki, check the wiki, wiki health, garden the wiki, find broken links or orphans or stale pages, or on a periodic maintenance request. Runs the bundled deterministic script, then interprets the report and proposes fixes. Vault path via ~/.claude/vault-config.json."
---

# LLM Wiki — Lint

Deterministic checks run in code; judgment stays with the LLM; edits stay
with the owner. Vault path: `$GT_VAULT` if set, else `vault_path` in `~/.claude/vault-config.json`.

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

   **Fixes go through the write queue**, never a direct Write/Edit (Core
   rule 1; the gt guard denies it). Put each revised section body in a
   scratch file outside the vault and queue it with gt's scripts, at
   `~/.claude/plugins/cache/golden-thread-plugin/gt/<newest>/scripts/`:
   ```
   python3 <gt-scripts>/gt_write_queue.py --vault "<vault>" \
     --path "Knowledge/<Page Title>.md" --op replace-section --section "<heading>" \
     --content-file <scratch file> --session <session id> --hint "lint fix: <finding>"
   ```
   `replace-section` replaces the whole body of `## <heading>`, so the file
   carries everything you keep. A missing return link or an inbound link for
   an orphan is `--op append --section "Related"` (one line); a missing page
   is `--op create`; a wrong `status:` is
   `--op set-property --key status --value <value>` (no content file).
   A citation fix (unsourced, superseded-cited) on a block-list `sources:`
   (one `- ` item per line), or any frontmatter change set-property cannot
   express, is a whole-file write: read the page, rebuild its full text with
   the corrected list, and queue `--op replace-file --content-file <scratch
   file>` (no section; the broker escalates if the page changed since).
   Index entries go through `wiki_log.py <vault> index`, as in ingest.

   **Declines are recorded, not forgotten**: when the user rejects a
   finding, queue it onto `lint-declines.md` as
   `- <finding text> | <one-line rationale>` (`--path lint-declines.md
   --op append`, one line per decline in the scratch file). The script
   suppresses ledger entries from every future run. To reinstate, delete its
   line (ask the owner, or queue a `replace-section` of the ledger section).

   Then drain once, for all fixes and declines:
   ```
   python3 <gt-scripts>/gt_broker.py drain --vault "<vault>"
   ```
   and report it in one line: applied N, held N, escalated N.

4. **Log** — never append to `log.md` by hand (in a Golden Thread vault it is
   generated). Record a `lint` entry with finding counts and what was fixed:
   ```
   python3 <plugin>/scripts/wiki_log.py <vault> log lint "wiki" \
     --line "<N> findings, <M> fixed, <D> declined"
   ```
   In a gt vault the script hands the line to gt's
   `Projects/golden-thread/tools/gt_log.py --vault "<vault>" add`.

## Status semantics

`stale` means "probably wrong", not "old". Set only when a page's source
has been superseded, or when the owner rules it stale during review.
Old-but-correct pages stay `mature`. This keeps the flag trustworthy.
