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

Promote to **Core** (`Projects/golden-thread/core-rules/`) by one of **two paths** —
see "Promoting to Core" below:

- **Designation (primary).** The user names the rule Core and it is Core from that
  moment; no test applies and **no prior incident is required**. A rule that must be
  immutable is Core as soon as that is known — requiring it to fail first means
  accepting the failure. **Canary rules** are designated too: kept because their
  absence is *visible* rather than *costly*, they are the health check on enforcement
  itself and are never judged by the gate.
- **Promotion from levels 1–5 (gated).** An existing item moving up must pass the
  three-question test below.

Either way it must be a **rule** (something that must be done), not a fact (something
that is true). Facts stop at `global-memory/`. Only rules enter Core.

Observed drift is **not** an entry requirement — a rule that drifts was mis-tiered.
Drift is a diagnostic, not a qualification.

### Two routing questions, not one

The ladder answers only half of "where does this belong."

- **Reach** — how universally does this apply? This picks the **level**.
- **Audience** — *would this make sense to someone who has never heard of this
  vault?* If yes, it is a **repo fact** and also belongs in that project's
  `CLAUDE.md`, regardless of which level it occupies.

Independent axes: reach moves a fact **up**, audience moves it **out**. A fact can
do both. The repo layer is **not another rung** — a repo fact is not more universal
than a Core rule, it is aimed at a different reader.

## Graduating a fact out to a repo

Destination: `Projects/<slug>/CLAUDE.md`, committed to that project's repo root,
where **every session working in that code reads it automatically** — no plugin, no
configuration, no vault access.

**Where it comes from.** `runbook.md` is the incubator: repo facts are written there
the moment they are learned, with no ceremony and no review. That is the only place
**stability can be observed** — the graduation trigger below is meaningless without
somewhere a fact sits and is watched. Capture is cheap; graduation is deliberate.

**What qualifies.** Passes the teammate test *and* has stopped changing. Stability
is the signal; a fact still in motion is not ready to publish.

**What belongs there.** How to work in the code: the real command, the environment
trick, the structural landmine, the file that must never be copied between hosts.

**The content rule is absolute.** Substantive content must be **self-contained** — a
reader with no vault gets full value. Vault paths appear only in an optional trailing
section, located via `~/.claude/vault-config.json`, ignorable when absent. A
`CLAUDE.md` that only points into the vault is inert for everyone it exists to reach.

**Layering.** A repo with several areas carries a root `CLAUDE.md` for shared rules
plus one per area; they are read cumulatively, so state a shared rule once at the
root and never repeat it.

**Log it** with `graduate`, naming the destination repo from `source.md`.

### The full ladder

| Level | Home | Guarantee |
|---|---|---|
| 1 | session | none |
| 2 | `Projects/<slug>/memory/` | loaded on demand |
| 3 | `research.md` / `decisions.md` | project-scoped, durable |
| 4 | `Knowledge/` | cross-project |
| 5 | `global-memory/` | all projects, index always available |
| **6** | **`core-rules/`** | **enforced by hooks every turn, all projects** |

Levels 1–5 hold **facts** and are read on demand. Level 6 holds **rules** and is
pushed into every turn by a hook. A human approves every promotion.

## Promoting to Core

Core is the only tier with an enforcement mechanism, so promotion is a **wiring**
operation, not an editorial one. A rule filed in `core-rules/` but never wired is not
a Core rule — it is an ordinary note in a folder with an impressive name.

### The gate: three questions

**Designated rules skip this**, canaries included. It applies only when an existing
item at levels 1–5 is being promoted up. Each is asked in the negative — *if this rule
were **not** enforced*:

1. **Correctness** — would it cause a misunderstanding by Claude that leads to code
   written incorrectly, or a change implemented wrongly, not at all, or in a way that
   is not allowed?
2. **Cost** — would it cause more work, or force backing out a solution already
   implemented?
3. **Cascade** — would it cause a cascade in which rules at the lower five levels are
   misrepresented, or are written such that they cannot or should not be followed?

**Any single YES qualifies it.** Three NOs means it stays at its current level.
Record the three answers in the rule file's body.

### The procedure

1. **Write the rule as an imperative** — "Begin every response with the current
   timestamp," not "responses should have timestamps." Descriptions drift; commands
   re-anchor.
2. **Set the frontmatter** — `level: core` plus `enforcement: validated` (preferred
   whenever mechanically checkable) or `reminder`.
3. **Place the file in `core-rules/`** — the canonical home; nothing outside it may
   hold a Core rule.
4. **Wire the mechanism** — `reminder` → the `UserPromptSubmit` hook; `validated` →
   additionally a `Stop` hook check. Both live at `~/.claude/golden-thread/hooks/` and
   read the rule files at run time, so a new rule needs no script edit.
5. **Verify it fires** — `echo '{}' | ~/.claude/golden-thread/hooks/inject_core_rules.sh`
   and confirm the rule appears. An unverified Core rule is an assumption.
6. **Log it** with `graduate`, then run `/gt:gt-lint` — `core-unenforced` must be clean.

Restart Claude Code after wiring a **new hook event**; adding a rule to an
already-wired event needs no restart.

### Keep the Core tier small

Every rule in Core competes for attention with the others, so reliability falls as the
tier grows. A hard constraint that matters only inside one project is **Context**, not
Core — critical is not the same as universal.

### Demotion

Order matters: **unwire the mechanism first**, then move the file and change the
frontmatter. The reverse leaves a hook pointing at a rule that no longer exists.
Never demote a canary for failing the gate.

The tiering model itself lives in `core-rules/core_rule_priority_model.md`.

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
