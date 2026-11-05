---
name: gt-calibrate
description: "Run model calibration now: re-run recent agent-stage work a tier and an effort up and down, have a zero-context judge compare, and show each stage's verdict and the command to apply it. Runs even when model_calibration is off. Use when the user says: calibrate the models, calibrate the agents, run model calibration, are my agents on the right model."
plain:
  - cmd: "/gt:gt-calibrate"
    does: "Checks whether each of gt's helper agents runs on the right model, and shows what to change."
    how: "Re-runs recent helper work one model tier down, one up and with more effort, has a fresh judge compare the results, and prints keep, move down, move up or raise effort for each. It applies nothing; it spends some of your allowance, and a budget stops it."
    detail: "gt_model_calibrate.py; sample, budget, runner and weekly-allowance line come from the model_calibration_* settings; verdicts are kept in model-calibration/latest.json."
    when: "A new model has shipped, or a hint says calibration is old."
    next: "/gt:gt-settings"
model_intent: balanced
---

# Golden Thread Calibrate

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| `gt_model_calibrate.py …`, `gt_settings.py models …`, `gt_model_policy.py set …` | **terminal**: the calibration pass spends allowance and writes a plan directory, and the policy commands change settings |

A **terminal** row is a step sandbox mode refuses on purpose. Give the user the exact command to run from a terminal.

