#!/usr/bin/env python3
"""Golden Thread settings: one registry, one file, full user control.

Every OPTIONAL behaviour this system performs on its own -- checking component drift
at session start, printing a report card at compact, and whatever is added later --
must be something the user can see and switch off. A system that acts automatically
and cannot be inspected or disabled is not trustworthy, however good its intentions,
and the whole point of Golden Thread is that a mechanism which cannot be verified is
not enforcement.

Settings live in `~/.claude/vault-config.json` beside `vault_path`. That file
already exists on every install and is already read by the hooks, so no new
location is introduced.

## What this registry does NOT cover, and why that is not an oversight

Four hooks run unconditionally and have no entry here. They are not missing; they are
the mechanism that carries and backstops the Core rules themselves:

    inject_core_rules.sh     UserPromptSubmit + SessionStart/compact -- injects the
                             rules. Off, there is no Core tier at all.
    validate_response.sh     Stop -- the Validated backstop for
                             core_timestamp_every_message and
                             core_no_secrets_in_transcript.
    guard_session_claims.sh  PreToolUse -- core_concurrent_session_claim.
    guard_vault_writes.sh    PreToolUse -- core_explicit_vault_target.

A switch here would make them the one thing they must never be: enforcement that the
session being enforced can turn off. That is the same judgement recorded for modules
in gt_components.CORE_ENFORCEMENT_HOOKS (owner decision, 2026-09-14) -- a mechanism a
user can switch off must not be able to take a Core rule's mechanism with it.

This is a claim about SWITCHING, not about visibility. install.sh names every hook it
wires, `gt_doctor.py` reports whether each is registered and current, and the vault's
CLAUDE.md tells the assistant to say the state out loud each session. Removing one is
a deliberate act -- uninstall, or delete its registration from ~/.claude/settings.json
-- and it stays deliberate rather than becoming a config key.

Two guards ARE registered, and the line is not "core versus not". `test_gate` governs
core_test_before_commit because a gate that fires where it cannot be satisfied is a
gate people switch off for everything, so it is better switched off knowingly, per
repo, than bypassed wholesale; `protected_paths` governs guard_protected_paths.sh,
which is tied to no Core rule. (0.18.0 adds a third: `foreign_checkout_guard` governs
guard_foreign_checkout.sh, also tied to no Core rule.)

## Adding a setting

Append one entry to SETTINGS. Nothing else needs to change: `/gt:gt-settings`
renders whatever is registered, validates against `values`, and the reader
helper below gives any script the value with its default applied. An optional
automatic behaviour that is not in this registry is not a setting -- it is an
undocumented behaviour, and that is the thing this file exists to prevent.
"""
import json
import os
import sys

CONFIG = os.path.expanduser("~/.claude/vault-config.json")

