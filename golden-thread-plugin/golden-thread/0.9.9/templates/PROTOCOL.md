# Golden Thread Protocol

Rules for maintaining vault integrity.

## When to Graduate

The ladder, lowest to highest: session conversation → `Projects/<slug>/memory/` →
the project's `decisions.md` / `research.md` / `design.md` → `Knowledge/` →
`global-memory/` → `core-rules/`. Levels 1–5 hold **facts** and are read on demand;
level 6 holds **rules** and is pushed into every turn by a hook. A human approves
every promotion.

### Two routing questions, not one

The ladder answers only half of "where does this belong."

- **Reach** — how universally does this apply? This picks the **level**.
- **Audience** — *would this make sense to someone who has never heard of this
  vault?* If yes, it is a **repo fact** and also belongs in that project's
  `CLAUDE.md`, regardless of which level it occupies.

These are independent axes. Reach moves a fact **up**; audience moves it **out**.
A fact can do both: "diff local against remote before overwriting production" is a
`global-memory` fact *and* a repo fact, because anyone touching those servers needs
it whether or not they have ever seen this vault.

The repo layer is **not level 7.** A repo fact is not more universal than a Core
rule — it is aimed at a different reader. Adding it gives the model a second
direction, not another rung.

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

Promote to `core-rules/` (the Core tier) by one of **two paths** — see
"Promoting to Core" below for the procedure:

- **Designation (primary).** The user designates the rule as Core. It is Core from
  that moment; no test applies and no prior incident is required. A rule that must be
  immutable is Core as soon as that is known — requiring it to fail first means
  accepting the failure, which defeats the one tier whose purpose is that it never
  breaks. **Canary rules are designated too**: kept because their absence is *visible*
  rather than *costly*, they are the health check on enforcement itself and are never
  judged by the gate below.
- **Promotion from levels 1–5 (gated).** An existing item moving up must pass the
  three-question test below.

Either way it must be a **rule** (something that must be done), not a fact (something
that is true). Facts stop at `global-memory/`. Only rules enter Core.

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

## Graduating a fact out to a repo

The outward axis. Its destination is `Projects/<slug>/CLAUDE.md` in the vault, which
is committed to that project's repo root — where **every Claude Code session working
in that code reads it automatically**, with no plugin, no configuration, and no vault
access. It is the only channel that costs the reader nothing.

**Where it comes from.** `runbook.md` is the incubator: repo facts are written there
the moment they are learned, with no ceremony and no review. That is the only place
**stability can be observed** — the graduation trigger below is meaningless without
somewhere a fact sits and is watched. Capture is cheap; graduation is deliberate.

**What qualifies.** The fact passes the teammate test *and* has stopped changing.
Stability is the signal: a fact that survived several sessions unchanged is safe to
publish. One still in motion is not.

**What belongs there.** How to work in the code: the real command, the environment
trick, the structural landmine, the file that must never be copied between hosts.
Facts about the code, not about our notes on the code.

**The content rule — this one is absolute.** The substantive content must be
**self-contained**. A reader with no vault gets full value. Vault paths appear only
in a clearly optional trailing section, located through `~/.claude/vault-config.json`
rather than a hardcoded path, and ignorable when absent. A `CLAUDE.md` that only
points into the vault is inert for everyone the exit exists to reach.

**Layering.** A repo with several project areas carries a root `CLAUDE.md` for shared
rules plus one per area. Claude Code reads them cumulatively down the tree, so a
shared rule is stated once at the root and **never repeated** in an area file.

**Log it** with `graduate`, naming the destination repo from `source.md`.

## Promoting to Core

Core is the only tier with an enforcement mechanism, so promotion is a **wiring**
operation, not an editorial one. A rule filed in `core-rules/` but never wired is not
a Core rule — it is an ordinary note in a folder with an impressive name. This is the
exact failure the tier exists to prevent, so the procedure ends in verification.

Anyone — user or session — can push a rule into Core at any time. No prior incident is
required or wanted.

### The gate: three questions

**Designated rules skip this**, canaries included. It applies only when an existing
item at levels 1–5 is being promoted up. Each question is asked in the negative — *if
this rule were **not** enforced*:

1. **Correctness** — would it cause a misunderstanding by Claude that leads to code
   written incorrectly, or a change implemented wrongly, not at all, or in a way that
   is not allowed?
