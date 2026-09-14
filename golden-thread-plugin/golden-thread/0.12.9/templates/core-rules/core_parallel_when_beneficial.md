---
name: core_parallel_when_beneficial
description: "CORE rule — in every project, work that splits into independent units runs in parallel, up to the configured budget. Serial execution on a multi-core machine is a choice that must be justified. Governs what the assistant RUNS and what it WRITES. Reminder tier: 'would parallelism have helped' is not mechanically checkable."
metadata:
  node_type: memory
  type: core
  level: core
  enforcement: reminder
  promoted: 2026-09-12
imperative: "Parallelise any work that can be parallelised, in every project — independent units run concurrently up to the configured budget, and serial execution must be justified, not assumed."
gated_by: parallel_work
budget_from: parallel_max
---

# Use the whole machine

**Parallelise any work that can be parallelised, in every project — independent units
run concurrently up to the configured budget, and serial execution must be justified,
not assumed.**

The default is wrong. A `for` loop, a sequence of tool calls, a sweep over 43 projects,
a backtest over 200 tickers and a test suite all run on one core unless someone says
otherwise, on a machine with many. Nothing reports the waste: the work completes,
correctly, slowly, and the output looks identical to the fast version.

## Repetition is the strongest trigger

**The more times the work runs, the less optional this is.** One serial run wastes a few
minutes once. The same shape repeated — the same command over a list, a suite run twenty
times a day, a nightly sweep, a backfill over every project, a loop you are about to
enter for the second time this session — pays that cost on **every** iteration, forever,
and each individual run still looks fine.

Three specific triggers, any one of which means stop and parallelise before continuing:

1. **You are writing a loop over a list.** The loop body is the unit. Decide the worker
   count before you write the `for`.
2. **You have already done this once serially in this session.** The second occurrence
   is the signal — not a reason to push through by hand.
3. **Something else will run this again.** A script, a cron job, a CI step, another
   session tomorrow. Work that recurs earns the parallel version *and* the cheaper
   question underneath it: does it need to redo everything every time, or can it skip
   what has not changed?

Parallelism and not-repeating-the-work are the same instinct. Answer both.

## Scope — everything, not just this vault's tools

This is not a rule about the `gt` test runner. It governs **all work in every project**,
in whatever language, on whatever host, and it has two halves:

**What the assistant RUNS.** Any command, sweep, build, test suite, migration, download
or host fan-out. If it takes more than a few seconds and divides, it runs divided.

**What the assistant WRITES.** Code, scripts and jobs are authored parallel-capable by
default when the work divides: a worker pool rather than a bare loop, a `-j` / `--jobs`
/ `--workers` flag, and a default taken from the budget below rather than hardcoded to
1 or to this machine's core count. A script that can only ever run serially has made the
same mistake permanent, in a file, for everyone who runs it later.

Someone else's build system counts too: pass `make -j`, `pytest -n`, `cargo --jobs`,
`xargs -P`, `ninja` — the flag already exists in nearly every tool that matters.

## The budget is a setting, and it applies to all of it

| Setting | Meaning |
|---|---|
| `parallel_work` | `on` (default) or `off`. Off, this rule stops being injected and everything runs serially. |
| `parallel_max` | `auto` (default — as many as the machine allows) or a worker ceiling. |

Ask for the number rather than inventing one — from a shell script in any project:

```bash
JOBS=$(python3 <plugin>/scripts/gt_settings.py jobs 42 --io-bound)
```

or in Python, `gt_settings.parallel_jobs(units, io_bound=…)`. Both return 1 when
`parallel_work=off`, never exceed `parallel_max`, and never return more workers than
there are units of work. Show the current values with `/gt:gt-settings`. The injected
rule carries the ceiling on its own line, so the budget is visible every turn rather
than looked up.

`auto` is deliberately not a number: a stored count is wrong on the next machine and
wrong again after a hardware change, and this config syncs between machines — the same
argument as computing task priority at the moment of asking rather than storing it.

