# Build note — gt 0.17.1

**Read this first if you are the person taking this tree into the other repository.**

This file travels with the publish. `SOURCE.json`, beside it, names the exact commit this tree
was cut from. The project CHANGELOG does **not** travel — it lives in the parent directory of
the publishing repo, outside the published set — so **this file is the only record of what
changed that arrives with the code.** Keep it accurate or the drop is opaque on arrival.

---

## 1. What this drop is

| Plugin | Version | Was |
|---|---|---|
| `gt` | **0.17.1** | 0.16.5 |
| `gt-demo`, `gt-farm`, `gt-flow`, `gt-report-card`, `gt-watch` | **0.17.1** | 0.16.5 |
| `gt-usage` | **0.1.3** | 0.1.2 |
| `gt-wiki` | **0.2.3** | 0.2.2 |

**Every module moved, and it had to.** Each module's `module.json` carries a `requires_gt`
range, and every one of them capped at `<0.17.0`. A gt of 0.17.1 is outside all of them, so
publishing gt alone would have shipped seven modules that refuse to install. They are now
`>=0.17.1,<0.18.0`.

**There is no 0.17.0.** It was cut and then held back so that one further change — session
state written before the context runs out — could go into the same release rather than trail
it by a day. If you see 0.17.0 named anywhere in a commit message or an older note, it means
this release.

**Both 0.17.1 and 0.16.5 are in the tree.** `dev/sync-gt-src.sh` publishes the newest release
of each plugin *and the one before it*, deliberately, so `install.sh` can roll back.

---

## 2. What changed, in one paragraph each

Full detail is in `MANUAL.md` under **Checks and cadences**; this is the shape of it.

- **`gt_secrets.py` — a credential scanner that is its own process.** Nothing in that process
  ever prints matched source text, and no other check runs beside it. A finding is
  `path:line` + rule id + length: never an excerpt, never a prefix, never a hash. It is
  deliberately **not** a `gt-scan` member, because every other scanner prints the text it
  found — there the text *is* the finding, here it is the thing being protected — and two
  opposite output rules in one process is precisely how an earlier attempt leaked.
- **`gt_scan_code.py` — source validation.** The second `gt-scan` member. Rules are **data**,
  from `lint` packs, in a documented subset of ast-grep's rule schema. Each rule declares the
  evaluator tier it needs, and a rule whose tier is absent is reported **SKIPPED, never
  silently passed**. Emits SARIF 2.1.0.
- **Three cadences.** `tests/run.sh` over the scanners' file set (fails the run); a commit gate
  over the **staged diff only** (denies the commit); `gt_sweep.py` over the **whole tree**,
  weekly (reports, never blocks).
- **`gt_check_report.py`** files each check's **verdict, count, scope and ref** into the vault
  — never a finding's content.
- **`gt_code_review.py` — a framework that ships zero opinions.** See §5; this is the single
  most misreadable thing in the release.
- **`gt_schedule.py`** installs, verifies and removes the launchd jobs, validating **through
  launchd** rather than through a terminal run.
- **`gt_daily.py`** writes the day's facts into `Daily Notes/<date>.md` — terse by design, and
  it never touches the `## Noticed` section.
- **`gt_state.py`** writes session state **before** the context runs out. Its signal is
  `ctx_pct` and never the rate-limit meter; the two are unrelated and both look like
  plausible percentages, which is how that mistake stays hidden.
- **`gt_demote.py`** is documented for the first time. It shipped in an earlier release and
  appeared in no document.

---

## 3. What to do on arrival

```bash
# 1. Confirm you have what you think you have.
cat SOURCE.json                 # the commit this tree was cut from, and every plugin version

# 2. Install. It always installs the NEWEST version directory present.
bash install.sh
#    then restart Claude Code — plugins and hooks load at session start.

# 3. Prove it, from the tree you just installed from.
./selftest.sh

# 4. Full release verification, if you are re-publishing rather than just installing.
bash dev/release-check.sh
bash tests/run.sh
```

`install.sh` is the **only** updater. Do not copy directories into place by hand: the installer
also removes what older releases left behind, using `retired.json`, and one install from any
old release must end where a fresh install would.

---

## 4. The one thing that needs a decision: ast-grep

