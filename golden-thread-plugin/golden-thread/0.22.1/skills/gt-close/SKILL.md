---
name: gt-close
description: "Close this session's work, the opposite of /gt:gt-open: write back what it learned and release its vault claim. Use when the user says: close this session, close out the session, I'm done for now, wrap up this session, close the project I opened. To complete or archive a project, task or handoff, use /gt:gt-complete."
plain:
  - cmd: "/gt:gt-close"
    does: "Saves what this session learned and closes it."
    how: "Writes the session's findings into the project's notes through the safe write queue, then lets go of the session's claim on the vault."
    detail: "reads your role's close plan (gt_persona.py close-plan; nothing without a persona), runs the gt-work write-back (research.md, decisions.md, design.md) through gt_write_queue, then gt_session.py release."
    when: "You are finished for now and want nothing lost."
    next: "/gt:gt-list, /gt:gt-complete"
    hear: "close this session|close out the session|im done for now|wrap up this session"
model_intent: balanced
---

# Golden Thread — Close

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_session.py release` | skipped: a sandboxed session holds no registration of its own to release (the server stamps its own session on every write); say so and stop after the write-back |
| `gt_persona.py close-plan …` | **terminal**: it reads the project README, which sandbox mode refuses; skip it and close as you always do |

`/gt:gt-open` loads a project into a session; `/gt:gt-close` ends that session's work on it. It
does not complete, archive or retire anything: that is `/gt:gt-complete` (since 0.21.0 — before
it, `/gt:gt-close` did what `/gt:gt-complete` now does).

The write-back takes the routes `/gt:gt-work` lists.

## Steps

1. **Read the close plan** (0.21.4). It says what to put first for the user's role:
   ```bash
   python3 <base_dir>/../../scripts/gt_persona.py close-plan --project "<slug>" --vault "<vault>"
   ```
   **It prints nothing** when no persona is set, or none sets a close plan: then close exactly as
   below, with nothing changed and nothing said about it. When it prints a plan, carry it through
   step 2:
   - in gt-work's survey, look first for the items the plan leads with, each in its own segment;
   - in the wrap-up, report those first, in the plan's order, then the rest in gt-work's order;
   - `Wrap-up: brief first`: open the wrap-up with at most five plain lines (what moved, what was
     decided, what is blocked or waiting on someone), then all the detail below it.

   The plan is order and emphasis only. Every gt-work step still runs, everything it would capture
   is captured (an item the plan does not lead with is never skipped or shortened), and the Core
   rules apply as always. It is not the Audience block: that shapes the wording, this the order.
2. **Write back.** Read `<base_dir>/../gt-work/SKILL.md` — `<base_dir>` is the path in this skill's
   `Base directory for this skill:` header — and follow it from its first step to its last, with the
   project this session opened (`/gt:gt-open <slug>`) as the project. Do not restate or shorten its
   steps here: that file is the procedure, and a change to it is a change to this command. If it
   finds nothing worth keeping, say that in one line.
   **Then check every item has a home.** Before releasing, go through what this session surfaced and
   name any that reached no file: a finding (the project's `research.md`), something a person must
   act on (a task under the README's `## Tasks`), a finding that belongs to another project (that
   project's `research.md`, or `INBOX.md`), platform or tool knowledge (the wiki, only when the user
   asked for it to be written down), and a defect found in a gt command while closing (a task in
   `golden-thread`). One line naming what was left out and why; say nothing when everything has a
   home. This only names gaps: it writes nothing the user has not agreed to.
3. **Release the session.** Unless the write-back has already released this session's registration
   (it says so when it has), run
   ```bash
   python3 "<vault>/Projects/golden-thread/tools/gt_session.py" --vault "<vault>" release --id "<session id>"
   ```
   (`<vault>` is `$GT_VAULT`, else `vault_path` from `~/.claude/vault-config.json`; Core rule 2: name
   it. The flag is `--id`: `release` takes no `--session`.) Echo what it printed. `nothing to
   release` is a normal result, not a failure: this session held no registration, so say so in one
   line and go on. It refuses to clear claims another live process holds; relay that and stop,
   never `--force` it.
4. **Say what is left.** One line: the project is still open (not completed); to finish it, run
   `/gt:gt-complete project <slug>`. If the session has no project open, ask which work to write
   back before running `/gt:gt-work`; never guess.

## Rules

- **Close is not complete.** Nothing is archived, shelved, moved or marked done here.
- **Follow gt-work by reference.** Do not copy its steps into this file.
- **The close plan orders, never filters.** A persona changes what comes first, never what is
  kept or what is enforced.
- Every vault write is a tool's, through the write queue. No Write/Edit on a vault path.
- **Verify a path before you hand it over (0.21.4).** A command you give the user that reads or edits a path, or a path a wrap-up states as fact, is checked first with `<base_dir>/../../scripts/gt_path_check.py` (`path_stat` where the shell cannot see it; `vault_report` `path_check` under sandbox mode) and labelled verified or unverified; gt-work has the full rule.
- **Read in a subagent, keep conclusions here (0.21.4).** If the write-back needs files this
  session has not already read (long logs, another project's notes), a subagent reads them and
  returns only the facts to write. Never paste whole files into the main session: every reply
  after it re-sends them, and a close is when the session is already largest.