SETTINGS = {
    "component_updates": {
        "default": "report",
        "values": ["off", "report", "confirm", "auto"],
        "summary": "What to do when INSTALLED hooks/scripts differ from what is checked in.",
        "detail": (
            "off     nothing is checked\n"
            "report  print the drift, change nothing  (default)\n"
            "confirm print the drift and the exact command to apply it\n"
            "auto    apply stale/missing silently\n"
            "\n"
            "`auto` never overwrites a file where the INSTALLED copy is newer than the\n"
            "source, and never deletes one absent from the source. On 2026-08-29 the real\n"
            "drift ran that direction: a naive updater would have reverted the timestamp\n"
            "validator and deleted the claim guard. Note these files EXECUTE on every\n"
            "prompt and the plugin source sits in a cloud-synced folder, so `auto` means a\n"
            "sync from another machine can change what runs here."),
    },
    "version_check": {
        "default": "report",
        "values": ["off", "report"],
        "summary": "Check at session start whether a newer plugin version is checked in.",
        "detail": (
            "off     no checking\n"
            "report  name the newer version and how to install it  (default)\n"
            "\n"
            "`component_updates` asks whether the installed FILES match a given version.\n"
            "This asks whether that version is still the newest one available -- the axis\n"
            "the component check is blind along. On 2026-08-30 gt 0.6.0 was found\n"
            "installed with 0.9.4 checked in beside it since the day before, four hooks\n"
            "registered in settings.json pointing at files that had never been copied. A\n"
            "component check aimed at 0.6.0 reported clean the whole time.\n"
            "\n"
            "There is deliberately no `auto`. Installing a version rewrites hook\n"
            "registrations and prunes caches; doing that mid-session leaves the running\n"
            "session executing hooks that no longer match the ones on disk. An upgrade is\n"
            "a decision with a restart attached."),
    },
    "orphan_check": {
        "default": "report",
        "values": ["off", "report", "reap"],
        "summary": "Look for abandoned Claude WORKERS (background shells) at session start.",
        "detail": (
            "off     no checking\n"
            "report  list stalled workers and how to reap them  (default)\n"
            "reap    terminate stalled workers automatically\n"
            "\n"
            "OWNERSHIP is the first test (0.15.0). A shell with a live `claude` process\n"
            "above it belongs to that session: it is listed as information -- session id,\n"
            "claude pid, uptime, and `WAITING on: <what it polls>` for a poll loop -- and\n"
            "is never reaped, whatever its CPU says and even under `reap`. ORPHAN means no\n"
            "`claude` remains above it. THIS session's own workers are the exception to the\n"
            "quiet half: undeclared or idle ones are still raised, because they are yours to\n"
            "answer for.\n"
            "\n"
            "Only then does CPU decide, and for an orphan it decides alone: consumed across\n"
            "the whole process tree, not age and not whether someone wrote a note about it.\n"
            "On 2026-08-29 ten orphans were found alive across three sessions, the oldest at\n"
            "10 days 23 hours, every one having burned under 0.05 seconds of CPU. All ten\n"
            "would have passed a documentation check. A worker younger than five minutes is\n"
            "not judged at all -- it has not had time to accumulate any.\n"
            "\n"
            "A DECLARED worker that is stalled is reported more urgently, not less --\n"
            "someone was told work was happening and it is not. `reap` only ever kills\n"
            "stalled ORPHANS; one consuming CPU is never touched, and neither is one whose\n"
            "session is still alive."),
    },
    "push_check": {
        "default": "report",
        "values": ["off", "report"],
        "summary": "Check at session start whether the vault has commits not yet pushed.",
        "detail": (
            "off     no checking\n"
            "report  name the count, the age of the oldest, and the push command  (default)\n"
            "\n"
            "The vault is a git repo so its truth survives one disk and reaches the other\n"
            "machines. A commit that never leaves is not backed up, is invisible to a\n"
            "session on another host, and looks finished: gt-work reports success and the\n"
            "tree goes clean. On 2026-09-02 the vault was found 16 commits ahead of origin\n"
            "with the oldest dating back weeks. Nothing had failed -- every session had\n"
            "committed correctly, none had pushed, and no check asked.\n"
            "\n"
            "A branch with NO UPSTREAM is reported separately and more loudly. It cannot be\n"
            "ahead, so a naive count returns zero and reads as healthy, when in fact the\n"
            "commits have nowhere to go at all.\n"
            "\n"
            "There is deliberately no `auto`. Pushing is outward-facing: it publishes to a\n"
            "remote others read, it can be rejected, and an unpushed commit is sometimes\n"
            "correct -- work held back on purpose. A push that surprises its author is\n"
            "worse than a delay that annoys them."),
    },
    "surface": {
        "default": "on",
        "values": ["off", "on"],
        "summary": "At session start, show MUST DO deadlines, new handoffs and pre-compaction state.",
        "detail": (
            "off  nothing is surfaced\n"
            "on   every session: overdue and near items from <vault>/deadlines.md, counted\n"
            "     down live; once each: handoffs under a week old and state files gt_state\n"
            "     wrote before a compaction; and a count of open tasks citing a handoff  (default)\n"
            "\n"
            "Until 0.17.2 gt wrote handoffs, state files and handoff tasks and NOTHING at\n"
            "session start read any of them, so each reached the next session only if\n"
            "someone thought to look. Two credential rotations sat 15 days overdue at the\n"
            "top priority that way (2026-09-28): priority is a sort order, not an alarm.\n"
            "This is the alarm; the task stays the record. It never writes to the vault."),
    },
    "daily_comms_content": {
        "default": "follow",
        "values": ["follow", "off"],
        "summary": "Whether THIS machine may put email and Teams information into the daily note: follow the vault's policy, or force it off here.",
        "detail": (
            "follow  use the vault-wide policy  (default)\n"
            "off     never on this machine, whatever the vault says -- e.g. a work machine\n"
            "\n"
            "The policy itself lives in the shared vault and is OFF by default:\n"
            "  gt_daily.py --vault <vault> --comms-content on|off\n"
            "A machine can only tighten it, never loosen it: the vault is shared, so mail or\n"
            "chat content written on one machine is readable from every other -- including a\n"
            "work machine whose employer does not want such content anywhere Claude can read\n"
            "(owner, 2026-09-30). While off, gt_daily makes no room for a --comms section,\n"
            "offers it in no handoff, and if one still holds content a NOTE says so -- it never\n"
            "deletes another tool's writing."),
    },
    "release_announce": {
        "default": "off",
        "values": ["off", "draft", "post"],
        "summary": "What dev/publish.sh does about the release's Discussions announcement: warn, write a draft, or post it.",
        "detail": (
            "off    warn that the release is unannounced, as before  (default)\n"
            "draft  write the announcement to a file and print its path; a person posts it\n"
            "post   create the Discussion in the repo's Announcements category\n"
            "\n"
            "One post covers every release no Discussion names yet, built from the CHANGELOG.\n"
            "It passes the same scrub as a release (employer/host terms, home paths, IP\n"
            "addresses, credential scan) first; a hit posts nothing and leaves the draft.\n"
            "Announcing never blocks a release -- the release is published by then\n"
            "(owner, 2026-09-30)."),
    },
    "agent_specialization": {
        "default": "off",
        "values": ["off", "on"],
        "summary": "Whether ingest and validation skills hand their work to a job-typed specialist agent.",
        "detail": (
            "off  every skill runs inline, as before  (default)\n"
            "on   a skill with a job-type spec (ingest-code, ingest-docs, ingest-tool,\n"
            "     validate) spawns a specialist agent with that spec applied; its full output\n"
            "     lands in the spool and the session sees a summary\n"
            "\n"
            "Independent of skeptic_pass: this does not switch the gt-work skeptic on or off.\n"
            "A job type with no spec runs inline with a notice. Specs are data:\n"
            "gt_agent_spec.py list | validate <spec>."),
    },
    "skeptic_pass": {
        "default": "off",
        "values": ["off", "on"],
        "summary": "Whether /gt:gt-work runs a skeptic agent over the session's findings before they land.",
        "detail": (
            "off  write-back as before  (default)\n"
            "on   a zero-context agent reads the research entries about to be written and\n"
            "     flags unverified or overclaimed figures first; nothing is dropped, the\n"
            "     session decides\n"
            "\n"
            "Independent of agent_specialization (0.18.0): this alone turns the skeptic on,\n"
            "and does not hand ingest or validation to specialist agents."),
    },
    "handoff_surface": {
        "default": "any",
        "values": ["any", "project", "manual"],
        "summary": "Where a handoff that has not been handled is shown.",
        "detail": (
            "any      every session start, whatever project is opened  (default)\n"
            "project  only when /gt:gt-open opens the handoff's own project\n"
            "manual   only when you run /gt:gt-handoff-handle\n"
            "\n"
            "A handoff keeps being shown until it is handled (marked, or every task citing\n"
            "it closed) or deferred to a date. It is shown as one line -- path, project, age,\n"
            "open items -- never its body, so being told costs no project context; handling\n"
            "it is /gt:gt-handoff-handle. `project` needs /gt:gt-open: a session that never\n"
            "opens the project never sees it, which is why `any` is the default (owner,\n"
            "2026-09-28)."),
    },
    "task_surface": {
        "default": "on",
        "values": ["off", "on"],
        "summary": "At session start, one line counting p:: 1 tasks waiting on you.",
        "detail": (
            "off  nothing\n"
            "on   one line: p:: 1 tasks waiting on you, how many overdue, how many open over\n"
            "     a week, and the commands to list and work them  (default)\n"
            "\n"
            "A count, never the tasks: being told must cost no project context. Tasks deferred\n"
            "to a later date (gt_task.py defer) are not counted until that date. Counted by\n"
            "the vault's gt_task.py, which parses with the TASKS.md rollup's own rules."),
    },
    # `watch` and `closeout_check` (and `report_card`, below) moved out in 0.15.0: the watch
    # and report-card modules declare them in module.json and they register from there
    # while the module is installed. Their explanations moved with them (`detail`).
    "test_gate": {
        "default": "auto",
        "values": ["off", "warn", "auto", "block"],
        "summary": "Refuse a `git commit` of code whose tests have not been seen to pass.",
        "detail": (
            "off    commit whatever you like; the Core rule is not injected either\n"
            "warn   allow the commit, but say the tests were not seen to pass\n"
            "auto   block when the repo HAS a discoverable test command, warn when it\n"
            "       does not  (default)\n"
            "block  block regardless -- a repo with no tests cannot commit code\n"
            "\n"
            "Enforces core_test_before_commit through a PreToolUse guard. Evidence is a\n"
            "receipt: tests/run.sh and dev/release-check.sh write one when they pass, any\n"
            "project can write one with `gt_test_receipt.py record --ok`, and editing a file\n"
            "after a run invalidates the receipt for that file automatically.\n"
            "\n"
            "`auto` exists because a gate that fires where it cannot be satisfied is a gate\n"
            "people switch off for everything. A repo with no test entry point is not asked\n"
            "to have one; a repo that has one is held to it.\n"
            "\n"
            "Never blocked: docs-only commits, a repo containing `.gt-no-test-gate`, a\n"
            "command run with GT_TEST_GATE=off, and anything the guard cannot parse.\n"
            "\n"
            "On 2026-09-12 a release was committed and pushed with a stale MANIFEST.json.\n"
            "The gate that catches exactly that existed and had been run before the last few\n"
            "edits rather than after. The discipline was not the problem; it had no\n"
            "mechanism."),
    },
    "parallel_work": {
        "default": "on",
        "values": ["off", "on"],
        "summary": "Run divisible work in parallel instead of one unit at a time.",
        "detail": (
            "off  everything runs serially, and the Core rule asking for parallelism is\n"
            "     NOT injected -- a rule the user has switched off must stop being\n"
            "     asserted, or the setting is decoration\n"
            "on   independent units run concurrently, up to `parallel_max`  (default)\n"
            "\n"
            "This is the switch behind the Core rule core_parallel_when_beneficial, and it\n"
            "reaches two places: the rule injected into every turn, and the default worker\n"
            "count of the test runner (tests/prun.py).\n"
            "\n"
            "The serial default was never a decision. A loop, a sweep over 43 projects and\n"
            "a test suite all run on one core unless someone says otherwise, and nothing\n"
            "reports the waste -- the work completes, correctly, slowly, and its output is\n"
            "identical to the fast version. Measured here on 2026-09-12: the 672-test suite\n"
            "took 509s serial and 108s parallel, 4.7x, with CPU going from roughly one core\n"
            "to 404%.\n"
            "\n"
            "`off` is for when a machine is busy with something else, or when a parallel run\n"
            "is producing a failure a serial run does not -- that difference is a finding\n"
            "worth keeping, which is why the serial path stays supported rather than being\n"
            "removed as dead weight."),
    },
    "parallel_max": {
        "default": "auto",
        "values": None,
        "validate": "auto, or a positive integer number of workers",
        "summary": "Ceiling on concurrent workers. `auto` = as many as the machine allows.",
        "detail": (
            "auto  as many as this MACHINE allows -- the ceilings are measured at install\n"
            "      and again at every upgrade, and stored as `parallel_profile`: cores for\n"
            "      CPU-bound work, 2x cores (bounded by memory) for I/O-bound work, which\n"
            "      is what the tools here mostly do  (default)\n"
            "N     never more than N workers at once, whatever the work\n"
            "\n"
            "`auto` is deliberately not a number YOU have to choose. It resolves through\n"
            "`parallel_profile`, which install.sh measures on this machine and re-measures on\n"
            "every upgrade -- so a new machine or a RAM change is picked up without anyone\n"
            "editing a config, and nothing in the source pretends to know your hardware.\n"
            "Run `gt_settings.py detect-machine --write` to re-measure by hand, or\n"
            "`detect-machine` alone to see what would be measured. Your OWN setting is never\n"
            "overwritten by that: the profile records the hardware, this setting records what\n"
            "you will allow.\n"
            "\n"
            "Set a number when the machine has to stay responsive for something else, or\n"
            "when a remote end is rate-limited. `1` is not the same as parallel_work=off:\n"
            "one worker still runs through the parallel path, so it does not tell you\n"
            "whether the parallel path is what broke a test. Use `off` for that."),
    },
    "brief_absence_days": {
        "default": "7",
        "values": ["off", "3", "7", "14", "30"],
        "summary": "How long away from a project before /gt:gt-open leads with a generated catch-up brief.",
        "detail": (
            "N    when a project has not been opened on this machine for N days, gt-open\n"
            "     starts with one generated paragraph (150 words at most): commits since\n"
            "     the last open, the newest research entry, the oldest open p::1 task and\n"
            "     anything waiting on you. Then it reads the files as usual.  (default 7)\n"
            "off  never on its own; `/gt:gt-open <slug> --brief` still asks for one\n"
            "\n"
            "The brief is assembled from git and structured fields (gt_catchup.py), never a\n"
            "summary of prose, and is labelled as generated. A project with no commits\n"
            "while you were away gets none (0.18.0)."),
    },
    "foreign_checkout_guard": {
        "default": "on",
        "values": ["off", "on"],
        "summary": "Deny git commit/push inside a checkout you declared as another machine's.",
        "detail": (
            "on   a `git commit` or `git push` whose directory is inside a path listed in\n"
            "     `foreign_checkouts` in ~/.claude/vault-config.json is denied, naming the\n"
            "     checkout and the supported route. Read-only git and file writes are\n"
            "     untouched. With nothing declared it denies nothing.  (default)\n"
            "off  no check\n"
            "\n"
            "Ownership is DECLARED, never guessed from a path, remote or credential:\n"
            "  python3 ~/.claude/golden-thread/hooks/guard_foreign_checkout.py add <path> \\\n"
            "      [--label 'the build machine'] [--route 'run copygt.sh there']\n"
            "  ... list | remove <path>\n"
            "One-off exception, visible in the transcript: GT_FOREIGN_CHECKOUT=allow git push\n"
            "\n"
            "Why: 2026-09-11 a session committed a release into another machine's checkout\n"
            "and started rewriting its remote URL to get the push through (0.18.0)."),
    },
    "protected_paths": {
        "default": "ask",
        "values": ["off", "ask"],
        "summary": "Prompt before Write/Edit to core-rules, global-memory, local packs, gt hooks or settings.json; refuse overwriting a Source.",
        "detail": (
            "ask  a Write or Edit to the vault's core-rules/, global-memory/ or\n"
            "     Projects/golden-thread/packs/, to ~/.claude/golden-thread/, or to\n"
            "     ~/.claude/settings.json always shows the permission prompt, whatever the\n"
            "     permission mode; overwriting or editing an EXISTING file under Sources/\n"
            "     is refused -- supersede it with a new file instead. Creating a new Source\n"
            "     is unaffected.  (default)\n"
            "off  no check\n"
            "\n"
            "These paths load into every session of every project (core-rules,\n"
            "global-memory), enforce everything else (the hooks and settings.json), decide\n"
            "what every scan checks (packs/, added 0.16.0 -- local packs outrank both core\n"
            "and contributed definitions and are not checked by anyone else), or are\n"
            "immutable by convention (Sources). Until 0.12.9 nothing but skill prose kept a\n"
            "session from writing them. `ask` does not block legitimate work -- promoting a\n"
            "fact to global-memory still works -- it makes a person see it happen.\n"
            "\n"
            "Covers the Write, Edit, MultiEdit and NotebookEdit tools. It does NOT see a\n"
            "shell command that writes the same file (cp, sed -i, a script); gt's own vault\n"
            "tools are covered by guard_vault_writes instead."),
    },
    "decision_signals": {
        "default": "default",
        "values": None,
        "validate": "default, off, or ;-separated edits: +phrase adds, -phrase removes",
        "summary": "Phrases gt-lint's decision-candidate check looks for in design.md/research.md.",
        "detail": (
            "default  the built-in list: we chose, we decided, this is intentional,\n"
            "         don't change this, do not change this, deliberately, by design,\n"
            "         we use ... instead of, this workaround, existing behavio(u)r is\n"
            "         correct, trade-off we accepted  (default)\n"
            "off      no phrases: the check reports nothing\n"
            "edits    `;`-separated changes to the built-in list -- `+we went with` (or a\n"
            "         bare `we went with`) adds, `-deliberately` removes. e.g.\n"
            "         gt_settings.py set decision_signals \"+we went with;-deliberately\"\n"
            "\n"
            "Matching is case-insensitive and line by line; `...` in a phrase matches up to\n"
            "60 characters of anything. A hit is only a CANDIDATE in the review queue --\n"
            "nothing writes an ADR. Decline one line for good with\n"
            "`suppress: <path>:#<hash>` in lint-declines.md (the queue entry prints it).\n"
            "Added 0.18.0: decisions stated in prose and never recorded are re-debated by\n"
            "the next session, or changed because nothing said they were deliberate."),
    },
    # install_demo was removed in 0.14.0: the demo is a module, and whether it is
    # installed is a module choice (install.sh --with/--without demo, recorded in
    # ~/.claude/golden-thread/install-choices.json). A user's install_demo key in
    # vault-config.json is left alone; a later migration removes it.
    # -- 0.18.0: memory and gt-optimize ------------------------------------------------
    "knowledge_access_log": {
        "default": "on",
        "values": ["off", "on"],
        "summary": "Log each Read of a Knowledge/ page to <vault>/usage/knowledge.jsonl (local, never committed).",
        "detail": (
            "on   the log_knowledge_read hook (PostToolUse on Read) appends one line per\n"
            "     Knowledge page read: page, short session id, date  (default)\n"
            "off  nothing is logged; gt-optimize's knowledge-unused finding then has no new\n"
            "     evidence and reports from whatever the log already holds\n"
            "\n"
            "Git shows when a page was WRITTEN, never whether anyone READ it, so a page written\n"
            "once and never consulted looked exactly like one read every week. The log lives\n"
            "in usage/, which the hook makes git-ignored with its own usage/.gitignore."),
    },
    "optimize_session_days": {
        "default": "30",
        "values": ["7", "30", "90", "180"],
        "summary": "Window, in days, for gt-optimize's session member (prompt-cache cost).",
        "detail": (
            "How far back gt_optimize_session.py reads the Claude Code transcripts when\n"
            "classifying cache writes (cold / growth / expiry / invalid). --days overrides it\n"
            "for one run. A longer window catches a project untouched this month whose old\n"
            "sessions were the largest cost; a shorter one reflects current habits."),
    },
    "optimize_avoidable_pct": {
        "default": "50",
        "values": ["25", "50", "65", "75"],
        "summary": "gt_optimize_session.py --check with no PCT: exit 3 above this avoidable share.",
        "detail": (
            "The avoidable share is cache writes caused by expiry (a session idled past its\n"
            "cache lifetime) or invalidation (the cached prefix changed), as a percentage of\n"
            "all cache writes. `--check PCT` names a threshold for one run; this is the one\n"
            "used when PCT is left out."),
    },
    "memory_contradiction_check": {
        "default": "on",
        "values": ["off", "on"],
        "summary": "Whether /gt:gt-work checks memory notes it wrote against their neighbours for contradictions.",
        "detail": (
            "on   after write-back, gt_memory_check.py compares the memory notes written or\n"
            "     changed this session with same-topic notes in the project, and shows each\n"
            "     pair whose sentences disagree (enabled/disabled, a different value for the\n"
            "     same key) with a question for you. Nothing is changed  (default)\n"
            "off  the pass is skipped"),
    },
    "promotion_candidates": {
        "default": "on",
        "values": ["off", "on"],
        "summary": "Whether /gt:gt-work lists promotion candidates it can detect mechanically.",
        "detail": (
            "on   after write-back, gt_promote_detect.py lists memory notes edited in 3 of\n"
            "     the project's last 5 memory commits (-> research.md / decisions.md),\n"
            "     research.md sections that overlap another project's (-> Knowledge/), and\n"
            "     global-memory notes naming a project (gt_lint's global-scope-leak,\n"
            "     -> demotion). Nothing is promoted without your yes  (default)\n"
            "off  the pass is skipped"),
    },
    "promotion_overlap": {
        "default": "80",
        "values": ["60", "70", "80", "90"],
        "summary": "Token overlap (%) at which two projects' research.md sections are a Knowledge/ candidate.",
        "detail": (
            "Overlap is the shared distinct words of two sections over the smaller section's\n"
            "distinct words (no stemming). Lower finds more, and more noise."),
    },
    # -- the validation host (0.18.0): gt_check.py runs module checkers, gt_apply.py
    #    writes the fixes they propose. --
    "commit_checks": {
        "default": "off",
        "values": ["off", "on"],
        "summary": "Refuse a `git commit` whose staged content no passing gt_check.py run covers.",
        "detail": (
            "off  commits behave exactly as before; checkers run only when asked  (default)\n"
            "on   the commit guard refuses a commit unless every staged file's EXACT staged\n"
            "     bytes appear in a gt_check.py receipt on which every applicable checker\n"
            "     passed. Run `gt_check.py run --staged`, then commit.\n"
            "\n"
            "One guard for every module's checkers, not one hook per module. `cannot-check`\n"
            "(a missing tool, a timeout, malformed output) is not a pass. Per commit:\n"
            "GT_CHECK_GATE=off git commit ...; per repo: `.gt-no-test-gate`."),
    },
    "addon_fixes": {
        "default": "propose",
        "values": ["off", "propose", "apply"],
        "summary": "What happens to the fixes a checker proposes: ignored, listed for you, or applied.",
        "detail": (
            "off      proposals are discarded; findings are reported as before\n"
            "propose  proposals are kept and shown with their diffs; nothing changes until\n"
            "         you run `gt_apply.py apply <id>` (or --all)  (default)\n"
            "apply    after a check run, proposals are applied automatically -- but only a\n"
            "         FIRST-PARTY checker's (its script matches the release MANIFEST); any\n"
            "         other is capped at propose, and the output says so\n"
            "\n"
            "Every apply re-reads the file under a lock and writes only if its hash still\n"
            "equals the one the checker examined; runs EVERY applicable checker on a staged\n"
            "copy first; refuses protected paths, files claimed by another live session,\n"
            "files outside the checker's findings, and content rules (exec bits, new files,\n"
            "symlinks, invisible Unicode, secret/scrub matches, size, HTML scripts and new\n"
            "hosts). Applied fixes are left uncommitted; `gt_apply.py undo <id>` restores."),
    },
    "addon_fix_size_limit": {
        "default": "16k",
        "values": ["1k", "4k", "16k", "64k", "256k"],
        "summary": "Largest size change (bytes added or removed) one add-on fix may make.",
        "detail": (
            "A formatter's fix is usually small. A proposal that grows or shrinks a file by\n"
            "more than this is refused by gt_apply.py whatever its grant -- a fix that\n"
            "large is a rewrite, and a rewrite needs a person.  (default 16k)"),
    },
}


