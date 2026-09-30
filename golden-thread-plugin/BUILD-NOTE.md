# Build note — gt 0.17.8

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

| Plugin | Version | Was |
|---|---|---|
| `gt` | **0.17.8** | 0.17.7 |
| `gt-demo`, `gt-farm`, `gt-flow`, `gt-report-card`, `gt-watch` | **0.17.8** | 0.17.7 (content unchanged) |
| `gt-visualize` | **0.2.0** | 0.1.0 |
| `gt-usage` | 0.1.3 | unchanged |
| `gt-wiki` | 0.2.3 | unchanged |

> **Why 0.17.8 exists — explainers came out as close-ups (2026-09-29).** A gt-visualize story that focused one or two parts in every scene rendered "all zoomed in from the start": the camera framed only the focus. 0.2.0 makes the shape a rule. The page opens on an establishing shot, frames everything a scene shows, never comes closer than 55% of the overview, and closes wide again; `--check` refuses a story that does not open and close on the whole system, shows fewer than 3 parts in a middle scene, or lacks a caption per scene (rules S1–S10, F1–F6, in the skill).

> **Why 0.17.7 exists — the installer went silent (2026-09-29).** (Released first as 0.17.6 that morning, withdrawn after 22 minutes, and re-cut here as 0.17.7 so a machine that installed the withdrawn 0.17.6 is not left holding a different release with the same number.) An install over a 0.9.4 vault
> printed nothing while it backed the vault up and applied its upgrades, and was stopped twice as
> hung. Every slow step now announces itself before it starts and reports its time; the vault
> upgrade prints `[k/N]` per step; any step quiet for 15s prints `...still <doing X> (45s)`. **If an
> install seems to stall, it is now telling you what it is doing — let it run.**

> **Why 0.17.5 exists — the two requests from this machine's first install (2026-09-29).**
> (1) The SessionStart component check reported `badpath` and `no-manifest`: it is registered with
> the path of the plugin source install.sh ran from, and that source had moved. It now checks the
> INSTALLED copy of the same release and tells you to re-run `install.sh` from where the source now
> lives, which re-points the hook. (2) A vault write with no registered session now warns at once,
> naming the file and the `gt_session.py register` command; `gt_session.py release` prints the same
> reminder. **After installing 0.17.5, re-run install.sh from the new location once** so the hook
> points at a path that exists.

> **Why 0.17.4 exists — read this if 0.17.3 would not install.** `packs/community/` shipped
> EMPTY, and git does not keep an empty directory, so it was missing from gt-src and from any
> repository that committed it. gt treats a missing release pack directory as tampering and
> refuses to load its definitions. 0.17.4 ships `packs/community/README.md` so the directory
> survives git, and the release gate now fails on any empty directory. Nothing else changed.
> **If your repository committed 0.17.3, commit 0.17.4 over it** — the fix is a file, and only a
> file survives the commit.

**The five gt-versioned modules moved with gt** (their `requires_gt` is now `>=0.17.8,<0.18.0`);
their content is unchanged. gt-usage and gt-wiki keep their own version trains; gt-visualize starts its own at 0.1.0. 0.17.6 and
0.17.5 are in the tree, deliberately, so `install.sh` can roll back. 0.17.3 changed only the
publish (layout + checksums) and `/gt:gt-doctor`'s gt-src check; everything in §2 below arrived
in 0.17.2.

gt now has **28 skills** (five new) and eleven core settings (three new: `surface`,
`handoff_surface`, `task_surface`).

---

## 2. What changed

Three themes. The MANUAL has a new top-level section, **Tasks and handoffs**, for the first two.

**Tasks and handoffs — create, see, handle.**
- `/gt:gt-task` creates a task through the new vault tool `gt_task.py` (`add`, `list`, `done`,
  `drop`, `defer`, `count`), optionally tied to a `[[page]]` or vault path with `[ref:: …]`
  (must resolve). `/gt:gt-task-list` shows tasks by filter (`<slug>`, `p1`–`p3`, `mine`,
  `overdue`, `stale`, `deferred`, `ref:<text>`) without loading any project. `/gt:gt-task-handle`
  works a filtered set: done, drop (reason required), defer (future date and reason required),
  keep. Tasks stay in README `## Tasks`, parsed with `gt_tasks.py`'s own rules; IDs are
  `slug:LINE:HASH` and refused if the line changed. `gt_tasks.py` hides a deferred task
  (`[defer:: YYYY-MM-DD]`) from ranking and escalation until its date. `gt_task.py` refuses a
  README another live session has claimed.
