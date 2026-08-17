---
name: core_timestamp_every_message
description: "CORE rule — begin EVERY response with the current wall-clock timestamp. Un-removable; applies to all chats, all projects, every turn."
metadata:
  node_type: memory
  type: core
  level: core
  enforcement: validated
  supersedes: feedback_timestamp_every_message
---

**Begin every response with the current wall-clock timestamp.** Pull the real
current time from the environment (the injected `Current date and time:` line, or
`date` via Bash) — never guess. Format short and clear, e.g. `2026-08-16 15:14 CDT`.
Applies to EVERY chat and project, on EVERY turn — no exceptions.

**Tier:** Core / Validated (see [[core_rule_priority_model]]). Core **by designation**;
Validated because it is trivially checkable and must not depend on in-the-moment
discipline.

## This rule is the canary — do not demote it

It answers **NO to all three promotion-gate questions**: an absent timestamp writes no
bad code, costs no rework, and corrupts nothing at the lower five levels. Judged on
consequence alone it would never qualify for Core. It is Core anyway, deliberately, and
a future session must not "helpfully" demote it for failing a test it was never meant
to take.

Its value is **observability of the enforcement mechanism itself**, which no
consequence-based test can measure. The rule is small, cheap, and *highly visible* — it
sits at the top of every single reply. So the moment the timestamp stops appearing,
that is an immediate, unmistakable signal that **enforcement has broken**: the hook is
unwired, the script moved, the vault is unreachable, or `settings.json` was
overwritten. Every other Core rule fails *silently*. This one fails *loudly*.

That makes it the health check for the entire tier. Without it, a broken
`UserPromptSubmit` hook could go unnoticed for weeks while the rules it was supposed to
be re-asserting quietly stopped applying — precisely the failure the Core tier exists
to prevent.

Being a canary is a legitimate standing reason for **designation**, independent of the
three gate questions. See "Governance" in [[core_rule_priority_model]].

**Enforcement mechanisms:**
- *Reminder:* the `UserPromptSubmit` hook already injects the current date/time — its
  text should be an imperative ("Begin your reply with this exact timestamp line").
- *Validated:* a `Stop`/output hook should reject/annotate any reply that does not
  begin with a timestamp. This is the un-losable form.

**Why:** a rule stored without a tier or an enforcement mechanism can silently drift
out of application over a long session — the exact failure the priority model exists
to prevent. The user runs long sessions with rapid-fire questions and needs to see
when each answer was current relative to real-world/market time.

**How to apply:**
- Timestamp at the start of each response; for long responses spanning background
  work, timestamp the update when it is actually posted, not when the task started.
- Do not estimate — use the real clock.
