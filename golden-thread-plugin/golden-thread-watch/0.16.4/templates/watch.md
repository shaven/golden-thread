---
url: https://github.com/example/widget-lib.git
label: Widget library
track: [tags, releases, commits]   # any of: commits, tags, releases
branch:                            # empty: the remote's HEAD
watch_paths: [SECURITY.md]         # commits touching these are reported for review
current_version:                   # the version in use here, if any (e.g. 2.4.0)
p0_when: []                        # extra watch-specific P0 patterns (regex), e.g. ["token (leak|exposure)"]
gh_repo:                           # owner/repo for releases and advisories via `gh`, when the url is a mirror
starred: false
added:
---
# Widget library

A standing watch, created by `/gt-watch:gt-watch add`. The frontmatter says what to watch;
this body is free notes — why it matters here, which projects depend on it, what to do
when it ships a fix.

Machine state (what was last seen, the event queue, the read cursor) lives outside the
vault in `~/.claude/golden-thread/watch/`. Delete this note, or run
`/gt-watch:gt-watch remove <slug>`, to stop watching.
