---
name: gt-ingest
description: "Import an existing project's memory files, CLAUDE.md constraints, and notes into the Golden Thread vault without destructive writes. Nothing is deleted from the original locations. External sources (URLs, docs) are stored immutably in Sources/ before being synthesized into Knowledge pages."
---

# Golden Thread Ingest

Scan an existing project and migrate its knowledge into the vault. All migrations are copies — originals are never deleted. Raw sources are stored immutably in `Sources/` before being synthesized into Knowledge pages.

## Source immutability

Any external content (URL, uploaded doc, pasted text) ingested as a Knowledge page must first be stored as a raw, unedited file in `Sources/`:

```
Sources/YYYY-MM-DD <title>.md
```

Frontmatter:
```yaml
---
title: <human-readable title>
url: <original URL, if web>
local_path: <original file path, if local>
fetched: <YYYY-MM-DD>
supersedes: []
---
```

The source file is then **never modified**. The Knowledge page synthesizes from it and cites it via `sources:` frontmatter. If the upstream content changes later, `/gt:gt-refresh` creates a new Source file and supersedes the old one — the old file stays on disk unchanged.

## Steps

**Step 1 — Identify project and vault**

Read `~/.claude/vault-config.json` for vault path. If missing → tell the user to run `/gt:gt-init` first.

Ask: "Which project directory should I scan? (default: current working directory)"

Also ask: "What is this project's slug in the vault? (e.g. `shel`, `my-project`) — I'll put migrated content in `<vault>/Projects/<slug>/`"

If `<vault>/Projects/<slug>/` doesn't exist yet → run `vault_init.py create-project` first.

**Step 2 — Scan**

The script is at `<base_dir>/../../scripts/gt_ingest.py`, where `<base_dir>` is the path shown in the `Base directory for this skill:` header. Run:
```bash
python3 <base_dir>/../../scripts/gt_ingest.py "<project-dir>" --json
```

Parse the JSON array of candidates.

**Step 3 — Present findings by group**

Group candidates by `suggested_dest`. For each non-empty group, show a brief summary and ask for confirmation before doing anything:

**decisions** (constraints, patterns, and rules):
> "I found N item(s) that look like technical decisions or constraints — CLAUDE.md rules, patterns to follow or avoid. These would go into `Projects/<slug>/decisions.md`."
> Show filename + first line of content_preview for each.

**research** (findings and gotchas):
> "I found N item(s) that look like findings, gotchas, or session research. These would go into `Projects/<slug>/research.md`."

**design** (architecture and structure):
> "I found N item(s) describing the project's architecture or design. These would go into `Projects/<slug>/design.md`."

**knowledge** (platform-level facts that apply beyond this project):
> "I found N item(s) that look like platform-level knowledge — things that apply to other projects too. These would go into `<vault>/Knowledge/` as wiki pages, with the raw source stored in `Sources/`."
> For each: ask for a page title if the filename isn't descriptive.

**global_memory** (cross-project facts for every session):
> "I found N item(s) that look like cross-project facts — platform constants, tool configs, system URLs. These would go into `<vault>/global-memory/`."

**ideas** (concepts for OTHER projects):
> "I found N item(s) that look like ideas for projects other than `<slug>`. These could become a new project scaffold or go into a shared ideas list."
> For each: ask "New project scaffold, shared ideas file, or skip?"

**skip** items are silently excluded (e.g. `user.md` personal prefs — those stay in session memory).

**Step 4 — Confirm before writing**

"Ready to migrate the approved items. Shall I proceed? This will COPY files — nothing is deleted from their current location."

Wait for explicit yes.

**Step 5 — Execute migrations**

For each approved item by destination:

- **decisions** → append a dated section to `<vault>/Projects/<slug>/decisions.md`
- **research** → append a dated section to `<vault>/Projects/<slug>/research.md`
- **design** → append to `<vault>/Projects/<slug>/design.md`
- **knowledge** → two-step:
  1. Store raw content immutably in `Sources/YYYY-MM-DD <title>.md` with frontmatter
  2. Write Knowledge page at `<vault>/Knowledge/<title>.md`:
     ```yaml
     ---
     title: <title>
     category: reference
     tags: []
     sources: ["Sources/YYYY-MM-DD <title>.md"]
     created: <today>
     updated: <today>
     status: seed
     ---
     ```
  3. Add one-line entry to `<vault>/index.md`
- **global_memory** → write as `<vault>/global-memory/<filename>` and add entry to `global-memory/MEMORY.md`
- **new project ideas** → run `vault_init.py create-project` for each new project slug

After all writes, update `<vault>/Projects/<slug>/memory/MEMORY.md` with entries pointing to the migrated items.

Append a `[ingest]` entry to `<vault>/log.md`:
```
<today> [ingest] Ingested <project-dir> into Projects/<slug> — <N> items migrated
```

**Step 6 — Populate source.md**

An ingested project almost always has its topology recorded somewhere in the material you just read — deploy scripts, server names in notes, SSH aliases, README install steps. Fill in `<vault>/Projects/<slug>/source.md` from that evidence:

1. **Topology** — `local`, `remote`, `bastion-jump`, or `bastion-direct`. Infer from how many hosts appear and whether access goes through a gateway.
2. **Deployment targets** — one row per role × env. Take host names from `~/.ssh/config` where they match names in the notes; that file is authoritative for the alias → address mapping.
3. **File plan** (bastion only) — any file the notes show existing on more than one host is `static`; anything host-specific is `unique`. Notes describing a file that "drifted" or "had to be fixed separately on each box" are telling you it is `static` and currently unmanaged — record it as such.
4. **Fleet** — if the vault has `Projects/INFRASTRUCTURE.md`, link it rather than copying the host table in.

Record only what the material actually states. Mark anything you are inferring as `TODO — verify`, and list those gaps in the summary so the user can close them.

**Step 7 — Wire CLAUDE.md if not already done**

Check `<project-dir>/CLAUDE.md` and `~/.claude/CLAUDE.md` for Golden Thread sections. If missing from either, offer to add them now.

**Step 8 — Summary**

```
Ingest complete.

  Migrated to decisions.md:    N items
  Migrated to research.md:     N items
  Migrated to design.md:       N items
  Stored in Sources/:          N files  ← immutable raw originals
  Promoted to Knowledge/:      N pages
  Added to global-memory/:     N items
  New project scaffolds:       N projects
  source.md:                   <topology>, N targets, M gaps marked TODO

  Original files: unchanged at their current locations.
  Run /gt:gt-lint to verify vault health.
```

If `source.md` has any `TODO` entries, list them explicitly — these are the questions only the user can answer, and they are cheapest to close now while the material is fresh.
