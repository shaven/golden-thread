---
name: gt-pane
description: "Change the gt button pane in plain words: move, reorder, swap, add, remove, rename or re-word buttons, reset them to a role's defaults, or turn the pane or its allowance line on or off. Use when the user says: move Overdue to the top, reorder my buttons, add a button for X, remove the Z button, rename a button, change what a button says, reset my buttons, show my buttons, turn the gt pane off, show the allowance in the pane."
plain:
  - cmd: "/gt-pane:gt-pane"
    does: "Changes the buttons in your gt pane: their order, their names and the words each one sends."
    how: "You say what you want in plain words; Claude makes the change in your own button list and shows the new list."
    when: "A button is in the wrong place, you want one for something you do every day, or one you never press."
    detail: "the side column docks only in Claude Code's fullscreen renderer; if the pane never docks or shows one line, run /tui fullscreen."
    next: "/gt-pane-buttons"
    hear: "move a button|add a button|remove a button|rename a button|reorder my buttons"
model_intent: fast
---

# Golden Thread pane

The gt pane is a column of twelve buttons beside the conversation. Each button has a **label** (what you see) and a **prompt** (the plain words it sends when pressed). The list is the user's own file, `~/.claude/golden-thread/pane/buttons.json`; the plugin never holds it and an upgrade never overwrites it. It is seeded once from the user's role (persona) and is theirs from then on.

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell cannot write gt's own files. The pane list is one of them, so every change below is a **terminal** step: give the user the exact `gt_pane.py` command to run from a terminal, and carry on. Listing (`list`) still works. With sandbox mode off nothing changes: run the commands as written.

| Step | Under sandbox mode |
|---|---|
| `gt_pane.py list` | runs as written: it only reads |
| `gt_pane.py move` / `swap` / `add` / `remove` / `rename` / `set-prompt` / `reset` | **terminal**: it writes `~/.claude/golden-thread/pane/buttons.json` |
| `gt_settings.py set pane_at_start` / `pane_status` / `pane_buttons` | **terminal**: it writes gt's settings under `~/.claude` |

Script: `<base>/../../scripts/gt_pane.py` (installed copy: `~/.claude/golden-thread/hooks/gt_pane.py` -- use that one, its path survives version bumps, and write it as a plain absolute path with no `~`: the lockdown allow rules name that exact path). Call it with `python3`. **Never edit or write `buttons.json` by hand** -- the script validates every change (12 buttons at most, a label of 22 characters at most, no empty text, no control characters) and writes it all-or-nothing, so a mistake can never leave the user with a half-written list. Positions are counted from 1, top to bottom; the keys 1 to 9, 0, q and w are positions 1 to 12.

## Plain words to commands

Always start with `python3 ~/.claude/golden-thread/hooks/gt_pane.py list` so the positions are known, then map the request:

| The user says | Run |
|---|---|
| "show my buttons", "what buttons do I have" | `gt_pane.py list` |
| "Move Overdue to the top" | `gt_pane.py move <position of Overdue> 1` |
| "put Start my day last" | `gt_pane.py move <its position> <number of buttons>` |
| "move Overdue above Inbox" | `gt_pane.py move <Overdue's position> <Inbox's position>` (a move puts it where the other one was) |
| "swap Overdue and Inbox" | `gt_pane.py swap <a> <b>` |
| "add a button called Weekly report that says write my weekly report" | `gt_pane.py add "Weekly report" "Write my weekly report"` (add `--at 1` for the top) |
| "remove the Overdue button", "I never use that one" | `gt_pane.py remove <position>` |
| "rename Overdue to Late" | `gt_pane.py rename <position> "Late"` |
| "make the Overdue button say <words>" | `gt_pane.py set-prompt <position> "<words>"` |
| "put my buttons back", "start over", "the list is broken" | `gt_pane.py reset` (add `--persona <name>` for another role's set) |

Match a button by its label, ignoring case; if two could match, ask which. If the pane is full (12) and the user wants another, offer to remove or replace one -- do not choose for them. Prompts are plain words the user would say to Claude ("Show my overdue tasks"), one line, never a slash command or a shell command. A label that is too long: suggest a shorter one (22 characters at most; about 19 fits the pane without being cut).

`reset` replaces the whole list with a role's defaults and keeps the old file beside it as `buttons.json.before-reset-<time>`; say so, and confirm with the user before running it unless the list is damaged.

After every change, show the new list from the script's own output and say the pane shows it the next time it is opened (`/gt-pane-buttons` twice, or a new session). The pane can also be rearranged without Claude: press **Arrange** in the pane. **Update Actions**, the control below Arrange, starts this skill from the pane, and **Open Help** below it opens `/gt:gt-help`. Neither takes one of the 12 buttons.

## Turning the pane on and off

- "turn the gt pane off / hide the button pane": `python3 ~/.claude/golden-thread/hooks/gt_settings.py set pane_at_start off` (`closed` keeps it available with `/gt-pane-buttons` but does not open it at start; `open` is the default).
- "stop showing my usage in the pane": `gt_settings.py set pane_status off` (`on` brings it back). The line under the buttons shows the last plan-allowance figures the usage module measured -- read-only, never estimated.
- "show fewer buttons" / "I can't reach Arrange or the usage line": `gt_settings.py set pane_buttons 6` (3 to 12; 12 is the default). Only the first N buttons are drawn; the rest stay in the list, and Arrange still shows all of them so a hidden one can be moved up. It is a setting, not an edit of the list: the buttons are not deleted.

Both take effect from the next session, or the next `/gt-pane-buttons`.

## When the pane does not show

The pane needs Claude Code 2.1.290 or later and a terminal or the desktop app; in VS Code or an older Claude Code it quietly does not appear. `/gt-pane-buttons` toggles it. If the buttons file is damaged the pane shows one red line saying so; `gt_pane.py reset` fixes it.

**The side column docks only in Claude Code's fullscreen renderer.** Under the classic renderer (the main-screen layout: tmux by default, `CLAUDE_CODE_NO_FLICKER=0`, and some terminals, Obsidian's among them) it does not dock, however wide the window is. The buttons then ride in a row above the prompt, and the line above that row says "pane docks only in fullscreen — run /tui fullscreen". The fix is the user's to make in that session: `/tui fullscreen` (Claude Code restarts and resumes the session to switch), after which the pane docks; `CLAUDE_CODE_NO_FLICKER=1` before starting Claude Code does the same for every session. Check this first when someone says the pane "never docks" or "shows only a one-line band"; do not go looking for a gt fault.

In a fullscreen window narrower than about 144 columns the pane cannot sit beside the conversation either, so the same button row appears (without that hint); widening the window docks it.
