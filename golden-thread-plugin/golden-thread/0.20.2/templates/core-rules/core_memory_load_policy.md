---
name: core_memory_load_policy
description: "CORE rule — load the global-memory index only when explicitly asked or when a /gt:* skill is invoked; follow the platform wiki from index.md into Knowledge/."
metadata:
  node_type: memory
  type: core
  level: core
  enforcement: reminder
  promoted: 2026-08-16
---

**Do not auto-load the full memory index.** Load
`global-memory/MEMORY.md` only when the user explicitly asks, or when a `/gt:*` skill
is invoked. For platform/domain facts, start at the vault
`index.md` and follow links into `Knowledge/` rather than pulling everything up front.

**Tier:** Core / Reminder (see [[core_rule_priority_model]]). Core because it governs
context loading in every session; Reminder because it is a behavioral gate, not
output-checkable.

**Why:** part of the Golden Thread in the global CLAUDE.md. Promoted to an explicit
Core-tier file 2026-08-16 so the loading policy is captured in the golden-thread
project. Keeping the index out of context until needed preserves attention for the
Core rules that *are* always loaded.

**How to apply:**
- Treat MEMORY.md as on-demand, not standing context.
- When a `/gt:*` skill runs, it may load the index as part of its own Context tier.
