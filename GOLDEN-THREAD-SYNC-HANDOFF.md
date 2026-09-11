# Golden Thread — Sync Handoff

**Release:** gt 0.9.13 · gt-wiki 0.1.1
**Written:** 2026-09-10, from shmacbook, after installing and verifying here
**Plugin commits:** `52030e5`, `f807662`, `189c9d8` (branch `main`, **not pushed to origin**)

> Written fresh on 2026-09-10. If a `GOLDEN-THREAD-SYNC-HANDOFF.md` from another machine
> arrives via OneDrive after this, OneDrive will keep both as a conflicted copy — reconcile
> rather than assuming either one is the whole picture.

---

## Update, 2026-09-10 evening — the second working tree merged in

The second working tree (`OneDrive/Projects2/golden-thread`) had moved past this one. Its
changes were copied in **by file, never by git** — the two repos still share no history.

**Came across:**

- `gt-wiki` 0.1.1 gains `scripts/wiki_log.py` (deterministic `log.md` / `index.md` writes)
  and `scripts/wiki_refresh.py` (git-diff change detection for local sources). The
  `gt-wiki-ingest` and `gt-wiki-refresh` skills now call them. Both smoke-tested against a
  scratch vault — `self-verified`.
- `gt_ingest.py` keyword lists and the `CONVENTIONS.md` example filename were
  **scrubbed of employer-specific platform names** — in **every** release directory, not only
  0.9.13, since the older ones ship too. Each touched release's `MANIFEST.json` had those two
  hashes updated; 0.9.13's was regenerated and its `files` map matches the other tree exactly.
- README acknowledgments now credit Jonathan Tucci's llm-wiki and project-flow plugins as the
  origin of the wiki pattern and of the two scripts. Wording is neutral: this repo is public,
  and **nothing employer-identifying is committed here**.

**Fixed here, and should be carried back to the other tree:**

- `gt-wiki-refresh` selected candidates by `remote:`/`url:`, but `wiki_refresh.py` keys on
  `local:` — a source with only `remote:` is reported `no-locator`. The skill now says
  `local:`/`url:`, sets `upstream_sha:` on the superseding source, and logs through
  `wiki_log.py`.
- `gt-wiki-ingest` and `templates/wiki-CLAUDE.md` now record `upstream_sha:` for sources
  inside a git repo, so the first refresh has an exact baseline.
- ONBOARDING, OBSIDIAN-WORKFLOW, golden-thread-docs and the developer guide now describe the
  two scripts; `.html` rebuilt with `build-docs.py`, four PDFs re-rendered (MANUAL unchanged)
  and audited with pypdf — control string present in all seven tracked PDFs, zero leak hits.
- `docs/workflow.html`: an example naming a specific service mesh became a generic proxy.

