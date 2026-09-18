---
name: gt-route
description: "Mid-conversation check: work out what this session has actually become, where its output belongs, and whether it is happening in the right place. Use when the user says: where does this go, where should this live, am I in the right project, should I be in the terminal for this, what should I do with this, I'm not sure where I'm going with this — or whenever the session has drifted far from what it opened with. Not a gate at the start of a session; a cheap check any time."
---

# Golden Thread Route

**Where does this belong, and am I in the right place to be doing it?**

Every other `/gt:*` skill fires at a session boundary — `gt-open` at the start,
`gt-work` and `gt-wiki-ingest` at the end. This one serves the **middle**, which is
where a session actually discovers what it is about.

## The premise

A user who knows exactly what they want can route themselves. This skill exists for
the far more common case: intent that only takes shape once the work is underway. A
session opens with "can I change the font size" and ends up closing an open research
question about hook enforcement. No classifier reading the first message could route
that, and one that tried would send it somewhere wrong with confidence.

So do not treat this as a gate. It is a **cheap, repeatable check**, run whenever the
ground has shifted. Being wrong costs one re-run.

## Vault location

Use `$GT_VAULT` if it is set (a session pinned to one vault, such as the demo); otherwise read `~/.claude/vault-config.json` for `vault_path`. If missing → tell the user to run
`/gt:gt-init` first.

## Steps

**Step 1 — Name what this has become**

Read the conversation already in context. **Do not read vault files for this step** —
the point is a cheap check, and re-reading project docs to answer "where are we" defeats it.

State plainly, in one or two sentences: what the session opened with, and what it is
about *now*. If those differ, **say so explicitly**. Naming the drift is most of the
value here — the user has usually felt it without articulating it.

> "This started as a font-size question. It stopped being one the moment we found the
> plugin has no such setting — it is now a finding about how the harness is built."

If nothing has drifted, say that too, in one line, and go to Step 3.

**Step 2 — Route the output**

Where does what we have learned belong? Use the promotion ladder in the vault's
`CLAUDE.md`. More than one row can apply; say so rather than forcing a single answer.

| If it is… | It goes to | Invoke |
|---|---|---|
| Ephemeral — true only of this session | Nowhere. Say so. | — |
| A dated finding about this project | `Projects/<slug>/research.md` | `/gt:gt-work` |
| A decision, with alternatives rejected | `Projects/<slug>/decisions.md` as an ADR | `/gt:gt-work` |
| How the thing is built, currently | `Projects/<slug>/design.md` | `/gt:gt-work` |
| Session detail worth keeping, project-scoped | `Projects/<slug>/memory/` | `/gt:gt-work` |
| Platform, infra, tooling or convention knowledge useful **outside** this project | A `Knowledge/` page | `/gt-wiki:gt-wiki-ingest` |
| A cross-project **process** rule | `Projects/PROTOCOL.md` | `/gt:gt-work` |
| Needed in **every** project without exception | `global-memory/` | `/gt:gt-promote` |
| Belonging to a **different** project than the one loaded | `INBOX.md`, one checkbox line | just append it |
| An idea with no home yet | `INBOX.md` | just append it |

The `Knowledge/` row belongs to the optional **gt-wiki** module. If the gt-wiki plugin is
not installed (its `gt-wiki:*` skills are not listed), say so and name
`bash install.sh --with wiki` instead of invoking `/gt-wiki:gt-wiki-ingest`; the
routing answer (a `Knowledge/` page) still stands.

Three rules that decide most ambiguous cases:

- **Project-only facts never go to the wiki.** The test is whether a session in an
  unrelated project would be helped by it.
- **`global-memory/` is `core_global_memory_scope` and it is strict** — *every* project,
  not "several". Almost nothing qualifies. When tempted, it is a Knowledge page instead.
  (Cite the rule by NAME, never by its injected number. The numbering is derived at run
  time from the rules that are gated ON, so switching `test_gate` or `parallel_work` off
  renumbers everything below it. This line said "Core rule 4" until 0.16.5, which is
  `core_test_before_commit` — a different rule entirely, and one the renumbering would
  have moved anyway.)
- **Wrong project is not a problem.** `INBOX.md` takes a line from any session with no
  project, priority or date, and `/gt:gt-review` files it later. Never leave a stray
  finding in the wrong project's `research.md` because moving it felt like work.

**Step 3 — Route the work**

Is this happening in the right place? Check three things and only mention the ones that
are actually wrong — a clean check is one line, not three.

- **Project.** Does this touch a project that is not loaded? → `/gt:gt-open <slug>`.
  If it touches *two* projects, say which one owns it and route the other to `INBOX.md`.
- **Harness.** Claudian and the terminal run the same agent; pick on interface, not
  capability. Anything needing a **real PTY** — `ssh`, `git rebase -i`, a TUI installer,
  an interactive prompt — belongs in the terminal. Anything drawing on **vault context**
  (editor selection, linked notes, `@file` mentions) belongs in Claudian. See the
  `Claudian` Knowledge page.
- **Model and effort.** Mechanical, high-volume or well-specified work does not need the
  largest model. Work that has already produced a wrong answer once probably does. Say
  so plainly if there is a mismatch; do not switch anything.

**Step 4 — Say what to do next**

End with **one** concrete next action — a skill to invoke or a file to append to. Not a
menu. If two things genuinely must happen, order them and say which is first.

If the honest answer is "nothing, keep going", say that. A check that always finds work
is a check nobody runs twice.

## Rules

- **Read nothing you do not need.** This skill runs on the conversation already in
  context. Reading `research.md` to decide where a note goes costs more than the note.
- **Never write anything.** Routing decides *where*; the destination skill does the
  writing, under its own claim. The one exception is a single `INBOX.md` checkbox line,
  which is the whole point of `INBOX.md` — claim it, append, release.
- **Do not re-route what is already routed.** If the user has already invoked `gt-work`
  for this material, say so and stop.
- **One destination is a finding; five is a shrug.** If everything looks like it belongs
  everywhere, the material has not been thought through — say that instead of listing
  every table row.
- **State the drift even when the user has not asked.** This is the half that has no
  other home: `gt-open` cannot see it and `gt-work` sees it too late.
- **Being wrong is cheap and expected.** Say what you think, name your confidence, and
  let the user correct it. Do not stall for certainty that the middle of a session
  cannot supply.
