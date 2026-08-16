---
name: gt-promote
description: "Graduate a fact, finding, or idea up the knowledge hierarchy: project memory → decisions/research → Knowledge wiki page → global-memory."
---

# Golden Thread Promote

Move knowledge up the hierarchy so it's available in the right scope.

## The Hierarchy

```
session conversation
      ↓ graduate
project memory (memory/*.md)
      ↓ graduate
project files (decisions.md / research.md / design.md)
      ↓ graduate
Knowledge/ wiki page (applies beyond this project)
      ↓ graduate
global-memory/ (loaded in every session, all projects)
```

An idea that doesn't fit in any existing project can become a new project scaffold.

## Steps

**Step 1 — Identify what to promote**

Ask: "What would you like to promote? You can describe it, paste the content, or give me a filename."

Also accept: "I want to review promotion candidates" → scan recent entries in research.md and decisions.md for items that have `→ promote` or `candidate` notes, plus any `status: seed` Knowledge pages that might be ready to graduate to `growing`.

**Step 2 — Determine destination**

Ask (if not obvious from context):
1. "Does this apply only to `<project-slug>`, or to other projects too?"
   - Project-only → goes into decisions.md or research.md (if not already there)
   - Cross-project → goes into `Knowledge/` as a wiki page
2. "Is this something every Claude Code session should know about, regardless of project?"
   - Yes → goes into `global-memory/`

**Step 3 — Execute**

