---
name: gt-task-list
description: "Deprecated alias — use /gt:gt-list tasks. Still works, and will be removed in a future release: it does exactly what /gt:gt-list tasks does (show open tasks, filtered (read-only))."
model_intent: fast
---

# gt-task-list (deprecated alias)

**First, print this one line to the user, verbatim:**

> Note: gt-task-list is deprecated. Use /gt:gt-list tasks instead.

Then do exactly what `/gt:gt-list tasks` does: read `<base_dir>/../gt-list/SKILL.md` — `<base_dir>` is the
path in this skill's `Base directory for this skill:` header. Take the vault location and the
tool paths from the top of that file, then follow its
**`## Tasks`** section, with whatever the user passed to this command as that section's
argument. That section is this command's procedure, moved there unchanged in 0.18.1
(verb-first vocabulary: one verb per action, the artifact as its argument), so the result is the
same. Skip that file's sections for the other artifacts.

This alias has no trigger phrases of its own — they moved to `gt-list` — and it will be removed in a
future release.
