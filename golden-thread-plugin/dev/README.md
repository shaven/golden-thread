# `dev/` — the release machinery

Nothing here is installed or shipped to a user. These are the checks a change passes
before it becomes a release, and the tools that publish one.

**If you cloned this repo to use the plugin, you never need anything in this
directory.** `install.sh` is at the repo root and is the only thing you run.

## The gate

`dev/release-check.sh` is the whole gate; every check below is a step in it.

```bash
dev/release-check.sh           # everything
dev/release-check.sh --quick   # skip the test harness and selftest
```

The exit code is the number of failed steps. Each step exists because something once
shipped broken while everything looked fine.

| Step | Script | What it refuses to let through |
|---|---|---|
| syntax | *(inline)* | a shell script that does not parse, a Python file that does not compile |
| versions | *(inline)* | a version directory whose `plugin.json` disagrees with its name |
| manifest | `gt_components.py` | a `MANIFEST.json` that no longer matches the tree |
| skills | `skill_lint.py` | two skills sharing a trigger phrase |
| cli contract | `check_cli_contract.py` | a vault tool that writes without accepting `--vault` and `--dry-run` |
| installer version | `check_installer_version.py` | `install.sh` or `selftest.sh` changed since the newest release was cut |
| retired | `check_retired.py` | a release that stops installing a hook-dir file or registering a hook without listing it in `retired.json`, so upgrades from older releases would leave it behind |
| modules | `plugins.py module-check` | a module whose `module.json` is invalid (unknown key, a listed file missing, a hook claiming a Core-rule enforcement script, a version mismatch) or whose `requires_gt` does not admit the gt being released |
| wiring coverage | `check_wiring_coverage.py` | a shipped hook, script, skill, tool or Core rule that does not reach its destination in a real install |
| docs | *(inline)* + `build-docs.py` | a skill, vault tool, setting or `dev/` script documented nowhere; HTML drifted from its `.md`; a PDF older than its source; docs that never name the current version |
| scrub | `scrub_check.py` | an employer or machine-specific string in anything shipped |
| tests | `../tests/run.sh` | a failing test |
| selftest | `../selftest.sh` | an install that does not work from cold |

### The three newest gates, and the bug each one is for

- **`check_cli_contract.py`** — a rehearsal migrated the *live* vault because
  `gt_adr.py migrate` took no `--vault`. A Core rule telling callers to name the vault
  is unfollowable if the tool offers no way to name it, so this asserts the flags exist
  — by RUNNING `--help`, since a flag can exist in source and still not parse after the
  subcommand.
- **`check_wiring_coverage.py`** — `guard_vault_writes.sh` shipped inert through three
  releases: copied, in the manifest, verified by the component check, and registered in
  `settings.json` by nothing. This installs into a throwaway home — including the
  *upgrade* path, where a vault already exists — and asks every shipped item where it
  ended up. Nothing is hand-listed; each set is read from the release.
- **`check_installer_version.py`** — that fix was then committed with no version bump,
  so two published states both called themselves 0.12.2, one wiring the hooks and one
  not. `MANIFEST.json` covers only what lives inside a version directory, and
  `install.sh` sits at the root. This makes changing it require a bump.

## Publishing

**`dev/publish.sh` is the entry point.** It runs every publish requirement in order and
stops at the first failure, so "publish" is one command rather than four things to
remember in sequence:

```bash
dev/publish.sh --list      # the requirements, in order
dev/publish.sh --dry-run   # what each would do
dev/publish.sh             # publish
```

| Step | Guarantees |
|---|---|
| `gate` | every release check passes, tests and selftest included |
| `committed` | the tree is committed, so what is published equals a commit |
| `pushed` | the commit exists on the remote others read |
| `gt-src` | the shared working copy holds this release, verified by selftest |
| `announced` | a Discussion names this version (warn only — needs `gh`) |
| `logged` | the vault records the publish |

The requirements are **data** at the top of the script, and `--list` prints them, so the
contract is readable without reading the implementation. There is no `--skip`: a step you
may skip is not a requirement. It exists because on 2026-09-12 the gate, the commit and
the push all happened and gt-src was left a release behind — invisible from this repo,
since everything here was correct and only the copy the other machine reads was stale.


| Script | What it does |
|---|---|
| `sync-gt-src.sh` | publishes the newest release **and the one before it** (so `install.sh <previous>` can roll back) to the shared working copy (`gt-src`) from a **committed** tree, scrubbed and verified. `--dry-run` shows what would change |
| `foreign_files.py` | lists files in the publish destination that the publisher did not write, so a second writer is named before `rsync --delete` removes it |
| `render-pdfs.sh` | re-renders the PDFs after a docs change, in parallel — one Chrome per document, each with its own `--user-data-dir`; workers from `gt_settings.py jobs`, `GT_RENDER_JOBS=1` for serial |
| `feature_requests.py` | validates the cross-machine feature-request queue |
| `submissions.py` | validates a **contributed pack** before a human reads it: proves reachability (a Tier A slot must carry no free text at all — that absence is the proof, never the declared tier), and refuses instruction-shaped prose, ReDoS-prone and backreferencing patterns, subtractive packs, executable content, non-UTF-8 bytes, invisible code points, copyleft licences, and missing provenance or DCO. `validate <pack>` per pack; `slots` prints the open slots and their tier. Contributor-facing spec: `../SUBMISSIONS.md` |
| `shipped_hashes.py` | writes `<release>/templates/shipped-hashes.json`: the sha256 of every vault tool and githook text any release shipped (git history plus the version dirs in the tree; a hash once listed is never dropped). `vault_refresh.py` replaces only a vault copy matching one of them, so rerun `python3 dev/shipped_hashes.py golden-thread/<ver>` after editing a template tool or githook; `test_vault_refresh` fails until you do |
| `publish.sh` | runs every publish requirement in order; `--list` prints them, `--dry-run` rehearses |
| `scrub_check.py` | scans for employer and machine-specific strings; exit 1 = hits, other = could not scan (also a failure — an unscanned file is not a clean file)  `--repo <dir>` scans everything a push publishes (tracked + untracked-not-ignored) |
| `plugins.py` | the ONE rule for which plugins this repo ships: `list` prints `dir version name` for the newest release of every plugin; `releases DIR [N]` lists the N newest (default 2), which is what `sync-gt-src.sh` publishes; `manifest-check` / `manifest` verify or write a plugin's `MANIFEST.json`. The gate, `package.sh` and `sync-gt-src.sh` all read it, so a new plugin is picked up everywhere at once |
| `check_retired.py` | compares a release with the one before it and fails when something stopped being installed or registered without a `retired.json` entry — the record `install.sh` uses to remove what older releases left behind |

## Tests

```bash
tests/run.sh                    # every test, in parallel (one process per TestCase class)
tests/run.sh test_gt_lint       # one module, still parallel
tests/run.sh -j 4               # cap the workers
GT_TEST_SERIAL=1 tests/run.sh   # plain unittest, one process
```

`tests/prun.py` is the parallel runner. The suite is I/O-bound almost end to end — each
test shells out to `install.sh`, `vault_init` or a hook and waits — so the cores sat idle
while the wall clock ran: 509s serial against 109s parallel, same tests.

Keep `GT_TEST_SERIAL=1` in mind when a failure looks strange. **If a test fails in
parallel and passes serially, that difference is the finding**, not an excuse to re-run.

`GT_TEST_VERSION=<version>` pins the release under test, so a release being built can be
tested beside the current one.
