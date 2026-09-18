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

    def test_one_malformed_priority_does_not_sink_the_whole_rollup(self):
        # gt_closeout._tasks treats `[p:: high]` as p 3; gt_tasks.parse_tasks calls
        # int() unguarded, so one typo in one README kills TASKS.md for every project.
        self.project("alpha", tasks=["- [ ] fine task [p:: 2]"])
        self.project("typo", tasks=["- [ ] bad task [p:: high]"])
        proc = self.py(self.tool, "--vault", self.vault)
        self.assertOk(proc, "one malformed [p:: high] in one README crashed the whole rollup")
        self.assertIn("fine task", (self.vault / "TASKS.md").read_text())


    # -- a hand edit is still discarded, but no longer in silence (0.16.3) -----------------
    #
    # TASKS.md is a projection: a tick made here was never going to survive, by design. The
    # defect was that it was discarded SILENTLY by a bare truncating write, with no record of
    # what this tool had produced -- so a person could not tell a regeneration from their own
    # edit going missing.

    DIGEST = "Projects/golden-thread/.tasks-digest"
    BACKUPS = "Projects/golden-thread/backups"

    def test_a_clean_regeneration_says_nothing_and_keeps_no_backup(self):
        """The warning has to be rare enough to mean something."""
        self.project("alpha", tasks=["- [ ] do it [p:: 2]"])
        self.run_rollup()                                   # seeds the digest
        proc = self.py(self.tool, "--vault", self.vault)    # nothing touched it since
        self.assertNotIn("not what this tool last generated", proc.stdout + proc.stderr)
        self.assertFalse((self.vault / self.BACKUPS).exists(),
                         "an untouched file must not accumulate a backup on every run")

    def test_a_hand_edit_is_backed_up_and_reported(self):
        self.project("alpha", tasks=["- [ ] do it [p:: 2]"])
        self.run_rollup()
        target = self.vault / "TASKS.md"
        target.write_text(target.read_text() + "\n- [x] I ticked this by hand\n",
                          encoding="utf-8")
        proc = self.py(self.tool, "--vault", self.vault)
        out = proc.stdout + proc.stderr
        self.assertIn("not what this tool last generated", out)
        self.assertIn("README.md", out, "must say where the edit actually belongs")
        kept = list((self.vault / self.BACKUPS).glob("TASKS.md.*"))
        self.assertEqual(len(kept), 1, "exactly one copy of the edited file")
        self.assertIn("I ticked this by hand", kept[0].read_text(encoding="utf-8"))
        self.assertNotIn("I ticked this by hand", target.read_text(encoding="utf-8"),
                         "the projection is still regenerated -- the edit does not survive")

    def test_the_digest_is_recorded_so_the_next_run_is_quiet(self):
        self.project("alpha", tasks=["- [ ] do it [p:: 2]"])
        self.run_rollup()
        d = self.vault / self.DIGEST
        self.assertTrue(d.is_file(), "no generation receipt written")
        self.assertRegex(d.read_text(encoding="utf-8").strip(), r"^[0-9a-f]{64}$")

    def test_first_run_also_says_where_the_edit_belongs(self):
        """The message naming README.md used to sit in the `else` of `if not recorded`,
        so the FIRST run -- the one most likely to be swallowing a hand edit made before
        the check existed -- was the one run that never said where the edit belonged."""
        self.project("alpha", tasks=["- [ ] do it [p:: 2]"])
        (self.vault / "TASKS.md").write_text("- [x] I ticked this before the check existed\n",
                                             encoding="utf-8")
        (self.vault / self.DIGEST).unlink(missing_ok=True)    # genuinely no receipt yet
        proc = self.py(self.tool, "--vault", self.vault)      # so: the first run
        out = proc.stdout + proc.stderr
        self.assertIn("not what this tool last generated", out)
        self.assertIn("no previous digest recorded", out, "not the first-run path")
        self.assertIn("README.md", out,
                      "the first run backed the edit up without saying where it belongs")

    # -- the receipt, the backups, and the runs that collide (2026-09-18) ------------
    def test_an_unwritable_receipt_still_reports_the_backup(self):
        """The receipt is written AFTER os.replace. Unguarded, an exception there ran
        instead of `return notes`: TASKS.md was already replaced and the hand edit already
        copied aside, but the person saw a traceback where "kept a copy: ..." belonged."""
        self.project("alpha", tasks=["- [ ] do it [p:: 2]"])
        self.run_rollup()                                   # seeds the receipt
        d = self.vault / self.DIGEST
        d.unlink()
        d.mkdir()                                           # cannot be written as a file
        target = self.vault / "TASKS.md"
        target.write_text(target.read_text() + "\n- [x] I ticked this by hand\n",
                          encoding="utf-8")
        proc = self.py(self.tool, "--vault", self.vault)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0,
                         "an unwritable receipt crashed the run AFTER TASKS.md was "
                         "replaced:\n" + out)
        self.assertIn("kept a copy:", out,
                      "the backup was made but never reported -- the user saw nothing")
        kept = list((self.vault / self.BACKUPS).glob("TASKS.md.*"))
        self.assertEqual(len(kept), 1)
        self.assertIn("I ticked this by hand", kept[0].read_text(encoding="utf-8"))
        self.assertIn("receipt", out, "the failed receipt itself was not reported")
        self.assertIn("TASKS.md written", proc.stdout,
                      "the rollup's own success line never printed")

    def test_backups_are_bounded_and_other_tools_backups_are_left_alone(self):
        """A frozen receipt makes every later run copy TASKS.md aside, so an unchanged
        vault grew backups for ever. Keep the newest KEEP_BACKUPS and nothing else."""
        self.project("alpha", tasks=["- [ ] do it [p:: 2]"])
        self.run_rollup()
        bdir = self.vault / self.BACKUPS
        bdir.mkdir(parents=True, exist_ok=True)
        for i in range(25):
            (bdir / ("TASKS.md.2026-01-%02d-120000" % (i + 1))).write_text("old %d\n" % i)
        (bdir / "lint-queue.2026-01-01-120000").write_text("gt_lint's, not ours\n")
        target = self.vault / "TASKS.md"
        target.write_text(target.read_text() + "\n- [x] hand edit\n", encoding="utf-8")
        proc = self.py(self.tool, "--vault", self.vault)
        self.assertOk(proc)
        kept = sorted(p.name for p in bdir.glob("TASKS.md.*"))
        self.assertLessEqual(len(kept), 10,
                             "backups are unbounded: %d copies kept" % len(kept))
        self.assertTrue((bdir / "lint-queue.2026-01-01-120000").is_file(),
                        "pruning ate another tool's backups in the shared directory")
        # The copy this run just made is the one that must never be pruned.
        newest = max(kept)
        self.assertIn("hand edit", (bdir / newest).read_text(encoding="utf-8"))

    def test_backups_older_than_the_age_bound_are_pruned(self):
        import os as _os
        import time as _time
        self.project("alpha", tasks=["- [ ] do it [p:: 2]"])
        self.run_rollup()
        bdir = self.vault / self.BACKUPS
        bdir.mkdir(parents=True, exist_ok=True)
        long_ago = _time.time() - 60 * 86400
        for i in range(3):
            p = bdir / ("TASKS.md.2026-01-%02d-120000" % (i + 1))
            p.write_text("ancient %d\n" % i)
            _os.utime(p, (long_ago, long_ago))
        target = self.vault / "TASKS.md"
        target.write_text(target.read_text() + "\n- [x] hand edit\n", encoding="utf-8")
        self.assertOk(self.py(self.tool, "--vault", self.vault))
        left = sorted(p.name for p in bdir.glob("TASKS.md.*"))
        self.assertEqual(len(left), 1,
                         "60-day-old backups survived the age bound: %s" % left)
        self.assertIn("hand edit", (bdir / left[0]).read_text(encoding="utf-8"))

    def test_two_runs_in_the_same_second_keep_both_backups(self):
        """Backup names are second-resolution and shutil.copy2 overwrites, so two runs
        inside one second used to leave ONE file where two edits had been kept."""
        m = load_module(self.tool, "gt_tasks_backup_race")
        m._backup_stamp = lambda: "2026-09-18-120000"       # pin the collision
        target = self.vault / "TASKS.md"
        bdir = self.vault / self.BACKUPS
        target.write_text("EDIT ONE\n", encoding="utf-8")
        m.write_rollup(self.vault, "generated A\n")
        target.write_text("EDIT TWO\n", encoding="utf-8")
        m.write_rollup(self.vault, "generated B\n")
        kept = sorted(bdir.glob("TASKS.md.*"))
        bodies = sorted(p.read_text(encoding="utf-8") for p in kept)
        self.assertEqual(len(kept), 2,
                         "the second backup overwrote the first: %s"
                         % [p.name for p in kept])
        self.assertEqual(bodies, ["EDIT ONE\n", "EDIT TWO\n"],
                         "a backed-up hand edit was destroyed by the next run's copy")

    def test_dry_run_line_count_matches_the_rendered_file(self):
        """`len(L)` counted append CALLS, several of which push multi-line chunks."""
        self.project("alpha", tasks=["- [ ] do it [p:: 2]"])
        proc = self.py(self.tool, "--vault", self.vault, "--dry-run")
        self.assertOk(proc)
        m = re.search(r"\((\d+) line\(s\)", proc.stderr)
        self.assertIsNotNone(proc.stderr)
        self.assertIsNotNone(m, "no line count in: " + proc.stderr)
        self.assertEqual(int(m.group(1)), len(proc.stdout.splitlines()) - 1,
                         "the dry run misreported how many lines it rendered")

    def test_no_temp_file_is_left_behind(self):
        """The write is atomic (tmp + fsync + replace) so a torn TASKS.md never exists --
        but a leftover tmp at the vault root would be its own mess."""
        self.project("alpha", tasks=["- [ ] do it [p:: 2]"])
        self.run_rollup()
        self.assertEqual(list(self.vault.glob("TASKS.md.gt-tmp*")), [])


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

    def test_span_window_reaches_pp0(self):
        # NOW is Wed 12:00 Chicago.
        self.assertEqual(self.eff(3, [], "Tue 16:00 → Thu 16:44 America/Chicago -> 0"), 0)
        self.assertEqual(self.eff(3, [], "Fri 16:00 → Sun 16:44 America/Chicago -> 0"), 3)

    def test_shelved_and_done_tasks_and_closed_projects_never_escalate(self):
        self.assertEqual(self.eff(3, [self.task(p=7, due=self.d(-1))]), 3)
        self.assertEqual(self.eff(3, [self.task(done=True, due=self.d(-1))]), 3)
        for stage in ("complete", "archived"):
            with self.subTest(stage=stage):
                self.assertEqual(self.eff(3, [self.task(p=1, since=self.d(-30), due=self.d(-1))],
                                          stage=stage), 3)


