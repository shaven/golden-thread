---
name: core_secrets_live_in_the_store
description: "CORE rule — a secret's value rests only in the secrets store or in a mode-600 file the store wrote; never in source, a vault file, a repo, a log, or a session. Applies to every project, every host, every turn."
metadata:
  node_type: memory
  type: core
  level: core
  enforcement: reminder
  promoted: 2026-09-07
  designated_by: user
  companion: core_no_secrets_in_transcript
imperative: "A secret's value rests only in the secrets store (sops + age on a dedicated host) or a mode-600 file the store wrote — never in source, a vault file, a repo, a log, or a session; if you find one anywhere else, file it, rotate it, and move it."
---

**A secret's value rests only in the secrets store, or in a mode-600 file the store
wrote. Never in source code, never in a vault file, never in a repository, never in a
log, never in a session.** If you meet one anywhere else, treat it as disclosed: file
it against `Projects/secrets-management/`, rotate it through the store, and move the consumer
to a file the store writes.

The store is defined by `Projects/secrets-management/` ADR-1: sops + age on a dedicated secrets host, every
file encrypted to two recipients, mirrored as ciphertext to a private git repo and to
claudebox. Until a given secret has been seeded there, the interim home is the mode-600
file its rotation script writes, and the value is still never inline.

## Why this is Core

`core_no_secrets_in_transcript.md` governs the *session*: a value must not pass through
a conversation. This rule governs *rest*: where a value is allowed to exist at all. The
week of 2026-09-01 showed that the session rule alone is not enough. Values sat inline
in a relay daemon's source, in three cron scripts on a second host, in eight files that
shared one credential, in two vault memory notes pushed to GitHub, and in a repo mirror
awaiting `git init`.
Every one of those was a future transcript leak waiting for a grep, and two of them
became one.

Designated Core by the user on 2026-09-07 ("That should probably be a core rule that
secrets must be stored in the vault"). Designation skips the gate, but the gate is
answered here so the tier is justified rather than asserted.

## The gate — all three, answered 2026-09-07

| Gate | Answer |
|---|---|
| **Correctness** | **Yes.** A value that rests inline gets copied: the 08-24 shares fix, the 09-05 fingerprint sweep and the 09-06 docket research each found the *same* credential in a place nobody knew it had reached. A session that finds a secret inline writes the next script the same way. |
| **Cost** | **Yes.** Every inline value found this week produced a rotation task, a history rewrite, or both. Four rotations and one force-push in seven days, none of which a store-first rule would have required. |
| **Cascade** | **Yes.** Two vault memory files spelled out live credentials. Everything written from those files downstream, runbooks, research entries, Knowledge pages, inherits the leak and cites a value instead of a path, which breaks the session rule and the vault's own source conventions at once. |

Three yeses, plus designation.

## Why Reminder, not Validated, today

The Stop hook can check a reply; it cannot check a host. The mechanical check for this
rule is a credential-shaped-literal scan over the vault and over each repo before
commit, which is a `pre-commit` hook and a `gt-lint` check, not a reply validator.
Until that scan exists this rule is Reminder. Promote to Validated when
`Projects/secrets-management/` ships the scan; the frontmatter and this section change
together.

## How to apply

- **Writing a script that needs a credential**: read it from a mode-600 file the
  store writes (`install -m 600` over ssh from a `sops --decrypt --extract`). Never
  a literal, never an environment assignment in a crontab line, never `-u user:pass`.
- **Finding a credential inline**: do not copy it, do not print it. Record the path
  and line in `Projects/secrets-management/` `source.md`, file the rotation task, and let the
  rotation script move it.
- **Writing vault notes about a credential**: the path, the key name, the consumer,
  the mode. Never the value. A vault file is a repo.
- **Placeholders in examples**: `<client-secret>`, `$VAR`, `change-me`. The bad-password
  sentinel in a "must be refused" check is allowed and should be a dictionary phrase
  such as `definitely-wrong`, so a scanner can be told about it by hash.
- **The two identities** (the secrets host's everyday age key and the recovery key) are
  themselves secrets under this rule: never in git, never in a session. The
  passphrase-encrypted QR blob is the one artefact that may be copied freely.

## Related

- `core_no_secrets_in_transcript.md` — the session half of the same principle
- `Projects/secrets-management/` — the store, the inventory, the rotation contract
- `Projects/production-patches/` — the rotation scripts being absorbed
