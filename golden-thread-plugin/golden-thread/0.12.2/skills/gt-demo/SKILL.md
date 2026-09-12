---
name: gt-demo
description: "Run a live Golden Thread demo in its own throwaway vault: an eleven-act guided tour of PizzaBot 3000 that runs itself — the presenter only clicks Next. Commands: start (build the demo vault), tour (run the guided tour), end (show what the demo produced), clean (rebuild the demo vault), remove (delete the demo), status. Use when the user says: run the demo, start the demo, demo Golden Thread, give the tour, reset the demo, clean up the demo, remove the demo."
---

# Golden Thread Demo

An eleven-act tour of Golden Thread on PizzaBot 3000, a fictional project. It runs in its
**own vault** — built by `start`, rebuilt by `clean` — so nothing the demo does can reach
the user's real vault, and nothing ever needs rewinding.

## Paths

`<base>` is this skill's base directory (the `Base directory for this skill:` line).

- script: `<base>/../../scripts/gt_demo.sh`
- tour: `<base>/../../templates/demo-pizzabot/tour.md`
- demo vault: `$GT_DEMO_VAULT` if set, else `~/.claude/golden-thread/demo-vault`

## Commands

### start

Run `bash <base>/../../scripts/gt_demo.sh start` and show its output. It prints the one
command that opens the demo session:

```
cd "<demo vault>" && GT_VAULT="<demo vault>" claude
```

Tell the presenter to run that in a **new terminal**, then `/gt:gt-demo tour` there. The
demo session works on the demo vault only; this session is untouched.

### tour

1. **Check the session.** Run `echo "$GT_VAULT"`. It must equal the demo vault path. If it
   is empty or different, STOP: say the tour only runs in a session opened with the
   command `start` printed, and show that command again. Never run the tour against the
   user's real vault.
2. **Read the tour.** Read `<base>/../../templates/demo-pizzabot/tour.md`. Each
   `## Act N — Title` section has `narration:`, `do:` and `point:` lines.
3. **Run each act in order:**
   - say the `narration:` line to the audience, prefixed `**Act N — Title**`
   - carry out the `do:` line exactly — invoke the named skill with the Skill tool, or run
     the named command — working in the demo vault (`$GT_VAULT`). When a skill asks a
     question the act already answers (which project, which claim), answer it from the
     act; ask the presenter only when the act does not say.
   - say the `point:` line, one sentence
   - then ask with the multiple-choice question tool — one question, header `Tour`, with
     options **Next: Act N+1 — <title>**, **Repeat this act**, **Skip ahead**, **End tour**
     (after the last act: **Show the receipt** and **End tour**)
4. **Buttons:** Next runs the next act. Repeat runs the same act again. Skip ahead asks
   which act, as a second multiple-choice question listing the remaining titles. End tour
   stops and suggests `/gt:gt-demo end`. Show the receipt runs `end`.

Keep narration short — the audience is watching the work, not reading prose. Never type
a credential-shaped string in a reply; act 1 shows the secret check with a prepared
transcript for exactly that reason.

### end

Run `bash <base>/../../scripts/gt_demo.sh end` and display the output — every commit and
file the tour produced. Suggest `/gt:gt-demo clean` before the next run.

### clean

Confirm first: "This deletes the demo vault and builds a fresh one. The real vault is not
touched. Continue? (yes/no)". On yes run `bash <base>/../../scripts/gt_demo.sh clean`, then
show the command to reopen the demo session.

### remove

Confirm first: "This deletes the demo vault, removes the gt-demo skill from the installed
plugin, and sets install_demo=no so a reinstall does not bring it back. Continue? (yes/no)".
On yes run `bash <base>/../../scripts/gt_demo.sh remove`, then tell the user to restart
Claude Code.

### status

Run `bash <base>/../../scripts/gt_demo.sh status`.

## Rules

- The demo never touches the real vault or `~/.claude/vault-config.json`; if a step would,
  stop and say so.
- PizzaBot 3000 is fictional — say so when presenting to an audience.
- `clean` and `remove` delete only a directory carrying the demo marker; the script
  refuses anything else.
