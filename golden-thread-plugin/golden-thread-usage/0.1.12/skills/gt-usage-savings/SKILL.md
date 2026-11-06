---
name: gt-usage-savings
description: "Show what gt's own mechanisms saved or cost in tokens, per mechanism and per engine, with gt's overhead and a net figure. Use when the user says: is gt saving me tokens, how much does gt cost me, show my gt savings, is gt worth it, export my savings, benchmark gt, what did farming save."
plain:
  - cmd: "/gt-usage:gt-usage-savings"
    does: "Shows what gt's own mechanisms saved or cost you in tokens."
    how: "Reads the local savings ledger and prints, per mechanism, the saving, gt's own overhead and the net figure, then a NET table (what each mechanism kept out of the conversation minus what it spent on Claude, with any that lost marked), then what farmed packets kept off Claude, per engine. The whole report is always shown, never a summary; --full adds the NET and farm sections. Nothing leaves your machine."
    when: "You wonder whether gt pays for itself."
    next: "/gt-usage:gt-usage"
    hear: "is gt saving me tokens|show my gt savings|what did farming save"
model_intent: balanced
---

# Golden Thread Usage: savings ledger

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) nothing here needs the vault: the ledger and the transcripts it reads live under `~/.claude/`, which stays readable. Every step runs as written.

What gt's mechanisms actually used against what the same work would have used set up differently,
with gt's own overhead as a cost beside the savings and a net figure at the bottom. It is recorded
on this machine from Claude Code's own transcripts. **No figure is ever sent anywhere.**

## Run it

```bash
python3 ~/.claude/golden-thread/hooks/gt_savings.py record     # catch up first (silent hook does it each session)
python3 ~/.claude/golden-thread/hooks/gt_savings.py report     # last 7 days  # gt:no-spill
```

`report` alone prints the savings headline (USD first) and the one table, one line per mechanism.
That is the answer to an ordinary "what did gt save". Add `--full` when the person asks for the full
view, the NET section, per-mechanism cost, or the farm detail (owner, 2026-10-16: "for a regular
request it doesn't need to be this verbose").

**Always show the whole report** (owner, 2026-10-16). The person does not see the Bash tool's
output, so paste every line `report` printed into your reply as ONE fenced code block, unchanged:
every row, in its own order and spacing (with `--full`, the NET and FARM sections too). Never summarize it, pick rows from it, round it or turn it into a markdown table. This
holds whatever reply length is set: the report is what was asked for, so a "brief" or "about 5
lines" setting does not apply to it. After the block add **one sentence**: the net figure and which
rows are only `estimated` or `design`. Do not restate the table in prose. The same goes for `farm`,
`projects` and `net-check` output when one of them is what was asked for.

Options a person asks for in words:

- a different window: `report --days 30`, or `report --since 2026-10-01 --until 2026-10-07`
- one project: `projects` lists the opaque ids the ledger holds, then `report --project <id>`
  (the ledger stores a hash of the project, never its name)
- "which model did the saving": `report --by-model` adds mechanism x model rows (agents apart from the
  main session); they add up to the table's rows. Cache reads are priced per model (the 5.5 Opus and
  Sonnet at 0.05 of input, Fable 0.025, the rest 0.1). The agent-models row is priced at the policy
  tier (what the routing policy would have chosen for that spawn type), so it reads the same in an Opus
  session and a Sonnet session; setting `explicit_model_policy` (`leave` default, `warn`, `downgrade`) decides
  whether an explicit Opus spawn is advised on or lowered.
- "what would `average` have cost me this week": `report --profile average` (or `very-high`,
  `inherit`). Only the counterfactual columns move; the actual columns never do
- a shareable summary: `export --days 7 --out summary.md`. It holds mechanism totals, their
  states and a bar chart, and the farmed packets with engines numbered rather than named (a
  service name can be an internal host): no vault path, project, session or page content. Say where the file was
  written, and that sending it anywhere is the user's own act
- a head-to-head: run the same task twice, once with gt and once without, then
  `benchmark --task <label> --with-gt <tokens> --without-gt <tokens>`. Those are the only figures
  the report labels **measured** for the "without gt" side

## NET per mechanism (gt 0.21.4)

Keeping text out of the main conversation is not free: a subagent opens its own context before it
reads a byte (about 18,000 tokens for an Explore agent, measured), a spill file the model reads back
puts the text in anyway, an audit call is a call. `report --full` therefore adds a **NET**
section: per mechanism, the tokens **kept** out of the main context, the tokens the mechanism itself
**spent** on Claude, and the net. Show it verbatim after the table and say, in one sentence, which rows
are negative and what that means: it cost more than it kept out.

