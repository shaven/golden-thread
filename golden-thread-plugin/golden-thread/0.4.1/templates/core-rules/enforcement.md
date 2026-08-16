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
            "command": "$CLAUDE_PROJECT_DIR/.golden-thread/inject_core_rules.sh" }
        ]
      }
    ]
  }
}
```

The script's stdout is added to the turn's context. Point `command` at wherever the
project incorporates this folder (a copy, a symlink, or an absolute path into the
vault). Its output is an **imperative** ("Begin your reply with this exact
timestamp"), not a description — descriptions drift, commands re-anchor.

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
            "command": "$CLAUDE_PROJECT_DIR/.golden-thread/validate_response.sh" }
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

## Incorporating into a project (the "point at it" step)

1. From the project repo: `mkdir -p .golden-thread` and copy or symlink the two
   scripts from this folder into it (symlink keeps one source of truth).
2. Add the two hook blocks above to the project's `.claude/settings.json`.
3. In the project's `CLAUDE.md`, state that Core rules are defined in
   `Projects/golden-thread/core-rules/` and enforced by these hooks.

## Scope note

Wiring these at the **user-global** level (`~/.claude/settings.json`) makes the Core
tier apply to *every* project automatically — which is the intent of "Core." Wiring
per-project scopes them to that project (Context tier). Choose global for true Core
rules; per-project for Context rules a single project owns.
