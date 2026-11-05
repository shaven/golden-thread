---
name: gt-optimize
description: "Find what costs context and earns nothing back, in the vault (duplicate facts, dead index rows, relative dates, memory over budget, unread pages) and in sessions (lost prompt-cache writes). Reports never write; --demote, --archive and --supersede move or mark content and leave a pointer. Use when the user says: what is costing context, optimize the vault."
plain:
  - cmd: "/gt:gt-optimize"
    does: "Finds what costs context and earns nothing back."
    how: "Reads the vault and your session history for duplicated facts, dead index rows and prompt-cache waste, and reports. Reporting never writes; the apply options move or mark content and leave a pointer behind."
    when: "Sessions feel slow or expensive."
    next: "/gt:gt-calibrate"
model_intent: balanced
---

# Golden Thread Optimize

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_optimize.py --vault … [--json]` (the report) | `vault_report` `{report: optimize, project}` for the vault member; `gt_optimize_session.py` reads transcripts and runs from the shell as written |
| `gt_optimize.py --archive` / `--supersede` (`--apply`) | `vault_optimize` `{action: archive \| supersede, …, apply}` — a preview unless `apply: true` |
| `gt_optimize.py --demote` (a note out of `global-memory/`) | **terminal**: `global-memory/` is changed by the owner, never by a gt tool |
| `gt_write_queue.py …` | `vault_queue_write` — the same `path`, `op`, `section`, `key` and `hint`, with the text itself as `content` (no `--session`: the server stamps its own). The broker decides at once and the result says apply, held or escalate. `design.md` and `global-memory/` are refused (owner only) |
| `gt_broker.py drain` | nothing to run after `vault_queue_write`; `vault_queue_drain` if anything was queued from the shell |
| `gt_log.py … add "<line>"` | `vault_log_add` `{line}` — the full line shape; with `--event …`, the same fields (`event`, `item`, `from`, `to`, `level_from`, `level_to`, `project`) |
| `gt_model_calibrate.py …`, `gt_model_policy.py set …`, `gt_settings.py models apply-calibration …` | **terminal**: the calibration pass spends allowance and writes a plan directory, and the policy command changes settings |
| `gt_optimize.py --execution …`, `gt_metrics.py verify …` | **terminal**: the execution member reads the vault's metrics and `--apply` changes settings |
| a write to `design.md` or `global-memory/` (the broker escalates one it is handed as a `#conflict`) | **owner only**: gt's MCP tools never edit them, with or without sandbox mode. Show the user the proposed text and the file it belongs in; they apply it in an editor. A `#conflict` task already raised for one points at its conflict file in `Projects/golden-thread/spool/broker/conflicts/` |

A **terminal** row is a step sandbox mode refuses on purpose — it changes settings or install state, or rewrites the vault wholesale. Give the user the exact command to run from a terminal, and carry on with the rest.

Memory is read into every session. A duplicated fact is paid for on every turn, in every
project, forever — this finds that waste. It never removes anything: what it writes is a
demotion, an archive of old research entries, or a supersede mark, and each leaves a pointer.

## Two members, one report

`gt_optimize.py` is an aggregator (the same `gt_aggregate` rules as `/gt:gt-scan` and
`/gt:gt-allin`) over two members:

| member | asks | cost paid |
|---|---|---|
| `vault` | what the vault **stores** that a session pays for | every session, forever |
| `session` | what sessions **carry**: prompt-cache writes by cause, from the Claude Code transcripts | once per resume |

A bare run runs both and ends with `N of 2 member(s) ran`. A member that could not run — no
transcripts on this machine, a crash — is printed as **COULD NOT RUN** and the exit is 3: that is
never a clean result. `--only vault` or `--only session` runs one.

## What it is not

This is not `/gt:gt-lint`. Lint asks *is the vault structurally correct* — broken links, missing
index rows. This asks *is the vault paying for words it does not need*. Different question,
different answer, run both.

## The two classes are labels, not permissions

The report is printed in two sections. Both are **reported and never applied** — the class says
how confident the finding is, not what may be done with it.

| class | printed as | what it is |
|---|---|---|
| mechanical | `MECHANICAL — decidable from the text` | a blank-line run, a `MEMORY.md` row pointing at a file that is not there |
| judgement | `JUDGEMENT — reported, never applied` | two facts saying the same thing, a memory file over budget, a relative date, a repeated line |

