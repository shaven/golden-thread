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

**Begin every response with the current wall-clock timestamp.** It is the first text you write this turn, including any line before your first tool call. Pull the real current
time from the environment (the injected `Current date and time:` line, or `date` via
Bash) — never guess. Format short and clear, e.g. `2026-08-16 15:14 CDT`.
Applies to EVERY chat and project, on EVERY turn — no exceptions.

**What the `Stop` hook checks (0.20.2): the final answer must open with the stamp.**
The final answer is the last text of the turn — the part the user reads — and its first
characters are the timestamp. A turn is often text → tool_use → more text; the closing
text is the one that must begin with it. A stamped opening also passes: if the first text
block of the turn begins with the timestamp, the turn is accepted whatever the closing
text starts with. Either one satisfies the rule; a turn where neither opens with the
stamp is blocked. The safe habit is to stamp both — a one-line preamble before your first
tool call is the first text you write, so give it the stamp too, and open the final
answer with it again — but neither is required when the other is present.

**A tool call to obtain the correct time is not a violation; guessing to avoid one
is.** The constraint is on *text you emit*, so `date` may be run first — and must be,
when no `Current date and time:` value was injected (hook-feedback turns, for
instance, carry none). Silent tool calls do not "begin" a reply; words do.

**Never carry a timestamp forward or estimate elapsed time.** If work in the turn
took a while, that does not license adjusting the number — either use the injected
value verbatim or run `date` and use what it returns.

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
sits at the top of every single reply. So if the timestamp stops appearing, the hook
itself has stopped running — unwired, script moved, or `settings.json` overwritten.
Every other Core rule fails *silently*; this one fails *loudly*. **But only for that
one failure mode** — see the correction below.

**Corrected 2026-08-18 — it is not a complete health check.** On 2026-08-17 a Dropbox
file-provider mount dropped, the vault became unreachable, and rules 2 and 3 stopped
being injected for roughly twenty hours. **The timestamp kept appearing the whole
time**, so nothing looked wrong. The injector had a hardcoded fallback that emitted
the timestamp when no rules could be loaded — the canary had a privileged survival
path, which is exactly backwards for a health indicator.

A canary detects *total* hook failure. It cannot detect *partial* rule loss, and
partial loss is the more likely failure because it happens whenever the vault moves.
The injector now emits an explicit `ENFORCEMENT DEGRADED` banner in that state, with
the reason, so the gap is visible on the first turn rather than after a day. Without it, a broken
`UserPromptSubmit` hook could go unnoticed for weeks while the rules it was supposed to
be re-asserting quietly stopped applying — precisely the failure the Core tier exists
to prevent.

Being a canary is a legitimate standing reason for **designation**, independent of the
three gate questions. See "Governance" in [[core_rule_priority_model]].

**Enforcement mechanisms:**
- *Reminder:* the `UserPromptSubmit` hook already injects the current date/time — its
  text should be an imperative ("Begin your reply with this exact timestamp line").
- *Validated:* a `Stop`/output hook should reject/annotate any reply whose final answer
  (and first text) does not begin with a timestamp. This is the un-losable form.

**Why:** a rule stored without a tier or an enforcement mechanism can silently drift
out of application over a long session — the exact failure the priority model exists
to prevent. The user runs long sessions with rapid-fire questions and needs to see
when each answer was current relative to real-world/market time.

**How to apply:**
- Open the final answer with the timestamp. If you also write text before a tool call,
  stamping that opening passes on its own, but the final answer is the one the user reads.
- Use the injected `Current date and time:` value verbatim, or run `date` if none was
  injected. Do not estimate, and do not adjust for time spent working in the turn.
- For a long turn spanning background work, the value is still the one you were given
  or the one `date` returns when you check — never an interpolation between them.

**Known failure mode (observed 2026-08-16, four times in one session):** reacting to
tool output — or writing a lead-in sentence — before the timestamp, then closing with a
summary that is not stamped either. Before 0.20.2 the hook read only the first text block,
so stamping the closing summary alone was blocked even though the user saw a stamped
reply; since 0.20.2 a stamped final answer satisfies the rule on its own. A reply where
neither the first text nor the final answer begins with a timestamp is still blocked.

The pattern is a **reflex**, not an oversight: the pull is to respond to what a tool
just returned before addressing the user. That is why the imperative alone does not
reach it.

**Four for four.** The Reminder tier did not prevent a single one of the four, despite
the imperative being injected on every turn and rewritten twice to be more explicit —
including once *immediately before* the next violation. The `Stop` hook caught all
four. This is the clearest evidence in the vault for why a rule is designated
Core/**Validated** rather than Core/Reminder: a more emphatic reminder is not the
lever. Do not "fix" this by rewording the injection again.

**What the validator does not catch:** it checks the *shape* of the timestamp, not its
truth. A well-formed but fabricated value passes. That is an accepted limit of a cheap
mechanical check — the substance still depends on following the paragraph above.