def _module_settings():
    """Register the settings every effectively-ON module declares (0.14.0).

    Read from each module's newest module.json via the gt_components beside this file
    (not whatever `gt_components` sys.path would find), so the registry and the
    installer agree on which modules are on. The plugin root is this file's own when it
    runs from a release (<root>/golden-thread/<ver>/scripts), otherwise the one
    install.sh wired into settings.json. A module setting never overrides a gt one.
    Anything missing -- an older gt_components, no plugin root -- registers nothing.

    0.15.0, the first modules with settings (watch, report card), found three gaps:

      * a module's `detail` was dropped, so `explain` lost the text saying why a setting
        exists. It is now carried when the module declares it.
      * with no reachable plugin source (moved, renamed, a gt-src folder deleted) nothing
        registered, so `get("watch")` answered None from inside the installed hook. The
        installed plugin caches are now read instead: install.sh keeps a cache only for
        modules that are on, and copies module.json into it.
      * design §4 "Remove": a value the user set survives a module being switched off.
        A module that is in the source tree but OFF registers its settings marked
        `orphaned` -- still readable, shown only when set, never deleted -- so turning it
        back on finds the value where it was left.
    """
    try:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location(
            "gt_components_for_settings", os.path.join(here, "gt_components.py"))
        gc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gc)
        ver_dir = os.path.dirname(here)
        if (os.path.basename(os.path.dirname(ver_dir)) == "golden-thread"
                and gc._newest_release(os.path.dirname(ver_dir)) is not None):
            root, gt_version = os.path.dirname(os.path.dirname(ver_dir)), os.path.basename(ver_dir)
        else:
            root, gt_version = gc.plugin_root_from_settings(), None
        mods = []                                   # (name, data, orphaned)
        if root and os.path.isdir(root):
            det = gc.module_detail(root, gt_version=gt_version)
            for m in gc.discover_modules(root):
                if m["reasons"]:
                    continue
                mods.append((m["name"], m["data"], det.get(m["name"], {}).get("state") != "on"))
        else:
            mods = [(n, d, False) for n, d in _cached_modules(gc)]
        # ON modules first, so an orphaned copy of a key never shadows a live one.
        for name, data, orphaned in sorted(mods, key=lambda t: t[2]):
            for s in data.get("settings") or []:
                if s["key"] in SETTINGS:
                    continue
                detail = s.get("detail") if isinstance(s.get("detail"), str) else s["summary"]
                SETTINGS[s["key"]] = {
                    "default": s["default"], "values": list(s["values"]),
                    "summary": s["summary"], "module": name, "orphaned": orphaned,
                    "detail": "%s\n\nProvided by the %s module%s." % (
                        detail, name, " (currently OFF: a value you set is kept, and takes "
                        "effect again with install.sh --with %s)" % name if orphaned else "")}
    except Exception:
        return


