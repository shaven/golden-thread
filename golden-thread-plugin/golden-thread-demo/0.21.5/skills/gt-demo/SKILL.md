---
name: gt-demo
description: "Run a live gt demo in its own throwaway vault: a self-running guided tour of PizzaBot 3000. Commands: start, tour [role], walkthrough [role], end, clean, remove, status. Use when the user says: run the demo, start the demo, demo Golden Thread, give the tour, reset the demo, remove the demo."
plain:
  - cmd: "/gt-demo:gt-demo"
    does: "Runs a self-playing tour of Golden Thread on a pretend pizza-ordering project."
    how: "Builds a throwaway vault of its own and walks through the main commands in it. Your real vault is never touched."
    when: "You are new, or you want to show someone."
    next: "/gt:gt-open"
    hear: "run the demo|start the demo|give the tour|demo golden thread"
model_intent: fast
---

# Golden Thread Demo

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_demo.sh …` (start, tour, clean, remove), `gt_demo_track.py run` | **terminal**: the demo vault lives under `~/.claude/golden-thread/`, which sandbox mode write-protects. Run the demo in a session without sandbox mode |

A **terminal** row is a step sandbox mode refuses on purpose — it changes settings or install state, or rewrites the vault wholesale. Give the user the exact command to run from a terminal, and carry on with the rest.

A guided tour of Golden Thread on PizzaBot 3000, a fictional project: twenty core acts
(acts 9 to 19 show what 0.21.4 added), plus one act for each installed Golden Thread module
that ships one. It runs in its
**own vault** — built by `start`, rebuilt by `clean` — so nothing the demo does can reach
the user's real vault, and nothing ever needs rewinding.

## Paths

`<base>` is this skill's base directory (the `Base directory for this skill:` line).

- scripts: `<base>/../../scripts` — this module's own scripts; the tour's `<scripts>`
  placeholder means this directory
- script: `<root>/scripts/gt_demo.sh`, where `<root>` is the folder two levels above this skill's base directory, written as a plain absolute path with no `..` and no `~` (the lockdown allow rules name that exact path)
- core: the directory printed by `bash <root>/scripts/gt_demo.sh core-scripts` — gt's
  own scripts (`vault_init.py`, `gt_lint.py`, ...); the tour's `<core>` placeholder means
  this directory. Since 0.14.0 the demo is its own plugin (`gt-demo`, the `demo` module),
  so gt's scripts are not beside it. `core-scripts` resolves them, first hit wins:
  `$GT_CORE_SCRIPTS`; the newest `gt/<version>/scripts` beside this plugin in the same
  plugin cache; the newest `golden-thread/<version>/scripts` beside it in a source
  checkout; `~/.claude/plugins/cache/golden-thread-plugin/gt/<newest>/scripts`. Each
  candidate must hold `vault_init.py`. If it exits non-zero, gt is not installed: say so
  and stop. (The Core-rule hooks the tour runs, like `validate_response.sh`, are called
  from the stable hooks dir `~/.claude/golden-thread/hooks/`, where install.sh puts them.)
- module scripts: the directory printed by
  `bash <root>/scripts/gt_demo.sh module-scripts <name>` — an installed module's own
  scripts; the tour's `<module:<name>>` placeholder means this directory (e.g.
  `<module:watch>/gt_watch.py`: since 0.15.0 watch is its own module, plugin `gt-watch`,
  not a gt core script). First hit wins: the newest `gt-<name>/<version>` beside this
  plugin in the same cache; the newest `golden-thread-<name>/<version>` beside it in a
  source checkout; `~/.claude/plugins/cache/golden-thread-plugin/gt-<name>/<newest>`. Each
  candidate must carry a module.json naming the module. Exit 3 means the module is not
  installed: skip that act with the reason printed, as for a module act whose skill is
  missing.
- tour: the output of `bash <root>/scripts/gt_demo.sh tour-acts` — never read
  `templates/demo-pizzabot/tour.md` directly. `tour-acts` prints gt's core acts from that
  file, then one act for each Golden Thread module that is installed right now (enabled in
  `~/.claude/settings.json` and present in the plugin cache) and ships one (module.json
  `demo`, e.g. the wiki or watch module's `demo/act.md`), ordered by module name and
  numbered on from the core acts; a module act's heading ends `(module: <name>)`. Install
  a module and its act is in the next tour; remove it and the act is gone. Only
  first-party modules supply acts.
- demo vault: `$GT_DEMO_VAULT` if set, else `~/.claude/golden-thread/demo-vault`

## Commands

### start

Run `bash <root>/scripts/gt_demo.sh start` and show its output. It prints the one
command that opens the demo session:

```
cd "<demo vault>" && GT_VAULT="<demo vault>" GT_WATCH=report GT_WATCH_STATE="<demo vault>/.demo/watch" claude
```

Tell the presenter to run that in a **new terminal**, then `/gt-demo:gt-demo tour` there. The
demo session works on the demo vault only; this session is untouched.

### tour

1. **Check the session.** Run `echo "$GT_VAULT"`. It must equal the demo vault path. If it
   is empty or different, STOP: say the tour only runs in a session opened with the
   command `start` printed, and show that command again. Never run the tour against the
   user's real vault.
2. **Assemble the tour.** Run `bash <root>/scripts/gt_demo.sh tour-acts` once and
   use its stdout as the tour. If it printed `note:` lines (a module act that was skipped),
   mention each one to the presenter in one line before act 1. Each
   `## Act N — Title` section has `narration:`, `do:` and `point:` lines. In a `do:` line,
   replace `<scripts>` with `<base>/../../scripts`, `<core>` with the core directory (run
   `core-scripts` once, before act 1), `<module:<name>>` with that module's scripts
   directory (run `module-scripts <name>` once, before the first act that uses it) and
   leave `$GT_VAULT` for the shell.