A repeated line is judgement, **not** mechanical. Measured against a real vault, the examples
found were an ADR's required `- **Rejected alternatives**:` section label and a line shared by
two Dataview queries. In prose a repeated line is waste; in a document built from repeated
structure it *is* the structure, and no script can tell which it is looking at.

## There is no mechanically-safe `--apply`

`--apply` **without** `--demote` is a usage error (exit 2), and there is no flag that applies the
mechanical class.

One existed, for about an hour on 2026-09-16. It was restricted to a "SAFE" class said to be
decidable from the text alone, and an independent validation took it apart: `dead-index-row`
deleted **live** rows, because the link target was parsed with `[^)]+` and used raw, so
percent-encoded names, titled links, angle-bracket links and `mailto:`/`obsidian://` links all
read as missing files — seven of the eight rows in the fixture were live, and a deleted row
carries the note's one-line description, which no diff brings back. Blank-run collapsing was not
fence-aware and rewrote the inside of code blocks. Findings computed from one read were applied
to another by line index. CRLF files were rewritten to LF and mode `0600` became `0644`.
`--project` was joined to the vault unvalidated and edited files outside it entirely.

It was **removed rather than repaired**. Each failure was a case the author had not thought of,
which is the point: the "safe" classification was a claim about the world, and the world kept
producing exceptions. The findings are still worth having — and the valuable ones were always
the judgement class, which needed a reader from the start.

## The ladder is about cost, not maturity

`/gt:gt-promote` moves knowledge **up** by how settled it is. `--demote` moves it **down** by how
often it is paid for:

| tier | read | cost |
|---|---|---|
| `global-memory/` | every session of every project | most expensive |
| `Projects/<slug>/memory/` | every session of one project | |
| `Knowledge/<page>.md` | when someone asks for it | cheapest |

A fact in `global-memory/` that only one project needs is charged to every session forever.
Moving it does not make it less true — it makes it cost what it is worth.

## Steps

**Step 1 — Locate the vault**

Use `$GT_VAULT` if set; otherwise `~/.claude/vault-config.json`. If neither → `/gt:gt-init`.
The vault is **always named explicitly** on the command line — never inferred (Core rule 2).

**Step 2 — Report. Reporting never writes.**

```bash
python3 <base_dir>/../../scripts/gt_optimize.py --vault "<vault>" [--project SLUG] \
    [--only vault|session] [--cost] [--days N] [--json]
```

`--project` takes a **slug, not a path**: a value containing `/` or `\`, or that is `.` or `..`,
or that resolves outside the vault, is a usage error. Each section prints its first 40 findings and then
a count of the rest. `--cost` adds the **open cost** table: per project, the lines and approximate
tokens `/gt:gt-open` actually loads (README, source, idea, research — skimmed as gt-open skims it
past 200 lines — decisions, design, spec, runbook, and the memory index; never the memory notes
themselves), the total, and the once-per-session CONVENTIONS + PROTOCOL. `--json` always carries
it as `open_cost`.

The **session** member reads `~/.claude/projects/**/*.jsonl` for the last `--days` (setting
`optimize_session_days`, default 30) and splits every cache write into `cold`, `growth`,
`expiry` (the gap outlived the TTL the prefix was written at — 5 minutes or 1 hour, read from the
write) and `invalid` (the cached prefix changed). Only the last two are avoidable. Dollars are
**list-price equivalents, not a bill**. For a one-liner, or a threshold gate, run the member
directly:

```bash
python3 <base_dir>/../../scripts/gt_optimize_session.py --brief
python3 <base_dir>/../../scripts/gt_optimize_session.py --check [PCT]   # exit 3 above PCT
```

When expiry dominates, the advice is the user's habit, not the vault: cut before stepping away
(`/gt:gt-minimize`), not after.

Read the report with the user. Most findings are for a person to act on by editing the file.

**Step 3 — Work the judgement list with the user**

These are the valuable ones and they need a person:

- `duplicate-fact` — the same sentence in two context-loaded files. Decide which one owns it and
  link to it from the other. Usually the answer is "the more specific scope wins".
- `memory-bloat` — a `global-memory/` file over 30 non-blank lines. The message names the
  `--demote` command for it. Remember Core rule 6: global-memory is only for what EVERY project
  needs.
- `relative-date` — "last week" in a file injected into a session months later. Replace with the
  absolute date. Only flagged in silently-loaded files, where it genuinely misleads.
- `duplicate-line` — check whether it is structure before removing it.
- `single-project-global` — a `global-memory/` note whose body names exactly one project (the
  evidence `/gt:gt-lint`'s `global-scope-leak` reads, sharpened to the demotion case). It is that
  project's memory charged to every other project. The message names the `--demote` command.
- `knowledge-unused` — a `Knowledge/` page with no read logged in `--unused-days` (default 90),
  or **never read** since the access log began. Reads are logged by the `log_knowledge_read`
  hook (setting `knowledge_access_log`) to `usage/knowledge.jsonl`, which is never committed.
  With no log yet, nothing is reported. A candidate for review, not for deletion: some pages are
  rightly rare.

When the user agrees a fix and you make it, it goes through the write queue (Core rule 1),
never Write, Edit or a shell redirect: write the section's corrected body to a scratch file
outside the vault and queue it as `--op replace-section`:

```bash
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
    --path "<vault-relative file>" --op replace-section --section "<heading>" \
    --content-file <scratch>/<file>.md --hint "gt-optimize: <finding id>"
