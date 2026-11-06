---
name: gt-minimize
description: "Prune a heavy session before cutting it: measure it and its prompt cache, keep what is worth keeping, then /compact or /clear while cheap; `compact` does the whole cut (capture, handoff, then a compact or a clear, per cut_kind and cut_handoff); `clear` forces a clear. Use when the user says: minimize this session, prune this session, trim the context before I step away, compact this session, compact with a handoff, hand off and compact, hand off and clear."
plain:
  - cmd: "/gt:gt-minimize"
    does: "Trims a heavy session before you cut it, keeping only what is worth keeping."
    how: "Measures what the session carries and whether its prompt cache is still warm, saves the few things worth saving to the vault or inbox, and tells you to run /compact or /clear while that is cheap."
    when: "A long session is about to go cold."
    next: "/compact"
    hear: "minimize this session|prune this session|trim the context"
  - cmd: "/compact"
    does: "Shrinks this conversation into a summary so it costs less to continue."
    how: "Claude Code's own command. It replaces the long history with a summary; gt saves its state first so nothing it needs is lost."
    when: "The session is large and you will carry on with it."
    next: "/gt:gt-minimize"
model_intent: balanced
---

# Golden Thread Minimize

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_minimize.py …` | unchanged: it reads the session transcript, not the vault |
| `gt_write_queue.py …` | `vault_queue_write` — the same `path`, `op`, `section`, `key` and `hint`, with the text itself as `content` (no `--session`: the server stamps its own). The broker decides at once and the result says apply, held or escalate. `design.md` and `global-memory/` are refused (owner only) |
| `gt_broker.py drain` | nothing to run after `vault_queue_write`; `vault_queue_drain` if anything was queued from the shell |

A session left idle past its cache lifetime rebuilds its whole prefix on the next turn, and the
price is set by **how large the conversation was**, not how long the break was. Compacting while
the cache is still warm reads the prefix from cache and costs a fraction of the context size;
compacting after it expired pays the full rebuild first. And `/compact` is lossy in ways nobody
chooses: a fact found mid-session that belongs in the wiki is summarised away.

This skill makes the cut safe enough to actually do: **measure, triage, prune, cut** — in that
order, quickly, because the cache is a clock.

## What it is not

- **Not a handoff.** It writes no in-flight note. If something must carry forward to the next
  session — a half-done change, an open decision, the next command — that is
  `/gt:gt-create handoff`'s job, and this skill sends you there (owner decision, 2026-10-01).
- **Not a report on the past.** `/gt:gt-optimize --only session` measures cache waste across
  sessions over weeks. This is about the one session you are in, now.
- **Not a decision-maker.** A script cannot tell a load-bearing fact from a tool result that
  happened to be long. The script measures; you and the user decide.

## Steps

**Step 1 — Measure (read-only)**

```bash
python3 <base_dir>/../../scripts/gt_minimize.py
```

It finds this session's transcript from `$CLAUDE_CODE_SESSION_ID`; if that is not set, pass
`--session <id>` or `--transcript <file>` (or `--latest`, which says it guessed). It prints:

- the **billed context** of the last turn (a measurement from the usage row) and the peak since
  the last compaction;
- a **breakdown labelled as an estimate** — conversation, tool output, tool calls, injected
  context (hook output and skill bodies) — and whatever the transcript does not account for
  (system prompt, tool definitions) as **unattributed**, never assigned to a guess;
- the **cache state**: warm with N minutes left, or expired. The TTL is the one the last turn
  wrote at, not a constant.

Tell the user the size and the cache state in one line. If the cache has expired, say so plainly:
the next turn rebuilds the prefix whatever happens, so there is no rush — but the habit to build
is cutting *before* stepping away.

**Step 2 — Triage: what is worth keeping**

Go through the session and list, briefly, the things that would be lost by a cut and are worth
more than the session itself. Be ruthless: most of a long session is tool output that has done
its job.

| it is… | where it goes |
|---|---|
| a platform / tool / infrastructure fact that would help outside this project | `Knowledge/` — through `/gt:gt-promote` (or the wiki-ingest path for something new) |
| a project finding or decision | the project's `research.md` / ADR — through `/gt:gt-work` |
| a thought that belongs to another project, not yet worked | one checkbox line in `INBOX.md` (below) |
| work in flight, an open question, the next command | **`/gt:gt-create handoff`** — not here |
| everything else | dropped — say so, so the user can object |

Present the list as **keep → destination** and **drop**, and ask the user to confirm or move
items between them. Nothing is written until they answer.

**Step 3 — Write the keepers through the queue (Core rule 1)**

Every vault write goes through the write queue, never Write, Edit or a shell redirect. An INBOX
line, for example — write it to a scratch file outside the vault first:

```bash
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
    --path "INBOX.md" --op append --content-file <scratch>/inbox-line.md \
    --hint "gt-minimize: keeper from session triage"
