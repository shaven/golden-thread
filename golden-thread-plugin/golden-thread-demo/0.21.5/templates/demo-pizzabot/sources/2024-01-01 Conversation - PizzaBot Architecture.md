---
title: "PizzaBot 3000 Architecture — Design Session"
local: "(fictional — no local file)"
ingested: "(set at demo time)"
---

> ⚠️ This is a canned source for the Golden Thread demo. It simulates a real architecture conversation that would be ingested during a live demo.

## Session Summary

**Question asked:** How should PizzaBot 3000 handle the conflict detection and negotiation loop? What's the API contract?

**Answer:**

The system has three layers:

1. **Intake layer** — A FastAPI `/order` endpoint accepts a JSON payload with `size`, `crust`, `toppings[]`, and an optional `override_pizzabot` flag. Input is validated with Pydantic. The endpoint is synchronous for v1 (async queue in v2).

2. **Conflict detection layer** — A `ConflictDetector` class first checks toppings against a versioned YAML Topping Conflict Matrix (TCM). The TCM maps topping pairs to conflict severity (0=fine, 1=eyebrow raise, 2=negotiation required, 3=hard block). If the TCM doesn't cover the combination, an LLM call with a "pizza expert" system prompt makes the call. Results are cached by topping-set hash to avoid repeat LLM calls for the same combination.

3. **Negotiation layer** — If severity ≥ 2, the `Negotiator` class generates a passive-aggressive response. The response is returned to the client with a `negotiation_required: true` flag and a `challenge` string. The client must re-submit with either a modified order or `override_pizzabot: true`. After 3 override attempts, the order is auto-submitted with a final passive-aggressive sign-off logged.

**Key architectural decisions captured:**
- TCM is versioned in git, reviewed quarterly, loaded at startup
- LLM fallback adds ~200ms p95 latency — acceptable for v1
- Passive-aggressive tone is default; tone is a config enum for future flexibility
- `override_pizzabot` bypasses negotiation but not hard blocks (severity 3)
- Orders are not persisted in v1 — stateless API, downstream submission is a mock

**Where the knowledge came from:** Design session between architect and product owner. No external sources consulted.
