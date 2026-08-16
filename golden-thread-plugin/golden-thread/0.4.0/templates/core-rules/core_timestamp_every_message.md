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

**Tier:** Core / Validated (see [[core_rule_priority_model]]). Core because it must
hold on every turn everywhere; Validated because it is trivially checkable and must
not depend on in-the-moment discipline.

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
