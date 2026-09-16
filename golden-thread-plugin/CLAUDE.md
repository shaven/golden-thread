## Golden Thread — golden-thread

Scope rule: write project-specific facts to `Projects/golden-thread/` only. For cross-project or platform facts, use `/gt:gt-promote`.

At the start of every session, resolve the vault from `~/.claude/vault-config.json`
(key `vault_path`), then read the project memory index and follow its links:
`<vault>/Projects/golden-thread/memory/MEMORY.md`

Platform wiki:
`<vault>/index.md` → follow links into `Knowledge/`


## Changing documentation: what moves together

Docs here are **five files and three generated artefacts**, and they drift apart silently
because nothing requires them to be edited in one go. The release gate checks only that every
skill is *named* in three of them — it cannot tell whether what they say is still true.

When you add a command, change behaviour, or remove a feature, update **all** of:

| file | what it is |
|---|---|
| `../README.md` | the **front door** — the first thing anyone reads. Its version callout must describe the CURRENT release, not the last one. |
| `README.md` | the plugin's own reference |
| `MANUAL.md` | the full manual |
| `golden-thread-docs.md` | the command reference table |
| `../CHANGELOG.md` | what changed and **why**, including anything removed and what it did wrong |
| `../SUBMISSIONS.md` | only if the pack format, slots or validator verdicts changed |

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