def _cached_modules(gc):
    """-> [(name, data)] for every valid module.json in the installed plugin caches."""
    base = os.path.expanduser("~/.claude/plugins/cache/golden-thread-plugin")
    out = []
    try:
        plugins = sorted(os.listdir(base))
    except OSError:
        return out
    for plugin in plugins:
        vd = gc._newest_release(os.path.join(base, plugin))
        if vd is None:
            continue
        data, reasons = gc.validate_module(vd)
        if data and not reasons:
            out.append((data["name"], data))
    return out


_module_settings()


def _load():
    try:
        with open(CONFIG) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(d):
    with open(CONFIG, "w") as fh:
        json.dump(d, fh, indent=2)
        fh.write("\n")


def get(name):
    """Value with the registered default applied. For use by hooks and scripts."""
    spec = SETTINGS.get(name)
    if not spec:
        return None
    v = _load().get(name)
    if v is None:
        v = ""
    elif not isinstance(v, str):
        # Including JSON true/false. A `["yes", "no"]` branch mapping those to strings
        # lived here until 0.16.5; no setting has ever declared those values, gt's or a
        # module's, so it never ran. A hand-written `true` is a typed value where a
        # named one belongs, and falls back to the default like any other non-string.
        sys.stderr.write("gt_settings: %s=%s in %s is not a string; using the default %r\n"
                         % (name, json.dumps(v), CONFIG, spec["default"]))
        v = ""
    v = v.strip().lower()
    if spec["values"] is None:
        return v if _freeform_ok(name, v) else spec["default"]
    return v if v in spec["values"] else spec["default"]