**Not done:** employer-specific strings remain in **git history** of this public repo (every
release's `gt_ingest.py` before this commit). Removing them means a history rewrite and a force
push — the owner's call, not a sync step.

`selftest.sh` passed after the merge.

---

## What you are syncing, in one line

Until 0.9.13, `GOLDEN THREAD components: clean` meant *the files are present*. It did not
mean *anything is wired*, and on a second machine those were different facts.

## The symptom that started this

A machine with every gt file installed correctly, the component check reporting **clean**, and
**not one SessionStart hook that had ever run** — `~/.claude/settings.json` had no
`SessionStart` entries at all.

Nothing reported it, and nothing could have. `MANIFEST.json` held `files`, `generated` and
`version` — a file inventory with no notion of wiring. And the only components that could
report *"SessionStart is not wired"* **are** the SessionStart hooks; unwired, they never run
to say so.

## What 0.9.13 changes

| | Before | After |
|---|---|---|
| `MANIFEST.json` | `files`, `generated`, `version` | adds `hooks` — the 9 entries expected in `settings.json`, each naming its owner |
| Clean message | `installed matches <version>` | `installed matches <version>, all 9 hooks wired` |
| `install.sh` | wired 6 hooks from a hardcoded list | reads the same declaration the checker uses, then **verifies** what it wrote |
| Install output | registration printed *below* "Restart Claude Code" | summary moved last; wiring result is visible |
| `selftest.sh` | looped over existing entries (zero entries = zero failures = "pass") | asserts all 9 against the declaration |
| `INSTALL.md` | verify step checked one hook | also names `gt_components.py wiring` |

New states: **`unwired`** (no entry for that event names the script — it never runs) and
**`badpath`** (wired, but names a path that does not exist here).

---

## Do this on the other machine

```bash
cd "<path>/Golden Thread/golden-thread-plugin"
bash install.sh
```

Then **restart Claude Code** — hooks load at session start.

`install.sh` is the only updater. `claude plugin update` cannot work here: the marketplace is
registered as a directory source whose path is its own install location, so it syncs onto
itself and always reports up to date. That mistake once left 0.6.0 installed for a fortnight
with 0.9.4 checked in beside it.

### What a good install prints

Registration and verification now appear **above** the skill list, with "Restart Claude Code"
genuinely last:

```
Registered 6 hooks in ~/.claude/settings.json:
  PreCompact       gt_report_card.py
  SessionEnd       gt_report_card.py
  SessionStart     gt_components.py, gt_push_check.py, gt_version_check.py, gt_workers.py
Verified hook wiring → every hook this installer owns is connected
```

Six, not nine, is correct here: the three enforcement hooks
(`inject_core_rules.sh`, `validate_response.sh`, `guard_session_claims.sh`) are wired by
`vault_init.py install-core-rules` against a vault, which need not exist when `install.sh`
runs. `/gt:gt-init` wires those.

---

## Verify it actually landed

```bash
# 1. All nine wired?
python3 ~/.claude/golden-thread/hooks/gt_components.py wiring \
  "<path>/Golden Thread/golden-thread-plugin/golden-thread/0.9.13"
#    expect: all 9 declared hooks are wired

# 2. What a session start will say
python3 ~/.claude/golden-thread/hooks/gt_components.py check \
  "<path>/Golden Thread/golden-thread-plugin/golden-thread/0.9.13"
#    expect: GOLDEN THREAD components: clean — installed matches 0.9.13, all 9 hooks wired.

# 3. Core rules being asserted?
echo '{}' | ~/.claude/golden-thread/hooks/inject_core_rules.sh
#    expect: the Core rules print
```

Command 1 is the broader of the first two — it compares live settings against the declaration,
so it can report a hook that was never registered. Command 3 proves one hook *answers*, which
is the check that could not see this failure.

**Run these on the second machine especially. Files sync; `settings.json` does not.**

### If it still reports `unwired`

Re-run the installer that owns the entry — the report names it. Do not hand-edit
`settings.json`: the installer is idempotent, and it gets right the two things hand-wiring
gets wrong — the quoting (the source path contains a space, `Golden Thread`, and an unquoted
argument splits on it) and the fact that `gt_components` takes the **version directory** while
`gt_version_check` takes the **plugin root**. Swap those and the version check goes
permanently blind.

`badpath` almost always means a `settings.json` still pointing at a version directory a later
release removed. Re-running `install.sh` repoints it.

---

## Also changed in this sync — read if you distribute from the zip

**`golden-thread-plugin.zip` was rebuilt, and the old one was broken.** `package.sh` had
`VERSION="0.1.0"` pinned against a directory gone for a dozen releases, and never copied
`hooks/` at all — so the zip that `INSTALL.md`, `ONBOARDING.md` and `golden-thread-docs.md`
all tell a new machine to unzip contained a pre-history build **with no Core-rule enforcement
in it**. Both fixed; the version is now derived the way `install.sh` derives it, and the build
prints what it shipped. Verified by installing from a clean extract in a throwaway HOME.

The stale zip is backed up at `~/.claude/golden-thread/backups/` on shmacbook.

**Documents corrected** — all were stale in ways no build step could catch:

- root `README.md` — said v0.9.11, and called `golden-thread-docs` a stale 0.6.0 snapshot.
  That warning was true when written and made false by the 2026-09-09 refresh; it was
  steering readers away from a live document.
- `golden-thread-developer-guide.html` — read `v0.9.12 · Plugin cache: …/gt/0.9.5/`,
  contradicting itself in one line. HTML-only, no markdown, so `build-docs.py` never
  regenerates it.
- `docs/workflow.html`, `docs/ingesting.html` — still `gt v0.2.0`.

**PDFs re-rendered and audited**: MANUAL via WeasyPrint 69.0 on the render host, the rest via
local Chrome headless with `--no-pdf-header-footer` (without that flag Chrome embeds the
source `file://` URL, which once put a home directory path inside three shipped PDFs). Audited
with pypdf, not grep — grep returns a false clean on font-subset encoded text. Control string
present in all five, zero leaks.

---

## State of the two repos

| | Committed | Pushed |
|---|---|---|
| Plugin (`OneDrive/Projects/Golden Thread`) | yes — `52030e5`, `f807662`, `189c9d8` | yes, through `e2c755a` (the evening merge above is committed on top, not pushed) |
| Vault (`Dropbox/Projects/Obsidian`) | yes — `2c92878`, `f16a1b3` | **no** |

OneDrive is the channel the other machine reads, so a push is not required for this sync.
Push when you want them off these two machines.

**Full write-up:** `Projects/golden-thread/research.md` in the vault, entries dated 2026-09-10.
