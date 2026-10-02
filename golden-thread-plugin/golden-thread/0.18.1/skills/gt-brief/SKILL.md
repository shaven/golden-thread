---
name: gt-brief
description: "Draft a self-contained CLAUDE.md section for a project's code repo from its vault notes — what it is, the stable decisions, where it runs, what not to do — printed for review, never written. Use when the user says: /gt-brief, brief the repo, draft the repo CLAUDE.md, graduate this project to its repo, what should the repo's CLAUDE.md say."
---

# Golden Thread — Brief a repo

The outward axis of PROTOCOL.md, *Graduating a fact out to a repo*, as a tool. A project's
`CLAUDE.md` is committed to its repo root, where every Claude Code session in that code reads
it with no vault and no plugin. This drafts that section from the vault; a person decides what
goes in.

## Steps

**1 — Resolve the vault and project.** `$GT_VAULT` if set, else `vault_path` from
`~/.claude/vault-config.json`. The project is the one named, or the session's project; a
sub-project's bare slug works.

**2 — Draft it.** `<base_dir>` is the path in the `Base directory for this skill:` header.

```bash
python3 <base_dir>/../../scripts/gt_brief.py --vault "<vault>" <slug> [--repo <path to the code repo>]
```

stdout is the proposed section; stderr says what was missing (no ADRs, no `source.md`, no
vision line) and, with `--repo`, where it would go. Nothing is written anywhere.

What it contains: the project in one paragraph; **Constraints** — every ADR that is neither
superseded nor declares `Expires when:` / `Expires:` (a decision with a known end is not an
invariant); **Where it runs** from `source.md`; **What not to do** — the rejected alternatives
of current ADRs, one line each; then the optional *Deeper context* trailer, located through
`~/.claude/vault-config.json`. ADR numbers, `[[wikilinks]]` and lines naming vault folders are
stripped, because the content rule is absolute: a reader with no vault gets full value.

**3 — Review it with the user.** Apply the teammate test line by line — *would this make sense
to someone who has never heard of this vault?* — and drop any constraint still in motion
(stability is the graduation signal). `runbook.md` is deliberately not read: it is the
incubator, and whether a fact there has stopped changing is the user's call. Offer to add
stable runbook facts by hand.

**4 — Place it, only on the user's yes.** The destination is `Projects/<path>/CLAUDE.md` in the
vault (committed to the repo root), so the write goes through the write queue like any vault
write:

```bash
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" --path "Projects/<path>/CLAUDE.md" \
    --op <create|replace-file> --content-file <file holding the reviewed text> --session <session id> --hint "gt-brief"
python3 <base_dir>/../../scripts/gt_broker.py drain --vault "<vault>"
```

`create` when the project has no `CLAUDE.md` yet, `replace-file` with the file's whole new text
when it has one. If the file already has hand-written content, merge the reviewed section into it rather than
replacing it. If the repo is a separate checkout, tell the user the path to copy it to; never
write into a code repo from here. Log it with the `graduate` verb, naming the repo from
`source.md`:

```bash
python3 "<vault>/Projects/golden-thread/tools/gt_log.py" --vault "<vault>" add \
    '<today> [graduate] Projects/<path>/ → <repo>/CLAUDE.md: briefed <N> constraints'
```
