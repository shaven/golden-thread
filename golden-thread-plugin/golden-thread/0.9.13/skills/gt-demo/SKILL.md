---
name: gt-demo
description: "Manage a repeatable live demo session. Commands: start (arm snapshot + set up demo project), end (show what the demo produced), clean (restore demo project from template, re-arm for next run), remove (tear down the skill and all demo infrastructure). Use when the user says: run the demo, start the demo, demo Golden Thread, reset the demo, clean up the demo, remove the demo."
---

# Golden Thread Demo

Run a repeatable demo of the Golden Thread workflow using the PizzaBot 3000 demo project.

## Overview

The demo loop is: `start` → [give demo] → `end` → `clean` → repeat as needed. `remove` tears everything down permanently when you're done with all demos.

```
/gt:gt-demo start    — arm the demo (set up PizzaBot project from template)
/gt:gt-demo end      — show what the demo produced (commits, files)
/gt:gt-demo clean    — restore PizzaBot from template, re-arm for next run
/gt:gt-demo remove   — remove all demo infrastructure permanently
```

## Script location

The demo script ships inside the plugin. Resolve it at runtime:

```
SCRIPT="$HOME/.claude/plugins/marketplaces/golden-thread-plugin/plugins/gt/scripts/gt_demo.sh"
```

If that path doesn't exist, tell the user to reinstall the plugin (`bash install.sh` from the plugin source).

## Commands

### start

1. Check that `~/.claude/gt-demo-snapshot` does not exist. If it does, tell the user to run `clean` first.
2. Run: `bash "$HOME/.claude/plugins/marketplaces/golden-thread-plugin/plugins/gt/scripts/gt_demo.sh" start`
3. Tell the user the demo is armed and walk them through the demo script:

   **Demo script:**
   > The PizzaBot 3000 project is now set up in `Projects/demo-pizzabot/`.
   > The demo shows: load the project → ingest the architecture source → query the wiki.
   >
   > 1. `/gt:gt-open demo-pizzabot` — open the project
   > 2. `/gt-wiki:gt-wiki-ingest` — ingest `Sources/2024-01-01 Conversation - PizzaBot Architecture.md`
   > 3. `/gt-wiki:gt-wiki` — query "How does PizzaBot handle topping conflicts?"
   >
   > Then run `/gt:gt-demo end` when the demo is complete.

### end

1. Run: `bash "$HOME/.claude/plugins/marketplaces/golden-thread-plugin/plugins/gt/scripts/gt_demo.sh" end`
2. Display the output (commits and files produced during the demo).
3. Tell the user to run `/gt:gt-demo clean` when ready to repeat.

### clean

1. **Dry run first.** Run: `bash "$HOME/.claude/plugins/marketplaces/golden-thread-plugin/plugins/gt/scripts/gt_demo.sh" clean --dry-run`
   - If it prints `REFUSED`, show the reason and stop. It refuses when a commit to be
     undone is already pushed, or when uncommitted work outside the demo would be lost.
     Do not work around it.
2. **Confirm, showing the exact commits.** The reset undoes **every** commit since
   `start` — from any session, not only the demo's. Show the list the dry run printed,
   then ask:
   > "This resets the vault to the snapshot, undoing the commits above, and restores demo-pizzabot from the template. Ready to clean? (yes/no)"
3. Wait for confirmation.
4. Run: `bash "$HOME/.claude/plugins/marketplaces/golden-thread-plugin/plugins/gt/scripts/gt_demo.sh" clean`
5. Tell the user the demo is re-armed and ready to run again.

### remove

1. **Confirm before proceeding.** Tell the user:
   > "This will permanently remove the gt-demo skill, the demo project artifacts, and snapshot file, and set install_demo=no so a reinstall does not bring it back. Are you sure? (yes/no)"
2. Wait for confirmation.
3. Run: `bash "$HOME/.claude/plugins/marketplaces/golden-thread-plugin/plugins/gt/scripts/gt_demo.sh" remove`
4. Tell the user that gt-demo is fully removed and to restart Claude Code to clear the skill from this session.

## Rules

- Always confirm before `clean` and `remove` — these are destructive operations.
- The demo runs in the user's real vault. `remove` commits only the demo's own files; `clean` is the one command that rewinds history, which is why it dry-runs first.
- `clean` restores from the plugin template, not from git — so it works unlimited times.
- Never print credential values or vault config secrets.
- The demo project (PizzaBot 3000) is fictional. Make sure this is clear when presenting to an audience.
