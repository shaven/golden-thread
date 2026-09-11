"""gt_tasks.py: README `## Tasks` lines -> the ranked TASKS.md rollup.

The rollup is run through its CLI against a sandbox vault. The escalation rules are
clock-dependent, so they are pinned in-process through `effective()` with a fixed
`now` (the module reads no config at import).
"""
import datetime as dt
import re
import unittest

from _harness import Sandbox, load_module


def readme(slug, pp=3, stage="active", tasks=(), parent=None, typ="project", extra=""):
    fm = ["---", "type: %s" % typ, "slug: %s" % slug, "domain: test", "stage: %s" % stage,
          "pp: %s" % pp]
    if parent:
        fm.append("parent: %s" % parent)
    if extra:
        fm.append(extra)
    fm.append("---")
    return "\n".join(fm) + "\n\n# %s\n\n## Tasks\n\n" % slug + "\n".join(tasks) + "\n\n## Notes\n\n- [ ] not a task, wrong section\n"


class GtTasksTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.tool = self.vault / "Projects" / "golden-thread" / "tools" / "gt_tasks.py"
        self.today = dt.date.today()

    def day(self, offset):
        return (self.today + dt.timedelta(days=offset)).isoformat()

    def project(self, slug, parent=None, **kw):
        d = self.vault / "Projects" / (parent or "") / slug if parent else self.vault / "Projects" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "README.md").write_text(readme(slug, parent=parent, **kw))

    def run_rollup(self):
        proc = self.py(self.tool, "--vault", self.vault)
        self.assertOk(proc, "gt_tasks.py failed")
        return (self.vault / "TASKS.md").read_text()

    @staticmethod
    def section(text, title):
        m = re.search(r"^## %s.*?$(.*?)(?=^## |\Z)" % re.escape(title), text, re.M | re.S)
        return m.group(1) if m else None

    # -- structure -----------------------------------------------------------
    def test_sections_in_order_and_file_is_regenerated(self):
        (self.vault / "TASKS.md").write_text("HAND EDIT that must not survive\n")
        self.project("alpha", tasks=["- [ ] do it [p:: 2]"])
        text = self.run_rollup()
        self.assertNotIn("HAND EDIT", text)
        heads = ["## Inbox", "## Review", "## Waiting on you", "## Ready to work", "## Project standing"]
        pos = [text.find(h) for h in heads]
        self.assertTrue(all(p >= 0 for p in pos), "missing section in:\n" + text)
        self.assertEqual(pos, sorted(pos), "sections out of order")

    def test_inbox_lists_unchecked_lines_with_hint_and_skips_checked(self):
        inbox = self.vault / "INBOX.md"
        inbox.write_text(inbox.read_text() + "\n- [ ] buy a torque wrench [project:: garage] [since:: 2026-09-01]\n"
                         "- [ ] a bare thought\n- [x] already filed -> [[alpha]]\n")
        text = self.run_rollup()
        sec = self.section(text, "Inbox")
        self.assertIn("## Inbox — not yet filed (2)", text)
        self.assertIn("| 2026-09-01 | buy a torque wrench | `garage` |", sec)
        self.assertIn("| — | a bare thought | — |", sec)
        self.assertNotIn("already filed", text)

    def test_only_lines_under_tasks_heading_of_project_readmes_count(self):
        self.project("alpha", tasks=["- [ ] real task"])
        self.project("notaproject", typ="area", tasks=["- [ ] ignored task"])
        text = self.run_rollup()
        self.assertIn("real task", text)
        self.assertNotIn("ignored task", text)
        self.assertNotIn("not a task, wrong section", text)

    # -- ranking -------------------------------------------------------------
    def test_rows_sort_by_project_priority_then_task_priority(self):
        self.project("hi", pp=1, tasks=["- [ ] hi-three [p:: 3]", "- [ ] hi-one [p:: 1] [since:: %s]" % self.day(0)])
        self.project("lo", pp=2, tasks=["- [ ] lo-one [p:: 1]"])
        text = self.section(self.run_rollup(), "Ready to work")
        order = [text.find(s) for s in ("`PP1-P1` | hi-one", "`PP1-P3` | hi-three", "`PP2-P1` | lo-one")]
        self.assertTrue(all(p >= 0 for p in order), text)
        self.assertEqual(order, sorted(order), "rows are not ranked PP then P:\n" + text)

    def test_waiting_routes_rows_to_their_section(self):
        self.project("alpha", tasks=["- [ ] ask the user [waiting:: user]",
                                     "- [ ] do it myself",
                                     "- [ ] vendor reply [waiting:: external]"])
        text = self.run_rollup()
        self.assertIn("ask the user", self.section(text, "Waiting on you"))
        self.assertIn("do it myself", self.section(text, "Ready to work"))
        self.assertIn("vendor reply", self.section(text, "External and parked"))
        self.assertNotIn("vendor reply", self.section(text, "Ready to work"))

    def test_shelved_and_done_tasks_are_never_ranked(self):
        self.project("alpha", pp=2, tasks=["- [ ] live one [p:: 2]",
                                           "- [ ] shelved one [p:: 7] [due:: %s]" % self.day(-30),
                                           "- [x] finished one [p:: 1]"])
        text = self.run_rollup()
        self.assertNotIn("shelved one", text)
        self.assertNotIn("finished one", text)
        standing = self.section(text, "Project standing")
        row = [l for l in standing.splitlines() if l.startswith("| `alpha`")][0]
        self.assertTrue(row.rstrip().endswith("| 1 | 1 |"), "open/shelved counts wrong: " + row)
        self.assertIn("| PP2 |", row, "a shelved overdue task escalated its project: " + row)

    def test_sub_projects_are_rolled_up_with_parent_slug(self):
        self.project("parent", tasks=[])
        self.project("child", parent="parent", tasks=["- [ ] nested task"])
        text = self.run_rollup()
        self.assertIn("| nested task | `parent/child` |", text)

    def test_review_section_lists_closeout_candidates(self):
        self.project("finished", tasks=["- [x] a", "- [x] b"])
        text = self.run_rollup()
        sec = self.section(text, "Review")
        self.assertIn("(1)", text[text.find("## Review"):text.find("\n", text.find("## Review"))])
        self.assertIn("`finished`", sec)
        self.assertIn("empty: every task is checked off", sec)

    @unittest.expectedFailure  # defect: 2026-09-11-task-rollup-due-today-and-bad-priority
    def test_one_malformed_priority_does_not_sink_the_whole_rollup(self):
        # gt_closeout._tasks treats `[p:: high]` as p 3; gt_tasks.parse_tasks calls
        # int() unguarded, so one typo in one README kills TASKS.md for every project.
        self.project("alpha", tasks=["- [ ] fine task [p:: 2]"])
        self.project("typo", tasks=["- [ ] bad task [p:: high]"])
        proc = self.py(self.tool, "--vault", self.vault)
        self.assertOk(proc, "one malformed [p:: high] in one README crashed the whole rollup")
        self.assertIn("fine task", (self.vault / "TASKS.md").read_text())


