---
name: gt-scan
description: "Scan a tree against the definitions in effect here — source validation against the `lint` rules, plus naming conventions and file encoding per language — and report which checks ran, not just what they found."
---

# Golden Thread Scan

Check code against the definitions this machine actually has, and say which checks ran.

## What it is

`gt-scan` is an **aggregator**. It owns no checking logic; it runs leaf commands and reports
each one separately. The members:

| member | script | checks |
|---|---|---|
| `code` | `gt_scan_code.py` | source validation, against the `lint` rules in effect |
| `language` | `gt_scan_language.py` | naming conventions and encoding, per language |

`gt_scan.py --list` is the authority on that table — it marks a member the release does not
carry as `(NOT INSTALLED)` rather than dropping it, because a listed member that is missing
reads as coverage.

Everything the scan looks for comes from **packs**, not from the scripts. There is no registry
*skill*; the registry is a script beside this one — `gt_registry.py show <slot>` for what is in
effect, `sources` for every pack found in precedence order, `slots` for the slot table. A
contributed language pack changes what the scan does with no code change.

## The rule that matters here

**"A member could not run" is not a pass.** If a check could not load its definitions, the
aggregator exits 3 and says so — it never reports "0 findings", because "nothing is wrong" and
"nothing was checked" would otherwise print identically. Read the *N of M members ran* line
before you read the finding count.

## Source validation (the `code` member)

`gt_scan_code.py` checks code against the `lint` rules in effect. **The rules are data, never
code**: each comes from a `lint` pack in the slot registry, written in a documented subset of
ast-grep's rule schema — `id`, `message`, `severity`, `note`, `files`, `ignores`, and a `rule`
object with atomic (`pattern`, `kind`, `regex`), relational (`inside`, `has`, with `stopBy`) and
composite (`all`, `any`, `not`) forms. Same names, same nesting, same meanings as upstream, so
ast-grep's published rule reference is true here. gt evaluates that data; it never executes
anything a pack supplies.

A rule declares the **evaluator tier** it needs:

| tier | available | what it is |
|---|---|---|
| `text` | always | regex over raw lines; any language, no parser |
| `stdlib` | always, **Python only** | structural matching through the `ast` module |
| `astgrep` | only if the ast-grep CLI is on `PATH` (or `GT_ASTGREP_BIN` names it) | structural matching for many languages |
| `treesitter` | only if `tree_sitter` and a grammar import | structural matching via tree-sitter |

**A rule whose tier is absent here is reported `SKIPPED` — never silently passed.** This is the
aggregator's "a member could not run is not a pass", one level down: same failure, same answer.
Without the ast-grep CLI, a rule declaring a structural shell matcher says it did not run
instead of matching nothing and reading as clean. `--rules` lists every rule, the tier it wants,
`ok` or `SKIPPED` here, the tiers this machine has, and the languages gt ships *validated* rules
for — a shorter list than what an installed engine can parse, stated separately on purpose.

Read the affirmative line the same way as the aggregator's: it names how many rules were
**evaluated** out of how many were given, before the finding count.

Its own exit codes: `0` clean and everything ran · `1` findings · `2` usage · `3` nothing found
**but** a rule was skipped or a pack problem was reported · `4` nothing to scan with. `1`
outranks `3` deliberately — a run can both skip a rule and find something, and a caller
deciding whether to block needs "was anything found" answered first. The skip is still reported
in the affirmative line and in SARIF either way.

Flags worth knowing, beyond `--vault` and `--json`: `--sarif FILE` writes SARIF 2.1.0, with
skipped rules carried as notifications; `--exclude GLOB` (repeatable); `--baseline FILE` hides
findings already accepted there; `--write-baseline FILE` records the current findings as
accepted; `--staged` scans what is staged for commit rather than the working tree.

**A baselined finding is invisible** — that is the whole hazard of having a baseline. The file
carries a `reasons` map keyed `"path::rule"`, preserved across a rewrite, because an accepted
finding with no reason is a silenced one and nobody can tell later which it was. A baseline that
cannot be read is announced and everything is reported. Only write one deliberately, and never
to make a scan quiet.

## Steps

**Step 1 — Locate the vault (optional)**

