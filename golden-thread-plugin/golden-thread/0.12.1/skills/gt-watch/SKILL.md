---
name: gt-watch
description: "Watch any git repo the user depends on and raise a P0 when it ships a security fix. Commands: add <git-url>, remove <slug>, list, check (fetch now), show <slug> (explain the queued changes), ack [<slug>] (mark seen). Use when the user says: watch a repo, track a repo, follow this library, is anything new upstream, what changed upstream, security fix upstream, stop watching X, mark the watch seen."
---

# Golden Thread Watch

A watch is a note in the vault, `Projects/golden-thread/watches/<slug>.md`, made from
`templates/watch.md`; its frontmatter says what to watch (url, label, track, branch,
watch_paths, current_version, p0_when, gh_repo). A cron job (`gt_watch.py fetch`) asks
each remote what changed and queues classified events outside the vault, in
`$GT_WATCH_STATE` or `~/.claude/golden-thread/watch/`. The SessionStart hook reports
the queue — P0s first. This skill is the interactive side.

Script: `<base>/../../scripts/gt_watch.py` (installed copy:
`~/.claude/golden-thread/hooks/gt_watch.py` — use that one for `install-cron`, because
its path survives version bumps). Vault: `$GT_VAULT` if set, else `vault_path` in
`~/.claude/vault-config.json`. Pass the environment through unchanged — the demo sets
`GT_VAULT`, `GT_WATCH` and `GT_WATCH_STATE` and every command must honour them.

**Severity is decided by the script's rules, never by you.** P0: a new security
advisory; a new tag or release carrying a CVE/GHSA id or "security fix/release/update"
or "vulnerability"; a commit carrying a CVE/GHSA id; a change to SECURITY.md or a
watch_path that says one of those phrases; a watch's own `p0_when` pattern. Do not
promote or demote an event because its prose sounds urgent or harmless — explain it,
and let the user override by editing the watch note.

## add <git-url>

1. Ask (once, together) for a label and anything non-default: which kinds to track
   (commits, tags, releases — default all), watch_paths, the version in use here.
   Accept "defaults" and move on.
2. Run `python3 <script> add <url> --label "<label>" [--track tags,commits]
   [--watch-path src/auth/] [--current-version 2.4.0] [--p0-when "<regex>"]`.
   It verifies the URL with `git ls-remote` (an unreachable URL is refused with the
   reason — relay it), writes the note, and takes a baseline so only changes AFTER
   now are reported.
3. If it says the `watch` setting is off, offer to switch it on
   (`python3 <base>/../../scripts/gt_settings.py set watch report`) and to install the
   hourly fetch (`python3 ~/.claude/golden-thread/hooks/gt_watch.py install-cron --every 1h`).
   Do either only on a yes. Skip this step when `GT_WATCH` is set in the environment
   (a demo) — it must not change the user's settings or crontab.

## remove <slug>

`python3 <script> remove <slug>`. Offer `uninstall-cron` only if no watches remain.

## list

`python3 <script> list` — show it as is.

## check

`python3 <script> check` fetches now, whatever the setting, then show
`python3 <script> show`.

## show [<slug>]

1. Run `python3 <script> show <slug>` (add `--all` for already-acknowledged events).
2. For each P0 and review event, read what changed: the cache path printed at the end
   is a bare clone — `git -C <cache> show --stat <sha>`, `git -C <cache> log -1 <sha>`,
   `git -C <cache> for-each-ref refs/tags/<tag> --format='%(contents)'`.
3. Explain in plain words: what changed, why the rule fired (quote the reason), and
   what it means for the user's projects that depend on it (check the watch note's
   body and `current_version`).
4. **On a P0**, file it in `<vault>/INBOX.md` as one line, under a vault claim like
   any other write (register the session and claim `INBOX.md` first):
   `- [ ] P0 upstream: <label> <ref> — <reason> (/gt:gt-watch show <slug>)`
   Say that it was filed. Do not file review or routine events.
5. Offer to `ack` what was shown.

## ack [<slug>]

`python3 <script> ack [<slug>]` marks queued events seen; the hook stops reporting them
and a later change is reported again. It never touches what the fetch has seen.

## Rules

- Never write `state.json` or `events.jsonl` yourself — only the fetch does. `ack` is
  the only thing that writes `acked.json`.
- Never put watch state in the vault; only the watch notes live there.
- Watch notes are ordinary notes — the user may edit their frontmatter in Obsidian.
