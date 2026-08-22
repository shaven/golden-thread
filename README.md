# Golden Thread

A memory system for AI coding sessions, built on plain markdown and git — and,
unusually, one where the rules that matter most are **mechanically enforced** rather
than merely written down.

Every AI session starts with amnesia. You explain your conventions, the session does
good work, the session ends, and it is all gone. Golden Thread is one Obsidian vault
that acts as shared memory between you and every session you run: sessions read from
it at startup, look things up while working, and write back what they learn.

Its distinguishing idea is the second problem, the one most memory systems never
address: **writing a rule down does not mean it gets followed.**

Plugin **v0.9.0**. Four Core rules currently enforced, two of them *validated* — a
`Stop` hook inspects the finished reply and blocks it if the rule was broken.

## "In context" is not "applied"

A rule can sit in a loaded file for an entire session and still be quietly dropped for
dozens of turns — not because it was deleted, but because whatever is immediately
salient crowds it out of attention.

So a rule is only as durable as *the mechanism that re-asserts it*. Every rule carries
a **scope** (`core` / `context` / `generic`) and an **enforcement** (`reminder`, which
is re-injected every turn, or `validated`, where the output is checked and a violating
reply is blocked). A rule that must never break is Core + Validated — the only
combination that does not depend on in-the-moment discipline.

The Core tier is deliberately small; every addition dilutes the reliability of the rest.

| Rule | Enforcement | What it does |
|---|---|---|
| `core_no_secrets_in_transcript` | **validated** | Never put a secret's value into the session — not to inspect, redact or check it |
| `core_timestamp_every_message` | **validated** | Begin every reply with the current wall-clock timestamp |
| `core_global_memory_scope` | reminder | `global-memory/` holds only facts needed in *every* project |
| `core_memory_load_policy` | reminder | Do not auto-load the full memory index |

Two ways in: the user **designates** a rule, or an existing fact is **promoted** and
must answer three questions — would its absence cause incorrect code, cause rework, or
cascade into lower-level rules being written wrongly?

The two validated rules show why both paths exist. `core_no_secrets_in_transcript`
answers yes to all three. `core_timestamp_every_message` answers **no to all three** and
is Core anyway: it is the *canary*, kept because its absence is visible rather than
costly, so a broken hook announces itself.

## What is in this repo

| Path | What it is |
|---|---|
| `golden-thread-plugin/golden-thread/<ver>/` | The `gt` plugin — 12 skills, scripts, templates, hooks |
| `golden-thread-plugin/golden-thread-wiki/<ver>/` | The `gt-wiki` plugin — 5 skills for standalone wiki vaults |
| `golden-thread-plugin/install.sh` | Installs both, wires the hooks, registers the marketplace |

The vault *content* lives in a separate private repo. This one is the machinery.

## Install

```bash
bash golden-thread-plugin/install.sh
# then restart Claude Code — plugins and hooks load at session start
```

Scaffold a vault, or adopt an existing one:

```bash
python3 <plugin>/scripts/vault_init.py fresh --vault <path> --domain <name>
python3 <plugin>/scripts/vault_init.py install-core-rules --vault <path>
```

**Verify enforcement is live** — a rule that is not wired is not a rule:

```bash
echo '{}' | ~/.claude/golden-thread/hooks/inject_core_rules.sh
```

The Core rules should appear. If instead you see `ENFORCEMENT DEGRADED`, the vault
cannot be reached and the rules are not loaded — the banner names the cause.

## The skills

Twelve skills. Each composes through files rather than through other skills, so
removing any one leaves the rest working.