CHI = "America/Chicago"


def chicago(y, mo, d, h, mi, s=0):
    from zoneinfo import ZoneInfo
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=ZoneInfo(CHI))


class WindowTest(Sandbox):
    """parse_window / window_contains: daily and cross-day span forms, host-zone free."""

    # 2026-09-11 is a Friday; 09-13 Sunday; 09-14 Monday; 09-16 Wednesday.
    def setUp(self):
        super().setUp()
        v = self.make_vault()
        self.m = load_module(v / "Projects" / "golden-thread" / "tools" / "gt_tasks.py", "gt_tasks_win")

    def is_open(self, rule, when):
        return self.m.window_open(rule, when) is not None

    def test_daily_form_unchanged(self):
        rule = "Mon-Fri 07:00-15:15 America/Chicago -> 0"
        self.assertEqual(self.m.window_open(rule, chicago(2026, 9, 16, 12, 0)), 0)
        self.assertTrue(self.is_open(rule, chicago(2026, 9, 16, 7, 0)))
        self.assertTrue(self.is_open(rule, chicago(2026, 9, 16, 15, 15)), "end is inclusive")
        self.assertFalse(self.is_open(rule, chicago(2026, 9, 16, 15, 15, 1)))
        self.assertFalse(self.is_open(rule, chicago(2026, 9, 16, 6, 59)))
        self.assertFalse(self.is_open(rule, chicago(2026, 9, 13, 12, 0)), "Sunday")
        # reversed ranges never opened before; they still do not
        self.assertFalse(self.is_open("Fri-Mon 07:00-15:15 America/Chicago -> 0", chicago(2026, 9, 11, 12, 0)))
        self.assertFalse(self.is_open("Mon-Fri 22:00-02:00 America/Chicago -> 0", chicago(2026, 9, 16, 23, 0)))

    def test_malformed_rules_are_closed_not_crashes(self):
        for rule in (None, "", "garbage", "Xyz-Fri 07:00-15:15 America/Chicago -> 0",
                     "Mon-Fri 25:00-26:00 America/Chicago -> 0",
                     "Fri 16:00 → Sun 16:44 Not/AZone -> 0", "Fri 16:00 → Sun 16:44 -> 0"):
            with self.subTest(rule=rule):
                self.assertIsNone(self.m.window_open(rule, chicago(2026, 9, 11, 17, 0)))

    def test_cross_day_span(self):
        for sep in ("→", "-", "–", " → ", " - "):
            rule = "Fri 16:00%sSun 16:44 America/Chicago -> 0" % sep
            with self.subTest(sep=sep):
                self.assertIsNotNone(self.m.parse_window(rule))
                self.assertTrue(self.is_open(rule, chicago(2026, 9, 11, 17, 0)), "Fri 17:00")
                self.assertTrue(self.is_open(rule, chicago(2026, 9, 11, 16, 0)), "start inclusive")
                self.assertTrue(self.is_open(rule, chicago(2026, 9, 12, 3, 0)), "Sat")
                self.assertTrue(self.is_open(rule, chicago(2026, 9, 13, 16, 44)), "end inclusive")
                self.assertFalse(self.is_open(rule, chicago(2026, 9, 13, 16, 45)), "Sun 16:45")
                self.assertFalse(self.is_open(rule, chicago(2026, 9, 11, 15, 59)), "Fri 15:59")
                self.assertFalse(self.is_open(rule, chicago(2026, 9, 16, 12, 0)), "Wed")

    def test_week_wrapping_span(self):
        rule = "Sun 17:00 → Fri 16:00 America/Chicago -> 1"
        self.assertEqual(self.m.window_open(rule, chicago(2026, 9, 13, 17, 0)), 1, "Sun 17:00")
        self.assertTrue(self.is_open(rule, chicago(2026, 9, 14, 0, 0)), "Mon midnight")
        self.assertTrue(self.is_open(rule, chicago(2026, 9, 16, 12, 0)), "Wed")
        self.assertTrue(self.is_open(rule, chicago(2026, 9, 11, 16, 0)), "Fri 16:00")
        self.assertFalse(self.is_open(rule, chicago(2026, 9, 11, 16, 1)), "Fri 16:01")
        self.assertFalse(self.is_open(rule, chicago(2026, 9, 12, 12, 0)), "Sat")
        self.assertFalse(self.is_open(rule, chicago(2026, 9, 13, 16, 59)), "Sun 16:59")

    def test_zone_comes_from_rule_not_utc_instant(self):
        # Fri 17:00 Chicago == Fri 22:00 UTC; Sun 16:45 Chicago == Sun 21:45 UTC
        utc = dt.timezone.utc
        rule = "Fri 16:00 → Sun 16:44 America/Chicago -> 0"
        self.assertTrue(self.is_open(rule, dt.datetime(2026, 9, 11, 22, 0, tzinfo=utc)))
        self.assertFalse(self.is_open(rule, dt.datetime(2026, 9, 13, 21, 45, tzinfo=utc)))
        self.assertFalse(self.is_open(rule, dt.datetime(2026, 9, 11, 16, 30, tzinfo=utc)),
                         "16:30 UTC is 11:30 Chicago: host/UTC reading leaked in")
        # naive datetimes are UTC, never host-local
        self.assertTrue(self.is_open(rule, dt.datetime(2026, 9, 11, 22, 0)))