```

A fix to a `global-memory/` file is not applied by the broker: it goes to the owner for
review as a task, which is intended. A line above a file's first `## ` heading goes as
`--op replace-file`: read the file, build its full new text, and queue that. The broker
escalates rather than overwrite if the file changed after you read it.

**Step 4 — Demote, when a note belongs somewhere cheaper**

```bash
# preview — nothing moves
python3 <base_dir>/../../scripts/gt_optimize.py --vault "<vault>" \
    --demote global-memory/<note>.md --to project-memory --project <slug>

# do it
python3 <base_dir>/../../scripts/gt_optimize.py --vault "<vault>" \
    --demote global-memory/<note>.md --to project-memory --project <slug> --apply
```

`--to` is `knowledge` or `project-memory`; omitted, it is one tier cheaper than where the note
sits. `--to project-memory` requires `--project <slug>`. The path is relative to the vault. The
work is done by `gt_demote.py`, which `gt_optimize.py` delegates to so the order below has one
implementation and one set of tests.

**The order is the safety property:**

1. **write** the destination, with a provenance comment naming where it came from;
2. **verify** it by re-reading it from disk and comparing digests — then re-read both sides again
   immediately before the source is touched, because the invariant is not "it was there" but "it
   is there now";
3. **only then** replace the source with a small pointer to where it went.

A failure at any step leaves **duplication** — the same content in two places, which a reader can
see and resolve. The old `--apply` failed by **deletion**, which no diff brings back. That
asymmetry is the entire reason demotion exists as a separate command rather than as another
`--apply`.

A `MEMORY.md` row naming the moved file is **not** rewritten. The run says so, before and after:
those rows now point at the pointer, which is correct and worth tidying by hand.

**Step 4b — Archive old research entries, or mark one superseded**

`research.md` is append-only and only grows; `/gt:gt-open` skims it past 200 lines, and a March
finding sits above the April entry that corrected it with equal weight. Two actions, both
**previews unless `--apply`**:

```bash
# move every `## YYYY-MM-DD...` entry before the cutoff to research-archive-<YYYY>.md
python3 <base_dir>/../../scripts/gt_optimize.py --vault "<vault>" \
    --archive --project <slug> --before 2026-01-01 [--apply]

# mark one entry superseded by a later one, in place
python3 <base_dir>/../../scripts/gt_optimize.py --vault "<vault>" --supersede \
    --file Projects/<slug>/research.md --entry "<old heading>" --by "<new heading>" [--apply]
```

- **Archive** writes the archive file(s) first, through the write queue, reads them back, and
  only when every moved entry is verified on disk rewrites `research.md` — leaving one dated
  line per moved entry under `## Archived entries`, linking to it. A heading that is not a
  dated entry is never moved. If `research.md` changed in the meantime it is left alone and the
  entries simply exist in both places — visible, never lost.
- **Supersede** adds `> superseded_by: [[file#heading]] — marked <date>` directly under the old
  entry's heading (the `superseded_by:` idiom `Sources/` already uses). The entry stays, word for
  word. An `--entry` or `--by` heading that matches zero or several headings is refused.
- Both refuse a file another live session has claimed (Core rule 1), naming the holder.
- Neither summarises or rewrites anything. Compressing memory is out of scope on purpose: the
  line a summary drops is the one nothing downstream can notice is gone.

Choose the cutoff with the user; show the preview first, always.

**Step 5 — Record**

Log the run with `work`, through the tool — never append to `log.md` by hand:

```bash
python3 "<vault>/Projects/golden-thread/tools/gt_log.py" --vault "<vault>" add \
    "<today> [work] gt-optimize: <what was fixed or demoted>"
```

