---
type: project
slug: demo-pizzabot
domain: demo
stage: designing
topology: local
pp: 2
tags:
  - demo
  - fictional
  - pizzabot
---

# PizzaBot 3000

> ⚠️ This is a fictional demo project. It exists purely to demonstrate Golden Thread workflows. Do not confuse with real work.

**One-line vision:** An AI-powered pizza ordering assistant that argues with you about your topping choices.

## Status Board

| Area | Status | Next Action |
|---|---|---|
| Idea | ✅ done | — |
| Research | 🔄 in progress | Decide on topping-conflict resolution algorithm |
| Design | 🔄 in progress | Finalize API contract for `/order` endpoint |
| Implementation | ⬜ not started | — |

## Tasks

- [ ] Ship the `/order` endpoint contract to the front-end team [p:: 2] [since:: {{TODAY}}] [due:: {{TODAY}}]
- [ ] Decide the negotiation round limit (3 rounds, then auto-submit?) [p:: 2] [since:: {{TODAY}}]
- [ ] Write the passive-aggressive copy deck for the ten most common conflicts [p:: 3] [since:: {{TODAY}}]
- [ ] Evaluate a gluten-free crust option [p:: 5] [since:: {{TODAY}}]

## Why This Project Exists

PizzaBot 3000 is the Golden Thread demo project. It has enough moving parts to show the full workflow — ingesting a source, generating a wiki page, querying across the vault — while being obviously fictional so nobody mistakes it for real work.

## Related

- `idea.md` — the original brain dump
- `research.md` — findings on topping conflict algorithms
- `decisions.md` — ADR-001: why we chose arguing over accepting
- [[Topping Conflict Matrix]] — the conflict table the bot consults
