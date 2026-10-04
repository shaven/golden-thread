# Build note — gt 0.20.0

**Read this first if you are the person taking this tree into the other repository.**

> **This file does not travel in gt-src** (owner ruling, 2026-10-01). gt-src carries only the code,
> what `install.sh` installs, and its own tools (`copygt.sh`, `validate-install.py`, `SHA256SUMS`,
> `SOURCE.json`). On the receiving machine the report `copygt.sh` writes replaces this note; its
> content reaches you from the owner. `SOURCE.json`, at the root of gt-src, names the exact commit
> the tree was cut from.

> **Not released yet.** 0.20.0 = the Windows completion (built as 0.19.3, never released) plus
> gt unlock (security, off by default; `SECURITY.md`) plus the Claude Code integration items 0–4
> and 6 of `plan-0.20.0-claude-code-integration.md` (stage plugin agents with model and effort,
> the `gt:pipeline-stage` workflow, LOTR `outputSchema`, gt-lotr's MCP server on Windows, the
> `--dry-run` placement fix; no skill forks). Built on `feat/0.20.0` from `8078835`.
> Nothing is pushed, tagged or synced to gt-src until the owner says release; then it goes straight
> to main. The table below is 0.19.1's, with 0.19.2 and 0.20.0 on top. There is no published 0.19.3.

---

## 1. Versions

