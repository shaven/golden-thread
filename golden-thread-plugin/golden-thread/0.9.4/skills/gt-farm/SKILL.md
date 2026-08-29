---
name: gt-farm
description: "Hand a task to an external AI service instead of doing it here, as a self-contained work packet with a strict return contract. Use when work is bulk, mechanical, or wants a genuinely non-Claude second opinion — bulk page fetching, freshness sweeps, broad cited research, independent verification. Produces a packet the user pastes into a web UI today and a script sends to an API later; the packet is identical either way."
---

# Golden Thread Farm

Move work out of this context — not because Claude cannot do it, but because
doing it here spends the scarce resource, which is context, on raw material
rather than judgement.

The unit is a **work packet**: self-contained, with a return contract. The
transport is swappable. Nothing in the packet knows whether a human or a script
carries it.

Full design: `<vault>/Projects/external-ai-tools/design.md`.

## Step 1 — Apply the routing test. Most things do not leave.

All four gates must pass. **Any failure and the task stays here** — say which gate
failed and do the work.

| Gate | Question |
|---|---|
| **Stateless** | Answerable with no vault or repo state? |
| **Self-contained** | Fits in a paragraph plus a list of URLs? |
| **Checkable** | Verifiable without redoing the work? |
| **Releasable** | Every input safe to hand a third party? (ADR-3) |

The default is that a task stays. This skill is for the minority that clears all
four — it is not a way to avoid work, and a packet nobody can check is worse than
no packet.

**Releasable is default-deny.** Never include: account identifiers, balances,
position sizes, risk parameters, hostnames, internal IPs or domains, credential
locations, non-public source code, or personal identifiers. A question that
cannot be asked without one of those does not leave; abstract it until it can, or
do it here. Check this gate while **building** the packet, not before sending it.

## Step 2 — Pick the vendor

Read the vendor table in `design.md`. Route by what the task needs:

| Task shape | Send to |
|---|---|
| Many URLs → clean Markdown | you.com Contents API (cheapest per page) |
| "Did any of these change upstream?" | Whichever has free budget left today |
| Broad research wanting citations | you.com Research API, or ARI by relay |
| **Second opinion where being wrong is expensive** | **A non-Claude model — this is the one thing more Claude cannot buy** |
| Anything needing repo or vault state | **Nobody. It failed gate 1.** |

## Step 3 — Build the packet

Plain Markdown, because every transport accepts it:

```
### TASK
<one sentence: what to produce>

### INPUTS
<URLs, quoted text, explicit constraints — everything needed, nothing assumed>

### RETURN CONTRACT
Answer ONLY in this shape. Omit preamble, apologies and restatement.

FINDINGS:
  - claim: <one sentence>
    source: <URL that supports it, or NONE>
    confidence: high | medium | low
GAPS:
  - <what you could not establish, and why>
```

- **`source: NONE` is a legal answer.** A claim admitting it has no source beats
  one that invents a citation. Say so in the packet.
- **`GAPS` is mandatory, and `GAPS: None` must be justified, not asserted.** The
  first real return claimed "None. All 7 questions were fully established" while one
  claim's citation did not support it and another had been falsified by direct
  observation an hour earlier — both at `confidence: high`.
- **A claim that *follows from* a source but is not *stated by* it must be marked
  `source: NONE`.** This is the failure mode the format exists to catch: an inference
  presented as a citation reads exactly like a fact until someone opens the page.
- **Watch the citation spread.** Six of seven claims sharing one URL is what a model
  that read a single page and generalised looks like.
- Write the packet so it survives being read with no other context. If it only
  makes sense to someone who watched this session, it is not a packet.

Save it to `<vault>/Projects/external-ai-tools/packets/<YYYY-MM-DD>-<slug>.md` so
the request is auditable next to whatever came back.

## Step 4 — Choose the transport

**Relay (rung 0, available now).** Hand the user the packet and say where to paste
it. The human is the transport, which is what keeps it within terms — you.com
§2.4(16) permits automated access *only* via an API key, so for that vendor relay
is the permanent ceiling for the web UI, not a stage to grow out of.

**Wire (rung 1+).** A runner script POSTs the identical packet to an API. Requires
a key; record only its **location**, never its value. See the automation ladder in
`design.md` for what each further rung costs.

## Step 5 — Ingest what comes back

- Everything enters **`unverified`** (Core rule 6). It has no verification state
  until something here gives it one.
- Promote a finding to `self-verified` only by **opening its `source:` URL** and
  confirming the claim. Not by finding it plausible.
- A packet whose findings are all `source: NONE` produced an opinion, not
  research. Record it as such or discard it.
- File results next to the packet, then promote anything durable with
  `/gt:gt-promote`.

## Rules

- Never send a packet that fails a gate — including when the user is impatient.
  The gates are the entire value; a packet that skips them is just a slower way to
  get an unverifiable answer.
- Never put a secret's value in a packet. Core rule 2 applies to every transport.
- Never claim a farmed result is verified because the external tool sounded certain.
- Prefer vendors whose output carries citations; the Checkable gate depends on them.
- One packet, one task. A packet asking three questions returns three half-answers.
