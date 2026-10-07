---
name: gt-runbook-lint
description: "Scan all project runbooks for content that has drifted into multiple runbooks and should be promoted to a shared layer. Use when the user says: lint runbooks, check runbooks for drift, scan for duplication across runbooks, find graduation candidates in runbooks."
model_intent: balanced
---

# Golden Thread Runbook Lint

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_lint.py --vault … --runbooks --json` | `vault_report` `{report: runbook_lint}` |
| `gt_write_queue.py …` | `vault_queue_write` — the same `path`, `op`, `section`, `key` and `hint`, with the text itself as `content` (no `--session`: the server stamps its own). The broker decides at once and the result says apply, held or escalate. `design.md` and `global-memory/` are refused (owner only) |
| `gt_broker.py drain` | nothing to run after `vault_queue_write`; `vault_queue_drain` if anything was queued from the shell |
| `gt_log.py … add "<line>"` | `vault_log_add` `{line}` — the full line shape; with `--event …`, the same fields (`event`, `item`, `from`, `to`, `level_from`, `level_to`, `project`) |

Repetition across runbooks means a fact is general — the exact problem the PROTOCOL.md layer and Knowledge pages exist to prevent. Detect duplication, classify it, route it to the right layer via `/gt:gt-promote`.

## Vault location

Use `$GT_VAULT` if it is set (a session pinned to one vault, such as the demo); otherwise read `~/.claude/vault-config.json` for `vault_path`. If missing → tell the user to run `/gt:gt-init` first.

## Steps

**Step 1 — Run the detector**

The script is at `<base_dir>/../../scripts/gt_lint.py`, where `<base_dir>` is the path shown in the `Base directory for this skill:` header. Run:
```bash
python3 <base_dir>/../../scripts/gt_lint.py --vault "<vault>" --runbooks --json
```

It is read-only. It collects every `Projects/**/runbook.md` (sub-projects included), normalises each line (whitespace collapsed; bullets, numbering and code-fence markers dropped; headings and lines under 25 characters ignored) and reports each cluster of identical or near-identical lines (difflib ratio ≥ 0.9) that appears in 2 or more runbooks — once, with every `path` and `line`. Exit 0 = nothing duplicated, 1 = clusters found.

If the output says `nothing to compare` (fewer than 2 runbooks) → report "Fewer than 2 runbooks found — nothing to compare." and stop.

**Step 2 — Widen each cluster into content**

The detector matches lines, not intent. For each `runbook-duplicate` finding, read the surrounding section in every listed runbook and decide what the repeated *content* is — a whole procedure, a setup step, a constraint or a config snippet. Adjacent clusters from the same pair of runbooks usually belong to one block; merge them. Also skim for same-intent content worded too differently for the detector to catch, and add it as a cluster if you find it.

**Step 3 — Classify each cluster**

For each cluster, determine the right destination using this routing table:

| Content type | Destination | Promote via |
|---|---|---|
| Process rule (how work gets done across all projects) | `Projects/PROTOCOL.md` | `/gt:gt-promote` path: PROTOCOL.md |
| Repo/service fact (how a specific system works) | That project's `decisions.md` or `<repo>/CLAUDE.md` | `/gt:gt-promote` path: repo |
| Platform or infra knowledge (applies to all projects on this stack) | `Knowledge/<page>.md` | `/gt:gt-promote` path: Knowledge |
| Coincidental phrasing (genuinely different content, similar wording) | Leave in place | Note as false positive |

**Step 4 — Present clusters for approval**

For each cluster:
- Show the duplicated content (from the runbooks it appeared in)
- State your classification and proposed destination
- Ask: "Promote this to `<destination>`, or leave in place?"

**Step 5 — Execute approved promotions**

Route approved clusters through `/gt:gt-promote` with the appropriate path. `gt-promote` handles the mechanics (writing the generalized rule, cross-linking, log entry).

After promotion, remove or replace the duplicated lines in each source runbook with a reference to where the rule now lives:
```markdown
> See: [[Knowledge/Page]] or Projects/PROTOCOL.md § Section
```

Ask before modifying each runbook. A runbook is vault content, so the change goes through the write queue (Core rule 1), never Write, Edit or a shell redirect: write the section's new body — the duplicated lines replaced by the reference — to a scratch file outside the vault and queue it:
```bash
python3 <base_dir>/../../scripts/gt_write_queue.py --vault "<vault>" \
  --path "Projects/<slug>/runbook.md" --op replace-section --section "<heading>" \
  --content-file <scratch>/<slug>-runbook.md --hint "runbook-lint: promoted to <destination>"
```
Duplicated lines above the runbook's first `## ` heading go as `--op replace-file` instead: read the file, build its full new text, and queue that (no `--section`).
When every approved runbook is queued, apply them once and tell the user in one line what the broker applied, held or escalated:
```bash
python3 <base_dir>/../../scripts/gt_broker.py drain --vault "<vault>"
```

**Step 6 — Log**

Record the run with the tool — never append to `log.md` by hand:
```bash
python3 "<vault>/Projects/golden-thread/tools/gt_log.py" --vault "<vault>" add \
  "<today> [runbook-lint] N clusters found, M promoted, P left in place (false positives)"
```

## Rules

- `gt_lint.py --runbooks` only detects and writes nothing — never auto-edit a runbook from its report
- A cluster is not automatically a defect — similar phrasing about genuinely different facts is a false positive; name it and move on
- Re-run after promotions to confirm the overlap is gone
- One-off operational steps that are project-specific are correct to stay in the runbook — only promote things that are genuinely generalizable
