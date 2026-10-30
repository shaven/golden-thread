---
name: gt-init
description: "Create the gt vault and wire it to this project, or re-check an existing setup. Use when the user says: install Golden Thread, set up Golden Thread, set up my notes vault, connect this project to my vault, check my Golden Thread setup."
plain:
  - cmd: "/gt:gt-init"
    does: "Sets up the Golden Thread vault and connects it to the current project."
    how: "Creates the vault structure and the connection, or checks an existing setup and repairs what is wrong. It asks you about the choices."
    when: "First use, or a vault that no longer connects."
    next: "/gt:gt-doctor"
model_intent: balanced
---

# Golden Thread Init

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `vault_init.py fresh --vault …`, `vault_init.py install-core-rules --vault …` | **terminal**: init writes `~/.claude/vault-config.json`, the hooks in `~/.claude/settings.json` and `~/.claude/CLAUDE.md`, which sandbox mode write-protects by design |
| `vault_init.py create-project --vault …` | `vault_project` `{action: create, name, …}` in an existing vault; with `--project-dir`, **terminal** |

A **terminal** row is a step sandbox mode refuses on purpose — it changes settings or install state, or rewrites the vault wholesale. Give the user the exact command to run from a terminal, and carry on with the rest.

Set up the Golden Thread vault and connect it to a project. Idempotent — safe to re-run on an existing setup.

## Install choices — ask first, install after

If gt came from `/plugin install` in Claude Code (the session-start line says it is not set up) and the machine-level install has not run yet, use `/gt:gt-setup`: it asks the module question first and then these same choices. This section is for a vault or project wiring that needs an installer run.

Do this FIRST whenever the user asks you to install, re-install or upgrade gt (the installer is `install.sh` in the `golden-thread-plugin` folder they installed from; ask for the folder if you do not have it). An installer you run is not at a terminal, so it cannot ask: every choice would silently take its default, the security level included. So you ask, before it starts, and the installer refuses to start without the security answers.

1. **List the questions.** Run `bash <plugin-folder>/install.sh --questions --json`. It installs nothing. Read `questions` in the order given; the security level (`lockdown`) is first, then `sandbox_mode`, which is also a security question (`security: true`), then the rest. `paranoid` is listed with `applies: false`: the security level's top rung, `paranoid` (strict plus locks on gt's own code), answers it, so it is not asked.
2. **Ask each one with AskUserQuestion, security level first.** One question at a time, in the listed order, using the question's `prompt`. Show its `plain_text` (the same words the installer prints at a terminal and `gt_settings.py explain` prints) so the person can choose without technical knowledge, and offer the `options` with each option's `explain` line. Put the `recommended` option and the machine's `current` value (the pre-selected one) first and say which is which; "Not sure?" and "You can change it later" are part of `plain_text`, so say them. AskUserQuestion takes up to four options: for a longer list (the personas) name the rest in the question and let the person type one. A question with `applies: false` is not asked. Never answer a question for them, and never pick a default for a security question to move things along.
3. **Show ONE review screen.** After the last answer, show every answer in one message, each marked "chosen by you" or "kept (current value)", and ask whether to install or to change one (go back to that question). This review screen is the last step before the installer runs; no install command runs before the person has confirmed it.
4. **Only then run the installer**, with every answer as a flag (or the same answers as JSON in a file given to `--answers`):

   ```bash
   bash <plugin-folder>/install.sh --vault "<vault>" --lockdown <level> --sandbox-mode <off|on> --token-profile <minimum|standard|maximum> --model-profile <name> --persona <names|none> --reply-length <normal|brief|terse> --expiry-guard <value> --statusline <yes|no> --ast-grep <yes|no> --open-guide <yes|no>
   ```

   Add `--paranoid on` only when the person asks for paranoid's locks with a lower level (for example trust-local plus paranoid); the `paranoid` level already turns them on, and `--lockdown paranoid --paranoid off` is refused. `--token-profile` is one choice that sets the model profile, the reply length, the double-check, vault hints, the expiry guard, import helpers and calibration together; `--model-profile`, `--reply-length` and `--expiry-guard` after it only override their own part. The last question, **Open the guide?** (`open_guide`, flag `--open-guide`), is the last one and is only asked when someone is at a desktop: its `applies` is false in CI, over SSH, on a machine with no display, and with `--accept-defaults`, and then it is not asked. A yes opens the Getting started page of the offline help for the persona they chose (`python3 <plugin-folder>/golden-thread/<version>/scripts/gt_help.py start --persona <name>`); print the file address too, in case a window did not appear.

   Its own output names each applied choice and says whether it was chosen or defaulted ("lockdown: trust-local (your choice)"); show those lines, and the closing "Chosen for you" list if there is one.
