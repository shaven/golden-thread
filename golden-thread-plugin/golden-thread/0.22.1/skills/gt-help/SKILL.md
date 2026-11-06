---
name: gt-help
description: "Open an offline page that plays a gt command against a demo vault, with what it does and what to try next; with no command, a searchable index for your role. Use when the user says: gt help, show me how a command works, what does this command do, show me around gt, getting started with gt, what can I do with gt, show me the workflows for my role."
plain:
  - cmd: "/gt:gt-help"
    does: "Opens a page that shows any gt command running, in a simulated terminal, with plain-words notes beside it."
    how: "Opens one offline web page in your browser, on the command you name or on an index of every command. Nothing is run on your computer and nothing is sent anywhere."
    detail: "help.html in the release's help/ folder, copied with each installed module's help-data file to ~/.claude/golden-thread/help/ and opened with a #command&persona=... address; the transcripts were recorded against the PizzaBot demo vault."
    when: "You want to see a command work before you use it, or you are new to gt."
    next: "/gt-demo:gt-demo"
    hear: "gt help|show me how a command works|what does this command do|walk me through a command|show me around gt|getting started with gt|what can i do with gt|show me the workflows for my role"
model_intent: fast
---

# Golden Thread Help

`/gt:gt-help [command]` opens **one** offline page, `help.html`, parameterised by its address:
`help.html#list` shows `/gt:gt-list` (a simulated terminal that plays the command against the
PizzaBot demo vault, with *What it does*, *How it works*, *When to use it* and *What to try next*),
`help.html#all` lists every command, and no fragment opens the index. Two more tabs are not commands:
`help.html#start` is **Getting started** (why gt, what it never does without asking, where to use it,
three things to say on your first day for your role, the gt pane, and what to do when stuck) and
`help.html#workflows` is **Workflows** (the role workflows as conversations, filtered to your persona,
with an *All roles* button; `&roles=all` opens it unfiltered). `help.html#walkthrough` is **First
day** (what gt is and does, how careful Claude is and who decides, then a first-day path: install,
open a project, work, hand off, close the session with `gt-close`, come back the next day, with
what you see at each step) and `help.html#roles` is **Role guides**, one per persona pack
(`#role:<persona>`: the pack's own title, description, scaffold and pane buttons, its most-used
commands and a worked example). Both ride in `help-guides.js` beside help.html. Getting started
links to both. Your personas (the `persona`
setting) ride along (`#list&persona=project-manager`): the index leads with **For you**, plain mode
drops internals unless *Show details* is opened, and a switcher shows how another role would use gt.
The page offers a guided tour ("Show me around"). **Every command is always one click away under
"All commands".**

Every command's transcript is a real recording: each gt script the command runs was run for real
against a throwaway demo vault, and its output is shown in full. A long command (an ingest, a
promotion) is split into named stages with a "Jump to stage" bar. Where a step needs a model, the
transcript shows a clearly labelled SAMPLE packet instead; no model is ever called. A command that
makes a file (an explainer, a code city, a flow view) shows a screenshot of it, with an "Open what it
made" link to the real file in the `artifacts/` folder staged beside the page.

Nothing is run on the user's machine and nothing goes over the network: the page is a file, and the
transcripts in it were recorded when this release was built.

## Steps

1. Run, with the command the user named (without the `/gt:gt-` prefix, e.g. `list`, `visualize`,
   `calibrate`), or `start` for Getting started, `walkthrough` for the first-day walkthrough, `roles`
   for the role guides, `workflows` for the Workflows tab, or `tokens` for the guide to spending
   fewer tokens (when the user asks how to use less allowance), or with nothing for the index:
   ```bash
   python3 <base_dir>/../../scripts/gt_help.py [<command>]
   ```
   It stages the page, opens the browser and prints the address it opened. `--list` prints every
   command instead (one line each) and opens nothing; `--print` prints the address without opening it.
2. An unknown command exits 2 and names the closest ones; show those and ask which was meant.
3. Say which command's page you opened, in one line. Do not describe the page: it describes itself.

If the browser cannot be opened (no desktop), give the user the printed `file://` address to open
themselves.
