"""gt_task.py — tasks created, listed and worked through a tool (owner, 2026-09-28).

What carries weight: a task written here ranks in TASKS.md exactly like a hand-written one (one
parser, one store); nothing is ever deleted; a drop needs a reason and a deferral a future date;
an ID taken before someone edited the file can never close the wrong line; and a README another
live session has claimed is not written -- the write stays queued (Core rule 1).

Since 0.17.11 every README/INBOX write goes through the write queue and is drained by the broker
at once; the tests assert it ARRIVED that way (a broker log row, an empty queue), not merely that
the file changed.
"""
import datetime as dt
import json
import os
import shutil
import unittest

from test_gt_tasks import readme
from _harness import Sandbox, SCRIPTS, load_module

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
        # install.sh puts the broker in the stable hooks dir; that is where gt_task looks
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True, exist_ok=True)
        for n in ("gt_broker.py", "gt_write_queue.py"):
            shutil.copy(SCRIPTS / n, self.hooks / n)
        self.spool = self.vault / "Projects" / "golden-thread" / "spool"

    def t(self, *args, today=TODAY, env=None):
        e = {"GT_TODAY": today}
        e.update(env or {})
        return self.py(self.tool, *args, env=e)

    def queued(self):
        q = self.spool / "queue"
        return sorted(p for p in q.glob("*.json")) if q.is_dir() else []

    def broker_rows(self):
        rows = []
        for f in sorted((self.spool / "broker").glob("log-*.jsonl")):
            rows += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return rows

    def claim_readme(self):
        """Another LIVE session claims alpha's README; this test process stands in for it."""
        sess = self.tools / "gt_session.py"
        env = {"CLAUDE_SESSION_ID": "other-session", "CLAUDE_PID": str(os.getpid())}
        self.assertOk(self.py(sess, "--vault", self.vault, "register", "--task", "t", env=env))
        self.assertOk(self.py(sess, "--vault", self.vault, "claim", "Projects/alpha/README.md",
                              env=env))
        return sess, env

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
        self.assertIn("queued", p.stderr)
        self.assertIn("- [ ] claimed", self.readme_path.read_text(), "a claimed README was written")
        self.assertEqual(len(self.queued()), 1, "a held write must stay queued, not be dropped")
        self.assertEqual(self.broker_rows()[-1]["decision"], "held")


