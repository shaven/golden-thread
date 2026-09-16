# PizzaBot 3000 — the Golden Thread tour

The CORE acts of the tour. `/gt-demo:gt-demo tour` never reads this file directly: it runs
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
do: Append a dated entry to `Projects/demo-pizzabot/research.md`: "Customers accept the bot's topping advice 3× more often when it cites the TCM rule by name." Then add ADR-003 to `decisions.md`: cap negotiation at three rounds, then auto-submit.
point: Nothing learned in this session depends on anyone remembering it.

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

## Act 9 — Inbox, close, and the receipt

narration: One thought is waiting in the inbox from earlier. File it, close the session, and see everything the tour produced.
do: Invoke the gt-review skill to file the line in `INBOX.md`, then the gt-work skill to capture the session, then run `bash <scripts>/gt_demo.sh end`.
point: Every step of the tour is in the vault and in git — nothing lived only in the conversation.
