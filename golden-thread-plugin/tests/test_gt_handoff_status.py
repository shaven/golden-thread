"""gt_handoff_status.py — a handoff is open, deferred to a date, or handled.

The rules that carry weight: a deferral cannot exist without a date (an open-ended one is a
drop wearing a softer word), a handoff whose citing tasks are all closed is handled without a
second step, an upgrade does not resurface every old handoff in the vault, and nothing here
ever writes outside the vault.
"""
import json
import os
import shutil
import unittest

from _harness import Sandbox, SCRIPTS, TOOLS

TOOL = SCRIPTS / "gt_handoff_status.py"
HANDOFF = SCRIPTS / "gt_handoff.py"
TODAY = "2026-09-28"


class StatusBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        self.proj = self.vault / "Projects" / "alpha"
        (self.proj / "handoff").mkdir(parents=True)
        self.readme("")
        self.f = self.proj / "handoff" / "2026-09-28-handoff.md"
        # Fixtures are written as LF bytes: write_text would make them CRLF on Windows, and
        # gt writes handoffs LF (a CRLF handoff is a separate case, not this test's).
        self.f.write_bytes(("---\nstatus: open\n---\n# Handoff\n").encode())
        self.rel = "Projects/alpha/handoff/2026-09-28-handoff.md"

    def readme(self, tasks):
        (self.proj / "README.md").write_bytes(("# alpha\n\n## Tasks\n\n" + tasks).encode())

    def tool(self, *args, today=TODAY):
        return self.py(TOOL, *args, env={"GT_TODAY": today})

    def listed(self, today=TODAY):
        p = self.tool("list", "--vault", self.vault, "--json", today=today)
        self.assertOk(p)
        return json.loads(p.stdout)


class States(StatusBase):
    def test_open_is_listed(self):
        self.assertEqual([r["path"] for r in self.listed()], [self.rel])

    def test_handled_is_not(self):
        self.assertOk(self.tool("mark", self.rel, "--vault", self.vault, "--status", "handled",
                               "--reason", "settled"))
        self.assertEqual(self.listed(), [])
        text = self.f.read_text()
        self.assertIn("status: handled", text)
        self.assertIn("2026-09-28 handled: settled", text, "the reason is recorded")

    def test_all_citing_tasks_closed_means_handled(self):
        self.readme("- [x] Decide X — context in handoff/2026-09-28-handoff.md [p:: 1]\n")
        self.assertEqual(self.listed(), [])

    def test_one_open_citing_task_keeps_it_open_and_is_counted(self):
        self.readme("- [x] A — context in handoff/2026-09-28-handoff.md\n"
                    "- [ ] B — context in handoff/2026-09-28-handoff.md [p:: 1]\n")
        (r,) = self.listed()
        self.assertEqual((r["open_items"], r["closed_items"]), (1, 1))


class Deferral(StatusBase):
    def test_a_deferral_needs_a_date(self):
        p = self.tool("mark", self.rel, "--vault", self.vault, "--status", "deferred")
        self.assertEqual(p.returncode, 2)
        self.assertIn("--until", p.stderr)
        self.assertIn("status: open", self.f.read_text(), "a refused mark changes nothing")

    def test_a_deferral_to_today_or_earlier_is_refused(self):
        for d in ("2026-09-28", "2026-09-01"):
            p = self.tool("mark", self.rel, "--vault", self.vault, "--status", "deferred",
                         "--until", d)
            self.assertEqual(p.returncode, 3, d)

    def test_deferred_is_hidden_until_the_date_then_open_again(self):
        self.assertOk(self.tool("mark", self.rel, "--vault", self.vault, "--status", "deferred",
                               "--until", "2026-10-05", "--reason", "after the release"))
        self.assertEqual(self.listed(today="2026-10-04"), [])
        (r,) = self.listed(today="2026-10-05")
        self.assertIn("deferral ended", r["note"])

    def test_until_is_cleared_when_it_is_reopened(self):
        self.tool("mark", self.rel, "--vault", self.vault, "--status", "deferred",
                 "--until", "2026-10-05")
        self.assertOk(self.tool("mark", self.rel, "--vault", self.vault, "--status", "open"))
        # the queue's set-property cannot delete a key (0.17.11), so the date is replaced
        self.assertNotIn("2026-10-05", self.f.read_text().split("## Status log")[0])
        self.assertIn("until: none", self.f.read_text())
        (r,) = self.listed(today="2026-09-29")
        self.assertEqual((r["status"], r["until"]), ("open", None))


class Legacy(StatusBase):
    def test_no_status_and_recent_is_open(self):
        self.f.write_bytes(("# Handoff by hand\n").encode())
        (r,) = self.listed()
        self.assertIn("no status", r["note"])

    def test_no_status_and_old_is_history(self):
        import os, time
        self.f.write_bytes(("# old\n").encode())
        t = time.time() - 20 * 86400
        os.utime(self.f, (t, t))
        self.assertEqual(self.listed(), [])

    def test_mark_adds_frontmatter_to_a_file_without_any(self):
        self.f.write_bytes(("# Handoff by hand\n\nbody\n").encode())
        self.assertOk(self.tool("mark", self.rel, "--vault", self.vault, "--status", "handled"))
        text = self.f.read_text()
        self.assertTrue(text.startswith("---\nstatus: handled\n---\n"), text[:80])
        self.assertIn("body", text)