class EscalationTest(Sandbox):
    """effective(): PP0 only via an open window; soft rules floor at PP1."""

    NOW = dt.datetime(2026, 9, 9, 17, 0, tzinfo=dt.timezone.utc)   # Wed 12:00 America/Chicago
    TODAY = NOW.date()

    def setUp(self):
        super().setUp()
        v = self.make_vault()
        self.m = load_module(v / "Projects" / "golden-thread" / "tools" / "gt_tasks.py", "gt_tasks_under_test")

    def task(self, **kw):
        t = {"done": False, "p": 3, "waiting": "agent", "due": None, "since": None, "blocks": None}
        t.update(kw)
        return t

    def eff(self, pp, tasks, escalate=None, stage="active"):
        return self.m.effective(pp, escalate, tasks, self.NOW, self.TODAY, stage)[0]

    def d(self, offset):
        return (self.TODAY + dt.timedelta(days=offset)).isoformat()

    def test_no_rule_leaves_baseline(self):
        self.assertEqual(self.eff(3, [self.task()]), 3)

    def test_stale_p1_escalates_one_level(self):
        self.assertEqual(self.eff(3, [self.task(p=1, since=self.d(-8))]), 2)
        self.assertEqual(self.eff(3, [self.task(p=1, since=self.d(-7))]), 3, "7 days is not yet stale")

    def test_blocking_task_escalates(self):
        self.assertEqual(self.eff(3, [self.task(blocks="other")]), 2)

    def test_due_dates_escalate_within_window_and_when_overdue(self):
        for off, want in ((5, 3), (4, 3), (3, 2), (1, 2), (-2, 2)):
            with self.subTest(due_in_days=off):
                self.assertEqual(self.eff(3, [self.task(due=self.d(off))]), want)

    @unittest.expectedFailure  # defect: 2026-09-11-task-rollup-due-today-and-bad-priority
    def test_task_due_today_escalates(self):
        # days_since(due) is 0 on the due date; `0 or -99` turns it into -99, so the
        # one day a deadline matters most is the one day it does not escalate.
        self.assertEqual(self.eff(3, [self.task(due=self.d(0))]), 2,
                         "a task due TODAY did not escalate its project (due tomorrow does)")

    def test_soft_rules_floor_at_pp1(self):
        tasks = [self.task(p=1, since=self.d(-30)), self.task(due=self.d(1)), self.task(blocks="x")]
        self.assertEqual(self.eff(1, tasks), 1)
        self.assertEqual(self.eff(2, tasks), 1)

    def test_open_window_reaches_pp0_and_closed_window_does_not(self):
        self.assertEqual(self.eff(3, [], "Mon-Fri 07:00-15:15 America/Chicago -> 0"), 0)
        self.assertEqual(self.eff(3, [], "Mon-Fri 13:00-15:15 America/Chicago -> 0"), 3)
        self.assertEqual(self.eff(3, [], "Sat-Sun 07:00-15:15 America/Chicago -> 0"), 3)

    def test_shelved_and_done_tasks_and_closed_projects_never_escalate(self):
        self.assertEqual(self.eff(3, [self.task(p=7, due=self.d(-1))]), 3)
        self.assertEqual(self.eff(3, [self.task(done=True, due=self.d(-1))]), 3)
        for stage in ("complete", "archived"):
            with self.subTest(stage=stage):
                self.assertEqual(self.eff(3, [self.task(p=1, since=self.d(-30), due=self.d(-1))],
                                          stage=stage), 3)


if __name__ == "__main__":
    unittest.main()
