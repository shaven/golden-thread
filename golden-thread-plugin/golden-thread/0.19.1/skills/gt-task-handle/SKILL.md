---
name: gt-task-handle
description: "Deprecated alias, removed after 0.18.x — use /gt:gt-handle task. Kept working for one release: it does exactly what /gt:gt-handle task does (work through open tasks)."
model_intent: balanced
---

# gt-task-handle (deprecated alias)

**First, print this one line to the user, verbatim:**

> Note: gt-task-handle is deprecated. Use /gt:gt-handle task instead.

Then do exactly what `/gt:gt-handle task` does: read `<base_dir>/../gt-handle/SKILL.md` — `<base_dir>` is the
path in this skill's `Base directory for this skill:` header. Take the vault location and the
tool paths from the top of that file, then follow its
**`## Tasks`** section, with whatever the user passed to this command as that section's
argument. That section is this command's procedure, moved there unchanged in 0.18.1
(verb-first vocabulary: one verb per action, the artifact as its argument), so the result is the
same. Skip that file's sections for the other artifacts.

This alias has no trigger phrases of its own — they moved to `gt-handle` — and it is removed in the
release after 0.18.x.
