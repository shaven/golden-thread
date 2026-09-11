"""gt_closeout.py: which projects look finished, and a record of asking.

Rules (any one makes a candidate; complete/archived projects never are):
  R1 past-due  open >= 3 and overdue_share >= 0.8
  R2 done      tasks >= 5, done_share >= 0.8 and urgent <= 1
  R3 quiet     open > 0, newest_task >= 21 and last_work >= 21
  R4 empty     no open tasks at all, and there were tasks once
last_work comes from `<date> [work] <slug>` lines in log.md (the gt-work format).
"""
import datetime as dt
import json
import unittest

from _harness import Sandbox


def readme(slug, tasks, stage="active", parent=None):
    fm = "---\ntype: project\nslug: %s\nstage: %s\npp: 3\n%s---\n" % (
        slug, stage, ("parent: %s\n" % parent) if parent else "")
    return fm + "\n# %s\n\n## Tasks\n\n" % slug + "\n".join(tasks) + "\n"


class GtCloseoutTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.tool = self.vault / "Projects" / "golden-thread" / "tools" / "gt_closeout.py"
        self.today = dt.date.today()
        self.records = self.vault / "Projects" / "golden-thread" / "closeout-signals.jsonl"

    def day(self, offset):
        return (self.today + dt.timedelta(days=offset)).isoformat()

    def project(self, slug, tasks, parent=None, **kw):
        d = self.vault / "Projects" / parent / slug if parent else self.vault / "Projects" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "README.md").write_text(readme(slug, tasks, parent=parent, **kw))

    def log(self, *lines):
        with open(self.vault / "log.md", "a") as fh:
            fh.write("\n" + "\n".join(lines) + "\n")

    def gc(self, *args):
        return self.py(self.tool, "--vault", self.vault, *args)

    def candidates(self):
        proc = self.gc("candidates", "--json")
        self.assertOk(proc)
        return {c["slug"]: c for c in json.loads(proc.stdout)}

    # -- rules ---------------------------------------------------------------
    def test_r1_past_due(self):
        self.project("talk", ["- [ ] rehearse %d [due:: %s]" % (i, self.day(-2)) for i in range(4)]
                     + ["- [ ] follow up [due:: %s]" % self.day(10)])
        c = self.candidates()
        self.assertIn("R1", c["talk"]["rules"])
        self.assertEqual((c["talk"]["open"], c["talk"]["overdue"]), (5, 4))

    def test_r1_needs_three_open(self):
        self.project("small", ["- [ ] a [due:: %s]" % self.day(-2), "- [ ] b [due:: %s]" % self.day(-2)])
        self.assertNotIn("small", self.candidates())

    def test_r2_mostly_done(self):
        self.project("nearly", ["- [x] a", "- [x] b", "- [x] c", "- [x] d", "- [ ] e [p:: 3]"])
        self.assertIn("R2", self.candidates()["nearly"]["rules"])

    def test_r2_blocked_by_urgent_open_work(self):
        tasks = ["- [x] t%d" % i for i in range(8)] + ["- [ ] u1 [p:: 1]", "- [ ] u2 [p:: 2]"]
        self.project("busy", tasks)
        self.assertNotIn("busy", self.candidates())

    def test_r3_quiet_uses_worklog(self):
        self.project("dormant", ["- [ ] old idea [since:: %s]" % self.day(-40)])
        self.log("%s [work] dormant — wrote 1 finding(s)" % self.day(-30))
        c = self.candidates()
        self.assertIn("R3", c["dormant"]["rules"])
        self.assertEqual(c["dormant"]["last_work"], 30)

    def test_r3_suppressed_by_recent_work(self):
        self.project("dormant", ["- [ ] old idea [since:: %s]" % self.day(-40)])
        self.log("%s [work] dormant — wrote 1 finding(s)" % self.day(-30),
                 "%s [work] dormant — wrote 2 finding(s)" % self.day(-1))
        self.assertNotIn("dormant", self.candidates())

    @unittest.expectedFailure  # defect: 2026-09-11-closeout-slug-and-log-format
    def test_r3_last_work_is_not_credited_from_a_longer_slug(self):
        # `\[work\] alpha\b` also matches "[work] alpha-beta": work on a sibling
        # whose slug merely starts with this one keeps this one looking active.
        self.project("alpha", ["- [ ] old idea [since:: %s]" % self.day(-40)])
        self.project("alpha-beta", ["- [ ] live [since:: %s]" % self.day(-1)])
        self.log("%s [work] alpha — wrote 1 finding(s)" % self.day(-30),
                 "%s [work] alpha-beta — wrote 1 finding(s)" % self.day(0))
        c = self.candidates()
        self.assertIn("alpha", c, "work logged for `alpha-beta` was credited to `alpha`, "
                                  "so the quiet project was not flagged")
        self.assertEqual(c["alpha"]["last_work"], 30)

    def test_r4_everything_checked(self):
        self.project("done", ["- [x] a", "- [X] b"])
        self.assertIn("R4", self.candidates()["done"]["rules"])

    def test_project_that_never_had_tasks_is_not_a_candidate(self):
        self.project("fresh", [])
        self.assertNotIn("fresh", self.candidates())
        self.assertNotIn("golden-thread", self.candidates())

    def test_shelved_tasks_do_not_count_as_open(self):
        self.project("leftovers", ["- [x] shipped", "- [ ] someday [p:: 7]", "- [ ] never [p:: 9]"])
        c = self.candidates()["leftovers"]
        self.assertEqual((c["open"], c["shelved"]), (0, 2))
        self.assertIn("R4", c["rules"])

    def test_closed_stages_are_never_candidates(self):
        for stage in ("complete", "archived"):
            self.project("closed-" + stage, ["- [x] a"], stage=stage)
        c = self.candidates()
        self.assertNotIn("closed-complete", c)
        self.assertNotIn("closed-archived", c)

    def test_malformed_priority_is_tolerated(self):
        self.project("typo", ["- [x] a", "- [ ] b [p:: high]"])
        proc = self.gc("signals", "typo")
        self.assertOk(proc)
        self.assertEqual(json.loads(proc.stdout)["open"], 1)

    def test_sub_project_slug(self):
        self.project("parent", [])
        self.project("kid", ["- [x] a"], parent="parent")
        self.assertIn("parent/kid", self.candidates())

    def test_text_output(self):
        proc = self.gc("candidates")
        self.assertOk(proc)
        self.assertIn("No project looks ready to close.", proc.stdout)
        self.project("done", ["- [x] a"])
        proc = self.gc()                      # candidates is the default command
        self.assertOk(proc)
        self.assertIn("done  [R4]", proc.stdout)
        self.assertIn("empty: every task is checked off", proc.stdout)

    # -- the record ----------------------------------------------------------
    def test_ask_answer_history_round_trip(self):
        self.project("done", ["- [x] a", "- [x] b"])
        self.assertOk(self.gc("ask", "done", "tasks-rollup"))
        self.assertOk(self.gc("answer", "done", "later", "after", "the", "demo"))
        rows = [json.loads(l) for l in self.records.read_text().splitlines()]
        self.assertEqual([r["event"] for r in rows], ["asked", "answered"])
        self.assertEqual(rows[0]["source"], "tasks-rollup")
        self.assertEqual(rows[0]["rules"], ["R4"])
        self.assertEqual(rows[0]["signals"]["done"], 2)
        self.assertEqual((rows[1]["answer"], rows[1]["note"]), ("later", "after the demo"))

        hist = self.gc("history", "done")
        self.assertOk(hist)
        self.assertIn("asked", hist.stdout)
        self.assertIn("1 asked, 0 closed, 1 declined or deferred", hist.stdout)

    def test_record_is_append_only(self):
        self.project("done", ["- [x] a"])
        for _ in range(3):
            self.assertOk(self.gc("ask", "done"))
        self.assertEqual(len(self.records.read_text().splitlines()), 3)

    def test_unknown_slug_and_bad_answer_are_rejected_and_not_recorded(self):
        self.assertEqual(self.gc("ask", "nope").returncode, 2)
        self.assertEqual(self.gc("signals", "nope").returncode, 2)
        self.assertEqual(self.gc("answer", "done", "maybe").returncode, 2)
        self.assertFalse(self.records.exists(), "a rejected command still wrote a record")

    def test_history_when_empty(self):
        proc = self.gc("history")
        self.assertOk(proc)
        self.assertIn("no close-out history yet", proc.stdout)


if __name__ == "__main__":
    unittest.main()
