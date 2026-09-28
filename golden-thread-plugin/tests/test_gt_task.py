"""gt_task.py — tasks created, listed and worked through a tool (owner, 2026-09-28).

What carries weight: a task written here ranks in TASKS.md exactly like a hand-written one (one
parser, one store); nothing is ever deleted; a drop needs a reason and a deferral a future date;
an ID taken before someone edited the file can never close the wrong line; and the tool refuses
to write a README another live session has claimed (Core rule 1).
"""
import datetime as dt
import json
import unittest

from test_gt_tasks import readme
from _harness import Sandbox

TODAY = "2026-09-28"


class TaskBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.tools = self.vault / "Projects" / "golden-thread" / "tools"
        self.tool = self.tools / "gt_task.py"
        self.proj = self.vault / "Projects" / "alpha"
        self.proj.mkdir(parents=True)
        self.readme_path = self.proj / "README.md"
        self.readme_path.write_text(readme("alpha", tasks=["- [ ] existing [p:: 2] [since:: 2026-09-01]"]))
        (self.vault / "Knowledge").mkdir(exist_ok=True)
        (self.vault / "Knowledge" / "Quote API.md").write_text("# Quote API\n")

    def t(self, *args, today=TODAY):
        return self.py(self.tool, *args, env={"GT_TODAY": today})

    def add(self, text, *extra):
        p = self.t("add", text, "--vault", self.vault, "--project", "alpha", *extra)
        self.assertOk(p)
        return p.stdout.split()[1]            # "added <ID>"

    def ids(self, *filters, today=TODAY):
        p = self.t("list", "--vault", self.vault, *filters, "--json", today=today)
        self.assertOk(p)
        return json.loads(p.stdout)


class Add(TaskBase):
    def test_it_is_seeded_into_a_new_vault(self):
        self.assertTrue(self.tool.is_file(), "vault_init did not seed gt_task.py")

    def test_the_line_is_well_formed_and_newest_first(self):
        self.add("Rotate the relay credential", "--p", "1", "--ref", "[[Quote API]]")
        text = self.readme_path.read_text()
        tasks = text.split("## Tasks")[1]
        first = [l for l in tasks.splitlines() if l.startswith("- [")][0]
        self.assertEqual(first, "- [ ] Rotate the relay credential [ref:: [[Quote API]]] "
                                "[p:: 1] [waiting:: user] [since:: %s]" % TODAY)

    def test_it_ranks_in_the_rollup_like_a_hand_written_task(self):
        self.add("Ship it", "--p", "1")
        rollup = self.tools / "gt_tasks.py"
        self.assertOk(self.py(rollup, "--vault", self.vault))
        self.assertIn("Ship it", (self.vault / "TASKS.md").read_text())

    def test_a_ref_that_resolves_nowhere_is_refused(self):
        for ref in ("[[No Such Page]]", "Sources/missing.md", "../outside.md"):
            p = self.t("add", "x", "--vault", self.vault, "--project", "alpha", "--ref", ref)
            self.assertEqual(p.returncode, 3, ref)
        self.assertNotIn("- [ ] x ", self.readme_path.read_text())

    def test_fields_in_the_text_are_refused(self):
        p = self.t("add", "sneaky [p:: 1]", "--vault", self.vault, "--project", "alpha")
        self.assertEqual(p.returncode, 2)

    def test_inbox_lines_carry_the_project_hint_gt_review_reads(self):
        (self.vault / "INBOX.md").write_text("# Inbox\n")
        p = self.t("add", "an idea", "--vault", self.vault, "--inbox")
        self.assertOk(p)
        self.assertIn("- [ ] an idea [since:: %s]" % TODAY, (self.vault / "INBOX.md").read_text())

    def test_dry_run_writes_nothing(self):
        before = self.readme_path.read_text()
        self.assertOk(self.t("add", "x", "--vault", self.vault, "--project", "alpha", "--dry-run"))
        self.assertEqual(self.readme_path.read_text(), before)


class ListAndFilters(TaskBase):
    def test_filters_combine(self):
        self.add("urgent mine", "--p", "1")
        self.add("urgent agent", "--p", "1", "--waiting", "agent")
        self.add("late", "--due", "2026-09-20")
        texts = lambda rows: sorted(r["text"] for r in rows)
        self.assertEqual(texts(self.ids("p1", "mine")), ["urgent mine"])
        self.assertEqual(texts(self.ids("overdue")), ["late"])
        self.assertEqual(texts(self.ids("ref:quote")), [])

    def test_stale_is_p1_over_a_week(self):
        self.add("old p1", "--p", "1")
        self.assertEqual(self.ids("stale"), [])
        self.assertEqual([r["text"] for r in self.ids("stale", today="2026-10-06")], ["old p1"])


