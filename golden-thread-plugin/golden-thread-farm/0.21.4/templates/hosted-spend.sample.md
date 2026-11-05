# SAMPLE: hosted-engine spend setup (copy, then replace every value)

This is a SAMPLE. It describes a made-up engine on a made-up host called `example-provider`, not a real provider.
Nothing here is read by gt: copy the pieces you want into your own settings and service table, and change every value.
It holds no key, no account id and no real address, and it must stay that way.

## What 0.22.0 does with a hosted engine's cost

- gt-farm writes one ledger line per packet and keeps a small glance file
  (`~/.claude/golden-thread/farm/usage-glance.json`, mode 600): the month's spend, the calls today and the last 429, per engine.
- The spend is an **estimate**: the tokens sent and received, at the rate in the engine's row (`reference_rate_per_mtok`).
  It is not the provider's bill.
- `farm_budgets` refuses a send that would take the month's estimated spend past the amount, before any request is made.
- The status line stays quiet until an engine is at 80% of its budget or was answered with a 429 in the last hour
  (`usage_external auto`). Every dollar figure on it is labelled `(estimated)`. With no budget and no glance, it shows nothing.
- gt 0.22.0 **does not read a provider's balance or billing API**. The `balance_source` column below is RESERVED: it is not
  read by this release and is shown only so the shape is in one place. A measured balance would need a read-only billing
  key kept in the secrets store; it is never typed into a session and never written into the repository.

## 1. Settings (SAMPLE values)

```settings
# SAMPLE values: replace them with your own after you copy this
usage_external auto
farm_budgets example-hosted=10,provider:example-provider=25
```

Set them with `gt_settings.py set usage_external auto` and `gt_settings.py set farm_budgets "example-hosted=10,provider:example-provider=25"`.

## 2. Service-table row (SAMPLE: not a real provider)

```services
| name | transport | base_url | model | pool | local | key_file | reference_rate_per_mtok | provider | tier | balance_source |
|---|---|---|---|---|---|---|---|---|---|---|
| example-hosted | chat-completions | https://api.example-provider.invalid/v1 | example-model | | no | ~/.claude/golden-thread/farm/example-provider.key | 1.00 | example-provider | remote | store:example-billing-key |
```

`balance_source` is RESERVED and ignored in 0.22.0 (see above). `key_file` is where the engine's API key lives (mode 600, written by a
script you run, never typed into a session). The rate is a reference price per million tokens: set it from the provider's pricing page.
