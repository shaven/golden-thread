---
name: gt-task
description: "Deprecated alias, removed after 0.18.x — use /gt:gt-create task. Kept working for one release: it does exactly what /gt:gt-create task does (create a task)."
model_intent: balanced
---

# gt-task (deprecated alias)

**First, print this one line to the user, verbatim:**

> Note: gt-task is deprecated. Use /gt:gt-create task instead.

Then do exactly what `/gt:gt-create task` does: read `<base_dir>/../gt-create/SKILL.md` — `<base_dir>` is the
path in this skill's `Base directory for this skill:` header. Take the vault location and the
tool paths from the top of that file, then follow its
**`## Task`** section, with whatever the user passed to this command as that section's
argument. That section is this command's procedure, moved there unchanged in 0.18.1
(verb-first vocabulary: one verb per action, the artifact as its argument), so the result is the
same. Skip that file's sections for the other artifacts.

This alias has no trigger phrases of its own — they moved to `gt-create` — and it is removed in the
release after 0.18.x.
