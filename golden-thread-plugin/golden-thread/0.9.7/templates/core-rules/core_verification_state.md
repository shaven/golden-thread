---
name: core_verification_state
description: "State the verification state of every derived figure presented as fact."
metadata:
  type: core
  level: core
  enforcement: reminder
---

# State the verification state of every derived figure

**Label every derived figure you present as fact with its verification state —
`unverified`, `self-verified`, or `independently verified`.**

A derived figure is any number produced by analysis, computation or measurement.

- `unverified` — computed once, not checked
- `self-verified` — re-derived by the same session, which shares its own assumptions
- `independently verified` — re-derived by a fresh-context validator that never saw
  the reasoning

Applies to figures that inform a decision. It does not apply to incidental counts
("47 open tasks") or to values quoted directly from a source with the source named.

## Why this is Core

You cannot mechanically force verification to happen. **You can make its absence
visible.** That is the same principle that keeps `core_timestamp_every_message` in this
tier: the rule is cheap, it is seen constantly, and its absence is obvious.

Unverified numbers do not announce themselves. They arrive with the same confident
formatting as verified ones, and the reader has no way to tell them apart. Labelling is
what restores that distinction.

## The three-question gate

1. **Correctness — yes.** On 2026-08-21/22 a session presented a model as "broken"
   from a figure benchmarked against a rejected default config, and recommended a
   production change (Option B) that independent validation later refuted on three of
   four tickers. Every one of those figures would have been labelled `unverified`.
2. **Cost — yes.** The same session came within one command of a wholesale-replace that
   would have destroyed seven weeks of production rows, on the strength of an
   unverified read of a script's applicability.
3. **Cascade — yes.** Unverified figures were written into `decisions.md` and
   `research.md` as fact, where later work then built on them. That is the promotion
   ladder carrying an error upward.

## Enforcement — `reminder`, and honestly so

**This rule is NOT mechanically validated, and must not be described as if it were.**

`core_no_secrets_in_transcript` can be `validated` because credentials have a
recognisable shape. "An unverified number presented as fact" has no such shape — a
check would either fire on every reply containing a digit or miss the real cases. A
`validated` claim here would be exactly the "in context is not applied" failure this
tier exists to close, wearing the badge of the fix.

**What would make it `validated` later:** a required provenance token emitted alongside
any figure written into `decisions.md` / `design.md` / `research.md`. The write is a
narrow, detectable event in a way that conversational prose is not. That is the version
to build.

## Related

[[golden-thread/validation-agents]] — the validator this rule refers to.
`PROTOCOL.md` → "Verification before promotion" — the process half.
Per-project invariants live in each project's `validation-rules.md`.