class Settle(TaskBase):
    def test_done_checks_the_box_and_keeps_the_line(self):
        tid = self.add("finish", "--p", "1")
        self.assertOk(self.t("done", tid, "--vault", self.vault, "--reason", "shipped"))
        text = self.readme_path.read_text()
        self.assertIn("- [x] finish", text)
        self.assertIn("done %s: shipped" % TODAY, text)
        self.assertEqual([r for r in self.ids() if r["text"] == "finish"], [])

    def test_drop_needs_a_reason(self):
        tid = self.add("pointless")
        p = self.t("drop", tid, "--vault", self.vault)
        self.assertEqual(p.returncode, 2)
        self.assertOk(self.t("drop", tid, "--vault", self.vault, "--reason", "superseded"))
        self.assertIn("dropped %s: superseded" % TODAY, self.readme_path.read_text())

    def test_defer_hides_until_the_date_from_list_and_the_rollup(self):
        tid = self.add("later thing", "--p", "1")
        for bad in ("2026-09-28", "2026-09-01", "soon"):
            p = self.t("defer", tid, "--vault", self.vault, "--until", bad, "--reason", "r")
            self.assertEqual(p.returncode, 3, bad)
        self.assertOk(self.t("defer", tid, "--vault", self.vault, "--until", "2026-10-05",
                             "--reason", "after the release"))
        self.assertEqual([r for r in self.ids() if r["text"].startswith("later thing")], [])
        # the deferral note is part of the line (nothing is hidden in a field a reader skips)
        self.assertEqual([r["text"].split(" — ")[0].strip() for r in self.ids("deferred")],
                         ["later thing"])
        back = [r for r in self.ids(today="2026-10-05") if r["text"].startswith("later thing")]
        self.assertEqual(len(back), 1, "the task must come back on its date")

    def test_a_stale_id_is_refused(self):
        tid = self.add("target")
        self.add("pushes it down a line")
        p = self.t("done", tid, "--vault", self.vault)
        self.assertEqual(p.returncode, 1)
        self.assertIn("changed since it was listed", p.stderr)
        self.assertIn("- [ ] target", self.readme_path.read_text())

    def test_a_readme_claimed_by_another_live_session_is_refused(self):
        tid = self.add("claimed")
        sess = self.tools / "gt_session.py"
        import os
        # A live holder needs a live pid: this test process stands in for the other session.
        env = {"CLAUDE_SESSION_ID": "other-session", "CLAUDE_PID": str(os.getpid())}
        self.assertOk(self.py(sess, "--vault", self.vault, "register", "--task", "t", env=env))
        self.assertOk(self.py(sess, "--vault", self.vault, "claim", "Projects/alpha/README.md", env=env))
        p = self.py(self.tool, "done", tid, "--vault", self.vault,
                    env={"GT_TODAY": TODAY, "CLAUDE_SESSION_ID": "me"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("another live session", p.stderr)


class Rollup(TaskBase):
    def test_a_deferred_task_neither_ranks_nor_escalates(self):
        """Before 0.17.2 the rollup had no notion of a deferral: a put-off p:: 1 kept ranking and
        the stale-P1 rule kept escalating its project -- the thing a deferral exists to stop."""
        old = (dt.date.today() - dt.timedelta(days=20)).isoformat()
        later = (dt.date.today() + dt.timedelta(days=10)).isoformat()
        self.readme_path.write_text(readme("alpha", pp=3, tasks=[
            "- [ ] put off [p:: 1] [since:: %s] [defer:: %s]" % (old, later)]))
        self.assertOk(self.py(self.tools / "gt_tasks.py", "--vault", self.vault))
        text = (self.vault / "TASKS.md").read_text()
        self.assertNotIn("put off", text.split("## Project standing")[0])
        standing = [l for l in text.splitlines() if l.startswith("| `alpha`")][0]
        self.assertIn("| PP3 | PP3 | — |", standing, "a deferred stale P1 escalated its project")


if __name__ == "__main__":
    unittest.main()