| Plugin | Version | Note |
|---|---|---|
| gt (core) | **0.20.0** | 0.20.0: gt unlock (an authority daemon, TOTP + Touch ID / Windows Hello / Entra ID factors, sealed and brokered credentials, gated settings and publishing, folder locks, `verify` and doctor rows `unlock`/`security`; off by default) + the Windows completion: Windows completed (a `python3` shim for the model's shell, LF for every gt write, scheduled jobs on Task Scheduler, the test suite runs on Windows), installer rollback on failure, the scheduled jobs' interpreter chosen and proven rather than overwritten. 0.19.2: native Windows install (`install.cmd`, interpreter resolution, hooks under Git Bash, `/` manifest keys, LF vault writes, UTF-8 output). 0.19.1: model and effort profiles, recall benchmark, prompt hints (off), supersession and expiry at read time, queue-guard tokens, no bytecode in gt-src, post-install release resolution and receipt, one launchd interpreter, commit-gate timeout, Stop-validator install fix (no published 0.19.0) |
| gt-wiki | **0.2.7** | `requires_gt >=0.20.0,<0.21.0` only |
| gt-demo, gt-farm, gt-flow, gt-report-card, gt-watch | **0.20.0** | they move with gt |
| gt-visualize | **0.4.4** | `requires_gt >=0.20.0,<0.21.0` only |
| gt-usage | **0.1.6** | `requires_gt >=0.20.0,<0.21.0` only |
| gt-lotr | **0.3.0** | consumes gt unlock (grant check per call, grant ids in the audit, `mcp_only`, biometric consent option, `sealed:`/BYO secret refs); a Windows named-pipe front door; `outputSchema` on its four tools (MCP 2025-06-18+); on Windows the installer starts its MCP server with the resolved Python; still off by default |

Skills stay **36** (every one now declares a `model_intent`). gt_lint checks **24 → 25**
(`supersedes-missing`). Settings: two new (`vault_hints`, `allin_timeout`); the model profile
lives with the install choices. Doctor checks **16 → 17** (`model-policy`). Hook registrations
**16 → 17** (`vault_hints.py`, `install.sh`-owned). New scripts: `gt_model_policy.py`,
`gt_keyword_recall.py`, `gt_supersede.py`; `gt_bench.py recall`. 0.18.1 is the rollback target.

## 2. What changed, and why

The full account, one section per theme with the why, is `../CHANGELOG.md` (it travels in gt-src).
The short version:

- **gt sandbox mode (0.20.0, off by default):** `gt_settings.py set sandbox_mode on` writes Claude
  Code's sandbox + permission rules into `~/.claude/settings.json` (`gt_sandbox.py`, recorded and
  cleanly removable); the vault is reached through the new `gt-vault` MCP server
  (`gt_vault_mcp.py`, in gt's plugin manifest) and the write queue, with `~/.gt-inbox` as the one
  writable place. Native Windows: permission rules only. SECURITY.md §8.

- **One verb per action** (owner accepted 2026-10-01): `gt-create`, `gt-open`, `gt-list`,
  `gt-handle`, `gt-close`, each taking `project` / `task` / `handoff`. `gt-close project` refuses
  until every open task and handoff is decided, offers graduation, then **archives in place**
  (`stage: archived`); `--move` relocates to `Archive/<slug>/` only on request. New vault-tool
  verbs `gt_task.py shelve` and `move`.
- **Coding loop:** `gt-plan` (plan file `.claude/gt-plan-current.md`, waits for approval) and
  `gt-implement` (test-first phases, stops on red, commits only on yes). **gt-learn is folded into
  gt-work** as a *Learn* step (owner decision).
- **Decisions:** ADR fields `Supersedes`, `Expires when`, `Expires`; `gt_adr.py lineage`;
  `/gt:gt-query --lineage` and `--entity`; sub-project slugs resolve to their folder in `gt_adr`
  and `create-project --parent`; `gt-brief` drafts a repo `CLAUDE.md`.
- **Lint:** `adr-expires`, `bundled-concept`, `decision-candidate`, `memory-entity-orphan` — all
  review-queue questions.
- **Subtraction:** `gt-optimize` is an aggregator (`vault` + `session` members), with `--cost`,
  `--archive`, `--supersede`, `single-project-global`, `knowledge-unused`; a new
  `log_knowledge_read.sh` hook logs Knowledge reads to `<vault>/usage/`. `gt-minimize` prunes and
  writes **no in-flight note** (owner decision).
- **gt-work checks itself:** contradictions, promotion candidates, `research-digest.md`; the
  skeptic pass no longer needs `agent_specialization`.
- **Returning:** `gt_catchup.py` brief in gt-open; *What Changed This Session* in the handoff;
  `gt_state` speaks to the user via `systemMessage` and reads only its own session's ledger rows.
- **Install health and guards:** doctor rows `repo-target` (a note, `i`) and `hooks-schema`;
  `guard_foreign_checkout.sh` denies commit/push in a declared foreign checkout.
- **Checks modules contribute:** `gt_check.py` hosts module checkers (`checkers` key in
  `module.json`; `cannot-check` never passes; `commit_checks` gates commits, off by default);
  `gt_apply.py` is the only writer of their fixes (`addon_fixes`, hardened: CAS write, re-check by
  every checker, content rules, first-party-only auto-apply). New event kind `addon.fix`.
- **Model intent:** `model_intent: fast|balanced|deep`, resolved by `gt_model.py` through the new
  `model` pack slot (`packs/core/model.intents.pack.json`); the session member's price table moved
  to `scripts/gt_model_prices.json`, so no script names a model.
- **Across machines and outside sessions:** `gt-sync` (fast-forward only; `sync_check` off by
  default); `gt_reminder.py` with macOS, relay (SMS/Discord) and email channels, all off by
  default, and a `reminder` scheduled job that reads a mirror, never the vault.
- **Resumable batches:** `gt_checkpoint.py`; `gt_scan.py --resume`, `gt_ingest.py --resume/--done`.
- **Link suggestions** after a Knowledge write; **review stamps** (`last_reviewed`) from gt-query
  and gt-open.
- **Every slug-taking tool** resolves sub-projects through `gt_spool.resolve_project`;
  `gt_memory_check.py` with an unknown slug now exits 2 (was a silent 0).
- **Hook allowlists** checked against Claude Code's hooks and tools docs: 33 events, 48 tools.
- **Release pipeline:** `gt_pipeline.py` (`release-pipeline.tsv` + generated `release.sh`),
  README flag `release_pipeline:`, lint check `release-pipeline`, gt-upgrade migration
  `release-pipeline-flag`, gt-allin `pipeline` member.
- **Fast build loop and measurement:** `gt_recipe.py` (and `dev/copygt.sh` now generated from
  `dev/copygt.recipe`); `tests/run.sh --affected` scoped receipts on feature branches
  (`scoped_receipts`); cached test install; `gt_load.py` load-aware workers; `prun.py --hosts` and
  the `runners` setting; `gt_bench.py`; `gt_metrics.py` (`execution_metrics`); gt-optimize's opt-in
  `execution` member; doctor `execution` row; `test_tmpdir`.
- **Staged ingest and promote:** `gt_ingest_pipeline.py`, stage × kind agent specs; gt-work is the
  session kind; the skeptic stays decided by `skeptic_pass` alone.
- **Repository tooling:** `dev/remote-test.sh` (the suite on a remote Linux runner),
  `copygt.sh`, `dev/feature_requests.py --src` descends into `golden-thread-plugin/`.

## 3. What to run on the receiving machine

**One command, from gt-src:**

```bash
cd <gt-src>
./copygt.sh --dest <repo> --dry-run   # list every add, change and delete; writes nothing
./copygt.sh --dest <repo>             # verify, mirror, install, validate; commit only if clean
```

It does, in order, and stops at the first failure:

1. **Verify gt-src.** Every file is checked against `SHA256SUMS`, and `SHA256SUMS` against
   `SOURCE.json` `tree_sha256`, before anything is touched. A mismatch exits **3**, and nothing is
   written.
2. **Mirror onto `<repo>` exactly.** It adds, updates and **deletes** every tracked file gt-src no
   longer carries. `.git` and untracked local files are left alone.
   - It refuses a repo with uncommitted changes to tracked files.
   - It never writes `copygt.sh`, `validate-install.py`, `SHA256SUMS`, `SOURCE.json` or the report
     into the repo.
   - **First run after 0.17.11:** it deletes the five files that no longer ship
     (`*.code-workspace`, `BUILD-NOTE.md`, both `CLAUDE.md`, `dev/`, `SUBMISSIONS.md`).
   - **It also deletes release directories older than the previous one**, because gt-src carries
     only the newest two of each plugin. The dry run lists them, and git history keeps them.
   - The applied gt-src commit is recorded in `~/.claude/golden-thread/copygt/applied.json`.
3. **`install.sh`** runs from the repo. Its own post-install gate runs at the end, as before.
4. **Validate** (`validate-install.py`). It runs `gt_doctor.py post-install` at its `final` stage
   and checks that every declared plugin and skill landed. The report goes to
   `~/.claude/golden-thread/copygt/report-<time>.md` (`.json` beside it, install log beside it) and
   has four sections:
   - **PRESENT:** plugins, versions, skills, hooks wired, Core rules;
   - **MISSING:** anything declared but not installed;
   - **WORKED:** each check that passed;
   - **FAILED:** each check that failed **or could not run**.

   It ends with **`clean: N/N`**, where N counts what the tree DECLARED: copygt's own steps, two
   checks per plugin this machine installs, and every row the release's gate can emit.
5. **Commit, only when clean. It never pushes.** Pushing is outside copygt.sh, and by ADR-15 only
   the publishing Mac pushes to GitHub. The commit message names the gt-src commit and every
   plugin version. Anything not clean exits **4** before the commit and leaves the report. The
   copied files stay in the work tree for you to inspect.

Options: `--vault V`, `--report FILE` (never inside the repo).
Exit codes: 0 done, 2 refused, 3 checksum, 4 not clean, 5 commit failed. (`--no-push` is still accepted and changes nothing.)

**The tree_sha256 to compare is the one `sync-gt-src.sh` printed at the 0.18.1 sync** — not a
number written here, since this note is written before the cut. `copygt.sh` prints the value it
verified.

**The same Dropbox-synced vault?** If the receiving machine opens the same vault as the publishing
Mac, that vault will already have been upgraded to 0.18.1 here, `PROTOCOL.md` merged, and the merge
base recorded. `/gt:gt-upgrade` there should report nothing pending. **Never redo the
merge.**

**Settings are per machine.** `release_announce=post`, `skeptic_pass=on` and
`agent_specialization=on` were set on the publishing Mac only. The receiving machine keeps its own
settings; see `/gt:gt-settings`.

### 3a. By hand: the fallback when copygt.sh cannot be used

A hand copy must do what copygt.sh does:
- **Delete** every tracked file in the repo that gt-src no longer carries. A copy that only adds
  is how `Golden Thread.code-workspace` outlived its removal.
- Leave out `copygt.sh`, `validate-install.py`, `SHA256SUMS` and `SOURCE.json`. They are
  gt-src-only.

The steps below install **from gt-src itself**, which is the one place `SHA256SUMS` sits beside the
tree. The repo never holds `SHA256SUMS`, so `install.sh` run from the repo reports "not a
published gt-src", and `--require-checksum` would refuse there.

**The checksum check is automatic — and on this machine, make it strict.** gt-src ships
`SHA256SUMS` (a sha256 per file, by path) and `SOURCE.json` carries `tree_sha256` — the sha256 of
`SHA256SUMS`, one value for the whole release. Both are computed from the commit, not from the
copy. `install.sh` checks every listed file before it copies anything. By default a mismatch is
named and the install continues (so a stranger's download is never blocked); **here, run it with
`--require-checksum`**, which stops with **exit 8**, naming the file, if one is missing, changed,
or not where the list puts it. On success it prints
`checksum: all N published files present, in place and unchanged (tree_sha256 …)`.
**Compare that tree_sha256 with the one the publishing machine printed** — that is the check
that you have the newest release, not just an intact older one. If it fails: copy gt-src again,
whole, onto the repository root.

To check by hand without installing:

```bash
shasum -a 256 -c --quiet SHA256SUMS   # Linux: sha256sum -c --quiet SHA256SUMS
shasum -a 256 SHA256SUMS              # = "tree_sha256" in SOURCE.json
```

A file present but not in `SHA256SUMS` is not refused — the receiving repository has its own
(`.git`, local notes) — but `install.sh` names up to ten of them.

Then:

```bash
cat SOURCE.json                 # the commit this tree was cut from, and every plugin version
cd golden-thread-plugin
bash install.sh --require-checksum   # verifies every file, then installs NEWEST
#   then RESTART Claude Code: plugins and hooks load at session start.
./selftest.sh
```

**Post-install release gate.** It runs automatically, and you can run it by hand:

```bash
python3 ~/.claude/golden-thread/hooks/gt_doctor.py post-install --vault <vault>
```

It proves an installed machine is correct for the release: a PASS/FAIL/WARN/INFO/PENDING table,
exit 1 on any FAIL, a row that cannot run is a FAIL. `install.sh` runs it at the end (a real FAIL
exits **9**; rows needing `/gt:gt-upgrade` show PENDING), and the first SessionStart after a
version change runs it once more. It never writes the real vault or `settings.json`.

Then, in a Claude Code session on that machine:

0. Read the post-install table `install.sh` printed: every row PASS, or PENDING with its fix.
1. **`/gt:gt-upgrade`** — merges the PROTOCOL.md section *ADR fields and memory entities* and the
   gt-brief paragraph under *Graduating a fact out to a repo*, and refreshes the vault tools
   (`gt_adr.py`, `gt_session.py`, `gt_spool.py`, `gt_task.py` changed). Until it runs, `gt_task.py
   move`/`shelve`, `gt_adr.py lineage` and `start_commit` do not exist in the vault.
2. **`/gt:gt-doctor`** — expect two new rows: `repo-target` (an `i` note, normal) and
   `hooks-schema`. `guard_foreign_checkout.sh` and `log_knowledge_read.sh` show `unwired` until
   `install.sh` has run.
3. **Declare any foreign checkout** on this machine (see §5).
3c. **Optional: measure this machine's parallel profile** — `python3 <scripts>/gt_bench.py --dry-run`
    to see it, then without `--dry-run` to write it (about 3 minutes; it never touches other
    processes). `/gt:gt-doctor`'s `execution` row says whether the profile is measured, and warns
    if the shell runs under Rosetta.
3a. **Reminders, if wanted on this machine** (all channels are off until you do this):
    1. `python3 ~/.claude/golden-thread/hooks/gt_reminder.py setup macos|relay|email` — read the
       channel's setup.
    2. Relay or email: have the secrets store write `~/.claude/golden-thread/reminder/relay.json`
       or `email.json`, **mode 600** (never paste a credential into a session).
    3. `gt_settings.py set reminder_macos on` (or `reminder_relay sms|discord`,
       `reminder_email on`); optionally `reminder_days`.
    4. `gt_reminder.py check <channel>` — sends a real test; must say DELIVERED. For macOS,
       allow Script Editor's notifications first.
    5. `gt_schedule.py install reminder --vault <vault>` — writes the mirror, installs the 08:30
       job and proves it through launchd (a real reminder is sent if anything is due). This is the
       only proof through launchd; the tests stop at `launchctl`.
    6. Retiring the staged `Projects/secrets-management/deadline-reminder/` waits until one real
       reminder has arrived. Its list can be folded in with `gt_reminder.py import-tsv --only
       "Ladder decision"` — the other row is an older wording of rows `deadlines.md` already has.
3b. **Two machines on one vault:** `gt_settings.py set sync_check cached` for a free "vault is
    behind" line at session start; `/gt:gt-sync pull` before work, `push` after `/gt:gt-work`.
4. **Run the gate again** and drain anything waiting:
   `python3 ~/.claude/golden-thread/hooks/gt_broker.py drain --vault <vault>`.

## 4. How to work from now on (both machines)

- **Use the verbs.** `/gt:gt-create task …`, `/gt:gt-list tasks mine`, `/gt:gt-handle handoff`,
  `/gt:gt-close project <slug>`. The old names print a one-line notice and still work through
  0.18.x; they are removed in the release after.
- **Before stepping away from a long session:** `/gt:gt-minimize`, then `/compact` while warm.
- **Coding work:** `/gt:gt-plan`, approve, `/gt:gt-implement`. Nothing is committed without a yes,
  nothing is pushed.
- **Vault writes are queue-first, as in 0.17.11** — unchanged: queue it, then `gt_broker.py drain`.

## 5. What needs a decision

- **Declare foreign checkouts.** The guard is inert until a checkout is declared. On each machine,
  declare the checkouts the *other* machine owns:
  `python3 ~/.claude/golden-thread/hooks/guard_foreign_checkout.py add <path> --label "<owner>"
  --route "<the supported route>"`.
- **First-run lint volume.** On the owner's vault (read-only, 2026-10-01) `decision-candidate`
  found 114 (92 of them "deliberately") and `bundled-concept` 38 pages. Decide whether to keep the
  request's phrase list or `gt_settings.py set decision_signals "-deliberately"` (→ 22), and
  suppress narrative pages as they come up.
- **Pinning research findings.** gt-work allows adding `[pinned]` to an existing `##` heading in
  `research.md` — a narrow exception to append-only that PROTOCOL/CONVENTIONS do not yet word.
- **gt-open's project steps still name the old skills** — kept deliberately for the alias parity
  test; every other shipped text now names `gt-create handoff` / `gt-handle handoff`. Sweep them
  when the aliases are removed.
- **`commit_checks` and `addon_fixes apply`** are off / propose by default and no checkers ship;
  turning them on means nothing until a checker module is installed.
- **The five 0.17.10 agent spec files** (`templates/agent-specs/ingest-code`, `ingest-docs`,
  `ingest-tool`, `validate`, `skeptic`) are still shipped and ignored by the loader. Delete them, or
  keep them for the alias release.
- **`gt_secrets.py` flags `scripts/gt_metrics.py:451`** (a `tokens=` keyword argument) on the Mac —
  a false positive that blocks a local full-run receipt until you baseline it. Not reshaped.
- **Should gt's own repo adopt a release pipeline?** It would put `release.sh` and
  `release-pipeline.tsv` at the repo root; `dev/publish.sh` remains the release sequence for now.
- **gt sandbox mode defaults:** `sandbox_vault_reads deny` makes gt's own vault scripts
  unusable from the sandboxed shell (the MCP tools replace them); `allow` is the gentler option.
  The vault MCP process starts in every session even with the mode off (it then lists no tools).
- **When to release:** the owner says when. Then push straight to main, with no PR.

## 6. What will be misread if nobody says it

- **`agent type: none` is not an error.** `gt_agent_spec.py model <job>` names `gt:<stage>`
  only when the installed definition is exactly what the job should run; after a job-type
  override, a vault override of a stage, a hand edit, or on a Claude Code older than 2.1.78 it
  says why and the skill spawns the 0.19 way. A session started before `gt_model_policy.py apply`
  or an `agent_models` change keeps the definitions it loaded until it is restarted.
- **The installed gt-lotr `plugin.json` differs from the release on Windows, by design**: its
  MCP command is the resolved interpreter, not `python3`. Since sandbox mode the same holds for
  gt's own `plugin.json` (its `gt-vault` server).
- **`gt-vault` shows in `/mcp` with no tools** while sandbox mode is off — `vault_mcp auto`, by
  design, not a failure.

- **`repo-target` is never a problem.** It is an `i` row on every run where the working directory is
  the vault — that is gt's normal configuration. It exists so a code review is pointed at the
  right repo, not to be fixed.
- **A bare `gt_optimize.py` exits 3 on a machine with no Claude transcripts** (a CI runner): the
  session member could not run, and the aggregator says so rather than reporting clean.
  `gt_allin` runs it as `--only vault`.
- **`knowledge-unused` says nothing at first.** With no read log yet it reports nothing, by
  design; findings start appearing as reads accumulate.
- **`memory-entity-orphan` reports 0 until a project adopts `entities:`** — the adoption gate, not a
  broken check.
- **The first prompt of a session usually shows no `gt-state` line**: there is no usage reading of
  this session's own yet, so it "cannot tell". That is correct, not a regression.
- **The guard's Bash false positive is still there.** `guard_session_claims` matches `>` inside
  quoted strings and heredocs (e.g. `<YYYY>.md` in a Python heredoc) and can deny a harmless
  command as a vault write. Write such text with the Write tool to a file outside the vault, or put
  the script in a file and run it.
- **gt-lint's `release-pipeline` finds every project without the key** until `/gt:gt-upgrade` runs
  its `release-pipeline-flag` migration, which records `planned`.
- **A scoped receipt is not a release receipt.** `tests/run.sh --affected` lets a feature-branch
  commit through; the default branch, `release-check` and `publish` still need a full-suite run.
- **A bare `gt_optimize.py` still runs two members**; `execution` is opt-in (`--only execution`).
- **Execution metrics are partial:** no token cost per workflow yet, no before/after 0.17.11
  workflow timing, `gt_bench` calibrates on a synthetic workload, no weekly execution worker.
- **Not flakes after all:** the intermittent `test_package` / `test_install_vault_upgrade`
  failures were a SIGPIPE race in `printf | grep -q` under `pipefail` (fixed; see CHANGELOG).
  A failure in either is now a real failure.
- **`gt_check.py run` exit 3 is "nothing applied", not clean** — with no checker modules installed,
  every run says so.
- **A vault whose `tools/gt_events.py` predates 0.18.1 refuses an `addon.fix` event** until
  `/gt:gt-upgrade` refreshes the vault tools.
- **Reminder "DELIVERED" for macOS** means `osascript` exited 0; if Script Editor's notifications are
  off, nothing appears.
- **Run the suite on the remote runner, not the Mac** (`GT_TEST_VERSION=0.18.1 bash
  dev/remote-test.sh -j 2 …`). A local full run drove the Mac's load to 50–96.
