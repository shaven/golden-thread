---
name: gt-create
description: "Create a project, a task or a handoff — the create verb, with the artifact as its argument. Project: scaffold the standard structure and fill the brain dump. Task: one well-formed line, like a developer's TODO, optionally tied to a vault page, wiki page or source. Handoff: the facts for the next session, labelled by source and verification state, plus the design narrative in this session's words. Use when the user says: new project, create a project, start a project called X, add project X, make a sub-project under Y, add a task, make a task for, remind me to, todo:, track this as a task, write a handoff, hand this off to the next session."
---

# Golden Thread Create

One verb for making things. The artifact is the first argument:

| invoked as | creates |
|---|---|
| `/gt:gt-create project <slug>` (or `/gt:gt-create <slug>`) | a project — section **Project** |
| `/gt:gt-create task <text>` | a task line — section **Task** |
| `/gt:gt-create handoff [<slug>]` | a handoff document — section **Handoff** |

Dispatch is keyword matching on the first word: `project`, `task`/`todo`, `handoff`. Any other
first word is a project slug (the behaviour before 0.18.0, unchanged). No argument → infer from
what the user said ("remind me to …" is a task, "hand this off" is a handoff, "new project" is a
project); if it is still unclear, ask which.

Since 0.18.0 this replaces `gt-task` and `gt-handoff`. Those still work for one release as
deprecated aliases that follow the **Task** and **Handoff** sections here unchanged.

## Vault location

Use `$GT_VAULT` if it is set (a session pinned to one vault, such as the demo); otherwise read `~/.claude/vault-config.json` for `vault_path`. If missing → tell the user to run `/gt:gt-init` first. Pass it explicitly on every command (Core rule 2).

## Project

Scaffold a new project. The structure is created by script so every project starts identical; capturing the idea is the conversation's job.


### Project steps

**Step 1 — Gather inputs**

Don't over-ask — infer what you can from context. Collect:

1. **Slug** — lowercase-hyphenated folder name (e.g. `my-project`, `auth-rewrite`)
2. **Title** — human-readable name (e.g. "My Project", "Auth Rewrite"). If not given, derive from slug.
3. **Tags** — look at `<vault>/Projects/CONVENTIONS.md` for the tag taxonomy. Comma-separated.
4. **Domain** — the coarse grouping (`--domain`). Read the taxonomy in `CONVENTIONS.md` and pick an existing one; propose a new one only if nothing fits, and say so. This drives the Dataview views in `Projects/README.md`, so a project without it is invisible there.
5. **Sub-project?** — If the user says it belongs under an existing project, ask for the parent slug (`--parent`).
6. **Topology** — where the code lives. Ask "Where does this project's code live?" and map the answer:

   | Answer | `--topology` |
   |---|---|
   | Just a folder on this machine | `local` |
   | One repo deployed to one server | `remote` |
   | Several servers, reached through a gateway box | `bastion-jump` |
   | Several servers, each reachable directly | `bastion-direct` |

   Also collect `--repo-url` if there is a git remote. For bastion topologies,
   `--fleet` defaults to `[[INFRASTRUCTURE]]`; pass it explicitly only if the
   fleet is defined on a differently-named page.

   If the user doesn't know yet, omit `--topology` — it lands as `TODO` in
   `source.md` and `/gt:gt-lint` will surface it later.

**Step 2 — Check for conflicts**

Check whether `<vault>/Projects/<slug>/` (or `<vault>/Projects/<parent>/<slug>/`) already exists. If it does, tell the user and ask: "Work in the existing folder, or choose a different name?"

**Step 3 — Run the script**

The script is at `<base_dir>/../../scripts/vault_init.py`, where `<base_dir>` is the path shown in the `Base directory for this skill:` header. Run:

```bash
python3 <base_dir>/../../scripts/vault_init.py create-project \
  --vault "<vault>" \
  --name "<slug>" \
  --title "<title>" \
  --tags "<tag1>,<tag2>" \
  --domain "<grouping>" \
  [--parent "<parent-slug>"] \
  [--topology local|remote|bastion-jump|bastion-direct] \
  [--repo-url "<git-remote-url>"] \
  [--fleet "<fleet-page-name>"]
```

