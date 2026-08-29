# Golden Thread — Getting Started

A guided walkthrough for your first session. Five steps, ~10 minutes.

**Requirements:** Python 3.8+, Claude Code installed.

---

## Step 1 — Install

Unzip the plugin and run the installer:

```bash
unzip golden-thread-plugin.zip
cd golden-thread-plugin
bash install.sh
```

Then **restart Claude Code**. The `/gt:` skills are not available until you do.

The installer sets up two plugins:
- **`gt`** — memory, projects, and session workflow
- **`gt-wiki`** — knowledge base with immutable sources

---

## Step 2 — Initialize your vault

Open any Claude Code session and run:

```
/gt:gt-init
```

Claude will ask for:
1. A vault path — an Obsidian vault folder is ideal, but any folder works
2. Your domain/team name — e.g. "Acme CloudOps" or "My Team"

The script creates the folder structure and writes `~/.claude/vault-config.json` — the pointer every other skill reads. Safe to re-run on a machine you've already set up.

---

## Step 3 — Create your first project

```
/gt:gt-create
```

Claude will ask for a project name and a quick brain dump of what it is. That brain dump becomes `idea.md` — the immutable origin story for the project.

For a project you're already working on, use:

```
/gt:gt-ingest
```

This scans your existing notes and memory files and imports them into the vault without touching the originals.

---

## Step 4 — Open a project at the start of each session

```
/gt:gt-open <project-name>
```

This reads your project docs in order — idea → research → decisions → design → spec → runbook — then reads the session memory index and stops. It summarizes where things stand and asks where to pick up.

**Why it stops at the index:** a project with 70 notes would otherwise dump 2,000 lines into context. `gt-open` reads the one-line index and loads individual files only when you need them.

---

## Step 5 — Write back at the end of each session

```
/gt:gt-work
```

This is the step that keeps the vault alive. Claude will write back what was learned:

| Destination | What goes there |
|---|---|
| `research.md` | New findings and gotchas, dated |
| `decisions.md` | Architectural choices (ADRs) |
| `design.md` | Updated architecture if it changed |
| `runbook.md` | Operational commands and landmines |
| `memory/*.md` | Session state |

**Skipping `gt-work` is how the vault decays.** Facts that aren't written down are forgotten by the next session.

---

## Typical session pattern

```
/gt:gt-open my-project     ← load context, see where you left off

# ... work happens ...

/gt:gt-work                ← write back findings before closing
```

---

## Periodic maintenance

```
/gt:gt-lint                ← find broken links, orphaned pages, unlisted memory files
/gt:gt-promote             ← graduate a finding that proved true across projects
/gt:gt-refresh             ← check if any source documents changed upstream
/gt:gt-review              ← pull uncaptured items from Obsidian daily notes
```

---

## Adding a knowledge base (gt-wiki)

If you want a structured wiki alongside your project memory:

```
/gt:gt-wiki-init
```

Then add sources:

```
/gt:gt-wiki-ingest <url or file path>
```

Query it:

```
/gt:gt-wiki
```

The wiki uses immutable sources — every ingested document is stored verbatim and never modified. Knowledge pages are synthesized summaries that link to the originals.

---

## Key concepts in one paragraph

The vault is organized by **how widely a fact applies**. Session findings go in `memory/`. Settled discoveries go in `research.md`. Choices that shaped the architecture go in `decisions.md`. Facts true across multiple projects go in `Knowledge/`. Facts needed in every session everywhere go in `global-memory/`. Promote a fact when a second project proves it general — not before. When unsure, file it narrow; promotion is cheap, demotion isn't.

---

## Troubleshooting

**Skills not showing up** — restart Claude Code after install.

**"vault-config.json not found"** — run `/gt:gt-init`.

**A rule seems ignored** — `CLAUDE.md` is read at session start; rules added mid-session apply from the next session.

**Memory files aren't loading** — that's by design. Ask for a file by name, or run `/gt:gt-lint` to check if it's unlisted.

**Timestamp missing from responses** — the enforcement hook is unwired. Run `/hooks` in Claude Code or restart. If it persists, re-run `install.sh`.

---

For the complete skill reference, see [MANUAL.md](MANUAL.md).
