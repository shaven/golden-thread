---
name: gt-create
description: "Scaffold a new project in the vault with the standard structure. Use when the user says: new project, create a project, start a project called X, add project X, make a sub-project under Y. Gathers name, title, tags, and options, then runs the bundled script and fills in the brain dump."
---

# Golden Thread Create

Scaffold a new project. The structure is created by script so every project starts identical; capturing the idea is the conversation's job.

## Vault location

Read `~/.claude/vault-config.json` for `vault_path`. If missing → tell the user to run `/gt:gt-init` first.

## Steps

**Step 1 — Gather inputs**

Don't over-ask — infer what you can from context. Collect:

1. **Slug** — lowercase-hyphenated folder name (e.g. `my-project`, `auth-rewrite`)
2. **Title** — human-readable name (e.g. "My Project", "Auth Rewrite"). If not given, derive from slug.
3. **Tags** — look at `<vault>/Projects/CONVENTIONS.md` for the tag taxonomy. Comma-separated.
4. **Domain** — the coarse grouping (`--domain`). Read the taxonomy in `CONVENTIONS.md` and pick an existing one; propose a new one only if nothing fits, and say so. This drives the Dataview views in `Projects/README.md`, so a project without it is invisible there.
5. **Sub-project?** — If the user says it belongs under an existing project, ask for the parent slug (`--parent`).
6. **Runbook needed?** — "Will this involve operational procedures or touching production systems?" If yes, pass `--runbook`.
7. **Topology** — where the code lives. Ask "Where does this project's code live?" and map the answer:

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
  [--runbook] \
  [--topology local|remote|bastion-jump|bastion-direct] \
  [--repo-url "<git-remote-url>"] \
  [--fleet "<fleet-page-name>"]
```

Parse the JSON output. Show CREATED / SKIPPED / UPDATED lines.

**Step 4 — Fill idea.md**

`idea.md` is the project's origin story — the original brain dump. Fill it with everything the user said about this project in the conversation. Do not summarize or clean it up; capture it verbatim-ish. This file is immutable after creation.

Write to `<vault>/Projects/<slug>/idea.md` (or `<vault>/Projects/<parent>/<slug>/idea.md`):

```markdown
# <Title>

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
- Add to this project's `idea.md` under `## Related`
- Add a link back from the related project's README or research

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
    memory/         ← session memory files
    runbook.md      ← (if requested)

  Registered in: Projects/README.md

Next: Run /gt:gt-open <slug> at the start of future sessions.
If you have existing notes to import, run /gt:gt-ingest.
```

## Rules

- Scaffolding always goes through the script — never create the structure by hand
- If the script exits with a conflict (folder exists), stop and ask the user — never overwrite
- `idea.md` content comes from the conversation; don't generate a generic template, capture what was actually said
- Tags must come from the vault's CONVENTIONS.md taxonomy — don't invent new ones without asking
- Never copy the fleet's host table into a project's `source.md` — link it. Copies drift, which is the whole reason the fleet is defined once
- **Never create a category folder under `Projects/`.** Grouping is expressed by the `domain` property, not by nesting — `gt-lint`'s `memory-unlisted` check would silently stop reporting. The only valid second level is a real sub-project via `--parent`
- Record credential *locations* in `source.md`, never credential values