| Skill | What it does |
|---|---|
| `gt-init` | Sets up the vault and wires it to a project. Writes `vault-config.json`, the pointer every other skill resolves through, and adds the Golden Thread section to your global `CLAUDE.md`. Idempotent — safe to re-run on a new machine. |
| `gt-create` | Scaffolds a project: slug, domain, topology, tags, optional sub-project parent. Fills `idea.md` from what you actually said, then freezes it — that file is the traceable *why*, and it is never rewritten when the plan changes. |
| `gt-open` | Loads a project at session start, reading `source.md` first so you know which host serves which role before touching code. Stops at the memory *index* rather than the notes, so a 70-note project costs ~80 lines to open instead of ~2,000. |
| `gt-work` | Writes the session back: dated findings to `research.md`, stable choices to numbered ADRs in `decisions.md`, architecture rewritten in place in `design.md`, session state to `memory/`. The step people skip, and skipping it is what makes a vault decay. |
| `gt-promote` | Graduates a fact up a level once a second project proves it general — or *out* to a project's `CLAUDE.md`, where any agent working in that repo reads it with no vault and no setup. Reach picks the level; audience decides whether it also leaves. |
| `gt-validate` | Re-derives a claim with a validator given only the claim, the rules and the artifact — never the reasoning that produced it. Four classes: `empirical`, `vantage`, `rule-compliance`, `code`. Returns confirmed, refuted, or cannot-verify, and the third never counts as a pass. |
| `gt-query` | Answers "how does this work?" — reads `index.md`, follows wikilinks into `Knowledge/`, then falls back to grep and project memory. Flags anything returned that is marked `status: stale`. |
| `gt-ingest` | Imports an existing project's notes. Copies, never moves or deletes. Stores external sources immutably in `Sources/` before synthesising them, so the raw input survives whatever you later conclude from it. |
| `gt-review` | Sweeps Obsidian daily notes for tasks and ideas that were jotted down and never captured, then promotes the ones you pick into tracked projects. |
| `gt-refresh` | Checks `Sources/` for upstream changes. Supersedes with a *new* immutable file carrying `supersedes:` rather than editing the old one, so the record of what you believed and when stays intact. |
| `gt-lint` | Runs 13 deterministic health checks — broken links, orphans, index gaps, scope leaks, staleness, superseded sources — plus `core-unenforced`, which catches a rule that is stored but never re-asserted. |
| `gt-runbook-lint` | Finds procedures duplicated across project runbooks and routes them to the right shared layer: `PROTOCOL.md`, a `Knowledge/` page, or a repo `CLAUDE.md`. Duplication across two runbooks is the signal a fact belongs one layer out. |

## Typical use

| Situation | What you run | The part that bites |
|---|---|---|
| Starting something new | `/gt:gt-create`, then `/gt:gt-work` to close the session | `idea.md` is immutable after creation — it is the traceable *why* |
| Picking a project back up | `/gt:gt-open <slug>` | Check hosts and blockers in the summary *before* touching anything |
| "What should I work on?" | Regenerate `TASKS.md`, then read it | Priority is computed against the clock, never stored — a stale rollup is last week's ranking |
| Bringing an existing project in | `/gt:gt-ingest` | Copies, never moves. Count `[[wikilinks]]` before renaming: Obsidian resolves links by filename |
| You learned something | `/gt:gt-work`, later `/gt:gt-promote` | File at the narrowest honest scope; promotion is cheap, demotion is not |
| A number is about to become fact | `/gt:gt-validate` | Run it *before* the claim reaches `decisions.md` or production, not after |
| "How does this work?" | `/gt:gt-query <topic>` | Verify anything that comes back `status: stale` |
| Keeping the vault honest | `/gt:gt-lint` | `memory-unlisted` matters most — a note missing from the index is one no session will ever load |

Full walkthroughs, one section per skill, are in
[`golden-thread-plugin/MANUAL.md`](golden-thread-plugin/MANUAL.md).

## How knowledge moves

Six levels, and one direction that is not upward:

1. session → 2. `memory/` → 3. `research`/`decisions`/`design` → 4. `Knowledge/` →
5. `global-memory/` → 6. `core-rules/`

Levels 1–5 hold **facts** and are read on demand. Level 6 holds **rules** and is pushed
into every turn by a hook.

Separately, a second question — *would this make sense to someone who has never seen
this vault?* — sends a fact **out**, into that project's `CLAUDE.md`, committed to its
repo where any agent working in that code picks it up with no setup. Reach picks the
level; audience decides whether it should also leave.

## Maintenance is code, not judgement

The dangerous failure is not the fact you never captured — it is the fact you captured,
kept, and still serve after reality moved on. `gt_lint.py` runs 13 deterministic checks
(broken links, orphans, index gaps, scope leaks, 90-day staleness, superseded sources)
and `core-unenforced`, which catches a rule that is **stored but never re-asserted** —
the exact failure this system exists to close.

`skill_lint.py` enforces that no two skills can fire on the same intent — a rule most
systems state and check by hand. Adopting it found a live collision: two skills sharing
three verbatim trigger phrases.

## Contributing to this repo

Read [`CLAUDE.md`](CLAUDE.md) first — it carries the landmines, including the one that
has been re-broken by documentation four times: **hooks must never be referenced from
inside the vault.**