**memory/*.md → decisions.md or research.md**
- If it's a stable rule/constraint → append as an ADR to decisions.md
- If it's a finding/gotcha → append as a dated entry to research.md
- Log: `relocate`

**decisions/research → Knowledge/<page>.md**
- Write the page with proper frontmatter:
  ```yaml
  ---
  title: <descriptive title>
  category: <runbook|decision|reference|concept>
  tags: [<relevant tags>]
  sources: []
  created: <today>
  updated: <today>
  status: seed
  ---
  ```
- Add a one-line entry to `<vault>/index.md` under the appropriate category heading
- Add `[[<page title>]]` cross-links from any related Knowledge pages
- Log: `graduate`

**Knowledge/<page>.md → global-memory/**
- **Global-scope check — ask before writing:**
  > "This will be loaded in EVERY session for EVERY project. Does it contain zero project-specific facts — no project slugs, no service URLs specific to one project, no team-specific process? And has it proven useful in at least 2 unrelated projects?"
  - If no → keep in `Knowledge/` and suppress the promotion
- Keep the file under 30 lines. If more is needed, the detail belongs in a `Knowledge/` page that `global-memory/` points to.
- Write to `<vault>/global-memory/<slug>.md`
- Add entry to `<vault>/global-memory/MEMORY.md`
- Log: `graduate`

**Any level → new project scaffold**
- If the item is an idea for a separate project, run:
  ```bash
  python3 <base_dir>/../../scripts/vault_init.py create-project --vault "<vault>" --name "<new-slug>"
  ```
- Write the idea into the new project's `idea.md`
- Add to `<vault>/Projects/README.md`
- Log: `graduate`

**Retiring (removing from active use)**
- When a page is superseded or no longer accurate: update its `status:` to `stale`
- Never delete — just mark stale and note what superseded it
- Log: `retire`

## Log Entry

Always append to `<vault>/log.md` with the appropriate verb:
```
<today> [graduate] <source> → <destination>: <one-line description>
<today> [retire] Knowledge/<page>.md: superseded by <new-page>
<today> [relocate] <source> → <dest>: reclassified
```

---

## Promoting to Core (the top tier)

`Projects/golden-thread/core-rules/` is the top of the hierarchy, above
`global-memory/`. Promote here only when a rule must hold on **every turn, in every
project**.

**A rule earns Core when it has drifted or failed *despite being stored*, or the user
designates it always-on.** Importance alone is not the test — a critical trading
constraint that only matters inside a backtest is Context, not Core.

### Promotion is not a move — it is a wiring job

Moving the file is the easy part and, on its own, achieves nothing. The 2026-08-16
incident had the timestamp rule sitting in `global-memory` while silently not being
applied for dozens of turns. **A Core rule that is only stored is not Core.**

Steps, in order:

1. **Choose the enforcement**, and be honest about it:
   - `validated` — the rule is mechanically checkable (a required prefix, a forbidden
     token, a required file list). This is the only unbreakable form.
   - `reminder` — a judgement call that can only be re-asserted, not checked.
   Prefer `validated` for anything cheap to check.
2. **Set the frontmatter:**
   ```yaml
   metadata:
     type: core
     level: core
     enforcement: validated | reminder
     promoted: <today>
     supersedes: <old-filename-if-any>
   ```
3. **Move the file** to `Projects/golden-thread/core-rules/`, renamed `core_<topic>.md`.
4. **Wire or confirm the mechanism:**
   - `reminder` → add the rule's **imperative** to `hooks/inject_core_rules.sh`.
     Phrase it as a command ("Begin your reply with…"), never a description —
     descriptions drift, commands re-anchor.
   - `validated` → add a check to `hooks/validate_response.sh` that inspects the
     finished reply and blocks on violation. Keep it cheap and unambiguous.
   - Confirm both hooks are wired in `~/.claude/settings.json` (user-global = Core).
     If not: `vault_init.py install-core-rules --vault <vault>`.
5. **Update the pointer** in `global-memory/MEMORY.md` — it points at `core-rules/`,
   it does not hold Core rules inline. Leave a one-line note where the old file was.
6. **Verify** with `/gt:gt-lint` — `core-unenforced` must not fire for the new rule.
   That check exists precisely to catch step 4 being skipped.
7. **Log** in `log.md` with the `graduate` verb.

> **Writing a validator is a safety-critical act.** A `Stop` hook that blocks wrongly
> makes every session unusable. Fail open on any parse failure, honour
> `stop_hook_active` so a block can never loop, and test the allow cases before the
> block case.

### Demotion (Core → generic)

When a rule proves situational or is superseded, reverse it in this order:
**remove the enforcement first**, then move the file. Leaving a hook behind that
enforces a rule no longer in `core-rules/` is worse than either state.

1. Remove its imperative from `inject_core_rules.sh` and/or its check from
   `validate_response.sh`.
2. Set `level: generic` and drop `enforcement`.
3. Move to `global-memory/` (cross-project) or the owning project's `memory/`.
4. Re-index `MEMORY.md` and log with `retire` or `relocate`.

**Keep the Core tier small.** A bloated always-on tier dilutes attention on every rule
in it — which is the failure mode Core exists to prevent.

---

## Project lifecycle: rename, merge, archive

Projects get redefined, combined and retired. These are supported operations, not
manual sweeps — a half-finished rename leaves links pointing at nothing.

```bash
vault_init.py rename-project  --vault <v> --from <old> --to <new>
vault_init.py merge-project   --vault <v> --from <slug> --into <slug>
vault_init.py archive-project --vault <v> --slug <slug> --reason "<why>"
```

### Nothing is ever deleted

`archived` is the **stage** (`CONVENTIONS.md`: *"Retired or replaced"*); `retire` is
the **log verb** for `log.md`. Archiving keeps every note, decision and link intact —
it changes the project's status, not its contents.

A merge leaves the source as a **tombstone**: a README recording where it went. Notes
and links written before the merge still lead somewhere.

### What merge does and does not do

| Content | Handling |
|---|---|
| `memory/*.md` | Moved, filenames preserved so `[[wikilinks]]` keep resolving. A clash becomes `<name>__from_<slug>.md` and is flagged |
| `idea.md` | **Immutable — never concatenated.** Preserved verbatim as `<dst>/memory/idea_<src>.md` |
| `research.md` | Appended (dated and append-only, so interleaving is safe) |
| `decisions.md` | Appended with ADR ids **renumbered** to avoid collision; the original id is kept in the heading |
| `runbook.md`, `spec.md` | Appended |
| `design.md`, `source.md` | **Appended under a NEEDS REVIEW banner, not merged.** Two architectures or two topologies cannot be combined mechanically — a silent concatenation would describe neither system |
| frontmatter (`domain`, `tags`) | Destination's kept; parked in `review-queue.md` for you to confirm |

Everything requiring judgement lands in `review-queue.md`. Work through it before
calling the merge done, and re-run `/gt:gt-lint` — expect `memory-unlisted` until
`MEMORY.md` is tidied, and `project-missing` if anything still points at the old slug.
