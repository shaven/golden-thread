---
name: gt-validation
description: "Record what a validation established about a file — what was verified, what could not be determined — stamped with the file's content hash, so the definition goes visibly stale the moment the file changes."
---

# Golden Thread Validation Receipts

A completed validation writes the file's definition. Edit the file and the definition expires.

## Why

Every serious defect found in 0.16.0 was **a claim that outlived its implementation**: a
manifest row shape that stopped matching, a `lint` check listed as running while wired to
nothing, an aggregator counting installed rather than declared members. Each was true when
written. Nothing tied the claim to the code's current state, so nothing could notice when it
stopped being true.

A docstring is a claim. This is a claim with an expiry date attached to the bytes it describes.

## Steps

**Step 1 — Run the validation first**

This records a result; it does not produce one. Use `/gt:gt-validate` — an isolated validator
that re-derives the behaviour from the artifact rather than reviewing your reasoning.

**Step 2 — Record what it established**

```bash
python3 <base_dir>/../../scripts/gt_validation.py record \
    --file <path to the file> --verdict holds \
    --checked "each property that was verified, one flag per property" \
    --gap "each thing the validation could NOT determine" \
    --by "who or what ran it, and when"
```

`--verdict` is `holds`, `broken` or `cannot-verify`.

**Step 3 — `--gap` is not optional in spirit**

A receipt that flattens to "validated ✓" manufactures exactly the assurance this system keeps
having to dig back out. Record what was *not* established, in the artefact, permanently. A
validation that genuinely left nothing undetermined is unusual — if that is really so, say so
explicitly rather than by omission.

**Step 4 — Read it back when you rely on it**

```bash
gt_validation.py check --file F     # 0 covered · 1 stale · 2 never validated
gt_validation.py show  --file F     # the definition, with its gaps
gt_validation.py list               # everything, and what has gone stale
```

**Exit 2 means unknown, not clean.** A file nobody validated is not a file that passed, and
`check` keeps those two states apart deliberately — the same rule as "a member that could not
run is not a pass".

## Rules

- **Never record a receipt for a validation you did not actually run.** The receipt's only value
  is that it reports something established; a recorded assumption is worse than no record, since
  it looks like evidence.
- **Never quietly re-record to clear a stale one.** Stale means the file changed after it was
  validated — the answer is to validate it again, not to re-stamp it.
- **Say the state when you cite a definition**: `covered`, `stale` or `never`. Citing a stale
  receipt as current is the failure this exists to prevent.
- The ledger (`dev/validations.jsonl`) is in the repo on purpose: a validation describes what the
  code does, which travels with the code. Test receipts are machine-local for the opposite
  reason — they attest that a tree passed *here*.