5. **On exit 5** the installer stopped before installing anything because a security question had no answer, and printed the flags that answer it. Ask the person those questions exactly as above, then run it again. Never retry with defaults, and never pass `--accept-defaults` unless the user asks for it in so many words (it is the explicit opt-out for unattended runs, and the installer's summary says the defaults were accepted, not chosen).

Over an existing install the installer keeps the current values for anything not given; the current value is what is pre-selected in step 2.

## Steps

**Step 1 — Check existing config**

If `$GT_VAULT` is set, this session is pinned to that vault (the demo does this): say so and stop — gt-init must not change the machine's configuration from a pinned session.

Read `~/.claude/vault-config.json`. If it exists and the vault path is a real directory:
- Announce: "Golden Thread is already configured. Vault: `<path>`"
- Ask: "Would you like to wire this vault to a new project, or verify the existing setup?"
- If verify → skip to Step 6 (verification)
- If new project → skip to Step 5

**Step 2 — Gather vault info (fresh install only)**

Ask:
1. "Do you already have an Obsidian vault on this machine?"
   - If YES → "What is the path to your vault?" (no default; let them type it)
     Then tell them: "Golden Thread will only add files that are missing — your existing notes will not be touched."
   - If NO → "Where would you like to create the vault? (default: `~/GoldenThread`; on Windows `%USERPROFILE%\GoldenThread`)"
     Then tell them: "Obsidian is optional — the vault is plain Markdown files and works without it. But if you plan to use Obsidian, point this at your vault folder."
   - Either way, check the path before using it: `python3 <base_dir>/../../scripts/gt_vault_location.py check "<vault>"`. Exit 1 means it is in the Documents folder or on a synced or shared drive (iCloud, Dropbox, OneDrive, Google Drive, Box, a network share): show its warning as it is, then ask with AskUserQuestion: "Keep this path", "Use the recommended `~/GoldenThread`", or "Another path". It is a warning, never a refusal: if they keep it, carry on.
2. "What's your domain or team name? (e.g. 'Platform Engineering', 'Personal', 'Acme Engineering') — used to customize the vault schema"

**Step 3 — Gather project info**

Ask: "Do you want to wire this vault to a specific project right now?"
- Yes → ask for the project directory path (default: current working directory) and a short slug for the project name (kebab-case, e.g. `my-project`)
- No → skip project wiring steps

**Step 4 — Run vault_init.py**

The script is at `<base_dir>/../../scripts/vault_init.py`, where `<base_dir>` is the path shown in the `Base directory for this skill:` header at the top of this skill context. Resolve it to an absolute path before running.

Run:
```bash
python3 <base_dir>/../../scripts/vault_init.py fresh --vault "<vault>" --domain "<domain>"
```

Parse the JSON output. For each action:
- `created` → show as "✓ Created: <path>"
- `skipped` → show as "→ Already exists: <path>"
- `conflict` → STOP. Show: "⚠ Conflict: `~/.claude/vault-config.json` already points to a different vault. Run `/gt-init` again and choose 'connect' mode to switch."
- `error` → STOP. Show the error note.

If a project was specified, also run:
```bash
python3 <base_dir>/../../scripts/vault_init.py create-project --vault "<vault>" --name "<slug>" --project-dir "<project-dir>"
```

Show CREATED/SKIPPED/UPDATED report.

**Step 5 — Verify wiring**

Check that both of these files have a "Golden Thread" section:
- `~/.claude/CLAUDE.md`
- `<project-dir>/CLAUDE.md` (if a project was specified)

If either is missing the section, `vault_init.py create-project --vault "<vault>"` will have added it. Confirm it's present.

**Step 6 — Finish screen**

Run the finish screen and show its output as it is (it is plain words, written for someone who has not seen any of this before):

```bash
python3 <base_dir>/../../scripts/vault_init.py finish --vault "<vault>" [--project "<slug>"]
```

It names the vault path, lists what was created, runs the health check and shows each result as a short checklist with the fix under any line that is not green, then gives three first commands and the restart reminder. It writes nothing. A red line is not yours to fix silently: say what its `fix:` says and offer to run it.

It ends with an `obsidian://` link. Ask once whether to open the vault in Obsidian (optional; the vault is plain Markdown and works without it). On yes, open that link (`open "<link>"` on macOS, `start "" "<link>"` in cmd on Windows, `xdg-open "<link>"` on Linux); if Obsidian is not installed nothing opens, so also say that the install steps are in the README section "Obsidian and a terminal, side by side". Never open it without the yes.

If the vault was freshly created (not pre-existing), add:
> "Tip: run `/gt:gt-ingest` to pull in any existing `.claude/memory/` files, CLAUDE.md constraints, or project notes."

---

## Establish the Core-rule tier

`vault_init.py fresh --vault "<vault>"` now also installs `core-rules/` and
**wires the enforcement hooks** into `~/.claude/settings.json` (`UserPromptSubmit` +
`Stop`). Both steps matter: copying the module without wiring it produces an inert
Core tier, which is the exact failure this system exists to prevent.

For an existing vault that predates the tier:

```bash
python3 <base_dir>/../../scripts/vault_init.py install-core-rules --vault "<vault>"
```

Idempotent — re-running will not duplicate hook entries.

**Scope matters.** Wire at **user-global** `~/.claude/settings.json` so Core applies to
every project. Wiring into a project's `.claude/settings.json` scopes those rules to
that project, which is the Context tier, not Core.

After wiring, confirm with `/gt:gt-lint`: a `core-unenforced` finding means the rules
are stored but not actually being asserted.
