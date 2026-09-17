# Contributing a pack to Golden Thread

> **Reader:** an outside contributor sending a pack
> **Claims last checked against the code:** 2026-09-17 — see *The documents, and what belongs in each* in [`CLAUDE.md`](CLAUDE.md).

Golden Thread has **no plugin runtime**. Nobody's code runs on anyone's machine as a
third-party add-on. Instead you **submit a pack**, it is reviewed, and if accepted it is
**merged into this repository** and becomes part of Golden Thread — held to the same release
gate as everything else.

That is a deliberate choice. Containing hostile code while it runs was attempted and
abandoned; moving the decision to review time is the only version that holds. The cost is that
review has to stay cheap, which is what the rules below are for.

**Check before you send:**

```bash
python3 dev/submissions.py validate my-pack.pack.json
python3 dev/submissions.py slots          # which slots are open, and their tier
```

The validator has **three verdicts**, and only one of them is a refusal:

| verdict | exit | meaning |
|---|---|---|
| `READY` | 0 | send it |
| `REVIEW` | 1 | a human has to look — not a rejection |
| `REJECT` | 2 | fix the reason printed and re-run |

`REVIEW` exists because some fields are *supposed* to contain prose. A `runbook.step` reading
"You must stop the scheduler before migrating" is the slot working exactly as intended, and an
earlier version refused it — the matcher cannot tell a legitimate instruction from an injected
one, so it asks rather than guesses. Send a `REVIEW` pack; just expect a conversation about the
wording.

The validator runs before a human reads anything, so a rejected pack costs nobody any time.

---

## What a pack is

A pack is **one JSON file**: a manifest plus a list of entries. It is data, not a program.
A reviewer can read the whole thing, and `git diff` shows exactly what changed on an update.

```json
{
  "schema": 1,
  "slot": "naming",
  "name": "elixir",
  "tier": "A",
  "spdx": "MIT",
  "provenance": {
    "origin": "original",
    "contributor": "A Dev <dev@example.com>",
    "upstream": null
  },
  "dco": "Signed-off-by: A Dev <dev@example.com>",
  "entries": [
    { "lang": "elixir", "construct": "function", "style": "snake" }
  ]
}
```

Name the file `<slot>.<name>.pack.json`.

### The most useful thing to send: a language

`gt-scan` knows nothing about any language. Everything it knows arrives in packs, so **four
small packs teach it a language it has never seen**, with no change to any code:

| slot | what it says | example entry |
|---|---|---|
| `filetype` | which files are this language | `{"match": "*.ex", "lang": "elixir"}` |
| `construct` | how to find a function, class or type | `{"lang": "elixir", "construct": "function", "pattern": "^[ \\t]*def[ \\t]+([A-Za-z_][A-Za-z0-9_]{0,80})"}` |
| `naming` | how each construct should be named | `{"lang": "elixir", "construct": "function", "style": "snake"}` |
| `encoding` | charset, line endings, BOM | `{"lang": "elixir", "charset": "utf-8", "eol": "lf", "bom": "never"}` |

Send them together and say so in the pull request; they are reviewed as one contribution.

### Some slots are open but nothing reads them yet

`dev/submissions.py slots` lists every open slot. `gt_registry.py slots` additionally says
which ones a shipped tool actually **reads**, and today `secrets`, `lint`, `vocabulary`,
`validation_rules` and `runbook` have no consumer: a pack for one of them validates, merges,
resolves correctly — and then changes nothing until the tool that reads it ships.

That is stated plainly rather than discovered, because a definition nothing reads is the most
demoralising kind of contribution to make. Ask before spending time on one.

---

## Tiers are not a label you choose

Every slot is either **Tier A** or **Tier D**, decided by one question: *can this slot's
content reach the model's context?*

- **Tier A — it cannot.** These packs carry **no free text at all**. Every field is an
  enumerated value, a short token, a path or a pattern. That absence of prose *is* the proof;
  it is not a promise anyone has to take on trust.
- **Tier D — it can.** Vocabularies, validation rules and runbooks are read by an AI session.
  The format allows prose here, but a Tier D pack is **never merged verbatim**: the maintainer
  rewrites the wording and credits you for the content. Text that reaches a model steers every
  session that loads it, which is not something review can safely wave through.

Declaring the wrong tier is a rejection, not a correction. `dev/submissions.py slots` prints
the table.

---

## The rules, and why each exists

