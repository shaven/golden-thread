## Golden Thread — golden-thread

Scope rule: write project-specific facts to `Projects/golden-thread/` only. For cross-project or platform facts, use `/gt:gt-promote`.

At the start of every session, resolve the vault from `~/.claude/vault-config.json`
(key `vault_path`), then read the project memory index and follow its links:
`<vault>/Projects/golden-thread/memory/MEMORY.md`

Platform wiki:
`<vault>/index.md` → follow links into `Knowledge/`


## Changing documentation: what moves together

Docs here are **five files and three generated artefacts**, and they drift apart silently
because nothing requires them to be edited in one go. The release gate checks that every skill
is *named* in three of them, and since 2026-09-16 `dev/check_doc_counts.py` also derives the gt
version, the skill count, the Core-rule count, the lint-check count and every module version
from the source and fails the build on a mismatch. **It still cannot tell whether what a
document SAYS is true** — only whether its numbers are.

And it only sees the patterns it was taught. On 2026-09-17 a sweep found two counts it had been
passing clean for releases: the root README said "Sixteen skills in gt" directly above a table
listing twenty-three, because that file was not in the checker's list at all; and
`golden-thread-docs.md` said "seven hook-backed Core rules" where ten ship, because the number
was not adjacent to the phrase and the checker's spelled-number map began at nine. Both are now
covered. **Treat a green count gate as evidence about the counts it knows, not about the
document** — when you add a count to a doc, add it to the checker in the same commit.

When you add a command, change behaviour, or remove a feature, update **all** of:

| file | what it is |
|---|---|
| `../README.md` | the **front door** — the first thing anyone reads. Its version callout must describe the CURRENT release, not the last one. |
| `README.md` | the plugin's own reference |
| `MANUAL.md` | the full manual |
| `golden-thread-docs.md` | the command reference table |
| `../CHANGELOG.md` | what changed and **why**, including anything removed and what it did wrong |
| `BUILD-NOTE.md` | the handoff that TRAVELS. `../CHANGELOG.md` does not reach gt-src, so this is the only account of the release the other machine gets: versions, what changed, what to run, what needs a decision, and what will be misread. Rewrite it for each release rather than appending. |
| `../SUBMISSIONS.md` | only if the pack format, slots or validator verdicts changed |

### Where a new feature's docs go

A release with a lot of new documentation gets it **structured, not appended** (owner,
2026-09-28). Each document has one job; put each piece where that job is:

| Document | What a new feature gets there |
|---|---|
| `MANUAL.md` | Grouped by **what the user is doing**, not by file. A feature joins the section for that activity (e.g. `## Tasks and handoffs`, after `## Daily work`) — a short model paragraph first, then one `###` per command. Reference detail for a script stays under *Checks and cadences* / *Script reference*, cross-linked both ways. A new workflow also gets a *Typical use cases* entry. |
| `README.md` (plugin) | A row in the skills table, under the matching group. **One** callout for the release naming its themes — not a changelog. |
| `../README.md` | The version callout: 3–5 bullets, user-facing only. |
| `golden-thread-docs.md` | Command reference rows only. |
| `../CHANGELOG.md` | One section per theme, each saying **what** and **why**; then *Known, and not fixed* and *Measured, and deliberately not built*. |
| `BUILD-NOTE.md` | Versions, what to run after install, what needs a decision, what will be misread. Rewritten per release. |

Then regenerate, in this order — each step depends on the one before:

```bash
python3 build-docs.py --build     # .md -> .html
dev/render-pdfs.sh                # .html -> .pdf
python3 dev/plugins.py manifest golden-thread/<version>
```

### The part the gate cannot do for you

The gate catches a missing *mention* and a stale PDF. It cannot catch a doc that is
**accurate about a previous release**, which is the failure that actually happens. On
2026-09-16 a full pass found four of those at once: `SUBMISSIONS.md` used a slot no tool reads
as its worked example, so a contributor would have followed it exactly and produced a pack that
does nothing; the validator's three verdicts were undocumented, so `REVIEW` read as a rejection;
`retract` was undocumented everywhere while two files claimed "the user's own packs always win",
which was false for union slots until `retract` existed; and the front-door README's headline
callout still described 0.15.0.

So when you change behaviour, **re-read the surrounding paragraph, not just the line you came
for**. And prefer a claim that checks itself: `gt_registry.CONSUMERS` names which tool reads
each slot and a test asserts each one really does, so that claim cannot quietly stop being true
the way every other one on this list can.