class TaskEventsTest(Sandbox):
    """gt_tasks emits task.open/task.done by diffing README checkboxes against the event
    stream -- only once seeded, only on change, never on --dry-run/--json, never fatally."""

    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.tools = self.vault / "Projects" / "golden-thread" / "tools"
        self.spool = self.vault / "Projects/golden-thread/spool/events"

    project = GtTasksTest.project

    def rollup(self, *args):
        p = self.py(self.tools / "gt_tasks.py", "--vault", self.vault, *args)
        self.assertOk(p, "gt_tasks.py failed")
        return p

    def events(self):
        import json
        f = self.vault / "Projects/golden-thread/events.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def seed(self):
        self.assertOk(self.py(self.tools / "gt_events.py", "--vault", self.vault, "backfill"))

    def test_unseeded_stream_emits_nothing(self):
        self.project("alpha", tasks=["- [ ] one", "- [x] two"])
        self.rollup()
        self.assertFalse(self.spool.exists(), "an unseeded rollup announced every task")

    def test_only_changes_are_emitted_and_an_unchanged_rerun_adds_nothing(self):
        self.project("alpha", tasks=["- [ ] one [p:: 2]", "- [ ] two"])
        self.seed()
        seeded = len(self.events())
        self.rollup()
        self.assertEqual(len(self.events()), seeded, "an unchanged rollup emitted events")
        self.project("alpha", tasks=["- [x] one [p:: 1]", "- [ ] two", "- [ ] three"])
        for args in (("--dry-run",), ("--json",)):
            self.rollup(*args)
            self.assertEqual(len(self.events()), seeded, "%s emitted events" % args)
        self.rollup()
        new = self.events()[seeded:]
        self.assertEqual(sorted((e["kind"], e["note"]) for e in new),
                         [("task.done", "one"), ("task.open", "three")])
        self.assertEqual({e["project"] for e in new}, {"alpha"})
        self.assertTrue(all(e["item"].startswith("Projects/alpha/README.md#t-") for e in new))
        count = len(self.events())
        self.rollup()
        self.assertEqual(len(self.events()), count, "the same flip was emitted twice")

    def test_event_failure_never_stops_the_rollup(self):
        self.project("alpha", tasks=["- [ ] one"])
        self.seed()
        self.project("alpha", tasks=["- [x] one"])
        (self.tools / "gt_events.py").write_text("raise RuntimeError('broken event tool')\n")
        (self.vault / "TASKS.md").unlink(missing_ok=True)
        p = self.rollup()
        self.assertIn("NOT recorded", p.stderr)
        self.assertIn("TASKS.md written", p.stdout)
        self.assertTrue((self.vault / "TASKS.md").is_file())

    def test_unmergeable_events_file_never_stops_the_rollup(self):
        self.project("alpha", tasks=["- [ ] one"])
        self.seed()
        self.project("alpha", tasks=["- [x] one"])
        merged = self.vault / "Projects/golden-thread/events.jsonl"
        merged.unlink()
        merged.mkdir()                                  # cannot be replaced by a file
        p = self.rollup()
        self.assertIn("NOT regenerated", p.stderr)
        self.assertIn("TASKS.md written", p.stdout)