**Patterns are executable in effect.** A regex runs against real content. Nested unbounded
quantifiers (`(a+)+`), backreferences and patterns over 200 characters are refused, because
Python's engine backtracks and a bad pattern is a hang.

**Packs only ever add.** Nothing contributed may subtract from core scrub terms or core ignore
behaviour, or mark a file as unreviewed. Ignore and classification rules are security
controls — an exclusion is a silent hole.

**No executable content.** No shebangs, install scripts, hook paths or build files. A project
template that ships a `postinstall` runs on someone's machine.

**Everything must be readable text.** Binary or opaque content is refused outright, including
in tests. The xz-utils backdoor lived in a test fixture nobody read.

**No invisible characters.** Zero-width, direction-steering and variation-selector code points
are refused wherever they appear.

**Licence.** Golden Thread is **MIT, permanently** — there is no CLA and no plan to relicense,
so your grant is final and the terms can never change under you. Contributions are MIT
(inbound = outbound).

The check is an **allowlist**, and that is what does the refusing. `spdx` must be one of:

    MIT   BSD-2-Clause   BSD-3-Clause   Apache-2.0   ISC   CC0-1.0   Unlicense

Anything else fails closed as `licence-unknown`, whether or not anyone anticipated it. The same
test is applied to `provenance.upstream.spdx` when you adapt something, so an adapted pack
cannot carry in a licence its own manifest would have been refused for.

Beside the allowlist there is a short table of **common refusals with an accurate reason** —
not a second gate, just better wording for the cases people actually hit. Copyleft cannot be
merged into an MIT project, so every GPL, LGPL, AGPL and MPL identifier there is reported as
`licence-refused` and says *copyleft* (`network copyleft` for AGPL, `file-level copyleft` for
MPL); SSPL-1.0 and BUSL-1.1 are named too, as *not OSI-approved* and *not open source*. Both
spellings of every copyleft family are listed — the deprecated short forms (`GPL-3.0`, its `+`
variant) and the current `-only` / `-or-later` forms, plus `MPL-2.0-no-copyleft-exception` and
the whole `LGPL-2.1` family. Until 2026-09-17 only the short forms were listed, so
`GPL-3.0-or-later` — the identifier any modern licence scanner emits — was correctly refused
but read as "not in the allowed list", which invites a contributor to open an issue asking for
it to be added rather than to stop.

The SPDX List version those identifiers were taken from is recorded in the source as
`SPDX_LIST_VERSION = "3.29.0"` (verified against <https://spdx.org/licenses/> on 2026-09-17;
that list was released 2026-09-16), because a refusal rule is only as reproducible as the list
it was read against.

If you are adapting a secret-scanning ruleset, adapt **gitleaks** (MIT), not TruffleHog (AGPL).

**Provenance is mandatory.** `origin`, `contributor`, and for anything adapted the upstream
`name`, `version` and `spdx`. This is what makes it possible to answer "where did this come
from?" years later, in one query rather than by reading history.

**Rules lifted from commercial or closed tools are not accepted.** Reading a pack cannot
detect this, so it rests on your declaration of origin.

**DCO, not a CLA.** One line — `Signed-off-by: Your Name <you@example.com>` — asserting you
have the right to submit this. No paperwork.

---

## How it is handled

```
submit → automated validation → planning → review of the diff → merge → release
```

- Submissions are **public**; nothing is accepted in confidence, and we may independently
  implement similar functionality.
- **Updates are reviewed exactly like first submissions.** There is no auto-merge and no commit
  access, ever — that is the pattern that has compromised other projects, and the answer is no
  regardless of how much history a contributor has.
- Rejections are public and dated, with the reason.
- Contributions are only accepted for **slots that are currently open**. Everything else is
  closed by default; propose a new slot as an idea first.
- **A pack cannot switch another pack off.** `retract` is honoured only for packs in a user's
  own vault, never for a contributed or core pack — so a merged contribution can add a
  definition but can never retire someone else's. A user, on their own machine, can retract
  anything including yours; that is theirs to decide and it is reported to them as `RETRACTED`
  rather than silently applied.
- Merged packs are labelled `core` or `community`. Community packs may be removed in any
  release without a deprecation window.
- Once merged, the pack is Golden Thread's to maintain. MIT grants for released work are
  irrevocable; attribution can be changed to a pseudonym on request.

Credit goes in `CONTRIBUTORS.md` and the release notes.
