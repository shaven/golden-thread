# Build note — gt 0.17.11

**Read this first if you are the person taking this tree into the other repository.**

This file travels with the publish. `SOURCE.json`, at the root of gt-src, names the exact commit
this tree was cut from.

> **gt-src changed shape in 0.17.3 — read this before copying.** It now mirrors the GitHub
> repository's layout: the repo root (`README.md`, `CHANGELOG.md`, `LICENSE`, `CLAUDE.md`,
> `SUBMISSIONS.md`, `docs/`, `.github/`) with the plugin under `golden-thread-plugin/` —
> exactly where GitHub holds each file, minus release directories older than the previous one.
> Until 0.17.2 gt-src was the plugin folder alone, so a copy step written for that shape will put
> files one level too high. **Copy gt-src as a whole onto the repository root.** The CHANGELOG
> now travels too, so this note no longer has to stand in for it.


---

## 1. Versions

| Plugin | Version | Note |
|---|---|---|
| gt (core) | **0.17.11** | Core rule 1 revised; queue ops; scripts routed through the queue; gt_daily headings fix |
| gt-wiki | **0.2.4** | skills write through the queue; `wiki_log.py` uses `gt_log.py` and the queue in a gt vault |
| gt-farm, gt-watch, gt-demo | **0.17.11** | skills write through the queue; demo act 4 and demo ADR numbering fixed |
| gt-flow, gt-report-card | **0.17.11** | version bump only (they move with gt) |
| gt-visualize | **0.4.1** | wording only |
| gt-usage | 0.1.3 | unchanged |
| **gt-lotr** | **0.1.0** | **new optional module, off by default**: LOTR (also called gt MCP), one gateway to rule them all |

**LOTR (`gt-lotr` 0.1.0)** is a fixed four-tool MCP surface (`find`, `call_read`, `call_write`,
`call_consent`) in front of any number of downstream connections (GitHub, Jira, Microsoft Graph
and other REST APIs). It has a registry of who talks to whom, as whom, and from which machine.

- **Install:** `./install.sh --with lotr`. Nothing changes until then.
- **Runtime:** stdlib-only Python 3.9 or later, so the same code runs on a Mac and on a Linux hub.
- **Commands:** `lotr` (the same CLI is also called `mcp`), `lotrd` (the daemon) and `lotr_mcp.py`
  (the stdio shim).
- **Placement** is per zone: `local` (one box, e.g. a work machine) or `hub` with enrolled
  `client` machines; hybrid is not built yet.
- **Local security:**
  - private files;
  - a kernel peer-uid check on the socket;
  - TLS required beyond loopback;
  - local callers under an allow list and a tier ceiling;
  - consent-tier operations confirmed in a dialog raised by the daemon itself.
- **Credentials** are references (keychain, store, file), never values.
- **On the work machine:** connections there use that machine's own keychain. Employer hostnames
  stay in that machine's local registry, never in the vault.
- **Design and decisions:** vault `Projects/golden-thread/mcp-gateway/`, ADR-1..6.

## 2. What changed, and why

**Core rule 1 is now "queue first"** (owner, 2026-10-01: *"the agents write to the queues rather
than directly to the files"*). The rule id is unchanged (`core_concurrent_session_claim`); its
meaning has changed:

> Write vault content only through the write queue (`gt_write_queue.py`), then apply it with
> `gt_broker.py drain`; never edit a vault file directly, and never write one another live session
> has claimed.

- **The guard enforces it** (`guard_session_claims`).
  - **Denied:** a direct Write/Edit to any vault `.md`, claimed or not, outside `Sources/`,
    `core-rules/`, `.obsidian/`, `.git/`, `.gt/` and gt's own `spool/`, `sessions/` and `tools/`.
  - **Also denied:** the visible shell writes: `>`, `>>`, `tee`, `sed -i`, and `cp`/`mv` into the
    vault.
  - **Not visible to it:** a script that opens a file itself. The rule says so.
- **Queue ops**: `append`, `replace-section`, `create`, plus two new ones:
  - `set-property`: one top-level frontmatter key;
  - `replace-file`: the whole file, guarded by the hash of what was read.

  The broker escalates instead of overwriting when a target changed after the request was
  queued. It also escalates when a target was moved or deleted since; it never recreates a file
  at its old path.
- **Every `design.md` and `global-memory/` write goes to the owner for review.** This is the
  owner's choice.
- **Skills:** gt-work and 15 other core skills, plus the module skills, now queue their writes and
  drain once.
- **Scripts now route their vault writes through the queue:** `gt_daily`, `gt_task`,
  `gt_lint_weekly`, `gt_handoff`, `gt_handoff_status` and `wiki_log`.
- **Hooks dir:** `gt_write_queue.py`, `gt_broker.py` and `gt_demote.py` are now installed into
  the hooks dir, so the nightly jobs and the vault's `gt_task.py` can reach them.
- **`gt_broker.py audit [--since H]`**: lists vault `.md` files changed in the window that the
  broker did not write. It is a report, not an alarm.
- **gt_daily** puts each fact under the daily note's own headings (commit `22fb651`), and now
  writes through the queue.
- **Repository:** editor workspace files are untracked and `*.code-workspace` is ignored. The root
  `.gitignore` gains editor, cache, venv, log, conflict-copy and key-file patterns.

## 2a. Release gate: run BEFORE the build is cut

Owner, 2026-10-01 07:19. This gate is not optional.

`gt_daily` now writes the daily note through the write queue (one `replace-file` per run).
**These three tests must pass UNCHANGED against 0.17.11**, byte-identical to their text in commit
`22fb651`. If any of them had to be edited to pass, the replace-never-append contract broke:
**stop and tell the owner; do not ship.**

1. `test_gt_daily.ItWritesOnlyItsOwnBlock.test_a_second_run_replaces_the_block_rather_than_adding_one`
2. `test_gt_daily.FactsGoUnderTheirHeadings.test_the_owners_lines_in_a_section_survive_a_rerun`
3. `test_gt_daily.FactsGoUnderTheirHeadings.test_a_note_with_the_old_bottom_block_is_migrated`

```bash
cd golden-thread-plugin/tests && python3 -m unittest test_gt_daily     # every test passes
git diff 22fb651 -- golden-thread-plugin/tests/test_gt_daily.py      # the three bodies unchanged
```

Last checked 2026-10-01 07:20 on `feat/queue-first-writes`: all three bodies byte-identical to
`22fb651`, and the whole `test_gt_daily` suite passes (`self-verified`). Re-run it at the cut.

## 3. What to run after install

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

It proves an installed machine is correct for 0.17.11.
- **Output:** a PASS/FAIL/WARN/INFO/PENDING table with the elapsed time, about 1 s. It exits 1 on
  any FAIL. A row that cannot run is a FAIL, never a PASS.
- **Placement rows:**
  - the installed version and ON modules;
  - component drift;
  - hook and Core-rule wiring;
  - rule 1 queue-first, both in the vault and as injected;
  - the four queue scripts in the hooks dir, byte-identical to the release;
  - the guard denying a vault write;
  - queue health (WARN when requests wait);
  - vault migrations;
  - lotr;
  - the daily launchd job.
- **Smoke rows:** the installed copies run against throwaway vaults, in parallel, each step with a
  hard timeout:
  - a queue round trip;
  - broker escalation of a stale `replace-file`;
  - the guard;
  - all 10 injected rules;
  - the lotr daemon plus the MCP `tools/list`;
  - a `gt_daily` dry run.
- **When it runs on its own:**
  - `install.sh` runs it at the end. Rows that need `/gt:gt-upgrade` show PENDING; a real FAIL
    exits **9**.
  - The first SessionStart after a version change runs it once more and shows the result.
- **Read-only:** the real vault and `settings.json` are never written.

Then, in a Claude Code session on that machine:

0. Read the post-install table `install.sh` printed: every row PASS, or PENDING with its fix.
1. **`/gt:gt-upgrade`** refreshes the vault's `core-rules/` (rule 1's new text) and merges the
   PROTOCOL.md "Concurrent sessions" change.
