---
name: gt-handoff-handle
description: "Deprecated alias — use /gt:gt-handle handoff. Still works, and will be removed in a future release: it does exactly what /gt:gt-handle handoff does (work through the waiting handoffs)."
model_intent: balanced
---

# gt-handoff-handle (deprecated alias)

**First, print this one line to the user, verbatim:**

> Note: gt-handoff-handle is deprecated. Use /gt:gt-handle handoff instead.

Then do exactly what `/gt:gt-handle handoff` does: read `<base_dir>/../gt-handle/SKILL.md` — `<base_dir>` is the
path in this skill's `Base directory for this skill:` header. Take the vault location and the
tool paths from the top of that file, then follow its
**`## Handoffs`** section, with whatever the user passed to this command as that section's
argument. That section is this command's procedure, moved there unchanged in 0.18.1
(verb-first vocabulary: one verb per action, the artifact as its argument), so the result is the
same. Skip that file's sections for the other artifacts.

This alias has no trigger phrases of its own — they moved to `gt-handle` — and it will be removed in a
future release.
