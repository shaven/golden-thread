# Contributing a pack to Golden Thread

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

`READY` means send it. Anything else prints the exact reason — fix and re-run. The validator
runs before a human reads anything, so a rejected pack costs nobody any time.

---

## What a pack is

A pack is **one JSON file**: a manifest plus a list of entries. It is data, not a program.
A reviewer can read the whole thing, and `git diff` shows exactly what changed on an update.

```json
{
  "schema": 1,
  "slot": "secrets",
  "name": "cloud-keys",
  "tier": "A",
  "spdx": "MIT",
  "provenance": {
    "origin": "adapted",
    "contributor": "A Dev <dev@example.com>",
    "upstream": { "name": "gitleaks", "version": "8.18.0", "spdx": "MIT" }
  },
  "dco": "Signed-off-by: A Dev <dev@example.com>",
  "entries": [
    { "id": "aws-access-key", "pattern": "AKIA[0-9A-Z]{16}" }
  ]
}
```

Name the file `<slot>.<name>.pack.json`.

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
(inbound = outbound). Copyleft cannot be merged: GPL, AGPL and MPL are refused, including for
anything you adapt. If you are adapting a secret-scanning ruleset, adapt **gitleaks** (MIT),
not TruffleHog (AGPL).

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
- Merged packs are labelled `core` or `community`. Community packs may be removed in any
  release without a deprecation window.
- Once merged, the pack is Golden Thread's to maintain. MIT grants for released work are
  irrevocable; attribution can be changed to a pseudonym on request.

Credit goes in `CONTRIBUTORS.md` and the release notes.