2. **Confirm the new rule is live:**
   `echo '{}' | ~/.claude/golden-thread/hooks/inject_core_rules.sh`. Rule 1 must read "Write vault
   content only through the write queue…".
3. **Confirm the queue is reachable from the hooks dir:**
   `ls ~/.claude/golden-thread/hooks/gt_write_queue.py ~/.claude/golden-thread/hooks/gt_broker.py`.
   Without them the 22:00 daily-note job exits 3 with "write queue is not installed".
4. **Run the gate again:** `python3 ~/.claude/golden-thread/hooks/gt_doctor.py post-install --vault <vault>`. Every row must be PASS (or INFO).
5. **Apply anything waiting:** `python3 ~/.claude/golden-thread/hooks/gt_broker.py drain --vault <vault>`,
   then `… status --vault <vault>`. It should say the queue is empty.

## 4. How to work from now on (both machines)

- **Never edit a vault Markdown file directly** in a Claude Code session. Queue it:
  ```bash
  python3 ~/.claude/golden-thread/hooks/gt_write_queue.py --vault <vault> --path <rel .md> \
      --op append|replace-section|create|set-property|replace-file [--section "<heading>"] \
      --content-file <file> --session <id>
  python3 ~/.claude/golden-thread/hooks/gt_broker.py drain --vault <vault>
  ```
  The skills already do this. If the guard denies a write, its message gives the exact command.
- **`log.md`, `decisions.md` and `TASKS.md` keep their own tools:** `gt_log.py add`,
  `gt_adr.py allocate`/`merge` and `gt_tasks.py`.
- **Held means waiting, not failed.** A write to a file another live session claims stays queued.
  The next drain applies it; session start shows "WRITE QUEUE: N waiting".
- **Escalated means the owner decides.** It becomes a `#conflict` task, with every version kept in
  `spool/broker/conflicts/`.
- **Your own edits in Obsidian are unaffected.** No hook sees them, and the broker never overwrites
  them: a queued write to a file you changed is escalated.

## 5. What needs a decision

- **Two machines, one Dropbox-synced queue.**
  - The broker's lock is a file in the vault, and Dropbox does not make it atomic across machines.
  - Two machines draining in the same few seconds could in principle both apply one request.
  - **Recommendation:** treat this Mac as the machine that drains, the same rule as "only this Mac
    commits" for the vault. A drain on the other machine is safe while this Mac is idle.
  - Not yet measured.
- **When to release:** the owner says when. Then push straight to main, with no PR.

## 6. What will be misread if nobody says it

- **The same rule id now means something different.** An old vault copy of
  `core_concurrent_session_claim.md` still says "claim, then write" until `/gt:gt-upgrade` runs.
- **`until: none`** in a handoff's frontmatter means "no deferral". The queue cannot delete a
  frontmatter key.
- **The only remaining direct vault write by a script** is the broker's own `#conflict` task. It is
  written while the broker holds the drain lock.
- **Deleting or moving a file is not a queue op.** `gt_demote.py`, and demoting a Core rule out of
  `core-rules/`, still do it directly.