2. **Cost** — would it cause more work, or force backing out a solution already
   implemented?
3. **Cascade** — would it cause a cascade in which rules at the lower five levels are
   misrepresented, or are written such that they cannot or should not be followed?

**Any single YES qualifies it.** Three NOs means it stays at its current level — it
may still be a sound rule, just not a Core one. Record the three answers in the rule
file's body so the tier is justified rather than asserted.

### The procedure

1. **Write the rule as an imperative.** "Begin every response with the current
   wall-clock timestamp," not "responses should include a timestamp." Descriptions
   drift; commands re-anchor.
2. **Set the frontmatter:**
   ```yaml
   metadata:
     type: core
     level: core
     enforcement: validated   # or: reminder
   ```
   Choose `validated` whenever the rule is mechanically checkable — it is the only
   form that does not depend on in-the-moment discipline. Use `reminder` only where a
   check would be ambiguous.
3. **Place the file in `Projects/golden-thread/core-rules/`.** That folder is the
   canonical home, and nothing outside it may hold a Core rule.
4. **Wire the mechanism** — the step that makes it real:
   - `reminder` → the `UserPromptSubmit` hook re-injects it every turn
   - `validated` → *additionally* a `Stop` hook check that blocks a reply violating it

   Both hooks live at `~/.claude/golden-thread/hooks/` and read these rule files at
   run time, so a new rule is picked up without editing any script.
5. **Verify it actually fires.** An unverified Core rule is an assumption:
   ```bash
   echo '{}' | ~/.claude/golden-thread/hooks/inject_core_rules.sh
   ```
   Confirm the new rule appears in the output. For `validated`, also confirm the Stop
   hook rejects a deliberately violating reply.
6. **Log it** in `log.md` with `graduate`, then run `/gt:gt-lint` — the
   `core-unenforced` check must come back clean.

Restart Claude Code after wiring a **new hook event**; the settings watcher loads
hooks at session start. Adding a rule to an already-wired event needs no restart.

### Keep the Core tier small

Every rule in Core competes for attention with the others, so the tier's reliability
falls as it grows. Before promoting, ask whether the rule must genuinely hold
*everywhere, always*. A hard constraint that only matters inside one project is
**Context**, not Core — critical is not the same as universal, and inflating scope to
signal importance weakens the rules already there.

### Demotion

A Core rule drops out when it proves situational or is superseded. Demotion is the
same procedure in reverse, and the order matters: **unwire the mechanism first**, then
move the file and change the frontmatter. Unwiring last leaves a hook pointing at a
rule that no longer exists.

The tiering model itself — the scope × enforcement axes and the full frontmatter
contract — lives in [[CONVENTIONS]] and `core-rules/core_rule_priority_model.md`.
Don't restate it here; a third copy is how the other two go stale.

## Verification before promotion

A figure may move up the ladder only with its verification state attached. Core rule 5
requires the label; this is the process half — **what the label must say before a
figure is written down as fact.**

| Destination | Minimum state |
|---|---|
| session conversation | `unverified` is fine, if labelled |
| `Projects/<slug>/research.md` | `unverified` allowed — research.md is dated findings, not conclusions |
| `Projects/<slug>/decisions.md` / `design.md` | **`independently verified`** — these read as settled |
| `Knowledge/` · `global-memory/` | **`independently verified`** |
| a production change | **`independently verified`** |

"Independently verified" means re-derived by a validator that **never saw the
reasoning** — see `golden-thread/validation-agents` and `/gt:gt-validate`. A second
pass by the same session is `self-verified`: it shares the assumptions that produced
the error, so it confirms rather than checks.

**Why this rung exists.** On 2026-08-22 an unverified analysis reached `decisions.md`
as ADR-1 recommending a four-ticker production change. An independent validator refuted
three of the four — two of which would have deepened portfolio drawdown while appearing
to improve their own ticker's. The ladder carried the error upward because nothing on
it asked how the number was checked.

**A refutation supersedes, it does not delete.** Mark the original inline and record
the divergence; the wrong turn is evidence about how the error was made.

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

## Working Rules

Standing rules that apply to every project in this vault. Each was learned the
hard way; the linked note in `global-memory/` has the incident behind it.

