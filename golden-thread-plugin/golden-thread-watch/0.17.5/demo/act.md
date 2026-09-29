## Watch upstream

narration: PizzaBot depends on code it does not own. Golden Thread can stand watch on any git repo and raise a P0 the moment it ships a security fix — decided by rules, not by guessing from commit prose.
do: Invoke the gt-watch skill to add a watch on `file://$GT_VAULT/.demo/upstream/widget-lib.git` labelled "Widget library". Then run `bash <scripts>/gt_demo.sh upstream-release` to publish widget-lib v1.0.1, run `python3 <module:watch>/gt_watch.py fetch` (the job cron runs hourly), and finally `python3 <module:watch>/gt_watch.py --hook` to show the P0 exactly as the next session start will report it.
point: The upstream release carried CVE-2026-12345, so the next session opens with a P0 naming the repo and the reason — no one had to go looking.