class ThroughTheQueue(TaskBase):
    """0.17.11: the README is never edited by gt_task itself -- the broker applies the write."""

    def test_add_arrives_through_the_broker_and_leaves_the_queue_empty(self):
        self.add("via the broker", "--p", "1")
        self.assertIn("- [ ] via the broker", self.readme_path.read_text())
        (row,) = [r for r in self.broker_rows() if r["path"] == "Projects/alpha/README.md"]
        self.assertEqual((row["op"], row["section"], row["decision"]),
                         ("replace-section", "Tasks", "apply"))
        self.assertEqual(self.queued(), [])

    def test_settle_arrives_through_the_broker(self):
        tid = self.add("close me")
        self.assertOk(self.t("done", tid, "--vault", self.vault))
        self.assertIn("- [x] close me", self.readme_path.read_text())
        self.assertEqual([r["decision"] for r in self.broker_rows()], ["apply", "apply"])
        self.assertEqual(self.queued(), [])

    def test_inbox_add_is_an_append(self):
        (self.vault / "INBOX.md").write_text("# Inbox\n")
        self.assertOk(self.t("add", "an idea", "--vault", self.vault, "--inbox"))
        (row,) = self.broker_rows()
        self.assertEqual((row["path"], row["op"], row["decision"]), ("INBOX.md", "append", "apply"))

    def test_the_printed_id_closes_the_line_it_names(self):
        tid = self.add("named")
        self.assertOk(self.t("done", tid, "--vault", self.vault))
        self.assertIn("- [x] named", self.readme_path.read_text())

    def test_a_held_add_stays_queued_and_lands_once_the_claim_is_gone(self):
        sess, env = self.claim_readme()
        p = self.t("add", "waits", "--vault", self.vault, "--project", "alpha",
                   env={"CLAUDE_SESSION_ID": "me"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("another live session", p.stderr)
        self.assertNotIn("waits", self.readme_path.read_text())
        self.assertEqual(len(self.queued()), 1)
        self.assertOk(self.py(sess, "--vault", self.vault, "release", env=env))
        self.assertOk(self.py(self.hooks / "gt_broker.py", "drain", "--vault", self.vault))
        self.assertIn("- [ ] waits", self.readme_path.read_text())
        self.assertEqual(self.queued(), [])

    def test_with_no_broker_installed_the_request_waits_in_the_queue_in_the_shared_format(self):
        shutil.rmtree(self.hooks)
        before = self.readme_path.read_text()
        p = self.t("add", "no broker", "--vault", self.vault, "--project", "alpha")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("queued, not written yet", p.stderr)
        self.assertIn("drain --vault", p.stderr, "it must say how to drain")
        self.assertEqual(self.readme_path.read_text(), before)
        (req_file,) = self.queued()
        # the vault tool copies the request format; gt_write_queue is the one definition of it
        wq = load_module(SCRIPTS / "gt_write_queue.py", "gt_write_queue_for_task_test")
        self.assertIsNone(wq.validate(json.loads(req_file.read_text()), self.vault))
        self.assertOk(self.py(SCRIPTS / "gt_broker.py", "drain", "--vault", self.vault))
        self.assertIn("- [ ] no broker", self.readme_path.read_text())

    def test_inbox_done_is_a_replace_file_through_the_broker(self):
        (self.vault / "INBOX.md").write_text("# Inbox\n\nprose\n")
        self.assertOk(self.t("add", "an idea", "--vault", self.vault, "--inbox"))
        (row,) = self.ids("inbox")
        self.assertOk(self.t("done", row["id"], "--vault", self.vault, "--reason", "filed"))
        self.assertIn("- [x] an idea", (self.vault / "INBOX.md").read_text())
        self.assertEqual([(r["op"], r["decision"]) for r in self.broker_rows()],
                         [("append", "apply"), ("replace-file", "apply")])
        self.assertEqual(self.queued(), [])

    def test_an_inbox_edit_made_in_between_is_escalated_not_overwritten(self):
        inbox = self.vault / "INBOX.md"
        inbox.write_text("# Inbox\n\n- [ ] an idea [since:: %s]\n" % TODAY)
        (row,) = self.ids("inbox")
        shutil.rmtree(self.hooks)              # no broker: the request waits in the queue
        p = self.t("done", row["id"], "--vault", self.vault)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        (req_file,) = self.queued()
        req = json.loads(req_file.read_text())
        self.assertEqual((req["op"], req["target_existed"]), ("replace-file", True))
        wq = load_module(SCRIPTS / "gt_write_queue.py", "gt_write_queue_for_inbox_test")
        self.assertIsNone(wq.validate(req, self.vault))
        edited = inbox.read_text() + "- [ ] someone else's line\n"
        inbox.write_text(edited)               # an edit lands before the drain
        self.assertOk(self.py(SCRIPTS / "gt_broker.py", "drain", "--vault", self.vault))
        text = inbox.read_text()
        self.assertIn("someone else's line", text, "the in-between edit was overwritten")
        self.assertNotIn("- [x] an idea", text)
        self.assertEqual(self.broker_rows()[-1]["decision"], "escalate")
        self.assertEqual(self.queued(), [])

    def test_the_broker_itself_writes_directly(self):
        """A conflict task raised inside a drain cannot queue behind that drain's lock."""
        self.assertOk(self.t("add", "from the broker", "--vault", self.vault, "--project",
                             "alpha", env={"GT_BROKER_APPLYING": "1"}))
        self.assertIn("- [ ] from the broker", self.readme_path.read_text())
        self.assertEqual(self.broker_rows(), [])
        self.assertEqual(self.queued(), [])


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