def _freeform_ok(name, value):
    """Validate a setting whose values are not a closed list.

    Only `parallel_max` is free-form, and it is free-form for a reason: a worker
    ceiling is a number, and enumerating 1..64 in `values` would be a list nobody
    reads pretending to be a type. Anything unparseable falls back to the registered
    default rather than being written through, so a typo cannot silently uncap or
    serialise every run.
    """
    if name == "decision_signals":
        # A phrase list, not a closed set (0.18.0); see its `detail`. One line, non-empty.
        return bool(value.strip()) and "\n" not in value
    if name != "parallel_max":
        return False
    if value == "auto":
        return True
    try:
        return int(value) >= 1
    except ValueError:
        return False


def parallel_jobs(want, io_bound=False):
    """-> worker count to use for `want` units of independent work, honouring settings.

    The single place the two settings turn into a number, so every caller answers
    "how many workers" the same way instead of each inventing a policy.

    `parallel_work=off` returns 1 -- serial, through whatever path the caller uses.
    `parallel_max=auto` returns the profile's `cpu_max` (cores), or its `io_max` when
    the work is I/O-bound: 2x cores, bounded by memory. These tools spend their time
    waiting on install.sh, SSH and HTTP, so more workers than cores is the correct
    answer there and `auto` means "as many as the machine allows", not "exactly ncpu".
    The 2x rule is stated once, in detect_machine(), and `parallel_max` quotes it; this
    docstring said "cores + 4 (capped)" until 0.16.5, which no code here has ever done.
    """
    want = max(1, int(want))
    if get("parallel_work") == "off":
        return 1
    prof = machine_profile()
    cap = prof["io_max"] if io_bound else prof["cpu_max"]
    limit = get("parallel_max")
    if limit != "auto":
        cap = min(cap, int(limit))
    return max(1, min(want, cap))


