# PizzaBot 3000 — Idea

> ⚠️ Fictional demo project.

## The Problem

Pizza ordering is too easy. People click a few toppings, pay, and receive pizza. There is no friction. No one is challenged to reconsider their choices.

## The Idea

PizzaBot 3000 is an AI assistant that processes pizza orders but does not simply accept them. It reviews your topping selections against a proprietary Topping Conflict Matrix (TCM) and pushes back if your choices are questionable.

Examples:
- "Pineapple and anchovies? I need you to think about what you're doing."
- "Three meat toppings but no cheese? This order cannot proceed until we discuss this."
- "That's actually a solid order. Placing it now."

## Core Behaviors

1. **Order intake** — accept pizza configuration via REST API (`/order`)
2. **Conflict detection** — run selections through the TCM
3. **Negotiation loop** — if conflicts detected, engage the user in dialogue before proceeding
4. **Order submission** — once approved (by PizzaBot or the user overriding it), submit downstream

## Open Questions

- How opinionated should PizzaBot be? (aggressive/polite/passive-aggressive modes?)
- Who maintains the Topping Conflict Matrix? Is it versioned?
- What happens when the user overrides PizzaBot's recommendation 3 times in a row?
