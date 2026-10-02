---
name: gt-open
description: "Load a project from the vault at the start of a session. Use when the user says: open project X, load project X, work on X, continue X, start on X. Reads the core project docs in order — source, idea, research, decisions, design — indexes memory files without loading them, then summarizes the project state and asks where to pick up."
---

# Golden Thread Open

Load a project and build full context before proceeding.

**First word `handoff` or `task`?** Then this is not a project open — go to
*Opening a handoff or a task* at the end of this file and skip the steps below. Any other
argument is a project (unchanged).

## Vault location

Use `$GT_VAULT` if it is set (a session pinned to one vault, such as the demo); otherwise read `~/.claude/vault-config.json` for `vault_path`. If missing → tell the user to run `/gt:gt-init` first.

## Steps

**Step 1 — Find the project**

List folders in `<vault>/Projects/`. Skip `CONVENTIONS.md`, `PROTOCOL.md`, `README.md`, and any non-directory entries.

- **If the user provided no project name** (bare `/gt:gt-open` with no argument): read `<vault>/Projects/README.md` to get the project list with their current stage, then present a numbered menu:
  ```
  Projects:
  1. shel — implementing
  2. golden-thread — designing
  3. ...
  Pick a number (or type a name):
  ```
  Wait for the user to choose before continuing.

- **If the user provided a name**: fuzzy-match it against folder names. If multiple match, show the same numbered menu filtered to matches and ask. If none match, say so and offer to create it with `/gt:gt-create`.

**Step 2 — Read CONVENTIONS.md and PROTOCOL.md (once per session)**

If not already in context this session, read:
- `<vault>/Projects/CONVENTIONS.md` — tag taxonomy, lifecycle phases, file roles
- `<vault>/Projects/PROTOCOL.md` — execution protocol and cross-project process rules

**Step 3 — Read the project README**

Read `<vault>/Projects/<slug>/README.md`. Its YAML frontmatter (`domain`,
`stage`, `topology`, `tags`) is the fastest read of where this project stands —
take it before the prose. Note:
- One-line vision
- Status board (sub-projects, phases, next actions)
- Stage (idea / researching / designing / implementing / done)
- Tags and related links

**Step 3b — Catch-up brief (when returning after a while)**

Before reading anything else, run:
```bash
python3 <base_dir>/../../scripts/gt_catchup.py --vault "<vault>" --project <slug> --mark
```
Add `--brief` when the user asked for one (`/gt:gt-open <slug> --brief`), `--no-brief` when they
asked for none. It prints one paragraph — **only** when the project has not been opened on this
machine for `brief_absence_days` days (default 7) or `--brief` was given, and there were commits
to the project in that window — and nothing otherwise. Show it **first, verbatim**, as the
generated summary it says it is; never restate it as established fact. It does not replace the
reading sequence below: Step 4 still follows in full. `--mark` records this open so the next
absence is measured from now.

**Step 4 — Read documents in order**

