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
which is tied to no Core rule. (0.18.1 adds a third: `foreign_checkout_guard` governs
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
import re
import sys

CONFIG = os.path.expanduser("~/.claude/vault-config.json")


def _lockdown_detail():
    """The lockdown chart, from gt_lockdown's own table, so the setting cannot drift from it."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import gt_lockdown
        return (gt_lockdown.table_text() + "\n\n"
                "Changing it writes (or removes) only gt's own allow/deny rules, backs up\n"
                "settings.json first and logs the change; restart Claude Code afterwards.\n"
                "gt never changes the level by itself -- you choose it at install or here.")
    except Exception:                            # noqa: BLE001
        return "very-secure | mostly-secure | partly-secure | insecure (gt_lockdown.py missing)"

SETTINGS = {
    "allin_timeout": {
        "default": "300",
        "values": ["300", "600", "1200", "1800", "3600"],
        "summary": "Seconds each all-in check may take, in gt-allin and the commit gate.",
        "detail": (
            "300     the default; enough for a typical repo\n"
            "600-3600  for a large repo whose scan takes longer\n"
            "\n"
            "A check that runs past it is COULD NOT RUN, which refuses a commit like any other\n"
            "unknown. The commit gate runs the checks one after another and gives the whole\n"
            "run 12 times this. Until 0.19.1 it was a fixed 300 s, and gt's own repo (29\n"
            "release folders) could never pass the gate."),
    },
    "agent_models": {
        "default": "task",
        "values": ["task", "session"],
        "summary": "Pick each specialist agent's model by its task, not the session's.",
        "detail": (
            "task     each agent stage runs at its spec's tier: classify and draft haiku,\n"
            "         extract and place sonnet, reconcile, verify and generalize opus  (default)\n"
            "session  no model is passed; every agent runs on the session's model\n"
            "\n"
            "Whatever the skills' model profile, so a very-high machine still reads documents\n"
            "on sonnet (owner, 2026-10-02). Per-agent overrides, by stage or job type:\n"
            "gt_model_policy.py set --agent extract --model haiku [--effort low]. Since\n"
            "0.20.1 each stage's agent definition (gt:<stage>) carries model AND effort --\n"
            "classify and draft haiku (no effort), extract and place sonnet medium, the rest\n"
            "opus high -- and changing this setting rewrites them at once."),
    },
    "vault_hints": {
        "default": "off",
        "values": ["off", "on"],
        "summary": "Name up to three vault pages relevant to each prompt (titles only).",
        "detail": (
            "off     nothing is added  (default)\n"
            "on      a UserPromptSubmit hook matches the prompt against index.md and adds at\n"
            "        most three '- <title> — <path>' lines\n"
            "\n"
            "It reads one file, index.md, and never a page body; the session decides whether\n"
            "to open anything. Nothing is added below the match threshold, without a vault,\n"
            "or past its 0.8 s budget. A per-turn cost when on, which is why it ships off\n"
            "(request 2026-10-02-prompt-relevant-vault-hints, 0.19.1)."),
    },
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
            "on   a skill with a stage x kind spec (extract, classify, reconcile, draft,\n"
            "     verify, ... x code, docs, tool, session, wiki -- 0.18.1) spawns a specialist\n"
            "     agent with that spec applied; its full output\n"
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
            "Independent of agent_specialization (0.18.1): this alone turns the skeptic on,\n"
            "and does not hand ingest or validation to specialist agents."),
    },
    "handoff_surface": {
        "default": "any",
        "values": ["any", "project", "manual"],
        "summary": "Where a handoff that has not been handled is shown.",
        "detail": (
            "any      every session start, whatever project is opened  (default)\n"
            "project  only when /gt:gt-open opens the handoff's own project\n"
            "manual   only when you run /gt:gt-handle handoff\n"
            "\n"
            "A handoff keeps being shown until it is handled (marked, or every task citing\n"
            "it closed) or deferred to a date. It is shown as one line -- path, project, age,\n"
            "open items -- never its body, so being told costs no project context; handling\n"
            "it is /gt:gt-handle handoff. `project` needs /gt:gt-open: a session that never\n"
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
            "while you were away gets none (0.18.1)."),
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
            "and started rewriting its remote URL to get the push through (0.18.1)."),
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
    "unlock": {
        "default": "off",
        "values": ["off", "on"],
        "summary": "gt unlock: agents need your presence (TOTP + Touch ID / Windows Hello) for LOTR, secrets, publishing and gt's guards.",
        "detail": (
            "off  nothing is gated; every hook's path is unchanged  (default)\n"
            "on   the unlock authority (gt_unlockd.py) decides: LOTR connections, sealed and\n"
            "     brokered credentials, publish credentials, edits to gt's hooks/settings and\n"
            "     the security settings below need a grant, after you prove presence\n"
            "\n"
            "The source of truth is the unlock policy (~/.claude/golden-thread/unlock/policy.json,\n"
            "tightened by an administrator's floor), not vault-config.json: `set unlock on|off`\n"
            "runs `gt_unlock.py policy enable|disable`, which needs your enrolled factors.\n"
            "Enrol first: gt_unlock.py enroll touchid (macOS) / hello (Windows), then totp;\n"
            "with TOTP alone: enroll totp, then gt_unlock.py policy enable --factors totp.\n"
            "`gt_unlock.py status` names the next step.\n"
            "It is not anti-malware: something already running as you can wait for you to\n"
            "unlock. SECURITY.md says exactly what each level stops (0.20.1)."),
    },
    "lockdown": {
        "default": "very-secure",
        "values": ["very-secure", "mostly-secure", "partly-secure", "insecure"],
        "summary": "How much Claude may run without asking: allow rules gt writes into "
                   "~/.claude/settings.json for the level you choose (0.20.5).",
        "detail": _lockdown_detail(),
    },
    "sandbox_mode": {
        "default": "off",
        "values": ["off", "on"],
        "summary": "gt sandbox mode: fence Claude's shell and file tools off the vault and gt's state; reach the vault through gt's MCP and write queue.",
        "detail": (
            "Since 0.20.2 every skill works under it: each says which gt-vault MCP tool takes a\n"
            "step that would run a vault script from Claude's shell (gt_log, gt_tasks, gt_adr,\n"
            "gt_lint, gt_broker drain, ...), and hands you the few that need you (settings,\n"
            "install, upgrade, sync, Core rules) as one command to run in a terminal.\n"
            "\n"
            "off  Claude's shell and file tools reach the vault and gt's state as before  (default)\n"
            "on   gt_sandbox.py writes into ~/.claude/settings.json (and records what it wrote):\n"
            "       sandbox.enabled true, allowUnsandboxedCommands false, failIfUnavailable true\n"
            "       -- Claude Code's OS sandbox around Bash/PowerShell/Monitor and their children\n"
            "       (Seatbelt on macOS, bubblewrap on Linux and WSL2);\n"
            "       sandbox.filesystem.denyWrite: the vault, ~/.claude/golden-thread (hooks, state,\n"
            "       unlock home), the LOTR home and store, ~/.claude/plugins, settings.json and\n"
            "       vault-config.json; denyRead: the unlock home, LOTR, locked vault folders (and\n"
            "       the whole vault while sandbox_vault_reads is deny); allowWrite: ~/.gt-inbox,\n"
            "       the queue inbox, and ~/.gt-scratch, the stage agents' private scratch\n"
            "       folders; permissions.deny Read(...)/Edit(...) rules for the same\n"
            "       paths, because the file tools run outside the sandbox\n"
            "\n"
            "The vault is then read through gt's vault MCP server (vault_list, vault_read,\n"
            "vault_search) and written only through the queue (the vault_queue_write tool, or\n"
            "gt_write_queue.py, which drops its request in ~/.gt-inbox/queue/); the broker\n"
            "applies it outside the sandbox. Hooks and MCP servers run outside the sandbox by\n"
            "Claude Code's design, so they keep working. Restart Claude Code after switching.\n"
            "\n"
            "NATIVE WINDOWS has no Claude Code sandbox: only the permission rules are written\n"
            "-- friction, not a boundary. Linux/WSL2 need bubblewrap and socat; without them\n"
            "turning this on is refused, because failIfUnavailable would stop Claude Code from\n"
            "starting. Turning it OFF from inside the sandbox fails by design (~/.claude is\n"
            "write-protected there): run it in a terminal. With gt unlock on, switching it off\n"
            "needs a fresh confirmation. Not anti-malware: something already running as you\n"
            "is not fenced. SECURITY.md, 'gt sandbox mode' (0.20.1)."),
    },
    "sandbox_vault_reads": {
        "default": "deny",
        "values": ["deny", "allow"],
        "summary": "Under sandbox mode, whether Claude's shell and file tools may also READ the vault.",
        "detail": (
            "deny   the vault is read-denied too: Claude reads it only through the vault MCP\n"
            "       tools, which skip locked folders and cap what one call returns  (default)\n"
            "allow  the shell and file tools may read the vault (gt's vault scripts keep working\n"
            "       from Claude's shell); writes stay denied, and locked folders stay unreadable\n"
            "\n"
            "Only takes effect while sandbox_mode is on; changing it re-applies the settings.\n"
            "With deny, gt's own vault tools (gt_tasks, gt_lint, ...) cannot read the vault from\n"
            "Claude's shell either -- run them in a terminal, or use the MCP tools."),
    },
    "symlink_writes_outside_vault": {
        "default": "refuse",
        "values": ["refuse", "allow"],
        "summary": "Whether a vault tool may append through a link that points OUTSIDE the vault.",
        "detail": (
            "refuse  a vault file that is a link to somewhere outside the vault is not written\n"
            "        through; the tool stops and names the link  (default)\n"
            "allow   write through it -- for a link you made on purpose, e.g. a log shared with\n"
            "        another folder or machine\n"
            "        Every write it lets through is logged in the vault, with the file, where it\n"
            "        really went and the lines written: Projects/golden-thread/outside-vault-writes.jsonl\n"
            "\n"
            "A link that resolves INSIDE the vault is always written through, unless it reaches\n"
            "design.md or global-memory/, which no tool writes, link or not, whatever this says.\n"
            "Outside the vault is opt-in because the text can be model-chosen: a planted link to\n"
            "a shell profile would turn it into code (0.20.2, review M-A)."),
    },
    "push_fingerprint": {
        "default": "off",
        "values": ["off", "on"],
        "summary": "A fingerprint (gt unlock step-up) before every git push from the repos in push_fingerprint_repos.",
        "detail": (
            "off  pushes are not gated  (default)\n"
            "on   one switch (gt 0.20.3): needs one enrolled factor (enroll touchid, or hello on Windows), turns gt\n"
            "     unlock on in a PUSH-ONLY profile -- every scope open except gt:publish and the\n"
            "     unlock policy/enrollment, which need a fresh confirmation -- and installs a git\n"
            "     pre-push hook in each repo in push_fingerprint_repos. A push of main or a tag\n"
            "     from a repo that ships dev/release-check.sh runs it --quick first (docs gate).\n"
            "     `off` removes exactly those hooks and restores the earlier unlock policy.\n"
            "Proof: gt_push_guard.py check. Limit: a hook is skipped by `git push --no-verify`;\n"
            "the lock is a push credential sealed behind gt:publish (SECURITY.md)."),
    },
    "push_fingerprint_seal_token": {
        "default": "off",
        "values": ["off", "on"],
        "summary": "With push_fingerprint on, also seal the GitHub push token behind gt:publish (the lock).",
        "detail": (
            "off  the pre-push hook asks for the fingerprint; git uses gh's token as usual, so an\n"
            "     assistant session can still push after your touch  (default)\n"
            "on   the token moves into gt unlock's sealed store and git gets it only under gt:publish,\n"
            "     so a plain `git push --no-verify` still asks (limits: SECURITY.md 5.5a). gt unlock serves secrets only to its\n"
            "     MCP shim: run `push_fingerprint on` from YOUR terminal, and expect pushes started by an\n"
            "     assistant session to be refused. Set this before turning push_fingerprint on."),
    },
    "push_fingerprint_repos": {
        "default": "",
        "values": None,
        "validate": "comma-separated absolute paths of git repositories",
        "summary": "The git repositories push_fingerprint guards (comma-separated paths).",
        "detail": (
            "Absolute paths, comma-separated. Worktrees share their repository's hooks, so one\n"
            "path covers every worktree of it. Set this before `push_fingerprint on`."),
    },
    "commit_fingerprint": {
        "default": "off",
        "values": ["off", "on"],
        "summary": "A fingerprint on every commit: a Secure Enclave key signs it (gt_sign.py), in the repos in push_fingerprint_repos.",
        "detail": (
            "off  commits are not signed by gt  (default)\n"
            "on   one switch (gt 0.20.3, macOS): creates a Secure Enclave signing key that needs a\n"
            "     currently enrolled finger for every use, and sets each repo in\n"
            "     push_fingerprint_repos to sign every commit and tag with it (git's ssh signing).\n"
            "     Every commit asks for Touch ID; a rebase of ten commits is ten touches.\n"
            "     `off` puts the earlier git config back exactly; the key is kept.\n"
            "The server half makes the server refuse unsigned commits: `gt_sign.py github` shows the\n"
            "plan (the key as a GitHub signing key, and a ruleset requiring signed commits on the\n"
            "default branch with no bypass), `--apply` asks before changing it. Proof: gt_sign.py check --live."),
    },
    "vault_mcp": {
        "default": "auto",
        "values": ["auto", "on", "off"],
        "summary": "Whether gt's vault MCP server offers its tools (vault_list/read/search, vault_queue_write).",
        "detail": (
            "auto  offered while sandbox_mode is on, otherwise not  (default)\n"
            "on    always offered, to Claude Code and to any MCP client that starts the server\n"
            "off   never offered\n"
            "\n"
            "The server ships in gt's plugin manifest, so Claude Code starts it with every\n"
            "session; when its tools are not offered it lists none and reads nothing. It is\n"
            "itself writes only a new Sources/ file and the body of an ADR slot its own session\n"
            "allocated; every other vault write is queued with gt_write_queue's own validation\n"
            "and decided by the broker, or made by the vault's own tools, and none writes\n"
            "design.md or global-memory/ (0.20.2). It never returns a file outside the vault\n"
            "or inside a locked folder, and caps what one call returns. With gt unlock on, reads\n"
            "need the gt:vault:read scope and are served only to the session's registered vault\n"
            "server (door mcp_only), never to a process from Claude's shell. Restart Claude Code\n"
            "after switching (request 2026-10-02-vault-mcp-read-server, 0.20.1)."),
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
            "Added 0.18.1: decisions stated in prose and never recorded are re-debated by\n"
            "the next session, or changed because nothing said they were deliberate."),
    },
    # -- 0.18.1, group g5 (review stamps, vault sync, reminders) ------------------------
    "review_stamp": {
        "default": "on",
        "values": ["off", "on"],
        "summary": "Stamp last_reviewed on a Knowledge page when gt-query or gt-open reads it.",
        "detail": (
            "on   after /gt:gt-query answers from a Knowledge page, or /gt:gt-open loads one\n"
            "     as project context, gt_review_stamp.py queues `last_reviewed: <today>` on\n"
            "     it through the write queue  (default)\n"
            "off  nothing is stamped\n"
            "\n"
            "The wiki lint's review-due check runs from the NEWER of last_reviewed and\n"
            "updated, so a page read every week stops showing up as review-due merely\n"
            "because nobody has edited it. A page never stamped behaves exactly as before.\n"
            "Each stamp is one frontmatter line, at most once per page per day."),
    },
    "sync_check": {
        "default": "off",
        "values": ["off", "cached", "fetch"],
        "summary": "At session start, say whether the vault is BEHIND its upstream (gt_sync).",
        "detail": (
            "off     no behind-check  (default)\n"
            "cached  compare with the remote-tracking ref already on disk -- no network,\n"
            "        but only as fresh as the last fetch (the line says how old that is)\n"
            "fetch   run one bounded `git fetch` first (a few seconds at most, never a\n"
            "        password prompt), then compare\n"
            "\n"
            "The push check says when this machine has commits origin lacks. The opposite\n"
            "-- origin has commits this machine lacks -- needs a fetch to see, and a fetch\n"
            "is network I/O at session start, which is why it is opt-in. Pull with\n"
            "/gt:gt-sync pull (fast-forward only; never merges or rebases)."),
    },
    "reminder_days": {
        "default": "7",
        "values": ["1", "3", "7", "14", "30"],
        "summary": "Reminders cover overdue items plus items due within this many days.",
        "detail": (
            "Overdue items are always included. A row of <vault>/deadlines.md due within\n"
            "this many days is included too  (default 7). Applies to the push channels\n"
            "(reminder_macos, reminder_relay, reminder_email); the session-start MUST DO\n"
            "block keeps its own 14-day window."),
    },
    "reminder_macos": {
        "default": "off",
        "values": ["off", "on"],
        "summary": "Reminder channel: a macOS notification from the scheduled reminder job.",
        "detail": (
            "off  no notification  (default)\n"
            "on   the `reminder` job (gt_schedule.py install reminder) posts one macOS\n"
            "     notification listing overdue and near items. No credential.\n"
            "\n"
            "Setup and test: gt_reminder.py setup macos / gt_reminder.py check macos."),
    },
    "reminder_relay": {
        "default": "off",
        "values": ["off", "sms", "discord"],
        "summary": "Reminder channel: SMS or Discord through your notification relay.",
        "detail": (
            "off      nothing is sent  (default)\n"
            "sms      POST the reminder to your relay, routed to SMS\n"
            "discord  POST the reminder to your relay, routed to Discord\n"
            "\n"
            "The relay URL and any credential live in a mode-600 file written by your\n"
            "secrets store (~/.claude/golden-thread/reminder/relay.json), never here.\n"
            "Setup and test: gt_reminder.py setup relay / gt_reminder.py check relay."),
    },
    "reminder_email": {
        "default": "off",
        "values": ["off", "on"],
        "summary": "Reminder channel: an email through your SMTP server.",
        "detail": (
            "off  nothing is sent  (default)\n"
            "on   the reminder job sends one email over SMTP (STARTTLS or SSL)\n"
            "\n"
            "Server, addresses and password live in a mode-600 file written by your secrets\n"
            "store (~/.claude/golden-thread/reminder/email.json), never here.\n"
            "Setup and test: gt_reminder.py setup email / gt_reminder.py check email."),
    },
    # ---- 0.18.1: execution (fast build loop, execution metrics, measured profile) ----
    "execution_metrics": {
        "default": "on",
        "values": ["off", "on"],
        "summary": "Record one row per execution of tests, pipeline steps and gt skills (gt_metrics.py).",
        "detail": (
            "on   every instrumented run -- tests/prun.py, a release pipeline step, a gt skill --\n"
            "     appends one row: process, duration, exit, units, parallelism, tokens where\n"
            "     known, machine, commit. Arguments are HASHED, never stored, and a value with a\n"
            "     credential shape is stored only as its hash  (default)\n"
            "off  nothing is recorded, by anything\n"
            "\n"
            "The rows live in the vault project's metrics/ folder (or ~/.claude/golden-thread/\n"
            "metrics/ for a project with no folder) and never leave the machine except through\n"
            "the vault. `/gt:gt-optimize --execution` reads them: a rolling baseline per process,\n"
            "regressions against it, and savings that are measured rather than assumed."),
    },
    "scoped_receipts": {
        "default": "on",
        "values": ["off", "on"],
        "summary": "On a feature branch, accept a test receipt from the tests mapped to the changed files.",
        "detail": (
            "on   on a branch that is not the default branch, the commit guard accepts a\n"
            "     receipt from `tests/run.sh --affected` (prun.py --affected) for the files it\n"
            "     covered. The default branch, and every release gate, still need a FULL-suite\n"
            "     receipt  (default)\n"
            "off  every commit needs a full-suite receipt\n"
            "\n"
            "Why: gt 0.17.11 ran its ~2,400-test suite about eight times in one day, including\n"
            "for single-tool changes. The full suite stays required once per release."),
    },
    "test_tmpdir": {
        "default": "off",
        "values": ["off", "noindex"],
        "summary": "Where the test runner puts throwaway files (TMPDIR for prun.py).",
        "detail": (
            "off      leave TMPDIR as the system sets it  (default)\n"
            "noindex  a cache folder Spotlight and the sync agents ignore:\n"
            "         ~/Library/Caches/gt-tests.noindex on macOS, $XDG_CACHE_HOME/gt-tests\n"
            "         (else ~/.cache/gt-tests) elsewhere\n"
            "\n"
            "GT_TEST_TMPDIR=<path> overrides both for one run. On 2026-10-01 a full local run\n"
            "drove a Mac's load to 50-96: every throwaway install was file churn that the\n"
            "indexer, the sync agents and the virus scanner all reacted to. Known snag: a\n"
            "folder carrying com.apple.provenance refused copies from an Intel Homebrew Python\n"
            "(EPERM) -- `gt_doctor.py --only execution` reports a translated (Rosetta) shell."),
    },
    "runners": {
        "default": "",
        "values": None,
        "validate": "comma-separated ssh host aliases, or empty",
        "summary": "Remote hosts that may run tests and calibration (prun.py --hosts, gt_bench.py --hosts).",
        "detail": (
            "empty  no host is ever contacted  (default)\n"
            "a,b    these ssh aliases may receive a `git archive` of the COMMITTED tree (code\n"
            "       only -- nothing from the vault ever leaves the machine) and run tests or\n"
            "       the calibration workload there\n"
            "\n"
            "Opt-in by design: nothing is sent anywhere unless it is listed here. gt_bench.py\n"
            "--hosts measures each one; prun.py --hosts splits units across them, heaviest\n"
            "first, attributes every failure to its host, and reports an unreachable host as\n"
            "skipped -- never as passing."),
    },
    # install_demo was removed in 0.14.0: the demo is a module, and whether it is
    # installed is a module choice (install.sh --with/--without demo, recorded in
    # ~/.claude/golden-thread/install-choices.json). A user's install_demo key in
    # vault-config.json is left alone; a later migration removes it.
    # -- 0.18.1: memory and gt-optimize ------------------------------------------------
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
    # -- the validation host (0.18.1): gt_check.py runs module checkers, gt_apply.py
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
    with open(CONFIG, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(d, fh, indent=2)
        fh.write("\n")


# Settings that switch a GUARD off (0.20.1). With gt unlock on, changing one needs the scope
# gt:settings:security -- a fresh confirmation the unlock authority raises -- so a session
# cannot loosen its own guards with one command. With unlock off nothing changes.
SECURITY_KEYS = ("protected_paths", "test_gate", "foreign_checkout_guard", "component_updates",
                 "commit_checks", "addon_fixes", "unlock", "sandbox_mode", "sandbox_vault_reads",
                 "symlink_writes_outside_vault",
                 # loosening what Claude may run without asking must not be one agent command away
                 "lockdown",
                 # the fingerprint guards (0.20.4, independent review): off from a session must ask
                 "push_fingerprint", "push_fingerprint_seal_token", "push_fingerprint_repos",
                 "commit_fingerprint")


def _sandbox_switch(name, value, d):
    """sandbox_mode / sandbox_vault_reads (0.20.1): write or remove the Claude Code settings
    BEFORE the value is recorded, so vault-config.json never says "on" for settings that were
    refused. -> None to proceed, else an exit code (the refusal is printed)."""
    try:
        import gt_sandbox
    except ImportError:
        print("refused: gt_sandbox.py is not beside gt_settings.py; reinstall gt")
        return 1
    mode = value if name == "sandbox_mode" else (d.get("sandbox_mode") or "off")
    reads = value if name == "sandbox_vault_reads" else (d.get("sandbox_vault_reads") or "deny")
    try:
        if mode == "on":
            rep = gt_sandbox.apply(vault=d.get("vault_path"), reads=reads)
        elif name == "sandbox_mode":
            rep = gt_sandbox.remove()
        else:
            return None                          # reads changed while off: nothing to write
    except gt_sandbox.SandboxError as e:
        print("refused: %s" % e)
        return 1
    except OSError as e:
        print("refused: could not write the Claude Code settings (%s). Inside Claude Code's "
              "sandbox ~/.claude is write-protected by design: run this in a terminal." % e)
        return 1
    for c in rep.get("changes") or []:
        print("  " + c)
    for n in rep.get("notes") or []:
        print("  note: " + n)
    if rep.get("restart"):
        print("  Restart Claude Code for this to take effect.")
    return None


def _linux_package_hint(packages, os_release="/etc/os-release", which=None):
    """The install command for `packages` on THIS Linux, from /etc/os-release (ID, ID_LIKE),
    else from which package manager is on PATH. Usability run 2026-10-04: the hint said
    apt-get on a dnf machine."""
    import shutil
    which = which or shutil.which
    ids = []
    try:
        with open(os_release, "r", encoding="utf-8") as fh:
            for line in fh:
                k, _, v = line.strip().partition("=")
                if k in ("ID", "ID_LIKE"):
                    ids += v.strip().strip('"').lower().split()
    except OSError:
        pass
    managers = (("apt", ("debian", "ubuntu"), "sudo apt-get install %s"),
                ("dnf", ("fedora", "rhel", "centos"), "sudo dnf install %s"),
                ("pacman", ("arch",), "sudo pacman -S %s"),
                ("zypper", ("suse", "opensuse", "sles"), "sudo zypper install %s"))
    pk = " ".join(packages)
    for _name, family, cmd in managers:
        if any(i in family or any(i.startswith(f) for f in family) for i in ids):
            return cmd % pk
    for name, _family, cmd in managers:
        if which("apt-get" if name == "apt" else name):
            return cmd % pk
    return "install %s with your package manager" % pk


def _sandbox_prereqs_missing(name, value):
    """-> the refusal text when turning sandbox mode on (or changing its reads while it is
    on) cannot work on this Linux / WSL2 because bubblewrap or socat is missing, else None.
    Checked before any unlock confirmation is asked for."""
    d = _load()
    mode = value if name == "sandbox_mode" else (d.get("sandbox_mode") or "off")
    if mode != "on":
        return None
    try:
        import gt_sandbox
        pm = gt_sandbox.platform_mode()
        if pm not in ("linux", "wsl2"):
            return None
        missing = gt_sandbox.linux_deps_missing()
    except Exception:                            # noqa: BLE001 - _sandbox_switch reports it
        return None
    if not missing:
        return None
    pkgs = ["bubblewrap" if m == "bwrap" else m for m in missing]
    return ("refused: sandbox mode on %s needs %s, which %s not installed; Claude Code would "
            "refuse to start without it. Install it with:\n  %s\nthen run this again. Nothing "
            "was changed and no code was asked for."
            % ("WSL2" if pm == "wsl2" else "Linux", " and ".join(missing),
               "is" if len(missing) == 1 else "are", _linux_package_hint(pkgs)))


def _unlock_gate(name):
    """-> None when the change may proceed, else the refusal text. Fails closed when unlock is
    on and the authority cannot be asked."""
    try:
        import gt_unlock_policy
        if not gt_unlock_policy.enabled_fast():
            return None
        import gt_unlock_client
        ans = gt_unlock_client.tty_answer if sys.stdin and sys.stdin.isatty() else None
        v = gt_unlock_client.check("gt:settings:security", request=True,
                                   reason="change the gt setting %s" % name, answer=ans)
    except Exception as e:                       # noqa: BLE001
        return "gt unlock is on and the authority could not be asked (%s)" % type(e).__name__
    return None if v.get("allowed") else "%s (%s)" % (v.get("message"), v.get("code"))


def get(name):
    """Value with the registered default applied. For use by hooks and scripts."""
    spec = SETTINGS.get(name)
    if not spec:
        return None
    if name == "unlock":
        try:
            import gt_unlock_policy
            return "on" if gt_unlock_policy.enabled_fast() else "off"
        except Exception:                        # noqa: BLE001
            return spec["default"]
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
        # A phrase list, not a closed set (0.18.1); see its `detail`. One line, non-empty.
        return bool(value.strip()) and "\n" not in value
    if name == "push_fingerprint_repos":        # 0.20.3: absolute paths, comma-separated
        parts = [x.strip() for x in value.split(",") if x.strip()]
        return all(os.path.isabs(os.path.expanduser(x)) for x in parts) and "\n" not in value
    if name == "runners":                       # 0.18.1: ssh aliases, comma-separated
        return bool(re.fullmatch(r"[a-z0-9._@,-]*", value))
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
    # 0.18.1: a MEASURED profile (gt_bench.py) is kept across installs and upgrades while the
    # hardware is the same -- replacing a measurement with the rule of thumb it replaced would
    # undo it silently. A new machine or a core-count change still re-detects.
    if (old.get("source") == "measured" and not force
            and all(old.get(k) == fresh[k] for k in ("cores", "host"))):
        return old, False
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
    # The model profile lives with the install choices (install-choices.json), not here: one
    # source of truth, read for display (0.19.1, request model-and-effort-per-plugin).
    try:
        with open(os.path.expanduser("~/.claude/golden-thread/install-choices.json")) as fh:
            mc = (json.load(fh).get("model") or {})
    except Exception:
        mc = {}
    n_over = len(mc.get("skills") or {}) + len(mc.get("plugins") or {})
    print("  %-20s %s%s" % ("model_profile", mc.get("profile") or "inherit",
                            "" if mc.get("profile") else "   (none recorded)"))
    print("  %-20s %s" % ("", "the model and effort each skill runs at; %d override(s)" % n_over))
    print("  %-20s %s" % ("", "options: average | very-high | inherit   per skill: "
                              "gt_model_policy.py show / set / clear"))
    print()
    print("change with:  python3 gt_settings.py set <name> <value>")
    print("explain with: python3 gt_settings.py explain <name>")
    line = _lotr_summary()
    if line:
        print("\n%s  -- details: /gt:gt-settings lotr" % line)
    return 0


# ---------------------------------------------------------------- LOTR view (0.20.2) ----
# Owner, 2026-10-06: what is connected to LOTR and whether it works must be easy to find.
# gt core cannot import gt-lotr (a separate plugin), so this runs the INSTALLED lotr.py.

def _lotr_script():
    rec = os.path.join(os.path.expanduser("~"), ".claude", "plugins", "installed_plugins.json")
    try:
        with open(rec) as fh:
            e = (json.load(fh).get("plugins", {}).get("gt-lotr@golden-thread-plugin") or [])[0]
        path = os.path.join(str(e.get("installPath") or ""), "scripts", "lotr.py")
    except Exception:                                           # noqa: BLE001
        return None
    return path if os.path.isfile(path) else None


def _lotr_table(check=False, timeout=60):
    import subprocess                                           # noqa: PLC0415
    script = _lotr_script()
    if not script:
        return None
    # -X utf8, not PYTHONIOENCODING: -I ignores every PYTHON* variable, so on Windows the
    # child printed the table's ✓/✗ through cp1252 and crashed (found on gt-win11d, 0.20.2)
    args = ([sys.executable, "-I", "-X", "utf8", script, "status", "--table"]
            + (["--check"] if check else []))
    try:
        # read it back as UTF-8 too (Windows would decode the marks as cp1252)
        p = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "LOTR: the gateway did not answer (%s)\n" % type(e).__name__
    return p.stdout or p.stderr or "LOTR: no answer (exit %s)\n" % p.returncode


def _lotr_summary():
    """The one LOTR line plain `show` ends with; None when LOTR is not installed."""
    out = _lotr_table(timeout=20)          # a cold Python start on a busy Windows box can take >8 s
    if out is None:
        return None
    lines = [l for l in out.splitlines() if l.startswith("LOTR:")]
    return lines[-1] if lines else "LOTR: " + (out.strip().splitlines() or ["no answer"])[0][:120]


def lotr_view(check=False):
    out = _lotr_table(check=check)
    if out is None:
        print("LOTR is not installed on this machine (install gt with --with lotr).")
        return 0
    sys.stdout.write(out)
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
    # paths keep their case (0.20.3): lowercasing a repo path breaks it on a case-sensitive disk
    value = (value or "").strip() if name == "push_fingerprint_repos" else (value or "").strip().lower()
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
    if name in ("sandbox_mode", "sandbox_vault_reads"):
        # A sandbox that cannot run here is refused BEFORE gt unlock asks for codes (0.20.1,
        # finding M6: the bwrap check came after two TOTP codes, and suggested a --force this
        # command does not take). First the cheap check, which names what is missing and the
        # distro's install command and also covers a change to the reads setting while sandbox
        # mode is on; nothing is asked of anyone before it.
        missing = _sandbox_prereqs_missing(name, value)
        if missing:
            print(missing)
            return 1
    if name == "sandbox_mode" and value == "on":
        # Then the strict one: gt_sandbox.preflight really RUNS bwrap and socat, which catches
        # what a PATH lookup cannot (Ubuntu 24.04's AppArmor "uid map: Permission denied").
        # It runs second so that the answer of the cheap check never depends on the host.
        try:
            import gt_sandbox
            gt_sandbox.preflight()
        except ImportError:
            pass
        except gt_sandbox.SandboxError as e:
            print("refused: %s Nothing was changed and no code was asked for." % e)
            return 1
    # `unlock` itself is not gated here: `gt_unlock.py policy enable|disable` asks the
    # authority for K FRESH factors (step-up + K, ignoring any grant), which is stricter than
    # this gate -- asking both made one command need codes from two 30 s windows (M6).
    if name in SECURITY_KEYS and name != "unlock":
        refusal = _unlock_gate(name)
        if refusal:
            print("refused: changing %s needs a fresh confirmation (gt unlock): %s"
                  % (name, refusal))
            return 1
    if name in ("push_fingerprint", "commit_fingerprint"):
        # the guard does the work and reports it; the value is recorded only when it succeeded
        import subprocess
        guard = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "gt_push_guard.py" if name == "push_fingerprint" else "gt_sign.py")
        rc = subprocess.call([sys.executable, guard, value])
        if rc != 0:
            return rc
    if name == "unlock":
        import subprocess
        cli = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gt_unlock.py")
        return subprocess.call([sys.executable, cli, "policy",
                                "enable" if value == "on" else "disable"])
    d = _load()
    if not d.get("vault_path"):
        # Refuse to create a config that would leave the hooks unable to find the
        # vault -- writing a partial file here would break enforcement, not extend it.
        print("refusing to write %s: it has no vault_path. Run /gt:gt-init first."
              % CONFIG)
        return 2
    was = get(name)
    if name in ("sandbox_mode", "sandbox_vault_reads") and (was != value or name == "sandbox_mode"):
        rc = _sandbox_switch(name, value, dict(d, **{name: value}))
        if rc is not None:
            return rc
    if name == "lockdown":
        # the rules are written first; the value is recorded only when that succeeded
        try:
            import gt_lockdown
            rep = gt_lockdown.apply(value)
        except Exception as e:                   # noqa: BLE001
            print("refused: %s. Nothing was changed." % e)
            return 1
        for c in rep["changes"]:
            print("  " + c)
        if rep["changes"]:
            print("  settings.json backed up first; restart Claude Code for the change to apply")
    d[name] = value
    _save(d)
    print("%s: %s -> %s" % (name, was, value))
    if name == "agent_models" and was != value:
        # The stage agent definitions carry model and effort in their frontmatter (0.20.1), so
        # they follow the setting now, not at the next install. Only the agent files change.
        import subprocess
        mp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gt_model_policy.py")
        if os.path.isfile(mp):
            subprocess.call([sys.executable, mp, "apply", "--agents-only"])
    if spec.get("orphaned"):
        print("  note: the %s module is off, so this is kept but not in effect until "
              "install.sh --with %s" % (spec["module"], spec["module"]))
    return 0


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("show", "list"):
        return show()
    if a[0] == "lotr":
        return lotr_view(check="--check" in a[1:])
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