| Rule | Detail |
|---|---|
| Back up before overwriting | Timestamped backup before overwriting any file, on every server, no exceptions. `feedback_always_backup_before_overwrite` |
| Put the backup in the backup folder | The copy goes in the project's designated backup folder, mirroring the source tree — **never beside the file it replaces**. A `.bak` inside a directory that is loaded, served or packaged ships with the artifact and hides which file is live. `feedback_always_backup_before_overwrite` |
| Diff before overwriting prod | Never assume the same filename means the same file lineage. Diff local vs remote before `scp` to production. `feedback_diff_before_overwriting_prod` |
| List changed files | Every reply that edits files states which files changed and where. Don't make the reader reconstruct it from a diff. `feedback_always_list_changed_files` |
| Clear debug code before done | Network taps, monkey-patches and polling get removed before a task counts as finished. `feedback_clear_debug_code_before_production` |
| Be brief | Lead with the answer. Default under 15 lines. Cut recaps, restated reasoning and closing "want me to…" offers; put findings in the vault and link them. `feedback_be_brief` |
| State completion clearly | Say what was delivered and where, up front — don't bury it or end on an open question. `feedback_completion_clarity` |
| Ask which project | When a new topic appears, ask which project it belongs to before writing memory for it, then read that project's hub. `feedback_ask_project_for_new_topics` |
| **Offer an ADR when you learn a *why*** | When the user explains why something is the way it is — corrects a framing, overrules a recommendation, or supplies a rule that has no live signal — **ASK whether to write it as an ADR before moving on**. Do not silently absorb it into the current task. See below. |
| **Vault writes use absolute paths** | The Bash tool's working directory persists between calls, so a relative-path write after an unrelated `cd` lands somewhere unintended and still exits 0. Name every vault file by its full path. `feedback_vault_writes_use_absolute_paths` |
| **Register the session before shared writes** | More than one session works this vault at once. Drop `Projects/golden-thread/sessions/<session-id>.md` listing what you have open, and read the others' before editing any shared file. A claimed file is staged in `pending/`, not edited. See below. |

These live in `global-memory/` because they are not specific to any one project.
The table above is the index; the notes hold the reasoning.

### Offering an ADR — why this rule exists

Added 2026-08-26, user-directed: *"When you bring something up and it should have an
ADR ask me if I want you to create an ADR. I will try to remember otherwise."*

**The failure it prevents.** Over 2026-08-25/26 a deliberate design decision was written up
as a defect **four separate times** — the flat `$2,000` sizing budget, the 50k tier pin,
`fine_ladder.json`'s curated 5-rung ladder, and "5 of 7 machines stale" (four were switched
off by design). Every time the reasoning existed, in a code comment or only in the user's
head, and the vault did not have it. ADR-4 was written only after the fourth occurrence.

**The trigger.** Any of these in a user message is an ADR waiting to be written:
- a correction of framing — *"that is deliberate"*, *"it's not a bug"*
- an overrule — *"just use 2k"*, *"treat it like a 50k so you don't over leverage"*
- a fact with **no live signal**, derivable only from the user — account families, which
  machines are expected up, purchase history, broker rules
- a constraint stated in passing — *"we have forever so lets just play it out slow and steady"*

**The form.** One line, at the end of the reply, naming what the ADR would record:

> *That sounds like an ADR — "the 50k tier pin is deliberate risk policy". Want me to write it?*

Then move on. Do not stall the task waiting for an answer, and do not write the ADR unasked —
the user decides what is durable.

**Why asking rather than doing.** An ADR written without agreement records the assistant's
reading of a decision, which is exactly the thing that was wrong four times. The user
confirming the wording IS the verification step.

**Corollary — a constant that looks crudely conservative is usually load-bearing.** Before
filing anything in this vault as a defect, check for a `user-directed` comment, a docstring
saying *curated* or *stopgap*, or an existing ADR. Ask before raising the alarm.

## Concurrent sessions

Added 2026-08-28, user-directed: *"we need a folder that puts a session id in it so
that we can see who all is connected so we know what we are doing."*

**The failure it prevents.** On 2026-08-28 two sessions worked this vault
simultaneously. One built `claudebox` and wrote `index.md`, `INFRASTRUCTURE.md`,
`log.md` and a memory file. The other was running MSv6/NVDA work and wrote
`research.md`, `TASKS.md`, `log.md` and a project `README.md` — growing
`research.md` from +73 to +124 lines *within a few minutes*. Neither could see the
other. Nothing was lost only because one checked `git status` before committing and
noticed files it had never touched.