Read all existing files in this sequence (skip any that don't exist):
1. `source.md` — **read this before anything else that might lead to touching code.** It says where the code actually lives, which hosts serve which role in which environment, and which files are shared across servers. Acting without it is how you edit the wrong box or overwrite a file that exists on three machines.
2. `idea.md` — original brain dump, the "why"
3. `research.md` or `research/` folder — findings, codebase analysis, gotchas
4. `decisions.md` — ADRs, architectural choices made
5. `design.md` — current implementation approach
6. `spec.md` — implementation spec ready for handoff (if exists)
7. `runbook.md` — operational procedures (if exists)

**Then any handoff still waiting in this project** — run
`python3 ~/.claude/golden-thread/hooks/gt_surface.py handoffs --project <slug> --vault "<vault>"`.
It prints one line per handoff that has not been handled (or whose deferral has ended), and
nothing when the owner set `handoff_surface: manual`. A handoff exists because the last session
could NOT capture everything, so name each waiting one in the summary and offer
`/gt:gt-handoff-handle`. Do not read its body unless the user wants to handle it now. (Until
0.17.2 this skill never looked, so a handoff reached the next session only if someone
remembered it existed.)

If `source.md` links a fleet page (`**Fleet:** [[INFRASTRUCTURE]]`), read that page too — it holds the host table the project deliberately does not duplicate.

**On `research.md`:** it is append-only and grows without bound. If it exceeds ~200 lines, do not read it whole — read its `##` headings to learn what is covered, then read only the entries relevant to what the user is about to do, plus the most recent few.

**If `research-digest.md` exists and is current, read it instead of the headings.** Ask first:
`python3 <base_dir>/../../scripts/gt_digest.py check --vault "<vault>" --project <slug>` — exit 0
means the digest was built from `research.md` exactly as it is now (its frontmatter carries the
hash). The digest gives one line per recent section plus pinned findings; read the full entries
in `research.md` only where the work needs them. Exit 1 (missing or stale) → fall back to the
headings as above. `/gt:gt-work` regenerates the digest.

Then index the memory files — **do not read them all**:

8. `memory/MEMORY.md` — read this index only. It is one line per file (`- [Title](file.md) — description`), which is enough to know what exists and what each file covers.
9. **Do not follow the links yet.** Read an individual `memory/*.md` file only when the current task touches its subject, or the user asks for it.

This keeps session startup cheap. A project with 40 memory files costs ~45 lines to open instead of ~2,000, and the detail is still one read away the moment it is needed. Loading everything up front buys nothing and crowds out the context the actual work needs.

**Step 5 — Load sub-projects (if any)**

If the README status board references sub-projects (subfolders within the project), read their READMEs. Only go deeper into a sub-project if:
- The user named it specifically, OR
- The status board shows it as the active/next item

For sub-projects, follow the same read order (idea → research → decisions → design).

**Step 6 — Announce review queue**

Check `<vault>/review-queue.md`. If it has pending items, mention the count once:
> "The vault has N items waiting for review."

**Step 7 — Summarize and ask**

After loading, briefly tell the user:
- Current stage
- **Topology and targets** — the topology type and which hosts this project touches, so the user can correct a stale entry before work starts rather than after
- What the next action is (from the status board)
- Any blockers or open questions noted in the docs
- **What's available but not loaded** — how many memory files exist and roughly what they cover, so the user knows the depth is there to ask for
- **Which repo a repo-scoped command will answer for** — once per session, one line, when the working directory is the vault and the vault is a git repo: say that repo-scoped commands such as `/security-review` and `/code-review` (and any test runner or "current branch" tool) will target the **vault**, not the code under discussion, so a code review must be pointed at the code repo explicitly — or run `gt_code_review.py plan <repo>`, which takes the root as an argument. This is orientation, not a warning. `gt_doctor.py --only repo-target` shows the resolved paths.

Then ask: "Where do you want to pick up?"

## Rules

- **Never modify project files during loading** — this is read-only context gathering
- **Load lazily.** `MEMORY.md` is an index, not a manifest to expand. Read a memory file when the work needs it, not because it is listed
- **Don't re-read files already in context** from this session
- **For large projects** with many sub-projects, read only the top-level README first and ask which sub-project to dive into
- If `idea.md` is missing, the project is uninitialized — offer to run `/gt:gt-create` to scaffold it properly

## Opening a handoff or a task (0.18.0)

`gt-open` is the open verb for every artifact, not only projects (verb-first vocabulary,
0.18.0). Dispatch on the first word only; no fuzzy matching.

| invoked as | opens |
|---|---|
| `/gt:gt-open <slug>` | a project — the steps above, unchanged |
| `/gt:gt-open handoff` | menu of the waiting handoffs, then the chosen one |
| `/gt:gt-open handoff <id>` | that handoff: a path, a filename, or a unique part of one |
| `/gt:gt-open task <id>` | that task's full detail (`slug:LINE:HASH`, as `/gt:gt-list tasks` prints it) |

**A handoff.** List without reading any body:

```bash
python3 ~/.claude/golden-thread/hooks/gt_handoff_status.py list --vault "<vault>" --all
```

With no `<id>`, show the waiting ones as a numbered menu and wait for a choice. With an `<id>`,
match it against the listed paths; several matches → the same menu, filtered; none → say so.
Then read **that one** handoff in full, plus the `## Tasks` lines of its project's `README.md`
that cite its filename — nothing else (no project docs; that is `/gt:gt-open <slug>`). Summarise:
its status (and deferral date), what it says to do first, what it says must not be assumed, and
its open items. Offer `/gt:gt-handle handoff` to work through it. Read-only.

**A task.** Find it:

```bash
python3 <vault>/Projects/golden-thread/tools/gt_task.py list --vault "<vault>" <slug> --json
```

(`<slug>` is the part of the ID before the first `:`; add `deferred` and run again if it is not
there.) No row with that ID → the README changed since the ID was taken: say so and show the
project's current list. Otherwise show the full line and every field (`p`, `waiting`, `since`,
`due`, `defer`, `ref`), its age, and — only if it has a `ref::` — read that one file and say
what it holds. If the line cites a handoff, name it. Then offer the four ways on:
`/gt:gt-close task <id>`, `/gt:gt-handle task`, `/gt:gt-open <slug>` for the whole project, or
just start the work. Read-only.

## Review stamps for Knowledge pages (0.18.0)

Loading a project is read-only for **project** files. The one write it makes is outside the
project: after the Step 7 summary, if loading read any `Knowledge/` page as context (a fleet page
such as `[[INFRASTRUCTURE]]`, a page `source.md` or the README points at), stamp those pages so
the wiki lint's `review-due` check sees that they were read:
```bash
python3 <base_dir>/../../scripts/gt_review_stamp.py --vault "<vault>" "Knowledge/<Page>.md" ...
```
It writes `last_reviewed: <today>` through the write queue, skips pages already stamped today,
and does nothing when the `review_stamp` setting is `off`. Stamp nothing if no Knowledge page was
read.
