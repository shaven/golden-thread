---
name: gt-lint
description: "Audit the Golden Thread vault for broken links, orphaned pages, missing index entries, unlisted memory files, bloated global-memory files, project-specific facts in global scope, and Knowledge pages citing superseded sources."
---

# Golden Thread Lint

Health check for the vault. Run periodically to catch structural drift.

## Steps

**Step 1 — Locate vault**

Use `$GT_VAULT` if it is set (a session pinned to one vault, such as the demo); otherwise read `~/.claude/vault-config.json`. If neither gives a vault → tell user to run `/gt:gt-init`.

**Step 2 — Run gt_lint.py**

The script is at `<base_dir>/../../scripts/gt_lint.py`, where `<base_dir>` is the path shown in the `Base directory for this skill:` header. Run:
```bash
python3 <base_dir>/../../scripts/gt_lint.py "<vault>" --queue "<vault>/review-queue.md"
```

Parse the output.

**Step 3 — Interpret findings**

For each finding category, explain what it means and propose a fix:

**`index-gap`** — A Knowledge page exists but has no entry in `index.md`.
> "`Knowledge/<page>.md` isn't in the index. It won't be found by `/gt:gt-query`."
> Fix: show the line to add. Ask "Add it now?"

**`broken-link`** — A `[[wikilink]]` doesn't resolve to any file.
> "File X has a link to `[[Y]]` but that page doesn't exist. Either the page was renamed, or the link is wrong."
> Fix: show the link + surrounding context. Ask "Remove the link, or create the target page?"

**`orphan`** — A Knowledge page isn't reachable from index.md or any other page.
> "`Knowledge/<page>.md` isn't linked from anywhere — it's an island."
> Fix: add it to index.md or link it from a related page.

**`memory-unlisted`** — A file in `Projects/*/memory/` isn't referenced in that project's `MEMORY.md`.
> "`Projects/<project>/memory/<file>.md` isn't in the MEMORY.md index. Claude won't load it at session start."
> Fix: show the line to add. Ask "Add it now?"

**`global-gap`** — A file in `global-memory/` isn't reachable from `MEMORY.md`.
> "`global-memory/<file>.md` isn't in the global MEMORY.md index. It won't be loaded cross-project."
> Fix: show the line to add. Ask "Add it now?"

**`memory-bloat`** — A `global-memory/` file exceeds 30 lines.
> "`global-memory/<file>.md` has N lines. Global-memory files should stay under 30 lines — full reference tables belong in `Knowledge/`."
> Fix: propose moving detailed content to a new Knowledge page and replacing it with a pointer + 3-5 essential constants. Ask "Trim it now?"

**`global-scope-leak`** — A `global-memory/` file references a project slug.
> "`global-memory/<file>.md` mentions project slug `<slug>`. Global-memory should contain only cross-project facts."
> Fix: propose moving the project-specific content to `Projects/<slug>/memory/` or `Projects/<slug>/decisions.md`. Ask "Move it now?"

**`superseded-cited`** — A Knowledge page's `sources:` field cites a Source file that has been superseded.
> "`Knowledge/<page>.md` still cites `Sources/<old>.md`, but that source was superseded by `Sources/<new>.md`."
> This means the Knowledge page may contain outdated information.
> Fix: review what changed between old and new source, update the Knowledge page content if needed, then update `sources:` to point at the new file. Ask "Review and update now?"
> **This is the only finding that may justify setting `status: stale`** — if the source changed significantly and the page content can't be confidently updated this session.

**`stale`** — A Knowledge page has `status: stale`.
> "`Knowledge/<page>.md` is marked stale. Either update it or retire it via `/gt:gt-promote`."

**Step 4 — Approval loop**

For each proposed fix:
- Present the change
- Ask yes/no
- If yes → queue it immediately (below)
- If no → ask "Add to lint-declines.md to suppress this in future?" and queue the line if confirmed

Suppression format in `<vault>/lint-declines.md`:
```
suppress: Knowledge/page.md
suppress: Knowledge/page.md:[[BrokenLink]]
```

**How a fix reaches the vault (Core rule 1).** Never edit a vault page directly — the PreToolUse
guard denies a Write/Edit to vault content. Each approved fix becomes one write-queue request;
put its text in a scratch file and run
`python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" --path <vault-relative .md> --op <op> ... --session <session id> --hint "lint: <check>"`:

| Fix | Op |
|---|---|
| a line added to `index.md`, a `MEMORY.md`, or `lint-declines.md` (`index-gap`, `orphan`, `memory-unlisted`, `global-gap`, suppressions) | `append`, with `--section "<heading>"` for the index section it belongs under |
| a link removed or re-pointed inside a page (`broken-link`, `project-missing`) | `replace-section` of the `##` section holding it, with that section's **whole** new body |
| a new page (`broken-link` target, the Knowledge page a `memory-bloat` trim or `global-scope-leak` move creates, a tombstone README) | `create` |
| a top-level frontmatter field with a one-line value (`status: stale`, a flow-style `sources: [...]`) | `set-property --key <key> --value "<one-line value>"` (no content file) |
| a multi-line or nested frontmatter value (a block-list `sources:`), text above the first `##`, or edits scattered through one page | `replace-file`, with the page's **whole** new text |

Then drain once, after the last approved fix:

```bash
python3 <base_dir>/../../scripts/gt_broker.py drain --vault "<vault>"
```

Count a fix as **Fixed** only when the broker applied it. **Held** (another live session claims
the file) waits for the next drain; **escalated** becomes an owner task — and every write to
`global-memory/` is escalated by design, so `global-gap`, `memory-bloat` and `global-scope-leak`
fixes always land as owner review, never applied by this skill. Say so when proposing them.
A fix that would delete or rename a file cannot go through the queue: name it under
**Remaining** for the user instead.

**Step 5 — Summary**

```
Lint complete.

  Checked: N Knowledge pages, M memory files, P wiki links, Q sources
  Found:   X issues
  Fixed:   Y (with your approval, applied by the broker)
  Queued:  H held, E escalated to the owner
  Suppressed: Z (added to lint-declines.md)
  Remaining: W (need manual attention)
```

If the vault is clean: "✓ Vault is healthy — no issues found."

---

## Core-rule checks

These audit that Core rules are **enforced**, not merely stored — the distinction the
whole tier rests on.

| Check | Means |
|---|---|
| `core-misplaced` | A rule declares `level: core` but lives outside `core-rules/` |
| `core-no-enforcement` | `level: core` with no `enforcement` declared |
| `core-unenforced` | The declared mechanism is **not actually wired** — `validated` needs a `Stop` hook, `reminder` needs `UserPromptSubmit` |

`core-unenforced` is the important one. It is the machine-checkable form of "in
context ≠ applied": a rule can be perfectly written, correctly filed, and still never
re-asserted. Treat it as a real defect, not a style nit.

**Fix:** `vault_init.py install-core-rules --vault <vault>`, or wire the hook by hand
in `~/.claude/settings.json`. Do not suppress these — suppressing `core-unenforced`
re-creates the original bug with a paper trail saying it was fine.

## `project-missing`

A markdown link points at a `Projects/<slug>/` folder that does not exist — the usual
cause is a rename or merge that was done by hand and missed a reference. Obsidian
fails these silently.

Fix by re-pointing the link, or by leaving a tombstone README at the old slug (which
is what `merge-project` does automatically).