**Why git does not save you here.** Every session shares one working tree. There
are no branches to collide, so there is no merge and no conflict marker — just a
last-writer-wins overwrite. A whole-file rewrite, or a `git checkout` intended to
revert your own change, silently destroys the other session's uncommitted work.
The damage is invisible at the moment it happens.

### The convention

1. **Register.** Write `Projects/golden-thread/sessions/<session-id>.md` when you
   begin substantive work: what you are doing, and `files_claimed`.
2. **Look before writing.** `ls Projects/golden-thread/sessions/` and read the
   other sessions' claims before editing any shared file.
3. **Claim before editing**, not after. A claim written after the edit records a
   collision instead of preventing one.
4. **If a file is claimed, do not edit it.** Stage your change under
   `Projects/golden-thread/pending/` — a `git diff` patch for normal files, a
   `.logline` for append-only ones — and apply it once the claim clears.
5. **Heartbeat.** Refresh `last_execution` on every execution, so other
   sessions can tell a working session from an abandoned one.
6. **Release on completion.** When your writes are done, **delete the session
   file**. Absence means finished — no one has to interpret a status field.

### The tool

`Projects/golden-thread/tools/gt_session.py` does all of it. Session id resolves
from `$CLAUDE_CODE_SESSION_ID` automatically.

```bash
gt_session.py register --task "..." --files a.md b.md   # announce
gt_session.py beat                                       # heartbeat, every execution
gt_session.py claim path.md                              # refuses if another LIVE session holds it
gt_session.py check path.md                              # who holds this? exit 1 if someone else
gt_session.py list                                       # every session, STALE ones flagged
gt_session.py release                                    # delete the file -- done writing
```

**Liveness is a fact, not a guess.** Each file records `pid` and `host`. When the
host matches, `os.kill(pid, 0)` settles whether that process is still running, and
that answer overrides the clock — a session busy for two hours is `LIVE`, a session
that died thirty seconds ago is `STALE`. The heartbeat age is only the fallback for
when pid cannot be checked (another machine, missing pid). Default window 30 min.

Still advisory, deliberately: in one shared working tree a stuck hard lock is worse
than a stale hint.

**One id, one writer.** `register` refuses if that session id is already registered
to a *live* process, because two processes sharing an id make a claim ambiguous —
you can no longer tell which one holds a file. This is what `--resume`/`--continue`
produces, since resuming reuses the session id.

```
session <id> is ALREADY OPEN in another live process
  pid  : 4171 on shmacbook (running)
  --resume   take over that registration
  --new      register a second entry for this id, on purpose
```

If the previous pid is **dead**, the registration is replaced automatically with a
note — a crashed run should not require a decision. Nothing is silently shared:
two sessions use one id only when someone says `--new`.

### Rules for the files that collide

- **`log.md` is append-only.** Never rewrite it, never `git checkout` it. To
  remove your own line, delete that line surgically and leave every other line
  untouched.
- **`TASKS.md` is generated.** Never hand-edit; regenerate with `gt_tasks.py`.
- **Commit only your own files.** `git status` before every commit, and if it
  lists files you did not touch, do not sweep them in. `git commit -a` is
  never correct in a shared tree.

### Enforcement

This is **Core/Validated** as of 2026-08-28 — `core_concurrent_session_claim`, the
first rule injected each turn. A `PreToolUse` hook
(`~/.claude/golden-thread/hooks/guard_session_claims.sh`) denies a `Write`/`Edit`
against a vault file another **live** session has claimed, naming the holder and
pointing at `pending/`.

It **fails open** on every error path — no vault, unreadable session dir,
unparseable payload — because a guard that blocks wrongly makes every session
unusable, which is worse than the corruption it prevents.

**What it does not cover, deliberately:** `Bash` writes (`>`, `sed -i`, `tee`, `cp`)
are not inspected, since detecting them means parsing arbitrary shell. Files outside
the vault are ignored, and stale claims are ignored by design. It closes the common
case — an agent editing a file another agent is editing — not every case. A guard,
not a lock.

## Infrastructure

Server topology is defined once in [[INFRASTRUCTURE]] and referenced from each
project's `source.md`. **Never copy the host table into a project** — divergent
copies of shared facts are the exact failure mode this vault exists to prevent.

Before touching code on any host, read the project's `source.md` to confirm
which box serves that role in that environment.
