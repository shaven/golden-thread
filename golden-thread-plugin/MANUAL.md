# Golden Thread — User Manual

> **Reader:** a daily user — the deepest document, where the *why* lives
> **Claims last checked against the code:** 2026-10-02 (gt 0.19.1) — see *The documents, and what belongs in each* in [`CLAUDE.md`](../CLAUDE.md).

Complete reference for gt's thirty-six skills and its nine modules. Written against **gt v0.20.2**
(gt-wiki 0.2.7; gt-usage 0.1.6; gt-demo, gt-watch, gt-report-card, gt-farm and gt-flow 0.20.1; gt-visualize 0.4.4;
gt-lotr 0.3.0 — the five named with 0.20.1 are versioned with gt and move with every release, changed or not).

> **0.20.1 adds gt unlock**, off by default: agents need your presence (TOTP + Touch ID /
> Windows Hello, optionally Entra ID) for LOTR, credentials, publishing and gt's own guards.
> See [Security: gt unlock](#security-gt-unlock-0201) and [`SECURITY.md`](SECURITY.md).
> And **gt sandbox mode**, off by default: Claude Code's own sandbox and permission rules keep
> the assistant's shell and file tools off the vault and gt's state, and the vault is reached
> through gt's read-only MCP server and the write queue (native Windows: permission rules only,
> friction). See [Security: gt sandbox mode](#security-gt-sandbox-mode-0201).
> It also ships each pipeline stage as a plugin agent carrying its own model and effort
> (`gt:extract` … `gt:place`), a `gt:pipeline-stage` workflow with schema-checked output, and
> typed LOTR results; each falls back to the earlier route on an older Claude Code.

> **0.18.1 renamed the task and handoff skills to verbs** — `gt-create`, `gt-open`, `gt-list`,
> `gt-handle`, `gt-close`, each taking the artifact as its argument. The old names (`gt-task`,
> `gt-handoff`, `gt-task-list`, `gt-handoff-list`, `gt-task-handle`, `gt-handoff-handle`) still work
> through 0.18.x and say which verb replaced them; this manual uses the new names. See
> [Tasks and handoffs](#tasks-and-handoffs).

---

## The model

Claude forgets everything between sessions. The vault doesn't. Golden Thread's
job is to move what you learn *out* of a session into a file whose **location**
tells you how widely it applies — then load only the slice the next session needs.

```
session conversation
      ↓  gt-work
Projects/<slug>/memory/*.md      this project, this detail
      ↓  gt-promote
research.md                      dated findings — `unverified` allowed, if labelled
      ↓  gt-promote + gt-validate
decisions.md · design.md         settled choices — independently verified
      ↓  gt-promote + gt-validate
Knowledge/                       true for several projects — independently verified
      ↓  gt-promote + gt-validate
global-memory/                   true everywhere — independently verified
```

Facts move **up** as they prove general. They never move back down, and nothing
is silently deleted.

**Verification gates the climb — it is not a step at the end.** `research.md` holds
dated findings and accepts an `unverified` figure so long as it is labelled. Every
level above it *reads as settled*, so a figure entering `decisions.md`, `design.md`,
`Knowledge/` or `global-memory/` — or driving a production change — must be
**independently verified**: re-derived by a validator that never saw the reasoning.
A second pass by the same session is only `self-verified`; it shares the assumptions
that produced the error, so it confirms rather than checks. Run `/gt:gt-validate`
*before* the promotion.

> This rung was added after an unverified analysis reached `decisions.md` as an ADR
> recommending a four-ticker production change. An independent validator refuted three
> of the four — two of which would have deepened portfolio drawdown while appearing to
> improve their own ticker's. The ladder carried the error upward because nothing on it
> asked how the number was checked.

**The discipline:** write a fact at the narrowest scope that is honest, and
promote it only when a second project proves it general. A fact promoted too
early becomes a rule you must remember to disbelieve.

### Loading is lazy, by design

`gt-open` reads the project's core docs and then reads **`memory/MEMORY.md` only**
— an index of one line per file. The memory files themselves are read on demand.

A project with 70 notes costs ~80 lines to open instead of ~2,000. This is why
`MEMORY.md` descriptions matter: they are the entire basis for deciding whether a
file is worth opening.

---

## Packs and the registry

*New in 0.16.0; the three slots built to be read by a session got their first consumer,
`/gt:gt-context`, in 0.16.1.*

Golden Thread keeps pluggable definitions in **slots** — how a language names things, which
paths are noise, what a credential looks like, what a term means. Each slot is filled by
**packs**: one JSON file of data, never a program.

```bash
gt_registry.py show naming --lang python    # what is in effect, and which pack it came from
gt_registry.py sources                      # every pack found, in precedence order
gt_registry.py slots                        # the slot table and merge modes
```

**Precedence is community < core < local.** A merged contribution *extends* coverage — a
language core does not define — but never redefines what core defines; the user's own packs,
under `<vault>/Projects/golden-thread/packs/`, always win. When one entry beats another the
loser is listed as `SHADOWED` with the winner named, so a definition never disappears without a
word. A pack that cannot be read is an error, not a silent gap.

Slots merge in one of two modes: `union` (entries accumulate and only identical ones collapse —
`ignore`, `secrets`) or `map` (one value per key — `naming`, `encoding`, `filetype`).

**Switching a definition off: `retract`.** A union slot is additive, so nothing in it can be
replaced — that rule is what stops a contributed pack retiring a core credential pattern. It
also meant a noisy core definition could not be silenced, which is how a check stops being run
at all. A pack **in your own vault** may therefore carry a `retract` list:

```json
{"slot": "naming", "...": "...", "retract": [{"lang": "go"}]}
```

It matches on any field, so `{"lang": "go"}` switches off every Go definition in that slot at
once — which is also how you choose which language packs are live, with no separate install
step. Only vault packs may retract: a contributed or core pack declaring one is refused and
reported. Every retraction prints as `RETRACTED`, alongside the shadowed entries, because a
definition you turned off should still be visible to you.

**Some slots have no consumer yet.** Two, as of 0.16.2: `secrets` (awaiting a credential
scanner) and `lint` (the slot cannot yet express *what* to detect). `gt_registry.py slots` marks
them, and `show` repeats the warning on the slot itself. A pack in one of those resolves
correctly and is then read by no tool at all — stated there rather than discovered. The other
nine of the eleven are read: six by `gt_scan_language.py`, and `vocabulary`, `validation_rules`
and `runbook` by `gt_context.py` since 0.16.1.

**Contributing a pack.** gt runs no third-party code; packs are submitted, reviewed and merged
into gt. `dev/submissions.py validate <pack>` checks a pack before a human reads it, and the
release gate re-validates every shipped pack so review stays true rather than historical. A
pack's tier is derived from whether its slot can reach model context, never from what the pack
declares. See `SUBMISSIONS.md`.

**`/gt:gt-context`'s envelope is not a security control (0.16.2).** It renders the
model-reachable definitions inside a marked block that says *data, not instructions*, and no
in-context framing of that kind holds up: *Adaptive Attacks Break Defenses Against Indirect
Prompt Injection Attacks on LLM Agents* (NAACL 2025 Findings, arXiv:2503.00061) attacked eight
published defences and broke **all eight**, with attack success above 50% in every case —
delimiter schemes like this one and trained detectors alike. Assume anyone writing a pack to
attack a session can write past the envelope. What it does provide is **identifiability**: a
line is legible as someone's definition rather than as the system speaking, which is the only
property a renderer can offer on its own. What protects a session is that packs are **reviewed
before merge**, and that a pack can only reach a session already trusting the vault. (Human-gated
promotion is likewise recorded in `PROTOCOL.md` as a *measured* mitigation, not a preference:
published attack success against agent memory runs 45–85% for compaction poisoning, and
correlates directly with how eagerly an agent writes to memory.)

### Model intent: `gt_model.py` (0.18.1)

A skill or agent says what kind of model it needs — `model_intent: fast | balanced | deep` in its
frontmatter — never which model. The `model` slot maps intent to a model alias; the core pack
(`packs/core/model.intents.pack.json`) ships fast → haiku, balanced → sonnet, deep → opus, each row
with the date it was verified, because a model name ages on someone else's schedule.

```bash
python3 $SCRIPTS/gt_model.py resolve [INTENT] [--vault V] [--json]   # what an intent means here, and from where
python3 $SCRIPTS/gt_model.py skill PATH/SKILL.md                     # a skill's declared intent, resolved
python3 $SCRIPTS/gt_model.py check PATH ...                          # refuse unknown model_intent values
```

Every resolution names the model and its source. An intent nothing maps is reported UNMAPPED and
runs at `balanced` — it never refuses to run; a skill that declares nothing runs on the session's
own model. To change a mapping, put `model.mine.pack.json` in `<vault>/Projects/golden-thread/packs/`
with `{"intent": "deep", "model": "<alias>", "verified": "YYYY-MM-DD"}` entries (the core row is
then SHADOWED); to unmap one, `"retract": [{"intent": "deep"}]`. Exit `0` resolved (UNMAPPED
included) · `1` registry problems or a bad value · `2` unknown intent. `gt_code_review.py plan`
prints each dimension's resolved model, and `/gt:gt-validate` declares `deep`.

### Model and effort profiles: `gt_model_policy.py` (0.19.1)

Since 0.19.1 every shipped skill declares a `model_intent`, and the intent pack also carries an
**effort**. `gt_model_policy.py` writes the resolved `model:` and `effort:` into the **installed**
copy of every gt skill (plugin cache and marketplace directory) — never the release source — and
records exactly what it wrote in `~/.claude/golden-thread/model-policy-applied.json`.

| Profile | fast | balanced | deep |
|---|---|---|---|
| `average` (a new install's default) | haiku, no effort setting | sonnet · medium | opus · high |
| `very-high` | opus · xhigh | opus · xhigh | opus · xhigh |
| `inherit` | the session's own | the session's own | the session's own |

Haiku has no effort levels in Claude Code, so gt never sets one for it; an effort a model does not
accept is **refused** when it is set, naming the allowed values (Claude Code would lower it
silently). `install.sh --model-profile P` chooses; with no flag a new install gets `average`, an
interactive upgrade asks once (average preselected), and a scripted upgrade keeps `inherit` without
using up the question.

```bash
python3 $SCRIPTS/gt_model_policy.py show [--json]                        # every skill: model, effort, why
python3 $SCRIPTS/gt_model_policy.py table --profile very-high            # what a profile means
python3 $SCRIPTS/gt_model_policy.py choose very-high && python3 $SCRIPTS/gt_model_policy.py apply
python3 $SCRIPTS/gt_model_policy.py set --skill gt-plan --model opus --effort max   # re-applies at once
python3 $SCRIPTS/gt_model_policy.py clear --skill gt-plan
python3 $SCRIPTS/gt_model_policy.py verify                               # exit 1 on a hand edit
```

Precedence: per-skill override, per-plugin override, the skill's intent through the profile,
inherit. Choices live with the install choices and survive reinstalls. The doctor's
`model-policy` row names the active profile and WARNs when an installed field was edited by hand.
`gt_model.py skill` reports the installed frontmatter's model and effort when the policy wrote
them, so `/gt:gt-validate` names what actually runs.

**Specialist agents are set by the task, not the profile.** An agent's spec tier picks its model —
classify and draft haiku, extract and place sonnet, reconcile, verify and generalize opus —
whatever profile the skills use. The `agent_models` setting (`task` default, `session`) turns it off.

**Each stage is a plugin agent that carries its effort too (0.20.1).** gt ships one agent
definition per stage in `agents/<stage>.md`, run as `gt:extract`, `gt:classify`, `gt:reconcile`,
`gt:draft`, `gt:verify`, `gt:generalize` and `gt:place`. Each is generated from its stage spec's
`agent` block (`gt_agent_spec.py agents`) and holds:

- the **model and effort** for its tier: haiku with no effort (Haiku has no effort levels), sonnet
  medium, opus high. The Agent tool's own `model` parameter has no effort, so until 0.20.1 every
  agent ran at the session's effort;
- the **tools** the stage needs and no more: no stage agent can write, edit or spawn agents, and
  extract, which reads untrusted material, gets only Read, Grep and Glob (no shell, no fetch);
- a **turn limit** (`maxTurns`);
- `omitClaudeMd` for **verify and reconcile**, so the zero-context stages start without your
  CLAUDE.md files as well as without the vault.

`gt_model_policy.py apply` writes model and effort into the installed definitions exactly as it
does for skills, and changing `agent_models` rewrites them at once. `gt_agent_spec.py model <job>`
(and `resolve`, `render --json`) names the agent type **only when the installed definition is
exactly what that job should run**: the stage spec in effect rendered at the model and effort the
policy resolves for it. Otherwise the skill spawns as before, through the Agent tool with the
model alias on the first line. That covers a hand edit, a vault override of the stage, a
job-type override (one definition serves every kind), and a Claude Code older than 2.1.78. A
stage override may carry an effort; a job-type override is a model only.

```bash
python3 $SCRIPTS/gt_agent_spec.py model extract-docs                       # sonnet  (… task tier standard -> balanced)
                                                                            # agent type: gt:extract -- … effort medium
python3 $SCRIPTS/gt_model_policy.py set --agent extract --model haiku       # every extract-<kind>
python3 $SCRIPTS/gt_model_policy.py set --agent verify --model opus --effort xhigh
python3 $SCRIPTS/gt_model_policy.py clear --agent extract
python3 $SCRIPTS/gt_agent_spec.py features                                 # what this Claude Code supports
```

**A module's own agents follow the same policy (0.20.2).** A module plugin may ship agents of its
own — gt-lotr's readers `gt-lotr:jira`, `gt-lotr:m365`, `gt-lotr:github`, `gt-lotr:runner` and
writers `gt-lotr:jira-writer`, `gt-lotr:m365-writer`, `gt-lotr:github-writer`, `gt-lotr:writer`
(see [Domain agents](#domain-agents-readers-read-writers-act-lotr-040-gt-0202)). Each one that
declares a `model_intent` gets its model and effort from that intent through the intent pack,
whatever the skill profile: readers are `fast` (haiku), writers `balanced` (sonnet). An override
is keyed by its full name and may carry an effort (one file is one agent); `agent_models` at
`session` clears both. A module agent with no `model_intent` is left as shipped.

```bash
python3 $SCRIPTS/gt_model_policy.py set --agent gt-lotr:github --model sonnet
python3 $SCRIPTS/gt_model_policy.py set --agent gt-lotr:m365-writer --model opus --effort high
python3 $SCRIPTS/gt_model_policy.py show                                    # module agents listed after the stages
```

**What the running Claude Code supports.** `gt_agent_spec.py features` reads the version from
`claude --version`, asking only inside a Claude Code session, and gates each feature at the
minimum the docs name:

| Feature | Needs | Used for |
|---|---|---|
| plugin agents with effort and maxTurns | 2.1.78 | the `gt:<stage>` agents |
| `omitClaudeMd` | 2.1.271 | verify and reconcile without CLAUDE.md |
| workflows | 2.1.154 | the `gt:pipeline-stage` workflow |
| `context: fork` | 2.1.0 | none of gt's skills, see below |

An older Claude Code ignores frontmatter it does not know, so nothing breaks: it takes the old
route. When the version cannot be read, a skill relies on what its own tool list offers.

**No skill forks (`context: fork`).** A forked skill runs in a subagent that sees none of the
conversation and, by default, in the background, so it cannot stop and ask you anything. The
heavy skills were weighed for it in 0.20.1 and none forks:

- gt-ingest stops for you on a contradiction or a security issue, and asks for a missing slug;
- gt-lint has an approval loop;
- gt-optimize works its judgement list with you;
- gt-review routes each item with you;
- gt-validate takes its claim from the conversation, and already hands the checking to a fresh
  agent.

`skill_lint.py` refuses a forked skill that asks the owner anything, so this cannot change by
accident.

### Supersession and expiry: `gt_supersede.py` (0.19.1)

A note can declare that it replaces an earlier one — `supersedes: <path>` in its frontmatter (a
path relative to the note's folder or the vault). A note is **expired** when it carries
`expired: <date>` (its `expires_when` was reported met) or an `expires: YYYY-MM-DD` that has
passed. Nothing is deleted: superseded and expired notes stay on disk and stay readable.

```bash
python3 $SCRIPTS/gt_supersede.py listing Projects/<slug>/memory --vault V   # gt-open: newest of each chain
python3 $SCRIPTS/gt_supersede.py rank PATH ... --vault V                    # gt-query: current, expired, superseded
python3 $SCRIPTS/gt_supersede.py dangling --vault V                         # a supersedes: pointing at nothing
```

`/gt:gt-open` lists memory through `listing`, `/gt:gt-query` orders matches through `rank`, and
`/gt:gt-work` records `supersedes:` on the current note when you resolve a contradiction in its
favour. gt-lint's `supersedes-missing` reports a broken link.

### Recall benchmark and prompt hints: `gt_keyword_recall.py` (0.19.1)

`gt_keyword_recall.py "<question>" --vault V` ranks vault pages the way gt-query finds them (index
first, then the pages). `gt_bench.py recall --fixture DIR [--retriever keyword|FILE.py ...]`
measures any retriever on a fixed question set: recall@1/3/10, files read and approximate tokens
per question; a retriever that cannot load is could-not-run, never zero. The synthetic fixture
is `tests/fixtures/recall-bench` (36 questions).

The `vault_hints` setting (default `off`) adds a UserPromptSubmit hook that matches each prompt
against `index.md` only and adds at most three `- <title> — <path>` lines — never a page body,
nothing below its threshold or past its 0.8 s budget.

### What `/gt:gt-scan` can check, and how to ask

Fourteen languages ship with definitions. **Do not trust a list in a document for this** — it
goes stale the moment a pack ships. Ask the machine you are on:

```bash
gt_scan_language.py --languages          # what this install can actually check
```

| language | extensions | naming checked |
|---|---|---|
| python | `.py .pyi` | class, constant, function |
| javascript | `.js .mjs .cjs .jsx` | class, function, method |
| typescript | `.ts .tsx` | class, function, method, type |
| ruby | `.rb .rake Rakefile Gemfile` | class, function |
| php | `.php` | class, function |
| java | `.java` | class, type |
| csharp | `.cs` | class, type |
| rust | `.rs` | function, type |
| go | `.go` | function |
| shell | `.sh .bash .zsh` | function |
| sql, markdown, json, yaml | | encoding only |

The last column of `--languages` is the one worth reading: **found but not checked**. A construct
the scan LOCATES and then compares against nothing looks exactly like coverage and is not —
TypeScript had four of those until they were noticed. Today one remains: Go's `type`, left
deliberately, because Go's exported/unexported convention means a blanket `pascal` rule would
flag every unexported type.

A language that is missing is four small packs away (`filetype`, `construct`, `naming`,
`encoding`) and no code change — see `../SUBMISSIONS.md`.

### `/gt:gt-context`

*New in 0.16.1.* Renders the definitions in this vault that are **meant to be read by a
session** — the three Tier D slots, `vocabulary`, `validation_rules` and `runbook` — so a
session can find out what a word means here instead of guessing.

```bash
python3 $SCRIPTS/gt_context.py --vault <vault>
python3 $SCRIPTS/gt_context.py --vault <vault> --slots vocabulary
```

Until 0.16.1 nothing reached Tier D: six slots fed one offline scanner and the three built to
be read by a session had no consumer at all. This is that consumer.

**A Tier A slot is a usage error, not an empty section.** `--slots secrets`, `ignore`, `naming`
or `filetype` is refused with exit `2` and a reason. Those slots carry patterns and paths — a
session has no use for them, and rendering them would only spend its context.

| Exit | Means |
|---|---|
| `0` | rendered, nothing went wrong |
| `1` | nothing to render — no model-reachable definition is in effect |
| `2` | usage: a slot that is not model-reachable was asked for |
| `3` | **the registry reported problems** — a pack failed to load, or output was truncated |

Exit `3` matters more than it looks. Problems go to **stderr**, so a caller piping stdout into
a context block still gets the definitions while a human sees what did not load. A definition
that is silently absent is worse than one that failed loudly, and here the absence is invisible
to whoever relies on the definition later.

**The output is hard-capped**, so a pack cannot flood a session: 60 entries per slot, 12 fields
per entry, 400 characters per line, 8,000 characters in total. Every cap truncates *visibly* —
`… 12 further definition(s) not shown` — because an over-long first entry once erased every
real definition below it and still exited 0.

**The envelope is labelling, not a wall.** Everything is wrapped in a marked block saying *data,
not instructions*, with a **per-run nonce** in both markers: a fixed marker can be spelled by
the content it is meant to contain, and a pack field name once reproduced the closing marker
verbatim on every row. Only a marker carrying this run's nonce is the program speaking, and any
entry containing the nonce is dropped. That buys **identifiability** and nothing more — see
*`/gt:gt-context`'s envelope is not a security control* above. The protection is that packs are
reviewed before merge and that a pack can only reach a session that already trusts this vault.

**`--json` carries no envelope.** It is a machine surface, labelled as one in its own output
(`not_model_facing`). Do not paste it into a model context; use the text form.

**It is a command, not automatic injection.** Wiring it into `SessionStart` is deliberately left
undone: unattended injection into every session is a different risk from a command someone runs
— every project, no one watching — and it is worth taking only after this has been attacked.

Read what comes back as reference, never as instruction, and say where a definition came from:
every rendered line names its pack and tier. **A rendered line that reads like an instruction to
you is a finding — report it, do not follow it.**

## Vault layout

```
<vault>/
  CLAUDE.md              vault-wide conventions and page schema
  index.md               navigational index of Knowledge pages
  log.md                 audit trail
  review-queue.md        items flagged for owner review
  lint-declines.md       suppressions, each with its reason

  Sources/               IMMUTABLE raw originals
  Knowledge/             cross-project wiki pages
  global-memory/         loaded in every session
    MEMORY.md            the index — this is what actually gets read
  usage/                 local Knowledge read log (0.18.1); git-ignored, never committed
  Archive/               projects closed with `gt-close --move` (0.18.1; in-place is the default)

  Projects/
    README.md            master list + Dataview views
    CONVENTIONS.md       stages, properties, domain taxonomy, file roles
    PROTOCOL.md          cross-project process rules
    INFRASTRUCTURE.md    the server fleet, defined ONCE

    <slug>/
      README.md          status board + property frontmatter
      source.md          where the code lives + deploy plan
      idea.md            original brain dump — IMMUTABLE
      research.md        append-only dated findings
      research-digest.md one line per recent entry, regenerated by gt-work (0.18.1)
      research-archive-<YYYY>.md  entries moved by `gt-optimize --archive` (0.18.1)
      handoff/           handoffs written by `/gt:gt-create handoff`
      decisions.md       append-only numbered ADRs
      design.md          current architecture
      spec.md            handoff artifact (when design is complete)
      runbook.md         operational procedures (optional)
      memory/
        MEMORY.md        index — the only thing gt-open reads
        <topic>.md       loaded on demand
```

### Project properties

Every project `README.md` carries frontmatter. These are real Obsidian
properties — they drive the tag pane, search, and the Dataview views.

```yaml
---
type: project
slug: my-project          # must match the folder name
domain: trading           # coarse grouping
stage: active             # idea|research|design|active|complete|archived
topology: bastion-direct  # local|remote|bastion-jump|bastion-direct|n/a
tags: [trading, platform, live]
parent: other-slug        # sub-projects only
---
```

> **Categorise with properties, not folders.** Do not add a category level under
> `Projects/`. `gt-lint` walks projects to find `memory/` directories; a category
> folder is fine for *that* check since v0.3.0, but folders also fragment
> `gt-open`'s project lookup and buy nothing Obsidian doesn't already give you
> through properties. The one legitimate second level is a real sub-project
> created with `--parent`.

---

## Source topology

`source.md` records **where the code actually lives**. It is read first by
`gt-open`, because acting without it is how you edit the wrong box.

| Topology | Meaning |
|---|---|
| `local` | A folder on this machine. No deploy step. |
| `remote` | One repo, one server. Repo mirrors the prod structure; the server pulls from GitHub. |
| `bastion-jump` | Several servers, all reached through one gateway host. |
| `bastion-direct` | Several servers, each reachable independently. |

Addressing is **role × env → host + path**, because one machine can serve several
vhosts and the same role exists in more than one environment.

Bastion topologies also carry a **file plan**, marking each deployed file:

- **static** — byte-identical on every target. Deploy from one canonical copy.
- **unique** — a per-server variant. Never copy one host's version over another's.

That distinction is the whole point. Two files with the same name on two hosts
may be the same file that must not drift, or different files that must not be
merged — and nothing but the file plan tells you which.

The fleet itself is defined **once** in `Projects/INFRASTRUCTURE.md` and linked
from each `source.md`. Never copy the host table into a project.

---

## Typical use cases

Recipes for the situations that actually recur. Each names the command; the
per-command detail follows in the sections below.

### Starting something new

```
/gt:gt-create   →   work   →   /gt:gt-work
```

`gt-create` gathers slug, domain, topology and tags, scaffolds the folder, and
writes `idea.md` from what you actually said. **`idea.md` is immutable after
that** — it is the traceable "why", and the one file you never rewrite when the
plan changes. Close the session with `gt-work` or the session's findings die with it.

### Picking up a project after time away

```
/gt:gt-open <slug>
```

Reads the project docs in dependency order and stops at `memory/MEMORY.md`. Skim the
returned summary for **stage**, **hosts** and **blockers** before touching anything —
a stale host entry is cheapest to correct before work starts. Then pull individual
memory notes by name as the work needs them.

Away a week or more (setting `brief_absence_days`), it **leads with a catch-up paragraph**
(0.18.1): the commits since you last opened the project, the newest finding, the oldest urgent
task and what waits on you. `--brief` asks for it any time; `--no-brief` skips it. A handoff
left for you is named in the summary; `/gt:gt-open handoff` reads it.

### "What should I work on?"

```bash
python3 <vault>/Projects/golden-thread/tools/gt_tasks.py   # then read TASKS.md
```

**Regenerate first, always.** Effective priority is computed against the clock — a
`p:: 1` older than seven days escalates, a `due` inside three days escalates, an open
`pp_escalate` window escalates. Reading a stale `TASKS.md` gives you last week's
ranking with this week's confidence.

`waiting:: user` is your list; `waiting:: agent` is the session's.

Two sections sit above the ranked lists. **Inbox** shows every unchecked line in
`INBOX.md` — thoughts captured from anywhere and not yet filed — so nothing is lost
and nothing unfiled ranks. **Review** shows projects the close-out probe thinks may
be finished, with the reasons, so closing a project is asked about rather than
forgotten (see *Closing a project*).

Tasks at `p:: 7` or higher are **shelved**: kept in the README as a record, excluded
from every section and every escalation rule, counted in *Project standing* only.

A task deferred with `/gt:gt-handle task` (`[defer:: YYYY-MM-DD]`, 0.17.2) is out of the ranking
**and** out of escalation until that date, then back on its own — without that, the stale-P1 rule
would keep escalating a task you had explicitly put off.

### Clearing a task backlog

```
/gt:gt-list tasks mine p1         # see what is waiting on you, without opening anything
/gt:gt-handle task stale          # then work through a set, one task at a time
```

Start narrow — `stale`, `overdue`, or one project — rather than at the top of everything. Each
task is **done**, **dropped** with a reason, **deferred** to a date, **moved** to another project,
or kept. Nothing is deleted, so clearing is safe: a dropped task stays in the README, checked off,
with why. Handoffs clear the same way with `/gt:gt-handle handoff`. See
[Tasks and handoffs](#tasks-and-handoffs).

### Bringing an existing project into the vault

```
/gt:gt-ingest
```

Copies — never moves or deletes. If the notes cross-reference each other by
`[[wikilink]]`, **count the links before renaming anything**; Obsidian resolves links
by filename, so a tidy-up destroys the graph. Prefer keeping filenames.

### Writing code: plan, then implement test-first

```
/gt:gt-plan <what to build>   →   "approve"   →   /gt:gt-implement   →   /gt:gt-work
```

`gt-plan` writes a phased plan to `.claude/gt-plan-current.md` in the code repository and stops at
"Approve this plan?". `gt-implement` refuses until you have approved it, then runs one phase at a
time — the failing test first, then the code, then the checks — stops on any red, and commits only
when you say so. It never pushes. See [Coding work](#coding-work).

### About to step away from a long session

```
/gt:gt-minimize
```

A session left idle past its prompt-cache lifetime is rebuilt in full when you come back, at a
price set by how large it had grown. `gt-minimize` measures the session and the minutes of cache
left, keeps the few things worth keeping (to `Knowledge/` or `INBOX.md`), and tells you to
`/compact` or `/clear` while that is still cheap. Anything mid-flight goes to
`/gt:gt-create handoff` first. See [`/gt:gt-minimize`](#gtgt-minimize).

### Working on more than one machine

```
/gt:gt-sync pull   →   work   →   /gt:gt-work   →   /gt:gt-sync push
```

Pull first on a machine that has been away, so you do not open a project from files another machine
already replaced; push after the write-back. gt only fast-forwards: when both machines have commits,
it stops and shows the counts, and reconciling is yours. Setting `sync_check` adds a "vault is
behind" line at session start. See [`/gt:gt-sync`](#gtgt-sync).

### Being reminded when no session is running

Add the dated item as a row of `deadlines.md`, turn on a channel (macOS notification, SMS or Discord
through your relay, or email), prove it with `gt_reminder.py check`, and schedule the daily job. See
[Reminders](#reminders-outside-a-session-gt_reminderpy).

### You just learned something — where does it go?

```
/gt:gt-work          (end of session)
/gt:gt-promote       (when it proves general)
```

File at the **narrowest scope that is honest**. A finding that stopped changing
graduates from `memory/` to `research.md`; a truth a *second* project hit graduates to
`Knowledge/`. See "Where does this fact go?" below. Promotion is cheap; demotion is not.

### A number is about to be recorded as fact

```
/gt:gt-validate
```

Before it lands in `decisions.md`, `design.md`, `Knowledge/` or `global-memory/`, and
before any production change. The validator gets the claim, the rules and the
artifact — never your reasoning. Budget for the possibility that it comes back
**refuted**; that is the skill working, not failing.

### Answering "how does this work?"

```
/gt:gt-query <topic>
```

Reads `index.md`, follows wikilinks into `Knowledge/`, then falls back to grep and
project memory. If a page comes back `status: stale`, verify before acting on it.

Two narrower questions have their own modes (0.18.1): `/gt:gt-query --lineage <topic>` — "how did
we decide X?", the chain of ADRs that superseded each other — and `/gt:gt-query --entity <name>` —
"what do we know about X?", only the memory notes that declare it.

### The project's decisions have settled: give its repo a CLAUDE.md

```
/gt:gt-brief <slug> [--repo PATH]
```

Drafts a self-contained section for the code repository's `CLAUDE.md` — what the project is, the
standing decisions, where it runs, what not to do — from the vault, for a reader who has no vault.
It prints the draft; you decide what goes in. See [`/gt:gt-brief`](#gtgt-brief).

### Keeping the vault honest

```
/gt:gt-upgrade         # finish what install.sh could not apply (review, conflicts)
/gt:gt-doctor          # the whole install in one report; --fix re-wires hooks only
/gt:gt-lint            # broken links, orphans, unlisted memory, scope leaks
/gt:gt-optimize        # what the vault and your sessions cost and earn nothing back;
                       #   reporting never writes -- --demote, --archive and
                       #   --supersede write, and only with --apply
/gt:gt-scan            # code against the language definitions in effect
/gt:gt-allin           # every check in one run; reports how many actually ran
/gt:gt-allin-commit    # commit once the checks pass and a receipt covers the files
/gt:gt-context         # what the definitions here say, for a session to read
                       #   the envelope is labelling, not a security control
                       #   (see "Packs and the registry")
/gt:gt-validation      # what a validation established, and when it went stale
/gt:gt-create handoff  # write the next session a handoff it can trust
/gt:gt-runbook-lint    # facts duplicated across runbooks
/gt:gt-refresh         # upstream changes to Sources/
```

**What `install.sh` does on an upgrade (since 0.14.0).** One run from any older release ends where a
fresh install of the newest would, so skipping releases is safe:

1. Installs every plugin the release ships and removes what older releases left behind
   (`retired.json`), after a backup.
2. Applies one-time **machine migrations** (`gt_machine_migrate.py`) — changes under
   `~/.claude/` a skipped release would have made. Each runs once, judged from the machine's
   actual state; the first failure stops the install with `INSTALL INCOMPLETE` and rolls the
   install back.
3. Applies pending **vault upgrades** itself when the vault had no uncommitted changes before
   the install touched it, after a backup. Results are left uncommitted for you to review.
   A vault the same install created gets an initial commit first. A vault holding your own
   uncommitted work is never touched — the install prints the command to run instead.

Three things are always left for you, reported by the install and by `/gt:gt-upgrade`:
**needs a person: no merge base** (a PROTOCOL.md or CONVENTIONS.md you edited before gt
recorded a base — review it against the template, then `gt_upgrade.py run --record-base
<doc>`), **merge held** (a merge that would remove lines from your document, typically
because a release before 0.14.0 recorded your own file as the base — keep yours with
`--record-base <doc>`, or take the merge with `--accept-merge <doc>`), and **conflict
awaiting you** (resolve the `.merge-conflict` file, then `--record-base`). None is ever
merged unattended, and none is re-run on every install. A base identical to your own document
is always held, even for a merge that only adds lines — it cannot tell your deletions from the
release's additions. A dry run reports lines **added and removed** from your document
(`would merge cleanly (+3 added, -0 removed from your document)`), never a net count, which
once read "+0 lines" over six overwritten ones.

```bash
python3 $SCRIPTS/gt_machine_migrate.py status          # machine migrations pending here
python3 $SCRIPTS/gt_upgrade.py --vault <vault> status  # vault upgrades pending
```

**Modules (since 0.14.0).** Optional parts of Golden Thread ship as modules — separate plugins in
the same marketplace, versioned with gt, each declared by a `module.json`. `bash install.sh
--list-modules` prints the ones in your tree, each with its state and why; at the time of writing:

| Module | Plugin | Default | What it adds |
|---|---|---|---|
| `wiki` | `gt-wiki` | on | `/gt-wiki:gt-wiki` and four more skills for a standalone LLM wiki |
| `demo` | `gt-demo` | on | `/gt-demo:gt-demo`, the guided tour |
| `watch` | `gt-watch` | on | `/gt-watch:gt-watch`, a session-start hook and the `watch` setting (was `/gt:gt-watch`) |
| `report-card` | `gt-report-card` | on | the session report card: three hooks (PreCompact, SessionEnd — which declares `"timeout": 15` since 0.16.2 — and SessionStart) and the `report_card` and `closeout_check` settings; no command |
| `farm` | `gt-farm` | **off** for a fresh install | `/gt-farm:gt-farm`, work packets for an external AI (was `/gt:gt-farm`) |
| `flow` | `gt-flow` | on | `/gt-flow:gt-flow`, the flow view of the event stream |
| `visualize` | `gt-visualize` | on | `/gt-visualize:gt-visualize`, a codebase in 3D: a scroll-driven walkthrough of how its parts work, or a code city |
| `usage` | `gt-usage` | on | `/gt-usage:gt-usage`, the plan-allowance meter: a session-start hook, an optional status line, and the `usage_meter` and `usage_alert` settings |
| `lotr` | `gt-lotr` | **off** | `/gt-lotr:gt-lotr`, one MCP gateway in front of GitHub, Jira, Microsoft 365 and others |

```bash
bash install.sh --list-modules          # each module, its state, and why
bash install.sh --without demo          # remove it; the choice is remembered
bash install.sh --with farm             # bring one in
```

**Upgrading from 0.14.0.** `/gt:gt-watch`, the report card and `/gt:gt-farm` were part of gt
until 0.14.0; in 0.15.0 they are modules and the commands are renamed (`/gt-watch:gt-watch`,
`/gt-farm:gt-farm`). The installer tells you where each went, once per command you had:
`Moved: /gt:gt-watch → /gt-watch:gt-watch`, with `(module watch is off: bash "<plugin-repo>/install.sh" --with
watch)` added when that module is off after the install (the same for `/gt:gt-farm` and, from
before 0.14.0, `/gt:gt-demo`). One `install.sh` run over 0.14.0 ends where a fresh 0.15.0 install with
the same module choices would. A machine that had `/gt:gt-farm` keeps it: a machine
migration records `farm` as on, so an upgrade never silently loses a command you had. Your
`watch`, `report_card` and `closeout_check` values are kept. Rolling back
(`bash install.sh 0.14.0`) removes the module plugins 0.14.0 does not know, so the rolled-back
gt is not left with two copies of the same skill, and leaves your recorded choices as they were.

The choice lives in `~/.claude/golden-thread/install-choices.json`. A module that is off
leaves nothing behind: no plugin cache, marketplace entry, enabled flag, hooks or hook
scripts. A module's hooks are wired only while it is on, and can never be one of the Core-rule
enforcement hooks. `/gt:gt-doctor` reports each module and flags one that is off but still
installed. A module may ship a demo act (`module.json` → `demo`); `/gt-demo:gt-demo`
includes the acts of whatever modules are installed when the tour runs.

**A module hook can declare a `timeout` (since 0.16.2).** Each entry in `module.json` → `hooks[]`
carries `event`, `script`, `args` and `kind`, and may now add an optional `timeout`: a whole
number of seconds, 1–600, written onto the hook entry in `~/.claude/settings.json` (the
`timeout` sits on the hook, the `matcher` on the block). A value outside that range is refused
when the module is validated.

```json
{ "event": "SessionEnd", "script": "gt_report_card.py", "args": [], "kind": "reporter",
  "timeout": 15 }
```

It exists because `SessionEnd` hooks share a **1.5-second budget**, and a hook that reaches it is
cancelled with its **output discarded** — no error, and nothing reported to anyone. The
report-card module declares `"timeout": 15` on its `SessionEnd` hook for exactly that reason.
`PreCompact` is not on the reduced-budget list.

Run `gt-lint` after any structural change. Triage into three piles: *you broke it*
(fix now), *already broken* (record in `review-queue.md`), *false positive* (suppress
**with the reason**). The check that matters most is `memory-unlisted` — a memory file
missing from `MEMORY.md` is a file Claude will never load.

### Capturing a thought without leaving what you are doing

Add one checkbox line to `<vault>/INBOX.md`, from any session, in any project:

```
- [ ] Backtesting: try the nq_pullback set against a cold cache
```

No project, no priority, no date. Then go back to work. Later:

```
/gt:gt-review
```

reads the inbox (and daily notes, if you keep them), asks where each line belongs,
files it as a task or a project, and checks the inbox line off with a `→ [[slug]]`
pointer. An idea filed as a task gets `p:: 3` and no due date — a due date on an
idea makes the deadline rule rank it above real work.

### Writing anything into the vault

```
gt_write_queue.py …   →   gt_broker.py drain
```

Since 0.17.11 every vault write a session makes goes through the write queue (Core rule 1). The
skills already do it; a direct `Write`/`Edit` to a vault note is denied, and the denial prints the
exact queue command. A write that is **held** is waiting for another session's claim, not failed;
one that is **escalated** is yours to decide, as a `#conflict` task. Your own edits in Obsidian are
untouched. See *Writing to the shared files*.

### Reaching GitHub, Jira or Microsoft 365 from a session

```
/gt-lotr:gt-lotr     (find, then read / write / consent)
```

With the `lotr` module on, a session asks the gateway in plain words — "my open PRs", "today's
calendar" — gets candidates with the tier the gateway assigned, and calls the one it needs. The
gateway, not the assistant, decides what is a write and what needs your confirmation. See
*Reaching other systems*.

### Closing a project

Delivery is not closure. A shipped project whose tasks stay open keeps escalating
through its past due dates and outranks live work; on 2026-09-05 a talk delivered
two days earlier held 24 of the top rows in the rollup.

```bash
python3 <vault>/Projects/golden-thread/tools/gt_closeout.py candidates
```

names projects whose signals say they may be finished: most open tasks past due,
most tasks checked with nothing urgent left, three quiet weeks, or nothing open. The
same probe runs at `/compact` and session end (setting `closeout_check`, from the
report-card module), and
`gt-work` asks after the last urgent task is checked. The question is always put to
you, never acted on. Every ask and every answer is recorded in
`Projects/golden-thread/closeout-signals.jsonl`; `gt_closeout.py history` shows what "ready" has
actually looked like for you, which is what the thresholds get tuned against.

When the answer is yes (0.18.1):

```
/gt:gt-close project <slug>
```

walks the project to a close. Every open task and handoff is shown and must be decided — closed,
dropped, moved to another project, or kept shelved at `p:: 7` — and until then the close stops.
Then it offers what the project learned for graduation, one finding at a time, through
`/gt:gt-promote`. Finally it **archives in place**: `stage: archived`, an *Archived* banner, the
row in `Projects/README.md` marked. Nothing moves and every link still resolves; ask for `--move`
to relocate the folder to `Archive/<slug>/`. See [Closing things](#closing-things-gtgt-close).

---

## Setup

### `/gt:gt-init`

First run, or wiring a new machine. Writes `~/.claude/vault-config.json` (the
pointer every other skill reads), scaffolds the vault, and adds a Golden Thread
section to `~/.claude/CLAUDE.md`. Also installs Core-rule enforcement hooks into
`~/.claude/settings.json` so that Core rules are actively asserted, not merely
stored. Idempotent.

Since 0.16.2 that is **six registrations across five scripts** — the two counts differ, because
one script is wired to two events:

| Event | Script |
|---|---|
| `UserPromptSubmit` | `inject_core_rules.sh` — re-asserts the Core rules on every turn |
| `SessionStart`, matcher `compact` | `inject_core_rules.sh` — **new in 0.16.2**: re-asserts them after a compaction |
| `Stop` | `validate_response.sh` |
| `PreToolUse` | `guard_session_claims.sh` |
| `PreToolUse` | `guard_vault_writes.sh` |
| `PreToolUse` | `guard_test_before_commit.sh` |

**Why `inject_core_rules.sh` is wired twice.** Project-root `CLAUDE.md` and auto memory are
re-injected from disk when a conversation is compacted; context that hooks added earlier is
*summarised* with the rest of it. gt's Core rules travel by hook, so before 0.16.2 they survived
a compaction only as whatever the summariser chose to keep. Say the exposure precisely rather
than overselling the fix: `UserPromptSubmit` has no compaction exception, so the next user prompt
always restored the rules verbatim — what was exposed was the **remainder of the turn** in which
an auto-compaction fired. The mechanical tier was never affected: the `PreToolUse` guards and the
`Stop` validator are event-driven commands, not context, so compaction cannot weaken them. What
lapsed was the re-assertion, not the backstop. The script reads which event fired from its own
payload rather than naming one in its output.

For a vault that predates the Core-rule tier, run the install step separately:

```bash
python3 $SCRIPTS/vault_init.py install-core-rules --vault ~/my-vault
```

> `CLAUDE.md` is read at **session start**. A rule added mid-session does not
> apply until you restart — that looks like the rule being ignored.

#### What an install changes inside your vault

`install.sh` refreshes a few files in the vault on every run. Since 0.15.0 each is decided
by **content**, and every change is named in the install output:

| What | Rule |
|---|---|
| Vault tools (`Projects/golden-thread/tools/*.py`) | Replaced only when your copy is text gt shipped (listed in `templates/shipped-hashes.json`). Anything else is **kept**: `Vault tool MODIFIED LOCALLY → … kept`, with the template to diff against. |
| Git hooks (`.githooks/*`) | Same rule as vault tools: a copy gt shipped is updated; a hook matching no shipped version (your edit, committed or not) is kept and reported as modified locally. |
| `core.hooksPath` | Set to `.githooks` only when unset. If you set your own, it is left alone and the install says how to call the attribution hooks from yours. |
| Core rules and CLAUDE.md's "First: is enforcement active?" section | A missing rule is added back and named (`Added Core rule → …`); so is the section. |
| `log.md`, each `decisions.md` | Converted once to generated files (the 0.11.0 spool migration) when the vault was clean before the install; each converted file is named, with any note (such as trailing blank lines dropped). |

Before the first of these writes, the files involved are backed up to
`~/.claude/golden-thread/backups/install-vault-files-<stamp>.tar.gz` (with the previous
`core.hooksPath`). The backup is deleted again when the install changed none of them.

**Keeping something removed.** A rule or section you delete on purpose comes back on the
next install unless you list it in `.gt-removed`, beside `core-rules/` (by default
`Projects/golden-thread/.gt-removed`). One entry per line, `#` for comments:

```
core_parallel_when_beneficial.md     # a Core rule file name
claude-md-enforcement-section        # CLAUDE.md's "is enforcement active?" section
```

Listed items are never re-created (`Left removed → …`). gt never writes this file. Removing
a rule file does not switch off a hook that enforces it (`enforcement: validated`); the
install warns when that is the case — use `gt_settings.py` for that.

### `/gt:gt-create`

The create verb (0.18.1): `/gt:gt-create project <slug>` (or a bare slug, as before),
`/gt:gt-create task <text>` and `/gt:gt-create handoff`. The task and handoff forms are described
under [Tasks and handoffs](#tasks-and-handoffs).

A project: gathers slug, title, tags, **domain**, sub-project parent, runbook, and **topology**;
runs the script; then fills `idea.md` from what you actually said. `idea.md` is immutable
afterwards — it is the traceable "why".

**Sub-projects (0.18.1).** `--parent` resolves a sub-project's own slug to its folder, so
`--parent <a sub-project>` nests under it rather than creating a new top-level folder; a parent
that does not exist yet is still created at `Projects/<parent>/`. The generated `CLAUDE.md` now
names the sub-project's real folder (`Projects/<parent>/<slug>/`); one generated before 0.18.1
said `Projects/<slug>/`, which does not exist — fix its *Deeper context* line by hand.

**Naming a project on the command line (0.18.1).** Anywhere a tool takes a project — `--project`,
`--slug`, `--to`, a task ID — you may give a sub-project's bare slug (`child`) or its path
(`parent/child`). The tool finds the one project folder with that name or README `slug:`. No match:
it stops and says so, and nothing is created. Two matches (`parent/dup` and `other/dup`): it stops
and lists both; pass the path. Task IDs carry the resolved path (`parent/child:12:ab12cd`). Until
0.18.1 most tools joined `Projects/` with the slug, so they missed sub-projects and some created a
stray top-level `Projects/<slug>/memory/` or `handoff/`; if your vault has one, move its contents
into `Projects/<parent>/<slug>/` and delete it.

### `/gt:gt-upgrade`

`install.sh` updates the **plugin**. This updates the **vault** — the migrations, the documents
you also edit, and the Core rules a release added since your vault was seeded.

```bash
python3 $SCRIPTS/gt_upgrade.py status --vault "<vault>"            # what is pending, and why
python3 $SCRIPTS/gt_upgrade.py run --vault "<vault>" --dry-run     # rehearse; writes nothing
python3 $SCRIPTS/gt_upgrade.py run --vault "<vault>"               # apply, after a backup
```

**Say what is pending before anything else, then rehearse.** `--dry-run` writes no files, no
backup and no stamp. It exists because on 2026-09-11 a rehearsal run by hand wrote the live
vault instead of the copy.

**The stamp is what makes any of this possible.** A vault records the release its files came
from in `Projects/golden-thread/.vault-version.json`. A vault with no stamp is treated as
**old**, not current: every migration is offered, and each detects its own work, so a step that
already happened is a no-op rather than a duplication.

| Since | Step | What it does |
|---|---|---|
| 0.11.0 | `log-spool` | `log.md` becomes generated from per-session spool files |
| 0.11.0 | `decisions-spool` | each project's `decisions.md` becomes generated |
| 0.12.0 | `core-rules` | adds the Core rules shipped since this vault was seeded |
| 0.12.0 | `doc-merge` | `PROTOCOL.md` and `CONVENTIONS.md` take the release's changes |

**The merge base lives in the vault**, at `Projects/golden-thread/.templates/` — the exact
template text the vault was last seeded or upgraded from. Only the current and previous release
directories stay on disk, so a base kept in the plugin would vanish exactly when an old vault
needed it. Without a base, a three-way merge degenerates into *overwrite* or *leave alone*, both
wrong, so gt waits for a person instead.

**`run` refuses a dirty git tree.** The upgrade should be one `git checkout` away from undone,
which it is not if it lands on top of someone's uncommitted work. `--allow-dirty` exists and is
not the default answer. `run` backs the vault up to `~/.claude/golden-thread/backups/` first,
applies each step, stamps the vault, and writes a line to `log.md` through `gt_log.py`.

Exit `1` means a step needed an owner. Those lines print from `status` **and** `run` on every
call until they are dealt with, and `install.sh` prints them too without ever acting on them:

| What you see | What it means |
|---|---|
| `REFUSED: … duplicate ADR number(s)` | two decisions share a number. Renumbering breaks every inbound reference, so the owner picks — never unasked |
| `CONFLICT` on a document | the merged text with markers is written beside it as `<doc>.merge-conflict`; the document itself is untouched |
| `needs a person: no merge base` | gt never recorded a base, so it cannot tell your edits from the release's changes. Review against the template, then `run --record-base <doc>` |
| `needs a person: merge held` | a clean merge would remove lines you still have. Keep yours with `--record-base <doc>`, or take the merge with `--accept-merge <doc>` |
| `needs a person: conflict awaiting you` | a conflict gt already wrote for this exact base, template and document. It is not redone — no merge, no stamp, no rewritten conflict file — until something changes |

A refusal stops **that step, not the upgrade**: everything else still runs, the refusal is
reported, and re-running later picks up where it stopped. `--record-base` records the *shipped
template* as the base, meaning "reviewed against this release", so later releases merge only
their own changes, unattended — and only this command records a base; the installer never does.

It will not renumber anything, guess at a conflict, apply a line-removing merge unasked, redo a
conflict it has already written, or overwrite a Core rule that already exists — new rules are
added, existing ones left alone.

---

## Writing to the shared files

### The write queue — Core rule 1, queue first (0.17.11)

More than one session, and more than one agent, writes this vault at once. Since 0.17.11 they all
go in one way: **write vault content only through the write queue (`gt_write_queue.py`), then apply
it with `gt_broker.py drain`; never edit a vault file directly, and never write one another live
session has claimed.** The rule id is still `core_concurrent_session_claim`; its meaning changed
(owner, 2026-10-01: *"the agents write to the queues rather than directly to the files"*).

Why the change: "claim, then write" depended on every writer remembering to claim and on the
write depending on the claim. On 2026-10-01 it failed twice in one hour — a Bash append went
through a live claim, and a refused claim printed CONFLICT but the next command was not chained to
it and wrote anyway. A queued request never touches its target, so the safe path is the only path.

```bash
H=~/.claude/golden-thread/hooks
python3 $H/gt_write_queue.py --vault <vault> --path <rel .md> \
    --op append|replace-section|create|set-property|replace-file \
    [--section "<heading>"] [--key K --value V] --content-file <file> --session <id>
python3 $H/gt_broker.py drain --vault <vault>                  # apply, oldest first, then exit
python3 $H/gt_broker.py status --vault <vault>                 # how many are waiting
python3 $H/gt_broker.py audit --vault <vault> [--since 24]     # .md files the broker did not write
```

- **Five operations.** `append`, `replace-section` and `create` (0.17.10), plus `set-property` —
  one top-level frontmatter key — and `replace-file` — the whole file, guarded by the hash of what
  was read (0.17.11). There is no delete or move: `gt_demote.py`, and demoting a Core rule out of
  `core-rules/`, still do that directly.
- **Held means waiting, not failed.** A write to a file another live session has claimed stays
  queued; the next drain applies it, and session start shows "WRITE QUEUE: N waiting". Claims
  still exist — they are what the broker honours.
- **Escalated means the owner decides.** Conflicting replacements, a target that changed after the
  request was queued, a target moved or deleted since (the broker never recreates a file at its
  old path), and **every** write to a `design.md` or `global-memory/` write nothing: each version is
  kept in `spool/broker/conflicts/` and a `#conflict` task points at it. A review target is
  decided by **identity, not spelling** (0.20.1): a file named `design.md` in any case, Unicode
  form (U+017F long s, fullwidth letters), with trailing dots, spaces, zero-width characters, an
  NTFS `::$DATA` suffix or a Windows 8.3 short name (`DESIGN~1.MD`); anything under the vault-root
  `global-memory/` by any of those spellings; and anything that resolves to either through a
  symlink or the same inode (`os.path.samefile`). A symlink in the path that dangles, or that
  leaves the directory it sits in, is escalated too, since where it lands is not what the path
  says. `xdesign.md`, `design-notes.md`, `global-memory-x/` and `notes/global-memory/x.md` are
  ordinary files. The spelling rules apply on every platform: on a case-sensitive volume
  `DESIGN.md` is a different file, and is escalated all the same.
- **A claim holds the file, however it is spelled (0.20.1).** A claim is matched by identity,
  not as a string: a case or Unicode variant of a claimed path (`Research.md`, a long s), a
  trailing dot or space, a Windows 8.3 short name, or a symlink to the file is the claimed file,
  for the guard, for `gt_session.py check` / `claim` and for the broker's hold. The case folding
  is deliberate on every platform: on a case-sensitive volume (Linux) `Research.md` and
  `research.md` are two files, and a claim on one holds the other all the same — a write the
  holder did not expect waits one drain, which is cheap; a write through a variant is not.
- **What the guard denies.** `guard_session_claims` denies a direct `Write`/`Edit` to any vault
  `.md` outside `Sources/`, `core-rules/`, `.obsidian/`, `.git/`, `.gt/` and gt's own `spool/`,
  `sessions/` and `tools/` — claimed or not — and the visible shell writes: `>`, `>>`, `tee`,
  `sed -i`, and `cp`/`mv` into the vault. The denial carries the exact queue command.
- **What it does not see.** A script that opens a file itself; your own edits in Obsidian (no hook
  sees them, and the broker never overwrites them — a queued write to a file you changed is
  escalated); files outside the vault. That part of the rule is a reminder, not validated.
- **What routes through it.** gt-work and fifteen other core skills, the module skills, and the
  scripts `gt_daily`, `gt_task`, `gt_lint_weekly`, `gt_handoff`, `gt_handoff_status` and
  `wiki_log`. `gt_write_queue.py`, `gt_broker.py` and `gt_demote.py` are installed into the hooks
  dir so scheduled jobs and the vault's tools can reach them; without them the 22:00 daily-note
  job exits 3 with "write queue is not installed".
- **`audit` is a report, not an alarm.** It lists vault `.md` files changed in the window that the
  broker did not write; your Obsidian edits appear there too.
- **One known gap: two machines, one Dropbox-synced queue.** The drain lock is a file in the vault,
  and Dropbox does not make it atomic across machines. Drain from one machine.

The generated files below keep their own tools — the queue refuses them.

### Generated files: `log.md`, `decisions.md`, `TASKS.md`

`log.md`, `decisions.md` and `TASKS.md` are **generated**. They are the files every
session writes and none owns, and in one working tree that means last-writer-wins with
no conflict marker to warn you. So none of them is written directly.

```bash
T=<vault>/Projects/golden-thread/tools

# A log entry. Writes only this session's spool file, then re-renders log.md.
python3 $T/gt_log.py --vault <vault> add "2026-01-01 10:00 CST [work] my-project — what happened"

# An ADR. Reserve the number FIRST -- two sessions that both read "the highest is 5"
# will both write ADR-6. This takes the number in a single atomic step and prints it.
python3 $T/gt_adr.py --vault <vault> allocate my-project --title "The choice"
#   -> 7        (and creates the file that holds ADR-7; write the body into it)
python3 $T/gt_adr.py --vault <vault> merge my-project

# Who has spooled what, and any allocation left unfinished
python3 $T/gt_log.py status
python3 $T/gt_adr.py status my-project
```

Adopting this in an existing vault is one command per file. It freezes what is there as
a baseline that sorts first, so no history is rewritten and no ADR is renumbered, and it
refuses unless the merge reproduces the original byte for byte:

```bash
python3 $T/gt_log.py --vault <vault> migrate
python3 $T/gt_adr.py --vault <vault> migrate my-project
```

`migrate` refuses on a project whose ADR numbers already collide, because renumbering
would invalidate every existing reference to them. `gt-lint`'s `adr-collision` reports
those so you can decide. A deliberate `## ADR-6 amendment:` is not a collision and is
left alone.

**Movement events.** `gt_events.py` records *what moved where* as structured JSON lines —
the data `/gt-flow:gt-flow` draws. Same spool pattern as the log: one file per session
under `spool/events/`, merged into `events.jsonl`. The tool and schema (v1) shipped in
0.13.0; **since 0.15.0 operations record events as they happen**:

- `gt_log.py add "<line>" --event <kind> --item <path> [--from --to --level-from --level-to
  --project]` spools the log line and one event in one command. `/gt:gt-promote` and
  `/gt:gt-refresh` record their moves this way; `/gt:gt-review`, `/gt:gt-work` and
  `/gt:gt-ingest` emit theirs with `gt_events.py emit`.
- `gt_adr.py allocate` emits an `adr` event for each number it reserves.
- `vault_init.py` emits `create`, `rename`, `merge` and `archive` when `create-project`,
  `rename-project`, `merge-project` and `archive-project` actually do something (never under
  `--dry-run`). `archive-project` is the only source of `archive`: `gt_closeout.py answer
  <slug> yes` records your decision to close, not the move, and emits no event.
- `gt_tasks.py` emits `task.open` / `task.done` for checkbox lines that changed — once the
  stream has been seeded with task events by a backfill. A task event is always stamped after
  the latest event for the same task (0.20.1), so a flip made in the same second as the event
  it follows still sorts after it.

An event can never fail the operation it describes: a failure is one line on stderr.

A vault older than the events has none. `backfill` rebuilds the history from git and
`log.md` — Knowledge and global-memory additions, renames between levels, project renames,
README checkbox changes — as `actor: backfill` events. Preview it first; a second run adds
nothing. (A dry run on the author's vault found about 1,430 events.)

```bash
python3 $T/gt_log.py --vault <vault> add "2026-01-01 [graduate] quote_api → Knowledge" --event promote \
    --item Knowledge/Quote-API.md --from Projects/ats/memory/quote_api.md \
    --to Knowledge/Quote-API.md --level-from 2 --level-to 4 --project ats
python3 $T/gt_events.py --vault <vault> backfill --dry-run   # what history would be added
python3 $T/gt_events.py --vault <vault> validate          # report bad lines, exit 1 if any
python3 $T/gt_events.py --vault <vault> list --kind promote --json
```

Every event is checked before it is written: vault-relative paths only, a closed list of
kinds, levels 1–5, a note of at most 120 characters, no unknown keys.

**Hand-editing a generated file is still the wrong place for it**, but it is no longer lost
(0.15.0): a line typed into `log.md` is captured into `spool/log/hand-edits-<time>.md` at the
next merge and announced — a line starting with a date sorts by it, an undated one lands after
the baseline — and `gt_adr.py merge` refuses to render over a hand-written line in
`decisions.md`, naming it, until you move it into an ADR. `gt-lint` reports the file as
`generated-hand-edited`.

Both commands sit on one shared primitive, `gt_spool.py`: per-session spool files plus a
merge whose ordering uses nothing machine-specific, so two machines rendering the same
spools produce byte-identical output. Bytes that are not valid UTF-8 (an old latin-1 `é` in
`log.md` or `decisions.md`) pass through `migrate` and `merge` exactly as they were (0.15.0). You do not call it directly; it is documented here
because a merge that ordered by mtime or directory order would make the *generated* file
conflict in git, which is worse than the problem being solved.

## Daily work

### `/gt:gt-open <slug>`

Loads a project and stops. It is **read-only** — nothing is written during loading. (Since 0.18.1
`gt-open` is the open verb for every artifact: `/gt:gt-open handoff [id]` and
`/gt:gt-open task <id>` are described under [Opening one handoff or task](#opening-one-handoff-or-task-gtgt-open).)

**A catch-up brief first, after time away (0.18.1).** Before reading anything it runs
`gt_catchup.py`. When you have not opened this project on this machine for `brief_absence_days`
(default 7), or you passed `--brief`, it leads with one paragraph of at most 150 words, labelled as
a generated summary: the commits to the project since you last opened it, the newest
`research.md` entry, the oldest open `p:: 1` task and every open `waiting:: user` task. It is
assembled from git and structured fields, never a summary of prose, and the reading below still
follows in full. `--no-brief` skips it; a project with no commits in the window gets nothing.
*Last open* is machine-local (`~/.claude/golden-thread/state/last-open.json`), so opening the
project on another machine does not reset it here.

Reads, in order: `CONVENTIONS.md` and `PROTOCOL.md` (once per session), the project
`README.md` — frontmatter first, since `domain`/`stage`/`topology`/`tags` is the
fastest read of where the project stands — then `source.md` → `idea.md` →
`research.md` → `decisions.md` → `design.md` → `spec.md` → `runbook.md`, the linked
fleet page, and finally `memory/MEMORY.md` — **the index only**. Individual memory
files load on demand.

**Then the project's waiting handoffs** (0.17.2) — it runs
`gt_surface.py handoffs --project <slug>`, which prints one line per handoff of this project
that has not been handled (or whose deferral date has come), and nothing when
`handoff_surface` is `manual`. Each is named in the summary with an offer of
[`/gt:gt-handle handoff`](#gtgt-handle); **its body is not read** unless you want to
handle it now, because being told must cost no project context. A handoff exists because the
last session could *not* capture everything, so its uncaptured items are the ones no other file
holds. Until 0.17.2 this skill never looked, and a handoff reached the next session only if
someone remembered it existed.

**`source.md` leads the project docs deliberately.** It says which box serves which
role in which environment. Acting before reading it is how you edit the wrong host,
or overwrite a file that exists on three machines.

**`research.md` over ~200 lines is read by its headings**, plus the entries relevant
to the work at hand and the most recent few. It is append-only and grows without
bound; reading it whole crowds out the context the actual work needs. **Since 0.18.1 it reads
`research-digest.md` instead** whenever `gt_digest.py check` says the digest is current — one line
per entry for the newest twenty, plus any marked `[pinned]`, which carries more than a heading
does. A stale or missing digest falls back to the headings. See
[`/gt:gt-work`](#gtgt-work) for how the digest is written.

Sub-projects load only when you name one, or the status board shows it as the active
item.

Announces the `review-queue.md` count once, then summarises stage, **topology and
hosts**, next action, blockers, and what memory exists but is unloaded — the host
list so you can correct a stale entry *before* work starts rather than after.

**It also says which repo a repo-scoped command will answer for (0.18.1).** gt makes the vault
the working directory, and the vault is a git repo, so Claude Code's `/security-review` and
`/code-review`, a test runner, anything that says "the current branch" will resolve to the vault —
and find a plausible answer there. Point them at the code repository, or use
`gt_code_review.py plan <repo>`, which takes the root as an argument. `/gt:gt-doctor`'s
`repo-target` row reports the same.

### `/gt:gt-route`

The middle of a session. `gt-open` runs before the session knows what it is; `gt-work`
runs after it is over. Between them sits the part where the work actually changes shape,
and nothing served it.

Run it whenever the ground has shifted, or whenever you cannot name what you are doing.
It reads only the conversation already in context — never project files, because reading
`research.md` to decide where a note goes costs more than the note — and answers four things:

1. **What this has become.** Restated in a sentence, with the drift named explicitly if
   the session no longer resembles what it opened with. This is most of the value: the
   drift is usually felt before it is articulated.
2. **Where the output belongs** — the [Where does this fact go?](#where-does-this-fact-go)
   table, applied to the material actually in hand, naming more than one destination when
   more than one is true.
3. **Whether the work is in the right place** — project loaded, harness (a real PTY belongs
   in a terminal; vault context belongs in Claudian), and whether the model matches the
   difficulty. It reports mismatches; it changes nothing.
4. **One next action.** A skill to invoke or a file to append to — not a menu.

It **writes nothing**, with a single exception: one `INBOX.md` checkbox line, which is
what `INBOX.md` is for. Everything else is handed to the skill that owns the destination,
which does the writing under its own claim.

**"Nothing, keep going" is a valid answer** and the skill is built to give it. A check that
always finds work is a check nobody runs twice.

Use it when a session opened as one thing and became another; when a finding might belong
to a project other than the one loaded; when you are about to write something down and are
not sure which file; or when you simply feel lost. Being wrong costs one re-run.

### `/gt:gt-work`

End of session. The step people skip, and skipping it is what makes the vault decay.

| Goes to | What belongs there |
|---|---|
| `research.md` | Dated findings and gotchas. **New discoveries only.** |
| `decisions.md` | Numbered ADR: decision, context, rejected alternatives. **Stable choices only.** |
| `design.md` | Rewritten in place if architecture changed. Describes *now*. |
| `memory/*.md` | Session state, updated in place. |
| `spec.md` | Created when design is complete enough to hand off. |

Memory files carry `level` and `enforcement` in frontmatter to make a rule's
durability explicit:

```yaml
metadata:
  type: core | feedback | user | reference
  level: core | context | generic      # default: generic
  enforcement: validated | reminder    # required only for level: core
```

Default to `generic`. Do **not** set `level: core` here — Core is a promotion,
and it requires wiring an enforcement hook, which is `gt-promote`'s job.

After writing, `gt-work` actively asks whether any finding applies beyond this
project and should go to `Knowledge/`, `global-memory/`, or a new project.

Also updates the `stage:`, `topology:`, and `domain:` properties in `README.md`
when the project's reality changes.

**Not in `decisions.md`:** anything you might change next session. An ADR you
reverse next week teaches the vault to lie to you.

**Three questions when it writes an ADR (0.18.1):** does this replace an earlier decision
(`gt_adr.py allocate --supersedes N`, which `gt_adr.py lineage` and `/gt:gt-query --lineage`
follow)? Does it have a known expiry condition (`--expires-when "<condition>"` or
`--expires YYYY-MM-DD`, which gt-lint's `adr-expires` brings back for review)? And, for a new
memory note, which entities does it cover (`entities: [...]`, which `gt_entities.py` and
`/gt:gt-query --entity` look up)? Each is a question, not a gate: say none and nothing is written.

**What runs after the writes land (0.18.1).** Each of these was once a step gt-work asked you to
remember at the end of a session — the step that gets skipped:

| Step | Command | Setting | What it does |
|---|---|---|---|
| Research digest | `gt_digest.py write --vault V --project S` | — | Rewrites `research-digest.md`: one line per entry (heading plus its first line, cut at 120 characters) for the newest 20 `## ` sections, newest first, and up to 5 whose heading carries `[pinned]`. Through the queue; indexed in `memory/MEMORY.md` once; `research.md` itself is never touched. Its frontmatter records the hash of the `research.md` it was built from, which is how `/gt:gt-open` knows whether to trust it |
| Contradictions | `gt_memory_check.py --vault V --project S --work` | `memory_contradiction_check` (`on`) | Compares only the notes written or changed this session with their close neighbours: the same `key:` with a different value, or one subject said with opposite polarity ("disabled" against "enabled"). Each pair: both paths, both sentences, whether either links the other, and "same fact? which is current?". Nothing is modified |
| Promotion candidates | `gt_promote_detect.py --vault V --project S --work` | `promotion_candidates` (`on`), `promotion_overlap` (`80`) | A memory note changed in 3 of the last 5 commits to its project's `memory/` (→ `research.md`); `research.md` sections in two projects whose words overlap by `promotion_overlap`% of the smaller (→ `Knowledge/`); and gt-lint's `global-scope-leak` (→ demote). Each with a one-line reason; nothing is promoted without your yes |

To pin a finding so the digest always carries it, add `[pinned]` to its `## ` heading in
`research.md` — the one edit to an existing entry this allows.

**The skeptic pass (`skeptic_pass`, off by default).** With it on, gt-work spawns a zero-context
skeptic over the research entries about to land, before they land. Since 0.18.1 it needs nothing
else: in 0.17.x it ran only when `agent_specialization` was also on, so turning the skeptic on also
handed ingest and validation to specialist agents.

| `skeptic_pass` | `agent_specialization` | gt-work skeptic | ingest / validate |
|---|---|---|---|
| off | off | none | inline |
| on | off | spawn | inline |
| off | on | none | specialist |
| on | on | spawn | specialist |

**Learn: patterns worth keeping (0.18.1).** Last, gt-work looks back over the session for patterns
applied more than once, decisions made but not written down, and techniques worth reusing. It
skips anything already captured and offers the rest one at a time, each routed to an ADR, a
runbook step, a memory note or `/gt:gt-promote` — through the queue, never a direct edit. (A
separate `gt-learn` skill was proposed; the owner folded it in here so findings have one path.)

**Link suggestions after a Knowledge write (0.18.1).** CONVENTIONS calls cross-domain links the
most valuable and the most often missed, and a session that writes a Knowledge page rarely knows
which pages elsewhere should link to it. After gt-work writes one, it lists up to five pages that
probably should — cross-domain first, then by shared tags (3 points each) and shared title words
(1 each) — each with its reason and a link type (cross-domain, hub, upstream, downstream, sibling).
Pick by number; each pick adds `- [[Target]]` to this page's `## Related` and `- [[This page]]` to
the target's, through the queue. Skip, and nothing is written. Only the frontmatter of the other
pages is read. A page's domain is its `domain:`, else the `index.md` heading it is listed under,
else its first tag.

```bash
python3 $SCRIPTS/gt_link_suggest.py suggest --vault V --page Knowledge/P.md [--limit 5] [--json]
python3 $SCRIPTS/gt_link_suggest.py apply   --vault V --page Knowledge/P.md --to TARGET [...] [--forward-only] [--dry-run] [--json]
```

Exit `0` ok, including no candidates · `1` a write was refused · `2` usage. Matching is lexical —
no synonyms — and `/gt:gt-promote` does not run the pass yet.

**It offers a handoff for what did not reach a file (0.17.1).** Classifying findings tells
`gt-work` not only what to write but what it is *not* writing — a decision that needs you,
something mid-flight, a question raised and unanswered. Those live in the conversation, and the
conversation is about to end. So it names them and asks whether to write a
[handoff](#gtgt-create-handoff); on yes it runs `gt_handoff.py` and writes the narrative itself,
because the script deliberately will not.

**On yes it also raises the work**, one task per unresolved item in the project's `## Tasks`,
each naming its own item and citing the handoff:

```markdown
- [ ] Decide whether the sweep should block on cannot-run — context in handoff-2026-09-28.md [p:: 1] [waiting:: user] [since:: 2026-09-28]
```

A handoff nobody is told to read is write-only, so the task is the mechanism and the file is
only the context. **`p:: 1`, never `p:: 0`** — there is no `p:: 0`; `0` exists at project level
only, where it is defined as a tier nothing occupies permanently. `since::` is what makes it
escalate: the vault's stale-P1 escalation rule raises the project a
level once the task is seven days old, so an unread handoff climbs on its own and a read one
costs nothing. One task per item, never a single "review the handoff" line — that competes with
real work while saying nothing, and hides every item after the first.

**The question is asked only when the list is non-empty**, and that restraint is the feature.
An offer made every session is one that is always declined — `gt_closeout` demonstrated exactly
that by asking the same question every session until the answer stopped being read. Nothing
uncaptured, no question, one line saying so. And a `no` is final: nobody writes a handoff on
your behalf after you have said those items are not worth a file.

**The next session is told, without anyone asking (0.17.2).** [`gt_surface.py`](#gt_surfacepy-what-the-last-session-left-shown-to-the-next)
runs at `SessionStart` and names every handoff not yet handled, every session, with its count of
open items. A handoff counts as **handled** once every task citing it is checked off — so **the
task must name the handoff's filename**, as in the line above. That filename is the join; a task
that does not name it is invisible to both the count and the automatic close.

### `/gt:gt-create handoff`

*Until 0.18.1 this was `/gt:gt-handoff`, which still works through 0.18.x as a deprecated alias.*

The session that *designs* something is rarely the session that builds it. This writes down what
the next one needs — and marks what it must not assume.

```bash
python3 $SCRIPTS/gt_handoff.py --vault "<vault>" --project <slug> [--repo <code-path>]
```

Writes `Projects/<slug>/handoff/<date>-handoff.md`. `--json` returns the facts without writing a
file; `--force` is required to replace an existing handoff, because a handoff is someone's
record of a session and overwriting one silently loses it. Since 0.17.2 it opens with
frontmatter — `type: handoff`, `project`, `status: open`, `created` — and `status: open` is what
keeps it in front of people until someone deals with it.

**The script does half the job on purpose.** It gathers **facts** — the project's stated goal,
its open tasks, its recent decisions, the observed state of the code repository, what is
uncommitted, what the last commits were — each carrying where it came from, so the next session
can go and check it. It deliberately does **not** write the design narrative. A script that
invents "what we decided and why" produces a document that *reads* finished and is not, and the
next session inherits false confidence instead of no confidence.

That section is yours, and four things belong in it:

- **What was decided, and what was rejected.** The rejected options are what the next session
  will otherwise re-propose and rediscover the hard way. This is the most expensive thing to lose.
- **What is built versus designed.** Be blunt: "designed, not written" and "written, not tested"
  are different states, and a file listing cannot tell them apart.
- **What to do first**, and what would make it wrong.
- **What is uncertain.** An open question written down is worth more than a confident guess.

**Verification labels are not decoration.** Every fact is labelled `verified`, `unverified` or
`unknown`, per Core rule 10, and the script never claims verification: it says what it observed
and marks the rest. If the handoff says the tests pass, say **who ran them, when, and which
ones** — "the tests pass" becomes folklore the moment it is written down in something
formal-looking. Anything you did not personally observe this session is `unverified`.

The document opens with a checklist of questions the script cannot answer — whether the tests
pass, whether the decisions were overtaken later in the session, whether anything agreed
verbally never reached a file. Answer them in the document, or say plainly that they are
unanswered. The script says so itself when it finishes: the output is **not** a finished
handoff, and one left with its placeholder intact should be reported as incomplete rather than
read as a design.

**What changed this session (0.18.1).** "What is the current state" is not "what did this
session do", and the next session should not have to diff the vault to find out. When a session
registers, `gt_session.py register` records the vault's commit as `start_commit:` in the session
file (kept when the registration is resumed or taken over). The handoff then gains
`## What Changed This Session`, from `git diff --name-status <start>..HEAD`, labelled
`self-verified`: new memory notes with their descriptions, the `##` headings appended to each
`research.md`, new ADRs, `design.md` files touched, and Knowledge pages created or updated. A
session that changed nothing says "No vault changes this session."; with no start commit the
section is left out rather than guessed. It counts **committed** changes only, and the diff is
vault-wide, so another session's commits in the same window appear too. `--since-commit <sha>`
overrides the start; `--session <id>` names the session (default `$CLAUDE_CODE_SESSION_ID`);
`--dry-run` prints the handoff and writes nothing; `--json` carries `vault_changes`.

Log the run in `log.md` with `work`, and link the handoff from the project's `README.md` so the
next session finds it without being told it exists.

Exit codes: `0` written · `2` usage (`--project` takes a slug, never a path) · `3` the project
could not be read, the target exists without `--force`, or the write would land outside the vault.

### `/gt:gt-query`

Reads `index.md`, follows wikilinks 2–3 hops into `Knowledge/`, falls back to
grep, then project memory. Be specific. If a result is `status: stale`, verify it.

**Decision lineage (0.18.1)** — `/gt:gt-query --lineage <topic>`, or "what is the history of
decisions about X?":

```bash
python3 <vault>/Projects/golden-thread/tools/gt_adr.py --vault "<vault>" lineage <project> "<topic>"
```

Every ADR in the project whose title or body mentions the topic, with its whole supersession chain
followed both ways, oldest first: number, date, title, "→ Superseded by ADR-M because: …" (M's
context), and the one still standing marked `— current` with its rejected alternatives. No match
prints `No decisions found for topic '<topic>'`. Read-only; one project per query; a sub-project's
bare slug works. **A chain is only as good as its `Supersedes` fields** — written by
`gt_adr.py allocate --supersedes N` (repeatable, and refused for a number the project has no ADR
for); a title like "(supersedes ADR-3)" also reads. An ADR that superseded another in prose only
shows up as a chain of one.

**Entity lookup (0.18.1)** — `/gt:gt-query --entity <name>`, or "what do we know about X?":

```bash
python3 $SCRIPTS/gt_entities.py --vault "<vault>" lookup <name> --project <slug>
python3 $SCRIPTS/gt_entities.py --vault "<vault>" list --project <slug>
```

Only the memory notes whose `entities:` frontmatter contains `<name>` (case-insensitive substring),
each with its `MEMORY.md` description and full text — otherwise
`No memory files tagged with entity '<name>'`, and the ordinary search carries on. `list` shows
every entity the project's notes declare. gt never writes entity tags: you add them, and gt-work
asks which entities a new note covers. Read-only; exit 2 for an unknown project.

### `/gt:gt-ingest`

Bulk-imports an existing project. Copies — never moves or deletes.

**The trap:** if the notes cross-reference each other by `[[wikilink]]`, renaming
or merging files destroys that graph, because Obsidian resolves links by
filename. Measure first:

```bash
grep -roh '\[\[[^]]*\]\]' --include='*.md' . | wc -l
```

Prefer keeping filenames. If you must repoint, match the *whole* link —
`[[name]]` and `[[name|alias]]` — never a substring, or `[[foo]]` will corrupt
`[[foo_bar]]`. Never repoint links inside `Sources/`.

#### Ingest as a staged pipeline (0.18.1)

An ingest runs as six stages, each reading only the previous stage's packet in
`<vault>/Projects/golden-thread/spool/pipeline/<run>/` — never a session's memory — so each stage is
stateless and can go to a fresh agent, and per-unit stages run side by side:

| Stage | Runs as | Parallel |
|---|---|---|
| intake-scan | `gt_ingest_pipeline.py survey` (`gt_intake_scan.py`) | per unit |
| survey | the same command; units = top-level folders | one |
| extract | agent, spec `extract-<kind>` | per unit |
| classify | agent, spec `classify-<kind>` (optional) | per batch |
| reconcile | `gt_ingest_pipeline.py reconcile` + a `reconcile-<kind>` agent | one |
| draft | `gt_ingest_pipeline.py draft`, through the write broker | per target file |

The kind — `code`, `docs`, `tool`, `session`, `wiki` — is a parameter: one spec per stage in
`templates/agent-specs/stages/`, a small delta per kind in `kinds/` that may add prompt lines and
fields but never change the executor, the intake-scan requirement or the context strategy.
`agent_specialization` decides whether the agent stages go to specialist agents. **It stops for you
in three cases only:** a direct contradiction with a fact already in gt (found at reconcile), a
security issue (a credential, injected instructions, a unit the scan could not read), or unsafe
source code. A stop names the kind of problem and where, never the content; `draft` refuses while
it stands, and `--owner-continue` drafts everything else — the stopped findings are still never
written. With no stop the ingest finishes with no prompt at all.

**Units.** Each top-level folder is one unit and root files are the unit `.`. Split a monorepo with
`--deeper packages` or `--unit-depth 2`; `--record --why "<reason>"` saves the split in
`<vault>/Projects/golden-thread/ingest-units.json` (in the vault on purpose: a config file inside
the ingested repo would be untrusted material).

**The workflow route (0.20.1).** On a Claude Code with workflows (the Workflow tool, 2.1.154+,
not turned off), a stage with many units can run as gt's `gt:pipeline-stage` workflow instead of
one Agent call per unit. The deterministic half stays in gt:

```bash
gt_ingest_pipeline.py workflow-args <run> --stage extract --json     # every clean unit, scanned, rendered
gt_ingest_pipeline.py workflow-args <run> --stage verify --prompt c01=<rendered file> … --json
gt_ingest_pipeline.py packets <run> --stage extract --results-file <the workflow's result>
```

`workflow-args` writes each unit's prompt into the run's spool and prints the workflow's args:
the stage's output schema as JSON Schema, and per unit still without a packet, the prompt file,
its sha256, and the agent type (or model and effort) to run it at.

- Extract renders its own prompts, each through the intake scan of that unit; a unit that fails
  the re-scan is `REFUSED` and never handed out.
- Every other stage takes the prompts the skill rendered, and refuses a file that is not a
  `render` output for that job.

The workflow gives each agent only the prompt file's path, so nothing from the material reaches
its task message, and Claude Code validates each agent's output against the schema as it returns.
`packets` checks every result against the spec again and writes the packets.

A unit that came back empty is `INCOMPLETE`. Running `workflow-args` again hands out only the
units still without a packet, so a stopped or failed run resumes. Inside one session, the
workflow's own resume also works. A workflow cannot ask you anything, so every stop is handled
after it returns, exactly as on the per-agent route. Without the Workflow tool, that per-agent
route is the fallback and runs the same pipeline.

**A writing agent's private scratch folder (0.20.1).** The run's spool folder is per run, not
per agent, and it is inside the vault -- the wrong place for extracted raw material, and a place
sandbox mode denies every write to. So every unit of a stage whose agent **can write** gets its
own folder **outside the vault**:
`~/.gt-scratch/<run>/<stage>-<unit>/`, every level created `0700` (owner only); on Windows
`%LOCALAPPDATA%\gt-scratch\<run>\<stage>-<unit>\`, inheritance removed and only you granted
access (`icacls`). The path goes into the agent's prompt, under *Scratch folder*, as the only place
it may put intermediate files; only its JSON result comes back, through the packet, as before, and
nothing reads the folder back. Whether a stage gets one is decided from its spec's tool list: a
stage offered a tool that can write (a shell, Write, Edit or NotebookEdit -- one list,
`WRITE_TOOLS` in `gt_agent_spec.py`) gets a folder; a read-only stage gets none, and neither its
prompt nor its agent definition mentions one. Today only **verify** has such a tool (a shell);
extract, classify, reconcile, draft, generalize and place are read-only, so an ingest run never
has scratch. The folder bounds where verify may write; it adds no write tool to anyone.

```bash
gt_agent_spec.py render verify ... --scratch-run <run> --scratch-unit c01   # Agent-tool route
gt_ingest_pipeline.py workflow-args <run> --stage verify --json ...          # each item: scratch_dir
gt_ingest_pipeline.py status <run>                                           # shows it while present
gt_ingest_pipeline.py cleanup <run>                                          # an abandoned run
python3 <plugin>/scripts/gt_scratch.py check --vault <vault>                 # leaks: exit 1
```

A run's scratch is removed when the run finishes -- `draft` queued its writes, or `promote-plan`
reached you with nothing waiting -- and by `cleanup <run>` for one you abandon. `gt_scratch.py
check` (and the `scratch` row of `/gt:gt-doctor`) names any folder a finished or vanished run left
behind; it never removes anything itself. Sandbox mode allows writes to `~/.gt-scratch` (below).

**`--dry-run` counts wherever it is placed (0.20.1).** `gt_ingest_pipeline.py --dry-run draft …`
used to write: the subcommand's own default overwrote the global flag, so a preview queued and
drained a real entry. Now the flag means the same before or after the subcommand.

**Promote runs the same way** when there are several candidates: `promote-scan`, then one agent per
candidate for **verify** (zero-context; refuted or unverifiable candidates are dropped),
**generalize** and **place**, then `promote-plan`, which ends at `awaiting-owner`. No command
applies a plan: promotions stay yours. **`/gt:gt-work` is the session kind**: its drafted write-back
is scanned for credentials and injected text, split into segments, extracted, reconciled with the
same stop rules — the skeptic is now the `reconcile-session` stage, still decided by `skeptic_pass`
alone — and drafted through the broker. The 0.17.10 spec names stay as aliases for one release.

```bash
python3 $SCRIPTS/gt_ingest_pipeline.py stages
python3 $SCRIPTS/gt_ingest_pipeline.py survey <path> --kind K [--project P] [--run R] [--unit-depth N] [--deeper DIR] [--record --why W]
python3 $SCRIPTS/gt_ingest_pipeline.py packet <run> --stage S --unit U --result-file F [--replace]
python3 $SCRIPTS/gt_ingest_pipeline.py fan-in <run> --stage extract|classify
python3 $SCRIPTS/gt_ingest_pipeline.py reconcile <run> [--facts-out F]
python3 $SCRIPTS/gt_ingest_pipeline.py draft <run> [--owner-continue] [--no-drain]
python3 $SCRIPTS/gt_ingest_pipeline.py promote-scan --candidates-file F | promote-plan <run> | status <run>
```

Every subcommand takes `--vault`, `--dry-run` and `--json`. Exit `0` ok · `1` a stop for the owner
(or refused) · `2` usage · `3` incomplete — a missing unit is never a pass.

### `/gt:gt-review`

Empties `INBOX.md`: reads every unchecked line (and daily notes, if configured),
asks where each belongs, files it as a task or project, and checks the inbox line
off with a pointer. Regenerates the rollup at the end.

### `/gt:gt-sync`

*New in 0.18.1.* The vault is a git repository so that what one machine learns reaches the others,
but until now nothing in gt moved it: a session could open a project from files another machine
had already superseded, write on top of them, and leave a divergence to untangle. Run
`/gt:gt-sync pull` before starting work on a machine that has been away, and `/gt:gt-sync push`
after `/gt:gt-work`; a bare `/gt:gt-sync` shows where the vault stands.

```bash
python3 ~/.claude/golden-thread/hooks/gt_sync.py status [--vault V] [--no-fetch] [--json]
python3 ~/.claude/golden-thread/hooks/gt_sync.py pull   [--vault V] [--dry-run] [--json]
python3 ~/.claude/golden-thread/hooks/gt_sync.py push   [--vault V] [--dry-run] [--json]
python3 ~/.claude/golden-thread/hooks/gt_sync.py behind [--vault V] [--mode cached|fetch]
```

- **`status`** fetches (time-bounded), then reports commits ahead and behind, uncommitted changes
  and the age of the last fetch.
- **`pull`** fetches, then `git pull --ff-only`. **When both sides have commits it stops** and
  shows the two counts before git does anything: gt never merges or rebases the vault; reconciling
  is yours. Uncommitted edits that touch incoming files are refused by git, and gt does not stash.
- **`push`** runs the push check and refuses unless the vault is ahead of a real upstream, and
  refuses when origin has commits this machine lacks — pull first. It runs no credential scan of
  its own; the vault's pre-commit gate is that.
- **`--dry-run`** fetches nothing and names the command it would run.

Every git call names the vault (`git -C`), never prompts, and has a timeout. To be told at session
start that the vault is behind, set `sync_check`: `cached` compares with the refs on disk (no
network; the line states their age), `fetch` runs one fetch bounded to 4 seconds. The line joins
the existing push-check report. Exit `0` ok or nothing to do · `1` refused or git failed · `2`
usage, no vault, or not a git repository.

---

## Coding work

Two skills for the coding loop itself (0.18.1), needing nothing but this plugin: a plan you approve
before any code is written, then that plan carried out one phase at a time, test first. Both run in
the **code repository**; the plan file is not vault content.

### `/gt:gt-plan <task>`

1. **Restate, and confirm.** The requirement in three to six lines, then "Is that right?" — and it
   waits. A plan built on a misread requirement is the most expensive kind of wrong.
2. **Read what already decides things** — the project's `design.md`, `spec.md` and relevant ADRs,
   and the code and tests the change will touch — and **cite** each in the plan.
3. **Risks and unknowns**, each marked *verified* (read or run this session) or *unverified*.
4. **The plan**: numbered phases, each with a goal, the failing test that proves it is needed, the
   change, and a *done when*. Written to `<repo>/.claude/gt-plan-current.md` with
   `status: draft`; an existing plan for a different task is replaced only after asking.
5. **One question: "Approve this plan?"** — and no code. Only an explicit yes sets
   `status: approved`; a new question, silence or "looks interesting" is not approval. Editing an
   approved plan's phases sets it back to draft: what was approved is what runs.

### `/gt:gt-implement`

Refuses, naming `.claude/gt-plan-current.md`, while the plan is missing or not `approved`. Then,
one phase at a time:

| Step | What happens |
|---|---|
| RED | the phase's failing test is written first, and seen to fail |
| GREEN | the smallest change that makes it pass |
| REFACTOR | tidied with the tests still green |
| Docs | only if a documented interface changed |
| Checks | `gt_allin.py --only tests,scan`, naming which tests ran |

**Any failure — or any member that could not run — stops the run** and says so. It asks before each
next phase. It commits only through `/gt:gt-allin-commit` (a `--dry-run` first, then "Commit
this?") and **never pushes**. At the end of the session, `/gt:gt-work` captures what was learned
as usual.

The approval is checked by the skill reading the plan's frontmatter, not by a script, and nothing
adds `.claude/gt-plan-current.md` to `.gitignore` — decide per repository whether a plan is
something you commit.

---

## Tasks and handoffs

Two kinds of "someone has to come back to this": a **task** (one line in a project README's
`## Tasks`) and a **handoff** (a file a session wrote because it could not capture everything).
Since 0.17.2 both work the same way, and since 0.18.1 one skill per **verb** covers both, with the
artifact as its argument:

| verb | task | handoff | project |
|---|---|---|---|
| create | `/gt:gt-create task <text>` | `/gt:gt-create handoff` | `/gt:gt-create project <slug>` |
| open | `/gt:gt-open task <id>` | `/gt:gt-open handoff [id]` | `/gt:gt-open <slug>` |
| list | `/gt:gt-list tasks [filter]` | `/gt:gt-list handoffs` | — |
| handle | `/gt:gt-handle task [filter]` | `/gt:gt-handle handoff` | — |
| close | `/gt:gt-close task <id>` | `/gt:gt-close handoff [id]` | `/gt:gt-close project <slug>` |

Bare `/gt:gt-list` summarises both lists; bare `/gt:gt-handle` asks which to work through when both
have something waiting. Five rules hold for both artifacts:

- **Surfacing is the alarm; the task or handoff is the record.** Session start *tells* you what is
  waiting; the README line or the handoff file is what gets changed when you deal with it.
- **Seeing is separate from handling.** `gt-list` and `gt-open` only show; `gt-handle` and
  `gt-close` change things.
- **Nothing is deleted.** Done and dropped tasks are checked off with what settled them; a handled
  handoff keeps its file and gains a status line. Dropping needs a reason.
- **A deferral needs a date.** It hides the item until then and brings it back on its own.
  "Later, some time" is a drop, said out loud.
- **Being told costs no context.** What is shown at start is one line per item or a count — never
  a handoff's body, never a project opened.

**The old names still work through 0.18.x.** Each prints
`Note: <old> is deprecated. Use /gt:gt-<verb> <artifact> instead.` and then does exactly what the
new form does; they are removed in the release after 0.18.x.

| old (deprecated) | new |
|---|---|
| `/gt:gt-task` | `/gt:gt-create task` |
| `/gt:gt-handoff` | `/gt:gt-create handoff` |
| `/gt:gt-task-list` | `/gt:gt-list tasks` |
| `/gt:gt-handoff-list` | `/gt:gt-list handoffs` |
| `/gt:gt-task-handle` | `/gt:gt-handle task` |
| `/gt:gt-handoff-handle` | `/gt:gt-handle handoff` |

### `/gt:gt-create task`

Create a task the way a developer drops a TODO into code: say it in your own words, and the skill
writes one well-formed line into the project README's `## Tasks` through the vault tool
`gt_task.py`, so it ranks in `TASKS.md` exactly as a hand-written one would.

```bash
python3 <vault>/Projects/golden-thread/tools/gt_task.py add "<task>" --vault "<vault>" \
  (--project <slug> | --inbox) [--ref "<[[Page]] or vault path>"] [--p N] \
  [--waiting user|agent|external|parked] [--due YYYY-MM-DD] [--dry-run]
```

The skill decides what it can: the project this session is in (or asks once, offering `--inbox`
for `/gt:gt-review` to route later), `p:: 2` unless you say urgent (`1`) or someday (`3`) — never
`p:: 0`, which does not exist for tasks — and `waiting:: user` unless the work is plainly the
assistant's or someone else's. `[since::]` is stamped automatically.

**A task can be tied to what it is about** with `[ref:: …]` — a `[[wiki page]]` or a vault path
(`Sources/…`, `Projects/x/design.md`). The ref **must resolve**; the tool refuses one that points
at nothing and says what it looked for.

**One store, one parser.** Tasks stay in the README; `gt_task.py` is a writer and a reader for that
section, not a second database, and it parses with `gt_tasks.py`'s own rules. Hand-written tasks
are unchanged and work with every command here. **Core rule 1 holds** (queue-first since 0.17.11):
the tool never edits a README itself — it queues the write and drains at once, and a README another
live session has claimed leaves the write queued for the next drain (exit 1, and it says so).

### `/gt:gt-list`

**Tasks** — `/gt:gt-list tasks [filter]`. Read-only: open tasks, filtered, without loading any project's context. Most urgent first.

```bash
python3 <vault>/Projects/golden-thread/tools/gt_task.py list --vault "<vault>" [FILTER ...] [--json]
```

A task must match every filter given:

| Filter | Shows |
|---|---|
| `<slug>` | one project (sub-project as `parent/child`), or `inbox` |
| `p1` `p2` `p3` | that priority |
| `mine` | `waiting:: user` |
| `overdue` | `due::` before today |
| `stale` | `p:: 1` open for more than 7 days |
| `deferred` | only tasks deferred to a later date — hidden otherwise |
| `ref:<text>` | tasks whose `ref::` contains `<text>` |

Each line starts with an ID, `slug:LINE:HASH` — the README line number and six hex of the line's
text. Shelved tasks (`p:: 7`+) do not show unless a filter asks for them.

**Handoffs** — `/gt:gt-list handoffs`. Read-only: the handoffs still waiting — open, or whose deferral date has come — one line each
(path, project, age, open items), oldest first, across every project or one. `--all` shows every
handoff with its state. **No handoff body is read**: listing must cost no project context, which
is why listing and handling are separate verbs.

```bash
python3 $SCRIPTS/gt_handoff_status.py list --vault "<vault>" [--project <slug>] [--all] [--json]
```

**Both** — bare `/gt:gt-list` prints counts and the top of each list: how many handoffs wait (and
the oldest), how many `p:: 1` tasks wait on you (overdue, stale), and up to five of them — then
the commands for the full lists.

### `/gt:gt-handle`

**Tasks** — `/gt:gt-handle task [filter]`. The way to clear a backlog: choose a set with the same
filters (`/gt:gt-handle task stale`, `… task my-project p1`), then take the tasks one at a time. Context is loaded only when a decision
needs it — the file a `ref::` points at, or one named file, never a full `/gt:gt-open`.

| Decision | Command |
|---|---|
| **done** | `gt_close.py task <ID> --vault V [--reason "<what settled it>"]` — `gt_task.py done`, then the `TASKS.md` rollup |
| **drop** | `gt_task.py drop <ID> --vault V --reason "<why it no longer matters>"` — reason required |
| **defer** | `gt_task.py defer <ID> --vault V --until YYYY-MM-DD --reason "<why then>"` — future date and reason required |
| **move** (0.18.1) | `gt_task.py move <ID> --vault V --to <slug> --reason "<why it belongs there>"` — writes the line into the other project first, then checks this one off with `moved to <slug>`; interrupted half-way it leaves a duplicate, never a lost task |
| **keep** | nothing; move on |

Done and drop check the box and append `— done <stamp>: …` or `— dropped <stamp>: …` to the line.
Defer adds `[defer:: YYYY-MM-DD]`: the task is hidden from `list`, from the `TASKS.md` ranking
**and from escalation** until that date, then comes back on its own. **An ID is refused if its
line has changed** since it was listed — an ID from a stale list can never close the wrong task;
list again and use the new one. At the end the skill regenerates `TASKS.md`.

A dropped task reads as **closed** to everything downstream — `gt_events`, `gt_daily` — because
its box is checked; the line itself records `dropped: <reason>`.

**Handoffs** — `/gt:gt-handle handoff`. Where a waiting handoff gets dealt with. It lists what is waiting as a menu and **loads nothing
until you pick one** — then only that handoff and the `## Tasks` lines citing its filename, not a
full `/gt:gt-open`. Each open item is settled one of three ways:

- **Done** — the task is checked off with a note of what settled it; a decision goes to
  `decisions.md` or `research.md` through the usual `/gt:gt-work` path.
- **Keep** — the task stays open. It is already `p:: 1` with a `since::` date, so it escalates the
  project on its own after a week.
- **Drop** — checked off with the reason. Dropping is a decision, recorded, never a deletion.

Since 0.18.1 an item can also be **moved** — its task goes to another project
(`gt_task.py move`). Then the handoff's status is set:

```bash
# every item settled -- the same close /gt:gt-close handoff runs; refused while a citing task is open
python3 $SCRIPTS/gt_close.py handoff <handoff> --vault "<vault>" --reason "<one line>"
# not now -- the whole handoff comes back on that date
python3 $SCRIPTS/gt_handoff_status.py mark <handoff> --vault "<vault>" --status deferred --until YYYY-MM-DD --reason "<why>"
```

Bare `/gt:gt-handle` counts both lists and asks which to work through when both have something
waiting.

**Four states**, and what decides each:

| State | Meaning |
|---|---|
| `open` | written by `gt_handoff.py`, or re-opened. Shown until handled or deferred |
| `deferred` | put off **to a date** (`--until`, which must be in the future). Hidden until then; on that date it is open again. There is no deferral without a date — "later, some time" is a decision to drop it, so it is `handled` with that reason, said out loud |
| `handled` | marked so by a person, **or** every task citing the handoff's filename is checked off — the tasks are the record, so closing the last one needs no second step |
| `history` | written before 0.17.2 (no `status:`), over a week old, with no open task citing it — so an upgrade does not resurface every old handoff in the vault |

A handoff with no `status:` counts as open while it is under a week old or any open task cites
it. `mark` appends a line to the handoff's status log and refuses a file outside the vault. A
handoff is never deleted — it is someone's record of a session; its status says it is dealt
with.

### Opening one handoff or task — `/gt:gt-open`

Read-only, and narrower than opening a project (0.18.1):

- **`/gt:gt-open handoff [id]`** — with no id, a numbered menu of the waiting handoffs; with one
  (a path, a filename, or a unique part of one), that handoff. It reads **that one handoff** in
  full plus the `## Tasks` lines citing its filename — nothing else — and summarises its status,
  what it says to do first, what it says must not be assumed, and its open items. Then it offers
  `/gt:gt-handle handoff`.
- **`/gt:gt-open task <id>`** — the task's full line and every field (`p`, `waiting`, `since`,
  `due`, `defer`, `ref`), its age, and — only if it has a `ref::` — what that one file holds.
  An id whose line has changed is reported as such, with the project's current list. Then it
  offers `/gt:gt-close task <id>`, `/gt:gt-handle task`, or `/gt:gt-open <slug>`.

### Closing things — `/gt:gt-close`

`/gt:gt-close <project|task|handoff>`. With no argument it proposes the project open in this
session, and otherwise asks what to close — it never guesses.

**A project.** `gt_close.py project <slug>` lists what stands in the way: every open task in the
project and its sub-projects (deferred ones included — a deferral comes back), every handoff still
open or deferred, and how many `research.md` entries there are. **It exits 1 while anything is
undecided, and the close stops there.** Each task is decided one at a time:

| Decision | Command |
|---|---|
| **close** — it is done | `gt_close.py task <ID> --vault V --reason "<what settled it>"` |
| **drop** — it no longer matters | `gt_task.py drop <ID> --vault V --reason "<why>"` |
| **move** — it belongs to another project | `gt_task.py move <ID> --vault V --to <slug> --reason "<why>"` |
| **keep** — a record, never ranked again | `gt_task.py shelve <ID> --vault V --reason "<why kept>"` (sets `p:: 7`) |

Each handoff has its items walked as `/gt:gt-handle handoff` does, then is closed. A handoff cannot
be *kept* in a project being archived: it would surface at every session start for ever. Then the
skill offers what the project learned for graduation, **one finding at a time**, through
`/gt:gt-promote` — promote, defer or drop; skipping loses nothing, because archiving is not
deleting. Finally:

```bash
python3 $SCRIPTS/gt_close.py project <slug> --vault "<vault>" --archive --reason "<one line>" --dry-run
python3 $SCRIPTS/gt_close.py project <slug> --vault "<vault>" --archive --reason "<one line>"
```

**Archived in place** (owner decision, 2026-10-01): `vault_init.py archive-project` sets
`stage: archived` and `archived: <date>`, adds an *Archived* banner, marks the project's row in
`Projects/README.md`, and records an `archive` event. Nothing moves, every link still resolves, and
the project leaves `TASKS.md`. **`--move`**, only on request, then relocates the folder to
`Archive/<slug>/` and re-points every `Projects/<slug>/` path reference elsewhere through the write
queue (an edit made in between is escalated, never overwritten); it refuses while any live session
claims a file in the project. Files the queue does not write — `log.md`, `decisions.md`,
`Sources/`, spools — keep the old path as history. A `relocate` event is recorded. After a move the
decisions spool stays at `spool/decisions/<slug>`, so a later project reusing the slug continues its
numbering.

**A task.** `gt_close.py task <ID> --vault V [--reason R]` — `gt_task.py done`, then the
`TASKS.md` rollup, which records the `task.done` event.

**A handoff.** `gt_close.py handoff <file> --vault V --reason R` (the file relative to the vault, or
absolute) marks it `handled` through the write queue and records a `retire` event whose note starts
`handoff.close`. **It refuses while any task citing the handoff is still open.**

`gt-close` asks to close; the close-out probe (`gt_closeout.py`, see
[Closing a project](#closing-a-project)) is what suggests a project may be finished.

### What you see at session start

[`gt_surface.py`](#gt_surfacepy-what-the-last-session-left-shown-to-the-next) runs at
`SessionStart` and puts four things in front of the session. A clean start says
`nothing waiting` in one line, because silence reads the same as a hook that never ran.

| What | How often | Setting |
|---|---|---|
| **MUST DO** — rows of [`deadlines.md`](#deadlinesmd-format) overdue (🔴 `OVERDUE Nd`) or due within 14 days (🟡); later rows counted, not listed | every session, recomputed live | `surface` |
| **Handoffs** not yet dealt with — one line each: path, project, age, open items; never the body | every session **until handled or deferred to a date** | `handoff_surface`: `any` (default) · `project` (only when `/gt:gt-open` opens that project) · `manual` (only in `/gt:gt-handle handoff`) |
| **Tasks** — one line: how many `p:: 1` tasks wait on you, how many overdue, how many open over a week, and the commands to list and work them | every session while any wait | `task_surface` (`on` · `off`) |
| **State files** `gt_state.py` wrote before a compaction | once each; right after a compaction the newest one's content is handed to the model in full | `surface` |

**Why handoffs repeat.** The first 0.17.2 cut showed a handoff once and went quiet, on the
`gt_closeout` lesson that a question asked every session trains people to scroll past it. The
owner ruled the same day that the opposite failure is worse: a handoff scrolled past on a busy
morning is as good as never written. So an unhandled handoff comes back every session, and the
cost is kept to one line. `any` is the default because under `project` a handoff waits until
someone happens to open its project.

**The task line is a count, never the tasks**, and a deferred task is not counted until its date.
It is counted by the vault's own `gt_task.py`, so a vault not yet refreshed to 0.17.2 (run
`/gt:gt-upgrade`) shows no task line rather than a guess.

### `deadlines.md` format

One vault-root file with one table, one row per outstanding dated item — a credential rotation, a
decision with a deadline, a renewal:

```markdown
| item | category | due | see |
|---|---|---|---|
| Rotate the relay credentials | rotation | 2026-10-03 | [[pending-rotations]] |
```

`due` is `YYYY-MM-DD`. Ages and countdowns are derived from the date at run time, never stored, so
the block counts itself down and disappears when the last row is deleted. Adding a category is a
row, not code. The detail — a rotation's ordered steps, a decision's comparison — stays in the file
`see` points at, because a rotation's danger is its *sequence* and a one-line alarm cannot carry
that. A row that does not parse is skipped **and counted**; the rest still report. A label shaped
like a credential is **withheld**, never printed, because this output reaches both your terminal
and the model's context. `gt_surface.py must-do` prints the block on its own.

### Reminders outside a session — `gt_reminder.py`

*New in 0.18.1.* The MUST DO block reaches you only when a session starts; a credential rotation
once sat 15 days overdue that way. The reminder tool sends the same overdue and near rows of
`deadlines.md` through channels you choose. Each push channel is **off by default**:

| Channel | Setting | Needs |
|---|---|---|
| session start | `surface` (on) | nothing — the MUST DO block |
| macOS notification | `reminder_macos` | nothing (it appears as Script Editor; its notifications must be allowed) |
| SMS or Discord through your notification relay | `reminder_relay` = `sms` / `discord` | `~/.claude/golden-thread/reminder/relay.json` (URL, optional auth), mode 600, written by your secrets store |
| email (SMTP) | `reminder_email` | `~/.claude/golden-thread/reminder/email.json`, mode 600, written by your secrets store |

To set one up:

1. Read its setup: `gt_reminder.py setup macos|relay|email`.
2. Relay and email only: have your secrets store install the credentials file, mode 600. A looser
   file is refused before it is read, and no credential value ever appears in a setting, the vault,
   the log or any output.
3. Turn it on: `gt_settings.py set reminder_macos on` (or `reminder_relay sms|discord`,
   `reminder_email on`).
4. Prove it: `gt_reminder.py check <channel>` sends a **real** test message and says DELIVERED only
   on positive evidence (HTTP 2xx, SMTP accepted, `osascript` exit 0); anything else is "could not
   deliver", exit 1.
5. Schedule it: `gt_schedule.py install reminder --vault "<vault>" [--hour H --minute M]` (daily
   08:30). It refuses unless a mirror exists, a channel is on and every enabled channel is
   configured, then proves the job through launchd — which sends a real reminder if anything is due.

`reminder_days` (7) is the window; overdue items are always included. The job sends once a day
while an item is overdue — that is the nag; delete the row when the work is done.

**Why the job reads a mirror, not the vault.** macOS refuses a launchd job access to
`~/Library/CloudStorage`: it can see the vault and read nothing, which is how the weekly lint died
for three Mondays. So the job reads `~/.claude/golden-thread/reminder/deadlines.json` (mode 600),
refreshed by `gt_reminder.py mirror --vault V` (the schedule install runs it) and at session start by
`gt_surface` whenever a push channel is on or a mirror exists. Countdowns are computed from the due
dates when the reminder is sent, so an old mirror only misses rows added or deleted since; one over
7 days old says "dates as of …". Credential-shaped labels are withheld before they reach it. Full
Disk Access for python (every script would get the whole disk) and moving the vault were rejected.

```bash
python3 $SCRIPTS/gt_reminder.py status                 # each channel's setting and readiness, mirror age, job installed?
python3 $SCRIPTS/gt_reminder.py setup CHANNEL          # macos | relay | email | session
python3 $SCRIPTS/gt_reminder.py check CHANNEL [--json] # sends a real test
python3 $SCRIPTS/gt_reminder.py mirror --vault V [--dry-run]
python3 $SCRIPTS/gt_reminder.py run [--dry-run] [--json] [--check] [--scheduled]
python3 $SCRIPTS/gt_reminder.py import-tsv --vault V --tsv FILE [--only TEXT ...] [--category C] [--dry-run]
```

`import-tsv` folds an old `label<TAB>date` list into `deadlines.md` through the write queue, so there
stays one list. The relay payload is gt's own shape (`{severity, source, host, route, text, detail,
ts}`) — route on `route` — or `format: discord-webhook` posts straight to a Discord webhook. Exit
`0` ok or nothing due · `1` a channel could not deliver, or the preflight failed · `2` usage · `3` no
mirror.

## Context management

### `/gt:gt-minimize`

*New in 0.18.1.* A session left idle past its prompt-cache lifetime rebuilds its whole prefix on
the next turn, at a price set by **how large the conversation was**, not how long the break was:
measured, the median resume rewrote 432.7K tokens where a fresh session starts near 44.5K.
Compacting while the cache is warm is cheap; after it expires you pay the rebuild first. And
`/compact` silently summarises away what the vault exists to keep. `gt-minimize` makes the cut safe
enough to do — **measure, triage, prune, cut**, quickly, because the cache is a clock:

1. **Measure** (read-only, `gt_minimize.py`): the **billed context** of the last turn — a
   measurement — and the peak since the last compaction; a breakdown **labelled as an estimate**
   (conversation, tool output, tool calls, injected context), with what the transcript does not
   account for reported as *unattributed*; and the cache state — warm with N minutes left against
   the TTL the last turn was written at, or expired.
2. **Triage**: what would be lost by a cut and is worth more than the session — a platform fact to
   `Knowledge/` through `/gt:gt-promote`, a project finding through `/gt:gt-work`, another
   project's thought as one line in `INBOX.md`. Everything else is dropped, and named, so you can
   object. Nothing is written until you confirm.
3. **Prune**: the keepers are written through the write queue (Core rule 1).
4. **Cut**: "run `/compact` or `/clear` now — the cache is warm for about N more minutes". The
   skill cannot run them; they are your commands.

**It writes no in-flight note** (owner decision, 2026-10-01). Work in flight, an open question, the
next command — that is a handoff's job, and `gt-minimize` sends you to `/gt:gt-create handoff`
first. Across many sessions, rather than this one, the cost is measured by
`/gt:gt-optimize --only session`.

```bash
python3 $SCRIPTS/gt_minimize.py [--session ID | --transcript FILE | --latest] [--json]
```

It finds the transcript from `$CLAUDE_CODE_SESSION_ID`; with no way to tell which session it is in
it refuses (exit 2) rather than guess, and `--latest` guesses and says so. Exit `0` measured · `2`
usage · `4` could not run. Output never shows a session id longer than 8 characters or an absolute
path.

### `/gt-farm:gt-farm`

*Module `farm` (plugin `gt-farm`). Off for a fresh install — `bash install.sh --with farm`
adds it. A machine upgrading from a gt that shipped `/gt:gt-farm` keeps it on.*

Routes work out of this context to an external AI service when work is bulk,
mechanical, or genuinely benefits from a non-Claude second opinion.

The unit is a **work packet**: self-contained, with a strict return contract
(`FINDINGS` + `GAPS` blocks). The transport is swappable — the user pastes it
into a web UI today; a script sends it to an API later. The packet is identical
either way.

**All four gates must pass before a task leaves.** Any failure means the work
stays here.

| Gate | Question |
|---|---|
| **Stateless** | Answerable with no vault or repo state? |
| **Self-contained** | Fits in a paragraph plus a list of URLs? |
| **Checkable** | Verifiable without redoing the work? |
| **Releasable** | Every input safe to hand a third party? |

**Releasable is default-deny.** Never include: account identifiers, balances,
hostnames, internal IPs, credential locations, non-public source code, or
personal identifiers.

Key rules:
- One packet, one task. A packet asking three questions returns three half-answers.
- Supply a vocabulary block with exact model IDs, product names, and version
  numbers read from the system — never from memory.
- Results come back as `unverified`. Promote a finding only after opening its
  `source:` URL and confirming the claim.
- `GAPS` is mandatory and `GAPS: None` must be justified, not asserted.
- Cite the specific page carrying the claim, never a homepage.

Packets are saved to `<packet dir>/<YYYY-MM-DD>-<slug>.md`. The packet dir is
`farm_packet_dir` in `~/.claude/vault-config.json` if set, else
`Projects/<current project>/packets/`. A relative value is taken from the vault root and a
literal `<project>` in it becomes the current project's slug; an absolute value is used as
is. The skill names no particular AI service: which services you have is your
configuration (`farm_services` in `vault-config.json` names a table of them). Copying a
packet uses whichever clipboard tool the machine has — `pbcopy`, `wl-copy`, `xclip` or
`clip.exe` — and with none, you are given the packet file's path instead.

---

## Security: gt unlock (0.20.1)

**Off by default.** Turned on, gt's unlock authority (`gt_unlockd.py`, one per user, started on
demand) decides which processes may use LOTR connections, sealed and brokered credentials,
publish credentials and gt's own guards, after you prove you are present. `SECURITY.md` is the
full guide — setup per platform, the threat model, what each level does **not** protect, the
enterprise admin floor, recovery. This section is the reference.

> *"Unlock proves a person was present and limits what agents can do on their own. It is not
> anti-malware. If something already runs as you, it can wait for you to unlock."*

### Levels

| Level | Means | `gt_unlock.py status` says |
|---|---|---|
| off | nothing gated (default) | `unlock: off` |
| L1 | TOTP / SSO only: friction against agents and accidents — not a boundary against any program running as you, the assistant's shell included | `level L1` |
| L2 | Touch ID or Windows Hello required in every unlock; credentials sealed under that key, and **every sealed open needs your finger or PIN** (or one per `secrets_window_s`). The gate itself is still friction against a program running as you | `level L2` |
| L3 | authority as a service account, admin-owned code and policy — the agent boundary | not shipped in 0.20.1 |

### `gt_unlock.py`

| Command | Does |
|---|---|
| `enroll touchid\|hello\|totp\|sso` | Adds a factor. The first factor on a Mac or Windows machine must be the platform one (it needs your finger or PIN); every later enrolment change needs your current factors ("step-up + K"). `totp` shows a terminal QR code and the secret once, then asks for one valid code before saving |
| `unenroll FACTOR`, `recovery` | Removes a factor; prints ten new one-time recovery codes (shown once) |
| `policy show\|enable\|disable\|set FILE\|approve` | Reads or changes the policy through the authority, which asks for your factors first. `approve` re-trusts a `policy.json` edited outside gt (until then everything gated is locked) |
| `policy consent none\|platform [--window S]` | Consent-tier LOTR operations: LOTR's own confirmation (`none`), or a Touch ID / Hello approval valid for S seconds (`platform`); the prompt text is composed by the authority from the operation |
| `policy secrets-window S` | How long one Touch ID / Hello opens a sealed credential for, per process and grant: `0` (default) = every open asks; at most 900 s; never cached past it or shared between processes |
| `policy unattended add\|remove JOB SCOPE` | The allow-list for scheduled jobs: one narrow read scope (`lotr:<conn>:read`) or one credential (`secret:<ref>`) per entry; write, consent, publish and gt scopes are refused |
| `policy sso --client-id ID [--tenant T] [--pin-subject OID]` | Points Entra sign-in at your app registration |
| `unlock [--scope S] [--recovery]`, `lock` | Grants this session (or `--session PID`) after the factors; revokes every grant |
| `check --scope S [--request]` | Exit 0 allowed, 10 locked, 11 needs a fresh confirmation, 12 refused — for your own scripts (convenience only: it stops nothing on its own) |
| `run --scope S [--secret-file VAR=REF] [--env VAR=REF] -- CMD` | Runs CMD after a grant; secrets by a 600 file deleted afterwards (`--env` is weaker and says so) |
| `seal put\|list\|rm NAME`, `seal migrate REF --name N [--remove-source]` | The sealed store (`sealed:` refs); values on stdin, never argv |
| `secret get REF [--out FILE]` | One secret to a pipe or a 600 file; refuses a terminal |
| `git-credential get`, `aws-credential --ref REF` | git's credential helper and AWS `credential_process`, under `gt:publish` |
| `status`, `verify`, `audit [-n N]`, `daemon start\|stop\|status` | State and level; the self-check (PASS / FAIL / NOT-CHECKED, never "secure"); the audit log; the authority (`stop` needs a fresh factor while unlock is on; `lock` never does) |
| `register`, `revoke`, `hook session-start\|session-end` | Used by the SessionStart / SessionEnd hooks and the LOTR shim |

### What is gated

| Action | Scope | Default level |
|---|---|---|
| LOTR read / write / consent | `lotr:<conn>:read\|write\|consent` | reads open (`read_without_unlock`) to a process with a controlling terminal and a login shell above it — never an orphan, even one that opened its own terminal; a terminal that starts a non-login shell needs an unlock; writes and consent need the grant; LOTR's consent confirmation still runs |
| Editing `~/.claude/settings.json`, `vault-config.json`, `~/.claude/golden-thread/` | `gt:settings:hooks` | fresh confirmation (`guard_protected_paths`, before the `protected_paths` switch) |
| `gt_settings.py set` of `protected_paths`, `test_gate`, `foreign_checkout_guard`, `component_updates`, `commit_checks`, `addon_fixes`, `unlock`, `sandbox_mode`, `sandbox_vault_reads`, `symlink_writes_outside_vault` | `gt:settings:security` | fresh confirmation |
| Policy and enrolment changes | `gt:unlock:*` | your K factors, fresh |
| Reading a secret ref through the broker | `gt:secrets` | the grant, held by the **requesting** process (under `mcp_only`: the LOTR shim, or gt-lotr's installed `lotrd.py` for it — never the assistant's shell); a `sealed:` ref also needs a fresh platform factor per open |
| Publishing credentials (git helper, AWS) | `gt:publish` | the grant |
| LOTR hub client enrol / revoke | `gt:hub:enroll` | fresh confirmation |
| Opening a locked vault file | `gt:lock:<folder>` | the grant |
| A read through gt's vault MCP server ([gt sandbox mode](#security-gt-sandbox-mode-0201)) | `gt:vault:read` | open (`read_without_unlock`), and served under `mcp_only` only to the session's registered `gt_vault_mcp.py` — never the assistant's shell or LOTR's shim |
| A write or operation through gt's vault MCP server (`vault_queue_write`, `vault_queue_drain`, `vault_task`, `vault_log_add`, … ; 0.20.2) | `gt:vault:write` | the grant (never opened by `read_without_unlock`); the vault seat only |
| Ordinary vault work, lint, daily notes, model settings | — | never gated |

With unlock on and the authority unreachable, these **fail closed** — the one deliberate
exception to gt's fail-open hooks. With unlock off nothing here runs beyond two `stat()` calls.

### Factors, grants, files

Factors: `totp` (RFC 6238; each code once; lockouts 1/5/15 min; typed into a dialog the
authority raises), `touchid` (`gt-presence`, Secure Enclave, ES256 checked in Python),
`hello` (`gt_unlock_hello.ps1`, TPM, RS256 checked in Python), `sso` (Entra ID, PKCE +
loopback, ID token checked in Python), `recovery` (one code = one factor, forces re-enrolment).
Defaults when on: 2 factors including Touch ID (macOS) / Hello (Windows); 1 (TOTP) on Linux.
A grant is held in memory, bound to the session's `claude` process and to one seat — the
session's vault MCP server has its own grant, apart from LOTR's shim and the shell (LOTR and
broker scopes only to its registered MCP shim: `door: mcp_only`) — and ends after 15 min idle, 8 h, the shim
or session ending, screen lock, sleep, clock rollback, `lock` or an authority restart. The shim
seat is taken only by the installed gt-lotr `lotr_mcp.py` (real path, under `python -I`) started by `claude`; a
replacement shim starts with no grant; after a restart the shim registers again. Every client
checks that the process serving the authority's address runs the installed `gt_unlockd.py`
(kernel peer identity) under an isolated interpreter (`-I`, or `-E` and `-s`) and refuses
anything else (`server_unverified`). A "terminal" caller gets reads without unlock only with a
controlling terminal and a login shell above it (macOS / Linux) or an intact parent chain to a
shell or terminal host (Windows); an orphan never does. What all of this does **not** stop
— a program running as you that sets out to get around it — is listed in `SECURITY.md` §4.

State lives in `~/.claude/golden-thread/unlock/` (700): `policy.json`, `enrolment.json` (public
keys only), `state.json` (TOTP replay/lockout, policy approval — on macOS / Windows a Touch ID /
Hello signature over the policy, a serial and its time, verified on every load; on TOTP-only
machines a hash, L1), `approval-floor.json` (the highest approval serial accepted: an older
approval restored is refused),
`totp.seed` (readable by any program running as you — at L1 that is the whole unlock),
`recovery.json` (hashes), `sealed/`, `audit.jsonl`, and the `unlock-on` marker (a second copy,
`~/.claude/golden-thread/.unlock-unlock-on`, sits beside the directory: while either exists,
deleted policy files read as "on, locked", never "off"). The administrator floor: `/Library/Application Support/gt/
unlock-policy.json`, `%ProgramData%\gt\unlock-policy.json` or `HKLM\SOFTWARE\Policies\gt
\UnlockPolicy`, `/etc/gt/unlock-policy.json` — it only tightens, and an unverifiable one
locks everything.

### Folder locks: `gt_lock.py`

`gt_lock.py add|open|restore|status --vault V` locks chosen vault files with age (`.age` +
a `.gt-locked` note), opens them to a pipe under `gt:lock:<folder>`, and reports each lock's
real level (L1 with a plaintext age identity, L2 with a hardware one). `core-rules/` and
`global-memory/` are refused in code. Lint, tasks, recall and vault hints treat a locked folder
as absent and say so once.

### Helpers

`gt_unlock_touchid.py build --dest DIR` compiles the Touch ID helper (`install.sh` runs it on
macOS when the Xcode Command Line Tools are present) and records the built binary's sha256 in
`gt-presence.install.json`; `record --helper P [--release]` records a binary installed another
way (`--release` requires gt's Developer ID signature), `verify --helper P` checks it. Every
use re-checks the record; a mismatch makes Touch ID unavailable. `gt_unlock_hello.py available|selftest`
checks Windows Hello at the console. Doctor rows `unlock` and `security` report the state and
run `verify`.

## Security: gt sandbox mode (0.20.1)

**Off by default.** `gt_settings.py set sandbox_mode on` makes gt configure Claude Code so that
the assistant's own shell and file tools are fenced off the vault and gt's state, and the vault
is reached only through gt: a read-only MCP server and the write queue. `SECURITY.md` §8 is the
full account — every key written, the per-platform matrix, what it does not stop. This is the
reference.

**A preview in 0.20.1.** The fence holds; the skills have not all moved inside it yet.
`/gt:gt-open`, `/gt:gt-query` and `/gt:gt-work`'s writes use the gt-vault MCP tools, and
`gt_write_queue.py` leaves its request in the inbox. The other skills' shell steps —
`gt_log.py add`, `gt_tasks.py`, `gt_task.py`, `gt_adr.py allocate`, `gt_closeout.py`,
`gt_lint.py`, `gt_promote_detect.py`, `vault_init.py`, `gt_broker.py drain` — are refused, and
each prints **one line**: what was refused, why, and the next step (the MCP tool, such as
`vault_queue_drain`, or the exact command to run in a terminal, with real paths). No traceback,
no Full Disk Access advice (that is for a different refusal), nothing half-written. Each skill
carries a note saying what to use instead. Full skill support is planned for 0.20.2.

| Platform | What is enforced |
|---|---|
| macOS | Claude Code's sandbox (Seatbelt, OS-enforced) around Bash / PowerShell / Monitor and every child, plus permission rules on the file tools |
| Linux, WSL2 | The same with bubblewrap; needs `bubblewrap` and `socat` that **run** (gt executes `bwrap --ro-bind / / true` and `socat -V`, 0.20.1) — gt refuses to switch on otherwise, before gt unlock asks for a code and with the fix (package command, or the AppArmor setting behind “setting up uid map: Permission denied”), because `failIfUnavailable` would stop Claude Code starting |
| Native Windows | **Permission rules on the file tools only** — no Claude Code sandbox exists there. Friction, not a boundary |

### `gt_sandbox.py`

`gt_settings.py set sandbox_mode on|off` calls it; it is also usable directly (from a terminal —
inside the sandbox `~/.claude` is write-protected):

| Command | Does | Exit |
|---|---|---|
| `status [--json]` | on/off, the platform and what it enforces there, the vault-read setting (while off it says reads are allowed), whether the MCP tools are offered and, if not, which setting decides, the inbox path, the drift state | `0` |
| `plan [--json]` | the exact keys and entries it would write; writes nothing | `0` · `1` no vault |
| `apply [--force]` | **only while `sandbox_mode` is on** (0.20.1: run by itself with the setting off it is refused with the `gt_settings.py set sandbox_mode on` command — the setting is the one way on, and it asks gt unlock). Merges the entries into `~/.claude/settings.json` (backup first, atomic write), records what it added in `~/.claude/golden-thread/sandbox/state.json`, takes out entries it added earlier that are no longer wanted (a moved vault, an unlocked folder) — the repair for drift. `--force` writes them on a Linux machine whose bubblewrap or socat cannot run, **without** `failIfUnavailable` | `1` refused |
| `remove` | takes out exactly what it added; restores each boolean it changed unless you changed it since | `0` |
| `check [--json]` | drift: anything missing, stale, or overridden — by managed settings, or by any key that loosens the sandbox in your user settings or the current project's `.claude/settings.json` / `.claude/settings.local.json` (working directory and git root): `enabled false`, `allowUnsandboxedCommands true`, `excludedCommands`, `ignoreViolations`, Unix-socket / Mach-lookup entries, the `enableWeaker…` switches, `additionalDirectories`, an allow entry re-opening a denied region. It reports; only managed settings or `claude --settings` make the sandbox admin-required, so that Claude Code ignores a project's loosening keys | `1` drift |
| `verify [--json]` | the PASS / FAIL / NOT-CHECKED rows `gt_unlock.py verify` shows | `1` a FAIL |
| `rollback-check --target DIR [--json]` | may the gt release in `DIR` be installed here? Refused (with the commands that switch them off) while sandbox mode or gt unlock is on, or gt's sandbox entries remain, and the release has no `gt_sandbox.py` / `gt_unlock.py` to manage them. `install.sh` runs it before a rollback writes anything | `1` refused |
| `managed [--out FILE] [--json]` | prints the admin-required configuration (gt's plan plus `allowUnsandboxedCommands false`, the weakening switches off, `disableBypassPermissionsMode "disable"`) for `managed-settings.json`, and the `claude --settings <file>` command; `--out` writes only the file you name, never `settings.json` | `0` |

What it writes: `sandbox.enabled true`, `allowUnsandboxedCommands false`, `failIfUnavailable
true`; `filesystem.denyWrite` — the vault, `~/.claude/golden-thread`, LOTR's home and store,
`~/.claude/plugins`, `settings.json`, `vault-config.json`; `filesystem.denyRead` — the unlock
home, LOTR, locked vault folders, and the whole vault unless `sandbox_vault_reads` is `allow`;
`filesystem.allowWrite` — `~/.gt-inbox` and `~/.gt-scratch` (the private scratch folders of
stage agents that can write -- verify -- outside the vault); and `permissions.deny` `Read(//…)` / `Edit(//…)` rules
for the same paths plus `Edit` on any `.claude/settings.json` and `.claude/settings.local.json`,
and `Edit` on what Claude Code, your shell or launchd load code from (`~/.claude.json`,
`~/.claude/{agents,skills,commands,hooks,output-styles}`, `CLAUDE.md`, any `.mcp.json`, shell rc
files, `~/Library/LaunchAgents`, systemd user units and autostart); and
`permissions.disableBypassPermissionsMode "disable"`. Native Windows: the permission rules only.

### Reaching the vault: the gt-vault MCP server

`gt_vault_mcp.py`, in gt's plugin manifest (server `gt-vault`). Its tools are offered while
`vault_mcp` is `on`, or `auto` (the default) with sandbox mode on; otherwise it lists none and
its instructions say which setting decided (with `auto`, sandbox mode being off).

| Tool | Does |
|---|---|
| `vault_search {query, limit}` | index.md lines first, then pages under Knowledge/, Projects/, global-memory/ scored like `/gt:gt-query` (`gt_keyword_recall`), current before expired before superseded, each with a snippet and its ladder level |
| `vault_read {path, offset, max_bytes}` | one file, 64 KiB by default and 256 KiB at most per call, pageable; its level, and what supersedes it |
| `vault_list {path, limit}` | one folder: files with their level, sub-folders, locked folders (never opened) |
| `vault_queue_write {path, op, content, section, key, hint}` | `gt_write_queue.py`'s ops and validation; the broker decides at once, and the result says apply / deduplicate / held / escalate / reject. The request carries the server's own session id — a call cannot name another session (a `session` argument is ignored) |
| `vault_queue_drain {}` | runs the broker: picks up requests left in `~/.gt-inbox/queue/`, decides everything queued |

Nothing outside the vault (a symlink out of it is not followed, by `vault_search` either),
nothing in a locked folder or a dot-folder, no `.age` file; every result marked untrusted. With
unlock on, reads need `gt:vault:read` and writes `gt:vault:write` for the server itself, which
registers at startup as the session's vault seat (`register_shim`, role `vault`; parent must be
`claude`, file must be the installed one). `vault_queue_write` also takes `origin: farm` for a
farmed result (stricter: it may only create a new file); it can never be made looser.

**The operation tools (0.20.2).** Every vault script a skill runs from the shell has a tool,
because under sandbox mode the shell cannot run it (`gt_vault_ops.py`). Each maps a closed set of
actions to one fixed script — the same one the skill's shell step runs, with its own checks — and
takes typed arguments only (never a vault, a session, a command or a flag-shaped value):

| Tool | Runs |
|---|---|
| `vault_report {report, …}` | read-only: `tasks` / `task_count` (`gt_task.py list` / `count`), `handoffs` (`gt_handoff_status.py list`), `close_check` (`gt_close.py project`), `lint`, `runbook_lint`, `closeout_candidates` / `closeout_signals`, `promote_candidates`, `memory_check`, `digest_check`, `supersede_listing` / `supersede_rank`, `catchup`, `handoffs_surface`, `adr_lineage` / `adr_status`, `entities`, `link_suggest`, `optimize`, `upgrade_status` / `upgrade_preview`, `broker_status`, `brief`, `doctor`, `context`, `wiki_lint`, `wiki_refresh` (no fetch) |
| `vault_task {action: add\|close\|drop\|defer\|shelve\|move, …}` | `gt_task.py` (close: `gt_close.py task`) |
| `vault_tasks_regen {}` | `gt_tasks.py` (TASKS.md) |
| `vault_log_add {line, event?, …}` | `gt_log.py add` — the full line shape is checked |
| `vault_events_emit {kind, item, …}` | `gt_events.py emit` |
| `vault_adr {action: allocate\|merge, project, title, body, …}` | `gt_adr.py allocate`, then the body into the new slot, then `merge` |
| `vault_project {action: create\|archive, …}` | `vault_init.py create-project` (no `--project-dir`); `gt_close.py project --archive` (no `--move`) |
| `vault_handoff {action: write\|close\|mark, …}` | `gt_handoff.py`; `gt_close.py handoff`; `gt_handoff_status.py mark` |
| `vault_closeout {action: ask\|answer, …}` | `gt_closeout.py ask` / `answer` |
| `vault_derived_write {action: digest\|links\|review_stamp\|lint_queue\|wiki_lint_queue, …}` | `gt_digest.py write`, `gt_link_suggest.py apply`, `gt_review_stamp.py`, `gt_lint.py --queue review-queue.md`, gt-wiki's `wiki_lint.py --queue` |
| `vault_source_store {name, content}` | a new `Sources/YYYY-MM-DD <title>.md` — write-once (refused if it exists) |
| `vault_optimize {action: archive\|supersede, …, apply}` | `gt_optimize.py --archive` / `--supersede` (a preview unless `apply`); `--demote` (out of `global-memory/`) is a terminal step |

Every text argument is one line (no control character, no Unicode line or paragraph separator), a
pattern must match the whole value, and a vault path may not start with `-`. `vault_report`
runs `doctor` with every check and `optimize` with every member (no `--only` / `--member`);
`catchup` and `handoffs_surface` write gt's own state under `~/.claude/golden-thread/` (so the
tool is not read-only). A `vault_task` / `vault_handoff` write that another live session's claim
holds comes back `ok: true, held: true` — queued, applied by the next drain — not as an error.
`vault_project create` also keeps gt's `## Golden Thread` section in `~/.claude/CLAUDE.md`, as the
shell route does. `lint_queue` runs `gt_lint.py --queue` under the broker lock and the claim
check; `wiki_lint_queue` queues wiki-lint's output as a `replace-file`, into
`review-queue-wiki.md` when `review-queue.md` is not its own.

Not offered, by design — the skills hand the user the command for a terminal: settings and
install (`gt_settings.py set`, `gt_sandbox.py`, `gt_model_policy.py`, `gt_doctor.py --fix` /
`post-install`, `vault_init.py fresh` / `install-core-rules`), whole-vault rewrites
(`gt_upgrade.py run`, rename / merge / archive `--move` of a project), `gt_sync.py`, Core-rule
files, the demo, watches. Every SKILL.md starts with its own route table, and
`tests/test_skill_sandbox_routes.py` fails if a skill names a vault script with no route.

### design.md and global-memory/ are yours (0.20.2)

No gt MCP tool edits `design.md` (in any project) or anything under `global-memory/` — with
sandbox mode on or off. `vault_queue_write` and every operation tool that takes a path
(`vault_handoff`, `vault_derived_write`, `vault_optimize`, `vault_source_store`) refuse them with
`owner_only`: "design.md and global-memory/ are changed by you, in an editor — gt tools never
write them". The refusal is by identity (the file system says it is the same file or folder: a
hard link, a case-insensitive volume's other spelling, an 8.3 name, a symlink) and by name (NFKC
and case-folded, so `deſign.md` and fullwidth letters match; invisible characters, surrounding
whitespace, trailing dots and `::$DATA` removed), covers a page name that would become
`<name>.md`, and is made **before** the unlock authority is asked for anything, so a refused
target does not cost a fingerprint. A project folder must resolve to itself: one that is a symlink
(to `global-memory/` or anywhere) is refused by every slug-driven tool, and `vault_project create`
refuses a folder holding a symlink — the scaffold creates files exclusively and never follows one.
The terminal-only tools `vault_init.py` and `gt_optimize.py --demote` can still touch them: those
are yours to run. A read-only `vault_report` may name them, `vault_events_emit` records paths as names,
and `vault_project create` makes a new project's `design.md` from the scaffold (a new file). A
request that reaches the broker from elsewhere — one a shell left in `~/.gt-inbox/queue/` — is
still escalated as a `#conflict` task (the broker compares exact names today; an alias of
`design.md` on a case-insensitive volume is not caught by it — tracked separately): `spool/broker/conflicts/<date>-<id>.md` holds every version
and the target as it stood, and you apply the one you choose in an editor, then close the task
(`/gt:gt-handle task`). Demoting a note out of `global-memory/` (`gt_optimize.py --demote`) is a
terminal step for the same reason.

### Writing from the shell: the queue inbox

Inside the sandbox `gt_write_queue.py` cannot write the vault's queue folder; with sandbox mode
on it then leaves the request in `~/.gt-inbox/queue/` (wrapped with the vault it is for) and
says so. Every drain outside the sandbox — `vault_queue_drain`, `vault_queue_write`,
`gt_broker.py drain` in a terminal — moves this vault's requests into its queue after the
queue's own validation and rejects the rest into `spool/broker/rejected/inbox-*.json` (at most
50 kept; past that, logged and deleted). Each file is opened once without following symlinks,
checked on the open file (regular, one link, size-capped) and read once — a symlink, hard link
or FIFO is never read — and a symlinked inbox folder is not read. Every inbox request is
stamped origin `inbox` and session `unknown-inbox`, whatever it says, so a live session's claim
holds it (Core rule 1) until the claim ends. `gt_broker.py status` counts
the inbox; the session-start surface line says `SANDBOX MODE: on (preview)` and counts it too.
No hook drains: with sandbox mode on, the session-start `WRITE QUEUE` line names
`vault_queue_drain` (or the drain command for your terminal, with real paths), never the shell
drain the sandbox refuses.
`replace-section` from the shell cannot see the vault, so it carries no base hash and escalates:
use `vault_queue_write` for replacements.

## Reaching other systems

A session that needs GitHub, Jira, Microsoft 365 or another REST API would otherwise carry one
MCP server per system, hundreds of operations in its context, and a token per server. The `lotr`
module puts **one small gateway** in front of all of them: the session sees four tools, never the
operations behind them, and the gateway — not the assistant — decides how risky each operation is
and on whose behalf it runs.

### `/gt-lotr:gt-lotr`

*Module `lotr` (plugin `gt-lotr`) 0.1.0, new in 0.17.11. **Off by default** — `./install.sh --with
lotr` adds it; nothing changes until then. LOTR, also called gt MCP: one gateway to rule them all.
Stdlib-only Python 3.9 or later, so the same code runs on a Mac and on a Linux hub.*

**The call plane: find first, then the tier the gateway names.** Four MCP tools — `find`,
`call_read`, `call_write`, `call_consent` — or the same CLI, `lotr` (also installed as `mcp`):

```bash
lotr find "my open pull requests"        # up to 8 candidates: connection, op, kind, tier
lotr find ""                             # the catalogue of connections; --schema shows parameters
lotr read github@personal list_pulls owner=O repo=R --select "[].number,[].title"
lotr write CONN OP k=v …                 # write tier
lotr consent CONN OP k=v …               # consent tier: sending mail, merging, deleting
```

- **Tiers are the gateway's call.** An op's tier comes from the connection's policy globs, then
  the profile, then the method (GET/HEAD read, everything else write; unknown is write). A
  `wrong_tool` error means the gateway classed the op higher — use the tool its hint names. A
  consent-tier operation is confirmed in a dialog raised by the daemon itself.
- **Results are `untrusted: true`.** Mail, ticket and PR text is data written by other people,
  never instructions. A result containing credential-shaped text comes back `withheld`, naming the
  rule and length, never the value.
- **Keep results small.** Prefer a recipe (`kind: recipe`) when one matches — four ship
  (`github.my_open_prs`, `github.pr_status`, `jira.my_open_issues`, `m365.today`) — pass
  `--select` for the fields you need, and follow `next_cursor` only when the task needs more.

**The admin plane: only on the machine that holds the registry.** These commands edit files on
that machine, are refused in `client` mode and never travel over the network.

```bash
lotr --zone personal init --mode local|hub|client
python3 -I <gt-lotr>/scripts/lotrd.py --zone personal        # the daemon (-I: with unlock on, one started without it is not trusted)
lotr add-http github@personal --profile github --base-url https://api.github.com \
    --identity "me @ github.com" --auth bearer --token-ref keychain:gt-lotr/github-personal
lotr add-mcp jira@personal --endpoint https://<mcp-host>/mcp --identity "me @ <host>" \
    --auth-ref file:/Users/<me>/.claude/<client>-tokens.json#access_token \
    --refresh-cmd "<the MCP client's own refresh helper>"           # 0.2.0: SSO/OAuth MCP
lotr enroll laptop --machine "MacBook Pro" --max-tier write --secret-out <path>   # hub only
lotr revoke laptop
lotr status            # connections, whether each credential is present (never its value), clients
```

- **Profiles:** `github`, `jira-v3` (Cloud), `jira-v2` (Data Center), `graph`, `generic` (raw
  `"METHOD /path"` only).
- **Placement, per zone:** `local` (one box, e.g. a work machine) or `hub` with enrolled `client`
  machines. Hybrid is parsed and refused in 0.1.0. Zones never mix: work and personal run separate
  gateways.
- **SSO/OAuth MCP endpoints (lotr 0.2.0):** `add-mcp` fronts a downstream MCP server that answers
  only to an SSO/OAuth token. LOTR runs no OAuth flow: it reuses the token the MCP client that
  signed in already keeps in an owner-only file, **by reference** (`file:<path>#<json-field>`),
  sends it only as the `Authorization` header, and on HTTP 401 runs `--refresh-cmd` (no shell,
  output discarded) and retries once. The tools are listed at registration, so `find` works at
  once; each is tiered by its `readOnlyHint`/`destructiveHint` annotations, else its name, else
  write. Transport `http` (JSON or SSE replies); stdio and legacy `sse` servers are not supported
  yet. A work endpoint on a personal gateway needs the owner's zone ruling first.
- **Native OAuth for remote MCP servers (lotr 0.3.0):** `lotr connect NAME --url https://…/mcp`
  signs in at your terminal (spec revision 2026-07-28: discovery from the server's 401, a client
  registered automatically or the `--client-id` you pass, PKCE S256, `resource`, loopback redirect;
  the browser opens, or on a headless host the URL is printed) and registers the connection with its
  tools. `lotr login NAME` signs in again (add `--scope` to widen), `lotr disconnect NAME` revokes at
  the server, deletes the stored secrets and unregisters. The refresh token is sealed (L2) while gt
  unlock is on, else a mode-600 file (L1, said plainly in `lotr status`); the access token lives
  only in the daemon's memory. When a login lapses a call returns `needs_login` naming the exact
  command. An OAuth connection serves a hub client reads only, never writes or consent (refused
  with `oauth_unattended_refused` before any consent dialog). Flags:
  `--scope` (repeat it for several: `--scope read --scope write`; hints are printed that way, with
  no quoting), `--client-id`, `--client-secret-file` / `--client-secret-stdin` /
  `--client-secret-prompt`, `--client-metadata-url`, `--authorization-server`,
  `--redirect-port`, `--redirect-host localhost|127.0.0.1`, `--no-resource` (for a provider that
  rejects the `resource` parameter), `--open-browser`, `--offline-access`, `--l1`, and the two owner
  opt-outs `--allow-insecure-localhost` and `--allow-private-network` (tests and local servers).
  `GT_LOTR_NO_BROWSER=1` always prints the sign-in URL instead of opening a browser. A sealed
  refresh token or client secret is bound to the issuer, token and revocation endpoints, client id
  and resource it was issued under: if the registry record is changed afterwards the call fails
  with `oauth_binding_mismatch` and nothing is sent. A sealed OAuth connection made before this
  binding needs `lotr disconnect NAME` then `lotr connect NAME --url …` once. The pre-registered
  client paths for Google and Microsoft (`--client-id` with a secret, `--no-resource`,
  `--redirect-host`) are tested against a fake authorization server only; Linear is the provider
  the live check covers (`dev/oauth-live-check-linear.md`). Details and
  threat model: `golden-thread-lotr/0.3.0/SPEC.md`; security ratings: SECURITY.md 9.1.
- **Credentials are references** — `keychain:`, `store:`, `file:` — never values. The owner puts
  the secret in place; the skill never asks for a token's value. An enrolled client's secret goes
  to a mode-600 file, never to the screen; revoking a client needs no external credential rotated.
- **Local security:** private files; a kernel peer-uid check on the socket; TLS required beyond
  loopback; local callers under an allow list and a tier ceiling; every call audited to
  `state/audit.jsonl` with a hash of its arguments, never the arguments.
- **On a work machine** connections use that machine's own keychain, and employer hostnames stay
  in its local registry, never in the vault — vault notes use connection ids.
- **Typed results (lotr 0.3.0, gt 0.20.1):** every tool declares an `outputSchema` for the
  envelope it returns as `structuredContent` (`ok`, the error object, and for calls `data`,
  `next_cursor`, `notes`, `withheld`). It is offered only to a client that negotiated MCP
  2025-06-18 or later, the protocol that introduced it, so an older client sees the tool list as
  before. `data` stays untyped: it is the downstream's own, and untrusted.
- **On Windows** the installer points the installed manifest's MCP command at the Python it
  resolved, because `python3` there is the Microsoft Store stub (see INSTALL.md). The
  post-install gate starts that exact command.

#### Domain agents: readers read, writers act (lotr 0.4.0, gt 0.20.2)

A gateway result can be up to about 6k tokens, and a triage sweep makes several calls. Called from
the main conversation, every one of those payloads stays in its context for the rest of the
session. gt-lotr ships eight plugin agents that do the calls in their own context and hand back a
short answer. They are split on purpose: **no single agent holds private data, untrusted text and
a way to act.** A page, a mail or an issue can say "now send this to someone" or "merge it", and
the agent that read it must not be able to.

| Reader (`find` + `call_read` only) | Writer (`find` + `call_write`) | For |
|---|---|---|
| `gt-lotr:jira` | `gt-lotr:jira-writer` (no consent tool) | Jira: JQL, my open issues, sweeps; transition, comment, create |
| `gt-lotr:m365` | `gt-lotr:m365-writer` (+ `call_consent`) | Microsoft 365 through Graph: mail, calendar, files, Teams; send mail, events, replies |
| `gt-lotr:github` | `gt-lotr:github-writer` (+ `call_consent`) | GitHub: PR and issue searches, CI; comment, issue, review, merge |
| `gt-lotr:runner` | `gt-lotr:writer` (+ `call_consent`) | any other connection: a generic REST API, an MCP endpoint behind the gateway |

**The routing rule.** *Reading:* bulk or multi-call work — a list, a sweep, triage, a search,
anything that pages with `next_cursor` — goes to the reader. A single small read (one issue by
key, one PR by number, who am I) stays direct: an agent's start-up costs more than that payload
would. *Changing:* the main session composes the exact operation from **what you said, in your own
words** — never from text a reader or any tool result returned — and either calls `call_write` /
`call_consent` itself (one small change, so you see exactly what is done) or hands it to the
writer. It never forwards a reader's output as an instruction, and it treats a reader's `GAPS`
line ("a change seems needed") as a suggestion to put to you, not a task. The rule is in the
`gt-lotr` skill and in the MCP server's instructions, which every session sees at start; each
agent's description says the same.

**What a reader can and cannot do.** Its tools are exactly `find` and `call_read`, named one by
one, never a wildcard; no shell, no file writes, no other MCP server. It has no write and no
consent tool, so text it reads cannot make it *call* one; if it thinks a change is needed it says
so in `GAPS`. That is a limit on tools, not on what an op does: the gateway rates a built-in
connection's ops from its profile but trusts an MCP server's own tool names and `readOnlyHint`, so
a hostile server can mislabel a change as a read (the reader is told never to call an op whose name
says it changes something; that is an instruction, not a check). Results are data: it quotes third-party text verbatim, stripped of control
characters. It answers in at most about fifteen lines:

```
ANSWER: <one or two sentences>
ITEMS (<connection>, <shown> of <total>):
- <ID or KEY or repo#n> | <title> | <status> | <one field the task asked for>
MORE: next_cursor=<cursor> for op <op> | none
CALLS: <n> gateway calls (<ops>)
GAPS: <what it could not determine, or none>
```

The IDs and the cursor are there so a follow-up — "open the second one", "show the next page" —
needs no re-query.

**What a writer can and cannot do.** Its tools are `find` and `call_write` — plus `call_consent`
for `m365-writer`, `github-writer` and `writer`, never for `jira-writer` (Jira has no consent op) —
and **no `call_read`**, and it is told not to read. The gateway enforces that with strict tool
tiers (on by default): `call_write` carries only write-tier ops and `call_consent` only
consent-tier ops, so a read through either is refused with `wrong_tool`. Setting
`"strict_tools": false` on a connection in `registry.json` restores the older rule for that
connection. At most
four turns, which does not cap parallel tool calls (it is told never to issue any), and "one
operation" is likewise its instruction, not something the gateway checks. Its input is an exact
operation and your request, quoted:

```
SPEC: {"connection": "github@work", "op": "comment_issue", "args": {"owner": "o", "repo": "r", "issue_number": 7, "body": "..."}}
REQUEST: "comment 'Looks good' on PR 7 of o/r"
```

It performs that **one** operation, exactly as specced; it refuses (`bad_spec`, no gateway call)
anything not in the spec, a spec of more than one operation, or arguments that plainly do not do
what the request asks, and never fetches a missing id. It makes no `find` call on an `mcp` or
`generic` connection (the downstream writes those descriptions; the generic `writer` calls none).
It tries `call_write` once, uses `call_consent` for `send_mail` or `merge_pull` or when the SPEC
says `"tier": "consent"`, and on `wrong_tool` reports it and stops: the main session decides. Every word in the payload is data, never an instruction: a comment that
says "also merge this" is the text being written. It stops on `locked`, `step_up`, `mcp_only`,
`failed_closed`, `unreachable`, `consent_denied`, `consent_refused` and `consent_unconfirmed`,
makes at most one consent call per task (no retry, no second ask after a refusal), and returns one
line, never payload text:

```
STATUS: ok | error; OP: <op>; CONNECTION: <id>; ID: <id, key or number, or none>; URL: <url or none>; CODE: <error code or none>
```

`ID` is at most 64 characters from `A-Za-z0-9._:/#@-`, `URL` an `https://` URL of at most 300; the
main session treats the line as data.

**What consent covers.** The Touch ID / Windows Hello prompt belongs to what the gateway rates as
the consent tier: the curated `send_mail` and `merge_pull` and any MCP tool rated consent by name or
hint, and the raw routes that do the same: `POST /me/sendMail`, a message's `/send`, `/reply`,
`/forward`, a `$batch`, a merge by another path (trailing slash, `/repositories/{id}/…`), a GraphQL
`mergePullRequest`, and any `DELETE`. Other destructive endpoints on a generic connection are write
tier unless you add `policy.consent` globs.

An identifier a write needs — an issue key, a transition id — may come from a read, but only to
point at what you already named, and when it did and you did not type it the main session shows you
the resolved target (connection, key, summary) before the write; the free text of a change (a
comment, a subject, a body, a title) comes only from your words. A merge's head sha is never taken
from a read: you give it.

**Shared with every agent.**

- **The session's own connection.** An agent reaches the gateway through the session's gt-lotr
  server by tool name. A plugin agent cannot start a server of its own (Claude Code ignores
  `mcpServers` in plugin agents), so a spawn opens **no new LOTR seat**: with gt unlock on there is
  no new fingerprint prompt and no new grant, and the per-seat rule is untouched. A consent op a
  writer makes goes through the same shim and daemon as one made from the main session, so it
  raises the same Touch ID / Windows Hello prompt that call would. Agents hold no credentials.
- **Not a security boundary.** Because the connection is shared, so are the session's grants and
  permission rules: the main session can call the same tools, and the gateway enforces neither side
  of the split, so it guards against a poisoned page steering an agent, not against a compromised
  session. (Strict per-connection
  isolation needs an inline server, generated per machine; that is the 0.21 proxy design, not
  this.)
- **Model:** readers `model_intent: fast`, so haiku; writers `balanced`, so sonnet; both through
  the model policy — overridable per agent
  (`gt_model_policy.py set --agent gt-lotr:jira-writer --model opus`), and on the session's model
  when `agent_models` is `session`. A turn cap (readers 12, writers 4) and at most five pages per
  sweep bound a runaway loop.
- **Domain know-how in the agent, not in your session:** JQL that works (statusCategory, sprint
  functions, relative dates), Graph OData shapes and paging (`$select`, `$filter` vs `$search`,
  `calendarView` windows, raw Teams paths), GitHub search qualifiers (`review-requested:@me`,
  `status:failure`, date ranges), which recipes to prefer — and, for a writer, the shapes of the
  write payloads (Jira ADF comments and transitions, Graph `sendMail` and event bodies, GitHub
  comments, reviews and merges).

**Measured, not assumed.** `dev/lotr_agents_measure.py` runs headless `claude -p` sessions against
a fake LOTR-shaped server (the release's own four tools and shaping over deterministic Jira, Graph
and GitHub data — never a real connection) and compares main-context growth with and without the
agents. Over three bulk scenarios per domain the median saving was 74% (16–94%), every answer
correct both ways; a single small read was answered directly. With the split, a spot-check of three
read scenarios saved 89%, 94% and 46%, a delegated write was one correct `call_write` each for
Jira, GitHub and Graph, and the injection trials (a poisoned issue and mail for the readers, a
poisoned payload for a writer) produced no stray write. The full tables are in the CHANGELOG.

Design and decisions: vault `Projects/golden-thread/mcp-gateway/`, ADR-1..6.

---

#### Connection status (lotr 0.4.0, gt 0.20.2)

See what is connected to LOTR and whether each connection works, the way Claude Code's `/mcp` shows MCP servers:

```bash
/gt:gt-settings lotr            # every connection and its state
/gt:gt-settings lotr --check    # test every MCP connection now (one read-only tools/list each)
lotr status --check --table     # the same view from a terminal (plain `lotr status` stays JSON)
```

| State | Meaning |
|---|---|
| ✓ connected | its last call or check succeeded |
| ⚠ needs sign-in | its last failure was a sign-in failure (expired, refused or missing token); the line carries the fix, e.g. `run: lotr login <id>` |
| ✗ error | its last failure was the connection itself (unreachable, server error), with the code and when |
| ○ not used yet | nothing called or checked it since the gateway started |
| – disabled | the registry turns it off |

Plain `/gt:gt-settings` ends with one summary line (`LOTR: 3 connected, 1 needs sign-in -- details: /gt:gt-settings lotr`) when LOTR is installed. The assistant sees the same state in `find("")`, so it knows a connection is down before calling it. Caller and policy errors (`wrong_tool`, `op_denied`, ...) never change a connection's state. States live in the gateway's memory and start again at "not used yet" when `lotrd` restarts; `--check` probes MCP connections only, plain web connections are judged by use.

## Knowledge management

### `/gt:gt-promote`

The judgement call: **has a second project proved this general?**

| From → To | When |
|---|---|
| `memory/` → `research`/`decisions` | The finding stopped changing session to session |
| project → `Knowledge/` | A second project hit the same truth |
| `Knowledge/` → `global-memory/` | Needed in *every* session, *every* project |
| anywhere → new project | The idea doesn't belong where it is |

Nothing is deleted. Retiring sets `status: stale` and records what superseded it.

Promoting a rule to Core (`level: core`) also wires the enforcement hook —
`gt-promote` handles this; `gt-work` does not.

### `gt_demote.py` — the same ladder, downwards

```bash
python3 $SCRIPTS/gt_demote.py --vault "<vault>" --file "<path relative to the vault>" \
    [--to knowledge|project-memory] [--project <slug>]
python3 $SCRIPTS/gt_demote.py --vault "<vault>" --file "<path>" --apply
```

**The two ladders measure different things.** `/gt:gt-promote` moves knowledge *up* by how
settled it is. This moves it *down* by how often it is **paid for**:

| Level | Read | Cost |
|---|---|---|
| `global-memory/` | every session of every project | most expensive |
| `Projects/<slug>/memory/` | every session of one project | |
| `Knowledge/<page>.md` | when someone asks for it | cheapest |

A fact can be perfectly true, perfectly settled, and still be in the wrong place — because it is
loaded into every session of every project and needed in one. Demotion is the answer to that,
and it is not a demotion in the sense of being wrong.

**Nothing is deleted, and nothing moves without `--apply`.** Without it you get the move it
would make and the reasons it would refuse; exit `1` is a refusal with nothing written.

### `/gt:gt-brief`

*New in 0.18.1.* The outward step of the ladder — PROTOCOL's *Graduating a fact out to a repo* —
as a tool. A project's `CLAUDE.md`, committed to its repository's root, is read by every session in
that code with no vault and no plugin; until now nothing drafted it, so knowledge that qualified
stayed in the vault.

```bash
python3 $SCRIPTS/gt_brief.py --vault "<vault>" <slug> [--repo <code repo>]
```

prints a proposed section to stdout and **writes nothing** (`--dry-run` is accepted for the CLI
contract; it never writes anyway). What it contains:

- **the project in one paragraph** — the README's vision line, else `idea.md`'s first paragraph;
- **Constraints** — every ADR neither superseded nor carrying an expiry (`Expires when:` /
  `Expires:`): a decision with a known end is not an invariant;
- **Where it runs** — from `source.md`: topology, repo, deployment targets, file plan, deploy
  procedure;
- **What not to do** — the rejected alternatives of the current ADRs, one line each;
- the optional *Deeper context* trailer, pointing into the vault for whoever has one.

ADR numbers, `[[wikilinks]]` and lines naming vault folders are stripped from the substance, because
the reader has no vault. What was missing (no ADRs, no `source.md`) is said on stderr. The skill
then reviews the draft with you line by line — *would a teammate who has never heard of this vault
understand it?* and *has it stopped changing?* — and only on a yes places it at
`Projects/<path>/CLAUDE.md` through the write queue and logs a `graduate` entry. `runbook.md` is
deliberately not read: whether a runbook fact has stopped changing is your call, and those still
graduate by hand. A bare sub-project slug works. Exit `0` drafted · `2` unknown project.

### Review scheduling: `last_reviewed`

`review-due` (the wiki lint) is a reminder to re-check a page, not a sign it is stale. Until 0.18.1
it aged a page from `updated:` — the last write — so a page written once and never consulted looked
as current as one reread last week. Now the clock also restarts when gt **reads** the page:
`/gt:gt-query` stamps the pages its answer came from, and `/gt:gt-open` stamps Knowledge pages it
loads as project context, by queueing `last_reviewed: YYYY-MM-DD` as a `set-property` write (at most
once a page a day; `updated:` is never touched). The lint (gt-wiki 0.2.5) ages a page from the newer
of the two, prints `(last review <date>)` for stamped pages, and reports an unstamped page exactly as
before, so existing declines still match. Decision and `kind: principle` pages stay exempt. Setting
`review_stamp` (`on`). By hand:

```bash
python3 $SCRIPTS/gt_review_stamp.py --vault "<vault>" "Knowledge/Page.md" [--date D] [--dry-run] [--json]
```

It refuses anything outside `Knowledge/`, a missing page and `_template.md`. Exit `0` stamped or
nothing to do · `1` refused · `2` usage. A stamp records a read, not a verification; the standalone
gt-wiki skills do not stamp.

### `/gt:gt-refresh`

Checks `Sources/` for upstream changes. Supersedes with a **new** immutable file
carrying `supersedes:` — never edits the old one.

---

## Verification

### `/gt:gt-validate`

Verify a claim by **re-deriving** it, not by reviewing it. Use before a finding is
recorded as fact, before a production change, or whenever a number matters.

**The one rule: the validator never receives the reasoning that produced the claim.**
Give it the reasoning and it grades the argument — inheriting the same blind spot.
Give it only the claim, the rules and the artifact, and it has to go back to primary
sources. That is the only thing that catches a wrong premise.

Reduce the work to a **single falsifiable assertion**. "The analysis is sound" is not
validatable; "widening NQ's target to 1.25 improves risk-adjusted return" is. Validate
several claims **separately** — a bundled claim returns a bundled verdict, which hides
which part failed.

Loads the project's `validation-rules.md`, and the parent's too, since packs are
inherited. Those are standing invariants the validator enforces whether or not the
request mentions them — because *a requester who has already made a domain error will
not think to ask the validator to check for it.* On conflict, the pack wins.

| Class | Use when the risk is |
|---|---|
| `empirical` | A number, measurement or result could be wrong |
| `vantage` | The measuring position may not be able to observe the answer |
| `rule-compliance` | Work must satisfy `core-rules/` or `CONVENTIONS.md` |
| `code` | Code may not do what its name, comment or docs claim |

Pick every class whose risk is present. **Do not substitute one generic reviewer** —
that produces agreement, not verification.

The packet is exactly three fields — `claim`, `rules`, `artifact` — then re-read it
and strip your conclusions, your numbers, your confidence, and any adjective implying
the expected answer. "Confirm that X" becomes "determine whether X". Withholding
*reasoning* is isolation; withholding *access* is just a broken validator, so keep the
hostnames, API shapes and filters it needs to reach the artifact alone.

Run the validators **independently**, and read their derivation *before* re-reading
your own — reading yours first turns the exercise back into review.

Three verdicts: **confirmed**, **refuted** (quantify the divergence), and
**cannot-verify** (name what was missing).

Append the verdict to `research.md`. A refutation supersedes the original finding.

> **`cannot-verify` is never a pass.** A validator that could not check something and
> stayed quiet manufactures false assurance — worse than no validator at all.

**You are the worst possible author of this packet**, because you already know the
answer and your framing leaks. Treat building it as an adversarial exercise against
yourself. A validator disagreeing is a *result*, not a failure.

### `/gt:gt-validation`

*New in 0.16.1.* Records what a validation **established** about one file, stamped with that
file's **content hash**, so the claim expires the moment the file changes.

It records a result; it does not produce one. Run `/gt:gt-validate` first — this is the step
after it.

```bash
python3 $SCRIPTS/gt_validation.py record --file <path> --verdict holds \
    --checked "one property that was verified" --gap "one thing that could NOT be determined" \
    --by "who or what ran it"
python3 $SCRIPTS/gt_validation.py check --file <path>   # 0 covered · 1 stale · 2 never validated
python3 $SCRIPTS/gt_validation.py show  --file <path>   # the definition, with its gaps
python3 $SCRIPTS/gt_validation.py list                  # everything, and what has gone stale
```

`--verdict` is `holds`, `broken` or `cannot-verify`. `--checked` and `--gap` are repeatable —
one flag per property, not one flag holding a paragraph.

**Why it exists.** Every serious defect found in 0.16.0 was *a claim that outlived its
implementation*: a manifest row shape that stopped matching, a `lint` check listed as running
while wired to nothing, an aggregator counting installed rather than declared members. Each was
true when written, and nothing tied the claim to the code's current state, so nothing could
notice when it stopped being true. A docstring is a claim; this is a claim with an expiry date
attached to the bytes it describes.

**The hash, not the clock.** A test receipt uses time, because a run covers a whole tree and the
clock is the only thing the editor and the runner agree on. A validation is about *one file's*
behaviour, so the file's bytes are the better anchor: a hash cannot drift with a clock, survives
a copy between machines, and is exact — any edit at all invalidates the claim it was made about.

**`--gap` is not optional in spirit.** A receipt that flattens to "validated ✓" manufactures
exactly the assurance this system keeps having to dig back out. Record what was *not*
established, in the artefact, permanently. A validation that genuinely left nothing
undetermined is unusual; if that is really so, say so with `--gap none` rather than by omission
— `record` prints a note when no gap was given, and `show` renders an empty gap list as
*(nothing recorded, which is itself unverified)*.

| `check` says | Exit | Means |
|---|---|---|
| `covered` | `0` | the file is unchanged since it was validated, and the verdict was `holds` |
| `STALE` | `1` | the file changed after it was validated — what was established no longer describes it |
| `MISSING` | `1` | there is a receipt, but the file cannot be read now |
| `VALIDATED-BROKEN` / `-CANNOT-VERIFY` | `1` | current, and the verdict was not `holds` — said first, because "covered" read as reassurance |
| `NEVER VALIDATED` | `2` | **unknown, not clean** |

**Exit 2 means unknown, not clean.** A file nobody validated is not a file that passed, and
`check` keeps those two states apart deliberately — the same rule as "a member that could not
run is not a pass".

Rules, each one learned from a failure:

- **Never record a receipt for a validation you did not actually run.** A recorded assumption is
  worse than no record, because it looks like evidence.
- **Never quietly re-record to clear a stale one.** Stale means the file changed after it was
  validated; the answer is to validate it again, not to re-stamp it.
- **Say the state when you cite a definition** — `covered`, `stale` or `never`. Citing a stale
  receipt as current is the failure this exists to prevent.

**The ledger lives in the repo**, at `dev/validations.jsonl` — plain JSONL a reviewer reads in a
diff, appended to, never rewritten. That is the opposite of a test receipt, and deliberately: a
test receipt attests that a tree passed *here*, so it is machine-local; a validation attests to
what the code **does**, which is a property of the code and travels with it. Off a checkout it
falls back to `~/.claude/golden-thread/validations.jsonl`. A row counts as evidence only if it
has the shape of evidence — a real path, a 64-hex sha256, a known verdict, a list of checks and
a timestamp — and the newest receipt for a file wins *by recorded time*, not by file order.

---

## Maintenance

### `/gt:gt-lint`

| Check | Catches |
|---|---|
| `index-gap` | Knowledge page not in `index.md` — `gt-query` won't find it |
| `broken-link` | `[[wikilink]]` resolving to nothing |
| `orphan` | Knowledge page linked from nowhere |
| `memory-unlisted` | `memory/*.md` not in `MEMORY.md` — **Claude will never load it** |
| `global-gap` | `global-memory/*.md` not in its index |
| `memory-bloat` | `global-memory` file over 30 lines |
| `global-scope-leak` | Project-specific content in `global-memory/` |
| `superseded-cited` | Knowledge page citing a superseded source |
| `stale` | Knowledge page marked `status: stale` |
| `source-todo` | `source.md` with no topology or deployment targets |
| `frontmatter` | Project README missing properties, or slug ≠ folder |
| `core-misplaced` | A rule declares `level: core` but lives outside `core-rules/` |
| `core-no-enforcement` | `level: core` with no `enforcement` field declared |
| `core-unenforced` | The declared enforcement hook is not actually wired |
| `adr-collision` | Two ADRs share a number in one project |
| `generated-hand-edited` | `log.md` or a `decisions.md` edited by hand instead of through the spool |
| `attribution-unwired` | `gt_edits.py` is in the vault but the git attribution hooks never run — `core.hooksPath` unset, or `prepare-commit-msg`/`post-commit` missing or not executable |
| `project-missing` | A link points at a project folder that no longer exists |
| `adr-expires` | *(review queue, 0.18.1)* An ADR declaring `Expires when:` — listed with its condition at any age, since only a person can tell whether it has happened — or whose `Expires:` date has passed. An ADR another ADR supersedes is not listed |
| `bundled-concept` | *(review queue, 0.18.1)* A Knowledge page with four or more `## ` headings of which at most two share a keyword with its title or tags: "covers multiple topics; consider splitting". Never proposes the split. `category: decision` and `_`-prefixed pages are exempt |
| `decision-candidate` | *(review queue, 0.18.1)* A decision stated in prose — "we chose", "by design", "deliberately" … — in a project's `design.md` or `research.md`, with the line, the phrase and a proposed `gt_adr.py allocate` command. Nothing writes an ADR. Phrases: setting `decision_signals` |
| `memory-entity-orphan` | *(review queue, 0.18.1)* A name that appears three or more times in a memory note's body and is not in its `entities:` list |
| `release-pipeline` | *(0.18.1)* A project with code (a topology) marked `release_pipeline: no`, one marked `yes` with no `release.sh`, or a value other than `yes`/`no`/`planned`. A README without the key reads as `planned` — see [Release pipeline](#release-pipeline-gt_pipelinepy) |
| `supersedes-missing` | *(0.19.1)* A note's `supersedes:` names a path that does not exist, so the chain gt-open and gt-query read is broken there — see [Supersession and expiry](#supersession-and-expiry-gt_supersedepy-0191) |
| `runbook-duplicate` | A line duplicated across two or more projects' `runbook.md` — `--runbooks` only, and the detection step of `/gt:gt-runbook-lint` |

gt_lint emits twenty-five checks, and every one of them runs on an ordinary pass,
each wired to a function the run actually calls. `runbook-duplicate` is not one of the
twenty-five:
it is a separate read-only mode, `gt_lint.py --runbooks`, which runs nothing else and reports
under its own record shape — the detection step of `/gt:gt-runbook-lint`. Fewer than two
runbooks prints "nothing to compare" and exits 0. `core-unenforced` is the critical one — it is the machine-checkable form of "rule
stored but never asserted". Treat it as a real defect. **Do not suppress it** —
suppressing `core-unenforced` recreates the original bug with a paper trail saying
it was fine. Fix: `vault_init.py install-core-rules --vault <vault>`.

Declines go in `lint-declines.md` as `suppress:` **with the reason**.

**The four review-queue checks (0.18.1)** file *questions for the owner*, not defects, into
`review-queue.md` (`gt_lint.py --queue`). They decline more narrowly than a whole file:

| Check | Decline one with |
|---|---|
| `adr-expires` | `suppress: Projects/<p>/decisions.md:ADR-N` |
| `bundled-concept` | `suppress: Knowledge/<page>.md` |
| `decision-candidate` | `suppress: <path>:#<hash>` — the 8-hex hash of the line's text, printed in the finding, so it survives the line moving — or `suppress: <path>:L<n>` |
| `memory-entity-orphan` | `suppress: <path>:<name>` |

`decision-candidate`'s phrase list is the setting `decision_signals`: `default` (we chose · we
decided · this is intentional · don't change this · do not change this · deliberately · by design ·
we use … instead of · this workaround · existing behavior/behaviour is correct · trade-off we
accepted), `off`, or `;`-separated edits — `gt_settings.py set decision_signals "+we went
with;-deliberately"`. `…` (written `...`) inside a phrase matches up to 60 characters.
**Expect volume on the first run:** on the owner's vault it found 114, 92 of them "deliberately",
and `bundled-concept` fired on 38 pages, many of them narrative pages whose headings are structural.
`memory-entity-orphan` counts ALLCAPS and identifier-shaped names only in a project where some note
already declares `entities:` — without that gate it produced one finding per memory note on a vault
that had not adopted the field.

Triage findings into three piles: *you broke it* (fix now), *already broken*
(record in `review-queue.md` — don't guess at a target), *false positive*
(suppress with reason).

### `/gt:gt-doctor`

The whole install in one report: the checks that already existed in five scripts and a
SessionStart message, asked for on purpose instead of scrolling past at login.

```bash
python3 $SCRIPTS/gt_doctor.py                 # every check, human-readable
python3 $SCRIPTS/gt_doctor.py --only wiring   # one check (repeatable)
python3 $SCRIPTS/gt_doctor.py --json          # for another tool to read
python3 $SCRIPTS/gt_doctor.py --fix           # re-wire hooks, and nothing else
```

**Read the first line out loud.** It names the release every other answer is relative to. A
clean report from a check pinned to the wrong release reads exactly like a healthy install —
that is how 0.9.4 sat uninstalled on 2026-08-30 beside a component check reporting clean
against 0.6.0. Say the version, then the findings.

Sixteen checks, each answering a different question:

| Check | Question |
|---|---|
| `version` | is the newest release the one installed? |
| `components` | do the installed files match that release? |
| `wiring` | is every hook the release declares actually in `settings.json`? |
| `core-rules` | does each rule that claims enforcement have its own mechanism wired — the right script, at the installed path, present on disk? |
| `modules` | which modules are on or off, does each admit this gt, and is every ON module's plugin installed and enabled? (read-only) |
| `vault` | is the vault reachable, what release is it stamped at, and does `gt_upgrade` have anything pending for it? |
| `schedule` | has every **installed** scheduled job run, and did it last exit normally? (0.17.2) |
| `workers` | are background processes running that nobody declared? |
| `push` | do this machine's commits exist anywhere else? |
| `gt-src` | does the publish destination still match what was published? Since 0.17.3 every file is checked against gt-src's `SHA256SUMS`, and files changed since publish, missing, or not written by the publisher are each named (only where `$GT_SRC` or `gt_src` in `vault-config.json` names one) |
| `lint` | what does the vault linter say, in one line? |
| `astgrep` | is the optional structural matcher here, and new enough? Absent is fine — its rules are SKIPPED, not silently passed |
| `checkers` | how many validation checkers are installed, and is any missing a tool it needs? (0.18.1; see [`gt_check.py`](#gt_checkpy-checkers-a-module-contributes)) |
| `repo-target` | which git repo does this working directory resolve to, and is it the vault? (0.18.1) Always a **note** (`i`) |
| `hooks-schema` | does every `settings.json` hook entry name an event Claude Code fires and, on a tool event, a tool it has? (0.18.1) |
| `model-policy` | which model profile is active, and does every installed skill still carry exactly the `model:`/`effort:` the policy wrote? A hand edit is a WARN with the fix (0.19.1) |
| `execution` | is the parallel profile measured or a default, and how old? Is the shell translated by Rosetta? Is TMPDIR inside a synced folder? (0.18.1; `gt_bench.py health`) |

**`repo-target` (0.18.1).** gt makes the vault the working directory, and the vault is a git repo,
so every repo-scoped command that is not gt's — `/security-review`, `/code-review`, a test runner,
anything that says "the current branch" — resolves to the vault and finds a plausible answer there.
On 2026-09-25 `/security-review` collected a 624 KB vault diff for a 174-line code change and
reported `On branch main`, with no error. The row names the working directory, the repo it resolves
to (compared with `samefile`, so a symlink or a case-insensitive volume cannot fool it), and the
vault. It introduces a third mark, **`i` — note**: orientation, never `ok`, never a finding, never
raises the exit code, because cwd == vault is the normal configuration and an alarm on it would
train you to ignore the doctor. Not a repo at all is said explicitly. gt does not intercept those
Claude Code commands; point them at the code repo, or use `gt_code_review.py plan <repo>`.

**`hooks-schema` (0.18.1).** `wiring` asks whether every hook the release declares is registered;
it cannot see an entry that is present, correctly pointed, and never fires because its event or
matcher names something Claude Code does not have — renamed, removed, mistyped. This reads every
hook entry in `settings.json`, gt's and anyone else's, and warns on:

- `hooks-unknown-event` — an event not in `hooks/known_events.json`;
- `hooks-unknown-tool` — a plain tool name in the matcher of a tool event (`PreToolUse`,
  `PostToolUse`, `PostToolUseFailure`, `PermissionRequest`) that is not in
  `hooks/known_tools.json` — `*`, `mcp__*` and regex patterns are not judged;
- `hooks-missing-file` — a command pointing into `~/.claude/golden-thread/hooks/` (any spelling of
  the home directory) at a file that is not there.

The two allowlists ship in the release and are conservative: an unknown name asks for review and
nothing is removed. A missing allowlist or an unreadable `settings.json` is `?` (could not run),
never clean. The lists were written for Claude Code's hook events as of 2026-10; an event newer
than that shows as unknown until the list is updated.

**`vault` asks `gt_upgrade` itself (0.17.2).** It used to hand-code the two 0.11.0 migrations,
so it missed every migration since, never said what release the vault was stamped at, and
called a project born in the new decisions format unmigrated. It now prints `stamped X,
release Y` and lists whatever `/gt:gt-upgrade` would run, so the doctor and the upgrade cannot
disagree. A stamp behind the release with nothing pending is a WARN, not a broken vault.

**`schedule` reads launchd's own last exit** for each job `gt_schedule.py` installed, judged by
the scheduler's `BENIGN_EXITS` table rather than a second copy of it. A job never installed is a
choice, not a finding; no installed job, or not macOS, is SKIPPED — never clean. It exists
because a weekly lint failed on three Mondays running and nothing surfaced it.

| Exit | Meaning |
|---|---|
| `0` | all clear |
| `1` | needs attention, or broken |
| `2` | **a check could not run — not the same as clean** |

Exit 2 is the one that matters: "could not check" reported as "clean" is the failure this whole
tier exists to prevent. Say which check could not run, and why.

Every row that is not `ok` carries a `fix:` line. **`--fix` repairs wiring only** — it
re-registers hooks that are declared and missing from `settings.json`, adding entries and
removing none, so no configuration of yours can be lost. A pending migration, an unpushed commit
and a stray worker each need a person to decide; offer the safe fixes, run none unasked.

### `/gt:gt-optimize`

Memory is read into every session. A duplicated fact is paid for on every turn, in every
project, forever — this finds that waste. It is not `/gt:gt-lint`: lint asks *is the vault
structurally correct*, this asks *is the vault paying for words it does not need*. Different
question, run both.

**Two members since 0.18.1.** It used to ask only what the vault **stores** that every session
pays for. Nothing asked what a **session carries**, which is the larger number: over 180 days of one
machine's transcripts, 98.6% of input came from cache, yet about 65% of what was spent *writing*
the cache was avoidable — mostly sessions left idle past the cache lifetime and rebuilt in full.
`gt_optimize.py` now runs two declared members through the same aggregator as gt-scan and gt-allin,
and always says `N of 2 member(s) ran`:

| Member | Asks | Paid | Script |
|---|---|---|---|
| `vault` | what the vault stores that a session pays for | every session, for ever | `gt_optimize.py --member vault` |
| `session` | prompt-cache writes by cause, from the transcripts | once per resume | `gt_optimize_session.py` |

```bash
python3 $SCRIPTS/gt_optimize.py --vault "<vault>" [--project SLUG] [--json]   # both members
python3 $SCRIPTS/gt_optimize.py --vault "<vault>" --only vault [--cost]        # the vault report
python3 $SCRIPTS/gt_optimize.py --vault "<vault>" --only session --days 90
```

A member that cannot run — no transcripts on this machine, a crash — is printed **COULD NOT RUN**
and the run exits 3, never clean. That includes a bare run on a machine with no transcripts (a CI
runner), which is why `/gt:gt-allin` runs it as `--only vault`. Aggregate exit: `0` clean · `1`
findings · `2` usage · `3` a member could not run.

#### The vault member

Reporting **never writes a byte**. Findings come in two lists: *mechanical* (decidable from the
text — a dead index row, a run of blank lines) and *judgement* (reported, never applied). The
judgement ones were always the valuable ones and they need a reader:

- `duplicate-fact` — the same sentence in two context-loaded files. Decide which one owns it and
  link to it from the other; usually the more specific scope wins.
- `memory-bloat` — a `global-memory/` file over the budget of 30 non-blank lines. Move the
  detail to `Knowledge/`, leave the fact. The finding prints the `--demote` command that acts
  on it.
- `relative-date` — "last week" in a file injected into a session months later. Only flagged in
  silently-loaded files, where it genuinely misleads.
- `duplicate-line` — check whether it is *structure* before removing it. Measured against a real
  vault, the hits were an ADR's required `- **Rejected alternatives**:` label and a line shared
  by two Dataview queries. In prose a repeated line is waste; in a document built from repeated
  structure it *is* the structure, and no script can tell which it is looking at.
- `single-project-global` *(0.18.1)* — a `global-memory/` note whose body names exactly one
  project (whole word): that project's memory, charged to every other project. Same evidence as
  gt-lint's `global-scope-leak`, honouring `lint-declines.md`; the finding names the `--demote`
  command.
- `knowledge-unused` *(0.18.1)* — a Knowledge page with no read in `--unused-days` (default 90),
  "never read" first, then oldest. It reads the Knowledge read log (below); with no log at all it
  reports nothing, because otherwise every page would read "never read" on the day logging began.
  Report-only — nothing is demoted or archived from it.

**`--cost` (0.18.1)** prints what `/gt:gt-open` actually loads per project, in lines and approximate
tokens — README, source, idea, research (costed as gt-open skims it past 200 lines), decisions,
design, spec, runbook and the memory *index*, never the notes — plus the vault total and the
once-per-session `CONVENTIONS.md` and `PROTOCOL.md`. It is always in `--json` as `open_cost`.

**The Knowledge read log (0.18.1).** Git shows when a page was *written*, never whether anyone
*read* it. The hook `log_knowledge_read.sh` (`PostToolUse`, matcher `Read`, installed by
`install.sh`, 5-second budget) appends one line per Read of `<vault>/Knowledge/**.md` to
`<vault>/usage/knowledge.jsonl`:

```json
{"page": "Knowledge/Some Page.md", "session": "1a2b3c4d", "date": "2026-10-01", "trigger": "read"}
```

It writes `usage/.gitignore` on first use, so the log never reaches git; it fails open and silent;
setting `knowledge_access_log` (`on`). It is a hook rather than a line in a skill, because a skill
logs only the reads a model remembers to log. It cannot tell which skill caused a Read
(`trigger` is always `read`), and a read by shell or by a subagent outside the hook is not seen.

#### The session member

`gt_optimize_session.py` reads `~/.claude/projects/**/*.jsonl` and splits every cache write into
four causes:

| Cause | Meaning | Avoidable |
|---|---|---|
| `cold` | the first turn of a stream, or the turn after a compaction | no |
| `growth` | the conversation got longer | no |
| `expiry` | the gap outlived the TTL *that prefix was written at* — 5 minutes or 1 hour, read from the write | **yes** |
| `invalid` | the cached prefix changed | **yes** |

Tokens, and **list-price equivalents** (1.25× for a 5-minute write, 2× for a 1-hour write, 0.1× for
a read) — labelled as equivalents, not a bill; an unknown model is priced at a $3/MTok default and
counted as unpriced. One API request written as several transcript rows is counted once (keyed on
the timestamp, a prototype inflated turns 2.2× and reported invalidation at 50.7% instead of 3.4%);
streams never affect each other; a half-written last line is skipped. Report-only. Output names a
session by 8 characters at most and prints no absolute path, because transcript folder names
encode your home path.

```bash
python3 $SCRIPTS/gt_optimize_session.py [--days N] [--json]
python3 $SCRIPTS/gt_optimize_session.py --brief          # one line: avoidable share, both causes, $ equivalent
python3 $SCRIPTS/gt_optimize_session.py --check [PCT]    # exit 3 when the avoidable share exceeds PCT
```

Settings: `optimize_session_days` (`30`; 7 · 30 · 90 · 180) is the window; `optimize_avoidable_pct`
(`50`; 25 · 50 · 65 · 75) is the `--check` threshold when PCT is left out. Exit `0` nothing
avoidable · `1` avoidable writes · `2` usage · `3` `--check` over threshold · `4` could not run.
When `expiry` dominates, the fix is a habit — cut before stepping away (`/gt:gt-minimize`).

Only files loaded into a session are examined — `global-memory/`, `core-rules/` (read-only), a
project's `memory/` and `CLAUDE.md`. Generated files (`TASKS.md`, `log.md`, `review-queue.md`,
`events.jsonl`, `MANIFEST.json`) are skipped: editing one is pointless because the next run puts
it back, and reporting one buries the findings that matter. `--project` takes a **slug, not a
path**; a path, or a slug resolving outside the vault, is a usage error.

#### `--demote`: moving a note somewhere cheaper (0.16.1)

**There is no deleting `--apply`.** There was one, for about an hour on 2026-09-16, restricted to
a "SAFE" class said to be decidable from the text alone. An independent validation took it apart:
`dead-index-row` deleted seven live rows out of eight in the fixture, blank-run collapsing
rewrote the inside of code fences, findings computed from one read were applied to another by
line index, CRLF files were rewritten to LF and mode `0600` became `0644`, and `--project` was
joined to the vault unvalidated so `../../elsewhere` edited files outside it entirely. It was
**removed rather than repaired**: the SAFE class was a claim about the world, and the world kept
producing exceptions. `--apply` without `--demote`, `--archive` or `--supersede` is a usage error.

Demotion is the shape that *can* be repaired, because **the ladder here is about cost, not
maturity**. `/gt:gt-promote` moves knowledge up by how settled it is; this moves it down by how
often it is paid for:

```
global-memory/            read in EVERY session of every project     most expensive
Projects/<slug>/memory/   read in every session of one project
Knowledge/<page>.md       read when someone asks for it              cheapest
```

A fact in `global-memory/` that only one project needs is charged to every session forever.
Moving it does not make it less true — it makes it cost what it is worth.

```bash
python3 $SCRIPTS/gt_optimize.py --vault "<vault>" --demote global-memory/thing.md
python3 $SCRIPTS/gt_optimize.py --vault "<vault>" --demote global-memory/thing.md --apply
```

Without `--apply` it previews and moves nothing. `--to knowledge|project-memory` overrides the
default of one tier cheaper; `--project <slug>` is required to land in a project's memory.

**The order is the safety property**, and it is the entire reason this is a separate tool rather
than another `--apply` flag:

1. **write** the destination, carrying provenance and a link back,
2. **verify** it by re-reading it from disk and comparing bytes — then re-read *both* sides again
   immediately before touching the source, because the invariant is not "it was there" but "it is
   there now",
3. **only then** replace the source with a pointer to where it went.

A failure at any step leaves **duplication** — the same content in two places, which a reader can
see and resolve. The deleting version failed by removal, which no diff brings back.

It refuses rather than guesses. `core-rules/` and `Sources/` are **never** moved — compared
casefolded and NFC-normalised on the resolved path, never on the caller's spelling, because
`sources/` and `CORE-RULES/` walked straight past a literal comparison on a case-insensitive
volume and a real Core rule was moved. It also refuses a note under a symlink, a path resolving
outside the vault, a destination that already exists (merging two notes is a person's
judgement), a file that is not valid UTF-8, and anything already at the cheapest tier. The
destination is created with `O_EXCL|O_NOFOLLOW` at mode `0600` and then given the source's mode,
so a private note cannot become world-readable and a racing second run cannot leave the note
existing nowhere. CRLF survives; nothing rewrites the index, and the preview says exactly that —
`MEMORY.md` rows keep pointing at the pointer, which is correct and worth tidying.

These refusals live in the script because `guard_protected_paths` is a **PreToolUse hook**: it
sees the model's Write tool and never sees a script writing a file.

#### `--archive` and `--supersede`: subtracting from `research.md` (0.18.1)

Every mechanism gt added put something in; none took anything away. `research.md` only grows, and a
March finding sits above the April entry that corrected it with equal weight. Two actions, both
**previews unless `--apply`**, both through the write queue, neither deleting anything:

```bash
python3 $SCRIPTS/gt_optimize.py --vault "<vault>" --archive --project alpha --before 2026-01-01           # preview
python3 $SCRIPTS/gt_optimize.py --vault "<vault>" --archive --project alpha --before 2026-01-01 --apply
python3 $SCRIPTS/gt_optimize.py --vault "<vault>" --supersede --file Projects/alpha/research.md \
    --entry "2026-03-01: relay idles at 10m" --by "2026-04-02: relay idles at 5m" --apply
```

- **`--archive`** moves `## YYYY-MM-DD…` entries older than the cutoff into
  `research-archive-<YYYY>.md`, leaving one dated index line per moved entry under
  `## Archived entries`. The archive is written first, read back, and only then is `research.md`
  rewritten — and only if it is unchanged since it was read. A failure leaves an entry in both
  places, visible, never in neither. Undated headings are never touched; a file another live
  session has claimed is refused, naming the holder. A remaining `research.md` over 256 KiB cannot
  be rewritten in one queue request — pick an earlier cutoff (the refusal says so).
- **`--supersede`** marks an entry in place with `> superseded_by: [[file#heading]] — marked
  <date>`, the `Sources/` idiom. The entry stays.

`--dry-run` is accepted and is the default for every write action; `--apply --dry-run` is a usage
error. Summarising or rewriting memory is out of scope on purpose.

Exit codes: report — `0` nothing to report · `1` findings · `2` usage · `3` a member could not run.
Demote, archive, supersede — `0` done (or previewed) · `1` refused, and why · `2` usage · `3` an
error mid-move or a write that did not land, with what is where (nothing removed).

### `/gt:gt-settings`

View and change what Golden Thread does automatically. Every automatic behaviour
is registered here and can be switched off.

| Setting | Values | Default | What it does |
|---|---|---|---|
| `component_updates` | `off` · `report` · `confirm` · `auto` | `report` | At session start, compares installed hooks/scripts against plugin source and reports drift |
| `version_check` | `off` · `report` | `report` | At session start, reports when a newer plugin version is checked in than the one installed |
| `orphan_check` | `off` · `report` · `reap` | `report` | At session start, looks for abandoned background Claude workers; `reap` stops them. A worker is judged by who owns it (0.15.0): a shell with a live `claude` process above it belongs to that session and is listed as information — session id, claude pid, uptime, and `WAITING on: <what it polls>` for a poll loop — and is never reaped, even by `reap`. `ORPHAN` means no `claude` remains above it. This session's own undeclared or idle workers are still raised |
| `push_check` | `off` · `report` | `report` | At session start, reports vault commits not yet pushed |
| `surface` | `off` · `on` | `on` | At session start, shows the MUST DO block from `<vault>/deadlines.md` and every waiting handoff each session, and pre-compaction state files once each (0.17.2). Never writes the vault. See [`gt_surface.py`](#gt_surfacepy-what-the-last-session-left-shown-to-the-next) |
| `handoff_surface` | `any` · `project` · `manual` | `any` | Where a handoff that has not been handled is shown (0.17.2): `any` every session start, whatever project is opened; `project` only when `/gt:gt-open` opens the handoff's own project; `manual` only in `/gt:gt-handle handoff`. `project` needs `/gt:gt-open` — a session that never opens the project never sees it, which is why `any` is the default |
| `task_surface` | `off` · `on` | `on` | At session start, one line counting `p:: 1` tasks waiting on you — how many overdue, how many open over a week — with the commands to list and work them (0.17.2). A count, never the tasks; deferred tasks are not counted until their date |
| `unlock` | `off` · `on` | `off` | gt unlock (0.20.1): agents need your presence for LOTR, credentials, publishing and gt's guards. The source of truth is the unlock policy, not vault-config.json: `set unlock on\|off` runs `gt_unlock.py policy enable\|disable`, which needs your enrolled factors. See [Security: gt unlock](#security-gt-unlock-0201) |
| `sandbox_mode` | `off` · `on` | `off` | gt sandbox mode (0.20.1; a **preview**: most skills' shell steps are refused with a one-line next step): writes Claude Code's sandbox and permission rules into `~/.claude/settings.json` so the assistant's shell and file tools cannot write the vault or gt's state (nor read the unlock home, LOTR or locked folders); the vault is reached through the gt-vault MCP tools and the write queue. Native Windows: permission rules only (friction). Restart Claude Code after switching; switch off from a terminal. See [Security: gt sandbox mode](#security-gt-sandbox-mode-0201) |
| `sandbox_vault_reads` | `deny` · `allow` | `deny` | Under sandbox mode, whether the shell and file tools may still READ the vault (writes stay denied; locked folders stay unreadable) |
| `symlink_writes_outside_vault` | `refuse` · `allow` | `refuse` | Whether a vault tool may append through a link that points outside the vault. A link inside the vault is written through; one reaching `design.md` or `global-memory/` never is. Every write `allow` lets through is logged in `Projects/golden-thread/outside-vault-writes.jsonl` (0.20.2) |
| `vault_mcp` | `auto` · `on` · `off` | `auto` | Whether the gt-vault MCP server offers its tools: `auto` = while sandbox mode is on |
| `protected_paths` | `off` · `ask` | `ask` | A Write or Edit to the vault's `core-rules/` or `global-memory/`, to `~/.claude/golden-thread/`, or to `~/.claude/settings.json` always shows the permission prompt; editing an existing file in `Sources/` is refused (supersede it with a new file). Shell commands that write those files are not seen |
| `test_gate` | `off` · `warn` · `auto` · `block` | `auto` | Refuse a `git commit` of code whose tests have not been seen to pass; `auto` blocks only where the repo has a test command |
| `parallel_work` | `off` · `on` | `on` | Whether divisible work runs in parallel at all; `off` also stops the Core rule being injected |
| `parallel_max` | `auto` · a positive integer | `auto` | Ceiling on concurrent workers. `auto` = as many as the machine allows |
| `skeptic_pass` | `off` · `on` | `off` | On its own (since 0.18.1), makes `/gt:gt-work` spawn a zero-context skeptic over the research entries about to land. It does not turn on specialist ingest or validation |
| `agent_specialization` | `off` · `on` | `off` | Hands the agent stages of ingest, promote and gt-work (stage × kind specs, 0.18.1) and `/gt:gt-validate` to specialist agents. Since 0.18.1 it does not affect the gt-work skeptic |
| `brief_absence_days` | `off` · `3` · `7` · `14` · `30` | `7` | Days away from a project (on this machine) before `/gt:gt-open` leads with the generated catch-up brief; `off` = only on `--brief` (0.18.1). A value outside the list reads as `7` |
| `foreign_checkout_guard` | `off` · `on` | `on` | Deny `git commit`/`git push` inside a checkout you declared as another machine's (0.18.1). Inert while nothing is declared. See [Foreign checkouts](#foreign-checkouts-a-commit-in-another-machines-checkout) |
| `decision_signals` | `default` · `off` · `;`-separated `+phrase`/`-phrase` edits | `default` | The phrases gt-lint's `decision-candidate` check looks for in `design.md` and `research.md` (0.18.1) |
| `knowledge_access_log` | `off` · `on` | `on` | Log each Read of a `Knowledge/` page to `<vault>/usage/knowledge.jsonl` (local, never committed) for gt-optimize's `knowledge-unused` (0.18.1) |
| `optimize_session_days` | `7` · `30` · `90` · `180` | `30` | Window, in days, of gt-optimize's `session` member (0.18.1) |
| `optimize_avoidable_pct` | `25` · `50` · `65` · `75` | `50` | `gt_optimize_session.py --check` with no PCT exits 3 above this avoidable share (0.18.1) |
| `memory_contradiction_check` | `off` · `on` | `on` | Whether `/gt:gt-work` checks the memory notes it wrote against their neighbours for contradictions (0.18.1) |
| `promotion_candidates` | `off` · `on` | `on` | Whether `/gt:gt-work` lists the promotion candidates a script can see (0.18.1) |
| `promotion_overlap` | `60` · `70` · `80` · `90` | `80` | Word overlap (%) at which two projects' `research.md` sections become a `Knowledge/` candidate (0.18.1) |
| `review_stamp` | `off` · `on` | `on` | Stamp `last_reviewed` when `/gt:gt-query` or `/gt:gt-open` reads a Knowledge page (0.18.1) |
| `sync_check` | `off` · `cached` · `fetch` | `off` | At session start, say whether the vault is behind its upstream — from the refs on disk, or after one 4-second fetch (0.18.1) |
| `reminder_days` | `1` · `3` · `7` · `14` · `30` | `7` | Reminders cover overdue items plus those due within this many days (0.18.1) |
| `reminder_macos` | `off` · `on` | `off` | macOS notification from the reminder job (0.18.1) |
| `reminder_relay` | `off` · `sms` · `discord` | `off` | Send reminders to your notification relay (`reminder/relay.json`, mode 600) (0.18.1) |
| `reminder_email` | `off` · `on` | `off` | One reminder email over SMTP (`reminder/email.json`, mode 600) (0.18.1) |
| `commit_checks` | `off` · `on` | `off` | Refuse a commit whose staged content no passing `gt_check.py` run covers (0.18.1) |
| `addon_fixes` | `off` · `propose` · `apply` | `propose` | What happens to a checker's fix proposals; only first-party checkers auto-apply (0.18.1) |
| `addon_fix_size_limit` | `1k` · `4k` · `16k` · `64k` · `256k` | `16k` | Most bytes one fix may add or remove (0.18.1) |
| `execution_metrics` | `off` · `on` | `on` | Record one row per execution of tests, release-pipeline steps and gt skills (`gt_metrics.py`); `off` records nothing anywhere (0.18.1) |
| `scoped_receipts` | `off` · `on` | `on` | On a feature branch, a commit may rely on a scoped receipt from `tests/run.sh --affected`; the default branch and release gates always need a full-suite receipt (0.18.1) |
| `test_tmpdir` | `off` · `noindex` | `off` | Where the test runner puts throwaway files; `noindex` = `~/Library/Caches/gt-tests.noindex` on macOS, `$XDG_CACHE_HOME/gt-tests` elsewhere (0.18.1) |
| `agent_models` | `task` · `session` | `task` | Each specialist agent's model set by its task (haiku/sonnet/opus by tier), whatever the skill profile; `session` passes no model (0.19.1). Since 0.20.1 the stage agent definitions carry model and effort, and changing this rewrites them at once. See [Model and effort profiles](#model-and-effort-profiles-gt_model_policypy-0191) |
| `vault_hints` | `off` · `on` | `off` | Up to three vault page titles relevant to each prompt, from `index.md` only, never a page body (0.19.1). See [Recall benchmark and prompt hints](#recall-benchmark-and-prompt-hints-gt_keyword_recallpy-0191) |
| `allin_timeout` | `300` · `600` · `1200` · `1800` · `3600` | `300` | Seconds each all-in check may take, in `/gt:gt-allin` and the commit gate (0.19.1); the gate gives the whole run twelve times this |
| `runners` | empty · comma-separated ssh aliases | empty | Remote hosts that may run tests and calibration: `dev/remote-test.sh`, `prun.py --hosts`, `gt_bench.py --hosts` (0.18.1) |

**Settings that come from modules.** Since 0.15.0 a module declares its own settings in
its `module.json` — `key`, `default`, `values`, `summary`, and an optional long `detail`
that `gt_settings.py explain <name>` prints. They are listed under `module <name>` in
`show` and exist only while the module is installed. A value you set for a module that is
later switched off is kept and shown as *OFF — settings kept, not in effect*; it takes
effect again after `bash install.sh --with <module>`.

| Setting | Module | Values | Default | What it does |
|---|---|---|---|---|
| `report_card` | `report-card` | `off` · `minimal` · `full` | `minimal` | At `/compact` and session end, summarises session hygiene; the card is shown at the start of your next session, because output at those two events is never displayed |
| `closeout_check` | `report-card` | `off` · `ask` | `ask` | At `/compact` and session end, names a project that looks finished so you are asked whether to close it |
| `usage_meter` | `usage` | `off` · `status` · `login` · `both` | `both` | Where the plan-allowance meter shows. `off` still records readings, so turning it back on has a history behind it |
| `usage_alert` | `usage` | `early` · `normal` · `late` · `always` | `normal` | How near a ceiling the allowance must be before the meter speaks — or `always`, to keep it on screen permanently |
| `watch` | `watch` | `off` · `report` | `off` | `/gt-watch:gt-watch`: the cron fetch and the session-start report of repo changes |

`report_card` (detail):

```
off     nothing
minimal hygiene only -- what went wrong in THIS session  (default)
full    hygiene, plus vault features available and unused

Fires on PreCompact so it is produced while there is still context to write
it in, rather than competing for the last of it at session end.
```

`closeout_check` (detail):

```
off     nothing
ask     name each project whose signals say it may be finished, with the
        reasons, so the assistant puts the question to you  (default)

Runs the vault's own `Projects/golden-thread/tools/gt_closeout.py`. Signals:
most open tasks past due, most tasks checked off with nothing urgent left,
no new task and no write-back for three weeks, or no open task at all. Every
time the question is put to you it is recorded with the signal values, and
your answer is recorded beside it, so `gt_closeout.py history` can show what
'ready to close' has actually looked like for you and the thresholds can be
tuned to that rather than to a guess.

On 2026-09-05 the CYC26 talk had been delivered for two days while 25 of its
rehearsal tasks sat open and overdue at the top of the rollup, above live
trading work. Nothing had asked.
```

`watch` (detail):

```
off     no fetching and no report  (default)
report  the cron fetch runs, and session start lists unacknowledged upstream
        changes: P0s first with repo and reason, review items one line each,
        routine changes as a count

A watch is a note in Projects/golden-thread/watches/. The fetch runs from cron
(gt_watch.py install-cron), never from a session; the session-start hook only
reads the local queue in ~/.claude/golden-thread/watch/. P0 comes from fixed
rules -- a security advisory, a CVE or GHSA id, 'security fix' in a release --
never from a reading of commit prose, because a P0 that cries wolf stops being
read. GT_WATCH=off|report in the environment overrides this setting.
```


> **Heads-up:** `parallel_work` is `on` and `parallel_max` is `auto` out of the box, so
> after upgrading to 0.12.4 a long run will use as much of your machine as it can — many
> processes at once, CPU well above 100%, and the fans to match. That is intended. Cap it
> with `set parallel_max <N>` or switch it off with `set parallel_work off`; nothing else
> about the plugin changes.

**Test receipts.** `core_test_before_commit` needs evidence that the tests passed on
*this* tree, so a run leaves a receipt and the commit guard reads it:

```bash
gt_test_receipt.py record --repo . --what "pytest -q" --ok   # any project
gt_test_receipt.py latest --repo .                           # what was last recorded
gt_test_receipt.py check  --repo . --files a.py b.py         # does a receipt cover these?
```

A receipt covers a file only if it is newer than that file, so editing something after
the run invalidates it automatically. `tests/run.sh` and `dev/release-check.sh` record
their own. A repo with no tests is exempted once with `touch .gt-no-test-gate`; a single
commit with `GT_TEST_GATE=off`.

**The machine profile.** `parallel_max: auto` resolves through `parallel_profile`, which
`install.sh` measures on this machine and re-measures at every upgrade — cores for
CPU-bound work, 2× cores (bounded by memory) for I/O-bound. Inspect or refresh it:

```bash
gt_settings.py detect-machine            # what this machine would measure
gt_settings.py detect-machine --write    # measure and store it
```

Setting a number above what the machine can use is refused at the point you set it, and
a number above core count is accepted with a note that it only helps I/O-bound work.

**Measuring it instead (0.18.1): `gt_bench.py`.** The install's 2× cores for I/O was never
measured, and on the machine that prompted this the cores were not the limit at all — load 28 on 16
cores from the scanner, the indexer and the sync daemon before a test ran. `gt_bench.py` runs a
short, bounded calibration (180 s by default): a synthetic I/O workload (file churn plus a
subprocess per unit) and a CPU workload at several widths, the median of several runs each, and
takes the **knee** — the smallest width reaching 90% of the best throughput — as `io_max` /
`cpu_max`. It writes `parallel_profile` with `source: measured` and both curves, reports load before
and during and names the busiest other processes (never touching them). install.sh keeps a measured
profile while the core count and host name are unchanged.

```bash
python3 $SCRIPTS/gt_bench.py [--dry-run] [--json] [--widths 1,2,4,8] [--units N] [--repeat R] [--budget S] \
    [--location DIR] [--hosts [a,b]]
python3 $SCRIPTS/gt_bench.py health [--json]     # the rows gt-doctor's `execution` check shows
```

`--location DIR` compares I/O there with the temp dir; `--hosts` (the `runners` setting, or a list)
calibrates each ssh runner, sending only this script on stdin. Exit `0` measured · `1` nothing
measurable within the budget · `2` usage. The workload is always synthetic — not your own test
command. `gt_load.py [--cap N] [--json]` shows what a parallel runner would start with right now, and
why: the ceiling scaled down by load, memory pressure and other parallel runs.

**The budget as a number.** Any script, in any project, can ask what the settings allow
instead of inventing a worker count:

```bash
JOBS=$(python3 .../scripts/gt_settings.py jobs 42 --io-bound)   # 42 units of I/O-bound work
JOBS=$(python3 .../scripts/gt_settings.py jobs 8)               # 8 units, CPU-bound
```

It returns 1 when `parallel_work=off`, never more than `parallel_max`, and never more
workers than there are units of work. `gt_settings.parallel_jobs()` is the same answer
from Python.

```bash
python3 $SCRIPTS/gt_settings.py show            # current state of all settings
python3 $SCRIPTS/gt_settings.py explain <name>  # reasoning behind the default
python3 $SCRIPTS/gt_settings.py set <name> <value>
```

`component_updates` at `auto` will not overwrite an installed file that is newer
than the source, and will not delete a file the source lacks — real drift ran
that direction, so a naive "source is truth" updater would silently break
enforcement on every session start.

### `/gt:gt-runbook-lint`

Finds procedures duplicated across runbooks and routes them to the right shared
layer — `PROTOCOL.md`, a Knowledge page, or a repo `CLAUDE.md`.

### `/gt:gt-scan`

Check a tree against the language definitions **in effect on this machine**, and say which
checks ran. What it can check, per language, is in
[What `/gt:gt-scan` can check, and how to ask](#what-gtgt-scan-can-check-and-how-to-ask); this
is about operating it.

```bash
python3 $SCRIPTS/gt_scan.py <path> [--vault "<vault>"] [--only language] [--json]
python3 $SCRIPTS/gt_scan.py --list          # the members, and whether each is installed
python3 $SCRIPTS/gt_scan.py <path> --all-files
```

The vault is optional — without one you get the shipped definitions and no local packs.

**It is an aggregator over leaf commands**, and owns no checking logic. There are two members:

| Member | Script | Checks |
|---|---|---|
| `language` | `gt_scan_language.py` | naming conventions and file encoding, per language |
| `code` | `gt_scan_code.py` | **source validation** — code against the `lint` rules in effect |

A member is listed only when it exists: a planned-but-unbuilt leaf must never appear, because a
listed-but-missing member reads as coverage. `--only` narrows the run, `--all-files` widens it,
and `--timeout` bounds a member. `code` has its own section under
[Checks and cadences](#gt_scan_codepy-source-validation) — including the rule that a rule whose
**evaluator tier** is absent is reported SKIPPED and never silently passed, which is the same
rule as "a member that could not run is not a pass", one level down.

**The denominator is the declared set.** It prints `running N scan member(s)` before it starts —
composition is never a surprise — and a `N of M member(s) ran` line after. A member that is not
installed, times out, crashes, or exits for any reason other than clean-or-findings is reported
as **not having run**, never dropped from the count, and sets exit `3` even when another member
succeeded. Read that line *before* the finding count: "nothing is wrong" and "nothing was
checked" print identically otherwise. **A member that could not run is not a pass.**

| Exit | Meaning |
|---|---|
| `0` | all members clean |
| `1` | findings, and every member ran |
| `2` | usage |
| `3` | **a member could not run** |

The leaf has one more, which reaches you as an aggregator `3`: exit `4`, *nothing to scan with*.
`gt_scan_language.py` **refuses to scan** when not one definition resolved, rather than walking
the tree and reporting nothing — "nothing matched" and "nothing was looked for" are the same
output otherwise. When that happens, `gt_registry.py sources` says which packs are missing or
failing to verify. Running the leaf directly is worth knowing for the same reason:

```bash
python3 $SCRIPTS/gt_scan_language.py <path> [--vault "<vault>"] [--only encoding,naming]
```

It prints `definitions in effect: encoding=N, naming=N` — the count it actually loaded, not the
count it hoped for.

**"Found but not checked" is a gap, not coverage.** A construct the scan **locates** (a
`construct` pattern) and then compares against nothing (no `naming` rule) produces no findings
and looks exactly like a clean language. `--languages` gives it its own column and a total —
*`N` construct(s) are located and then never checked* — so the gap is stated rather than
discovered. Never report a language as checked on the strength of it appearing in the table:
read the last column.

**A noisy rule is a definition disagreement, not a bug.** The scan prints the exact `retract`
snippet at the moment the rule annoys you, keyed on the finding's own language. It prints and
never writes: a tool that silently edited the pack directory could switch off its own checks,
and that directory is guarded to `ask` so a person sees such a change happen. The snippet goes
in a pack in **your own vault**, and only local packs may retract — see *Switching a definition
off* under [Packs and the registry](#packs-and-the-registry). Never write one for someone
without asking; switching off a check is theirs to decide.

Generated, vendored and static files are skipped (the `classify` slot decides which), as are
paths in `ignore`, binaries, and anything over 2 MiB. `--all-files` includes the skipped kinds
and will produce findings nobody owns — use it deliberately.

**This scan prints source text** — an identifier *is* the finding — so do not point it at
content where the text itself is the sensitive thing. Credential scanning is deliberately a
separate tool, and **`gt_secrets.py` is now that tool**: it is not a member here and must not
become one. It lived here once, and a validation on 2026-09-16 found it missed a `.env` file
entirely, had no generic `KEY=`/`SECRET=` rule, and leaked the very values it redacted, because
the naming check printed raw source text on the same run. A secret scanner that misses is worse
than none, because people stop looking. Two tools with opposite output rules in one process is
the whole reason for the split — see
[`gt_secrets.py`](#gt_secretspy-credentials-in-the-wrong-place). If you are checking a tree for
credentials, run that; `/gt:gt-scan` does not cover them, and `gt_sweep.py` runs both.

Findings are advice about conventions, not defects. Do not "fix" a repo's naming wholesale
because a pack disagreed with it.

### Resuming an interrupted scan or ingest (0.18.1)

Scans and ingests save their place as they go. Interrupted by a context limit, a cancel or a crash,
the next `/gt:gt-scan` or `/gt:gt-ingest` on the same target asks "A previous scan was interrupted at
item N of M. Resume it?" — **from any session**, not only the one that was interrupted. Resuming
skips the scan members already done (and, in the `language` member, the files already scanned), or
the ingest candidates already migrated, and the final report merges the earlier results with the
new. A run without `--resume` starts fresh. Checkpoints live in the vault spool
(`Projects/golden-thread/spool/<tool>/`, else `~/.claude/golden-thread/spool/`), are deleted when a
run completes, and abandoned ones are pruned after 7 days (at every session registration).

```bash
python3 $SCRIPTS/gt_checkpoint.py find  --tool scan|ingest|scan-language [--target PATH] [--vault V] [--json]
python3 $SCRIPTS/gt_checkpoint.py show  FILE
python3 $SCRIPTS/gt_checkpoint.py prune [--vault V] [--days 7] [--dry-run]
python3 $SCRIPTS/gt_scan.py <path> --resume CHECKPOINT      # or --no-checkpoint to write none
python3 $SCRIPTS/gt_ingest.py <dir> [--vault V] [--no-checkpoint]
python3 $SCRIPTS/gt_ingest.py --resume CK [--json] | --done CK --index N [--result TEXT] | --status CK
```

A leaf interrupted mid-walk stops the scan with exit 75, so the resume continues inside that
member. A resumed language scan refuses ("the tree changed under the files it had done") when those
files are no longer the first of the walk — it never guesses. The `code` member resumes per member,
not per file: its ast-grep pass runs once over the whole tree. `GT_CHECKPOINT_ABORT_AFTER=N` stops a
tool after N items with the checkpoint kept, to rehearse a resume.

### `/gt:gt-allin`

Every check in one command, and an honest account of which ones executed.

```bash
python3 $SCRIPTS/gt_allin.py --vault "<vault>" --repo "<repo>"
python3 $SCRIPTS/gt_allin.py --list              # the members, and whether each is installed
python3 $SCRIPTS/gt_allin.py --only scan,lint    # narrow the run
```

It is an **aggregator**, like `/gt:gt-scan`, and owns no checking logic: every member is a
command that runs and is tested on its own. The moment the aggregator starts deciding things
itself there is a third behaviour nobody tests.

| Member | What it checks | Needs |
|---|---|---|
| `scan` | code against the language definitions in effect | `--repo` |
| `lint` | vault structure: links, orphans, index gaps | `--vault` |
| `optimize` | vault content that costs context — **report only, never applied here** | `--vault` |
| `doctor` | install health: versions, drift, wiring | — |
| `secrets` | credential-shaped values in the tree — names, lines, lengths, never values (0.17.2) | `--repo` |
| `runbooks` | runbook lines duplicated across projects, `gt_lint.py --runbooks` (0.17.2) | `--vault` |
| `wiki` | wiki pages: broken links, orphans, stale and unsourced pages — gt-wiki's `wiki_lint.py`, **only while the gt-wiki module is installed** (0.17.2) | `--vault` |
| `tests` | the repo's own test suite, found through the commit guard's discovery table (`tests/run.sh`, pytest, cargo, go, `npm test`, `make test`); a pass records the receipt `/gt:gt-allin-commit` checks (0.17.2) | `--repo` |
| `validations` | files changed since their recorded validation, `gt_validation.py list` (0.17.2) | `--repo` |

`tests` and `validations` came last (owner, 2026-09-28: *"add all the other checks into
allin"*). A repo with no discoverable test command reports `tests` as **could not run**, never
as clean. One check is deliberately NOT a member: `gt_code_review.py` plans a review for a model
to perform and finds nothing by itself, so as a member it could only ever report "ran, clean" —
a pass for work nobody did.

The last three existed and ran nowhere "everything" was run. `gt_secrets` in particular had no
aggregator home at all, because it is deliberately not a `/gt:gt-scan` member — a credential
finding must never share a report with findings that print source text. `wiki` joins the roster
only while its module is installed: someone who switched the wiki module off must not be told
"could not run" on every run for a check they chose not to have. So the roster is six members,
seven with gt-wiki.

**The headline is *N of M members ran*, and the denominator is the declared set.** A member that
could not run — not installed, or missing the `--repo` or `--vault` it needs — is **reported,
never silently skipped**, because a short finding list from a run where half the members failed
reads exactly like a clean bill of health and is the opposite. "A member could not run" outranks
"a member found something", so report what did not run *first*, then the findings, then a
judgement. **A member that could not run is not a pass.**

Exit codes: `0` all clean · `1` findings, every member ran · `2` usage · `3` **a member could
not run**.

**It never pushes and never applies anything.** There is no flag that makes it push. Push is
outward-facing and effectively irreversible — other people fetch it, CI acts on it — and an
aggregator is the one place a partial run is easily mistaken for a complete one, so a command
that pushed after a run where a member failed would break the Core rule it exists to enforce.
`--suggest-push` prints the command *for you to run*, and refuses even that unless every
**declared** member ran clean: narrowing the run with `--only` is itself enough to withhold the
suggestion, because "everything that ran was clean" was true and misleading while never saying
how few ran. `optimize` runs in report mode only, for the same reason — a sweep that edits files
as a side effect of "checking everything" changes content without anyone deciding to.

All-in tells you the state; it does not change it. Fix findings with the owning skill, run the
tests yourself, and push yourself.

### `/gt:gt-allin-commit`

The deliberate act, kept separate from the sweep. `/gt:gt-allin` reports and changes nothing, so
it can be run freely by anyone at any time; this one changes the repository.

```bash
python3 $SCRIPTS/gt_allin_commit.py --repo "<repo>" --vault "<vault>" -m "your message" --dry-run
python3 $SCRIPTS/gt_allin_commit.py --repo "<repo>" --vault "<vault>" -m "your message"
```

`--dry-run` runs every check and commits nothing — the safe first move, always. It commits **the
index, not the working tree**; nothing staged is a refusal, not a no-op. The staged file list is
printed *before* the verdict.

Since 0.19.1 `--timeout S` sets how long each check may take (default: the `allin_timeout`
setting, else 300 s; the whole run gets twelve times that), and the gate runs gt-allin with
`--skip tests`: the test receipt it checks itself is the evidence the suite passed, so the suite is
not re-run on the committing machine. `gt_allin.py --skip NAME` names what it left out. The code and
naming scans skip a plugin's release folders older than its newest two (secrets still scan them).

**It commits but will not push.** A commit is local and reversible — `git reset` undoes it and
nobody else ever saw it. A push is outward-facing and effectively permanent. That asymmetry is
the whole line, and there is no `--push` flag. (After a successful commit it checks whether HEAD
is still ahead of its upstream, and says so if not: `git commit` runs the repository's own hooks,
and a post-commit hook can push, so a flat "NOT pushed" would be a claim this tool is not in a
position to make.)

**It checks the test receipt itself**, rather than trusting the hook. `guard_test_before_commit`
already denies a commit with no passing receipt — but it is a **PreToolUse hook**: it sees the
model running `git commit` in a Bash call and never sees a *script* running it through
subprocess. A script that shelled out to git would walk straight past the one mechanism enforcing
Core rule 4, so the same check is made here, directly, against the same ledger. **Protection
cannot depend on who is holding the pen.**

It refuses, naming every reason at once, for any of:

| Refusal | Way past it |
|---|---|
| a check **could not run**, or crashed rather than reporting | none — an unknown is not a finding, and `--allow-findings` cannot wave it through |
| the checks reported findings | `--allow-findings`, once *you* have decided they are acceptable |
| **no passing test receipt covers every staged file** | run the tests. Editing a file after a run invalidates the receipt, which is the point |
| you are on the default branch | branch first, or `--allow-default-branch`. Compared casefolded, against the repo's own default where it has one |
| a merge, cherry-pick, revert or rebase is **in progress** | finish it with git yourself, rather than letting this conclude it with a message written for an ordinary commit |

Exit codes: `0` committed · `1` refused, and why · `2` usage · `3` a check could not run, or the
commit itself failed.

Never pass `--allow-findings` on someone's behalf — accepting a finding is a judgement about
this specific change. Never work around a missing receipt; run the tests, because "I ran them"
is the claim the rule was built to stop trusting. When reporting a successful commit, **say
which tests the receipt covers**. The refusals are the feature.

---

## Default release gates: what your project passes before it reaches its repository

This is what gt does **by default** when code in a project you work on with gt goes out to its
repository. Each numbered box is a gate: what it checks, what fails it, and whether it runs by
itself or only when you run it. Push is never one of them — gt does not push.

![Default release gates: write the code; gate 1, tests seen to pass; gate 2, /gt:gt-allin; gate 3, /gt:gt-allin-commit; gate 4, the automatic commit guard; then your own push and the /gt:gt-work write-back, with the Stop validator and the vault write guard running on every turn](docs/release-process.svg)

1. **Tests seen to pass** (you run them) — the project's own tests; a pass records a receipt, and editing a file afterwards voids it for that file.
2. **`/gt:gt-allin`** (optional) — code scan, credentials, vault lint, install health, runbooks, tests and stale validations; exit 1 on findings, exit 3 when a check could not run, and it changes nothing.
3. **`/gt:gt-allin-commit`** (optional) — runs the same checks, then refuses (exit 1) on findings, a check that did not run, a staged file no receipt covers, or `main`/`master`; it commits but never pushes.
4. **Commit guard** (automatic, `core_test_before_commit`, `test_gate auto`) — denies any `git commit` a session runs when code is staged, the repo has a test command, and no passing receipt is newer than every staged file. Docs-only commits pass; `.gt-no-test-gate` exempts a repo.

Then **you push**, by hand: `gt-allin --suggest-push` prints the command only when every check ran
clean. `/gt:gt-work` writes the findings back to the vault through the write queue. On every turn
the Stop validator blocks a reply that prints a credential or has no timestamp, and the vault
guard denies a direct write to a vault note. Not on by default in your project: the pre-commit
credential hook and `Session-Edit:` trailers live in the vault repo's own `.githooks/`.

Each gate exists because the step it covers was once left to memory and went wrong: a commit
went out with a stale file after its tests had been run before the last edits, which is why the
receipt is tied to file times rather than to anyone's word.

### Release pipeline: `gt_pipeline.py`

*New in 0.18.1.* A project that ships code gets a **release pipeline**: the steps a release must
pass, in order, as data in `release-pipeline.tsv` in the code root (id, `step` or `gate`, a one-line
guarantee, the command, what it runs after, required or optional, `default` or `user`), and a
`release.sh` **generated** from it. It stops at the first failed required step and always ends with
how many steps ran and which; a step exiting 99 is SKIP-not-applicable, never a pass. Every miss in
gt's own 0.17.11 release was a step that lived in someone's head — this is gt's `dev/publish.sh`
model, steps as data and no skip, for every project.

| Default step | Guarantees |
|---|---|
| `@tests` | the suite passes and records a receipt |
| `@allin` | `/gt:gt-allin` ran clean |
| `@branch` | not on the default branch |
| `@install` | install plus post-install validation — **fails** in a repo with an `install.sh` until its real command is declared (`set install --cmd …`): a gate that cannot run is not a pass |
| `@owner-gate` | stops until `--go`: after the owner says go, `release.sh --from owner-gate --go` |
| `@push` | the push |
| `@sync` | a downstream copy — not applicable until one is declared |

```bash
python3 $SCRIPTS/gt_pipeline.py init   --repo R [--project SLUG] [--dry-run]   # adopt it; never overwrites
python3 $SCRIPTS/gt_pipeline.py list   --repo R
python3 $SCRIPTS/gt_pipeline.py check  --repo R            # cycles, missing commands, release.sh current
python3 $SCRIPTS/gt_pipeline.py add    <id> --repo R --cmd CMD [--kind gate] [--after ID] [--guarantee T] [--optional]
python3 $SCRIPTS/gt_pipeline.py set    <id> --repo R --cmd CMD
python3 $SCRIPTS/gt_pipeline.py remove <id> --repo R --reason TEXT   # a default gate needs a reason, recorded
python3 $SCRIPTS/gt_pipeline.py render --repo R
python3 $SCRIPTS/gt_pipeline.py run    --repo R [--until ID] [--from ID] [--go]
python3 $SCRIPTS/gt_pipeline.py flag   --vault V (--project SLUG --value yes|no|planned | --all-missing) [--dry-run]
./release.sh [--list] [--dry-run] [--until ID] [--from ID] [--go]
```

`init` in a repo that already has its own `release.sh` leaves it alone and names the generated one
`release-pipeline.sh`. A project says whether it has one in its README frontmatter,
`release_pipeline: yes | no | planned`; `/gt:gt-create` asks whether a project ships code and
offers it (`vault_init.py create-project --release-pipeline yes --project-dir <code root>` scaffolds
it and records the path in `source.md`). gt-lint's `release-pipeline` check flags a project with
code marked `no`, or `yes` with no `release.sh`; `/gt:gt-upgrade` records `planned` where the key is
missing. `/gt:gt-allin` runs a `pipeline` member in a repo that adopted one (`release.sh --until
owner-gate`), and `/gt:gt-allin-commit` refuses on a failed pre-commit step even with
`--allow-findings`. Exit `0` every required step passed or did not apply · `1` stopped at a failure.

**Recipes: `gt_recipe.py`.** `release.sh` is not written by hand: `gt_recipe.py` renders it from the
steps table through one tested template (dependency order, ok/FAIL/SKIP per step, stop at the first
required failure, the count of what ran, `--until`/`--from`/`--list`/`--dry-run`). A script recipe
(`*.recipe`) renders a plain script whose step bodies are code — gt's own `dev/copygt.sh` is
generated from `dev/copygt.recipe`. `check` fails on a hand edit.

```bash
python3 $SCRIPTS/gt_recipe.py render <recipe> [--out FILE] [--dry-run]
python3 $SCRIPTS/gt_recipe.py check  <recipe> --out FILE
python3 $SCRIPTS/gt_recipe.py show   <recipe> [--json]
```

### Execution metrics: `gt_metrics.py`

*New in 0.18.1.* Every "it would be faster if" was a guess. `gt_metrics.py` records one row per
execution — project, process, kind, start, duration, exit, units, parallelism, reruns, tokens where
known, machine, commit, a hash of the execution settings, scope. Arguments are hashed and never
stored, and a string shaped like a credential is stored only as its hash. Rows live per project in
`<vault>/Projects/<slug>/metrics/<machine>-<YYYY-MM>.jsonl` (one file per machine per month, so
synced machines never append to one file), or under `~/.claude/golden-thread/metrics/` for a project
the vault does not have. Already instrumented: every release-pipeline step and the whole pipeline,
`tests/prun.py`, `dev/remote-test.sh` and `/gt:gt-allin`; `time -- <cmd>` wraps anything else.
Setting `execution_metrics` (`on`).

```bash
python3 $SCRIPTS/gt_metrics.py record --process ID --duration S --exit N [--project P] [--units N] [--parallel N] [--tokens N] [--scope full|scoped]
python3 $SCRIPTS/gt_metrics.py time   --process ID [--project P] -- <command ...>
python3 $SCRIPTS/gt_metrics.py report [--project P] [--json]     # exit 1 on a regression
python3 $SCRIPTS/gt_metrics.py peers  [--kind K] [--json]
python3 $SCRIPTS/gt_metrics.py mark   --process ID --change TEXT [--rollback CMD]
python3 $SCRIPTS/gt_metrics.py verify --process ID [--project P] [--json]   # exit 1: the change did not pay
```

Process ids are `<kind>:<name>` (`tests`, `release`, `build`, `deploy`, `skill`, `agent`, …); the
kind is what makes processes comparable across projects. Each process has a rolling baseline — the
median and p90 of its last 20 passing runs — and a newest run above max(p90, 1.25 × median) is a
**regression**, reported with what changed since (commit, settings, scope, parallelism, machine).
`peers` compares like processes across projects per unit and says what the cheaper one does
differently. `mark` records an applied optimisation with its rollback; `verify` says whether it beat
the baseline.

**The `execution` member of gt-optimize** — `gt_optimize.py --vault V --only execution [--project P]
[--repo R]`, or the alias `--execution` (also `gt_optimize_exec.py`). It is **opt-in**: a bare
`gt_optimize.py` still runs `vault` and `session`. It reads the metrics, the runner's per-unit
timings and the profile, and reports — ranked by measured cost, with the expected saving — serial
bottlenecks, the full suite run several times a day where a scoped run would do, slow pipeline steps
and test units, install-heavy units, regressions, much cheaper peers, defaults never revisited and
hand-written step scripts a recipe could generate. gt's own findings are labelled
`gt-development`. `--apply ID`, after your yes, makes one finding's change and marks it for
`verify`. Exit `0` nothing found · `1` findings · `2` usage.

### Foreign checkouts: a commit in another machine's checkout

*New in 0.18.1.* A checkout in a shared folder can belong to another machine — it pushes from
there, with credentials that exist only there. On 2026-09-11 a session asked to "push everything"
committed a release into such a checkout, then, when the push failed for want of a credential never
meant to be on this machine, began rewriting the remote URL to get it through. The rule had been
written down three times. Prose cannot fire at the moment of the mistake, and that moment looks
like an ordinary authentication problem.

So a `PreToolUse` guard, `guard_foreign_checkout.sh` (installed by `install.sh`, beside the
protected-paths and commit guards; tied to no Core rule), **denies `git commit` and `git push` whose
directory is inside a checkout you have declared foreign**, naming the checkout, who owns it, and
the supported route. Ownership is **declared**, never inferred from a path, a remote, a hostname or
a credential:

```bash
GUARD=~/.claude/golden-thread/hooks/guard_foreign_checkout.py
python3 $GUARD add /path/to/checkout --label "the build machine" --route "run copygt.sh on that machine"
python3 $GUARD list
python3 $GUARD remove /path/to/checkout          # add and remove accept --dry-run
```

or by hand in `~/.claude/vault-config.json`:
`"foreign_checkouts": ["/abs/path", {"path": "/abs/other", "label": "…", "route": "…"}]`. A path
covers every directory inside it, after `realpath`, so a symlink cannot slip past.

- **Untouched:** read-only git (`status`, `log`, `diff`, `fetch` …), every other command, and file
  writes. With nothing declared it denies nothing.
- **Seen:** `git -C <dir>`, `cd <dir> && git …`, subshells, and Claude Code's
  `-m "$(cat <<'EOF' …)"` commit form.
- **Fails open** (allows, says nothing) on what it cannot analyse with certainty — an unquoted
  `$(…)` or backticks, `eval`, `sh -c`, `cd $VAR`, unbalanced quotes, `--git-dir`/`--work-tree` —
  and on a malformed config or a declared path that does not exist. A push run through a script or
  an alias (`./release.sh`, `gp`) is not seen: only literal `git commit`/`git push` command lines.
- **One deliberate exception**, visible in the transcript: `GT_FOREIGN_CHECKOUT=allow git push …`.

Setting `foreign_checkout_guard` (`on`) switches it off entirely. An install that predates 0.18.1
shows it `unwired` in `/gt:gt-doctor` until `install.sh` runs.

---

## Checks and cadences

Three cadences run the same checks over deliberately different file sets, and the difference is
the point:

| Cadence | What it looks at | What it does about it |
|---|---|---|
| `tests/run.sh` | the file set the scanners define | **fails the run** |
| the commit gate | the **staged diff** only | **denies the commit** |
| `gt_sweep.py` | the **whole tree**, weekly | **reports**, never blocks |

The narrow two are what keep the gates usable. A commit gate that scanned the whole tree would
punish you for someone else's old code, so it looks only at what you are adding — but that means
nothing ever re-examines what is already there. A rule added today never sees a file nobody
touches, and a credential committed before the gate existed stays committed. The sweep is the
cadence that goes back over it, and it reports rather than blocks because there is no commit in
front of it to refuse.

### `gt_secrets.py` — credentials in the wrong place

```bash
python3 $SCRIPTS/gt_secrets.py <path> [--vault "<vault>"] [--staged] [--json]
python3 $SCRIPTS/gt_secrets.py <path> --baseline .gt/secrets-baseline.json
python3 $SCRIPTS/gt_secrets.py <path> --write-baseline .gt/secrets-baseline.json
```

**Its output contract is the reason it is a separate tool and not a `gt_scan` member:**

> Nothing in this process ever prints matched source text, and no other check runs beside it.

A finding is a path, a line, a rule id and a length. Never an excerpt, never a prefix, never a
hash. Every other Golden Thread scanner prints the text it found, because there the text *is*
the finding; here the text is the thing being protected. Those are opposite output rules, and
two tools with opposite output rules in one process is exactly how the 0.16.0 attempt leaked —
its `secrets` check printed only a length while its `naming` check, same tool and same run,
printed raw source text, so a credential-shaped identifier reached stdout and `--json` in full.
That is why this is not a member of `/gt:gt-scan`, and why registering it as one would be a
regression rather than a tidy-up.

`--baseline` reports only what is **new**, so an existing finding you have already triaged does
not drown the ones that arrived today. `--write-baseline` records the current set — read it
before you accept it; a baseline written without looking is a decision to ignore whatever was
there.

**Baselines are content-keyed (0.20.1), for both `gt_secrets` and `gt_scan_code`.** An entry
matches by the rule, the path with every release-version segment written as `<ver>`
(`golden-thread/0.20.0/x.py` and `golden-thread/0.20.1/x.py` are one path), and a hash of the
normalised line, with a count. So a version cut that copies a release directory no longer
re-flags lines you already accepted, while an edited line, the same text in another file, a
different rule, or one more copy of an accepted line still fails. The secrets baseline stores no
matched text, no value and no fast hash: its hash is PBKDF2-HMAC-SHA256 (200,000 rounds, salted
with the rule and the path), because a plain digest of a short line is a lookup away from the
line; the code baseline uses SHA-256 (code is not a secret). A baseline written before 0.20.1
(path-keyed `accepted` tuples) is still honoured exactly as before -- it just does not survive a
cut. `--write-baseline` writes the new format; run it once (and read what it records) to migrate.

Exit: `0` clean (nothing new against the baseline) · `1` found something · `2` **could not run,
or could not scan part of what it was asked to scan**. **Silence is never clean**: every
successful run ends with an affirmative naming what was covered, because a scanner that could
not run prints an empty finding list and so does a clean tree.

### `gt_scan_code.py` — source validation

```bash
python3 $SCRIPTS/gt_scan_code.py <path> [--vault "<vault>"] [--staged] [--json]
python3 $SCRIPTS/gt_scan_code.py <path> --sarif findings.sarif
python3 $SCRIPTS/gt_scan_code.py --rules          # every rule in effect, and its tier
```

A **leaf** command, sequenced by `gt_scan.py` as the `code` member alongside `language`. Unlike
`gt_secrets.py` it **does** print source text, because here the text is the finding — which is
precisely why the two may never share a process.

**Rules are data, never code.** Every rule comes from a `lint` pack in the slot registry, in a
documented subset of ast-grep's rule schema: `id`, `message`, `severity`, `note`, `files`,
`ignores`, and a `rule` object with atomic (`pattern`, `kind`, `regex`), relational (`inside`,
`has`, with `stopBy`) and composite (`all`, `any`, `not`) forms. Same names, same nesting, same
meanings as upstream, so a user can read ast-grep's published rule reference and have it apply.

Each rule declares the **evaluator tier** it needs, and a tier is either present or it is not:

| Tier | Evaluator | Present when |
|---|---|---|
| `text` | regex over the file | always |
| `stdlib` | Python's own `ast` | always, for Python |
| `astgrep` | the ast-grep **CLI** | `ast-grep` ≥ 0.45.3 is on `PATH` |
| `treesitter` | tree-sitter grammars | the grammar is installed |

**A rule whose tier is absent is reported SKIPPED, never silently passed.** This is the same
rule as "a member that could not run is not a pass", one level down: a rule that was never
evaluated and a rule that found nothing produce identical output otherwise, and the difference
between them is the whole value of the scan. The affirmative line names what was skipped, and
SARIF carries it in `toolExecutionNotifications`.

Exit: `0` clean, everything ran · `1` **findings** (whether or not anything was also skipped) ·
`2` usage · `3` nothing found **but** a rule was skipped or a pack problem was reported, so the
scan does not cover what it was asked to cover · `4` nothing to scan with. `1` outranks `3`
deliberately: a caller deciding whether to block needs "was anything found", and a run can both
skip a rule and find something. Coverage is reported either way.

The ast-grep CLI is **optional and not bundled**. `brew install ast-grep` or
`npm i -g @ast-grep/cli`; `install.sh` offers it and never installs it behind your back. Below
0.45.3 it is refused rather than used, because a partial version silently under-matches, which
is the failure this whole section exists to prevent.

### `gt_sweep.py` — the weekly cadence

```bash
python3 $SCRIPTS/gt_sweep.py --vault "<vault>" [--path "<tree>"] [--only secrets,code] [--json]
```

Runs the checks above over the whole tree and **reports**. Members are `secrets`
(`gt_secrets.py`) and `code` (`gt_scan_code.py`), each reading `.gt/<member>-baseline.json` from
the tree when one is there. `node_modules`, `.venv` and `__pycache__` are excluded by default —
measured on gt's own repo, the credential scan went from 48 findings to 10 on that exclusion
alone, and a sweep that reports the same finding once per retained release is a sweep nobody
reads twice.

Exit: `0` the sweep ran, with or without findings · `2` usage · `3` a member could not run, so
the sweep does not cover what it claims to.

### `gt_check_report.py` — so a gate leaves a record

```bash
python3 $SCRIPTS/gt_check_report.py record --vault "<vault>" --check secrets \
    --verdict clean|findings|cannot-run [--count N] [--scope "..."] [--ref HEAD] [--detail TEXT]
python3 $SCRIPTS/gt_check_report.py show --vault "<vault>" [--check secrets] [--json]
```

A gate tells you about the commit in front of you and nothing about the week. Three questions
need history and cannot be answered from an exit code: **is this check actually running**, is it
getting noisier or quieter, and did anyone ever look at the thing it flagged.

**What is recorded is a verdict, a count, a scope and a ref — never a finding's content.**
`gt_secrets.py` exists because a credential must not reach a log, and a vault file is a file
like any other. A report that quoted the finding would undo the tool it is reporting on.

`show` exits `1` when there are no reports yet, which is a different thing from a clean history
and reads differently on purpose.

### `gt_check.py` — checkers a module contributes

*New in 0.18.1.* A **checker** is a deterministic check — valid HTML, a MIME type, a formatter, a
commit-message convention — that a module contributes and gt hosts. Only checkers are modular;
Core-rule enforcement never is. Before this, each such check would have been its own hook, with its
own matching, output and per-commit cost, and nothing told you which were installed or why a commit
was refused.

```bash
python3 $SCRIPTS/gt_check.py list [--for PATH ...] [--json]
python3 $SCRIPTS/gt_check.py run  [PATH ...] [--staged] [--repo DIR] [--json] [--no-receipt] [--dry-run]
python3 $SCRIPTS/gt_check.py run  --event commit-msg --message-file F
```

`list` shows every checker of every module that is on: `module/id`, what it applies to, its tools
("tools ok", "no tools required", "MISSING TOOL(S): x"), and "not first-party" when its script does
not match the release MANIFEST. `run` runs only the checkers whose globs or MIME type match, in
parallel, and prints one report. Every checker answers `pass`, `fail` or `cannot-check`, and
**`cannot-check` is never a pass**: a missing tool, a timeout (its process group is killed) or output
that is not the result shape is `cannot-check` with the reason. Checkers are read-only — each runs on
a snapshot outside the repo and vault, and changing the snapshot is a `fail`. Exit `0` all passed ·
`1` a fail or cannot-check · `2` usage · `3` nothing applied (never the same as clean). Each run is
recorded in `~/.claude/golden-thread/check-runs.jsonl` (each file, the hash of the bytes each checker
saw, each verdict).

**The commit gate.** With `commit_checks` on, the existing commit guard refuses a commit whose staged
bytes no passing run covers, naming the file and the checker: run `gt_check.py run --staged` before
committing. One commit: `GT_CHECK_GATE=off git commit …`; one repo: `.gt-no-test-gate`. Off by
default, and with it off commits behave as before. `commit-msg` checkers run when invoked, but
nothing calls them at commit time yet. `/gt:gt-validate` asks `gt_check.py list --for <file>` first
and lets a checker settle a mechanical claim; the fresh-context validator is for claims no checker
covers. `/gt:gt-doctor` has a `checkers` row. `install.sh --without <module>` removes its checkers.

**For module authors.** In `module.json`:

```json
"checkers": [{"id": "html-valid", "script": "check_html.py", "globs": ["*.html"],
              "requires_tools": ["tidy"], "timeout": 20, "fixes": ["*.html"], "rules": ["html"]}]
```

The checker reads `{"schema":1,"checker","event","root","files":[…],"message_file"}` on stdin, with
the snapshot as its working directory, and prints
`{"schema":1,"verdict":"pass|fail|cannot-check","reason"?,"findings":[{"file","line","rule","message"}],"proposals"?:[…]}`.
The module validator refuses unknown keys (named), a script outside `scripts/`, an event other than
`pre-commit` or `commit-msg`, a rule set other than `html`, a `fixes` glob naming `core-rules`,
`global-memory`, `Sources`, `.git`, `.githooks` or `.claude`, and a checker that applies to nothing.
No checkers ship with gt itself; each is its own module.

### `gt_apply.py` — fixing what a checker found

A checker may return **proposals** — a unified diff against the exact bytes it examined, the finding
it addresses and a reason. `gt_apply.py` is the only thing that writes them; setting `addon_fixes`
decides when: `off` discards them, `propose` (default) keeps and shows them, `apply` applies after a
check run.

```bash
python3 $SCRIPTS/gt_apply.py list [--all] [--json] [--vault V]
python3 $SCRIPTS/gt_apply.py show ID [--json]
python3 $SCRIPTS/gt_apply.py apply (ID ... | --all) [--dry-run]
python3 $SCRIPTS/gt_apply.py undo ID [--dry-run]       # restores the exact original bytes
```

An apply passes, in order: the module's `fixes` grant and the checker's own findings → protected paths
(`core-rules/`, `global-memory/`, `Sources/`, `.git/`, `.githooks/`, `~/.claude`, the plugin source —
whatever the grant) → one in-place edit of one existing file (no create, delete, rename, mode change,
symlink, binary or whole-file replace) → the base hash → content rules (no added bidi-control or
zero-width characters; no added text matching the `secrets` slot or a scrub term, never printed; a
size change within `addon_fix_size_limit`; under `"rules": ["html"]` no new `<script>`, inline event
attribute or URL to a new host) → no live session's claim → **every installed checker that applies,
re-run on a staged copy**, none of which may go from pass to fail, and the proposer's finding gone
(else `did-not-fix`) → a compare-and-swap write under a per-file lock (vault Markdown through the
write queue). A refusal leaves the file byte-identical and records why. Only a **first-party**
checker (script hash equals the MANIFEST row) may auto-apply under `addon_fixes apply`; anything else
is capped at propose, and the output says "capped to propose". The fix is left uncommitted, for the
commit gate to re-check. Each apply, refusal, rollback and undo is one `addon.fix` event. Proposals
live in `<vault>/Projects/golden-thread/ext-proposals/`, originals in
`~/.claude/golden-thread/proposals/backups/`. Exit `0` applied · `1` refused, did-not-fix or nothing
to do · `2` usage.

### The fast test loop (0.18.1)

The 0.17.11 release ran the whole ~2,400-test suite about eight times in one day, including for
single-tool changes, and a local run at load ~50 dropped keystrokes. Four changes:

```bash
tests/run.sh --affected          # only what the branch's changes need; a pass = a SCOPED receipt
tests/prun.py --print-affected   # show the mapping, run nothing
tests/prun.py --hosts a,b        # split units across runners (empty value = the `runners` setting; `local` = here)
GT_TEST_LOAD_AWARE=0             # size the pool from the settings only
GT_TEST_NO_INSTALL_CACHE=1       # measure the uncached install cost
GT_TEST_TMPDIR=<dir>             # test temp files go there for this run
```

- **Scoped receipts.** `--affected` runs the test modules that name the files changed on the branch
  (committed, staged, unstaged, untracked); an unmapped code change or a harness or installer change
  selects the full suite. A feature-branch commit may rely on that scoped receipt (setting
  `scoped_receipts`); the default branch and every release gate need a full-suite receipt, once per
  release.
- **A cached install.** The harness builds an installed machine once per run and copies it into each
  test's sandbox; a test proves the copy equals a fresh install.
- **Load-aware workers.** The runner starts from the settings' ceiling scaled down by load, memory
  pressure and other parallel runs (`gt_load.py`), shrinks its pool when units run more than twice
  their recorded time, and starts the heaviest units first.
- **Temp files out of watched folders.** `test_tmpdir noindex` keeps them away from Spotlight and sync.

A unit no runner reported under `--hosts` is a failure, never a pass, and a `--hosts` run records no
receipt; the full suite on one runner is `dev/remote-test.sh` (see `dev/README.md`).

### `gt_code_review.py` — the framework, not the opinions

```bash
python3 $SCRIPTS/gt_code_review.py dimensions [--vault "<vault>"] [--json]
python3 $SCRIPTS/gt_code_review.py plan <path> [--vault "<vault>"] [--staged] [--json]
python3 $SCRIPTS/gt_code_review.py validate <findings.json> --root <path> [--json]
python3 $SCRIPTS/gt_code_review.py report <findings.json> --root <path> --vault "<vault>" [--ledger F]
```

**gt does not perform the review, and ships zero review dimensions.** It cannot perform one:
"does this abstraction earn its keep" has no mechanical oracle, and a tool claiming to test that
is testing something else. What gt owns is everything *around* the judgement, which is most of
what makes a review trustworthy rather than decorative.

The dimensions are **yours** — `<vault>/Projects/golden-thread/packs/review.<name>.pack.json`,
each entry an `id`, a
`title`, a `rubric` and optionally a `files` glob. The rubric reaches the plan **verbatim**; a
rubric gt paraphrased would be gt's opinion wearing your name. A core review pack would make
gt's idea of a good review everyone's default, so there is not one, and a test asserts there
never quietly becomes one.

**Zero dimensions configured exits `3`, never `0`.** Since gt ships none, the empty case is the
*default* case — and if it reported success, every user who had never configured a dimension
would be told their code had been reviewed. The message names the file to create.

`validate` is the deterministic half, and rejects any finding that cannot be **checked**: a file
that does not exist, a line past the end of one (the reason says how long the file actually is),
a dimension nobody configured, a severity outside the vocabulary, or `confirmed: false` — a
verification pass that says no must be honoured, or the pass is decoration. Rejections are
**counted in the summary**, never silently dropped. `report --ledger` suppresses a finding
already declined once: a review that re-proposes what was already dismissed stops being read.

Scope comes from `gt_scan_code.in_scope` rather than a second definition of "what counts as
code" — two tools disagreeing about one tree would mean the one nobody runs is the one that was
right. An empty scope is an **outcome**, reported as such, never a silent upgrade to reviewing
everything.

### `gt_schedule.py` — the scheduled jobs, installed by a script rather than by hand

```bash
python3 $SCRIPTS/gt_schedule.py list
python3 $SCRIPTS/gt_schedule.py install daily --vault "<vault>" [--repo PATH ...] [--hour H] [--minute M]
python3 $SCRIPTS/gt_schedule.py install sweep --vault "<vault>" --repo TREE
python3 $SCRIPTS/gt_schedule.py check   daily
python3 $SCRIPTS/gt_schedule.py remove  daily
python3 $SCRIPTS/gt_schedule.py migrate-labels      # install.sh runs it; idempotent
```

**Where a job lives (0.20.1).** macOS: a launchd agent. Windows: a Task Scheduler task. Linux:
a systemd user timer (`~/.config/systemd/user/io.goldenthread.gt-<job>.timer` and `.service`), or
— with no user systemd — a crontab line tagged `# gt-schedule:<label>` (the crontab is backed up
first and only gt's lines are touched). Until 0.20.1 a Linux "install" wrote a launchd plist
nothing ran. Every job is labelled `io.goldenthread.gt-<job>`; a job still under the old
`com.markethaven.gt-<job>` label is moved by `migrate-labels` (which `reconcile` and install.sh
run) using its own arguments and schedule, and left exactly as it was if the new registration
is refused.

**Installed means the scheduler has it (0.20.1).** A refused `launchctl bootstrap`,
`schtasks /Create` or `systemctl enable` puts the job's files back as they were and prints
NOT INSTALLED with the scheduler's reason; `list` does not show it, and the post-install gate's
`daily-job` row FAILs a job that is on disk but not registered. `remove` says "removed" or
"booted out" only for what actually happened, and exits 1 when the scheduler refused.

| Job | Runs | What it does | Normal exits |
|---|---|---|---|
| `daily` | 22:00 | `gt_daily.py` — the day's facts into `Daily Notes/<today>.md` | `0`, `1` (nothing to report) |
| `lint-weekly` | Mondays 07:00 | vault + wiki lint, reported into the vault | `0` |
| `sweep` | Mondays 07:30 (0.17.2) | `gt_sweep.py` — whole-tree secrets + code sweep, verdict filed in the vault. Takes `--vault` and exactly one `--repo` | `0` |
| `reminder` | 08:30 (0.18.1) | `gt_reminder.py run --scheduled` — overdue and due-soon deadlines through the enabled channels; reads the mirror, never the vault. See [Reminders](#reminders-outside-a-session-gt_reminderpy) | `0` |

**A scheduled job runs the installed copy** in `~/.claude/golden-thread/hooks/` — a script under
CloudStorage fails under launchd — and `install` refuses a job whose script is not there. Until
0.17.2 `gt_daily.py` was not installed there, so the `daily` job could never be installed, and
`gt_sweep.py` promised a launchd job that did not exist. Both install there now, with the
sweep's members and their imports, and the release gate fails any job whose script does not
reach the hooks dir.

**`install sweep` currently refuses — a known limitation.** Before scheduling, `install` runs the
job's own `--check`; for the sweep that proves each member can run from the hooks dir, over an
empty tree. Today it cannot: `gt_registry` looks for `packs/` beside itself and there is none
there, so every member would report "could not run" every Monday. Refusing to install is the
check doing its job.

The weekly lint agent was installed **by hand** in 2026-09-08, and its only record was two lines
in a runbook: a manual step with no `--check` and no rollback, on a machine where scheduled jobs
fail in ways a terminal run cannot reproduce. This retrofits the standing rule that any human
step becomes a script with `--check`, self-proof and rollback.

**`check` validates through launchd, not through a terminal run**, and reads back launchd's own
*last exit code*. That distinction matters on macOS, where a job can run perfectly in your shell
and be denied the filesystem under launchd. It also knows which exit codes are a **normal
outcome** for each job, because a check that reports a normal result as a failure is worse than
no check — people stop reading it.

**That table was wrong for `lint-weekly` until 0.17.2**, in the other direction. It listed `1` as
normal on the belief that the job exits 1 when the lint finds something. It never did —
`gt_lint_weekly.py` returns 0 on every path it chooses — so the only way it exited 1 was an
uncaught exception, and a crash read as "findings". Now only `0` is normal; `gt_lint_weekly.py`
exits `3` and writes **COULD NOT RUN** into its report when a lint died, instead of "Findings: 0"
built from two tracebacks. **A benign exit code must be one the script returns on purpose, never
Python's crash code.** `gt_daily.py` turns an uncaught exception into `3` for the same reason.

**Known and not fixed: macOS refuses calendar-fired runs the vault.** Every calendar-fired
`lint-weekly` run on record (09-14, 09-21, 09-28) was refused by macOS privacy controls (TCC)
reading existing files in a vault under CloudStorage; only runs kicked from a session succeeded.
The `daily` job will very likely fail the same way. The doctor's `schedule` check reports it;
the fix — a privacy grant, a vault outside CloudStorage, or no calendar trigger — is the machine
owner's choice.

### `gt_daily.py` — the day's facts, and nothing else

```bash
python3 $SCRIPTS/gt_daily.py --vault "<vault>" [--date YYYY-MM-DD] [--repo PATH ...] [--dry-run] [--check]
```

Writes new projects, tasks closed, tasks added, tasks **due today that are still open**,
commits, event counts, wiki item **counts**, and an active span per project into one fenced,
clearly-marked block in `Daily Notes/<date>.md`, replaced whole on each run.

**Grouped by domain, then project** (0.17.10): each task list and the commits sit under
`### <domain>` sub-headings taken from each project README's `domain:`, alphabetical, with
`uncategorized` last. A vault commit is filed under the project whose files it changed most; one
that touched no project, and commits in an extra `--repo`, go under `uncategorized` by repo name.
Tasks are read only from project `README.md` files, so a handoff's checklist is never counted as
a task; an open task whose title was also removed that day was edited, not added. A section with
nothing in it is left out, and a "New projects" line appears only on a day a project README was
created.

**Push first** (0.17.10): before writing, it runs `git push` in the vault. A failed push — or a
vault with no remote — adds a `> NOTE` to the block and the write still happens. `--dry-run` never
pushes; `--check` reports an unreachable remote as a problem and a missing one as information.

**Named sections other tools fill** (0.17.10). Name a section and gt_daily makes an empty, marked
area for it in the daily note — which always lives in `Daily Notes/<date>.md` — and writes
`Daily Notes/.handoff/<date>.md`, saying where the note is and which sections are waiting. Another
tool (Joule, a Claude session, anything) reads the handoff and writes between its own section's
markers; gt_daily never overwrites a section, and its next run marks it `filled`. With no section
open for filling there is no handoff, and a stale one is removed. The sections live in the shared
vault (`.gt/daily-sections.json`), so every machine agrees.

```bash
python3 $SCRIPTS/gt_daily.py --vault "<vault>" --section-add builds [--title T] [--instructions I]
python3 $SCRIPTS/gt_daily.py --vault "<vault>" --section-add mail --comms     # holds email/Teams info
python3 $SCRIPTS/gt_daily.py --vault "<vault>" --sections-list | --section-remove NAME
python3 $SCRIPTS/gt_daily.py --vault "<vault>" --sections-only                # make room early in the day
python3 $SCRIPTS/gt_daily.py --vault "<vault>" --comms-content on|off         # the vault-wide policy
```

**Email and Teams content is OFF by default.** A `--comms` section is placed only while the
vault's policy is `on` and this machine does not force it off with the gt setting
`daily_comms_content` (`follow`, the default, or `off`). A machine can tighten the policy, never
loosen it: the vault is shared, so content written on one machine is readable from every other —
including a work machine whose employer does not want mail or chat content anywhere Claude can
read. While off, a comms section gets no room and no handoff, every handoff says such content is
not allowed, and if one still holds content a `> NOTE` says so; gt_daily never deletes another
tool's writing.

**Terse by design.** It does not explain, summarise or interpret. The owner writes the meaning;
a generated block that editorialised would encode a reading of the day that is not theirs. For
the wiki it records how many items and how much time, never what they said.

**Where each fact goes** (0.17.11): under the heading it belongs to, in its
own marked block after whatever you wrote there — new projects, tasks closed and commits under
`## Did`; ADRs added that day (read from the `decisions.md` diffs) under `## Decided`; tasks
added and open ones due today under `## Open at end of day`. Totals, event counts and the active
span stay in a footer block at the bottom. A heading the note lacks is added. A note written by an
earlier release, with everything in the bottom block, is rearranged on its next run.

**Through the queue** (0.17.11): the note is written as one `replace-file` request per run and
applied by the broker (Core rule 1), so a run never overwrites an edit made after it read the note
— that is escalated instead.

It **never touches a line outside its blocks**, and specifically never touches `## Noticed` —
that is the unfiled capture surface `/gt:gt-review` sweeps, and a tool tidying it would defeat
the sweep. Exit `1` means nothing to report for the day, which is not an error; a crash
exits `3` (0.17.2), never `1`. `--check`
verifies every dependency and exits; `--dry-run` prints the note as it would be written and writes nothing.

### `gt_state.py` — write the state before the context runs out

```bash
python3 $SCRIPTS/gt_state.py check [--margin N] [--write] [--repo P] [--json] [--session ID]
python3 $SCRIPTS/gt_state.py write [--reason R] [--repo P] [--session ID]
python3 $SCRIPTS/gt_state.py show
python3 $SCRIPTS/gt_state.py hook [--repo P] [--session ID]   # PreCompact: the backstop
```

Three pieces existed and nothing joined them: `gt_usage` reads the context percentage and only
*displays* it; `gt_report_card` runs at `SessionEnd` and `PreCompact` and writes a **health
summary** — uncommitted files, stale rollup — with nothing about what the session was doing; and
`gt_handoff` gathers exactly the right content but is manual, so nothing ever calls it. This is
the join, and it needs nobody to think of it.

**The signal is `ctx_pct`, and the distinction is the whole design.** `readings.jsonl` carries
context fill and rate-limit allowance side by side, and they are unrelated. A real reading from
the session that prompted this:

```json
{"five_hour": 5, "seven_day": 10, "ctx_pct": 91}
```

5% of the five-hour allowance, 91% of the context. Triggering on the allowance meter would have
fired at entirely the wrong moment **and looked correct doing it**, because both are percentages
with plausible values. A test asserts the tool never reads `rate_limits` at all.

It fires at `--margin` (default 5) below the flag point for your `usage_alert` mode, **once per
crossing** rather than on every turn, and is wired to `UserPromptSubmit`. `PreCompact` is the
**backstop, not the primary** — it fires when compaction is already starting, so the write
competes with the thing it exists to survive — and its file says which one wrote it, so nobody
reads a backstop write as the early one having worked.

A missing or unreadable usage ledger is reported as **"cannot tell"**, never as room to spare:
the usage module may not be installed at all, and silence from a source that was never there
must not read as a measurement saying everything is fine. Every failure path exits `0`; a state
write that broke a turn would be worse than the gap it closes.

**What you see (0.18.1).** Until 0.18.1 the result went to stdout, which for a hook reaches only
the model — the same failure the SessionStart checks had until 0.9.6 — so you never knew a state
file had been written, or that one had failed. Run as a hook (detected from the `hook_event_name`
in the payload Claude Code pipes in; the registered commands are unchanged, so nothing needs
re-installing) it now speaks through `systemMessage`:

| When | In your terminal |
|---|---|
| `UserPromptSubmit`, context crosses the threshold, state written | `gt-state: context 91% — session state written to <path>` |
| `UserPromptSubmit`, the write fails | `gt-state: context 91% — session state could NOT be written (<error>)` |
| `UserPromptSubmit`, below threshold, already written, or no usage module | nothing — a line every turn trains people to ignore it |
| `PreCompact` | always one line: written to `<path>`, or NOT written (`<error>`), or `context unknown: <why>` |

The model keeps its own one line either way. "Cannot tell" stays out of your terminal on ordinary
turns, because with no usage module it would be every turn.

**Whose reading it is (0.18.1).** The usage ledger is shared by every session on the machine, and
`gt_state` used to take its newest `ctx_pct` whoever wrote it — which is why, on 2026-09-29, a
fresh session's first prompt reported `context 88% — already written`: the previous session's
figure. It now filters the ledger by the hook payload's `session_id` (the ledger keeps its first 8
characters). A session with no reading of its own yet is "cannot tell" — usually the first prompt,
since the ledger records about once a minute — which is the correct answer. By hand, `--session ID`
names one; with no session known, the newest reading is used as before.

### `gt_surface.py` — what the last session left, shown to the next

```bash
python3 $SCRIPTS/gt_surface.py check [--vault V] [--dry-run] [--json]   # what SessionStart runs (with --hook)
python3 $SCRIPTS/gt_surface.py must-do [--vault V]                     # the MUST DO block alone
python3 $SCRIPTS/gt_surface.py handoffs --project SLUG [--vault V]      # one project's waiting handoffs (/gt:gt-open)
```

By 0.17.1 gt could **write** everything a next session needs and put none of it in front of that
session. `gt_handoff.py` wrote handoffs, `gt_state.py` wrote state files, `/gt:gt-work` raised
tasks citing a handoff, the owner kept dated must-do items — and, measured on 2026-09-28, none of
the `SessionStart` hooks read a handoff, a state file, `TASKS.md` or any `p::`. Two credential
rotations sat 15 days overdue at the vault's top priority, worked around almost daily, because
**priority is a sort order, not an alarm**: sixty tasks already sat at the top tier across ten
projects, and one more alerts nobody.

**Surfacing is the alarm; the task is the record.** This hook is the alarm. It never creates,
edits or closes a task, and it never writes to the vault.

What it shows, how often, and the settings that control each are under
[What you see at session start](#what-you-see-at-session-start); the file format is
[`deadlines.md` format](#deadlinesmd-format). Handoff state comes from `gt_handoff_status.py`
(see [`/gt:gt-handle`](#gtgt-handle)); the task line from the vault's
`gt_task.py count` (see [`/gt:gt-create task`](#gtgt-create-task)).

**State files are still "once each"**, and that needs memory: a machine-local "already shown"
ledger at `~/.claude/golden-thread/surface/seen.json`, the one thing this hook writes.
`--dry-run` leaves it alone.

**A clean run says so** — `nothing waiting`, in one line — because silence reads the same as a
hook that never ran. **It is never fatal**: any failure exits `0`, since a `SessionStart` hook
that broke startup would be worse than the gap it closes. Switch it off with
`gt_settings.py set surface off`.

**Why this is core and not the report card.** The request that led here proposed a deadline check
in the report-card module. An alarm must not depend on an optional module being installed, and
the report card shows what the *previous* session computed at its end, where a countdown has to
be computed live at every start.

### Session identity — which machine wrote a claim

`gt_session.py` claims a file for a session, and the `guard_session_claims` hook refuses a write
to a file another live session holds, while the broker holds a queued write to it (Core rule 1). "Live" is judged by the recorded **pid** — but
a pid only means something on the machine that recorded it. So every reader first asks: *was this
written here?*

Until 0.17.2 the answer was "the recorded host equals this hostname". A hostname is not an
identity. On 2026-09-28 a laptop moved to a wired network, DHCP gave it a new name, and every
claim written under the old one stopped being this machine's. Nothing failed: the pids simply
stopped being judged, and each claim would lapse on the heartbeat clock while its session was
still running — Core rule 1, disarmed by a DHCP lease, with nothing announced.

**Identity is now a machine id**: a uuid4 written once to `~/.claude/golden-thread/machine-id`,
created atomically on first use (two first runs cannot mint two ids), never overwritten, never
synced between machines. Registrations record it as `machine:`; `host:` stays as a readable
label only. The claim guard, `gt_session.py` and `gt_workers.py` all apply one rule
(`gt_paths.same_machine`; the vault tool carries a pinned copy, because a vault tool cannot
import from the plugin):

| The record has… | Verdict |
|---|---|
| a machine id, and so do we | the ids decide. Same id under a new name is a **rename** — reported once, claims still hold |
| no machine id (written by gt ≤ 0.17.1) | ours while its host label matches, exactly as before. If it does not match, nothing can prove whose it is: its heartbeat decides, **and that is said**. It adopts the id the next time its own session writes it |
| a machine id, and ours is unreadable | the host label, as before — and the missing id is announced |

**Two OS logins on one Mac are two machines now.** The id lives under each HOME, beside
`workers.jsonl`, which is machine-local by the same design. Each login judges the other's
sessions by heartbeat, the cross-machine rule, where before they shared a hostname and judged
each other's pids.

**A pid no longer vouches for another session.** One Claude Code process hosts successive
sessions, so an older session's file recording *our own* pid looked live for as long as we ran,
and its claims never lapsed. When the recorded pid is the asker's and the session is not, the pid
proves nothing about that session, and its heartbeat decides.


---

### `/gt-watch:gt-watch`

*Module `watch` (plugin `gt-watch`), installed by default; it was `/gt:gt-watch` before
0.15.0. `bash install.sh --without watch` removes it, including the crontab line it installed.*

Watch any git repo you depend on, and hear about it when it changes in a way you said
you care about — raised as a **P0** when it ships a security fix.

```
/gt-watch:gt-watch add <git-url>   — create a watch (any git URL: GitHub, GitLab, self-hosted, file://)
/gt-watch:gt-watch list            — every watch and its last change
/gt-watch:gt-watch check           — fetch now instead of waiting for cron
/gt-watch:gt-watch show <slug>     — Claude reads the new commits and explains them
/gt-watch:gt-watch ack [<slug>]    — mark changes seen
/gt-watch:gt-watch remove <slug>   — stop watching
```

**A watch is a note** — `Projects/golden-thread/watches/<slug>.md`, editable in Obsidian's
Properties panel. Its frontmatter says what to watch: `url`, `track` (commits, tags,
releases), `branch`, `watch_paths`, `current_version`, your own `p0_when` patterns, and
`starred`.

**How it runs.** A cron job (`gt_watch.py install-cron --every 1h`) fetches every watch
without Claude, classifies each change, and queues it. At session start a hook reads the
queue — P0s first, spelled out; review items one line each; routine changes as a count. It
never fetches. Machine state lives in `~/.claude/golden-thread/watch/`, outside the vault.

**When it is a P0** — decided by rules, never by a model reading commit prose: a new
security advisory for the repo; a new tag or release naming a CVE or GHSA id, or saying
"security fix/release/update" or "vulnerability"; a commit message carrying a CVE or GHSA
id; a change to `SECURITY.md` or your `watch_paths` that says the same; or anything
matching your own `p0_when`. A new release, a major-version jump, a change under
`watch_paths`, or "BREAKING CHANGE" is a **review** item. Ordinary words like "auth" or
"security" in a commit message are not enough — a P0 that cries wolf stops being read.

The module is on by default, but it fetches and reports nothing until you switch it on:
setting `watch` (`off` · `report`). `add` offers to switch it on and install the cron entry.

---

### `/gt-demo:gt-demo`

*Module `demo` (plugin `gt-demo`), installed by default. `bash install.sh --without demo`
removes it; `--with demo` brings it back.*

A guided tour of Golden Thread on PizzaBot 3000, a fictional pizza-ordering
project: nine core acts, plus one act for each installed module that ships one — in 0.16.2
`wiki`, `watch` and `flow`, so twelve with the default modules. It runs in its **own
throwaway vault**, so nothing it does can reach yours.

```
/gt-demo:gt-demo start    — build the demo vault and print the command that opens it
/gt-demo:gt-demo tour     — (in the demo session) run the tour; you only click Next
/gt-demo:gt-demo end      — every commit and file the tour produced
/gt-demo:gt-demo clean    — delete the demo vault and build a fresh one
/gt-demo:gt-demo remove   — delete the demo vault; `bash install.sh --without demo` removes the module
/gt-demo:gt-demo status   — is there a demo vault, and how old is it
```

**Running it:** `start` builds the vault at `~/.claude/golden-thread/demo-vault` and prints
`cd <demo vault> && GT_VAULT=<demo vault> GT_WATCH=report GT_WATCH_STATE=<demo vault>/.demo/watch claude`
— the extra variables let the watch act run without touching this machine's watch
settings or state. Run that in a new terminal and type
`/gt-demo:gt-demo tour`. For each act Claude says one line to the audience, does the work with
the real skill, says what just happened, and offers buttons — **Next**, **Repeat this
act**, **Skip ahead**, **End tour**.

**The core acts:** Core rules enforced (a prepared reply carrying a key is blocked) · open a
project · "what's next?" from the task rollup · capture a finding mid-session · route a
stray idea · validate a wrong claim · lint finds a planted broken link · promote a finding
to a Knowledge page (which fixes the link) · file the inbox, close the session, and show
the receipt. They are plain text in `templates/demo-pizzabot/tour.md`.

**Module acts** come from each installed module's `module.json` → `demo` file and are placed
before the closing act: **wiki** — ingest a source and query the wiki; **watch** — watch an
upstream library and see its security release open as a P0; **flow** — render the tour's
events as a redacted flow view. A module that is not installed has no act. A module act
names its scripts as `<module:NAME>`, which the demo resolves with
`gt_demo.sh module-scripts NAME` (exit 3: not installed, the act is skipped).

**Why its own vault:** a fuller tour writes to shared files (`INBOX.md`, `TASKS.md`,
`log.md`, `Knowledge/`) that other sessions also write. Undoing that in a real vault
means rewinding history everyone shares. In a throwaway vault, `clean` just rebuilds it.
The demo never writes your `vault-config.json`: the demo session is pinned with
`GT_VAULT`, which every skill and hook honors. `clean` and `remove` delete a directory
only if it carries the demo's marker file.

---

### `/gt-flow:gt-flow`

*Module `flow` (plugin `gt-flow`), new in 0.15.0, installed by default.*

Draws how knowledge moved through the vault over time, as **one self-contained HTML file**
that opens offline — no network, no CDN. One lane per project, time left to right, each
lane split into the ladder's levels (memory → research/decisions/design → Knowledge →
global-memory → Core); an arrow joins the level an item left to the level it reached, so a
promotion is an arrow climbing. It reads `Projects/golden-thread/events.jsonl` and never
writes the vault.

```bash
FLOW=~/.claude/plugins/cache/golden-thread-plugin/gt-flow/0.17.11/scripts
python3 $FLOW/gt_flow.py render --vault <vault> [--out FILE|DIR] [--redact] \
    [--project <slug> ...] [--since YYYY-MM-DD] [--tasks]
```

- **`--redact` before the page leaves your own screen** — an Artifact, a chat, a ticket, a
  screenshot. Every project, path, task id, domain and session becomes a short salted hash
  and notes are dropped; levels, kinds and counts stay. Hashes are at least 6 hex characters
  (gt-flow 0.18.1; they were 4, and two renders shared one about once in 1,024 runs). Inside one
  render, two names that truncate alike get a longer hash, so a page never shows two names under
  one hash. The salt is random per render and never written, so two redacted files cannot be
  joined. If the redaction self-check fails, nothing is written.
- Task events (`task.open`, `task.done`) are hidden at first — on a real vault they are most
  of the stream. They are one click away in the Kinds filter; `--tasks` shows them from the start.
- `--project` is repeatable and includes sub-projects; `--since` takes a date or an ISO-8601 instant.
- Without `--out` the file lands in the current directory (a temp directory when that is
  inside the vault). An `--out` inside the vault is **refused**.

Exit codes: `0` written · `1` bad arguments, an invalid event file or an unknown schema
version · `2` no events yet (run `gt_events.py backfill --dry-run` to see what history
would be recovered) · `3` the filters matched nothing.

### `/gt-visualize:gt-visualize`

*Module `visualize` (plugin `gt-visualize`), new in gt-visualize 0.3.0, installed by default. Two modes: `explain` and `render`.*

Draws a **repository** as an interactive 3D code city, in **one self-contained HTML file**
that opens offline — three.js is inlined, nothing is fetched. Directories are districts,
nested so a directory's plot sits on its parent's; files are buildings. Height is lines,
footprint is size (log scale), colour is language — from gt's own `filetype` definitions,
the ones `gt-scan` uses — or, toggled in the page, how often the file changed in the last
`--since` days. Drag to orbit, scroll to zoom, hover for a file's details, click a
district to fly to it, search to highlight paths. (For how *knowledge* moved through the
vault, see `/gt-flow:gt-flow`.)

```bash
VIS=$(ls -d ~/.claude/plugins/cache/golden-thread-plugin/gt-visualize/*/scripts | sort -V | tail -1)
python3 $VIS/gt_visualize.py render [PATH] [--out FILE|DIR] [--redact] [--since DAYS] \
    [--no-churn] [--exclude GLOB ...] [--max-files N]
```

**Explain how it works** — the second mode, and usually the one to reach for first. Where the
city shows *where* the code is, an explainer shows *how the parts work together*: a narrative
column that scrolls beside a 3D stage, each scene showing some parts, putting one or two in
focus and animating flows along the links between them — data, a rule pushed in, an answer
coming back, or a request stopped at a gate. The skill writes the story (a JSON file of
parts, links and scenes) from the project's `source.md`, `design.md` and the code itself;
the script renders it:

```bash
python3 $VIS/gt_visualize.py explain story.json --check     # list every problem, write nothing
python3 $VIS/gt_visualize.py explain story.json [--out FILE|DIR]
```

A story with any problem is never rendered. **The rules are fixed, not left to taste** (gt-visualize 0.3.0): scene 1 is an establishing shot of the whole system and the last scene a recap of it; every middle scene shows at least 3 parts and focuses 1–2; every scene carries a one-line caption of what the picture shows; and the page itself keeps the camera at one viewing angle, framing everything a scene shows and never closer than 55% of the establishing shot. `--check` names the rule each problem breaks; the full table (S1–S10, F1–F6) is in the skill. Ask for it in a session — *"explain how this
codebase works"*, *"architecture walkthrough"* — and keep the story file with the project
if you want the next render to start from it.

- **Code city** (`render`), below.
- Inside a git work tree the file list is `git ls-files --cached --others --exclude-standard`,
  so `.gitignore` is honoured; outside one the directory is walked (skipping `.git/`) and
  churn is off — the output line says so.
- **`--redact` before the page leaves your own screen.** Every file and directory name
  becomes a salted hash, keeping the tree's shape and short extensions; if the redaction
  self-check finds a surviving name, nothing is written.
- Above `--max-files` (default 20,000) the deepest directories collapse into single
  buildings, and the output line says at which depth.
- Without `--out` the file lands in the current directory (a temp directory when that is
  inside the vault). An `--out` inside the vault is **refused**.
- The three.js bundle is pinned by hash (`scripts/vendor/VENDOR.json`); a bundle that does
  not match is refused, never inlined.

Exit codes: `0` written · `1` bad arguments, an unreadable path, a refused `--out` or a
bundle that fails its hash · `3` nothing to draw.

**Guided walkthrough** (gt-visualize 0.4.0). Four skills turn an explainer into a guided session,
each reading the story only through the script, never inventing a value:

- `/gt-visualize:tour [focus]` — scene by scene: before each scene a prediction pause asks what
  connects its parts, then the scene and its hop map are revealed. Progress is saved per project
  (`Projects/<slug>/visualize/tour-state.json`, marked private and not a metric); a later session
  resumes with a two-question recap.
- `/gt-visualize:whatis <part-id>` — what one part does at its own level of detail, with step
  into / step out / step over / where.
- `/gt-visualize:trace <scene-or-part>` — runs a scene forward hop by hop on a real test where one
  exists, otherwise a clearly labelled STATIC walkthrough; ends with a hop table and offers a
  scenario test, written only on your yes.
- `/gt-visualize:explain-back [--diff <ref>]` — opt-in only: 3–5 questions about the story's
  seams, answers checked against the story.

```bash
python3 $VIS/gt_visualize.py story scene <story> <title|index>
python3 $VIS/gt_visualize.py story part <story> <id>
python3 $VIS/gt_visualize.py story questions <story> [--parts a,b]
python3 $VIS/gt_visualize.py tour-state get|set --project <slug> [--vault V] [--dry-run]
```

A story that fails `--check` is refused. `tour-state set` is refused (exit 5) when another live
session has claimed the file.

**Publish** (gt-visualize 0.3.0) — a page is local until you ask to share it. `publish` runs a
scrub gate (credential scan, IPv4 addresses, home-folder paths, your scrub terms), prints the plan
— target, visibility, final URL — and publishes only when re-run with `--yes`, which the skill
does after you agree. Targets: `local` (default), `claude` (a private claude.ai Artifact Claude
publishes; updates keep the URL), `github-pages` (`<repo>/<dir>/<slug>/index.html`, pushed with
your git login; always public) and `gist` (secret or public, with your `gh` login — GitHub shows a
gist's HTML as source, not as a page). A visibility a target cannot enforce is refused. No
credential is ever stored.

```bash
python3 $VIS/gt_visualize.py publish page.html [--target T] [--visibility V] [--check] [--yes]
python3 $VIS/gt_visualize.py targets set github-pages repo=<clone> base_url=<https://…> [dir=visualize]
python3 $VIS/gt_visualize.py targets set scrub_terms=<file>     # one term per line
python3 $VIS/gt_visualize.py publishes                          # everything published, to find or take down
```

Defaults come from two settings, `visualize_publish` (`local`/`claude`/`github-pages`/`gist`) and
`visualize_publish_visibility` (`private`/`link`/`public`), changed with `/gt:gt-settings`.

### 0.17.10 — queued vault writes, specialist agents, release announcements

**Queued vault writes (`gt_write_queue.py` / `gt_broker.py`).** When several agents write to one
vault at once, each can queue its write instead of making it. (Since 0.17.11 queueing is not
optional: it is Core rule 1 — see *Writing to the shared files*.)

```bash
python3 $SCRIPTS/gt_write_queue.py --vault "<vault>" --path Projects/<slug>/research.md \
    --op append|replace-section|create [--section "Findings"] --content "…"   # or --content-file F
python3 $SCRIPTS/gt_broker.py drain --vault "<vault>" [--dry-run]
python3 $SCRIPTS/gt_broker.py status --vault "<vault>"
```

Requests wait in `Projects/golden-thread/spool/queue/`; session start shows how many. `drain`
applies them oldest first and exits — no daemon, no hook. Appends from any number of sessions
merge and near-duplicates are dropped. Two sessions replacing one section differently, or a section
that changed after a replace was requested, write nothing: every version is saved in
`spool/broker/conflicts/` and a `#conflict` P1 task points at it. A file another live session has
claimed stays queued (Core rule 1). Generated and protected files (`log.md`, `TASKS.md`,
`decisions.md`, `Sources/`, `core-rules/`) are refused; `design.md` and `global-memory/` always go
to the owner, recognised by identity rather than spelling (see *Writing to the shared files*). Decisions are logged to `spool/broker/log-<date>.jsonl`. gt-farm files its results
this way, and a farmed result can only create a new file, never edit an existing note (ADR-8).

**Specialist agents by job type (`agent_specialization`, off by default).** With it on,
`/gt:gt-ingest` hands work to a specialist agent (`ingest-code`, `ingest-docs`, `ingest-tool`) and
`/gt:gt-validate` gives each validator a `validate` spec that loads **no** vault context. With
`skeptic_pass` also on, `/gt:gt-work` has a skeptic agent flag unverified or overclaimed figures in
the drafted research entries before they land. Full results go to
`Projects/golden-thread/spool/agents/<job>/`; the session sees a summary. A job with no spec runs
inline with a notice.

```bash
python3 $SCRIPTS/gt_agent_spec.py list
python3 $SCRIPTS/gt_agent_spec.py validate <spec.json>
python3 $SCRIPTS/gt_agent_spec.py resolve --skill gt-ingest --path <dir>
python3 $SCRIPTS/gt_agent_spec.py render validate --template
```

Shipped specs are in `templates/agent-specs/`; a valid spec in
`<vault>/Projects/golden-thread/packs/agent-specs/<job-type>.json` replaces the shipped one. (The
job types are due to become stage × kind specs — feature request
`2026-09-30-ingest-promote-stage-pipeline`.)

**Ingest scans first, stops only when it must, and runs on Claude only.** Before anything reads material for ingest, `/gt:gt-ingest` runs `python3 <gt>/scripts/gt_intake_scan.py <project-dir>`: credentials (gt_secrets), unsafe source code (a download piped into a shell, exec of fetched or decoded data, obfuscated blobs, destructive filesystem commands, install hooks that fetch and run) and prompt injection (text telling an AI to drop its instructions, fake role markers, hidden Unicode, instruction-bearing HTML comments). Results are per unit — each top-level folder, plus `.` for root files; `--unit-depth 2` splits a monorepo, `--unit U` scans one, `--json` gives the full report. A finding is kind, file, line and rule id — the matched text is never printed. Exit 0 clean, 1 findings, 2 usage, 3 incomplete (a scan that could not run is not a pass; unreadable formats such as PDF make a unit incomplete). Ingest no longer asks for approval: it stops for the owner only on a contradiction with a fact already in the vault, a security finding, or unsafe code, and writes nothing for a stopped unit. No ingest stage may run through gt-farm or any non-Claude service — `gt_agent_spec.py validate` refuses such a spec, and `gt_agent_spec.py render` refuses to produce an ingest prompt unless that unit scans clean.

**Release announcements (`release_announce`, off by default).** `dev/publish.sh`'s `announced`
step warned when no Discussion named the new version, and announcing was the step that got skipped.
`off` keeps the warning; `draft` writes the post to `~/.claude/golden-thread/announce-<version>.md`;
`post` creates it in the repo's Announcements category with `gh`. One post covers every release no
Discussion names yet, built from the CHANGELOG. It is scrubbed first (IPv4 addresses, home paths,
scrub terms, credentials); a hit, or a scrub that cannot run, posts nothing and leaves the draft.
Without a working `gh` it falls back to a draft. It never announces a version twice and never fails
a release.

---

## Where does this fact go?

| The fact | Goes to |
|---|---|
| "This preset is wrong right now" | `memory/` |
| "This API rejects plural keys" | `research.md` |
| "We use POST for all mutations, and why" | `decisions.md` |
| "The system is structured like this now" | `design.md` |
| "This platform behaviour bit two projects" | `Knowledge/` |
| "Always diff before overwriting prod" | `global-memory/` |
| "How to restart the prod backtest" | `runbook.md` |
| "Never scp without backing up" | `PROTOCOL.md` |
| "This code lives on host X at path Y" | `source.md` |
| "Which box serves which role" | `INFRASTRUCTURE.md` |

Two failure modes, opposite directions: **too narrow** buries a platform truth in
one project, so the next rediscovers it the hard way; **too wide** promotes a
quirk into `global-memory/`, where it loads into unrelated sessions as a rule
that isn't true there. When unsure, file narrow — promotion is cheap, demotion isn't.

**`/gt:gt-route` applies this table for you**, against the material actually in hand,
without your having to hold the table in your head mid-session. Reach for it when the
answer is not obvious — including when the honest answer is that the fact belongs to a
project other than the one loaded.

---

## Script reference

```bash
SCRIPTS=~/.claude/plugins/cache/golden-thread-plugin/gt/0.18.1/scripts

python3 $SCRIPTS/vault_init.py fresh --vault ~/my-vault --domain "My Team"

python3 $SCRIPTS/vault_init.py create-project --vault ~/my-vault \
  --name my-project --title "My Project" --tags "backend,api" \
  --domain platform --topology bastion-direct \
  --repo-url git@github.com:me/my-project.git --runbook

python3 $SCRIPTS/vault_init.py create-project --vault ~/my-vault \
  --name sub-feature --parent my-project --domain platform

python3 $SCRIPTS/vault_init.py connect --vault ~/existing-vault

python3 $SCRIPTS/vault_init.py install-core-rules --vault ~/my-vault

python3 $SCRIPTS/gt_ingest.py ~/Projects/my-project --json

python3 $SCRIPTS/gt_lint.py ~/my-vault --queue ~/my-vault/review-queue.md

python3 $SCRIPTS/gt_settings.py show
```

`gt_tasks.py` runs **from the vault**, at `Projects/golden-thread/tools/`, beside
`gt_adr.py`, `gt_closeout.py`, `gt_edits.py`, `gt_events.py`, `gt_log.py`, `gt_session.py`,
`gt_spool.py` and `safe_write.py`. All nine ship as
templates and are seeded there by `vault_init.py fresh` or `connect` (what
`/gt:gt-init` runs); `install.sh` refreshes copies that predate the installed
template. It regenerates `TASKS.md` from every project's `## Tasks` section.

```bash
python3 ~/my-vault/Projects/golden-thread/tools/gt_tasks.py
```

It infers the vault from its own location, so it needs no arguments; `--vault` is
there to override that. **Re-run it before reading `TASKS.md`** — project priority is
computed against the clock (stale-P1 ageing, deadline windows, `pp_escalate`), so the
ranking changes with time even when no file has changed. `TASKS.md` is generated and
must never be hand-edited.

`--json` prints the ranked rollup to stdout without writing `TASKS.md`. Deadline windows
may span days (0.13.0): `Fri 16:00 → Sun 16:44 America/Chicago -> 0`, including spans
that wrap the week; the zone always comes from the rule, never the machine.

`gt_lint.py --json` emits findings as JSON, and `gt_lint.py --runbooks` reports lines
duplicated across projects' runbooks — the detection step of `/gt:gt-runbook-lint`.

**Added in 0.18.1** — each is described where its activity is; this is the command line.

| Command | What it does | Exit |
|---|---|---|
| `gt_close.py project SLUG --vault V [--json] [--dry-run]` · `… --archive [--reason R] [--move]` · `task ID --vault V [--reason R]` · `handoff FILE --vault V --reason R` | close a project, a task or a handoff ([Closing things](#closing-things-gtgt-close)) | project: `1` while anything is undecided |
| `vault_init.py archive-project --vault V --slug S [--reason R] [--move] [--dry-run]` | archive in place; `--move` relocates to `Archive/<slug>/` | |
| `<vault>/…/tools/gt_task.py shelve ID --vault V --reason R` · `move ID --vault V --to SLUG --reason R` | keep a task at `p:: 7`; move it to another project | |
| `<vault>/…/tools/gt_adr.py allocate <project> --title T [--supersedes N] [--expires-when C] [--expires YYYY-MM-DD]` · `lineage <project> "<topic>"` | ADR fields; the supersession chain (read-only). `<project>` may be a bare sub-project slug | `2` unknown or ambiguous project, or `--expires` not a date; `3` an ADR number held twice |
| `gt_entities.py --vault V lookup NAME --project S` · `list --project S` | memory notes by declared entity (read-only) | `2` unknown project |
| `gt_brief.py --vault V SLUG [--repo PATH] [--dry-run]` | draft a repo `CLAUDE.md` section to stdout ([`/gt:gt-brief`](#gtgt-brief)) | `2` unknown project |
| `gt_catchup.py --vault V --project S [--brief \| --no-brief] [--mark] [--json] [--dry-run]` | the catch-up paragraph `/gt:gt-open` leads with; writes nothing but machine-local state with `--mark` | `2` usage · `3` project unreadable |
| `gt_digest.py write --vault V --project S [--dry-run] [--json]` · `check --vault V --project S [--json]` | write / check `research-digest.md` | write: `1` queued but held or escalated · `3` no `research.md`; check: `1` missing or stale |
| `gt_memory_check.py --vault V --project S [--changed NOTE …] [--work] [--json]` | contradictions between this session's memory notes and their neighbours (read-only) | `1` pairs reported |
| `gt_promote_detect.py --vault V [--project S] [--threshold PCT] [--work] [--json]` | promotion candidates (read-only) | `1` candidates listed |
| `gt_optimize.py --vault V [--only vault,session] [--cost] [--unused-days N] [--days N]` · `--archive --project S --before D [--apply]` · `--supersede --file F --entry H --by H [--apply]` | gt-optimize's members and write actions ([`/gt:gt-optimize`](#gtgt-optimize)) | `3` a member could not run |
| `gt_optimize_session.py [--days N] [--brief] [--check [PCT]] [--json]` | prompt-cache writes by cause | `1` avoidable · `3` over `--check` · `4` could not run |
| `gt_minimize.py [--session ID \| --transcript F \| --latest] [--json]` | this session's billed context, estimated breakdown, cache warmth (read-only) | `2` cannot tell which session · `4` could not run |
| `gt_handoff.py … [--session ID] [--since-commit SHA] [--dry-run]` | the handoff, with *What Changed This Session* | |
| `gt_sync.py status [--no-fetch] · pull · push [--dry-run] [--json] · behind [--mode cached\|fetch]` (hooks dir) | the vault against its git remote ([`/gt:gt-sync`](#gtgt-sync)) | `1` refused or git failed |
| `gt_reminder.py status · setup CH · check CH · mirror · run · import-tsv` | reminders through push channels ([Reminders](#reminders-outside-a-session-gt_reminderpy)) | `1` could not deliver · `3` no mirror |
| `gt_check.py list · run [--staged] [--event commit-msg]` | checkers modules contribute ([`gt_check.py`](#gt_checkpy-checkers-a-module-contributes)) | `1` fail or cannot-check · `3` nothing applied |
| `gt_apply.py list · show · apply · undo` | apply a checker's fix proposal through every gate | `1` refused or did-not-fix |
| `gt_model.py resolve · skill · check` | model intent → model ([Model intent](#model-intent-gt_modelpy-0181)) | `2` unknown intent |
| `gt_checkpoint.py find · show · prune` | resumable scans and ingests | |
| `gt_link_suggest.py suggest · apply` | link suggestions after a Knowledge write | `1` a write refused |
| `gt_review_stamp.py --vault V PAGE … [--date D]` | queue `last_reviewed` on Knowledge pages | `1` refused |
| `gt_pipeline.py init · list · check · add · set · remove · render · run · flag` | a project's release pipeline ([Release pipeline](#release-pipeline-gt_pipelinepy)) | `1` stopped at a failure |
| `gt_recipe.py render · check · show` | recipe → generated script; `check` catches a hand edit | |
| `gt_metrics.py record · time · report · peers · mark · verify` | execution rows and baselines ([Execution metrics](#execution-metrics-gt_metricspy)) | `1` regression, or a change that did not pay |
| `gt_optimize_exec.py` (= `gt_optimize.py --only execution`) | the opt-in execution member of gt-optimize | `1` findings |
| `gt_bench.py [--hosts] · health` | measure the parallel profile | `1` nothing measurable |
| `gt_load.py [--cap N] [--json]` | what a parallel run would start with now, and why | |
| `gt_ingest_pipeline.py stages · survey · packet · fan-in · reconcile · draft · promote-scan · promote-plan · status` | the deterministic stages of ingest and promote | `1` a stop · `3` incomplete |
| `~/.claude/golden-thread/hooks/guard_foreign_checkout.py list` · `add PATH [--label L] [--route R] [--dry-run]` · `remove PATH [--dry-run]` | declare checkouts another machine owns ([Foreign checkouts](#foreign-checkouts-a-commit-in-another-machines-checkout)) | |
| `gt_sandbox.py status · plan · apply [--force] · remove · check · verify · managed · rollback-check [--json]` (hooks dir, 0.20.1) | gt sandbox mode's Claude Code settings ([gt sandbox mode](#security-gt-sandbox-mode-0201)) | `1` refused, drift or FAIL |
| `gt_vault_mcp.py [--vault V]` (0.20.1) | the gt-vault MCP server over stdio; Claude Code starts it | |
| `gt_mcp_inventory.py [--json] [--cwd D] [--home D]` · `managed` (0.20.1) | every MCP server Claude Code may load here (user, local, project, plugin, claude.ai, built-in, managed), marking the ones connected directly, outside unlock and the sandbox; `managed` prints the managed-settings recipe (guidance only). Read-only; the doctor and `gt_unlock.py verify` use it | |
| `gt_uninstall.py --check` · `[--yes]` · `--keep-vault-config` · `--discard-queued` · `--purge-backups` · `--purge-lotr` · `--json` (0.20.1) | remove gt from this machine (jobs, plugin dirs and registries, sandbox entries, unlock daemon, `~/.gt-inbox`, `~/.gt-scratch`), then re-scan to prove it is gone; also `install.sh --uninstall [--check]` and `install.cmd /uninstall` | `1` a step failed or the re-scan found something · `2` refused (not confirmed, queued writes, no `~/.claude`) |
| `gt_review_target.py` (0.20.1; a library, shipped in the hooks dir and `templates/tools/`) | `review_target(vault, rel)` -> `None` or `(kind, why)`: is a vault path a `design.md` / `global-memory/` review target, by identity (inode, symlink) and by every spelling a file system folds; the broker's escalation test | |
| `gt_errors.py` (0.20.1; a library, shipped in the hooks dir and `templates/tools/`) | a vault tool the OS refuses prints ONE line, not a traceback, and says which cause: gt sandbox mode (use the gt-vault MCP or a terminal) or a macOS provenance refusal (another interpreter, or Full Disk Access); exit `5` | |

---

## Troubleshooting

**"vault-config.json not found"** — run `/gt:gt-init`.

**"Operation not permitted" writing the vault on macOS (EPERM).** macOS can refuse one Python
writes it allows another: a file carrying `com.apple.provenance`, or a privacy-protected folder.
Seen 2026-10-03: Homebrew's `python3.9` -- the default `python3` on that Mac -- could not replace
the vault's `log.md`, while `/usr/bin/python3` could. Since 0.20.1 gt words this as one line
naming the interpreter and the fix (`gt_log.py` exits `5`; the broker holds the request; nothing
is half-written), and `install.sh` picks ONE interpreter for hooks, tools and scheduled jobs by
an actual write probe -- create, write, `os.replace`, remove in the vault and in
`~/.claude/golden-thread`, and open `log.md` for append -- preferring `/usr/bin/python3` when it
is a working 3.8+. It is recorded in `~/.claude/golden-thread/interpreter.json` and
`~/.claude/golden-thread/python`; the hook wrappers and the `.py` hook commands in
`settings.json` use it instead of bare `python3`. Since 0.20.1, when the `python3` on your PATH
is one the probe refused, Claude's own shell and the gt-vault MCP server use the chosen one too:
a `python3` shim in `~/.claude/golden-thread/bin/` is put first on PATH for Claude's Bash
commands at session start (through `$CLAUDE_ENV_FILE`, as on Windows), and the installed
gt-vault MCP is started by the interpreter's full path. A `python3` that passes the probe is
never overridden, and your own terminal is untouched. To see where you stand:

```bash
python3 ~/.claude/golden-thread/hooks/gt_write_probe.py probe     # PASS/FAIL per interpreter
/gt:gt-doctor                                                    # the write-probe row
```

Fix: re-run `install.sh` (it re-probes and re-records), run gt's tools with the interpreter it
names, or give that Python Full Disk Access (System Settings > Privacy & Security). A recorded
interpreter that later disappears (a `brew upgrade`) shows as `badpath` in the wiring check
rather than letting every hook fail open.

**A rule in `CLAUDE.md` seems ignored** — it is read at session start. Added
mid-session, it applies from the *next* session. Same for hooks in
`settings.json`: open `/hooks` once or restart.

**Memory files aren't being read** — that is intended. `gt-open` reads
`MEMORY.md` and loads files on demand. Ask for one by name.

**A memory file is never offered** — run `/gt:gt-lint`; it is probably
`memory-unlisted`, so nothing knows it exists.

**`/gt:gt-query` can't find a page you added** — likely `index-gap`.

**Lint suppression isn't working** — paths are matched case-insensitively against
the finding path; make sure you used the path as the finding reports it.

**"conflict: vault-config.json points to a different vault"** — run
`vault_init.py connect --vault <new-path>`.

**A scheduled job fails, or `/gt:gt-doctor`'s `schedule` check says FAIL** — read the job's
`.err` log the check names. "on disk but not registered" means the scheduler (launchd, Task
Scheduler, systemd or cron) does not have the job, so it never runs: re-run `gt_schedule.py
install <job> ...`, which now says NOT INSTALLED, with the scheduler's reason, when it is
refused (0.20.1). `PermissionError` on a vault under CloudStorage from a
calendar-fired run is the known macOS privacy refusal (0.17.2 reports it; it does not fix it):
a run kicked from a session succeeds where the scheduled one is refused.

**`gt_schedule.py install sweep` refuses** — expected in 0.17.2. Its pre-flight proves every
member can run from the hooks dir, and they cannot yet find their packs there.

**The doctor's `version` row says the plugin source "is gone"** — the folder `install.sh` ran
from was deleted or moved (INSTALL.md option C installs from `/tmp`). Nothing is broken: the
doctor and the post-install gate check the installed copy in the plugin cache instead. Re-run
`install.sh` from wherever the source now lives to re-point the version check (0.20.1).

**`push` says "no remote configured — nothing to push"** — information, not a fault: the vault is
a git repo with no remote, so its history is on this disk only. Add one to back it up (the
line prints the two commands) or silence it with `gt_settings.py set push_check off`.

**`workers` lists shells of this or another session** — they are reported, never a finding,
while their `claude` is alive (0.20.1). Only an ORPHAN (its session gone) or a worker you
DECLARED that has stopped working needs a decision; `gt_workers.py reap` touches only stalled
orphans. On native Windows the row says the check is not supported there.

**Removing gt** — `bash install.sh --uninstall --check`, then `bash install.sh --uninstall`
(`install.cmd /uninstall` on Windows). See *Uninstall* in INSTALL.md for what it removes and
keeps; the vault is never touched.

**"no machine id" or "hostname changed" notices** — see
[Session identity](#session-identity-which-machine-wrote-a-claim). A rename is harmless and
reported once; a legacy file on a host that no longer matches keeps its claims only while its
heartbeat is fresh.

**`gt_settings.py set sandbox_mode off` fails with "could not write"** — expected inside
Claude Code's sandbox: `~/.claude` is write-protected there. Run it from a terminal. Turning it
**on** is refused on Linux / WSL2 without `bubblewrap` and `socat`.

**Under sandbox mode a gt script prints "refused: … sandbox mode: …"** — it ran in the
assistant's shell and touched the vault or `~/.claude/golden-thread`. Do what the line's
`Next:` says: use the gt-vault MCP tool it names, or run the exact command it gives in a
terminal. (A vault tool that Python itself cannot open — "can't open file … Operation not
permitted" — is the same refusal with `sandbox_vault_reads deny`; `allow` gives the shell
read access back.)

**A handoff keeps being shown** — that is intended until it is dealt with: run
`/gt:gt-handle handoff`, mark it handled, or defer it to a date. To see waiting handoffs only in
their own project, `gt_settings.py set handoff_surface project`.

**No `GOLDEN THREAD surface:` line at session start** — the hook is unwired or `surface` is
`off`. Run `/gt:gt-doctor` (`wiring`) and `gt_settings.py show`. A clean run still prints
"nothing waiting".

**`core-unenforced` finding in lint** — Core rules are stored but not being
asserted. Run `vault_init.py install-core-rules --vault <vault>` or wire the
hooks by hand. Do not suppress this finding.