3. **Run each act in order:**
   - say the `narration:` line to the audience, prefixed `**Act N — Title**`
   - carry out the `do:` line exactly — invoke the named skill with the Skill tool, or run
     the named command — working in the demo vault (`$GT_VAULT`). When a skill asks a
     question the act already answers (which project, which claim), answer it from the
     act; ask the presenter only when the act does not say.
   - an act ending `(module: <name>)` comes from that installed module; run it like any
     other act. If its skill is still not among your available skills (the module was
     installed after this session started), say "skipped: restart Claude Code to load the
     <name> module" in place of the `do:` and `point:` lines, then show the buttons
   - say the `point:` line, one sentence
   - then ask with the multiple-choice question tool — one question, header `Tour`, with
     options **Next: Act N+1 — <title>**, **Repeat this act**, **Skip ahead**, **End tour**
     (after the last act: **Show the receipt** and **End tour**)
4. **Buttons:** Next runs the next act. Repeat runs the same act again. Skip ahead asks
   which act, as a second multiple-choice question listing the remaining titles. End tour
   stops and suggests `/gt-demo:gt-demo end`. Show the receipt runs `end`.

Keep narration short — the audience is watching the work, not reading prose. Never type
a credential-shaped string in a reply; act 1 shows the secret check with a prepared
transcript for exactly that reason.

### tour ROLE (pm, executive, developer, operations)

A shorter tour for one role: 5 to 7 steps from `templates/demo-tracks/<role>.json`. Run `python3
<root>/scripts/gt_demo_track.py list` for the roles; an unknown role is that list and a stop. The session
check and the "never the real vault" rule are the same as for `tour`.

1. Run `python3 <root>/scripts/gt_demo_track.py show <role>` once. Each step has four lines: what you are
   doing, what to type, what you will see, why it matters.
2. For each step in order: say the goal in one sentence, then carry out the step's "type" line in the demo
   vault (invoke the named skill, or answer the sentence as the person would), show the result, and say
   the "why" in one sentence. Answer a skill's own questions from the step; ask the presenter only when
   it does not say.
3. After each step ask one multiple-choice question, header `Tour`: **Next step**, **Repeat this step**,
   **End tour**. After the last step offer `/gt-demo:gt-demo walkthrough <role>` to keep the page.

`python3 <root>/scripts/gt_demo_track.py run <role> --vault <demo vault>` steps through a track without
asking anything and checks it (the tests use it); it is never part of a presentation.

### walkthrough ROLE

The same steps as an offline page: run `python3 <root>/scripts/gt_demo_track.py path <role>`, then open
that file in the browser (`open <file>` on a Mac, `xdg-open` on Linux, `start` on Windows). It is one file on
this machine with a copy button on each command, back and next, and a progress bar; it makes no network
request. If the file does not exist, say so and offer `tour <role>` instead.

### end

Run `bash <root>/scripts/gt_demo.sh end` and display the output — every commit and
file the tour produced. Suggest `/gt-demo:gt-demo clean` before the next run.

### clean

Confirm first: "This deletes the demo vault and builds a fresh one. The real vault is not
touched. Continue? (yes/no)". On yes run `bash <root>/scripts/gt_demo.sh clean`, then
show the command to reopen the demo session.

### remove

Confirm first: "This deletes the demo vault. The demo skill itself stays installed until
you uninstall the module. Continue? (yes/no)". On yes run
`bash <root>/scripts/gt_demo.sh remove` and show its output. The demo is a Golden
Thread module, and only install.sh installs or removes modules: to uninstall it, the user
runs `bash install.sh --without demo` from the plugin repo, then restarts Claude Code. The
script never deletes plugin files itself.

### status

Run `bash <root>/scripts/gt_demo.sh status`.

### show (used by the tour)

`bash <root>/scripts/gt_demo.sh show <topic>` runs one feature act's real gt commands against
the demo vault and prints each command before its output (topics: tokens-startup,
tokens-output, tokens-compact, local-answers, farm-first, paths, effort, and, when their modules
are installed, workflow and lotr). Every command runs with HOME set to the demo vault's own
`.demo/home`, so a setting an act changes is the demo's, never the presenter's. A block headed
`DESCRIPTION, not a live run` is a setting's explanation or a quoted question, shown because the
feature cannot honestly run offline: say so to the audience. Show the output as printed; never
run a `START` line it prints, never start a model, and never compact this session.

## Rules

- The demo never touches the real vault or `~/.claude/vault-config.json`; if a step would,
  stop and say so.
- PizzaBot 3000 is fictional — say so when presenting to an audience.
- `clean` and `remove` delete only a directory carrying the demo marker; the script
  refuses anything else.
- Invoked as `/gt-demo:gt-demo <command>`.
