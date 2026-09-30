# Core-Rule Enforcement Wiring

The golden-thread **establishes** the Core rules here; this file is how they're made
un-removable in practice. Per [[core_rule_priority_model]], a Core rule is only as
durable as the mechanism that re-asserts it — storing it in a file is *not* enough.
Two mechanisms, matching the two enforcement strengths.

## 1. Reminder tier — `UserPromptSubmit` hook (re-inject every turn)

Every turn, before the model reads the prompt, inject the current timestamp and the
Core-rule reminders as context. This keeps the rules in *attention*, not just in a
file. Script: `hooks/inject_core_rules.sh`.

Wire it in the project's (or user-global) `.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command",
            "command": "$HOME/.claude/golden-thread/hooks/inject_core_rules.sh" }
        ]
      }
    ]
  }
}
```

The script's stdout is added to the turn's context. `install.sh` puts the hooks at
that fixed location — **do not point `command` into the vault** (see "Where the
scripts live" below for why). Its output is an **imperative** ("Begin your reply with
this exact timestamp"), not a description — descriptions drift, commands re-anchor.

## 2. Validated tier — `Stop` hook (reject a violating reply)

For Core rules that are mechanically checkable (the timestamp is the canonical case),
a `Stop` hook inspects the finished reply and, if the rule was broken, blocks with a
reason so the model must fix it. This is the only form that does **not** depend on the
model's in-the-moment discipline. Script: `hooks/validate_response.sh`.

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command",
            "command": "$HOME/.claude/golden-thread/hooks/validate_response.sh" }
        ]
      }
    ]
  }
}
```

A `Stop` hook that exits non-zero with a message (or emits
`{"decision":"block","reason":"…"}`) forces the model to continue and correct. Keep
validation to rules that are cheap and unambiguous to check; ambiguous rules stay
Reminder-tier.

## Where the scripts live — and why not here

The **rules** are vault content and live in this folder. The **hook scripts** are
machinery shipped by the `gt` plugin and installed to:

```
~/.claude/golden-thread/hooks/
```

That separation is deliberate. `settings.json` must reference the hooks by absolute
path, so that path must never move — but projects get renamed, merged and removed,
and the vault itself can move. A hook path that runs through a project slug is a
breaking change waiting to happen, and a silent one: a hook pointing at a missing
file simply stops firing.

So the scripts sit at a fixed location outside the vault and **locate the rules at
run time** (`gt_paths.py`: vault-config, then a search for the folder by its marker
file). Consequences:

- Rename or move a project — enforcement keeps working; the path self-heals.
- Edit a `core_*.md` rule — the injected text changes immediately. The rules are not
  duplicated inside the script.
- Vault unreachable — the injector still emits the timestamp, because that rule is
  Validated and must never silently stop.

Install or repair with:

```bash
python3 <plugin>/scripts/vault_init.py install-core-rules --vault <vault>
```

## Scope note

Wiring these at the **user-global** level (`~/.claude/settings.json`) makes the Core
tier apply to *every* project automatically — which is the intent of "Core." Wiring
per-project scopes them to that project (Context tier). Choose global for true Core
rules; per-project for Context rules a single project owns.