- Handoffs have a status (`gt_handoff_status.py`, hooks dir): `open`, `deferred` (future date
  required), `handled` (marked, or every task citing the handoff's filename checked off),
  `history` (pre-0.17.2, no status, over a week old, no open citing task). `gt_handoff.py`
  writes `status: open`. `/gt:gt-handoff-list` (read-only) and `/gt:gt-handoff-handle`.
  `/gt:gt-open` names the project's waiting handoffs; `/gt:gt-work` requires each task it raises
  to name the handoff's filename.

**Surfacing at session start** — new SessionStart hook `gt_surface.py`. Every session: a
MUST DO block from `<vault>/deadlines.md` (`| item | category | due | see |`; overdue 🔴, within
14 days 🟡), each unhandled handoff as one line (setting `handoff_surface`: `any` default,
`project`, `manual`), and one line counting `p:: 1` tasks waiting on you (setting
`task_surface`). Once each: state files from `gt_state.py`, handed to the model in full after a
compaction. Never loads a handoff body, never writes the vault; a clean start says "nothing
waiting".

**Wiring fixes.**
- Session claims keyed on a machine id (`~/.claude/golden-thread/machine-id`), not the hostname;
  a pid that is the asker's own no longer vouches for another session's file.
- `gt_daily`, `gt_schedule`, `gt_sweep` and the sweep's members install to the hooks dir; new
  `sweep` job (Mondays 07:30); `lint-weekly` normal exits corrected to `{0}`; crashes exit 3
  and say COULD NOT RUN.
- `gt_doctor` gains `schedule` and asks `gt_upgrade` for the `vault` check.
- `/gt:gt-allin` gains `secrets`, `runbooks`, `tests` (the repo's suite, records the receipt),
  `validations` and (with gt-wiki) `wiki`. A repo with no test command reads *could not run*.
- `dev/release-check.sh` files each run's verdict in `<vault>/.gt/checks/release.md`.
- `retired.json` removes a stray hand-copied `hooks/gt_ingest.py`.

---

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
cd golden-thread-plugin         # since 0.17.3 the plugin sits one level down, as on GitHub
bash install.sh --require-checksum   # the only updater; verifies every file, then installs NEWEST
#   then RESTART Claude Code — plugins and hooks load at session start.

./selftest.sh
```

Then, in a Claude Code session:

1. **`/gt:gt-upgrade`** — runs pending vault migrations and moves the vault's stamp to 0.17.2.
   A vault stamped at 0.16.5 or 0.17.1 is behind, and `/gt:gt-doctor`'s `vault` row says so.
2. **Confirm `<vault>/Projects/golden-thread/tools/gt_task.py` exists.** `install.sh` refreshes
   the vault tools (through `scripts/vault_refresh.py`) when the vault is a git repository.
   Until `gt_task.py` is there, the three task skills cannot run and the session-start task line
   stays silent rather than guessing. To refresh by hand:
   `python3 <release>/scripts/vault_refresh.py refresh --vault "<vault>"`.
3. **`/gt:gt-doctor`** — read the `wiring`, `vault` and `schedule` rows.
4. Start a new session and look for the `GOLDEN THREAD surface:` line: a MUST DO block,
   handoffs, a task count, or "nothing waiting". **No line means the hook is not wired** (or
   `surface` is `off`).

`deadlines.md` is optional; without it the MUST DO block is simply not shown.

---

## 4. What needs a decision

**Scheduled runs are refused the vault by macOS (TCC). Not fixed.** Every calendar-fired
`lint-weekly` run — 09-14, 09-21, 09-28 — was refused reading existing files in the CloudStorage
vault; only runs kicked from a session succeeded. The `daily` 22:00 job will very likely fail the
same way. 0.17.2 makes the failure visible (exit 3, COULD NOT RUN; the doctor's `schedule` check
says FAIL). The options — a privacy grant for the interpreter launchd runs, a vault outside
CloudStorage, or no calendar triggers — are the machine owner's choice.

**The `sweep` job cannot be installed yet.** `gt_schedule.py install sweep` runs
`gt_sweep.py --check` first, and it refuses: `gt_registry` cannot find `packs/` from the hooks
dir, so every member would report "could not run" every Monday. The refusal is the check working.
Making the packs resolvable from there is the open question.

---

## 5. What will be misread if nobody says it

**Handoffs now repeat at every session start until handled or deferred. That is by design**
(owner, 2026-09-28): a handoff scrolled past once is as good as never written. Deal with it in
`/gt:gt-handoff-handle`, or defer it to a date; to see it only inside its project,
`gt_settings.py set handoff_surface project`.

**Two OS logins on one Mac are now two machines** for session claims — the id lives under each
HOME — and judge each other's sessions by heartbeat. Before 0.17.2 they shared a hostname and
judged each other's pids.

**A dropped task counts as closed** in `gt_events`, `gt_daily` and every other reader, because
its box is checked; the line itself records `dropped: <reason>`. A drop is a decision, not a
completion — read the line if the difference matters.

**A task that does not name the handoff's filename is not joined to it**, so it neither counts
toward the handoff nor closes it.

**The module versions are not stale.** Nothing in them changed; do not bump them to match gt.

**The deadline alarm lives in gt core, not the report card**, so it does not depend on an
optional module and is computed live at every start.

**`lint-weekly` exiting 1 was always a crash**, never "findings". A note saying otherwise is wrong.

**Measured, and not built:** no detector for a session that ended uncaptured. Across 40 sessions
(8 lossy, 32 healthy) no signal combination separated them — the best AND-pairs caught 1 of 8.
`/gt:gt-work` still asks; nothing guesses.

---

## 6. Where to write back

Do not edit this tree to propose a change; only the publishing machine writes here, and the
next publish replaces whatever it finds. Requests go to `gt-feature-requests/new/` as a single
Markdown file. Anything left elsewhere is reported as a foreign file on the next publish and
then removed.
