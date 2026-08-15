---
name: gt-wiki-init
description: "Set up a new LLM Wiki vault from scratch. Use when the user says: set up the wiki, initialize my vault, create a knowledge base, install the wiki structure, get me started with the gt-wiki plugin. Gathers the vault path and domain, then runs the bundled init script which generates everything deterministically."
---

# LLM Wiki — Init

The generation is done by a script, not by hand: `scripts/vault_init.py`
creates the folders, seeds `CLAUDE.md` from the template, writes stub
`index.md` / `log.md`, copies `Knowledge/_template.md`, and records the
vault path in `~/.claude/vault-config.json`. It is idempotent (existing
files are skipped and reported, never overwritten) and exits 3 on any
conflict without touching anything.

## Workflow

1. **Gather two inputs**:
   - the vault path (an Obsidian vault folder is ideal; any folder works)
   - a one-line domain description, e.g. "cloud platform engineering"

2. **Run the script**:
   `python3 <plugin>/scripts/vault_init.py wiki --vault <path> --domain "<domain>"`
   (use mode `all` instead of `wiki` if the user also wants the
   project-flow workspace and that plugin is installed).

3. **Show the report** (CREATED / SKIPPED lines) and explain anything
   skipped: those files already existed and were left alone.

4. **On exit code 3 (conflict)**: the vault config already points at a
   different vault, or a file was invalid. Show both paths from the report
   and ask the user which vault should win; rerun with `--config` or fix
   by hand per their decision. Never resolve this silently.

5. **Verify**: run `python3 <plugin>/scripts/wiki_lint.py <path>` — a fresh
   vault reports 0 findings. Show the output.

6. **Point forward**: "`/gt:gt-wiki-ingest <url or file>` adds your first
   source. A good starting batch is the handful of docs you explain to
   every new teammate."

## Rules

- All file generation goes through the script — never create the structure
  by hand, so every vault starts identical
- Never touch anything outside what the script manages