If facts were merged or moved, note where they went in the project's `research.md`, so the
next session does not go looking for the copy that was moved — queued as an append:

```bash
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
    --path "Projects/<slug>/research.md" --op append --content-file <scratch>/research.md
```

If anything was queued in Step 3 or here, apply the queue once and tell the user in one line
what the broker applied, held or escalated:

```bash
python3 <base_dir>/../../scripts/gt_broker.py drain --vault "<vault>"
```

## What a demotion refuses

Refusals live in the script because `guard_protected_paths` is a PreToolUse hook: it sees the
model's Write tool and never sees a script writing a file, so script-driven edits would otherwise
walk straight past the guard. These are the ones the code performs:

- any path segment resolving to **`core-rules/`** or **`Sources/`** — compared on the resolved
  path, casefolded and NFC-normalised, so `sources/` and `CORE-RULES/` do not slip past on a
  case-insensitive volume;
- anything **outside the three tiers** — `global-memory/`, `Projects/<slug>/memory/` and
  `Knowledge/` are the only places it moves notes between;
- a note that **is, or sits under, a symlink**, and any path that resolves **outside the vault**;
- a note already at the **cheapest tier** with no `--to`: there is nowhere further down;
- `--to project-memory` with **no `--project`**, or a `--project` that is not a plain slug
  (`[A-Za-z0-9._-]`, 1–64 characters);
- a **destination that already exists** or is a symlink — merging two notes is a judgement a
  person makes;
- a source that is **not valid UTF-8**: it will not rewrite bytes it cannot read;
- a note **another live session has claimed** (Core rule 1) — read from the vault's session
  registry, ignoring this session's own claim and any released or stale one, and reported in a
  dry run as well as under `--apply`, because learning at apply time that someone else has the
  note open is learning one step too late. **A session file that cannot be read also refuses**:
  whether anyone holds the note is then unknown, and unknown is not clear.

  This last refusal was documented in `gt_demote.py` from 0.16.1 while no code performed it, and
  was implemented in 0.16.2 once a documentation sweep read the file against its own source. It
  is worth knowing as a worked example rather than hidden: prose in a file is not evidence about
  that file.

## Exit codes

| Code | Reporting | With `--demote` | With `--archive` / `--supersede` |
|---|---|---|---|
| 0 | every member clean | done, or a preview printed (no `--apply`) | done, or a preview printed |
| 1 | findings reported | refused, and the reason is printed | refused (claimed, no match, ambiguous heading) |
| 2 | usage — bad vault, bad `--project`, unknown `--only` member, or `--apply` without an action | usage — traversal in the path, or no such note | usage — missing `--before`, `--file`, `--entry` or `--by` |
| 3 | a member **could not run** — never a pass | an error mid-move; nothing was removed, so the content exists in both places | a write did not land; nothing was removed (an archive that landed while research.md did not leaves the entries in both places) |

`gt_optimize.py --member vault` (what the aggregator runs) keeps the pre-0.18 codes: 0 / 1 / 2.

## Rules

- **Report before demote, always.** A tool that reports and edits in one breath is one people
  stop running.
- **Never act on a finding on the tool's say-so.** The class is a confidence label. Deleting
  knowledge is not reversible by reading a diff.
- **Never edit `core-rules/` with this.** It is read, and reported on, and never written. A Core
  rule is re-asserted into every turn of every session; it is changed deliberately, by a person,
  with the reasoning recorded.
- Only files this tool is *for* are looked at — memory, global-memory, core-rules, CLAUDE.md and
  the living project docs. Generated files, `Sources/` and recorded artifacts are never reported.

## Execution: how work runs, not what it costs in context (0.18.1)

```bash
python3 <base_dir>/../../scripts/gt_optimize.py --execution --vault "<vault>" [--project SLUG] [--repo <code root>] [--json]
```

Reads what was MEASURED — the execution rows `gt_metrics.py` records for every project (tests,
release-pipeline steps, gt skills), the test runner's per-unit timings, the parallel profile and
the execution settings, and with `--repo` the repo's shell scripts — and reports, ranked by
measured cost with the expected saving: serial bottlenecks, redundant re-runs, the full suite run
where a scoped run would do, slow steps and slow test units, repeated installs, regressions
against each process's own rolling baseline (naming what changed since), a much cheaper peer
process in another project (and what it does differently), defaults never revisited, and
hand-written step scripts a recipe could generate (`gt_recipe.py`). Findings about gt's own
development are labelled `gt-development`; every other project's are about its own processes.

