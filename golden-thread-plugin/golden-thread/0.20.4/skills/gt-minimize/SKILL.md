---
name: gt-minimize
description: "Prune a heavy session before cutting it: measure what it carries and whether its prompt cache is still warm, keep the few things worth keeping (promoted to Knowledge/ or INBOX.md), let the rest go, and tell the user to /compact or /clear now while cutting is cheap. Use when the user says: minimize this session, prune this session, trim the context before I step away, gt-minimize."
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

## Rules

- **Measure first, every time.** The size and cache state are what make the advice true; never
  say "cutting now is cheap" without the measurement that shows the cache is warm.
- **Never write an in-flight note.** Carry-forward is `/gt:gt-create handoff`.
- **Never promote without the user's yes**, and never drop something the user wanted kept. The
  triage list is a proposal.
- **Estimates are labelled as estimates.** The only measured number is the billed context.
