# Golden Thread — plugin repo

The `gt` and `gt-wiki` Claude Code plugins, plus the templates and hook scripts that
establish a Golden Thread vault. This repo is its own root — it is not part of the
main working repo and shares no rules with it.

## Layout

| Path | What it is |
|---|---|
| `golden-thread-plugin/golden-thread/<ver>/` | The `gt` plugin: skills, scripts, templates, hooks |
| `golden-thread-plugin/golden-thread-wiki/<ver>/` | The `gt-wiki` plugin |
| `golden-thread-plugin/install.sh` | Installs both, wires the hooks, registers the marketplace |
| `.../scripts/vault_init.py` | Vault scaffolding and lifecycle operations |
| `.../scripts/gt_lint.py` | The vault health checks |
| `.../scripts/gt_paths.py` | Vault + core-rules resolution, used by the hooks at run time |

## The rule that governs this repo

**A Core rule is only as durable as the mechanism that re-asserts it.** Storing a
rule in a file is not enforcement — the file may never be read. Anything claiming to
be Core must be wired to a hook and *verified to fire*.

## Landmines

**Hooks must never be referenced from inside the vault.** `settings.json` addresses
them by absolute path, so a path running through a project slug breaks silently on a
rename. They install to `~/.claude/golden-thread/hooks/` and locate the rules at run
time via `gt_paths.py`. This has been re-broken by documentation four times; if you
find a doc pointing at `core-rules/hooks/`, it is wrong.

**Never copy rule text into a hook script.** The injector reads the rule files at run
time. Duplicating a rule into the script creates two copies that drift.

**Bump the version and re-run `install.sh` after changing plugin content.** The
version is the directory name, `install.sh`'s `VERSION`, and `plugin.json` — all
three. Otherwise installed caches keep serving the old content.

**Do not pipe `install.sh` to `head`.** `set -o pipefail` aborts it partway, leaving
the cache updated and registration not done.

**The core-rule TEMPLATES are de-identified; a live vault's copies are not.** `templates/core-rules/*`
ships to strangers, so it names no host, no project and no path from this environment — a live vault's
`core-rules/` may and does. The two therefore differ by design, in wikilinks *and* in identifying
detail; do not "resync" by copying a vault copy over a template. Checked 2026-09-10: the templates
scan clean for host names, the vault copies keep the incident detail that makes the rules persuasive.

**Templates and the vault drift.** `templates/core-rules/*` is meant to stay
byte-identical to a live vault's `core-rules/`. Diff them before assuming either is
current.

**Verify a hook by running it**, not by reading it:

```bash
echo '{}' | ~/.claude/golden-thread/hooks/inject_core_rules.sh
```

**Writing a `Stop`-hook validator is safety-critical.** One that blocks wrongly makes
every session unusable. Fail open on any parse failure, honour `stop_hook_active` so
a block cannot loop, and test the allow cases before the block case.

## The documents, and what belongs in each

Nine documents, each with a different reader. The release gate checks that every skill is
*named* in three of them and that no PDF is older than its source — it cannot check whether
what a document **says** is still true, which is the failure that actually happens.

**Regeneration order** after any `.md` change (each step depends on the one before):

```bash
cd golden-thread-plugin
python3 build-docs.py --build              # .md -> .html
dev/render-pdfs.sh                         # .html -> .pdf
python3 dev/plugins.py manifest golden-thread/<version>
```

### Front door

| | |
|---|---|
| **`README.md`** | **Reader:** someone who has never heard of this. **Must carry:** what it is in two sentences; the distinguishing idea (rules are mechanically enforced, not merely written); a version callout describing the **current** release and what it added; install; the skills table; how knowledge moves; contributing pointers; licence. **Must not carry:** step-by-step usage (that is `ONBOARDING.md`) or implementation detail (`MANUAL.md`). **Goes stale at:** the version callout — it is the first thing to rot when a release ships — and every count in it. |

### Contributor-facing

| | |
|---|---|
| **`SUBMISSIONS.md`** | **Reader:** an outside contributor. **Must carry:** why there is no plugin runtime; what a pack is, with a worked example **in a slot a shipped tool actually reads**; how tiers are derived rather than declared; the validator's three verdicts and that `REVIEW` is not a rejection; every rule with the reason it exists; which slots currently have no consumer; that `retract` is vault-only so a contribution can never retire someone else's definition; the licence terms. **Goes stale at:** the worked example's slot, the verdict list, the open-slot list. |
| **`CHANGELOG.md`** | **Reader:** someone upgrading. **Must carry:** per release, the change *and why it was made*; anything **removed** and what it actually did wrong; anything that breaks or needs migration. **Must not carry:** a silent removal. If a feature was cut, the entry says what the defect was. |

### Plugin reference

| | |
|---|---|
| **`README.md`** (plugin) | **Reader:** someone who has installed it and wants the reference. **Must carry:** what it does; every skill as a row; packs and the registry including precedence and `retract`; vault structure; the immutability model; key files. |
| **`MANUAL.md`** | **Reader:** a daily user. **Must carry:** the model; packs and the registry in full (merge modes, precedence, `retract`, slots without consumers); vault layout; use cases; setup; daily work; every setting. The deepest document — anything with a *why* belongs here rather than in a README. |
| **`golden-thread-docs.md`** | **Reader:** quick lookup, and the printed PDF. **Must carry:** an accurate version line for **every** plugin and module; the Core rules; one row per skill; the modules. **Goes stale at:** the version line, which names six plugins whose numbers move independently. |

### Getting started

| | |
|---|---|
| **`INSTALL.md`** | **Reader:** installing for the first time, or verifying a fork. **Must carry:** every install route; exactly what the installer does and touches; how to verify; connecting an existing vault. |
| **`ONBOARDING.md`** | **Reader:** the first thirty minutes. **Must carry:** numbered steps from install to a first written-back session; the typical session pattern; periodic maintenance. **Must not carry:** anything optional — this document is the happy path. |
| **`OBSIDIAN-WORKFLOW.md`** | **Reader:** someone working in Obsidian alongside sessions. **Must carry:** the division of labour; what to edit where; the promotion ladder; the plugin list. |

### The rule that applies to all of them

**Never write a count you have not derived.** Skill counts, rule counts, check counts and
version numbers drift silently and are quoted across several documents at once, so one stale
number becomes four. Count from the source — `ls .../skills`, `ls .../core-rules` — at the
moment of writing. As of 2026-09-16 a single pass found `gt Skills (16)` in two documents when
21 ship, a version line naming five modules at `0.15.0` after they had all moved to `0.16.0`,
and `gt-lint`'s check count given as 18 in the docs and 13 here.

**Prefer a claim that checks itself.** `gt_registry.CONSUMERS` records which tool reads each
slot, and a test asserts each one really does — which is why that claim cannot quietly stop
being true the way every count on this page can.

## Checks

```bash
python3 <plugin>/scripts/gt_lint.py <vault>
```

Thirteen checks. `core-unenforced` is the important one — it catches a rule that is
stored but never re-asserted, which is the exact failure this system exists to close.

## Deeper context, if this machine has the vault

If `~/.claude/vault-config.json` exists, read `vault_path` from it; the notes are at
`Projects/golden-thread/`. **If it is absent, skip this — everything above stands
alone.**

| Question | File |
|---|---|
| Where the pieces live and how they install | `source.md` |
| Current architecture | `design.md` |
| Why, and what was rejected | `decisions.md` |
| Dated findings | `research.md` |
| The Core rules themselves | `core-rules/` |
