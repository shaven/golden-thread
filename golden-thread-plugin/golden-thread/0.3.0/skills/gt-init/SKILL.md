---
name: gt-init
description: "Initialize the Golden Thread vault and wire it to the current project, or re-run to verify an existing setup is correct."
---

# Golden Thread Init

Set up the Golden Thread vault and connect it to a project. Idempotent — safe to re-run on an existing setup.

## Steps

**Step 1 — Check existing config**

Read `~/.claude/vault-config.json`. If it exists and the vault path is a real directory:
- Announce: "Golden Thread is already configured. Vault: `<path>`"
- Ask: "Would you like to wire this vault to a new project, or verify the existing setup?"
- If verify → skip to Step 6 (verification)
- If new project → skip to Step 5

**Step 2 — Gather vault info (fresh install only)**

Ask:
1. "Where should the vault live? (default: `~/Documents/Obsidian/GoldenThreadVault`)"
2. "What's your domain or team name? (e.g. 'Platform Engineering', 'Personal', 'Acme Engineering') — used to customize the vault schema"

**Step 3 — Gather project info**

Ask: "Do you want to wire this vault to a specific project right now?"
- Yes → ask for the project directory path (default: current working directory) and a short slug for the project name (kebab-case, e.g. `my-project`)
- No → skip project wiring steps

**Step 4 — Run vault_init.py**

The script is at `<base_dir>/../../scripts/vault_init.py`, where `<base_dir>` is the path shown in the `Base directory for this skill:` header at the top of this skill context. Resolve it to an absolute path before running.

Run:
```bash
python3 <base_dir>/../../scripts/vault_init.py fresh --vault "<vault>" --domain "<domain>"
```

Parse the JSON output. For each action:
- `created` → show as "✓ Created: <path>"
- `skipped` → show as "→ Already exists: <path>"
- `conflict` → STOP. Show: "⚠ Conflict: `~/.claude/vault-config.json` already points to a different vault. Run `/gt-init` again and choose 'connect' mode to switch."
- `error` → STOP. Show the error note.

If a project was specified, also run:
```bash
python3 <base_dir>/../../scripts/vault_init.py create-project --vault "<vault>" --name "<slug>" --project-dir "<project-dir>"
```

Show CREATED/SKIPPED/UPDATED report.

**Step 5 — Verify wiring**

Check that both of these files have a "Golden Thread" section:
- `~/.claude/CLAUDE.md`
- `<project-dir>/CLAUDE.md` (if a project was specified)

If either is missing the section, `vault_init.py create-project` will have added it. Confirm it's present.

**Step 6 — Summary**

Show a clean summary:
```
Golden Thread is ready.

  Vault:   <vault-path>
  Project: <project-slug> → <vault>/Projects/<slug>/

  Skills:
    /gt-ingest  — import existing memory into the vault
    /gt-work    — write back session findings
    /gt-promote — graduate facts up the hierarchy
    /gt-lint    — audit vault health
    /gt-query   — look things up
```

If the vault was freshly created (not pre-existing), add:
> "Tip: run `/gt-ingest` to pull in any existing `.claude/memory/` files, CLAUDE.md constraints, or project notes."