def detect_machine():
    """-> the ceilings THIS machine supports, measured now.

    `auto` has to mean "as much as this machine allows", and the machine is not
    knowable from a number someone wrote down: 20 workers was right for the laptop it
    was chosen on and arbitrary everywhere else. So the ceilings are derived from the
    hardware and stored at install time (see write_machine_profile), not guessed at
    each call and not frozen into the source.

    cpu_max = logical cores. More processes than cores on CPU-bound work costs
    context switching and returns nothing.

    io_max = 2x cores, and never more workers than there is memory to hold them
    (~1 worker per 256 MB, which is generous for a subprocess that spends its life
    waiting). I/O-bound work is waiting, not computing, so oversubscribing cores is
    the correct answer -- the limit is memory and the remote end, not the CPU.
    """
    import platform
    import socket
    import subprocess

    def sysctl(name):
        try:
            out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True)
            return int(out.stdout.strip())
        except Exception:
            return 0

    cores = os.cpu_count() or 4
    physical = sysctl("hw.physicalcpu") or cores
    membytes = sysctl("hw.memsize")
    if not membytes:                      # Linux
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        membytes = int(line.split()[1]) * 1024
                        break
        except Exception:
            membytes = 0
    memory_gb = round(membytes / (1024 ** 3), 1) if membytes else 0.0
    by_memory = int(memory_gb * 4) if memory_gb else 10 ** 6   # ~1 worker / 256 MB
    return {
        "cores": cores,
        "physical_cores": physical,
        "memory_gb": memory_gb,
        "cpu_max": max(1, cores),
        "io_max": max(2, min(cores * 2, by_memory)),
        "host": socket.gethostname(),
        "platform": platform.platform(),
    }


