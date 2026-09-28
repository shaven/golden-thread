---
name: gt-farm
description: "Hand a task to an external AI service instead of doing it here, as a self-contained work packet with a strict return contract. Use when the user says: farm this out, make a work packet, send this to another AI, get a non-Claude second opinion. Also when work is bulk, mechanical, or wants a genuinely non-Claude second opinion — bulk page fetching, freshness sweeps, broad cited research, independent verification. Produces a packet the user pastes into a web UI today and a script sends to an API later; the packet is identical either way."
---

# Golden Thread Farm

Move work out of this context — not because Claude cannot do it, but because
doing it here spends the scarce resource, which is context, on raw material
rather than judgement.

The unit is a **work packet**: self-contained, with a return contract. The
transport is swappable. Nothing in the packet knows whether a human or a script
carries it.

This skill is self-contained: the gates, the service routing and the return contract
are all below. It needs no particular project, page or file in the vault, and names no
particular AI service — which services exist is the user's configuration.

**Vault:** `$GT_VAULT` if set, else `vault_path` in `~/.claude/vault-config.json`.
**Current project:** the project this session opened (`/gt:gt-open`), or ask.
**Packet dir:** `farm_packet_dir` in `~/.claude/vault-config.json` if set, else
`Projects/<current project>/packets/`. A relative value is taken from the vault root; the
literal `<project>` in it is replaced by the current project's slug (so
`Projects/<project>/farm/` works for every project); an absolute value is used as is.
Create the directory if absent. If there is no vault and no absolute `farm_packet_dir`,
ask where to save rather than writing into the working directory.

## Step 1 — Apply the routing test. Most things do not leave.

All four gates must pass. **Any failure and the task stays here** — say which gate
failed and do the work.

| Gate | Question |
|---|---|
| **Stateless** | Answerable with no vault or repo state? |
| **Self-contained** | Fits in a paragraph plus a list of URLs? |
| **Checkable** | Verifiable without redoing the work? |
| **Releasable** | Every input safe to hand a third party? |

The default is that a task stays. This skill is for the minority that clears all
four — it is not a way to avoid work, and a packet nobody can check is worse than
no packet.

**Releasable is default-deny.** Never include: account identifiers, balances,
position sizes, risk parameters, hostnames, internal IPs or domains, credential
locations, non-public source code, or personal identifiers. A question that
cannot be asked without one of those does not leave; abstract it until it can, or
do it here. Check this gate while **building** the packet, not before sending it.

## Step 2 — Pick the service

Which services exist is the user's choice, not this skill's. If `farm_services` in
`~/.claude/vault-config.json` names a Markdown file (absolute, or relative to the vault),
read its table — one row per service: name, what it is good for, transport (web UI or
API), terms that limit automation, and where its key lives (location only, never the
value). If there is no such file, ask the user which services they have access to, and
offer to write that file so the question is not asked again.

Then route by the **capability** the task needs:

| Task shape | Send to |
|---|---|
| Many URLs → clean Markdown | a page-contents / extraction API (usually cheapest per page) |
| "Did any of these change upstream?" | whichever configured service has budget left today |
| Broad research wanting citations | a search or research service that returns per-claim citations |
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
  supporting nothing. One run cited a service provider's bare product homepage.
- **Citation spread is a weak heuristic, not evidence.** It was adopted as the quality
  measure and then disproved: a run that performed **zero searches** returned nine
  claims with nine distinct URLs — the widest spread recorded — every one resolving.
  Use spread to rank what to verify first. The signal that actually holds is
  **grounding metadata reporting no sources while claims carry sources**, which is a
  fact about what the request did rather than how the answer looks.
- Write the packet so it survives being read with no other context. If it only
  makes sense to someone who watched this session, it is not a packet.

Save it to `<packet dir>/<YYYY-MM-DD>-<slug>.md` (see the top of this skill) so the
request is auditable next to whatever came back.

## Step 3b — Choose the MODE before you finish the packet