class CliClockTest(Sandbox):
    """GT_NOW + --json through the real CLI."""

    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.tool = self.vault / "Projects" / "golden-thread" / "tools" / "gt_tasks.py"

    project = GtTasksTest.project

    def cli(self, *args, tz="UTC", now="2026-09-11T22:00:00+00:00"):
        return self.run_cmd([self.py_exe(), self.tool, "--vault", self.vault, *args],
                            env={"GT_NOW": now, "TZ": tz})

    @staticmethod
    def py_exe():
        import sys
        return sys.executable

    def fixture(self):
        self.project("market", pp=2, extra='pp_escalate: "Fri 16:00 → Sun 16:44 America/Chicago -> 0"',
                     tasks=["- [ ] weekend check [p:: 2] [waiting:: user]",
                            "- [ ] old urgent [p:: 1] [since:: 2026-08-01]"])
        self.project("other", pp=1, tasks=["- [ ] ask it [p:: 1] [waiting:: user]",
                                           "- [ ] build it [p:: 2] [due:: 2026-09-12]",
                                           "- [ ] vendor [waiting:: external]"])
        (self.vault / "INBOX.md").write_text("# Inbox\n\n- [ ] loose thought [since:: 2026-09-01]\n")

    def json_of(self, proc):
        import json
        self.assertOk(proc, "gt_tasks.py --json failed")
        return json.loads(proc.stdout)

    def test_json_never_writes_and_parses(self):
        self.fixture()
        tasks_md = self.vault / "TASKS.md"
        if tasks_md.exists():
            tasks_md.unlink()
        for args in (("--json",), ("--json", "--dry-run")):
            with self.subTest(args=args):
                data = self.json_of(self.cli(*args))
                self.assertFalse(tasks_md.exists(), "--json wrote TASKS.md")
                self.assertEqual(data["version"], 1)
                self.assertIn("generated_at", data)
                self.assertEqual([i["text"] for i in data["sections"]["inbox"]], ["loose thought"])
                self.assertIsInstance(data["sections"]["review"], list)
                t = {x["text"]: x for x in data["tasks"]}
                self.assertEqual(set(t), {"weekend check", "old urgent", "ask it", "build it", "vendor"})
                self.assertEqual([x["rank"] for x in data["tasks"]], list(range(1, 6)))
                self.assertTrue(t["weekend check"]["window_open"])
                self.assertEqual(t["weekend check"]["priority"], "PP0-P2")
                self.assertFalse(t["ask it"]["window_open"])
                self.assertTrue(t["old urgent"]["stale"])
                self.assertFalse(t["build it"]["stale"])
                self.assertEqual(t["build it"]["due"], "2026-09-12")
                self.assertEqual(t["old urgent"]["since"], "2026-08-01")
                for k in ("rank", "project", "priority", "text", "due", "since", "window_open", "stale"):
                    self.assertIn(k, data["tasks"][0])
        tasks_md.write_text("SENTINEL\n")
        self.json_of(self.cli("--json"))
        self.assertEqual(tasks_md.read_text(), "SENTINEL\n", "--json modified TASKS.md")

    def test_json_rank_matches_markdown_order(self):
        self.fixture()
        data = self.json_of(self.cli("--json"))
        self.assertOk(self.cli(), "markdown run failed")
        md = (self.vault / "TASKS.md").read_text()
        md_rows = [l.split(" | ")[1] for l in md.splitlines() if l.startswith("| `PP")]
        # markdown groups by waiting (user, agent, other); within that, global rank order
        groups = {"user": 0, "agent": 1}
        want = [x["text"] for x in sorted(data["tasks"],
                                          key=lambda x: (groups.get(x["waiting"], 2), x["rank"]))]
        self.assertEqual(md_rows, want)
        pri = [l.split(" | ")[0].strip("|` ") for l in md.splitlines() if l.startswith("| `PP")]
        by_text = {x["text"]: x["priority"] for x in data["tasks"]}
        self.assertEqual(pri, [by_text[t] for t in md_rows])

    def test_host_tz_does_not_change_window_result(self):
        self.fixture()
        a = self.json_of(self.cli("--json", tz="UTC"))
        b = self.json_of(self.cli("--json", tz="Asia/Tokyo"))
        self.assertEqual([(x["text"], x["priority"], x["window_open"]) for x in a["tasks"]],
                         [(x["text"], x["priority"], x["window_open"]) for x in b["tasks"]])
        # Sun 16:45 Chicago = 21:45 UTC: closed under either host zone
        for tz in ("UTC", "America/Chicago"):
            with self.subTest(tz=tz):
                d = self.json_of(self.cli("--json", tz=tz, now="2026-09-13T21:45:00Z"))
                self.assertFalse({x["text"]: x for x in d["tasks"]}["weekend check"]["window_open"])

    def test_bad_gt_now_fails_loudly(self):
        self.fixture()
        proc = self.cli("--json", now="yesterday-ish")
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