Reporting writes nothing. To apply one, **ask the owner first**, then:

```bash
python3 <base_dir>/../../scripts/gt_optimize.py --execution --vault "<vault>" --apply <ID> [--dry-run]
```

Only a finding with an exact change (a setting, a measurement) can be applied; it is recorded as
a change marker with its rollback, and `gt_metrics.py verify --process <P> --project <SLUG>`
later reports the measured before/after against the baseline. A change that did not beat it is
reported as such, with the rollback offered.

## Calibration: is each agent stage on the right model and effort? (0.21.0)

Off by default. With `model_calibration` set to `report` (`/gt:gt-settings`), a bare run adds a
third member, `calibration`. `--only calibration` runs it on its own **whatever the setting says**
(naming it is the request; one line says the setting is off) — and `/gt:gt-calibrate` is that, by
its own name. Every guard below still applies. With the setting off and no explicit request nothing
here runs and nothing is spent.

```bash
python3 <base_dir>/../../scripts/gt_model_calibrate.py report --vault "<vault>"
```

It samples recent runs of each agent stage (the newest `model_calibration_sample`, from pipeline
units whose prompt was kept), re-runs each one tier down, one tier up, one effort higher and one effort lower, and
has a zero-context deep-tier adjudicator judge the outputs. Every sampled prompt goes through the
intake scan first; one that fails is excluded, counted, and never sent. The pass prices itself
before it starts and **refuses, running nothing, when the estimate exceeds
`model_calibration_budget`** — say so and stop; do not raise the budget for the owner.

**With a runner** (`--runner`, `$GT_CALIBRATION_RUNNER` or the saved `model_calibration_runner` — which ships
set to the command below, so this is the normal case — a command given the prompt on stdin,
e.g. `claude -p --model {model} {effort_args} --no-session-persistence --strict-mcp-config --tools {tools} --allowedTools {tools} --settings '{"disableAllHooks":true}'`; `{tools}` is filled with the stage's own tools, so a candidate can read what the real agent reads) the pass runs
and scores in one go. **Without one** (the setting set to empty on purpose) it writes a plan and prints its directory; then:

1. `gt_model_calibrate.py units <plan-dir> --pending --agent-tool --json` lists each unit with
   its model and prompt file. Spawn one Agent per `pending` unit with that `model` and the prompt
   file's contents as the prompt (add: "Return only the JSON, free-text fields at most 40 words"), and save its JSON with
   `gt_model_calibrate.py record <plan-dir> <unit-id> --result-file <F>`. Units marked
   `needs-runner` are the effort runs: the Agent tool takes no effort, so leave them; the report
   will say effort was not calibrated, and why.
2. Run `units ... --pending --agent-tool --json` again: the `adjudicate` units are now ready
   (their prompts hold the outputs, labelled and shuffled). Spawn and `record` them the same way.
3. `gt_model_calibrate.py score <plan-dir>` prints the verdicts.

With `skill_runlog` on, the same pass also grades the **skills** (0.21.4): gt keeps a
local log of how each gt skill ran (a SessionStart catch-up over the transcripts; model, effort, tokens,
wall clock, outcome and a redacted, intake-scanned digest of the task), and a skill with
`skill_runlog_min_runs` completed runs (default 5) is replayed on a sample of its digests one tier
down, one up, one effort higher, one effort lower and at its current choice, in text with no tools, and judged by the
same adjudicator. The units, the budget and the weekly-allowance line are the same ones, the report
has a **SKILL CALIBRATION** section, each verdict prints
`gt_model_policy.py set --skill <name> --model <m> [--effort <e>]`, and `--skill <name>` limits the
pass to those skills. With `skill_runlog` off none of this appears.

Show the owner each stage's verdict — **keep**, **move-down**, **move-up**, **raise-effort**, **lower-effort** or
**INVALID RUN** (every candidate failed, or none could read its input: no verdict, and the runner's
stderr tail is shown) —
with its agreement, missed and added findings, schema failures, refutations and the cost of each
choice, and the exact `gt_model_policy.py set --agent ...` command or
`gt_settings.py models apply-calibration <stage>` (the verdicts are stored in
`~/.claude/golden-thread/model-calibration/latest.json`; `/gt:gt-settings models` shows them beside
every agent type). **Never run that command yourself**; the owner applies a recommendation or ignores it. Stages without enough recorded runs
are listed as not calibrated, with the reason.
