# PizzaBot 3000 — the Golden Thread tour

The CORE acts of the tour (twenty since 0.21.4; acts 9 to 19 show what 0.21.4 added and run
`gt_demo.sh show TOPIC`, which prints each real command before its output). `/gt-demo:gt-demo tour` never reads this file directly: it runs
the output of `gt_demo.sh tour-acts`, which prints these acts in order and then one act
per installed Golden Thread module that ships one (module.json `demo`, e.g. the wiki
and watch modules' `demo/act.md`), ordered by module name and numbered on after these.
Install a module and its act appears in the next tour; remove it and the act is gone. An
act that belongs to a module lives in that module, never here.

One `## Act N — Title` section per act, each with three lines: `narration:` (said to the
audience before the act), `do:` (what Claude does — invoke the named skill or run the
named tool, exactly as written), and `point:` (said after, one sentence, what the
audience just saw). Reorder or reword freely; keep the three lines. Paths are relative
to the demo vault.

Four placeholders, resolved by the tour runner before it runs a `do:` line:

- `$GT_VAULT` — the demo vault. The demo session is opened with it set (the tour checks
  this first), so it is the same directory `gt_demo.sh` built from `$GT_DEMO_VAULT`.
- `<scripts>` — the demo module's own scripts directory: `<base>/../../scripts`, where
  `<base>` is the gt-demo skill's base directory. Only `gt_demo.sh` lives there.
- `<core>` — gt's scripts directory, as printed by `bash <scripts>/gt_demo.sh core-scripts`
  (the demo is a separate plugin since 0.14.0, so gt's scripts are not beside it).
- `<module:NAME>` — an installed module's scripts directory, as printed by
  `bash <scripts>/gt_demo.sh module-scripts NAME`. Only module acts use it (e.g. the
  watch module's act runs its script from `<module:watch>`), and a module act is in the
  tour only when its module is installed.

## Act 1 — Core rules, enforced

narration: Golden Thread keeps rules that hold on every turn — not in a prompt someone might forget, but enforced by hooks.
do: Point at the timestamp that opened this very reply. Then run the installed Stop hook on the sample transcript: `bash ~/.claude/golden-thread/hooks/validate_response.sh < "$GT_VAULT/.demo/secret-transcript.json"` and show its verdict. Never type a credential-shaped string yourself.
point: Every reply is stamped, and a reply carrying a secret is blocked before anyone sees it.

## Act 2 — Open a project

narration: Each project lives in the vault as a handful of plain files. Opening one gives a new session the whole story in seconds.
do: Invoke the gt-open skill for `demo-pizzabot`.
point: A cold session now knows the idea, the research, the decisions and where work stopped.

## Act 3 — What's next?

narration: Tasks live in each project's README. Golden Thread ranks them across every project, against today's date.
do: Run `python3 Projects/golden-thread/tools/gt_tasks.py` and show the top of `TASKS.md`.
point: The task due today rose to the top on its own — priority is computed, never stored.

## Act 4 — Capture as you go

narration: Findings and decisions are written down the moment they happen, not at the end of the day.
do: Never write a vault note directly — the write hook denies it. Put the finding "Customers accept the bot's topping advice 3× more often when it cites the TCM rule by name." in a scratch file outside the vault, queue it with `python3 <core>/gt_write_queue.py --vault "$GT_VAULT" --op append --path Projects/demo-pizzabot/research.md --section "<today's date> — Citing the TCM rule" --content-file <scratch file>`, then apply it with `python3 <core>/gt_broker.py drain --vault "$GT_VAULT"` and show the broker's line. Then the decision: reserve its number with `python3 Projects/golden-thread/tools/gt_adr.py --vault "$GT_VAULT" allocate demo-pizzabot --title "Cap negotiation at three rounds, then auto-submit"` (it prints 3), fill in the slot it created, `Projects/golden-thread/spool/decisions/demo-pizzabot/0003.md`, below its heading with **Status**, **Decision**, **Context** and **Consequences** like ADR-001, and render `decisions.md` with `python3 Projects/golden-thread/tools/gt_adr.py --vault "$GT_VAULT" merge demo-pizzabot`.
point: Nothing learned in this session depends on anyone remembering it — and every write went through the queue or the ADR allocator, so a second session working this vault at the same moment could not have clobbered it.

## Act 5 — Where does this go?

narration: Ideas arrive in the middle of other work. Golden Thread says where each one belongs.
do: Invoke the gt-route skill with: "Loyalty points printed on PizzaBot receipts — where does this belong?"
point: A stray idea is placed, not lost in the wrong conversation.

## Act 6 — Check the claim

narration: The research says twelve toppings make 4,096 three-topping pizzas. Before anyone builds on that, check it.
do: Invoke the gt-validate skill on the claim in `Projects/demo-pizzabot/research.md` under "Menu Size".
point: A fresh validator re-derived 220, not 4,096 — the research had counted every subset, not three-topping pizzas.

## Act 7 — Lint the vault

narration: Vaults drift — links break, pages go unindexed. Lint finds it.
do: Invoke the gt-lint skill. Show the broken link to `Topping Conflict Matrix` and leave it for the next act.
point: The README points at a page that does not exist yet.

## Act 8 — Promote what holds

narration: Some findings are bigger than their project. Those graduate to the shared wiki.
do: Invoke the gt-promote skill on the finding "conflicts are pairwise" in `Projects/demo-pizzabot/research.md`, creating the Knowledge page `Topping Conflict Matrix`.
point: The finding is now a Knowledge page — and the broken link from act 7 resolves.

## Act 9 — Fewer tokens at the start of every turn

narration: Everything Golden Thread says to Claude costs tokens on every turn. Since 0.21.4 a healthy session start is one line, and the Core rules go out in full once, then as one line.
do: Run `bash <scripts>/gt_demo.sh show tokens-startup` and show its output as printed. Point at this session's own one-line startup report above the prompt, then at the two sizes: the full rules on prompt 1, one line with the rule-5 reminder on prompt 2. Say that the first block is a description and the rest ran live, in the demo's own home.
point: The rules are still enforced every turn — only the repetition is gone.

## Act 10 — Long output and big pastes stay out

narration: A 300-line log or a 100 KB paste can cost more than the question it answers. Golden Thread keeps them out of the conversation and keeps them on disk.
do: Run `bash <scripts>/gt_demo.sh show tokens-output` and show its output as printed: the spill marker and the file it names, the paste that was held back with its /gt:gt-digest offer, and the route the digest would take. Say that the last block is a description: the digest's reader is not run in the demo.
point: Claude saw the ends and the errors, not 300 lines; the paste waited on disk for a cheaper reader.

## Act 11 — Compact before it costs you

narration: A long session re-sends itself on every reply. Golden Thread says when the context is getting large, and can save, hand off and compact (or clear) on its own.
do: Run `bash <scripts>/gt_demo.sh show tokens-compact` and show its output as printed. Say plainly that auto_compact is shown as its setting and the handoff it writes (a dry run): this session is never compacted. Finish by naming `/gt:gt-help tokens`, the Spending Fewer Tokens guide the last command located.
point: The nudge came once per band, and the handoff exists before anything is cut.

## Act 12 — Notes answered locally

narration: A question about your own notes can be answered by a model on your own machine first. When there is none, nothing changes for you.
do: Run `bash <scripts>/gt_demo.sh show local-answers` and show its output as printed. Never run the START line it prints, and never start a model: in the demo it points at a script that only prints. Point at the silent fallback (exit 3, gt-query carries on), then at engine_stopped and the three-way question, which is shown as text, not asked.
point: No engine meant no difference; a stopped engine meant one question, never a surprise start.

## Act 13 — What your own engine is good for

narration: Routine work can go to a model you run yourself instead of your Claude allowance. Golden Thread says which work, and never starts anything on its own.
do: Run `bash <scripts>/gt_demo.sh show farm-first` and show its output as printed. Point at the work list for a developer, and at the engine reported DOWN: nothing listens on the demo engine's port, so nothing was sent anywhere.
point: farm_first suggests and prints commands; it changes nothing until you run one.

## Act 14 — Check the path before you say it

narration: A file path stated from memory is the cheapest fact to get wrong. Core rule 10 says check it, and label what was checked.
do: Run `bash <scripts>/gt_demo.sh show paths` and show its output as printed: EXISTS and MISSING from the path check, then the three path findings lint raised on a note written three wrong ways (that note is removed again afterwards).
point: A missing file is said to be missing because it was checked, not because it was remembered.

## Act 15 — Right model, right effort

narration: Thinking costs output tokens. Golden Thread reads where effort goes and says where less would do — without spending anything to find out.
do: Run `bash <scripts>/gt_demo.sh show effort` and show its output as printed. Say that the report read the demo's own fixture transcripts, made no API call and spent no allowance, and that calibration in both directions (a tier and an effort up and down) is shown as its description, because running it spends allowance.
point: Suggestions are measured and advisory; nothing was changed and nothing was spent.

## Act 16 — A credential never lands in a file

narration: gt keeps long output and big pastes out of the conversation by saving them to a file. A saved file must never become a second home for a password or key — Core rule 9.
do: Run `bash <scripts>/gt_demo.sh show secrets` and show its output as printed: the 300-line output with a key on line 7, where the preview and the spill file carry a "withheld" marker instead of the key, then the long paste with the same key, held back and saved with the line withheld, and its reason naming how many lines. Say that the key was assembled by the demo script and is not real.
point: The scan runs before anything is written, and when it cannot run, nothing is kept.

## Act 17 — A command only you can run

narration: Some actions are yours alone on purpose: an agent may not apply a reviewed change to a design document. When gt has to stop and hand you a command, it should be one paste, not a line to retype.
do: Run `bash <scripts>/gt_demo.sh show owner-command` and show its output as printed: the needs_owner refusal it describes, the command handed over (printed here, because the demo never touches your clipboard), then the one-line script form that ran and deleted itself.
point: The gate is unchanged — gt only made the hand-off a single paste, and it never reads your clipboard or runs the command itself.

## Act 18 — When a guard stops you, it says why

narration: A guard that blocks without a reason costs more than it saves. Two of them: a test run that keeps stopping for approval, and a commit that strays outside the plan.
do: Run `bash <scripts>/gt_demo.sh show guards` and show its output as printed: the doctor reading a fixture session where the same test command asked three times, naming the gate and the fix, then the scope_gate setting described.
point: Each block names its gate and its fix, and the doctor changed nothing to find it.

## Act 19 — Your role, your session

narration: A developer and a project manager do not want the same first suggestions. A persona reorders what gt offers first — and hides nothing.
do: Run `bash <scripts>/gt_demo.sh show roles` and show its output as printed: the roles gt ships, what a developer and a project manager are each offered first, and the day-one walkthrough page built for a developer (built, not opened).
point: Same tools for everyone; only the order changes with the role.

## Act 20 — Inbox, close, and the receipt

narration: One thought is waiting in the inbox from earlier. File it, close the session, and see everything the tour produced.
do: Invoke the gt-review skill to file the line in `INBOX.md`, then the gt-work skill to capture the session, then run `bash <scripts>/gt_demo.sh end`.
point: Every step of the tour is in the vault and in git — nothing lived only in the conversation.
