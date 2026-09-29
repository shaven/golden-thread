---
name: gt-flow
description: "Draw how knowledge moved through the vault over time: one lane per project, time left to right, an arrow each time an item climbed the ladder (session, memory, research/decisions/design, Knowledge, global-memory, Core). Renders one self-contained offline HTML file from the event stream, filterable by project and kind. Use when the user says: show how knowledge moved, visualize the vault, flow view, show the flow, knowledge timeline, how did this project's knowledge climb, draw the promotions, show me the ladder over time."
---

# Golden Thread Flow

A read-only view of `<vault>/Projects/golden-thread/events.jsonl`, the structured event
stream gt writes (schema v1). It never writes the vault; it writes one HTML file.

Script: `<base>/../../scripts/gt_flow.py` (`<base>` is this skill's base directory).
Vault: `$GT_VAULT` if set, else `vault_path` in `~/.claude/vault-config.json`. Pass the
environment through unchanged (the demo sets `GT_VAULT`).

## Render

1. Decide who will see the page.
   - **Only the user, on this machine:** real names are fine.
   - **Anyone else, or anywhere else** — an Artifact, a chat, a ticket, email, a slide,
     a screenshot, a file copied off this machine: **always add `--redact`**. No
     exception, even when the user says the names are not sensitive — render a second,
     redacted file for sharing instead. Never publish or attach an unredacted render.
2. Run:
   `python3 <script> render --vault <vault> [--redact] [--tasks] [--project <slug> ...] [--since YYYY-MM-DD] [--out <file-or-dir>]`
   - Task events (`task.open`, `task.done`) start hidden: on a real vault they are most
     of the stream and bury the knowledge movement. They are still in the page — one
     click on their Kinds checkboxes shows them. Add `--tasks` only when the user asks
     about tasks, or the render would otherwise show nothing.
   - `--project` is repeatable and includes sub-projects; `--since` takes a date or an
     ISO-8601 instant.
   - Without `--out` the file lands in the current directory, or in a temp directory
     when the current directory is inside the vault. An `--out` inside the vault is
     refused — pick a path outside it.
3. It prints `wrote <path> (N of M events...)`, and says how many task events are hidden. Open that path: `open <path>` on macOS,
   `xdg-open <path>` on Linux, `start "" <path>` on Windows. It works offline — no
   network, no CDN.
4. Tell the user what they are looking at: projects are lanes, height in a lane is the
   level (Core at the top, "no level" at the bottom), colour and shape give the kind
   family, an arrow joins the level an item left to the level it reached. Hover, click
   or Tab onto a mark for its time, kind, item, from → to, level change and note. The
   header stamps the generation time, the count shown (e.g. "211 of 1430 events shown
   (task events hidden — toggle in Kinds)"), and whether names are redacted.

Redacted pages replace every project, file path, task id, domain and session with a
short salted hash (`p-3fa2`, `f-91c0`), drop notes, and keep levels, kinds, colours
and counts. The salt is random per render and not saved, so a redacted page cannot be
mapped back, and two redacted renders do not share hashes. If the script reports that
its redaction self-check failed, nothing was written: report it, never work around it.

## When there are no events

Exit 2 means `events.jsonl` is missing or empty. Relay the script's message, then
explain: events come from gt's own tools as they run (promote, review, create, ADRs,
tasks, through `gt_events.py`), so a vault that predates them has none. History can be
recovered from git — preview first and show the user what it would add:
`python3 <vault>/Projects/golden-thread/tools/gt_events.py backfill --vault <vault> --dry-run`.
Run the real backfill only on the user's yes, under their usual vault write rules. Do
not hand-write events and never create an empty page.

## Other exits

- 1 with "unknown event schema version" — the vault's events are newer than this
  module; say so and suggest updating Golden Thread (`bash install.sh`).
- 1 with "invalid event" — relay the file and line; `gt_events.py validate` lists
  every bad line. Do not edit `events.jsonl` by hand: it is generated from the spool.
- 3 — the filters matched nothing; show the command and offer to widen them.

## Rules

- Read-only: never write `events.jsonl`, the spool, or anything else in the vault.
- `--redact` before anything leaves the user's own screen, every time.