## Decide it explicitly, every time

Before running anything that will take more than a few seconds, answer out loud:

1. **Does the work split into independent units?** Files, projects, hosts, tickers,
   test cases, pages to fetch, commits to scan, rows to score.
2. **CPU-bound or I/O-bound?** CPU-bound → one process per core. I/O-bound (SSH, HTTP,
   disk, a subprocess you then wait on) → concurrency well above core count is correct
   and usually the bigger win.
3. **If serial, why?** State the reason. "I didn't think about it" is not one.

## How, by shape of work

| Shape | Mechanism |
|---|---|
| Independent shell commands over a list | `xargs -P "$(sysctl -n hw.ncpu)"`, `parallel`, or `&` + `wait` |
| Independent tool calls in one turn | issue them in a **single** message — sequential calls are round-trip-bound for no reason |
| Read-heavy sweeps across many files | subagents in one message; their file dumps stay out of the main context, which is also the open context-shedding task |
| Test suites | `pytest -n auto`, `make -j`, `tests/prun.py` here, the runner's own parallel flag anywhere else |
| CPU-bound Python | `concurrent.futures.ProcessPoolExecutor` — a `ThreadPoolExecutor` buys nothing against the GIL |
| Subprocess-bound Python | `ThreadPoolExecutor` is right — the GIL is released while you wait |
| Many hosts | fan out over SSH concurrently, one log per host, then join |

**Report the win.** Say what the wall clock and the parallelism actually were, so the
next session does not re-derive the same speedup. A claimed speedup carries its
verification state like any other figure ([[core_verification_state]]).

## Where parallelism is forbidden, not merely unhelpful

Running these concurrently is a defect, not an optimisation:

- **Writes to a shared vault file.** One writer per file —
  [[core_concurrent_session_claim]]. Parallel *reads* are free; parallel writes to
  `log.md`, `decisions.md` or `TASKS.md` are the exact collision the spool model exists
  to prevent.
- **Order-dependent steps.** Migration chains, ADR number allocation, anything where
  step N reads what step N-1 wrote.
- **Rate-limited or single-session remotes.** Brokerage and vendor APIs, a box that
  allows one login.
- **Production changes.** Sequential and observable beats fast and simultaneous; and
  they wait for the 16:00 CT close regardless.
- **Anything whose failure mode is partial.** If half the units failing leaves a state
  no one can reason about, run it serially or make each unit atomic first.

## The measurement that designated this rule

2026-09-12. The `gt` test suite — 672 tests that spend nearly all their time shelling
out to `install.sh`, `vault_init` or a hook and waiting — went from **509s to 108s,
4.7×, with CPU rising from roughly one core to 404%** once it ran one process per
TestCase class (`tests/prun.py`, commit `ee9246e`). `self-verified`: measured on this
machine, same 672 tests passing both ways, not re-derived by an independent validator.

The figure is not the point and neither is the test suite. The 4.7× was available the
whole time, on every previous run, and the serial version never mentioned it.

**Tier:** Core (see [[core_rule_priority_model]])

**Why designated rather than gated:** the user designated it. It would pass the gate
anyway — a serial run of divisible work is pure cost (question 2), paid on every
occurrence, and invisible afterwards.

**Why `reminder` and not `validated`:** "parallelism would have helped here" has no
recognisable shape. A Stop hook cannot see whether a finished run was divisible, so a
check would either fire on every reply containing a loop or catch nothing. Claiming
`validated` where nothing validates is the failure this tier exists to close — see
`enforcement.md`. The honest mechanism is re-assertion every turn, which is what makes
the question get asked before the run rather than after. What *is* mechanically checked
is narrower and lives in the release gate: a shipped tool that runs divisible work must
expose a worker flag.

**How to apply:** at the moment you are about to run something long, name the unit of
independent work and the degree of parallelism — or name the reason there isn't one. When
you write the tool, give it a `--jobs` flag defaulting to the budget.
