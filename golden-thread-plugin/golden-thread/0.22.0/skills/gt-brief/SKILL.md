---
name: gt-brief
description: "Draft a self-contained CLAUDE.md section for a project's code repo from its vault notes — what it is, the stable decisions, where it runs, what not to do — printed for review, never written. Use when the user says: /gt-brief, brief the repo, draft the repo CLAUDE.md, graduate this project to its repo, what should the repo's CLAUDE.md say."
plain:
  - cmd: "/gt:gt-brief"
    does: "Drafts the CLAUDE.md section a project's code repo should carry, from your vault notes."
    how: "Reads the project's notes and prints a draft for you to review. It writes nothing."
    when: "A project is moving from the vault into its own code repository."
    next: "/gt:gt-promote"
    hear: "brief the repo|draft the repo claude.md|graduate this project to its repo"
model_intent: balanced
---

# Golden Thread — Brief a repo

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_brief.py --vault … <slug>` | `vault_report` `{report: brief, project}`; read the code repo (`--repo`) from the shell |
| `gt_write_queue.py …` | `vault_queue_write` — the same `path`, `op`, `section`, `key` and `hint`, with the text itself as `content` (no `--session`: the server stamps its own). The broker decides at once and the result says apply, held or escalate. `design.md` and `global-memory/` are refused (owner only) |
| `gt_broker.py drain` | nothing to run after `vault_queue_write`; `vault_queue_drain` if anything was queued from the shell |
| `gt_log.py … add "<line>"` | `vault_log_add` `{line}` — the full line shape; with `--event …`, the same fields (`event`, `item`, `from`, `to`, `level_from`, `level_to`, `project`) |

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

The rules for what a repo `CLAUDE.md` holds (they lived in a comment inside the scaffold
`gt-create` wrote until 0.21.4; the scaffold is now a short stub that points here):

- **It leaves the vault.** It is committed to the repo root, where every session working in that
  code reads it with no plugin, no configuration and no vault. It is the only channel that costs
  the reader nothing.
- **Self-contained, absolutely.** Everything above the optional *Deeper context* trailer must give
  full value to a reader with no vault. A line that only makes sense to someone who has seen the
  vault belongs in `design.md` or `research.md`, not here.
- **How to work in the code.** The real command, the environment trick, the structural landmine,
  the file that must never be copied between hosts: facts about the code, not notes about it.
- **Only what has stopped changing,** and passes the teammate test. A fact still in motion is not
  ready to publish.
- **Layering.** When the project is one area of a larger repo, the repo root carries its own
  `CLAUDE.md` with the shared rules; Claude Code reads both cumulatively, so a shared rule is
  stated once at the root and never repeated here.
- **The usual shape:** one or two sentences on what it is and what someone changing it can break;
  `## Layout` (a path table, each row `static` — byte-identical everywhere, must reach every
  target — or `unique` — a per-host variant, never cross-deploy); `## Landmines` (what is not
  obvious from the code and cost real time or damage — a named file, a named failure);
  `## Checks` (the command that actually verifies a change); then the *Deeper context* trailer.

**4 — Place it, only on the user's yes.** The destination is `Projects/<path>/CLAUDE.md` in the
vault (committed to the repo root), so the write goes through the write queue like any vault
write:

```bash
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" --path "Projects/<path>/CLAUDE.md" \
    --op <create|replace-file> --content-file <file holding the reviewed text> --session <session id> --hint "gt-brief"
python3 <base_dir>/../../scripts/gt_broker.py drain --vault "<vault>"
```

`create` when the project has no `CLAUDE.md` yet, `replace-file` with the file's whole new text
when it has one. gt_brief's last stderr line says which: absent, *still the unfilled scaffold*
(the stub `gt-create` writes, or the long placeholder scaffold of a project created before
0.21.4 — replace it whole), or *hand-written* — then merge the reviewed section into it rather
than replacing it. If the repo is a separate checkout, tell the user the path to copy it to; never
write into a code repo from here. Log it with the `graduate` verb, naming the repo from
`source.md`:

```bash
python3 "<vault>/Projects/golden-thread/tools/gt_log.py" --vault "<vault>" add \
    '<today> [graduate] Projects/<path>/ → <repo>/CLAUDE.md: briefed <N> constraints'
```