python3 <base_dir>/../../scripts/gt_broker.py drain --vault "<vault>"
```

Knowledge pages and project findings go through `/gt:gt-promote` and `/gt:gt-work`, which already
route their writes through the queue. Tell the user in one line what the broker applied, held or
escalated.

**Step 4 — Cut, now**

Tell the user, in this order:

1. what was kept and where it went, and what was dropped;
2. if anything is in flight: "run `/gt:gt-create handoff` first" — and stop until they have;
3. **"Run `/compact` (keeps a summary) or `/clear` (starts clean) now — the cache is warm for
   about N more minutes, so cutting now is cheap."** If it had already expired, say that the
   saving this time is the next session's size, not the rebuild.

You cannot run `/compact` or `/clear` yourself; they are the user's commands.

## Verbs: `compact` (one step, 0.21.4) and `clear` (0.22.0)

`/gt:gt-minimize compact` skips the triage conversation and does the cut for the user. It is the
same path the `auto_compact` setting runs by itself once you opt in (`/gt:gt-settings auto_compact auto`,
or `50`), on request. Its default, `warn`, only says once that the session is large and names this verb.
`/gt:gt-minimize clear` is the same with the kind forced to clear (`--kind clear` below).

Two settings shape every cut (0.22.0); read them first with
`python3 <base_dir>/../../scripts/gt_compact.py status --json` (`cut_handoff`, `cut_kind`):

| `cut_handoff` | step 1 (capture) | step 2 (handoff) |
|---|---|---|
| `facts` (default) | yes | yes, with its key facts (figures, names, settings, findings) |
| `handoff` | **skip**: nothing is saved to the vault | yes: what the session was doing, next steps, file paths |
| `none` | **skip** | **skip**: a plain cut, no `--handoff` |

`cut_kind` is `compact` (default: compacted in place) or `clear` (the session is cleared and the fresh
one starts from the handoff). A clear with `cut_handoff none` is refused: say so, change nothing, and
suggest `cut_handoff handoff` or a compact.

1. **Capture** (`facts` only). Run `/gt:gt-work` for this session's findings (queue, then `gt_broker.py drain`,
   as the core rules say; skip it if nothing is new). Say in one line what was saved.
2. **Handoff** (`facts` and `handoff`). Write the handoff with `/gt:gt-create handoff` and note its path.
   For `facts`, include the key facts; for `handoff`, keep it to the task, next steps and file paths.
3. **Request the cut.** `python3 <base_dir>/../../scripts/gt_compact.py request --handoff <that path>`
   (add `--kind clear` for the `clear` verb; with `cut_handoff none`, no `--handoff`).
   It refuses unless the handoff exists (except under `none`), so step 2 cannot be skipped. It prints one line. The
   request is for THIS session only (it reads `$CLAUDE_CODE_SESSION_ID`; several sessions may share
   the folder and none of the others will take it). Under gt sandbox mode, pass the vault path
   `vault_handoff` gave you: a handoff still waiting in the queue inbox (`~/.gt-inbox/queue/`) is
   accepted, and the request itself goes to `~/.gt-scratch/compact/` when the sandbox keeps it out
   of `~/.claude/golden-thread`.
4. **Say one thing and end the reply:** "Compacting when this reply ends; press Enter to load the
   handoff." (a clear: "Clearing when this reply ends; press Enter in the fresh session to load the
   handoff."; `none`: "Compacting when this reply ends.") Do not start other work after the request: the
   in-session mod cuts at the end of this turn and fills the prompt box with "Read the handoff at <path>
   and pick up where we left off." above anything already typed there. For a clear it runs `/clear`
   through Claude Code's command API; if that is not available it tells the user to type `/clear` and
   puts the line in the box after it (not yet seen in a live Claude Code window).
   This needs Claude Code 2.1.296 or newer: an older one does not tell the mod its session, so it
   takes no request and nothing cuts. If nothing happens (that, or the request was refused), say so
   and tell the user to type `/compact` (or `/clear`); the handoff is already saved.
5. **The pickup, after the cut.** When the user presses Enter on that line, read the handoff it
   names. Under gt sandbox mode the shell and Read cannot reach the vault: read it with
   `vault_read` (the vault-relative path), never with Read or `cat`.
   Then run `python3 <base_dir>/../../scripts/gt_minimize.py --record`: it adds one line to the savings
   ledger for the cut that just finished, and says in one line if it recorded nothing.

`gt_compact.py status` shows the `auto_compact` setting, what a cut is with `cut_kind` and
`cut_handoff`, and whether a request is waiting. The
cache-expiry guard and the gt-state threshold never compact at a turn end, so a request, the
setting and the guard cannot cut the same turn twice.

## Why was a cut suggested (`cut_advisor`)

With `cut_advisor` on (`suggest`; default off) a hook shows the user one line after a reply when a cut would
pay back. It makes no model call and never cuts. When the user asks why, or whether it was right, run
`python3 <base_dir>/../../scripts/gt_cut_advisor.py why` and read it out: the size, the prompts likely left (a
heuristic from the last 30 minutes), the table row and the saving. `status` shows runs and milliseconds. The
table was measured on one model and one prompt shape, one run per cell: say that, and give the user the choice;
to act on it, follow the steps above.

## Rules

- **Measure first, every time.** The size and cache state are what make the advice true; never
  say "cutting now is cheap" without the measurement that shows the cache is warm.
- **Never write an in-flight note.** Carry-forward is `/gt:gt-create handoff`.
- **Never promote without the user's yes**, and never drop something the user wanted kept. The
  triage list is a proposal.
- **Estimates are labelled as estimates.** The only measured number is the billed context.