This is `/gt:gt-optimize --only calibration` by another name: invoking it **is** the request, so it
runs even when `model_calibration` is `off` (say so in one line: "model_calibration is off; running
because you asked"). Every guard still applies and none is yours to lift: the pass **refuses, running
nothing, over `model_calibration_budget`**; it **will not start at or past the weekly-allowance line**
(`model_calibration_max_weekly_pct`); with an `unlimited` budget an estimate over 5 USD **asks the
owner at a terminal**. Say which guard stopped it and stop; do not raise a budget or a line for the
owner.

Vault: `$GT_VAULT` if set, else `vault_path` from `~/.claude/vault-config.json`; pass `--vault`.

## Steps

1. Run the pass:
   ```bash
   python3 <base_dir>/../../scripts/gt_model_calibrate.py report --explicit --vault "<vault>"
   ```
   The saved `model_calibration_runner` is used when set (`gt_settings.py get model_calibration_runner`).
   When it is empty the output names a plan directory instead, and the pass takes the **Agent-tool
   route**: follow **Calibration** in `<base_dir>/../gt-optimize/SKILL.md` from "Without one" — list the
   units with `gt_model_calibrate.py units <plan-dir> --pending --agent-tool --json`, spawn one Agent per
   pending unit with that unit's `model` and the prompt file's contents (add: "Return only the JSON, free-text fields at most 40 words"), `record` each result, do the
   adjudication units the same way, then `gt_model_calibrate.py score <plan-dir>`. Effort units are
   `needs-runner`: the Agent tool takes no effort, so leave them and say effort was not calibrated.
2. Show the owner each stage's verdict — **keep**, **move-down**, **move-up**, **raise-effort**, **lower-effort**, or
   **INVALID RUN** / **NOT CALIBRATED** with the reason — with its agreement, the cost of each
   choice and, for an invalid run, the runner's stderr tail the report prints. With `skill_runlog` on
   the report has a second section, **SKILL CALIBRATION**, with the same verdicts per skill (see below).
   **Say plainly that calibration needs recorded work.** It learns from the runs gt records as the
   owner works, so on a new install most stages say NOT CALIBRATED, and a verdict the report marks
   `thin` rests on very few runs: call it provisional, and suggest running it again after more use.
3. Offer the way to take a verdict: `python3 <base_dir>/../../scripts/gt_settings.py models apply-calibration <stage>`
   (or `all`), and the page that lists every agent type beside its verdict:
   `/gt:gt-settings models`. **Never run an apply command yourself**; the owner applies a
   recommendation or ignores it. The verdicts are stored in
   `~/.claude/golden-thread/model-calibration/latest.json` for exactly that.

## Skills (setting `skill_runlog`, off by default)

With `skill_runlog` on (`gt_settings.py get skill_runlog`), the same pass also grades the **skills**, from
the skill run log (a SessionStart catch-up over Claude Code's transcripts: skill,
model, effort, tokens, wall clock, outcome and a redacted, intake-scanned digest of the task; nothing
leaves the machine). A skill with `skill_runlog_min_runs` completed runs (default 5) is **replayed** on a
sample of its digests one tier down, one up, one effort higher, one effort lower and at its current choice, with the
skill's instructions and no tools, and the zero-context adjudicator compares the answers. It is one
pass with one budget: the skills join the stage plan, so every guard above covers them, and a refusal
names the skills it skipped. `--skill <name>` (repeatable) runs the pass for those skills only, which
keeps it inside a small budget. A skill with too few completed runs is **NOT CALIBRATED**, with how
many it has and needs.

Say plainly what a skill replay is: text only, from the skill's instructions and a digest of the task.
It says whether a neighbouring model understands and plans the task as well, not that it does the
tool work as well. Each verdict prints `gt_model_policy.py set --skill <name> --model <m> [--effort <e>]`;
**never run it yourself**. With `skill_runlog` off nothing about skills appears and nothing is recorded:
offer `gt_settings.py set skill_runlog on` only if the owner asks how to calibrate skills, and say that
it starts recording from the next session start (it also picks up the last 30 days still in the
transcripts).

## The findings pass (setting `findings_calibration`, on by default)

The same entry point also re-grades the **verification state of recorded findings**
(`research.md` sections and memory notes), because a label is only as good as the evidence behind it.
`report --explicit` appends a **Findings** section; run it alone with:

```bash
python3 <base_dir>/../../scripts/gt_model_calibrate.py findings --vault "<vault>" [--project <slug>] [--json]
```

It spends nothing (no model call) and **writes nothing**. For each finding it separates the
**observed symptom** (something was run and its result seen) from the **inferred mechanism** (read
from the source, never executed), and applies one rule: **no finding is calibrated above
`unverified` without evidence attached** (a command and its output, a file and line, a measurement,
or the user's own statement), however confidently it is worded. It reports every finding whose
stated label is above its evidence, or whose inferred mechanism is written as fact, with the
split and the calibrated label of each half. It reuses the skeptic rubric (`reconcile.json`: the
kinds `unverified` and `overclaimed`), so there is one rubric, not two.

Show the owner the overstated findings. A re-label happens **only on the owner's yes**, one finding at
a time, and goes through the write queue (Core rule 1) as a new, dated calibration entry; the recorded
finding is never rewritten:

```bash
python3 <base_dir>/../../scripts/gt_model_calibrate.py relabel <finding-id> --yes --vault "<vault>"
```

Without `--yes` it only says what it would append. Never pass `--yes` without the owner's yes.
`gt_settings.py set findings_calibration off` turns the pass off (here and at write-back).

To see where thinking tokens go outside gt's own stages (the main session, agents the owner spawns), and
what a multi-agent build cost, offer the read-only reports (counts only, advisory):
`python3 <base_dir>/../../scripts/gt_effort_report.py effort` and
`python3 <base_dir>/../../scripts/gt_effort_report.py builds --session-file <session>.jsonl`.

## Rules

- **Nothing is applied by this skill.** It reports and offers (a findings re-label only on the owner's yes).
- **Guards are not negotiable.** Budget, weekly-allowance line and the terminal confirmation stand.
- Stages without enough recorded runs are listed as not calibrated, with how many they have and need.
- Skills are replayed from a redacted digest and are never sent a digest that failed the intake scan.
- **Keep this session to verdicts (0.21.4).** Replay and adjudication run in the spawned agents;
  bring back what `record` needs and the scored verdicts. Never paste digests, transcripts or whole
  outputs into the main session: every later reply re-sends them.
