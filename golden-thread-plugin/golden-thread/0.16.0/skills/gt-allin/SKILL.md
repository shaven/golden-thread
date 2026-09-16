---
name: gt-allin
description: "Run every check in one command — scan, lint, optimize report, install health — and report how many actually ran, not just what they found. Never pushes, never applies a change."
---

# Golden Thread All-In

One command, every check, and an honest account of which ones executed.

## What it is

An **aggregator**, like `/gt:gt-scan`. It owns no checking logic; each member is a command that
runs and is tested on its own.

| member | what it checks |
|---|---|
| `scan` | code against the language definitions in effect |
| `lint` | vault structure: links, orphans, index gaps, scope leaks |
| `optimize` | vault content that costs context — **report only** |
| `doctor` | install health: versions, component drift, hook wiring |

## The two rules that matter

**1. "A member could not run" outranks "a member found something."** The headline always says
*N of M members ran*. Read that before the finding count — a short finding list from a run where
half the members failed reads exactly like a clean bill of health, and is the opposite.

**2. It never pushes and never applies anything.** Push is outward-facing and effectively
irreversible; other people fetch it and CI acts on it. An aggregator is also the one place a
partial run is easily mistaken for a complete one, so a command that pushed at the end of a run
where a member failed would break Core rule 4 — the rule it exists to enforce. There is no flag
that makes it push. `--suggest-push` prints the command *for you to run*, and refuses to suggest
even that when anything failed or any findings exist.

`optimize` is likewise run in report mode only. A sweep that edits files as a side effect of
"checking everything" changes content without anyone deciding to.

## Steps

**Step 1 — Locate the vault, and know the repo**

`$GT_VAULT`, else `~/.claude/vault-config.json`. `--repo` is the code tree; without it the
`scan` member cannot run — and will say so rather than being quietly dropped.

**Step 2 — Run it**

```bash
python3 <base_dir>/../../scripts/gt_allin.py --vault "<vault>" --repo "<repo>"
```

`--list` shows the members and whether each is installed. `--only scan,lint` narrows the run.

**Step 3 — Report in this order**

1. **What did not run**, and why. This is the part a person cannot recover by reading further.
2. What each member found.
3. Only then, an overall judgement.

Exit codes: `0` all clean · `1` findings, every member ran · `2` usage · `3` a member could not run.

**Step 4 — Then the work**

All-in tells you the state; it does not change it. Fix findings with the owning skill
(`/gt:gt-lint`, `/gt:gt-optimize`, `/gt:gt-scan`), run the tests yourself, and push yourself.

## Rules

- **Never report a clean run when a member did not execute.** Say "3 of 4 ran" and name the
  fourth. This is the single reason the command is shaped this way.
- **Never push on the user's behalf**, even when everything is green — this command cannot know
  whether the tests being relied on were actually run in this session.
- **Never pass `--apply`** to a member from here.
- If the user asks all-in to "fix everything", do the checks first and bring back the list. The
  fixes are separate, deliberate acts.