`gt_scan_code.py`'s `astgrep` evaluator tier shells out to the **ast-grep CLI**, which is
**optional and not bundled**.

- **Without it**, rules at that tier are reported **SKIPPED**. The scan still runs, still
  reports, and still says plainly what it did not cover. Nothing breaks.
- **With it**, those rules evaluate. `brew install ast-grep` or `npm i -g @ast-grep/cli`.
- **Below version 0.45.3 it is refused rather than used**, because a partial version silently
  under-matches — which is the exact failure the skip contract exists to prevent.

`install.sh` **offers** it and never installs it behind your back. The intended long-run
default is on, with both ways to install offered — but that is the user's choice to make on
their own machine, not something this drop performs.

The Python binding (`ast-grep-py`) was **removed**. It shipped macOS wheels for cp39 only,
0.25.0–0.30.0, and 0.30.0 lacked enough of the rule language to be worth keeping. If anything
downstream imports it, that import is now dead.

---

## 5. What will be misread if nobody says it

**`gt_code_review.py` ships ZERO review dimensions, and that is the design, not an omission.**

gt does not perform the review and cannot: "does this abstraction earn its keep" has no
mechanical oracle. What gt owns is everything around the judgement — planning the scope,
validating that a finding is *checkable*, suppressing what was already declined, and reporting
what was rejected as well as what was kept.

The dimensions belong to whoever is reviewing: a `review.*.pack.json` in their own vault, at
`<vault>/Projects/golden-thread/packs/`. A core review pack would make gt's idea of a good
review everyone's default, so there is not one, and a test asserts there never quietly becomes
one.

**Running it with nothing configured exits `3`, not `0`.** If it reported success, every user
who had never configured a dimension would be told their code had been reviewed. If you are
wiring this into anything automated, treat exit 3 from `dimensions` or `plan` as "not
configured", never as "clean".

The same shape runs through the release and is worth stating once: **a check that could not run
is not a pass.** `gt-scan` reports *N of M members ran* before any finding count; a rule whose
evaluator tier is missing is SKIPPED, not passed; `gt_secrets.py` ends every successful run with
an affirmative naming what it covered, because silence and cleanliness print identically.

---

## 6. Defects fixed in this release that could affect you

- **`install.sh` aborted under `set -e` on any machine without Homebrew or npm**, dying before
  its final line. If a machine was installed from a build cut between 0.16.5 and this one,
  re-run `install.sh` from this tree and confirm it prints **"Restart Claude Code"** at the end.
  That line is now asserted by a regression test under a stripped `PATH`.
- **`gt_scan.py --all-files` knocked out the `code` member**, reporting "1 of 2 member(s) ran".
  The aggregator forwarded a flag only the `language` member accepts. Members now declare their
  flag surface.
- **`gt_daily.py` mangled the text it quoted** — underscores read as Markdown emphasis, and a
  commit subject containing bold reduced to a single character.
- **Two checks that cried wolf**: `gt_daily --check` called a repository subdirectory "not a git
  repo", and `gt_schedule check` called the live weekly lint job broken for its *normal* exit
  code.
- **Core rules moved to the vault root** (`core-rules/`, from `Projects/golden-thread/`). The
  hooks resolve the folder wherever it is, so an un-migrated vault still works — but if you
  hold any script or document that hard-codes the old path, it is now wrong.

---

## 7. New release gates, if you re-publish from here

Two gates were added to `dev/release-check.sh` and will fail a build that would previously
have passed:

- **`scripts/*.py` must be documented.** Any script with an `argparse` command line must be
  named in at least one of `README.md`, `MANUAL.md`, `golden-thread-docs.md` or the repo-root
  `README.md`. When this gate was written it found **nine** commands documented nowhere.
- **`dev/check_docstring_flags.py`** — a flag a tool's own usage block advertises must be a flag
  argparse actually has. It reads **usage lines only**, because docstrings discuss flags in
  prose and that discussion is often about a flag that deliberately does *not* exist.

---

## 8. Where to write back

Do not edit this tree to propose a change; only the publishing machine writes here, and the
next publish replaces whatever it finds. Requests go to `gt-feature-requests/new/` as a single
Markdown file. Anything left elsewhere is reported as a foreign file on the next publish and
then removed.