Parse the JSON output. Show CREATED / SKIPPED / UPDATED lines.

The script emits the `create` event (`gt_events.py`) itself when it creates the
project's README — and never under `--dry-run` or for a folder that already existed.
Do not emit a second one. A line on stderr beginning `vault_init: create event NOT
recorded` means the project was created but the event was not; report it.

**Step 4 — Fill idea.md**

`idea.md` is the project's origin story — the original brain dump. Fill it with everything the user said about this project in the conversation. Do not summarize or clean it up; capture it verbatim-ish. This file is immutable after creation.

Vault content is written only through the write queue (Core rule 1) — never with Write, Edit or a shell redirect. The script has already created `idea.md` with its `# <Title>` heading, so write the rest to a scratch file outside the vault and queue it as an append to `Projects/<slug>/idea.md` (or `Projects/<parent>/<slug>/idea.md`):

```bash
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
  --path "Projects/<slug>/idea.md" --op append --content-file <scratch>/idea.md \
  --hint "gt-create: brain dump"
```

The scratch file holds:

```markdown
Created: <today>


## The Idea

<everything the user said, as completely as possible>


## Open Questions

<any unknowns or next-steps mentioned>


## Related

<links to related projects or Knowledge pages, if any>
```

**Step 5 — Cross-link**

If the project relates to existing projects or Knowledge pages, add `[[wikilinks]]` in both directions:
- Add to this project's `idea.md` under `## Related` (part of the Step 4 content)
- Add a link back from the related project's README or research — queue it with `--op append` (`--section "<heading>"` to place it under a section)

Then apply the queue once:

```bash
python3 <base_dir>/../../scripts/gt_broker.py drain --vault "<vault>"
```

Report what the broker applied, held or escalated in one line. A held write waits for the session holding the file; the next drain applies it.

**Step 6 — Summary**

```
Created project: <title>

  <vault>/Projects/<slug>/
    README.md       ← status board
    idea.md         ← brain dump (filled)
    source.md       ← where the code lives + deploy plan
    research.md     ← append-only findings
    decisions.md    ← append-only ADRs
    design.md       ← iterative architecture
    CLAUDE.md       ← written for the project's own repo root, not for the vault
    runbook.md      ← operational procedures
    memory/         ← session memory files

  Registered in: Projects/README.md

Next: Run /gt:gt-open <slug> at the start of future sessions.
If you have existing notes to import, run /gt:gt-ingest.
```


### Project rules

- Scaffolding always goes through the script — never create the structure by hand
- If the script exits with a conflict (folder exists), stop and ask the user — never overwrite
- **Never ask whether a runbook is wanted.** `runbook.md` is always created. It was opt-in until 0.6.1 and produced zero instances across eleven projects, so the flag became a no-op — `--runbook` still parses for call compatibility and changes nothing. A question whose answer changes nothing spends the user's attention and teaches them the skill is not reading the tool
- `idea.md` and the cross-links are written through the queue (`gt_write_queue.py`, then `gt_broker.py drain --vault "<vault>"`), never directly
- `idea.md` content comes from the conversation; don't generate a generic template, capture what was actually said
- Tags must come from the vault's CONVENTIONS.md taxonomy — don't invent new ones without asking
- Never copy the fleet's host table into a project's `source.md` — link it. Copies drift, which is the whole reason the fleet is defined once
- **Never create a category folder under `Projects/`.** Grouping is expressed by the `domain` property, not by nesting — `gt-lint`'s `memory-unlisted` check would silently stop reporting. The only valid second level is a real sub-project via `--parent`
- Record credential *locations* in `source.md`, never credential values

## Release pipeline (project path, after Step 3) — 0.18.0

Every project carries `release_pipeline: yes | no | planned` in its README frontmatter, a property
like `stage` and `topology`. Ask once, while gathering inputs: **"Does this project ship code —
something that gets tested, installed, pushed or deployed?"**

