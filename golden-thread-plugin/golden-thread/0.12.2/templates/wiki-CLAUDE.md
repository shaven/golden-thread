# {{DOMAIN}} Knowledge Wiki

This vault is the single source of truth for AI memory across all projects.

## First: is enforcement active?

This vault defines **Core rules** in `Projects/golden-thread/core-rules/` that are
meant to hold on every turn, in every project — see `Projects/PROTOCOL.md`. They are
enforced by hooks living **outside the vault**, at `~/.claude/golden-thread/hooks/`,
installed by the `gt` plugin.

**The rules travel with these files. The enforcement does not.** A copy of this vault
on another machine, or opened by another person, has the rules as *documents* and no
mechanism re-asserting them.

So before substantive work here, confirm the hooks are wired:

```bash
echo '{}' | ~/.claude/golden-thread/hooks/inject_core_rules.sh
```

- **Output lists the Core rules** — enforcement is active. Proceed normally.
- **Command not found, or empty output** — the plugin is not installed on this machine.
  **Say so plainly in your first reply.** Then continue: treat everything in
  `core-rules/` as advisory rather than guaranteed, and never state that a Core rule is
  enforced when it is not.

Install with `bash <plugin-repo>/install.sh`, then restart Claude Code.

This check exists because the failure is otherwise **silent**. A session without the
hooks reads exactly the same rules and simply never has them re-asserted — the precise
"in context is not applied" failure the Core tier was built to close, reappearing
whenever the vault outruns its machinery. Announcing it does not fix it; it stops the
gap being invisible.

### Say the state out loud — including when it is healthy

Since `gt` 0.9.6 the SessionStart hooks print their results to the user's terminal through
the hook `systemMessage` field (version, component drift, orphaned workers, unpushed
commits). Before that they reached only the assistant, as context, so a clean check and a
plugin that never ran looked identical from the user's side: silence.

**The assistant's announcement is the backstop, not the mechanism.** Open every session by
stating the startup check results in the first reply — one line covering plugin version,
component drift, and orphaned workers, whatever they say. If the terminal showed nothing at
login, that line is the only evidence the checks ran; if it showed the four lines, the
assistant's line confirms it read the same thing. Name the version the components were
matched against; "clean" against a stale version is the exact shape of the 2026-08-30
failure, where a component check aimed at 0.6.0 reported clean while 0.9.4 sat uninstalled
beside it.

The failure case was always meant to be announced. Announcing only the failure case is
what made silence ambiguous: the user cannot distinguish "checked, fine" from "nothing
checked" without being told which one happened.

## How to Read This

- `Knowledge/` — wiki pages. Each page is self-contained. Follow `[[wikilinks]]` for related topics.
- `Sources/` — immutable raw input (meeting notes, research dumps). Never edit these.
- `global-memory/` — facts loaded in every Claude Code session, regardless of project.
- `Projects/<slug>/` — per-project files. See `Projects/CONVENTIONS.md` for structure.
- `TASKS.md` — **generated** cross-project task rollup, ranked `PP<project>-P<task>`.
  Never edit it; tasks live under `## Tasks` in each project's `README.md`. Regenerate
  with `python3 Projects/golden-thread/tools/gt_tasks.py`. Ask "what's next?" and this
  is what answers it — but re-run it first, because project priority is computed
  against the clock, not stored. Its top two sections come from elsewhere: **Inbox**
  from `INBOX.md`, **Review** from `gt_closeout.py`.
- `INBOX.md` — the capture point. A thought that belongs to another project goes here
  as one checkbox line, from any session, with no project, priority or date. `/gt:gt-review`
  files it. Never leave an idea in the wrong project's `research.md`.
- `log.md` — audit trail of all Golden Thread operations.
- `index.md` — navigational index of all Knowledge pages.

## Knowledge Page Schema

Every page in `Knowledge/` must have this frontmatter:

```yaml
---
title: <descriptive title>
category: runbook | decision | reference | concept
tags: [comma, separated]
sources: []
created: YYYY-MM-DD
updated: YYYY-MM-DD
status: seed | growing | mature | stale
---
```

**Status vocabulary:**
- `seed` — newly created, may be incomplete
- `growing` — actively being refined, trust but verify
- `mature` — stable, well-sourced, high confidence
- `stale` — superseded or no longer accurate; do not use without verification

## Promotion Workflow

Knowledge moves up this hierarchy:

| Level | Location | Contents |
|---|---|---|
| 1 | session conversation | ephemeral — captured with `/gt-work` |
| 2 | `Projects/<slug>/memory/` | project-scoped session notes |
| 3 | `Projects/<slug>/decisions.md` | stable ADRs |
| 3 | `Projects/<slug>/research.md` | dated findings |
| 3 | `Projects/<slug>/design.md` | current architecture |
| 4 | `Knowledge/<page>.md` | cross-project platform knowledge |
| 5 | `global-memory/` | loaded in every session |

Use `/gt-promote` to graduate items between levels.
Use `/gt-lint` to check for gaps, broken links, and stale pages.

## Golden Thread Commands

| Command | Purpose |
|---|---|
| `/gt:gt-init` | Set up the vault, wire a project, write vault-config.json |
| `/gt:gt-create` | Scaffold a project and freeze its idea.md |
| `/gt:gt-open` | Load a project at the start of a session |
| `/gt:gt-work` | Write the session back to research/decisions/design; asks whether the project is finished |
| `/gt:gt-review` | File the inbox (INBOX.md, plus daily notes if you keep them) |
| `/gt:gt-query` | Look a topic up across Knowledge and project memory |
| `/gt:gt-ingest` | Import an existing project's notes (copies, never moves) |
| `/gt:gt-promote` | Graduate a fact up a level, or out to a repo CLAUDE.md |
| `/gt:gt-validate` | Re-derive a claim with a fresh-context validator |
| `/gt:gt-farm` | Hand bulk or mechanical work to an external AI as a packet |
| `/gt:gt-refresh` | Check Sources/ for upstream changes and supersede |
| `/gt:gt-lint` | Health checks, including core-unenforced |
| `/gt:gt-runbook-lint` | Find facts duplicated across runbooks |
| `/gt:gt-settings` | Inspect or switch off anything the plugin does on its own |

"What should I work on?" is answered by `python3 Projects/golden-thread/tools/gt_tasks.py`
followed by reading `TASKS.md`; regenerate first, because priority is computed against
the clock.

