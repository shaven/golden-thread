---
name: gt-demo
description: "Run a live Golden Thread demo in its own throwaway vault: a guided tour of PizzaBot 3000 that runs itself (ten core acts plus one per installed module that ships one) — the presenter only clicks Next. Commands: start (build the demo vault), tour (run the guided tour), end (show what the demo produced), clean (rebuild the demo vault), remove (delete the demo), status. Use when the user says: run the demo, start the demo, demo Golden Thread, give the tour, reset the demo, clean up the demo, remove the demo."
---

# Golden Thread Demo

A guided tour of Golden Thread on PizzaBot 3000, a fictional project: ten core acts, plus
one act for each installed Golden Thread module that ships one. It runs in its
**own vault** — built by `start`, rebuilt by `clean` — so nothing the demo does can reach
the user's real vault, and nothing ever needs rewinding.

## Paths

`<base>` is this skill's base directory (the `Base directory for this skill:` line).

- scripts: `<base>/../../scripts` — this module's own scripts; the tour's `<scripts>`
  placeholder means this directory
- script: `<base>/../../scripts/gt_demo.sh`
- core: the directory printed by `bash <base>/../../scripts/gt_demo.sh core-scripts` — gt's
  own scripts (`gt_watch.py`, `vault_init.py`, ...); the tour's `<core>` placeholder means
  this directory. Since 0.14.0 the demo is its own plugin (`gt-demo`, the `demo` module),
  so gt's scripts are not beside it. `core-scripts` resolves them, first hit wins:
  `$GT_CORE_SCRIPTS`; the newest `gt/<version>/scripts` beside this plugin in the same
  plugin cache; the newest `golden-thread/<version>/scripts` beside it in a source
  checkout; `~/.claude/plugins/cache/golden-thread-plugin/gt/<newest>/scripts`. Each
  candidate must hold `vault_init.py`. If it exits non-zero, gt is not installed: say so
  and stop. (The Core-rule hooks the tour runs, like `validate_response.sh`, are called
  from the stable hooks dir `~/.claude/golden-thread/hooks/`, where install.sh puts them.)
- tour: the output of `bash <base>/../../scripts/gt_demo.sh tour-acts` — never read
  `templates/demo-pizzabot/tour.md` directly. `tour-acts` prints gt's core acts from that
  file, then one act for each Golden Thread module that is installed right now (enabled in
  `~/.claude/settings.json` and present in the plugin cache) and ships one (module.json
  `demo`, e.g. the wiki module's `demo/act.md`), ordered by module name and numbered on
  from the core acts; a module act's heading ends `(module: <name>)`. Install a module and
  its act is in the next tour; remove it and the act is gone. Only first-party modules
  supply acts.
- demo vault: `$GT_DEMO_VAULT` if set, else `~/.claude/golden-thread/demo-vault`

## Commands

### start

Run `bash <base>/../../scripts/gt_demo.sh start` and show its output. It prints the one
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
2. **Assemble the tour.** Run `bash <base>/../../scripts/gt_demo.sh tour-acts` once and
   use its stdout as the tour. If it printed `note:` lines (a module act that was skipped),
   mention each one to the presenter in one line before act 1. Each
   `## Act N — Title` section has `narration:`, `do:` and `point:` lines. In a `do:` line,
   replace `<scripts>` with `<base>/../../scripts`, `<core>` with the core directory (run
   `core-scripts` once, before act 1) and leave `$GT_VAULT` for the shell.
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

### end

Run `bash <base>/../../scripts/gt_demo.sh end` and display the output — every commit and
file the tour produced. Suggest `/gt-demo:gt-demo clean` before the next run.

### clean

Confirm first: "This deletes the demo vault and builds a fresh one. The real vault is not
touched. Continue? (yes/no)". On yes run `bash <base>/../../scripts/gt_demo.sh clean`, then
show the command to reopen the demo session.

### remove

Confirm first: "This deletes the demo vault. The demo skill itself stays installed until
you uninstall the module. Continue? (yes/no)". On yes run
`bash <base>/../../scripts/gt_demo.sh remove` and show its output. The demo is a Golden
Thread module, and only install.sh installs or removes modules: to uninstall it, the user
runs `bash install.sh --without demo` from the plugin repo, then restarts Claude Code. The
script never deletes plugin files itself.

### status

Run `bash <base>/../../scripts/gt_demo.sh status`.

## Rules

- The demo never touches the real vault or `~/.claude/vault-config.json`; if a step would,
  stop and say so.
- PizzaBot 3000 is fictional — say so when presenting to an audience.
- `clean` and `remove` delete only a directory carrying the demo marker; the script
  refuses anything else.
- Invoked as `/gt-demo:gt-demo <command>`.
