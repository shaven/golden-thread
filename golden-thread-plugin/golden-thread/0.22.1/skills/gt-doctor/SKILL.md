---
name: gt-doctor
description: "Check this gt install in one command (version, drift, hook wiring, modules, migrations, stray workers, unpushed commits, lint) and name the gate that blocked a test run, with its fix. Use when the user says: is everything healthy, check my install, gt doctor, health check, is anything broken, why is a hook not firing, why is my test run blocked, why does it keep asking me to approve, why did my background agent die."
plain:
  - cmd: "/gt:gt-doctor"
    does: "Checks that your Golden Thread install is healthy."
    how: "Looks at the plugin version, hooks, modules, pending vault upgrades, stray workers and unpushed commits, and reports. It fixes nothing by itself."
    when: "Something seems off, or after an install or upgrade."
    next: "/gt:gt-upgrade, /gt:gt-settings"
    hear: "is everything healthy|check my install|gt doctor|health check"
  - cmd: "/gt:gt-doctor testing"
    does: "Finds out why a test run keeps stopping, and gives you the one fix."
    how: "Reads your Claude Code settings and the last refusal, prompt or provider error in your session, names the gate that fired (a missing permission rule, the folder fence, the edit mode, a Core rule, or a spend limit or login), and prints the command that clears it. It changes nothing until you run that command in a terminal and say yes."
    when: "A test or security-assessment run keeps asking you to approve things, or stopped or lost a background agent on its own."
    next: "/gt:gt-grant, /gt:gt-settings"
    hear: "why is my test run blocked|my tests keep stopping|why does it keep asking me to approve|why did my background agent die"
model_intent: fast
---

