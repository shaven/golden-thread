# Golden Thread

> **Reader:** someone who has never heard of Golden Thread
> **Claims last checked against the code:** 2026-10-02 (gt 0.19.1) — see *The documents, and what belongs in each* in [`CLAUDE.md`](CLAUDE.md).

A memory system for AI coding sessions, built on plain markdown and git — and,
unusually, one where the rules that matter most are **mechanically enforced** rather
than merely written down.

Every AI session starts with amnesia. You explain your conventions, the session does
good work, the session ends, and it is all gone. Golden Thread is one Obsidian vault
that acts as shared memory between you and every session you run: sessions read from
it at startup, look things up while working, and write back what they learn.

Its distinguishing idea is the second problem, the one most memory systems never
address: **writing a rule down does not mean it gets followed.**

Plugin **v0.19.3**. Ten Core rules currently enforced, five of them *validated* — a
hook inspects the finished reply (`Stop`) or the tool call about to run (`PreToolUse`)
and blocks it if the rule was broken.


> [!IMPORTANT]
> **0.19.3: Windows, finished.** `python3` now works in Claude's shell on Windows (a shim gt
> installs), every gt write is LF, scheduled jobs run on Task Scheduler, and the test suite runs
> on Windows. On every platform a failed install now rolls back to the state before it, and the
> scheduled jobs keep the interpreter that can write your vault instead of the one that ran the
> install. See *Windows* in [`INSTALL.md`](golden-thread-plugin/INSTALL.md).

> [!IMPORTANT]
> **0.19.2: gt installs on native Windows.** Run `install.sh` from Git Bash, or the new
> `install.cmd` from cmd.exe, PowerShell or Explorer. The installer finds a real Python 3.8+
> (the Microsoft Store Python does not count, and it says what to install when that is all
> there is), and the hooks run under Git Bash as Claude Code for Windows runs them. Nothing
> changes on macOS or Linux. See *Windows* in [`INSTALL.md`](golden-thread-plugin/INSTALL.md).

