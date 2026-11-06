---
name: gt-sync
description: "Keep the vault in step with its git remote: show ahead and behind, pull (fast-forward only), or push this machine's session commits after the push check. Use when the user says: sync the vault, pull the vault, push the vault, is the vault up to date, get the latest vault, publish my vault commits."
plain:
  - cmd: "/gt:gt-sync"
    does: "Keeps your vault in step with its git remote across machines."
    how: "Shows how far ahead or behind you are, pulls newer knowledge from another machine (never a merge), or pushes this machine's commits after the push check passes."
    when: "You work on more than one machine."
    next: "/gt:gt-doctor"
    hear: "sync the vault|pull the vault|push the vault"
model_intent: fast
---

# Golden Thread Sync

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_sync.py status` / `behind` / `pull` / `push` | **terminal**: they run git in the vault (denied to the shell) and reach the network |

A **terminal** row is a step sandbox mode refuses on purpose — it changes settings or install state, or rewrites the vault wholesale. Give the user the exact command to run from a terminal, and carry on with the rest.

**When `.git` is not writable** (the sandbox blocks writes to the vault's `.git`, so a fetch, pull or push fails with `Operation not permitted`): do not retry, and do not work around it with another route. Print the command for what the user asked, naming the vault, for them to run:

```
run this from a terminal:
git -C "<vault>" fetch
git -C "<vault>" pull --ff-only
git -C "<vault>" push
```

Print only the lines for the step asked: `fetch` for status or behind, `pull --ff-only` for a pull, `push` for a push after the push check has reported the vault ahead of a real upstream. Never print a pull without `--ff-only`.

The vault is a git repo so that what one machine learns reaches the others. This skill moves it
in both directions. It never merges and never rebases: when both sides have commits, it stops and
says so, and the owner reconciles.

## Vault location

Use `$GT_VAULT` if it is set; otherwise read `vault_path` from `~/.claude/vault-config.json`. Pass
it explicitly every time (`--vault "<vault>"`). Every git call the script makes is
`git -C <vault>`.

The script is at `<base_dir>/../../scripts/gt_sync.py`, where `<base_dir>` is the path in the
`Base directory for this skill:` header.

## Which verb

| The user wants | Run |
|---|---|
| where the vault stands (default, bare `/gt:gt-sync`) | `gt_sync.py status --vault "<vault>"` |
| the newest vault from another machine, before work | `gt_sync.py pull --vault "<vault>"` |
| this machine's commits on origin, after `/gt:gt-work` | `gt_sync.py push --vault "<vault>"` |

Add `--dry-run` to rehearse: no fetch, no pull, no push, only what the refs on disk say and the
command it would run.

## Reading the result

- **status**: `N ahead, M behind; K uncommitted change(s); last fetch …`. Behind → offer `pull`.
  Ahead → offer `push`. Both → say DIVERGED and stop; do not attempt to fix it.
- **pull**:
  - Exit 0 means it fast-forwarded (it names the file count and the newest commit) or was
    already up to date.
  - Exit 1 `STOPPED -- … DIVERGED` means local and remote both have commits. Show the user the
    two counts and leave the reconciliation to them. Never run `git merge`, `git rebase` or
    `git pull` without `--ff-only` yourself.
  - Exit 1 `git refused the fast-forward` usually means uncommitted local edits touch the
    incoming files. Say which.
- **push**:
  - It runs the push check first and refuses unless the check reports the vault AHEAD of a real
    upstream. No upstream, no remote and detached HEAD are all refusals, shown with the push
    check's own text.
  - It also refuses when origin has commits this machine lacks. Pull first.
  - Exit 0 means pushed, or nothing to push.
  - Pushing is outward-facing. Run it when the user asked for a push, or after `/gt:gt-work`
    when the user agrees. Never push on your own initiative.

## Session start

The opt-in `sync_check` setting (`off` by default) adds one line at session start, printed by
the push check:
- `cached` compares with the remote refs already on disk. It uses no network and says how old
  they are.
- `fetch` runs one fetch, bounded to a few seconds, before comparing.

Turn it on with `/gt:gt-settings`. The same line on demand: `gt_sync.py behind --vault "<vault>"`.

## Exit codes

`0` ok or nothing to do · `1` refused or git failed (diverged, push check failed, behind on push,
fast-forward refused) · `2` usage, no vault, or not a git repository.