Local packs live in the vault, so pass it when the user has their own definitions. Use
`$GT_VAULT` if set; otherwise read `~/.claude/vault-config.json` for `vault_path`. A scan works
without a vault — it just uses the shipped definitions only.

**Step 2 — Run the scan**

The scripts are at `<base_dir>/../../scripts/`, where `<base_dir>` is the path in the
`Base directory for this skill:` header.

```bash
python3 <base_dir>/../../scripts/gt_scan.py <path> [--vault "<vault>"] [--json]
```

Useful flags: `--list` (members and whether each is installed), `--only code` / `--only language`
(comma-separated), `--all-files` (also scan generated, vendored and static files, normally
skipped) and `--timeout`.

`--all-files` is a **`language`-member flag**: the aggregator forwards it to every member it
runs, and `gt_scan_code.py` does not accept it, so `--all-files` on its own makes the `code`
member exit 2 and be reported `COULD NOT RUN`. Pair it with `--only language`.

A leaf can also be run on its own — `gt_scan_code.py` has flags the aggregator does not forward
(`--rules`, `--sarif`, `--baseline`, `--staged`). Doing so gives up the *N of M members ran*
line, so read that leaf's own affirmative line instead.

**Step 3 — Read the result in this order**

1. `N of M member(s) ran` — if `N < M`, something was **not checked**. Say so first.
2. `COULD NOT RUN` lines — usually missing or unverifiable packs; `gt_registry.py sources` says why.
3. The findings themselves.

Exit codes: `0` all clean · `1` findings, every member ran · `2` usage · `3` a member could not run.

**Step 4 — Noisy rule?**

A rule firing on things that are not problems is a definition disagreement, not a bug. The scan
prints the exact `retract` snippet; it goes in a pack in the **user's own vault**:

```json
{"slot": "naming", ..., "retract": [{"lang": "go"}]}
```

Only local packs may retract, so this never affects anyone else, and `gt_registry.py show naming`
reports the rule as `RETRACTED` rather than hiding it. Never write this for the user without
asking — switching off a check is theirs to decide.

## Rules

- **Report what did not run before what was found.** A short finding list from a scan that half
  failed is worse than no scan, because it reads as a clean bill of health.
- **Never silently widen the scan.** `--all-files` includes vendored and generated code and will
  produce findings nobody owns; use it deliberately.
- **This scan prints source text** — an identifier, or the matched line, *is* the finding. Do
  not point it at content where the text itself is what needs protecting.
- **gt-scan does not cover credentials, and never will.** `gt_secrets.py` is that tool, and it
  is deliberately not a scan member. Its output contract is the opposite of this one: nothing in
  its process ever prints matched source text, and no other check runs beside it — a finding is
  `path:line`, a rule id and a **length**, never an excerpt, a prefix, a first-four/last-four or
  a hash. Two tools with opposite output rules in one process is exactly how the 0.16.0 attempt
  leaked: its `secrets` check printed only a length while its `naming` check, same tool and same
  run, printed raw source, so a credential-shaped identifier reached stdout and `--json` in
  full. That is why registering it as a member is a test failure. Run `gt_secrets.py` on its
  own; a reader who assumes gt-scan already covered credentials is the failure this separation
  exists to prevent.
- `language` findings are advice about conventions, not defects. Do not "fix" a repo's naming
  wholesale because a pack disagreed with it. `code` findings carry the severity their rule
  declares, and that severity is the pack author's opinion too — read the rule, not just the
  level.

## Resuming an interrupted scan (0.18.0)

Before running `gt_scan.py` on a path, look for an earlier run of the same scan that was
interrupted, in this session or any earlier one:
```bash
python3 <base_dir>/../../scripts/gt_checkpoint.py find --tool scan --target "<path>" --vault "<vault>"
```
If it prints a line, ask once: "A previous scan was interrupted at item N of M. Resume it? [y/n]"
- **y** → `gt_scan.py <path> --vault "<vault>" --resume "<checkpoint>"`. It skips the members
  already done, and the files the `language` member had already scanned, and reports the whole run
  as one (prior results merged).
- **n** → run as normal. A run without `--resume` always starts fresh.

Every run writes its checkpoint to the vault's spool and deletes it on completion;
`--no-checkpoint` runs without one.
