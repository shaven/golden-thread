# {{TITLE}}

<!--
  THIS FILE LEAVES THE VAULT.

  It is committed to this project's repo root, where every Claude Code session
  working in that code reads it automatically — no plugin, no configuration, no
  vault access. It is the only channel that costs the reader nothing.

  THE CONTENT RULE IS ABSOLUTE: everything above the optional trailing section
  must be SELF-CONTAINED. A reader with no vault gets full value. If a line only
  makes sense to someone who has seen this vault, it belongs in design.md or
  research.md instead — not here.

  What belongs here: how to WORK IN THE CODE. The real command, the environment
  trick, the structural landmine, the file that must never be copied between
  hosts. Facts about the code, not notes about the code.

  What qualifies: the fact passes the teammate test ("would this make sense to
  someone who has never heard of this vault?") AND has stopped changing. A fact
  still in motion is not ready to publish.

  LAYERING: if this project is one area of a larger repo, the repo root carries
  its own CLAUDE.md with the shared rules. Claude Code reads both cumulatively —
  so state a shared rule ONCE at the root and never repeat it here.

  Delete these comments once the file has real content.
-->

One or two sentences: what this is, and what someone changing it can break.

## Layout

| Path | What it is |
|---|---|
| `<path>` | `static` (byte-identical everywhere — must reach every target) or `unique` (per-host variant — never cross-deploy) |

## Landmines

The things that are not obvious from reading the code, and that cost real time or
real damage when missed. Be specific — a named file, a named failure.

## Checks

```bash
# the command that actually verifies a change here
```

## Deeper context, if this machine has the vault

If `~/.claude/vault-config.json` exists, read `vault_path` from it; the notes are at
`Projects/{{SLUG}}/`. **If it is absent, skip this section — everything above stands
on its own.**

| Question | File |
|---|---|
| Where this runs, which host, what deploys where | `source.md` |
| Current architecture | `design.md` |
| Why it was built this way, what was rejected | `decisions.md` |
| Dated findings and incidents | `research.md` |
| What was originally intended | `idea.md` |
