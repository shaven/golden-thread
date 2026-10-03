# PizzaBot 3000 — Research

> ⚠️ Fictional demo project.

## Topping Conflict Algorithms

Two approaches evaluated:

**Rule-based TCM** — a static YAML file mapping topping pairs to conflict severity (0–3). Fast, transparent, easy to update. Downside: someone has to maintain it and topping trends evolve.

**LLM-based judgment** — send the full order to an LLM with a "pizza expert" system prompt and ask it to rate the order. More flexible, can reason about novel combinations. Downside: latency, cost per order, non-deterministic.

**Decision:** Hybrid. Rule-based TCM for known conflicts (pineapple+anchovy always fires), LLM fallback for edge cases. TCM is versioned in git.

## Negotiation UX Research

Tested three tones in user interviews (n=12, all fictional):
- **Aggressive** ("That order is wrong and I won't submit it") — 0% approval
- **Polite** ("I noticed a potential conflict — want to reconsider?") — 67% approval
- **Passive-aggressive** ("Sure, I'll submit that. I just want you to know I have opinions.") — 100% approval

**Finding:** Passive-aggressive mode is the winner.

## API Research

Standard REST. Single endpoint `/order` accepting JSON:
```json
{
  "size": "large",
  "crust": "thin",
  "toppings": ["pepperoni", "pineapple", "anchovies"],
  "override_pizzabot": false
}
```

Returns either an order confirmation or a negotiation challenge.

## Menu Size

The menu has 12 toppings. **With 12 toppings there are 4,096 possible three-topping
pizzas**, so the Topping Conflict Matrix cannot list every combination by hand and needs
the LLM fallback.

## Finding: conflicts are pairwise

Every conflict found in testing was between exactly two toppings; no three-topping
combination conflicted unless one of its pairs already did. So the TCM only ever needs
pairs: 66 rows for 12 toppings, not one row per pizza. This holds for any menu — worth
keeping beyond this project.

## Open Questions

- Should `override_pizzabot: true` bypass all checks or just reduce friction?
- Rate limiting on the negotiation loop (max 3 rounds before auto-submit?)