def machine_profile():
    """The stored profile, or a live reading if none has been written yet.

    Stored, because a profile is measured at install and re-measured at update -- it
    must not drift under a running session, and every tool on the machine must get the
    same answer. Missing is not an error: a clone with no ~/.claude still has to work,
    it just falls back to reading the machine now.
    """
    prof = _load().get("parallel_profile")
    if isinstance(prof, dict) and prof.get("cpu_max") and prof.get("io_max"):
        try:
            return {"cpu_max": max(1, int(prof["cpu_max"])),
                    "io_max": max(1, int(prof["io_max"]))}
        except (TypeError, ValueError):
            pass
    return detect_machine()


def write_machine_profile(force=False):
    """Measure and store the profile. Called by install.sh on install AND upgrade.

    Returns (profile, changed). It rewrites `parallel_profile`, which is a record of
    the HARDWARE, and never touches `parallel_work` or `parallel_max`, which are the
    user's preferences: re-running the installer must not undo a ceiling somebody chose
    deliberately. A profile whose numbers are unchanged is not rewritten, so the
    detected_at timestamp means "when this machine last looked different".
    """
    import datetime
    d = _load()
    if not d.get("vault_path"):
        return None, False
    fresh = detect_machine()
    old = d.get("parallel_profile") or {}
    same = all(old.get(k) == fresh[k] for k in ("cores", "cpu_max", "io_max", "host"))
    if same and not force:
        return old, False
    fresh["detected_at"] = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    d["parallel_profile"] = fresh
    _save(d)
    return fresh, True


HOOK_FLAG = "--hook"


