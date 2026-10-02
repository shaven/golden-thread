---
name: gt-handoff-list
description: "Deprecated alias, removed after 0.18.x — use /gt:gt-list handoffs. Kept working for one release: it does exactly what /gt:gt-list handoffs does (show the waiting handoffs (read-only))."
---

# gt-handoff-list (deprecated alias)

**First, print this one line to the user, verbatim:**

> Note: gt-handoff-list is deprecated. Use /gt:gt-list handoffs instead.

Then do exactly what `/gt:gt-list handoffs` does: read `<base_dir>/../gt-list/SKILL.md` — `<base_dir>` is the
path in this skill's `Base directory for this skill:` header. Take the vault location and the
tool paths from the top of that file, then follow its
**`## Handoffs`** section, with whatever the user passed to this command as that section's
argument. That section is this command's procedure, moved there unchanged in 0.18.0
(verb-first vocabulary: one verb per action, the artifact as its argument), so the result is the
same. Skip that file's sections for the other artifacts.

This alias has no trigger phrases of its own — they moved to `gt-list` — and it is removed in the
release after 0.18.x.