# Golden Thread Doctor

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_doctor.py …` | from the shell for the install rows; its vault rows need `vault_report` `{report: doctor}`, which runs every check (no `--only`) |
| `gt_doctor.py --fix`, `gt_doctor.py post-install` | **terminal**: they write `~/.claude/settings.json` and `~/.claude/golden-thread/` |
| `vault_init.py install-core-rules --vault …` | **terminal** |
| `gt_version_check.py`, `gt_code_review.py plan` | unchanged |
| `gt_doctor.py testing …` | from the shell: it reads the project's settings and Claude Code's session transcripts, never the vault |
| `gt_testing_doctor.py fix …` | **terminal**: it writes the project's `settings.local.json` and needs a person to say yes |
| `gt_testing_doctor.py off`, `gt_testing_profile.py off` | **terminal**: they take gt's changes out of the project's settings files |

A **terminal** row is a step sandbox mode refuses on purpose — it changes settings or install state, or rewrites the vault wholesale. Give the user the exact command to run from a terminal, and carry on with the rest.

One report covering every automatic check this plugin makes, plus the ones that only
ever ran at session start where a user could miss them.

## Steps

**Step 1 — Locate the vault.** Use `$GT_VAULT` if set; otherwise read `vault_path`
from `~/.claude/vault-config.json`. If neither gives a vault, the doctor still runs —
it reports `vault: no vault configured` and the other checks stand.

**Step 2 — Run it.**

```bash
python3 <base_dir>/../../scripts/gt_doctor.py
```

Add `--vault <path>` to check a vault other than the configured one, `--only <check>`
to run just one, and `--json` when another tool will read the result.

**Step 3 — Read the first line out loud.** It names the release every other answer is
relative to. A clean report from a check pinned to an old release looks exactly like a
healthy install — that is how 0.9.4 sat uninstalled beside a component check reporting
clean on 2026-08-30. Say the version, then the findings.

**Step 4 — Report each finding with its fix.** Every row that is not `ok` carries a
`fix:` line. Offer to run the safe ones; do not run them unasked.

**Step 5 — Offer `--fix` only for wiring.** `--fix` re-registers hooks that are
declared but missing from `settings.json`. It adds entries and removes none, so no
user configuration can be lost. Nothing else is repaired automatically: a pending
migration, an unpushed commit, a stray worker and a module whose plugin state
contradicts its `module.json` each need a person to decide.

## Why is my test run blocked? (testing)

Use this when the question is not "is gt healthy" but "why does my test run keep stopping".
An unattended run is stopped by one of several different gates that all look like "it stopped",
and each has a different cure. Do not guess which: ask the doctor.

```bash
python3 <base_dir>/../../scripts/gt_doctor.py testing --project-dir <the project being tested>
```

It is read-only and needs no terminal. It reads the live settings (user, project, local and
managed), then the newest session transcripts of that project for the last refusals, answered
prompts and API errors, and prints each blocker with the gate's name, **one plain sentence on what
that gate protects**, why it fired here, and the one fix with its exact command. `--json` for a
tool, `--transcript <file>` to read one session on purpose. Exit 0 nothing found, 1 a blocker found.

Say each blocker back in the user's words, in this order, and stop at what they can act on:

| Gate in the report | What to tell them |
|---|---|
| `read-fence`, `outside-working-dir` | The folder is not a working folder yet. Give them `/add-dir <folder>` for this session, or the printed `fix add-dir` command to keep it for the project. **Never** suggest turning `blockReadsOutsideWorkingDirectories` off: that fence protects every project. |
| `prompt-prefix-mismatch` | A rule exists but Claude Code matches the **start of the command text**, not the folder, so `node /abs/x.mjs` is not covered by `node --test`. Give the printed `fix allow` command and, when the report says so, the relative form Claude must run. |
| `prompt-missing-rule` | No rule covers it. Give the printed `fix allow`, or `fix profile` for the standard test runners. If the report offers no fix, the command is one gt will not pre-approve: say so, and point to `/gt:gt-grant` for one time or a while. |
| `prompt-rule-not-applied` | A rule covers it, so restart Claude Code (it reads settings at start) or an `ask` rule is overriding it. |
| `edit-mode` | Edits ask in the default mode. Offer `fix accept-edits`, or the narrower `fix profile --sandbox <folder>`. |
| `core-rule` | A Core-rule gate protects the vault or secrets. It is not a permission, there is no fix, and you must not look for one: follow the supported route its message names (for example `--vault <path>` or the write queue). |
| `classifier`, `safety-check`, `deny-rule`, `hook-block` | Not allow-rule problems. Say who refused; a deny rule is theirs to remove. |
| `provider-spend-cap`, `provider-rate-limit`, `provider-auth`, `provider-overloaded`, `provider-network` | Not their settings. Read out **what clears it and when it resets**; for a spend cap name the service that holds the limit. |

A fix is **one change to this project's `.claude/settings.local.json`**: it prints the diff, needs a
person at a terminal and one yes, is backed up, and is recorded so it can be undone. Claude cannot
run it (an agent's shell has no terminal) and must not try to route around that. Hand over the
printed command and let them run it:

```bash
python3 <base_dir>/../../scripts/gt_testing_doctor.py fix add-dir <folder> --project-dir <the project>
python3 <base_dir>/../../scripts/gt_testing_doctor.py off --project-dir <the project>
```

`fix` takes `add-dir <folder>`, `allow <command prefix>`, `accept-edits` and `profile`; `--dry-run`
shows the diff and writes nothing; `--until 4h` makes it end by itself. `off` takes out everything gt
added to this project's settings files, restores `defaultMode`, and prints the diff; the same
teardown is `gt_testing_profile.py off`. After a fix Claude Code must be restarted to read it.

What it never does, and you must not either: lower the working-directory fence, edit a hook, touch
the write queue, the secrets checks or the explicit-vault-target check, or make a fix in the vault
(a project that is the vault is refused). A denied command is shown only as its first few words with
anything secret-looking hidden; do not go looking for the rest.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | all clear — every check ran and none of them is `WARN` or `FAIL` |
| 1 | something needs attention — at least one `WARN` or `FAIL` |
| 2 | a check could not run (`?`) and nothing outright failed — **not** the same as clean |

Exit 2 matters: "could not check" reported as "clean" is the failure this whole tier
exists to prevent. It is the state of the **worst** row, ranked `FAIL`, then `?`, then
`WARN` — so a run with both a failure and a check that could not run exits **1**, and
the `?` row is still sitting there unanswered. Never read exit 1 or 0 as "everything
was checked": read the marks. Any `?` row means that question has no answer yet — say
which check could not run, and why, before summarising anything else.

A `-` row is neither: it is a check that does not apply to this machine because nothing
configured it (no publish destination, for instance). It never raises the exit code,
and it is not "clean" either.

## What each check answers

| Check | Question |
|---|---|
| `version` | is the newest release the one installed? |
| `components` | do the installed files match that release? |
| `wiring` | is every hook the release declares actually in `settings.json`? |
| `core-rules` | does each Core rule that claims a mechanism actually have THAT mechanism wired? |
| `modules` | which modules are on or off and why, does each admit this gt release, and is every **on** module's plugin both registered in `installed_plugins.json` and enabled in `settings.json`? (read-only) |
| `model-policy` | which model profile is active, and does every installed skill still carry exactly the `model:`/`effort:` the policy wrote? A hand edit is a WARN with the fix (0.19.1) |
| `vault` | is the vault reachable, and are `log.md`/`decisions.md` migrated? |
| `workers` | are background processes running that nobody declared? |
| `push` | do this machine's commits exist anywhere else? |
| publish destination | does the publish destination still hold only what was published? |
| `lint` | what does the vault linter say, in one line? |
| `repo-target` | which git repo does this working directory resolve to, and is it the vault? Always an `i` (note) row: when it is the vault, repo-scoped commands such as `/security-review` and `/code-review` review the vault, not your code — point them at the code repo, or use `gt_code_review.py plan <repo>`, which takes the root explicitly |
| `hooks-schema` | does every `settings.json` hook entry name an event Claude Code fires (`hooks/known_events.json`) and, on tool events, a tool it knows (`hooks/known_tools.json`)? Flags `hooks-unknown-event`, `hooks-unknown-tool` and `hooks-missing-file` (an entry pointing at a gt hook file that no longer exists) as WARN — an unknown name prompts review, it is not proof of breakage |
| `context-reading` | can `context_nudge` and `auto_compact` act here? Always a note: no gt-usage status line means no context reading; `auto_compact` is terminal-only, needs the Stop payload's `background_tasks` (confirmed in 2.1.300), and `/gt:gt-minimize compact` needs 2.1.296+ (0.21.4); once cutting is opted in it names what a cut is (`cut_kind`, `cut_handoff`; 0.22.0) |

Those rows plus `schedule` and `astgrep` are the whole list — `--only` accepts exactly these names.

An `i` row is a **note**: orientation the operator needs, neither healthy nor broken. It
never raises the exit code and never prints `ok`.

**`core-rules` is not the same question as `wiring`, and the difference is the point.**
`wiring` asks whether every hook the RELEASE declares is present in `settings.json`.
`core-rules` starts from the other end: it reads each `level: core` rule in the vault and
checks that the specific script implementing its declared `enforcement` is wired — a
`reminder` rule needs a `UserPromptSubmit` hook running `inject_core_rules.sh`, a
`validated` rule needs a `Stop` hook running `validate_response.sh` — and that the script
exists on disk. It tolerates extra arguments and is strict about which script.

It was added in 0.16.5 because **nothing verified this**. The docstring had advertised the
check for releases while `CHECKS` did not contain it, and the one place that looked —
`gt_lint`'s `core-unenforced` — only asked whether SOME hook occupied the event, so a vault
with any third-party `UserPromptSubmit` hook and `inject_core_rules.sh` absent reported the
Core tier enforced. Both are fixed; this is the check that answers the question directly.

A failure here means a rule is **stored but not enforced** — present in the vault, read by
every session, and backed by nothing. The fix line is
`vault_init.py install-core-rules --vault <vault>` — named explicitly, per Core rule 2,
because a mutating tool must never infer its target.

Scope: it looks only inside `core-rules/`. A `level: core` file somewhere else is
`gt_lint`'s `core-misplaced`. The `enforcement` vocabulary is only `reminder` and
`validated`, so the `PreToolUse` guards some rules describe in prose are covered by
`wiring`, not here.

`modules` is read-only and never repairs anything. A module that is **off** is a choice
and is reported as "not installed by choice", never as drift — what it flags is a
contradiction: an off module whose plugin is still installed or enabled, an on module
whose plugin is missing, an invalid `module.json`, or a module that does not admit the
running gt release.

## The post-install gate and its receipt

`gt_doctor.py post-install --vault <vault>` is the release gate: a PASS/FAIL table, exit 1 on
any FAIL. Every **completed** run at the `session` or `final` stage records its verdict in
`~/.claude/golden-thread/post-install-validated.json` — the release version, `failed`, the
counts per state, the stage, the time, and `writer`, which says who ran it:

| `writer` | Who |
|---|---|
| `hook` | the SessionStart hook (`gt_version_check.py`), once per newly installed version |
| `manual` | a person or a session running the command (the default) |

Nothing is written by a gate that could not run (a crash is not a verdict, and the previous
receipt stays as it was), by `install.sh`'s `--stage install` run (vault rows are PENDING there
by design; the hook validates on the next session), or by `--dry-run`. Since 0.19.1 the
doctor is the receipt's only writer; before that only the hook wrote it, so a passing manual
run left the previous version's receipt in place.

Since 0.21.4 the gate has a `plugin-files` row: every plugin cache (gt and each enabled module) is
checked as the `install.sh --only` self-proof checks it -- nothing missing, changed or left over, no
other cached version. With no source tree to compare against it is a WARN.

## The `logs` row (0.22.0)

The report ends with one row listing every audit and log file gt keeps (unlock audit, LOTR audit, gateway audit, the write-queue log, the savings ledger, project events, and gt's own log), with each file's size and last write; it never reads their contents. gt's own log holds only fixed-shape records (time, component, level, event name, whole-number counts, error code): read it with `python3 <scripts>/gt_log.py tail`, search it with `search TEXT`, and hand a clean copy to someone helping you with `gt_log.py export --redact`. How much it records is the `log_level` setting (default `error`; `off` writes nothing).