A service's **research mode** will not honour the return contract. Measured: the same
packet that produced a clean `FINDINGS/GAPS` block in plain mode came back from that
service's deep-research mode as an executive summary with ASCII diagrams and a findings
table. That is not a
failed attempt at the format — it is a pipeline with its own view of a deliverable.

| Need | Mode | Cost |
|---|---|---|
| Parseable output — **required at rung 1+** | plain generation, contract enforced | shallower retrieval |
| Maximum source depth | research mode, contract abandoned | prose only; a human reads it; **rung 0 only** |

**Depth is a rung-0 capability.** Do not plan automation around a research mode.

Also state explicitly in the packet that **forum, community and blog URLs are not
documentation** — a run restricted to a provider's "official documentation" cited a
community forum thread on that provider's support site inside its citations table.


## Step 3c — Supply the vocabulary. Mandatory, and the cheapest win here.

**A model formulates its search queries from its priors, so a stale prior produces a stale
query, which retrieves a stale page, which is then cited for a current-sounding claim.**
Grounding does not cure staleness — it launders it, and every mechanical check still passes.

Measured, same packet and same model, with and without a vocabulary block:

| | Queries it ran | The rate-limit question |
|---|---|---|
| Without | `"<model id two generations old>" per token price` — stale | Invented a requests-per-minute / tokens-per-day table, cited to a page carrying no such table |
| **With** | `"<exact phrase from the current terms page>" site:<provider docs domain>` — `site:`-scoped, phrase-anchored | Put it in **`GAPS`**, noting *"older documentation references deprecated model series"* |

Claims went 9 → 6, with real gaps instead of padding. The fix is prompt-side and free.

**The rule generalises past model ids: any fact the model would otherwise recall, supply
instead.** Product names, version numbers, console paths, price points, today's date.

**Read the vocabulary from the system, never from memory** — your own recollection is the
thing being worked around. Model ids come from the provider's live model-listing API; console paths from
the console; versions from the package manifest. A vocabulary block written from memory
reproduces the bug it exists to fix.

## Step 4 — Choose the transport

**Relay (rung 0, available now).** Hand the packet over **from the file, never as
terminal text** — a rendered packet is a lossy view and long lines are silently
truncated by the display. This has already corrupted one run:

Extract from the `### TASK` heading to the end of the file, and put that on the clipboard
with whichever tool this machine has:

```bash
sed -n '/^### TASK/,$p' <packet-file> | pbcopy                          # macOS
sed -n '/^### TASK/,$p' <packet-file> | wl-copy                         # Linux, Wayland
sed -n '/^### TASK/,$p' <packet-file> | xclip -selection clipboard      # Linux, X11
sed -n '/^### TASK/,$p' <packet-file> | clip.exe                        # Windows / WSL
```

Check with `command -v` which one exists; do not guess. If none does (a headless or
remote session), do not print the packet as a substitute — give the user the packet
file's path and tell them to open it and copy from the `### TASK` line down.

Then say where to paste it. The human is the transport, which is what keeps it within
terms — many services permit automated access *only* through an API key, so for their web
UI relay is the permanent ceiling, not a stage to grow out of. Check the terms column of
the service table before suggesting anything else.

**Wire (rung 1+).** A runner script POSTs the identical packet to an API. Requires
a key; record only its **location**, never its value. The ladder: rung 0 is relay by a
human; rung 1 is a script sending one packet and parsing the contract; higher rungs
batch and schedule. Each rung up needs parseable output, so the contract is not optional
above rung 0.

## Step 5 — Ingest what comes back

- Everything enters **`unverified`**. It has no verification state until something
  here gives it one.
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
- Never put a secret's value in a packet. The no-secrets Core rule applies to every
  transport.
- Never claim a farmed result is verified because the external tool sounded certain.
- Prefer services whose output carries citations; the Checkable gate depends on them.
- One packet, one task. A packet asking three questions returns three half-answers.
