# PizzaBot 3000 — Decisions

> ⚠️ Fictional demo project.

## ADR-001: Passive-Aggressive Tone as Default

**Status:** Accepted

**Decision:** PizzaBot 3000 will use passive-aggressive tone as its default negotiation mode.

**Context:** User research (n=12, fictional) showed 100% approval for passive-aggressive responses vs 67% for polite and 0% for aggressive. The product vision explicitly calls for friction-as-a-feature.

**Consequences:** Copy needs to be carefully calibrated. Too aggressive and users feel attacked; too mild and the friction disappears. A tone configuration option may be added in v2.

---

## ADR-002: Hybrid Conflict Detection (TCM + LLM fallback)

**Status:** Accepted

**Decision:** Use a versioned YAML Topping Conflict Matrix (TCM) as the primary conflict detector, with LLM fallback for combinations not covered by the TCM.

**Context:** Pure rule-based is maintainable but can't reason about novel combinations. Pure LLM is flexible but adds latency and cost to every order.

**Consequences:** TCM must be versioned and reviewed quarterly. LLM fallback adds ~200ms latency for edge cases. TCM coverage % becomes a tracked metric.
