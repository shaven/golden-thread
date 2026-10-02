---
name: gt-create
description: "Scaffold a new project in the vault with the standard structure. Use when the user says: new project, create a project, start a project called X, add project X, make a sub-project under Y. Gathers name, title, tags, and options, then runs the bundled script and fills in the brain dump."
---

# Golden Thread Create

Scaffold a new project. The structure is created by script so every project starts identical; capturing the idea is the conversation's job.

## Vault location

Use `$GT_VAULT` if it is set (a session pinned to one vault, such as the demo); otherwise read `~/.claude/vault-config.json` for `vault_path`. If missing → tell the user to run `/gt:gt-init` first.

## Steps

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

## Rules

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