> [!IMPORTANT]
> **0.19.1: right-sized models, SSO for LOTR, and fixes from installing 0.18.1.** (There is no
> published 0.19.0.) **Model and effort per skill:** every skill declares fast/balanced/deep, and the
> installer writes a profile into the installed copies — `average` (haiku with no effort setting,
> sonnet·medium, opus·high; a new install's default), `very-high` (opus·xhigh) or `inherit` —
> with per-skill and per-plugin overrides (`gt_model_policy.py`) and a doctor `model-policy` row;
> specialist agents run on the model their task needs (`agent_models`, haiku/sonnet/opus by tier).
> **LOTR handles SSO:** `lotr add-mcp` fronts an SSO/OAuth MCP endpoint by reusing its client's
> token by reference, refreshing on 401 (gt-lotr 0.2.0). **Recall:** `gt_bench.py recall` measures
> how often lookup finds the right page; optional prompt hints (`vault_hints`, off by default).
> **Supersession and expiry** applied when notes are read (`gt_supersede.py`). **Fixes:** the
> queue guard reads shell tokens and follows `cd`; nothing writes bytecode into gt-src; post-install
> resolves the installed release from any path and every completed run writes its receipt; one
> recorded interpreter for every launchd job; the commit gate fits a large repo; the Stop validator
> no longer demands a timestamp from a turn that was never given one (the install-time alert).
> After installing, restart and run `/gt:gt-upgrade`.
>
> **0.18.1: one verb per action, a plan before code, and less to carry.**
>
> - **One skill per verb.** `/gt:gt-create`, `/gt:gt-open`, `/gt:gt-list`, `/gt:gt-handle` and the
>   new `/gt:gt-close` take what they act on — a project, a task, a handoff — as their argument.
>   Closing a project walks every open item to a decision, offers what it learned for promotion, and
>   archives it in place. The old task and handoff skill names still work through 0.18.x.
> - **Plan, then implement.** `/gt:gt-plan` writes a phased, test-first plan and waits for your
>   approval; `/gt:gt-implement` runs it one phase at a time, stops on any red, and commits only
>   when you say so.
> - **Decisions you can trace.** ADRs can record when they stop being true and what they replaced;
>   "how did we decide X?" prints the chain; `/gt:gt-brief` drafts a repo's `CLAUDE.md` from the
>   standing decisions.
> - **Less to carry.** `/gt:gt-optimize` now measures what your sessions spend rebuilding the prompt
>   cache, and can archive old research; `/gt:gt-minimize` prunes a session before you step away;
>   `/gt:gt-open` leads with a short catch-up after time away.
> - **Across machines and outside sessions.** `/gt:gt-sync` pulls and pushes the vault between
>   machines (fast-forward only); reminders reach you by macOS notification, SMS or Discord through
>   your relay, or email — each off by default, each with a test that sends a real message.
> - **A release pipeline for every project.** `release.sh`, generated from a table of steps and
>   gates you extend, stops at the first failure and never skips; gt measures every execution
>   (`gt_metrics.py`) and `/gt:gt-optimize --only execution` proposes faster ways to run them.
> - **Guards.** A commit or push in a checkout you declared as another machine's is refused, and
>   `/gt:gt-doctor` says which repo a review command will actually look at. Run `/gt:gt-upgrade`
>   after installing.
>
> **0.17.11** made every vault write go through the write queue (Core rule 1) and added the
> optional `gt-lotr` gateway; **0.17.10** a write broker and specialist agents; **0.17.3–0.17.5**
> gt-src checksums and install fixes. Details in the [CHANGELOG](CHANGELOG.md).

> **0.17.2: what is waiting on you is in front of you when a session starts — and easy to clear.**
>
> - **A MUST DO block at every start**, from one table in `<vault>/deadlines.md`: overdue items
>   🔴, items due within 14 days 🟡, counted down live. Two credential rotations once sat 15 days
>   overdue at top priority because priority is a sort order, not an alarm.
> - **Handoffs keep coming back until someone deals with them** — one line each, never the
>   body — and a count of urgent tasks waiting on you. Switch any of it off in `/gt:gt-settings`.
> - **Tasks the way a developer writes a TODO.** Add one tied to a page or source if you like,
>   list them without opening anything, and clear a backlog — done, dropped with a reason, or
>   deferred to a date (since 0.18.1: `/gt:gt-create task`, `/gt:gt-list`, `/gt:gt-handle`).
>   Handoffs get the same pair. Nothing is ever deleted.
> - **Session claims survive a network change** — keyed on a machine id instead of a hostname.
> - **Known:** macOS blocks scheduled (launchd) runs from reading a vault under CloudStorage; the
>   health check now says so. Details in the [CHANGELOG](CHANGELOG.md).

> [!NOTE]
> **0.16.2 closes the seam where the rules went quiet.** Context a hook adds does not
> survive compaction — project-root `CLAUDE.md` is re-injected from disk, hook context is
> summarised with everything else — so for the remainder of a turn in which auto-compaction
> fired, the Core rules were present only as whatever the summariser kept. They are now
> re-asserted on `SessionStart`/`compact` as well as every prompt. The mechanical tier was
> never affected: `PreToolUse` guards and the `Stop` validator are commands, not context.
>
> Also: `SessionEnd` hooks share a 1.5-second budget, and a hook that overruns is cancelled
> with its **output discarded** — a report card could have started truncating invisibly. Hooks
> can now declare a `timeout`. And the copyleft refusal knows the current SPDX spellings, so
> `GPL-3.0-or-later` is refused *as copyleft* rather than as an unrecognised identifier.
>
> **`/gt:gt-context` is explicit that its envelope is not a security control.** Published work
> (arXiv:2503.00061) broke all eight defences it tested, above 50% attack success in every
> case. The envelope makes content identifiable as someone's definition; what actually protects
> you is that packs are reviewed before merge.

> [!NOTE]
> **0.16.0 gave the definitions somewhere to come from, and something to read them.**
> Golden Thread has **no plugin runtime** — nobody's code runs on your machine as a
> third-party add-on. Contributions arrive as **packs**: plain JSON data, reviewed and
> merged into gt itself, after which they are first-party and held to the same release
> gate as everything else. See [SUBMISSIONS.md](SUBMISSIONS.md).
>
> The packs now have consumers. **`/gt:gt-scan`** checks code against the language
> definitions in effect on this machine — and knows nothing about any language itself, so
> four small packs teach it one it has never seen, with no code change. **`/gt:gt-optimize`**
> reports vault content that costs context and earns nothing back. **`/gt:gt-handoff`**
> writes the next session a handoff that marks what it must not assume. **`/gt:gt-allin`**
> runs every check and tells you how many actually *ran*, and **`/gt:gt-allin-commit`**
> commits only once a passing test receipt covers every staged file.
>
> A definition you disagree with is switched off from your own vault:
> `{"retract": [{"lang": "go"}]}`. Only your packs may retract, so a contributed pack can
> never retire someone else's definition.

> [!NOTE]
> **0.15.0 made gt a small core plus six optional modules**, and an upgrade no longer
> changes anything you wrote without saying so and asking. Three commands moved
> (`/gt:gt-watch` → `/gt-watch:gt-watch`, `/gt:gt-farm` → `/gt-farm:gt-farm`,
> `/gt:gt-demo` → `/gt-demo:gt-demo`), and the installer tells you. New with it: the vault
> records how knowledge moves as events, and **`/gt-flow:gt-flow` draws it** — see
> [Seeing how knowledge moved](#seeing-how-knowledge-moved). Choose modules with
> `install.sh --list-modules`, `--without <name>` and `--with <name>`; details in the
> [CHANGELOG](CHANGELOG.md).

> [!NOTE]
> **Since 0.12.4, work runs in parallel by default, and you will notice it.** A Core rule now
> asks for divisible work to be split across your processors instead of crawling through
> one core, and the tools here honour it: `tests/run.sh` and `dev/render-pdfs.sh` fan out,
> and so will anything Claude writes while the rule is active. **The first sign is
> usually your fans** — a suite that used to hold one core at 100% now holds sixteen at
> 400%+, and Activity Monitor fills with `python3` or `Google Chrome` processes that all
> disappear when the run ends. That is the feature working, not a runaway job. The
> measurement behind it: the test suite went from 509s to 108s, 4.7x.
>
> **You are in charge of how much of your machine it may use.**
>
> ```bash
> gt_settings.py set parallel_max 4      # never more than 4 workers, whatever the work
> gt_settings.py set parallel_work off   # serial, and the Core rule stops being injected
> gt_settings.py show                    # what is currently allowed
> ```
>
> **Also new in 0.12.5:** a `git commit` carrying code whose tests have not been seen to
> pass is **refused**. Evidence is a receipt written by your test run (`tests/run.sh` and
> `dev/release-check.sh` write their own; any project can with `gt_test_receipt.py record
> --ok`). If a repo has no tests, exempt it once with `touch .gt-no-test-gate`; for a
> single commit, `GT_TEST_GATE=off git commit …`; to switch it off entirely,
> `gt_settings.py set test_gate off`. Docs-only commits are never blocked.
>
> `parallel_max` defaults to `auto` — as many workers as the machine allows, and never
> more than there are units of work. Cap it before a long run if you need the machine to
> stay responsive for something else, if you are on battery, or if a remote end is
> rate-limited. `off` is the full stop: nothing runs in parallel and the rule is not
> asserted at all.

## Who this is for

You run several projects at once and the ideas never arrive in the right one. You are
deep in project A when the fix for project B occurs to you, a session ends before the
finding is written down, and by Monday you cannot remember which of the six things you
touched last week was the urgent one. If your attention scatters, from ADHD or simply
from load, a system that depends on remembering to file things in the right place will
not hold.

Golden Thread is built so that **capture never waits for the right context.** Write the
thought as one line in the vault's inbox, from whatever session or project you happen
to be in. A review sweep later routes each line to the project it belongs to. Every project keeps its open tasks in one place, and a single
generated rollup ranks them across *all* projects, escalating by age and deadline, so
"what should I work on?" has one answer instead of six status pages.

The result is one place to look. Sessions read from it at startup, write back what they
learned at the end, and the rules you most need to hold are re-asserted every turn
instead of trusted to memory.

## Feedback

If you are using Golden Thread, or tried it and bounced off, I would like to hear about
it. Questions, ideas, and "this worked for me" belong in
[Discussions](https://github.com/shaven/golden-thread/discussions). Bugs and anything
that behaved differently from the documentation belong in
[Issues](https://github.com/shaven/golden-thread/issues). Both are read.

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
| `core_concurrent_session_claim` | **validated** | Write vault content only through the write queue and apply it with `gt_broker.py drain`; never edit a vault file directly, and never write one another live session has claimed (queue-first since 0.17.11) |
| `core_no_secrets_in_transcript` | **validated** | Never put a secret's value into the session — not to inspect, redact or check it |
| `core_timestamp_every_message` | **validated** | Begin every reply with the current wall-clock timestamp |
| `core_global_memory_scope` | reminder | `global-memory/` holds only facts needed in *every* project |
| `core_memory_load_policy` | reminder | Do not auto-load the full memory index |
| `core_verification_state` | reminder | Label every derived figure with `unverified`, `self-verified` or `independently verified` |
| `core_explicit_vault_target` | **validated** | Name the vault on every mutating tool run — `--vault` or `--dry-run` |
| `core_secrets_live_in_the_store` | reminder | A secret's value rests only in the secrets store, never in source, a repo, a log or a session |
| `core_parallel_when_beneficial` | reminder | Parallelise divisible work in every project, within one `parallel_max` budget shared by every session on the machine; serial must be justified |
| `core_test_before_commit` | **validated** | Never commit code whose tests you have not seen pass; per-repo opt-out with `.gt-no-test-gate` |

Two ways in: the user **designates** a rule, or an existing fact is **promoted** and
must answer three questions — would its absence cause incorrect code, cause rework, or
cascade into lower-level rules being written wrongly?

Two of the validated rules show why both paths exist. `core_no_secrets_in_transcript`
answers yes to all three. `core_timestamp_every_message` answers **no to all three** and
is Core anyway: it is the *canary*, kept because its absence is visible rather than
costly, so a broken hook announces itself.

## What is in this repo

| Path | What it is |
|---|---|
| `golden-thread-plugin/golden-thread/<ver>/` | The `gt` plugin — 36 skills, scripts, templates, hooks, packs |
| `golden-thread-plugin/golden-thread-wiki/<ver>/` | Module `wiki` (plugin `gt-wiki`) — 5 skills for LLM wiki vaults |
| `golden-thread-plugin/golden-thread-demo/<ver>/` | Module `demo` (plugin `gt-demo`) — the guided PizzaBot 3000 tour |
| `golden-thread-plugin/golden-thread-watch/<ver>/` | Module `watch` (plugin `gt-watch`) — follow upstream git repos, P0 on a security fix |
| `golden-thread-plugin/golden-thread-report-card/<ver>/` | Module `report-card` (plugin `gt-report-card`) — the session report card and close-out question |
| `golden-thread-plugin/golden-thread-farm/<ver>/` | Module `farm` (plugin `gt-farm`) — work packets for an external AI service |
| `golden-thread-plugin/golden-thread-flow/<ver>/` | Module `flow` (plugin `gt-flow`) — an offline timeline of knowledge moving up the ladder |
| `golden-thread-plugin/golden-thread-visualize/<ver>/` | Module `visualize` (plugin `gt-visualize`) — a codebase in 3D: how-it-works walkthroughs and a code city |
| `golden-thread-plugin/golden-thread-usage/<ver>/` | Module `usage` (plugin `gt-usage`) — the plan-allowance meter, quiet until a window is near its ceiling |
| `golden-thread-plugin/golden-thread-lotr/<ver>/` | Module `lotr` (plugin `gt-lotr`) — LOTR, also called gt MCP: one gateway in front of GitHub, Jira, Microsoft Graph and other REST APIs; off by default |
| `golden-thread-plugin/install.sh` | Installs gt and every module that is on, wires the hooks, applies upgrades |

The vault *content* lives in a separate private repo. This one is the machinery.

## Documentation

The guides live one level down, under [`golden-thread-plugin/`](golden-thread-plugin/),
alongside the code they describe. Start with Getting Started; the Manual is the reference.

| Document | What it covers |
|---|---|
| [Changelog](CHANGELOG.md) | What changed in each release, newest first |
| [Announcements](../../discussions/categories/announcements) | Release write-ups: what broke, what changed, and what you have to do |
| [Getting Started](golden-thread-plugin/ONBOARDING.md) · [PDF](golden-thread-plugin/ONBOARDING.pdf) | A guided first session in six steps, about fifteen minutes |
| [User Manual](golden-thread-plugin/MANUAL.md) · [PDF](golden-thread-plugin/MANUAL.pdf) | Complete reference: every skill, the vault layout, verification, the promotion ladder |
| [Install Guide](golden-thread-plugin/INSTALL.md) | Installing gt and its modules, choosing modules, upgrading and rolling back, wiring the hooks, adopting an existing vault |
| [Obsidian & Daily Workflow](golden-thread-plugin/OBSIDIAN-WORKFLOW.md) · [PDF](golden-thread-plugin/OBSIDIAN-WORKFLOW.pdf) | Living in the vault day to day — daily notes, properties, Dataview |
| [Developer Guide](golden-thread-plugin/golden-thread-developer-guide.html) · [PDF](golden-thread-plugin/golden-thread-developer-guide.pdf) | Internals: hooks, scripts, the component manifest, extending the plugin |
| [Plugin Documentation](golden-thread-plugin/golden-thread-docs.md) · [HTML](golden-thread-plugin/golden-thread-docs.html) · [PDF](golden-thread-plugin/golden-thread-docs.pdf) | The combined document — overview, Core rules, every skill, install and operation, in one file. Refreshed to current on 2026-09-09 (it had been frozen at gt 0.6.0 for six releases); now tracks the shipped release and is checked for drift by `build-docs.py` |
| [`docs/workflow.html`](docs/workflow.html) · [`docs/ingesting.html`](docs/ingesting.html) | Standalone diagrams of the work and ingest loops |
| `golden-thread.pdf` | The earliest write-up here (2026-08-10); predates the current plugin layout, kept for reference |

The PDFs and HTML are rendered from the markdown beside them — when the two disagree,
**the markdown is the source of truth.**

## Install

```bash
bash golden-thread-plugin/install.sh --vault <path>
# then restart Claude Code — plugins and hooks load at session start
```

That installs gt and every **module** that is on. Nine ship: `wiki`, `demo`,
`watch`, `report-card`, `flow`, `visualize` and `usage` are on by default; `farm` is off for a
fresh install and kept on when you upgrade from a gt that had `/gt:gt-farm`; `lotr` (new in
0.17.11) is off until you ask for it with `--with lotr`. Choose with `--list-modules`,
`--without <name>` and `--with <name>`; the choice is remembered. Re-running it upgrades from any older release to
the newest, removing what old releases left behind and applying vault upgrades when your
vault is committed. See the [Install Guide](golden-thread-plugin/INSTALL.md).

Once installed, code in your own projects passes four gates by default before it reaches its
repository, and gt never pushes — [Default release gates](golden-thread-plugin/MANUAL.md#default-release-gates-what-your-project-passes-before-it-reaches-its-repository):

![Default release gates: tests seen to pass, gt-allin, gt-allin-commit and the commit guard, then your own push](golden-thread-plugin/docs/release-process.svg)

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

Thirty-six skills in gt, plus the skills of its modules (listed after gt's own). Each composes through files rather than through other skills, so
removing any one leaves the rest working.

| Skill | What it does |
|---|---|
| `gt-init` | Sets up the vault and wires it to a project. Writes `vault-config.json`, the pointer every other skill resolves through, and adds the Golden Thread section to your global `CLAUDE.md`. Idempotent — safe to re-run on a new machine. |
| `gt-create` | Creates a project, a task or a handoff. A project: slug, domain, topology, tags, optional sub-project parent; fills `idea.md` from what you actually said, then freezes it — that file is the traceable *why*, never rewritten when the plan changes. A task: one well-formed line in the project's `## Tasks`, the way a developer drops a TODO, optionally tied to a page or source that must exist. A handoff: what the next session needs, facts labelled with their source and verification state, what changed in the vault this session, and a narrative left blank for the person who did the work — a handoff that reads finished when it is not gives false confidence instead of none. |
| `gt-open` | Loads a project at session start, reading `source.md` first so you know which host serves which role before touching code. Stops at the memory *index* rather than the notes, so a 70-note project costs ~80 lines to open instead of ~2,000. Names that project's handoffs still waiting, without reading them. After a week away it leads with a generated catch-up paragraph — what was committed, the newest finding, what waits on you. `gt-open handoff` / `gt-open task <id>` open just one of those, read-only. |
| `gt-work` | Writes the session back: dated findings to `research.md`, stable choices to numbered ADRs in `decisions.md`, architecture rewritten in place in `design.md`, session state to `memory/`. The step people skip, and skipping it is what makes a vault decay. Then it checks its own work: memory notes that contradict each other, findings that look ready to promote, a refreshed research digest, and patterns worth keeping. What did not reach a file it offers to put in a handoff. |
| `gt-promote` | Graduates a fact up a level once a second project proves it general — or *out* to a project's `CLAUDE.md`, where any agent working in that repo reads it with no vault and no setup. Reach picks the level; audience decides whether it also leaves. |
| `gt-validate` | Re-derives a claim with a validator given only the claim, the rules and the artifact — never the reasoning that produced it. Four classes: `empirical`, `vantage`, `rule-compliance`, `code`. Returns confirmed, refuted, or cannot-verify, and the third never counts as a pass. |
| `gt-query` | Answers "how does this work?" — reads `index.md`, follows wikilinks into `Knowledge/`, then falls back to grep and project memory. Flags anything returned that is marked `status: stale`. Also answers "how did we decide X?" (`--lineage`, the chain of ADRs that replaced each other) and "what do we know about X?" (`--entity`). |
| `gt-ingest` | Imports an existing project's notes. Copies, never moves or deletes. Stores external sources immutably in `Sources/` before synthesising them, so the raw input survives whatever you later conclude from it. |
| `gt-review` | Empties the inbox: routes each captured-but-unfiled line (INBOX.md, plus daily notes if you keep them) into a tracked project. |
| `gt-sync` | Keeps the vault in step across machines: shows how far ahead or behind its remote it is, pulls only by fast-forward — stopping, never merging, when both sides have moved — and pushes only after the push check passes. |
| `gt-refresh` | Checks `Sources/` for upstream changes. Supersedes with a *new* immutable file carrying `supersedes:` rather than editing the old one, so the record of what you believed and when stays intact. |
| `gt-upgrade` | Updates the VAULT after `install.sh` updates the plugin — the step that did not exist until a 0.11.0 migration had to be run by hand across 43 projects. Migrations detect their own work, documents are three-way merged against a base the vault carries, and a step that needs a person is reported rather than guessed at. |
| `gt-doctor` | Answers "is this install healthy?" in one command — version, component drift, hook wiring, the vault's release stamp and pending migrations, the scheduled jobs' last exits, stray workers, unpushed commits, publish drift, lint. Every answer is stated relative to the release it was checked against, because a clean report from a check pinned to the wrong version reads exactly like a healthy install. |
| `gt-lint` | Runs 25 deterministic health checks — broken links, orphans, index gaps, scope leaks, staleness, superseded sources — plus `core-unenforced`, which catches a rule that is stored but never re-asserted, and four that file questions for you: an ADR whose expiry may have come, a page covering two topics, a decision stated in prose with no ADR, a memory note missing its entity tags — and `release-pipeline`, a project that ships code with no release pipeline declared. |
| `gt-optimize` | Finds what you pay for on every turn and get nothing back for — a fact duplicated across memory files, a dead index row, a `global-memory/` file over budget, a Knowledge page nobody reads — and, since 0.18.1, what your sessions spend rebuilding the prompt cache after sitting idle past its lifetime. Reporting never writes; moving a note somewhere cheaper or archiving old research happens only when asked, and deletes nothing. |
| `gt-scan` | Checks code against the language definitions this machine actually has — naming and encoding, per language, entirely from packs: a contributed language pack teaches it a new language with no code change. It reports how many checks RAN next to what they found, so a scan that could not load its definitions can never be mistaken for a clean tree. An interrupted scan resumes where it stopped, from any later session. |
| `gt-allin` | One command for every check, built so a skipped check can never pass for a clean one: the headline is "N of M members ran", and a member that could not execute outranks a member that found something. Since 0.17.2 it also runs the credential scan, the runbook lint and — while gt-wiki is installed — the wiki lint. It does not push — an aggregator is where a partial run is easiest to mistake for a complete one, and pushing there would break the very rule about seeing tests pass that the tool exists to serve. Since 0.18.1 it also runs a project's release pipeline up to the owner gate, where one is adopted. |
| `gt-context` | Renders what this vault's definitions SAY, for a session to read — the first consumer of the registry's model-reachable tier. Wrapped in an envelope that marks it as data rather than instruction, because a renderer can make content identifiable but cannot make it true. |
| `gt-validation` | Writes down what a validation established about a file, including what it could NOT determine, stamped with the file's content hash. Edit the file and the recorded definition goes visibly stale — because every serious defect this project has shipped was a claim that outlived its implementation. |
| `gt-allin-commit` | The separate, deliberate act of committing — kept apart from the sweep so a routine check is never also a write. It verifies a passing test receipt covers every staged file, refuses when a check could not run at all, and stops at the commit: a commit is reversible here, a push is fetched by other people. |
| `gt-list` | Shows what is waiting without opening anything — handoffs (open, or whose deferral date has come) and open tasks by project, priority, mine, overdue, stale, deferred or ref. Being told must cost no project context. Read-only. |
| `gt-handle` | Clears what is waiting one item at a time. A task is done, dropped with a reason, deferred to a date (hidden from ranking and escalation until then), moved to another project, or kept; a handoff's items are settled, then it is closed or deferred **to a date**. Nothing is deleted, which is what makes clearing safe. |
| `gt-close` | Closes a project, a task or a handoff. A project is walked to a close: every open item decided, what it learned offered for promotion, then archived in place so every link still resolves (`--move` to relocate it). |
| `gt-plan` | Writes a phased, test-first plan for a piece of coding work — after restating the requirement and reading the design it touches — and stops at "Approve this plan?". No code until you say yes. |
| `gt-implement` | Carries out the approved plan one phase at a time: the failing test first, then the code, then the checks, naming the tests that ran. Any red stops it; it commits only on your yes and never pushes. |
| `gt-brief` | Drafts the section a project's code repository should carry in its `CLAUDE.md` — what it is, the standing decisions, where it runs, what not to do — for a reader with no vault. Printed for review, never written on its own. |
| `gt-minimize` | Prunes a heavy session before you cut it: measures it and how long its prompt cache stays warm, keeps the few things worth keeping, and tells you to `/compact` or `/clear` while that is still cheap. |
| `gt-task`, `gt-handoff`, `gt-task-list`, `gt-handoff-list`, `gt-task-handle`, `gt-handoff-handle` | The pre-0.18.1 names, kept working through 0.18.x as deprecated aliases: each says which verb replaced it, then does exactly what that verb does. |
| `gt-settings` | Shows and changes everything the plugin does on its own — component drift checking, the version check, orphaned-worker detection, the unpushed-commit check, the session-start MUST DO and handoff surfacing (and where a waiting handoff is shown: every session, only in its project, or only on request), the pre-commit test gate, the parallel-work budget, the protected-path prompt, and the settings each installed module adds (the report card, the close-out question, the upstream watch). Every automatic behaviour is registered here and every one can be switched off. |
| `gt-route` | Mid-session: names what the session has actually become, says where its output belongs, and checks you are in the right project. For when a session has drifted from what it opened with, or you cannot name what you are doing. |
| `gt-runbook-lint` | Finds procedures duplicated across project runbooks and routes them to the right shared layer: `PROTOCOL.md`, a `Knowledge/` page, or a repo `CLAUDE.md`. Duplication across two runbooks is the signal a fact belongs one layer out. |

Module skills (each present only while its module is on):

| Skill | Module | What it does |
|---|---|---|
| `/gt-watch:gt-watch` | `watch` | Watches any git repo and opens your next session with a P0 when it ships something you need to know about — a security fix, a breaking change. Was `/gt:gt-watch` before 0.15.0. |
| `/gt-farm:gt-farm` | `farm` | Hands bulk or mechanical work to an external AI service as a self-contained packet with a strict return contract. The packet is identical whether you paste it into a web UI or send it to an API. Was `/gt:gt-farm`; off for a fresh install. |
| `/gt-flow:gt-flow` | `flow` | Draws how knowledge moved through the vault — one lane per project, an arrow each time an item climbed a level — as one offline HTML file. `--redact` before sharing. |
| `/gt-visualize:gt-visualize` | `visualize` | Explains a codebase in 3D — a scroll-driven walkthrough whose scenes highlight parts and animate the flows between them, written from the code and the vault — or draws it as an interactive code city — directories as districts, files as buildings; height by lines, colour by language or git churn — as one offline HTML file with three.js inlined. `--redact` before sharing. |
| `/gt-lotr:gt-lotr` | `lotr` | LOTR, also called gt MCP: one gateway to rule them all. Four fixed MCP tools (`find`, `call_read`, `call_write`, `call_consent`) in front of any number of connections, with a registry of who talks to whom, as whom, from which machine. Credentials are references, never values. Off by default: `install.sh --with lotr`. |
| `/gt-demo:gt-demo` | `demo` | A guided tour of the whole system against a throwaway vault, so nothing you try touches your own. |
| `/gt-wiki:gt-wiki` … | `wiki` | Five skills for a standalone LLM wiki: query, ingest, init, lint, refresh. |

The report card (`report-card` module) has no command: it runs at `/compact` and session
end, and is switched with `/gt:gt-settings`.

## The commands

The skills are how you talk to Golden Thread in a session. These are the tools it
installs **into your vault**, at `Projects/golden-thread/tools/`, which you or a skill
run directly. They exist because some things must not depend on a model remembering to
do them.

| Command | What it does |
|---|---|
| `gt_log.py add "<line>"` | Records a log entry. Writes only **your session's** spool file, so two sessions can never overwrite one another. `log.md` is rendered from those spools, never written directly. |
| `gt_adr.py allocate <project>` | Reserves the next ADR number and prints it. The number is taken with a single atomic operation, so two sessions cannot both take ADR-6 — which has happened. Write the decision into the file it names. Since 0.18.1 `--supersedes N`, `--expires-when` and `--expires` record what it replaced and when it stops being true, `gt_adr.py lineage <project> "<topic>"` prints a topic's chain of decisions, and a sub-project's bare slug resolves to its own folder. |
| `gt_log.py merge` · `gt_adr.py merge <project>` | Regenerate `log.md` / `decisions.md` from the spools. Idempotent: running twice changes nothing. |
| `gt_log.py add "<line>" --event <kind> --item <path>` | The same log line, plus one structured event recording what moved where. `gt-promote` and `gt-refresh` record their moves this way; `gt-review`, `gt-work` and `gt-ingest` call `gt_events.py emit`. |
| `gt_events.py` | The structured event stream (`events.jsonl`, schema v1) that `/gt-flow:gt-flow` draws. `backfill --dry-run` previews history rebuilt from git and `log.md` for a vault that predates the events. |
| `gt_task.py add` · `list` · `done` · `drop` · `defer` · `shelve` · `move` · `count` | Creates and works tasks through a tool instead of by hand (0.17.2; `shelve` and `move` 0.18.1). One store — the README's `## Tasks` — parsed with `gt_tasks.py`'s own rules. IDs are `slug:LINE:HASH` and refused if the line changed; `drop` needs a reason, `defer` a future date; `move` writes the other project first, so an interruption leaves a duplicate, never a lost task; nothing is deleted. Refuses a README another live session has claimed. |
| `gt_tasks.py` | Regenerates `TASKS.md`, the cross-project task rollup, ranked and computed against the clock rather than stored. |
| `gt_session.py` | Registers a session, claims the files it is about to write, and reports which other sessions are live. Liveness is checked against the OS, not guessed from a timestamp — and since 0.17.2 "this machine" is a machine id created once in `~/.claude/golden-thread/machine-id`, not a hostname a network change can alter. |
| `gt_closeout.py` | Names projects whose signals say they may be finished, with the reasons. Closure is asked for, never assumed. |
| `gt_edits.py` | Per-edit attribution, so a line in a shared file can be traced to the session that wrote it. |

`log.md`, `decisions.md` and `TASKS.md` are **generated**. Hand-editing them is not a
style violation — your change is simply lost at the next merge, and `gt-lint` reports it.

### The checks, and when each one runs

These live with the plugin rather than in your vault, at
`~/.claude/plugins/cache/golden-thread-plugin/gt/<version>/scripts/`, because a git hook
and a launchd job have to find them without a vault being open. The git hook resolves the
newest installed release by glob, so an upgrade does not rewrite it.

**Three cadences, narrow on purpose.** The same scanners run at three different widths,
and the width is what makes each one usable:

| Cadence | What it looks at | What it does |
|---|---|---|
| `tests/run.sh` | the file set the scanners define | fails the run |
| the commit gate (`.githooks/pre-commit`) | the staged diff only | denies the commit |
| `gt_sweep.py --vault V [--path P] [--only secrets,code]` | the whole tree, weekly | **reports, never blocks** |

A commit gate that scanned the whole tree would punish you for someone else's old code,
so it reads only what you are adding — but then nothing ever re-examines what is already
there. The sweep is the answer to *what is true of the tree as a whole*, and because it
surfaces old debt it must never block: a report you read on a Monday, not a wall in front
of a commit. It exits non-zero only when a member could not run. Run it from launchd
(`gt_schedule.py install sweep`, which currently refuses — see the scheduler below); harmless by hand.

**The two checks themselves:**

| Command | What it does |
|---|---|
| `gt_secrets.py <path> [--staged] [--json] [--exclude GLOB] [--baseline F] [--write-baseline F]` | Finds credentials that are in the wrong place, and never becomes the thing that leaks them. **Nothing in the process ever prints matched source text, and no other check runs beside it** — a finding is `path:line`, a rule id and a *length*. Never a prefix, never a redaction, never a hash: a hash of a short secret is crackable and still confirms a guess. That output contract is why it is a separate tool and not a `gt-scan` member; the 0.16.0 attempt leaked precisely because a `naming` check printed raw source in the same run. `--staged` reads staged blobs, not the worktree, because those are what the commit will contain. Exit 0 clean / 1 found / 2 could not run — and for credentials, *could not run* blocks. |
| `gt_scan_code.py <path> [--staged] [--sarif FILE] [--json] [--baseline F] [--write-baseline F] [--rules]` | Source validation: checks code against the `lint` rules in effect here. A leaf of `gt-scan` alongside `gt_scan_language.py` — `gt_scan.py --list` shows both members. Rules are **data**, from `lint` packs, in a documented subset of ast-grep's rule schema; gt evaluates that data and never executes anything a pack supplies. Four evaluator tiers: `text` and `stdlib` (Python's `ast`) are always present, `astgrep` and `treesitter` only if the optional dependency imports. **A rule whose tier is absent is reported SKIPPED, never silently passed** — exit 3 means nothing was found *but* the scan does not cover what it was asked to cover. Unlike `gt_secrets` it *does* print source text, because here the text is the finding — which is exactly why the two may never share a process. |

Accepted findings belong in a baseline (`--write-baseline .gt/secrets-baseline.json`,
`.gt/code-baseline.json`), which both the gate and the sweep pick up automatically, so
what you have examined goes quiet while anything new still fires. The commit gate has two
loud escapes — `git commit --no-verify` once, or `git config gt.secretsgate off` for the
clone, which prints that it is off on every commit.

**The record:**

| Command | What it does |
|---|---|
| `gt_check_report.py record --vault V --check <name> --verdict clean\|findings\|cannot-run [--count N] [--scope "…"] [--ref R] [--detail T]` · `show --vault V [--check C] [--json]` | Files a check's result into the vault, so a gate leaves a record. It stores a verdict, a count, a scope and a ref — **never a finding's content**, because a vault file is committed and pushed, and a report naming `path:line` for a credential would republish the location of every secret the scanner found. It answers the three questions an exit code cannot: is this check running at all, is it getting noisier, did anyone ever look. `cannot-run` is a first-class verdict, never laundered into `clean`. Recording is bookkeeping, so `record` always exits 0 and never commits. |

**Code review — a framework that ships no opinions:**

| Command | What it does |
|---|---|
| `gt_code_review.py dimensions` · `plan <path> [--staged]` · `validate <findings.json> --root P` · `report <findings.json> --root P --vault V [--ledger F]` | The deterministic half of code review. gt does not perform the review — *"does this abstraction earn its keep"* has no mechanical oracle — it does everything around the judgement: which dimensions over which files, then mechanical rejection of a finding citing a file that does not exist, a line past the end of one, an unknown dimension, an invalid severity or `confirmed: false`, then a record only survivors reach, with `--ledger` so a previously declined finding never returns as new. |

**gt ships ZERO review dimensions, deliberately.** The `review` slot is empty by design,
not by omission: the opinions are yours or your company's, supplied as a
`review.*.pack.json` in your own vault, where they arrive through the same registry as
everything else and get precedence, shadowed reporting, retraction and provenance for
free. **Zero dimensions configured exits 3 and says how to add one — it is never reported
as a clean review**, because "nothing was reviewed" and "reviewed, nothing found" are
different facts and only one of them is reassuring.

**The scheduler:**

| Command | What it does |
|---|---|
| `gt_schedule.py list` · `install <job> --vault V [--repo PATH …] [--hour H] [--minute M]` · `check <job>` · `remove <job>` | Installs, verifies and removes gt's launchd jobs — `daily`, `lint-weekly` and `sweep` (0.17.2; its pre-flight currently refuses, because the registry cannot find its packs from the hooks dir). It exists because the weekly lint agent was installed by hand in 2026-09-08, with no `--check` and no rollback. **Validation goes through launchd, not through a terminal run:** `install` does not stop at writing a plist — it bootstraps the job, kickstarts it, waits, then reads launchd's own last exit code and the job's output, because a job that works in a terminal can still fail under launchd. `remove` boots it out and deletes the plist, which is what makes `install` safe to re-run. |

**Session capture:**

| Command | What it does |
|---|---|
| `gt_daily.py --vault V [--date YYYY-MM-DD] [--repo PATH …] [--dry-run] [--check]` | Writes the day's **facts** into `Daily Notes/<date>.md`: tasks closed, commits per repo, event counts, wiki item counts, an active span per project. Terse by design — it does not explain, summarise or interpret, because a generated block that editorialised would encode a reading of the day that is not yours. Each fact goes under its heading in a marked block, replaced whole each run (new projects, tasks closed and commits under `## Did`; ADRs under `## Decided`; tasks added and due under `## Open at end of day`), with the counts in a footer block; your own lines stay above the blocks, and it never touches `## Noticed`, which is your unfiled capture surface and the section `/gt:gt-review` sweeps. Git is the primary source and events are enrichment: on 2026-09-27 the event log held 5 events on a day with 10 commits across three work streams. |
| `gt_state.py check [--margin N] [--write]` · `write [--reason R]` · `show` · `hook` | Writes the session's state **before** the context runs out, not as it does. **The signal is `ctx_pct` — context fill — and never `rate_limits`**: a real reading was `{"five_hour": 5, "seven_day": 10, "ctx_pct": 91}`, so triggering on the allowance meter would have fired at entirely the wrong moment and looked correct doing it. It fires once per crossing, at a margin (default 5) below the `usage_alert` flag point. PreCompact is the **backstop, not the primary** — it fires when compaction is already starting, so the write competes with the thing it exists to survive — and the write says which one it was. Never fatal, never blocking; a missing usage ledger reads as *cannot tell*, never as *plenty of room*. |
| `gt_surface.py check [--vault V] [--dry-run] [--json]` · `must-do [--vault V]` · `handoffs --project SLUG [--vault V]` | The **reader** for everything above, run at `SessionStart` (0.17.2). Every session: a **MUST DO** block from `<vault>/deadlines.md` — one table, `\| item \| category \| due \| see \|` — overdue 🔴 or due within 14 days 🟡, counted down from the row's date at run time, never stored. Every session until it is handled or deferred to a date: each waiting handoff, one line — path, project, age, open items — never its body (setting `handoff_surface`: `any` by default, `project` for only when `/gt:gt-open` opens that project, `manual` for only on request). Once each: a state file `gt_state.py` wrote; after a compaction the newest goes back to the model in full. **Surfacing is the alarm; the task is the record** — it never creates or closes a task and never writes the vault, only a machine-local "already shown" ledger. A clean start says "nothing waiting". Setting `surface`. |
| `gt_handoff_status.py list --vault V [--project SLUG] [--all] [--json]` · `mark FILE --vault V --status handled\|open\|deferred [--until YYYY-MM-DD] [--reason TEXT] [--dry-run]` | Whether a handoff is still waiting on someone (0.17.2). `open` keeps surfacing; `deferred` must carry a future `--until` date and is open again on that date ("later, some time" is `handled` with a reason); `handled` is marked by a person **or** follows when every task citing the handoff's filename is checked off; `history` is a pre-0.17.2 handoff with no status, over a week old, with no open citing task — so an upgrade does not resurface every old one. `mark` appends to the handoff's status log. It never loads a handoff's body. |

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
| Writing code | `/gt:gt-plan`, approve, then `/gt:gt-implement` | No code until the plan is approved; any red stops the run |
| Stepping away from a long session | `/gt:gt-minimize` | Cut while the cache is warm — after it expires the whole session is rebuilt first |
| A project is finished | `/gt:gt-close project <slug>` | Every open task and handoff must be decided before it archives |
| Keeping the vault honest | `/gt:gt-lint` | `memory-unlisted` matters most — a note missing from the index is one no session will ever load |

Full walkthroughs, one section per skill, are in
[`golden-thread-plugin/MANUAL.md`](golden-thread-plugin/MANUAL.md).

## How knowledge moves

Six levels, and one direction that is not upward:

1. session → 2. `memory/` → 3. `research`/`decisions`/`design` → 4. `Knowledge/` →
5. `global-memory/` → 6. `core-rules/`

Levels 1–5 hold **facts** and are read on demand. Level 6 holds **rules** and is pushed
into every turn by a hook.

**The other direction is about cost, not maturity.** `/gt:gt-promote` moves a fact up by
how *settled* it is; `gt_demote.py --vault V --file <path> [--to knowledge|project-memory]
[--project SLUG] [--apply]` moves it down by how often it is *paid for* —
`global-memory/` is read in every session of every project, `Projects/<slug>/memory/` in
every session of one, a `Knowledge/` page only when someone asks. A fact in global-memory
that only one project needs is charged to every session for ever; moving it does not make
it less true, it makes it cost what it is worth. Without `--apply` it is a dry run. The
order is write, verify by re-reading from disk, and only then remove the source, leaving a
pointer behind — so a failure leaves **duplication**, which a reader can see and resolve,
rather than deletion, which no diff brings back. That is the lesson of `gt_optimize`'s old
`--apply`, which an independent validation found destroyed notes seven ways and which was
removed rather than repaired. It refuses to move `core-rules/`, `Sources/`, or a file
another live session has claimed — and refuses when it cannot *read* a session file, because
"could not check" is not "clear".

Separately, a second question — *would this make sense to someone who has never seen
this vault?* — sends a fact **out**, into that project's `CLAUDE.md`, committed to its
repo where any agent working in that code picks it up with no setup. Reach picks the
level; audience decides whether it should also leave.

### Seeing how knowledge moved

Every move up (or across, or out of) the ladder is recorded as a **movement event** in
`Projects/golden-thread/events.jsonl` as it happens: a project created, a note captured
or filed, a promotion, an ADR, a source ingested or superseded, a task opened or done.
The skills record their own moves, so there is nothing to remember.

The `flow` module (on by default) draws that stream as **one self-contained HTML file**
that opens offline. Each project is a lane, time runs left to right, and height within a
lane is the ladder level. A dot is something arriving, a triangle something moving, a
square something leaving. **An arrow joins the level an item left to the level it
reached**, so a promotion is an arrow climbing.

![Flow view of three fictional PizzaBot projects: a finding captured in memory climbs to research, then to a Knowledge page once a second project hits it, then to global-memory and a Core rule](golden-thread-plugin/docs/flow-example.png)

*Three fictional demo projects over three weeks. In `demo-pizzabot`, a finding goes from
`memory/` (2) to `research.md` (3) to a Knowledge page (4) once `demo-delivery-drones`
hits the same problem, and later becomes a Core rule. The red line is a source being
superseded. Open [the interactive page](golden-thread-plugin/docs/flow-example.html)
(download it; GitHub shows the source) to filter by project or kind and click any mark
for its details. It was rendered from
[`flow-example-events.jsonl`](golden-thread-plugin/docs/flow-example-events.jsonl).*

In a session, ask for it — *"show how knowledge moved"* — or run `/gt-flow:gt-flow`.
Directly:

```bash
# The newest installed gt-flow, rather than a version number that goes stale in this README.
FLOW=$(ls -d ~/.claude/plugins/cache/golden-thread-plugin/gt-flow/*/scripts | sort -V | tail -1)
python3 $FLOW/gt_flow.py render --vault <vault>                      # whole vault
python3 $FLOW/gt_flow.py render --vault <vault> --project <slug> --since 2026-09-01
python3 $FLOW/gt_flow.py render --vault <vault> --redact             # before sharing
```

- **A vault older than 0.15.0 has no events yet.** `gt_events.py backfill --dry-run`
  shows the history it would recover from your git log and `log.md`; run it without
  `--dry-run` to add it. A second run adds nothing.
- **`--redact` before the picture leaves your screen.** Every project, path, task id and
  session becomes a salted hash and notes are dropped; levels, kinds and counts stay. An
  unredacted page says so in an orange badge at the top.
- Task events are hidden at first (on a real vault they are most of the stream); they
  are one click away in the Kinds filter, or shown from the start with `--tasks`.
- It only reads the vault. The file lands in the current directory, or `--out`; an
  `--out` inside the vault is refused.

Full reference, including exit codes, in the
[Manual](golden-thread-plugin/MANUAL.md#gt-flowgt-flow).

## Maintenance is code, not judgement

The dangerous failure is not the fact you never captured — it is the fact you captured,
kept, and still serve after reality moved on. `gt_lint.py` runs 25 deterministic checks
(broken links, orphans, index gaps, scope leaks, 90-day staleness, superseded sources),
`core-unenforced`, which catches a rule that is **stored but never re-asserted** — the
exact failure this system exists to close — and `adr-collision`, which catches two
decisions that ended up sharing one number.

`skill_lint.py` enforces that no two skills can fire on the same intent — a rule most
systems state and check by hand. Adopting it found a live collision: two skills sharing
three verbatim trigger phrases.

## Contributing a pack

gt runs **no third-party code**. Contributions are **submitted, reviewed and merged** into gt
itself, after which they are first-party and held to the release gate. New in **0.16.0**:

```bash
python3 dev/submissions.py slots                 # which slots are open, and their tier
python3 dev/submissions.py validate my.pack.json # check before you send
python3 <gt>/scripts/gt_registry.py show naming --lang python   # what is in effect, and from where
```

A pack is one JSON file of data, not a program, so a reviewer can read all of it. Its tier is
derived from whether its slot can reach model context — never from what the pack declares. Full
spec: [SUBMISSIONS.md](SUBMISSIONS.md).

## Contributing to this repo

Read [`CLAUDE.md`](CLAUDE.md) first — it carries the landmines, including the one that
has been re-broken by documentation four times: **hooks must never be referenced from
inside the vault.**

Before pushing, prove the repo still stands on its own:

```bash
bash golden-thread-plugin/selftest.sh
```

It installs into a throwaway home, scaffolds a vault and a project the way `/gt:gt-init`
does, and asserts that every file the manual tells a new user to run exists, that the
hooks answer, and that the fresh vault lints clean. Nothing on the machine is touched.
The vault this system was built in is a separate, private repo; nothing here depends
on it.

### If you change behaviour, change the documentation with it

The docs are **five markdown files plus generated HTML and PDFs**, and they drift apart
silently because nothing forces them to move together. The full list, and the order the
artefacts must be regenerated in, is in
[`golden-thread-plugin/CLAUDE.md`](golden-thread-plugin/CLAUDE.md) under *Changing
documentation*. **This README is the front door**: its version callout describes the current
release, and it is the first thing to go stale when one ships.

The release gate checks that every skill is *named* in three files, that no PDF is older than
its source, and — since 2026-09-16 — that every count quoted in the docs matches one derived
from the source (`dev/check_doc_counts.py`: skills, Core rules, lint checks, module versions).
It still cannot check whether what a document **says** is still true — and a doc
that is accurate about a *previous* release is the failure that actually happens. A single pass
on 2026-09-16 found four at once, including a contributor guide whose worked example used a
slot no tool reads: anyone following it exactly would have produced a pack that validates,
merges, resolves, and then does nothing.

So when you change something, re-read the surrounding paragraph rather than the line you came
for. Better still, make the claim check itself — `gt_registry.CONSUMERS` records which tool
reads each slot and a test asserts each one really does, which is why that particular claim
cannot quietly stop being true.

## Acknowledgments

Golden Thread builds on **Jonathan Tucci**'s llm-wiki and project-flow Claude Code
plugins. The core wiki pattern — immutable `Sources/`, synthesized `Knowledge/`,
wikilink-hop navigation — originates with Jonathan's design. Golden Thread extends it
with a hook architecture, Core-rule enforcement, a session report card, a separate
`gt-wiki` plugin, and a deeper project-memory hierarchy. Jonathan also identified key
drawbacks in Golden Thread's own original design — particularly around lazy loading and
the promotion discipline — and his insights directly informed the fixes that made it
practical to use at scale.

Two scripts in `golden-thread-wiki` come directly from Jonathan's work:

- **`scripts/wiki_log.py`** — deterministic writes to `log.md` and `index.md`, with a
  closed vocabulary of operations (ingest, query, lint, refresh, graduate, retire,
  relocate).
- **`scripts/wiki_refresh.py`** — git-native change detection for local sources:
  `git fetch` + `git diff base..head`, with `upstream_sha:` frontmatter for a precise
  diff baseline; web-only sources are flagged for the LLM to compare.

**Sergey Kryvets** recommended adding a visualization tool to Golden Thread — a great idea that became **gt-visualize**: the 3D code city, the scroll-driven explainers that show how a system's parts work together, publishing, and the guided walkthrough. Thank you, Sergey.

## License

[MIT](LICENSE).