class Safety(StatusBase):
    def test_it_refuses_a_file_outside_the_vault(self):
        outside = self.tmp / "elsewhere.md"
        outside.write_bytes(("# not in the vault\n").encode())
        p = self.tool("mark", str(outside), "--vault", self.vault, "--status", "handled")
        self.assertEqual(p.returncode, 3)
        self.assertEqual(outside.read_text(), "# not in the vault\n")

    def test_dry_run_writes_nothing(self):
        before = self.f.read_text()
        self.assertOk(self.tool("mark", self.rel, "--vault", self.vault, "--status", "handled",
                               "--dry-run"))
        self.assertEqual(self.f.read_text(), before)

    def test_gt_handoff_writes_status_open(self):
        """The writer and the reader must agree, or every new handoff is 'legacy'."""
        self.f.unlink()
        p = self.py(HANDOFF, "--vault", self.vault, "--project", "alpha")
        self.assertOk(p)
        (new,) = list((self.proj / "handoff").glob("*.md"))
        self.assertIn("status: open", new.read_text().split("# Handoff")[0])
        (r,) = self.listed()
        self.assertEqual(r["recorded"], "open")


class ThroughTheQueue(StatusBase):
    """0.17.11: `mark` queues set-property + append; the broker writes the handoff."""

    def rows(self):
        out = []
        for f in sorted((self.vault / "Projects/golden-thread/spool/broker").glob("log-*.jsonl")):
            out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return out

    def queued(self):
        q = self.vault / "Projects/golden-thread/spool/queue"
        return sorted(q.glob("*.json")) if q.is_dir() else []

    def test_mark_arrives_as_set_property_and_append(self):
        self.assertOk(self.tool("mark", self.rel, "--vault", self.vault, "--status", "deferred",
                               "--until", "2026-10-05", "--reason", "later"))
        self.assertEqual([(r["op"], r.get("section"), r["decision"]) for r in self.rows()],
                         [("set-property", None, "apply"), ("set-property", None, "apply"),
                          ("append", "Status log", "apply")])
        text = self.f.read_text()
        self.assertIn("status: deferred", text)
        self.assertIn("until: 2026-10-05", text)
        self.assertIn("- 2026-09-28 deferred until 2026-10-05: later", text)
        self.assertEqual(self.queued(), [])

    def claim(self):
        tools = self.vault / "Projects/golden-thread/tools"
        tools.mkdir(parents=True, exist_ok=True)
        shutil.copy(TOOLS / "gt_session.py", tools / "gt_session.py")
        env = {"CLAUDE_SESSION_ID": "holder", "CLAUDE_PID": str(os.getpid())}
        self.assertOk(self.py(tools / "gt_session.py", "--vault", self.vault, "register",
                              "--task", "t", "--files", self.rel, env=env))
        return tools / "gt_session.py", env

    def test_a_handoff_without_frontmatter_is_one_replace_file_through_the_broker(self):
        self.f.write_bytes(("# Handoff by hand\n\nbody\n").encode())
        self.assertOk(self.tool("mark", self.rel, "--vault", self.vault, "--status", "handled",
                               "--reason", "settled"))
        text = self.f.read_text()
        self.assertTrue(text.startswith("---\nstatus: handled\n---\n"), text[:80])
        self.assertIn("body", text)
        self.assertIn("- 2026-09-28 handled: settled", text)
        self.assertEqual([(r["op"], r["decision"]) for r in self.rows()],
                         [("replace-file", "apply")])
        self.assertEqual(self.queued(), [])

    def test_an_edit_in_between_is_escalated_not_overwritten(self):
        self.f.write_bytes(("# Handoff by hand\n\nbody\n").encode())
        sess, env = self.claim()                  # holds the request in the queue
        for t in ("gt_task.py", "gt_tasks.py"):   # the broker raises its #conflict task with these
            shutil.copy(TOOLS / t, sess.parent / t)
        p = self.tool("mark", self.rel, "--vault", self.vault, "--status", "handled")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("queued, NOT marked yet", p.stderr)
        (req_file,) = self.queued()
        req = json.loads(req_file.read_text())
        self.assertEqual((req["op"], req["target_existed"]), ("replace-file", True))
        self.f.write_bytes(("# Handoff by hand\n\nbody, edited by the holder\n").encode())
        self.assertOk(self.py(sess, "--vault", self.vault, "release", env=env))
        self.assertOk(self.py(SCRIPTS / "gt_broker.py", "drain", "--vault", self.vault))
        self.assertEqual(self.f.read_text(), "# Handoff by hand\n\nbody, edited by the holder\n")
        self.assertEqual(self.rows()[-1]["decision"], "escalate")
        self.assertIn("#conflict", (self.proj / "README.md").read_text())
        self.assertEqual(self.queued(), [])

    def test_a_handoff_claimed_by_a_live_session_stays_queued(self):
        self.claim()
        before = self.f.read_text()
        p = self.tool("mark", self.rel, "--vault", self.vault, "--status", "handled")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("queued, NOT marked yet", p.stderr)
        self.assertEqual(self.f.read_text(), before)
        self.assertEqual(len(self.queued()), 2, "status + status-log line both wait")
        self.assertEqual({r["decision"] for r in self.rows()}, {"held"})


if __name__ == "__main__":
    unittest.main()
