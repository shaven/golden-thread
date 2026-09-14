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

### CURRENT VOCABULARY — use these terms, not your own recollection
Today is <YYYY-MM-DD>. The following are current, read from <where you read them> today.
Use these names in your searches:

<exact model ids / product names / version numbers / console paths>

**Do NOT search for <the superseded names>.** Those are earlier generations and any
figures attached to them are out of date. If a page you find discusses only those, say so
in GAPS rather than reporting its numbers as current.

### RETURN CONTRACT
Answer ONLY in this shape. Omit preamble, apologies and restatement.

FINDINGS:
  - claim: <one sentence>
    source: <URL of the PAGE carrying the statement — not a homepage — or NONE>
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
- **Cite the page, not the site.** Require the URL of the specific page carrying the
  statement. A homepage or section index is not a source for a particular claim — and
  a site-level URL resolves with HTTP 200, so it passes a citation check while
  supporting nothing. One run cited a bare `https://workspace.google.com`.
- **Citation spread is a weak heuristic, not evidence.** It was adopted as the quality
  measure and then disproved: a run that performed **zero searches** returned nine
  claims with nine distinct URLs — the widest spread recorded — every one resolving.
  Use spread to rank what to verify first. The signal that actually holds is
  **grounding metadata reporting no sources while claims carry sources**, which is a
  fact about what the request did rather than how the answer looks.
- Write the packet so it survives being read with no other context. If it only
  makes sense to someone who watched this session, it is not a packet.

Save it to `<vault>/Projects/external-ai-tools/packets/<YYYY-MM-DD>-<slug>.md` so
the request is auditable next to whatever came back.

## Step 3b — Choose the MODE before you finish the packet

A vendor's **research mode** will not honour the return contract. Measured: the same
packet that produced a clean `FINDINGS/GAPS` block in plain mode came back from Deep
research as an executive summary with ASCII diagrams and a findings table. That is not a
failed attempt at the format — it is a pipeline with its own view of a deliverable.

| Need | Mode | Cost |
|---|---|---|
| Parseable output — **required at rung 1+** | plain generation, contract enforced | shallower retrieval |
| Maximum source depth | research mode, contract abandoned | prose only; a human reads it; **rung 0 only** |

**Depth is a rung-0 capability.** Do not plan automation around a research mode.

Also state explicitly in the packet that **forum, community and blog URLs are not
documentation** — a run restricted to "official Google documentation" cited a
`support.google.com/a/thread/` forum post inside its citations table.


## Step 3c — Supply the vocabulary. Mandatory, and the cheapest win here.

**A model formulates its search queries from its priors, so a stale prior produces a stale
query, which retrieves a stale page, which is then cited for a current-sounding claim.**
Grounding does not cure staleness — it launders it, and every mechanical check still passes.

Measured, same packet and same model, with and without a vocabulary block:

| | Queries it ran | The rate-limit question |
|---|---|---|
| Without | `"gemini-1.5-flash" per token price` — two generations stale | Invented `15 RPM / 1M TPM / 1500 RPD`, cited to a page carrying no such table |
| **With** | `"When you use Unpaid Services" site:ai.google.dev` — `site:`-scoped, phrase-anchored | Put it in **`GAPS`**, noting *"older documentation references deprecated model series"* |

Claims went 9 → 6, with real gaps instead of padding. The fix is prompt-side and free.

**The rule generalises past model ids: any fact the model would otherwise recall, supply
instead.** Product names, version numbers, console paths, price points, today's date.

**Read the vocabulary from the system, never from memory** — your own recollection is the
thing being worked around. Model ids come from a live `ListModels` call; console paths from
the console; versions from the package manifest. A vocabulary block written from memory
reproduces the bug it exists to fix.

## Step 4 — Choose the transport

**Relay (rung 0, available now).** Hand the packet over **from the file, never as
terminal text** — a rendered packet is a lossy view and long lines are silently
truncated by the display. This has already corrupted one run:

```bash
sed -n '/^### TASK/,$p' <packet-file> | pbcopy
```

Then say where to paste it. The human is the transport, which is what keeps it within terms — you.com
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
