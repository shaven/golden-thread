---
name: gt-scan
description: "Scan a tree against the language definitions in effect here — naming conventions and file encoding, per language, from the definition packs. Reports which checks ran, not just what they found."
---

# Golden Thread Scan

Check code against the definitions this machine actually has, and say which checks ran.

## What it is

`gt-scan` is an **aggregator**. It owns no checking logic; it runs leaf commands and reports
each one separately. Today there is one member:

| member | script | checks |
|---|---|---|
| `language` | `gt_scan_language.py` | naming conventions and encoding, per language |

Everything the scan looks for comes from **packs**, not from the scripts. There is no registry
*skill*; the registry is a script beside this one — `gt_registry.py show <slot>` for what is in
effect, `sources` for every pack found in precedence order, `slots` for the slot table. A
contributed language pack changes what the scan does with no code change.

## The rule that matters here

**"A member could not run" is not a pass.** If a check could not load its definitions, the
aggregator exits 3 and says so — it never reports "0 findings", because "nothing is wrong" and
"nothing was checked" would otherwise print identically. Read the *N of M members ran* line
before you read the finding count.

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

Useful flags: `--list` (members and whether each is installed), `--only language`,
`--all-files` (also scan generated, vendored and static files, normally skipped).

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
- **This scan prints source text** (an identifier is the finding). Do not point it at content
  where the text itself is sensitive — credential scanning is a separate tool, deliberately.
- Findings are advice about conventions, not defects. Do not "fix" a repo's naming wholesale
  because a pack disagreed with it.