- **agent-isolation** keeps the main-session path the agent replaced: the tool output it read less the
  answer it returned, each token weighted by what carrying it would have cost (written once, re-read on
  each of the turns that followed, counted from the transcript, at most 40, ended by a compaction), plus
  its output tokens and the model calls it made beyond its first and last (each would have re-read the
  main session's context); the spend is its whole usage priced at the session model. A short job in a
  small context is a loss, a multi-step one in a big context a gain; `gt_break_even.py advise N` is the
  cautious single-read rule, and this table is the measurement.
- **spill** keeps the lines kept out of a long command output; the spend is what the model read back
  from spill files. **farm** spends only what a packet's line records as a reference or audit call
  (`spent_eq`); say that when its spend is 0, because unrecorded is not zero.
- **There is no total.** The rows have different counterfactuals; adding them counts one thing twice.
  Every figure is `estimated`.
- **PAUSED** under the table means the mechanism's trailing 7-day net is negative over at least five
  events. It is written to `~/.claude/golden-thread/usage/net-paused.json` at each session start; the
  spill hook stands down while spill is paused and an auto-router (`farm_first yes`) asks first. It
  lifts when the week turns. It is advice to gt's own code; nothing is blocked. Do not turn a paused
  mechanism's setting off for the user; tell them what the table says.
- `python3 ~/.claude/golden-thread/hooks/gt_savings.py net-check` prints the 7-day nets and rewrites the
  pause state. `… overhead [--days 14] [--save]` measures what each agent type costs just to start from
  the transcripts and, with `--save`, makes `gt_break_even.py` use this machine's own figure.
- With `rules_injection compact` the overhead line prices the rule text as sent: the full text on
  the first prompt of a session and every 20th, a one-line form (about 45 tokens) otherwise. It is
  priced at the setting in force now; the ledger does not record which mode a past prompt ran in.

## Farmed packets (gt-farm)

Every packet gt-farm's sender sends to an engine, and every packet a person carried and recorded
with its `relay` step (see `/gt-farm:gt-farm`), is a `farm` line in the same ledger. The report shows them
twice: as the `Farmed packets (local / other engines)` row of the table, and in the **FARM**
section under it — per engine and model, local or other, packets, prompt and completion tokens,
measured against estimated, wall time and tokens per second, the reference rate and cost avoided,
sent against relayed, a trend over the window, and **kept off Claude**: the farmed work as a share
of all the work in the window. For the farm section alone:

```bash
python3 ~/.claude/golden-thread/hooks/gt_savings.py farm              # last 7 days  # gt:no-spill
python3 ~/.claude/golden-thread/hooks/gt_savings.py farm --days 30    # by week  # gt:no-spill
```

When someone asks what farming saved, paste the whole FARM section into one code block, unchanged (as above), and add one sentence: the
share kept off Claude and how many packets were `estimated`.

- **By purpose (gt 0.21.4).** When the ledger holds local reviews (the commit-step second opinion) or engine
  calibrations, the FARM section ends with a by-purpose block: reviews show the tokens kept out of Claude and the
  estimated tokens that reached the session instead; **a calibration is a cost with no saving claimed** (its
  Claude spend is shown). Quote the estimated/measured state the block gives.
- **A local engine's tokens are saved; another service's are off Claude but not free.** The
  table's `saved` column counts local engines only. Say so if the two are compared.
- **Estimated means estimated.** A relayed packet's tokens are the packet and reply lengths / 4,
  and an engine that sent no `usage` field is estimated the same way. Never quote them as measured.
- **A blank reference rate is not zero cost avoided.** The row shows tokens and `-` for cost; say
  the rate is unset, and that `reference_rate_per_mtok` in the farm service table sets it.
- **The share kept off Claude is not a percent of the allowance.** It compares farmed tokens with
  the work `record` measured in the same window. The FARM section shows the latest allowance
  reading beside it but never converts one into the other (the gt-usage rule). If Claude's own
  work is not in the ledger yet, no share is shown: run `record` first.
- **Failures and retries are not in the ledger.** gt-farm writes no line for a failed or rejected
  packet; its `send` output names them. Do not report a failure rate from this report.
- **Malformed lines are skipped and counted**, never guessed. If the report names a count, say so.

## Rules when reporting any of this

- **Keep the state next to the number.** `measured` came from transcript usage fields or a
  head-to-head benchmark; `estimated` is derived from measured inputs (a counterfactual price,
  characters / 4); `design` is a claim with no measurement behind it. Never present an `estimated`
  or `design` figure as measured, and never fold a `design` row into the net.
- **Dollars follow `savings_billing`.** On `api` (invoiced per token, how most companies pay) a
  saved dollar is spend avoided: say so, and lead with the "Rules, net of their own cost" headline line. On
  `subscription` (the default) they stay a list-price equivalent, as the next rule says.
- **Tokens are the unit, dollars are a list-price equivalent, not a bill.** A subscription is not
  billed per token. Say so whenever a dollar figure is quoted.
- **Effort is not claimed.** There is no per-call token field for effort, so the report does not
  attribute a saving to it.
- **The overhead line is partial.** It counts the rule text injected each prompt. Session-start
  output, report cards and hook context are not measured; say so if the net is quoted.
- **The Rules rows are a benchmark applied, not a measurement.** `Rules (rework avoided)`
  (which also holds the savings from the cuts gt makes itself; the report does not break those out)
  is the session's own work times the factor a 2026-10-11 A/B benchmark measured (rules off cost
  1.262x per finished task): `estimated`, and only as wide as that benchmark's tasks. Secret leaks,
  release re-runs and persona work are the `design` row beside it, outside the net.
- **The `LOTR find budget` row is an estimate of one thing.** Each search the gt-lotr gateway answers
  appends a line: the tokens of the result the model received against the tokens of the same
  connections' operations loaded as full tool definitions (characters / 4, so `estimated`). It does
  not say what the model would have done with those definitions, and it is a different claim from the
  measured `LOTR specialist agents` row. With no finds recorded it is a `design` row, not zero.
- **If recording is off**, the report says so (`savings_ledger` is `off`) and shows only what was
  recorded before. Do not turn it on for the user; `/gt:gt-settings savings_ledger on` does.
- **An empty ledger is not zero savings.** It means nothing has been recorded yet.

## What it is not

It is not a promise. On a machine whose profile is `very-high` and whose agents already run at the
session model, most rows are legitimately close to nothing, and the report shows that rather than
a design figure.

## Settings

`savings_ledger` — `on` (default) or `off`. Listed by `/gt:gt-settings show`.