| Answer | Pass |
|---|---|
| Yes, and the code lives at `<path>` | `--release-pipeline yes --project-dir "<path>"` |
| Yes, but no code folder yet | `--release-pipeline planned` |
| No — notes, research, a plan | `--release-pipeline no` |

Without the flag the script records `planned` for a project with a topology and `no` otherwise.

`yes` with `--project-dir` scaffolds the project's **release pipeline** in that code root — the
steps as data in `release-pipeline.tsv` and a generated `release.sh` — through `gt_pipeline.py
init`, which never overwrites anything (a `release.sh` gt did not write is left alone and the
pipeline's script is generated under another name, which `gt_pipeline.py list` shows). `source.md` records where it lives
(`**Release pipeline:**`). The default steps are gt's own release gates: tests with a receipt,
`/gt:gt-allin`, a branch (never the default branch), install + post-install validation, an
**owner gate**, push, downstream sync.

Tell the user, in the summary, how to extend it as the project proceeds:

```bash
python3 <base_dir>/../../scripts/gt_pipeline.py list  --repo "<code root>"
python3 <base_dir>/../../scripts/gt_pipeline.py add copy --repo "<code root>" --kind gate \
  --cmd "<your copy command> --dest <dir>" --after sync          # a user gate, at a stated position
python3 <base_dir>/../../scripts/gt_pipeline.py check --repo "<code root>"
```

Removing a default gate needs `--reason`, which is recorded in the steps file. `/gt:gt-lint`
flags a project with code marked `release_pipeline: no`, and a `yes` whose `release.sh` is
missing. An existing repo adopts the pipeline the same way: `gt_pipeline.py init --repo <path>`.

## Task

The user gives the task in their own words; this writes it through a tool so the line is always
well-formed (`[p::] [waiting::] [since::]`) and ranks in `TASKS.md` exactly like a hand-written
one. Tasks live in the project README's `## Tasks` — one store, nothing new.

Vault: `$GT_VAULT` if set, else `vault_path` from `~/.claude/vault-config.json`.
Tool: `<vault>/Projects/golden-thread/tools/gt_task.py`.

### Decide, don't ask, where you can

- **Project** — the one this session is working in. None open, or the user names none → ask once,
  offering `--inbox` (unfiled; `/gt:gt-review` routes it later).
- **Priority** — `--p 2` unless the user says urgent (`1`) or someday (`3`). Never `p:: 0`: it
  does not exist for tasks.
- **Waiting** — `user` unless the task is plainly work for the assistant (`agent`) or someone else
  (`external`).
- **Ref** — when the user ties it to something: `--ref "[[Wiki Page]]"`, or a vault path
  (`Sources/2026-09-28 …md`, `Projects/x/design.md`). The tool refuses a ref that resolves to
  nothing; if it does, tell the user what it looked for.
- **Due** — only if the user gave a date.

### Run it (task)

```bash
python3 <vault>/Projects/golden-thread/tools/gt_task.py add "<the task, one line>" \
  --vault "<vault>" --project <slug> [--ref "<ref>"] [--p N] [--waiting W] [--due YYYY-MM-DD]
```

Register the session and claim the README first (Core rule 1); the tool also refuses if another
live session holds it. Echo the line it wrote and its ID. Nothing else — do not start the task.

## Handoff

The session that *designs* something is rarely the session that *builds* it. This writes down
what the next one needs, and marks what it must not assume.

### Why the script only does half

`gt_handoff.py` gathers **facts**: the project's stated goal, open tasks, recent decisions, the
observed state of the code repository. Each one carries where it came from and a verification
label, per Core rule 10.

It deliberately does **not** write the design narrative. A script that invents "what we decided
and why" produces a document that *reads* finished and is not — and the next session inherits
false confidence rather than no confidence. That section is yours.

### Handoff steps

**Step 1 — Locate the vault**

`$GT_VAULT`, else `~/.claude/vault-config.json`. Named explicitly on the command line, never
inferred (Core rule 2).

**Step 2 — Gather the facts**

```bash
python3 <base_dir>/../../scripts/gt_handoff.py \
    --vault "<vault>" --project <slug> [--repo <code-path>]
```

Writes `Projects/<slug>/handoff/<date>-handoff.md`. Use `--json` to read the facts without
writing a file.

**What changed this session (0.18.0).** When this session registered with
`gt_session.py register`, the vault's HEAD was recorded as `start_commit`. The script finds it
from the session id (`--session <id>`, else `$CLAUDE_CODE_SESSION_ID`) and adds a
`## What Changed This Session` section: new memory files with their descriptions, research
headings appended, new ADRs, design files touched, Knowledge pages created or updated —
derived from `git diff <start>..HEAD`, labelled `self-verified`. Pass `--since-commit <sha>` to
diff from somewhere else. Only committed vault changes count; if the section is missing, no
start commit was found, and nothing was guessed. `--dry-run` prints the handoff without writing.

**Step 3 — Write the design section yourself**

Replace the placeholder under `## The design, in the author's words` with what only this
session knows:

- **What was decided**, and what was rejected — rejected options are the ones the next session
  will otherwise re-propose and re-discover the hard way.
- **What is built vs. designed.** Be blunt. "Designed, not written" and "written, not tested"
  are different states and the next session cannot tell them apart from a file listing.
- **What to do first**, and what would make it wrong.
- **What is uncertain.** An open question written down is worth more than a confident guess.

**Step 4 — Answer the checklist at the top**

The script writes questions it cannot answer: whether the tests pass, whether the decisions were
overtaken later in the session, whether anything agreed verbally never reached a file. Answer
them in the document (under `## What the next session must not assume`), or say plainly that they
are unanswered.

**How Steps 3 and 4 reach the file (Core rule 1).** The handoff is vault content, so you never
edit it directly — the PreToolUse guard denies a Write/Edit to it. The script created the file;
you change it only through the write queue. Write each new section body to a scratch file and
queue a `replace-section` for it:

```bash
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
    --path Projects/<slug>/handoff/<date>-handoff.md --op replace-section \
    --section "The design, in the author's words" --content-file <scratch file> \
    --session <session id> --hint "handoff design narrative"
```

and likewise with `--section "What the next session must not assume"` for the answered
checklist. Each body replaces the **whole** section, so it must carry everything you keep — for
the design section, that includes the `Repository:` line the script put under it. Drain once,
after Step 6's README link is queued too (below).

**Step 5 — Verification labels are not decoration**

If the handoff says the tests pass, say **who ran them, when, and which ones**. "The tests pass"
with no attribution becomes folklore the moment it is written down in something formal-looking.
Anything you did not personally observe this session is `unverified`.

**Step 6 — Record**

Log in `log.md` with `work` through its own tool —
`python3 "<vault>/Projects/golden-thread/tools/gt_log.py" --vault "<vault>" add "<date time tz> [work] <slug> — handoff written"`
— and link the handoff from the project's `README.md` so the next session finds it without being
told it exists. The link is a queued `append` (one line in a scratch file; add
`--section "<heading>"` if the README has a section that lists handoffs, otherwise it goes at the
end of the file):

```bash
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
    --path Projects/<slug>/README.md --op append --content-file <scratch file> \
    --session <session id> --hint "link the handoff"
```

Then drain once, for all of this skill's writes:

```bash
python3 <base_dir>/../../scripts/gt_broker.py drain --vault "<vault>"
```

Report it in one line: `N applied, N held, N escalated`. **Held** means another live session
claims that file — the request waits and the next drain applies it; never work around a hold.
**Escalated** means the broker could not decide (for a `replace-section`, the section changed
since you queued it) and made the owner a task pointing at the conflict file — say so. Until the
design section is applied, the handoff still reads as incomplete; say that too.

### Handoff rules

- **Never let the script's output stand alone.** A handoff with the placeholder still in it is
  incomplete, and the next session should say so rather than infer the design from a file list.
- **Never claim verification you do not have.** This document is exactly where an unverified
  claim gets promoted to settled fact.
- **Write down what was rejected.** It is the most expensive thing to rediscover.
- A handoff is not a status report. It exists to let someone else continue, not to summarise.
