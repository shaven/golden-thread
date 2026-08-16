# Golden Thread Protocol

Rules for maintaining vault integrity.

## When to Graduate

Promote from session memory → project files when:
- The finding won't change session-to-session
- You've verified it against actual behavior (not just observed once)
- You'd want to remember it next week

Promote from project files → Knowledge when:
- The fact applies to other projects too
- It describes a platform behavior, tool constraint, or architectural pattern
- It's stable enough to trust without re-verification

Promote to global-memory when:
- It's a cross-cutting fact needed in every session, for every project
- Examples: platform URLs, tool versions, authentication patterns

Promote to **Core** (`Projects/golden-thread/core-rules/`) when:
- The rule has drifted or failed *despite being stored*, or the user designates it
  always-on
- Promotion is not just a move: set `level: core`, set `enforcement`, and **wire the
  hook**. A Core rule that is only stored is not Core — that is the exact failure
  this tier exists to prevent.

### The full ladder

| Level | Home | Guarantee |
|---|---|---|
| 1 | session | none |
| 2 | `Projects/<slug>/memory/` | loaded on demand |
| 3 | `research.md` / `decisions.md` | project-scoped, durable |
| 4 | `Knowledge/` | cross-project |
| 5 | `global-memory/` | all projects, index always available |
| **6** | **`core-rules/`** | **enforced by hooks every turn, all projects** |

## Promotion Log Verbs

Every promotion is logged in `log.md` with a closed vocabulary:

| Verb | Meaning |
|---|---|
| `graduate` | Moved up the hierarchy (level N → level N+1) |
| `retire` | Marked stale; no longer accurate |
| `relocate` | Reclassified (e.g., research → decisions) |
| `ingest` | Imported from outside the vault |
| `work` | Session write-back |
| `query` | Lookup performed |

Format:
```
YYYY-MM-DD [verb] <source> → <dest>: <one-line description>
```

## Session Write-Back Checklist

At the end of any meaningful work session, run `/gt-work` and check:

- [ ] New findings appended to `research.md`
- [ ] New constraints/decisions appended to `decisions.md`
- [ ] `design.md` updated if architecture changed
- [ ] Memory files updated for next session
- [ ] Promotion candidates flagged

## Cross-Project Idea Capture

When you encounter an idea that belongs in a different project:

1. Run `/gt-promote` and choose "new project scaffold"
2. The idea goes into `Projects/<new-slug>/idea.md`
3. Add the project to `Projects/README.md`
4. When ready to start work, run `/gt-init` for that project directory

Don't let cross-project ideas sit in the wrong project's `research.md` — they'll get lost.