def hook_args(argv=None):
    """-> (argv without the hook flag, True if it was present).

    install.sh registers every hook command with `--hook`. That flag, not a
    guess, is what decides whether output is wrapped as hook JSON. 0.9.6 used
    `sys.stdout.isatty()`, which is False for a hook but ALSO for `| tee`, cron,
    a subagent and every command the assistant runs through its Bash tool -- so
    the plain-text report the docstrings promised came out as an escaped JSON
    blob for everyone except a human at a terminal.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    is_hook = HOOK_FLAG in argv or os.environ.get("GT_HOOK") == "1"
    return [a for a in argv if a != HOOK_FLAG], is_hook


def emit(text, event="SessionStart", as_hook=None):
    """Deliver a startup report to BOTH the user and the model.

    A SessionStart hook's plain stdout goes into the model's context only -- it is
    never rendered in the user's terminal. So every one of these checks was already
    "said out loud" in the sense its own comments claim, but only to the assistant.
    From the user's side a clean check and a hook that never ran are the same thing:
    silence. That is the 2026-08-30 shape again, one layer out -- the check was
    honest, its delivery went to the wrong audience.

    `systemMessage` is the field that surfaces to the user on any platform;
    `additionalContext` is what the model reads. Both are sent deliberately: the
    model must keep receiving this, because CLAUDE.md asks the assistant to open
    each session by stating these results, and that prose rule is the backstop for
    anything this JSON cannot reach.

    Hook JSON is produced only when the caller says it is a hook (`as_hook`, or
    the `--hook` flag or GT_HOOK=1 seen by `hook_args`). Every other caller -- a
    human, a pipe, the assistant's Bash tool -- gets the plain text unchanged.
    """
    text = (text or "").strip()
    if not text:
        return
    if as_hook is None:
        as_hook = hook_args()[1]
    if not as_hook:
        print(text)
        return
    json.dump({
        "systemMessage": text,
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        },
    }, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def capture(fn, *a, **kw):
    """Run fn, returning (result, text-it-printed).

    Lets the reporting functions keep their many plain print() calls -- they are
    readable that way, and they are also the manual CLI output -- while the hook
    entry point decides how the result is delivered.

    If fn raises part-way, everything it printed so far is KEPT and the failure is
    appended to it, so the report is delivered with the crash rather than replaced
    by it. 0.9.6 dropped the buffer on the way out: a worker report that had
    already listed three orphans, then hit an unexpected error in reap(), reached
    nobody -- only a traceback on stderr, which a hook shows to no one.
    """
    import contextlib
    import io
    import traceback
    buf = io.StringIO()
    r = None
    try:
        with contextlib.redirect_stdout(buf):
            r = fn(*a, **kw)
    except Exception as exc:
        buf.write("\n  ... check aborted: %s: %s\n" % (type(exc).__name__, exc))
        sys.stderr.write(traceback.format_exc())
    return r, buf.getvalue()


def show():
    cfg = _load()
    print("Golden Thread settings   (%s)" % CONFIG)
    print()
    groups = [(None, [n for n, s in SETTINGS.items() if not s.get("module")])]
    for mod in sorted({s["module"] for s in SETTINGS.values() if s.get("module")}):
        # An OFF module's settings are listed only when the user has set one: the value is
        # kept, and saying so beats a key that silently vanished from the list.
        names = [n for n, s in SETTINGS.items() if s.get("module") == mod
                 and (not s.get("orphaned") or cfg.get(n) not in (None, ""))]
        if names:
            groups.append((mod, names))
    for mod, names in groups:
        if mod and any(SETTINGS[n].get("orphaned") for n in names):
            print("module %s   (OFF — settings kept, not in effect; install.sh --with %s)"
                  % (mod, mod))
            print()
        elif mod:
            print("module %s" % mod)     # a module's settings, under its name
            print()
        for name in names:
            spec = SETTINGS[name]
            raw = cfg.get(name)
            cur = get(name)
            mark = "" if raw not in (None, "") else "   (default — not set in the file)"
            print("  %-20s %s%s" % (name, cur, mark))
            print("  %-20s %s" % ("", spec["summary"]))
            opts = " | ".join(spec["values"]) if spec["values"] else spec["validate"]
            print("  %-20s options: %s" % ("", opts))
            print()
    prof = cfg.get("parallel_profile")
    if isinstance(prof, dict):
        print("  %-20s %s" % ("parallel_profile",
                              "cpu_max %s, io_max %s   (%s core(s), %s GB, measured %s)"
                              % (prof.get("cpu_max"), prof.get("io_max"),
                                 prof.get("cores"), prof.get("memory_gb"),
                                 prof.get("detected_at", "?"))))
        print("  %-20s %s" % ("", "what `parallel_max: auto` resolves to on this machine; "
                                  "measured at install, re-measured on upgrade"))
        print()
    print("change with:  python3 gt_settings.py set <name> <value>")
    print("explain with: python3 gt_settings.py explain <name>")
    return 0


def explain(name):
    spec = SETTINGS.get(name)
    if not spec:
        print("unknown setting: %s" % name)
        print("known: %s" % ", ".join(SETTINGS))
        return 2
    print("%s  (current: %s, default: %s)" % (name, get(name), spec["default"]))
    print()
    print(spec["summary"])
    print()
    print(spec["detail"])
    return 0


def check_against_machine(value, force=False):
    """-> (refuse: bool, message or None) for a parallel_max the user typed.

    A ceiling the hardware cannot honour is not a preference, it is a typo with
    consequences: `40` on a four-core laptop makes nothing faster, it thrashes, and the
    person who typed it has no way to learn that from a confirmation message. So the
    value is checked against the measured profile at the moment it is set -- the only
    moment anyone is paying attention to it.

    Two bands, because "more than the processors" is not automatically wrong:

      * above io_max (2x cores)  -> refused. Nothing here can use it.
      * cores < N <= io_max      -> allowed WITH a warning. Oversubscribing cores is
                                    correct for I/O-bound work and useless for
                                    CPU-bound work; the user should know which they
                                    are buying.

    --force covers the case the profile cannot see: a fan-out over a hundred
    high-latency remotes is bounded by the network, not by this CPU. It is explicit, so
    it shows up in the transcript instead of being assumed.
    """
    if value == "auto":
        return False, None
    n = int(value)
    prof = machine_profile()
    cores, io_max = prof["cpu_max"], prof["io_max"]
    if n > io_max and not force:
        return True, ("%d is more than this machine can use: %d core(s), so at most %d "
                      "workers even for I/O-bound work.\n"
                      "  `auto` already means \"as many as this machine allows\", and it "
                      "re-measures itself on every upgrade.\n"
                      "  If you meant it -- a fan-out bounded by the network rather than "
                      "this CPU -- re-run with --force." % (n, cores, io_max))
    if n > cores:
        return False, ("note: %d exceeds this machine's %d core(s). That helps I/O-bound "
                       "work (waiting on SSH, HTTP, subprocesses) and does nothing for "
                       "CPU-bound work." % (n, cores))
    return False, None


def set_value(name, value, force=False):
    spec = SETTINGS.get(name)
    if not spec:
        print("unknown setting: %s" % name)
        print("known: %s" % ", ".join(SETTINGS))
        return 2
    value = (value or "").strip().lower()
    bad = (not _freeform_ok(name, value)) if spec["values"] is None \
        else (value not in spec["values"])
    if bad:
        print("invalid value %r for %s" % (value, name))
        print("valid: %s" % (spec["validate"] if spec["values"] is None
                             else " | ".join(spec["values"])))
        return 2
    if name == "parallel_max":
        refuse, msg = check_against_machine(value, force=force)
        if refuse:
            print("invalid value %r for parallel_max" % value)
            print("  " + msg)
            return 2
        if msg:
            print("  " + msg)
    d = _load()
    if not d.get("vault_path"):
        # Refuse to create a config that would leave the hooks unable to find the
        # vault -- writing a partial file here would break enforcement, not extend it.
        print("refusing to write %s: it has no vault_path. Run /gt:gt-init first."
              % CONFIG)
        return 2
    was = get(name)
    d[name] = value
    _save(d)
    print("%s: %s -> %s" % (name, was, value))
    if spec.get("orphaned"):
        print("  note: the %s module is off, so this is kept but not in effect until "
              "install.sh --with %s" % (spec["module"], spec["module"]))
    return 0


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("show", "list"):
        return show()
    if a[0] == "explain" and len(a) > 1:
        return explain(a[1])
    if a[0] == "set" and len(a) > 2:
        force = "--force" in a
        rest = [x for x in a[1:] if x != "--force"]
        if len(rest) < 2:
            print("usage: gt_settings.py set <name> <value> [--force]")
            return 2
        return set_value(rest[0], rest[1], force=force)
    if a[0] == "get" and len(a) > 1:
        print(get(a[1]) or "")
        return 0
    if a[0] in ("detect-machine", "machine"):
        force = "--force" in a
        if "--write" in a or force:
            prof, changed = write_machine_profile(force=force)
            if prof is None:
                print("refusing to write %s: it has no vault_path. Run /gt:gt-init first."
                      % CONFIG)
                return 2
            print("parallel_profile %s: %d core(s), %.1f GB -> cpu_max %d, io_max %d"
                  % ("updated" if changed else "unchanged", prof.get("cores", 0),
                     prof.get("memory_gb", 0.0), prof["cpu_max"], prof["io_max"]))
            return 0
        prof = detect_machine()
        print(json.dumps(prof, indent=2))
        return 0
    if a[0] == "jobs":
        # The budget as a NUMBER, for shell scripts and for any project's own tooling:
        #   JOBS=$(gt_settings.py jobs 42 --io-bound)
        # Without this the two settings are only reachable from Python, and a bash
        # script's only option is to invent its own policy -- which is what the Core
        # rule core_parallel_when_beneficial is trying to stop happening in every repo.
        rest = [x for x in a[1:] if x not in ("--io-bound", "--io")]
        io_bound = len(rest) != len(a[1:])
        try:
            want = int(rest[0]) if rest else 10 ** 6
        except ValueError:
            print("usage: gt_settings.py jobs [<units>] [--io-bound]", file=sys.stderr)
            return 2
        print(parallel_jobs(want, io_bound=io_bound))
        return 0
    print("usage: gt_settings.py [show | get <name> | set <name> <value> | "
          "explain <name> | jobs [<units>] [--io-bound] | "
          "detect-machine [--write] [--force]]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
