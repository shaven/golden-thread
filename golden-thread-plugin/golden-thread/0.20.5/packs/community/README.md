# packs/community — merged community contributions

This tier holds definition packs contributed by others and merged after review
(`../../../../SUBMISSIONS.md`). It ships empty until the first contribution lands.

**This file must stay.** The registry treats a missing `packs/community/` as a fault —
renaming a release pack directory is how a slot can be emptied in silence — and git does not
track an empty directory. Before 0.17.4 this folder was empty, so every copy made through git
(gt-src, and any repository that commits it) arrived without it, and gt refused to load its
definitions. Only `*.pack.json` files here are read; this README is not a pack.
