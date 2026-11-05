# Farm service table: starter (SAMPLE: not a real provider, and no verdicts)

Copy the table into your services file (the one `farm_services` in `~/.claude/vault-config.json` names), keep the rows
you want, and replace every `example` value with your own. This file ships **no calibration results**: a verdict is a
measurement of one model on your setup (`--status` shows when each was taken), and a hosted engine gets no automatic
work until you have measured it yourself. After editing, for each row you keep:

```
python3 <farm-scripts>/gt_farm_calibrate.py NAME --dry-run     # the plan and the estimated cost; sends nothing
python3 <farm-scripts>/gt_farm_calibrate.py NAME               # run it (a hosted engine only when you name it)
python3 <farm-scripts>/gt_farm_calibrate.py --status           # each engine: model, calibration, verdicts
```

The key file is mode 600 and written by a script you run, never typed into a session. `local: yes` rows are your own
machine or network; `local: no` rows are hosted, default to rung 3 (public or scrubbed material only) and need a
price (`reference_rate_per_mtok`, or a price row for the `provider`) before gt will cost them. `FARM-LOCAL-ENGINES.md`
sections 13 to 16 walk through each kind.

```services
| name | transport | base_url | model | pool | local | key_file | reference_rate_per_mtok | rung | provider |
|---|---|---|---|---|---|---|---|---|---|
| example-local | chat-completions | http://127.0.0.1:8080/v1 | example-local-model | local | yes | | 0 | | |
| example-hosted-small | chat-completions | https://api.example-provider.invalid/v1 | example-small-model | hosted | no | ~/.config/gt/farm/example-provider.key | 0.30 | 3 | example-provider |
| example-hosted-large | chat-completions | https://api.example-provider.invalid/v1 | example-large-model | hosted | no | ~/.config/gt/farm/example-provider.key | 3.00 | 3 | example-provider |
```
