---
name: gt-runbook-lint
description: "Scan all project runbooks for content that has drifted into multiple runbooks and should be promoted to a shared layer. Use when the user says: lint runbooks, check runbooks for drift, scan for duplication across runbooks, find graduation candidates in runbooks."
---

# Golden Thread Runbook Lint

Repetition across runbooks means a fact is general — the exact problem the PROTOCOL.md layer and Knowledge pages exist to prevent. Detect duplication, classify it, route it to the right layer via `/gt:gt-promote`.

## Vault location

Read `~/.claude/vault-config.json` for `vault_path`. If missing → tell the user to run `/gt:gt-init` first.

## Steps

**Step 1 — Collect all runbooks**

Find all `runbook.md` files in `<vault>/Projects/` (including sub-projects):
```bash
find "<vault>/Projects" -name "runbook.md" -type f
```

If fewer than 2 runbooks exist → report "Fewer than 2 runbooks found — nothing to compare." and stop.

**Step 2 — Detect repeated content**

Read each runbook and look for:
- Near-identical procedures appearing in 2 or more runbooks
- Repeated tool setup steps (same environment variables, same install commands)
- Repeated constraints or warnings (same platforms, same auth patterns)
- Repeated configuration snippets

Group similar content into clusters. A cluster is content that appears in ≥2 runbooks with the same intent, even if worded slightly differently.

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

Ask before modifying each runbook.

**Step 6 — Log**

Append to `<vault>/log.md`:
```
<today> [runbook-lint] N clusters found, M promoted, P left in place (false positives)
```

## Rules

- The script only detects — never auto-edit a runbook from a report
- A cluster is not automatically a defect — similar phrasing about genuinely different facts is a false positive; name it and move on
- Re-run after promotions to confirm the overlap is gone
- One-off operational steps that are project-specific are correct to stay in the runbook — only promote things that are genuinely generalizable
