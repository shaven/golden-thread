---
name: core_global_memory_scope
description: "CORE rule — global-memory/ holds ONLY facts needed in ALL projects; project-specific facts go in Projects/<slug>/memory/, never here."
metadata:
  node_type: memory
  type: core
  level: core
  enforcement: reminder
  promoted: 2026-08-16
---

**`global-memory/` contains only facts needed in EVERY project.** Anything specific
to one project belongs in `Projects/<slug>/memory/`, not here. This keeps the
always-loaded global tier small and universal — which is itself what keeps Core rules
reliable (a bloated global tier dilutes attention on every rule in it).

**Tier:** Core / Reminder (see [[core_rule_priority_model]]). Core because it governs
memory authoring in every session; Reminder because it is a judgment call at write
time, not mechanically checkable per-turn.

**Why:** part of the Golden Thread since the global CLAUDE.md was written. Promoted
to an explicit Core-tier file 2026-08-16 so the *scope discipline* is captured in the
golden-thread project itself, not only in CLAUDE.md prose.

**How to apply:**
- Before writing a `global-memory/` file, ask: "Is this true and useful in ALL
  projects?" If not, route it to the owning project's memory.
- When a new topic appears, ask which project it belongs to before creating memory
  (see [[feedback_ask_project_for_new_topics]]).
