---
name: core_no_secrets_in_transcript
description: "CORE rule — never put a secret's value into the session. Read the file directly, or report only its shape. Applies to every chat, every project, every turn."
metadata:
  node_type: memory
  type: core
  level: core
  enforcement: validated
  promoted: 2026-08-18
  supersedes: claude-standards/standards/10-secrets.md (the pasting clause)
---

**Never put a secret's value into the session — not to inspect it, not to redact it,
not to check it.** Read the file directly and use what you need without printing it, or
report only its *shape*: how long it is, which key it is, where it lives. A path is
safe; a value is not.

This covers passwords, tokens, API keys, client secrets, private keys and basic-auth
pairs — in commands, in command *output*, and in anything quoted back.

## Why this is Core, and Validated

Adopted from `claude-standards/standards/10-secrets.md`, which already said it. That
rule was correct, written down, and **broken twice on 2026-08-18** — because it lived on
a machine the vault had not ingested. It is promoted here so it cannot be invisible again.

Validated rather than Reminder because both failures came from *believing the redaction
had worked*. A reminder cannot catch that; only inspecting the finished reply can.

## The gate — all three, answered 2026-08-18

| Gate | Answer |
|---|---|
| **Correctness** | **Yes.** A literal in context makes a later session likelier to *write* that literal into code — the hardcoded-credential anti-pattern that took a day to remove from shome-security. And once disclosed the value must be rotated, so a session unaware of that writes code against a value about to become invalid. |
| **Cost** | **Yes.** The 2026-08-18 disclosure created a rotation task that would not otherwise exist, and it is still open. |
| **Cascade** | **Yes.** Vault content is written *from* sessions. A session holding a literal produces notes and runbooks citing the value instead of the path, violating the same standard's other clause and propagating the leak down into levels 1–5. |

Three yeses, plus independent designation by the user.

## How to apply

- **Reading a secret**: point a tool at the file. `grep -c`, `wc -c`, "does key X exist"
  — never `cat`, never `grep` that prints the matching line.
- **Reporting a secret**: "`NEST_CLIENT_SECRET`, 35 chars, in `secrets.env`" — never the value.
- **Debugging**: never `sh -x`, `set -x` or `--verbose` on anything that touches
  credentials. Tracing prints values past any mask you have written.
- **Placeholders**: use an obviously non-real form — `<client-secret>`, `$VAR`,
  `change-me`. A realistic-looking fake (`GOCSPX-xxxxxxxx…`) trips the validator, and
  that is deliberate: it also trips secret scanners in CI.
- **Discussing** credentials, prefixes and formats is fine. The rule is about values.

## What the validator catches

Known credential prefixes with real entropy behind them, PEM private-key headers, and a
credential keyword or `-u user:` followed by a literal. Variable references, placeholders
and redaction markers are explicitly allowed, so talking about credentials stays possible.

**Its limit**: it matches shapes, not meaning. A secret in an unrecognised format passes.
It narrows the failure; it does not remove the need to follow the rule.

## Known failure mode — how both leaks actually happened

Neither was a decision to paste a secret. Both were **redaction that silently did not
match**:

1. A `sed` pattern written for shell quoting, applied to PHP quoting — printed a client
   secret, refresh token and access token.
2. A mask covering `=value` forms, applied to `sh -x` output that renders `[ -z value ]` —
   printed three passwords.

The lesson is not "redact more carefully". It is **do not route the value through the
transcript at all**, because a redaction you cannot see fail is not a control.
