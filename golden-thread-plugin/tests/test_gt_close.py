"""gt_close.py and the gt_task.py verbs it needs -- closing a project, a task, a handoff (0.18.1).

Request 2026-10-01-gt-close-project, revised by the owner: a project closes only once every open
task and handoff has a disposition (close, drop, move, keep shelved at p:: 7); it is then ARCHIVED
IN PLACE (stage: archived) unless --move asks for Archive/<slug>/; a task close is gt_task done +
a task.done event; a handoff close is status `handled` + a handoff.close event. Every vault write
must arrive through the write queue (a broker log row), never a direct edit.
"""
import json
import os
import shutil
import unittest

from test_gt_tasks import readme
from _harness import Sandbox, SCRIPTS

CLOSE = SCRIPTS / "gt_close.py"
TODAY = "2026-09-28"
HANDOFF_REL = "Projects/alpha/handoff/2026-09-28-handoff.md"


class CloseBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.tools = self.vault / "Projects" / "golden-thread" / "tools"
        self.spool = self.vault / "Projects" / "golden-thread" / "spool"
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True, exist_ok=True)
        for n in ("gt_broker.py", "gt_write_queue.py", "gt_review_target.py"):
            shutil.copy(SCRIPTS / n, self.hooks / n)
        for slug in ("alpha", "beta"):
            p = self.py(SCRIPTS / "vault_init.py", "create-project", "--vault", self.vault,
                        "--name", slug, "--title", slug.title(), "--domain", "test")
            self.assertOk(p)
        self.alpha = self.vault / "Projects" / "alpha"
        self.beta = self.vault / "Projects" / "beta"

    # -- helpers ---------------------------------------------------------------
    def run_close(self, *args):
        return self.py(CLOSE, *args, env={"GT_TODAY": TODAY})

    def task(self, *args):
        return self.py(self.tools / "gt_task.py", *args, env={"GT_TODAY": TODAY})

    def add(self, text, project="alpha", *extra):
        p = self.task("add", text, "--vault", self.vault, "--project", project, *extra)
        self.assertOk(p)
        return p.stdout.split()[1]

    def listed(self, *filters):
        p = self.task("list", "--vault", self.vault, *filters, "--json")
        self.assertOk(p)
        return json.loads(p.stdout)

    def events(self):
        f = self.vault / "Projects/golden-thread/events.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def broker_paths(self):
        rows = []
        for f in sorted((self.spool / "broker").glob("log-*.jsonl")):
            rows += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return [r.get("path") for r in rows if r.get("decision") == "apply"]

    def handoff(self, status="open"):
        d = self.alpha / "handoff"
        d.mkdir(exist_ok=True)
        f = d / "2026-09-28-handoff.md"
        f.write_text("---\nstatus: %s\n---\n# Handoff\n\nDo the thing.\n" % status)
        return f

    def stage(self, readme_path):
        for line in readme_path.read_text().splitlines():
            if line.startswith("stage:"):
                return line.split(":", 1)[1].strip()
        return None


class ProjectGate(CloseBase):
    def test_an_open_task_halts_the_close_and_nothing_is_archived(self):
        self.add("Finish the migration", "alpha", "--p", "1")
        p = self.run_close("project", "alpha", "--vault", self.vault)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("Finish the migration", p.stdout)
        self.assertIn("HALT", p.stderr)
        p = self.run_close("project", "alpha", "--vault", self.vault, "--archive",
                           "--reason", "done")
        self.assertEqual(p.returncode, 1)
        self.assertNotEqual(self.stage(self.alpha / "README.md"), "archived")
        self.assertTrue(self.alpha.is_dir())

    def test_a_deferred_task_still_blocks_because_it_comes_back(self):
        tid = self.add("Later thing")
        self.assertOk(self.task("defer", tid, "--vault", self.vault, "--until", "2026-12-01",
                                "--reason", "after the freeze"))
        p = self.run_close("project", "alpha", "--vault", self.vault, "--json")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(json.loads(p.stdout)["undecided"], 1)

    def test_an_open_handoff_blocks(self):
        self.handoff("open")
        p = self.run_close("project", "alpha", "--vault", self.vault, "--json")
        self.assertEqual(p.returncode, 1)
        data = json.loads(p.stdout)
        self.assertEqual([h["path"] for h in data["open_handoffs"]], [HANDOFF_REL])

    def test_research_entries_are_offered_for_graduation(self):
        with open(self.alpha / "research.md", "a") as fh:
            fh.write("\n## 2026-09-20: the relay needs a restart after rotation\n\nbody\n")
        p = self.run_close("project", "alpha", "--vault", self.vault, "--json")
        self.assertIn("2026-09-20: the relay needs a restart after rotation",
                      json.loads(p.stdout)["research_entries"])

    def test_ambiguous_slug_is_refused_not_guessed(self):
        p = self.run_close("project", "nosuch", "--vault", self.vault)
        self.assertEqual(p.returncode, 3)


class Dispositions(CloseBase):
    def test_shelve_keeps_the_line_at_p7_and_out_of_the_list(self):
        tid = self.add("Nice to have", "alpha", "--p", "2")
        self.assertOk(self.task("shelve", tid, "--vault", self.vault, "--reason", "record only"))
        text = (self.alpha / "README.md").read_text()
        self.assertIn("- [ ] Nice to have", text)
        self.assertIn("[p:: 7]", text)
        self.assertIn("shelved %s: record only" % TODAY, text)
        self.assertEqual(self.listed("alpha"), [])
        self.assertIn("Projects/alpha/README.md", self.broker_paths(), "not through the queue")

    def test_move_writes_the_destination_first_then_checks_the_source_off(self):
        tid = self.add("Belongs elsewhere", "alpha", "--p", "1")
        p = self.task("move", tid, "--vault", self.vault, "--to", "beta", "--reason", "beta owns it")
        self.assertOk(p)
        self.assertEqual(self.listed("alpha"), [])
        moved = self.listed("beta")
        self.assertEqual(len(moved), 1)
        self.assertIn("Belongs elsewhere", moved[0]["text"])
        self.assertEqual(moved[0]["p"], 1, "fields travel with the line")
        self.assertIn("moved to beta %s: beta owns it" % TODAY,
                      (self.alpha / "README.md").read_text())
        paths = self.broker_paths()
        self.assertIn("Projects/beta/README.md", paths)
        self.assertIn("Projects/alpha/README.md", paths)

    def test_move_to_a_missing_project_changes_nothing(self):
        tid = self.add("Stay put")
        before = (self.alpha / "README.md").read_text()
        p = self.task("move", tid, "--vault", self.vault, "--to", "nosuch", "--reason", "x")
        self.assertEqual(p.returncode, 3)
        self.assertEqual((self.alpha / "README.md").read_text(), before)


class ArchiveInPlace(CloseBase):
    def test_after_every_item_is_decided_it_archives_in_place(self):
        tid = self.add("Keep as record")
        self.assertOk(self.task("shelve", tid, "--vault", self.vault, "--reason", "record"))
        self.assertOk(self.run_close("project", "alpha", "--vault", self.vault))
        p = self.run_close("project", "alpha", "--vault", self.vault, "--archive",
                           "--reason", "shipped")
        self.assertOk(p)
        self.assertTrue(self.alpha.is_dir(), "in place: the folder does not move")
        self.assertEqual(self.stage(self.alpha / "README.md"), "archived")
        index = (self.vault / "Projects" / "README.md").read_text()
        row = [l for l in index.splitlines() if "`alpha`" in l][0]
        self.assertIn("archived", row)
        self.assertNotIn("active", row)
        self.assertIn("archive", [e["kind"] for e in self.events()])

    def test_dry_run_archives_nothing(self):
        p = self.run_close("project", "alpha", "--vault", self.vault, "--archive",
                           "--reason", "x", "--dry-run")
        self.assertOk(p)
        self.assertNotEqual(self.stage(self.alpha / "README.md"), "archived")


class ArchiveMove(CloseBase):
    def setUp(self):
        super().setUp()
        with open(self.beta / "research.md", "a") as fh:
            fh.write("\n## 2026-09-20: see alpha\n\nAs in Projects/alpha/design.md and "
                     "[[Projects/alpha/README|alpha]].\n")

    def test_move_relocates_to_archive_and_repoints_links_through_the_queue(self):
        p = self.run_close("project", "alpha", "--vault", self.vault, "--archive", "--move",
                           "--reason", "retired")
        self.assertOk(p)
        self.assertFalse(self.alpha.exists(), "Projects/alpha/ should be gone")
        moved = self.vault / "Archive" / "alpha"
        self.assertTrue((moved / "README.md").is_file())
        self.assertEqual(self.stage(moved / "README.md"), "archived")
        research = (self.beta / "research.md").read_text()
        self.assertIn("Archive/alpha/design.md", research)
        self.assertIn("[[Archive/alpha/README|alpha]]", research)
        self.assertNotIn("Projects/alpha/", research)
        self.assertIn("Projects/beta/research.md", self.broker_paths(),
                      "the re-point must arrive through the write queue")
        index = (self.vault / "Projects" / "README.md").read_text()
        row = [l for l in index.splitlines() if "`alpha`" in l][0]
        self.assertIn("../Archive/alpha/", row)
        self.assertIn("archived", row)
        kinds = [e["kind"] for e in self.events()]
        self.assertIn("relocate", kinds)

    def test_move_dry_run_moves_nothing(self):
        p = self.run_close("project", "alpha", "--vault", self.vault, "--archive", "--move",
                           "--reason", "x", "--dry-run")
        self.assertOk(p)
        self.assertTrue(self.alpha.is_dir())
        self.assertFalse((self.vault / "Archive").exists())
        self.assertIn("would-move", p.stdout)

    def test_move_refuses_while_a_live_session_claims_a_file_in_it(self):
        sess = self.tools / "gt_session.py"
        env = {"CLAUDE_SESSION_ID": "other-session", "CLAUDE_PID": str(os.getpid())}
        self.assertOk(self.py(sess, "--vault", self.vault, "register", "--task", "t", env=env))
        self.assertOk(self.py(sess, "--vault", self.vault, "claim", "Projects/alpha/design.md",
                              env=env))
        p = self.run_close("project", "alpha", "--vault", self.vault, "--archive", "--move",
                           "--reason", "x")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertTrue(self.alpha.is_dir())
        self.assertIn("claimed by live session", p.stdout)

    def test_move_without_archive_is_a_usage_error(self):
        p = self.run_close("project", "alpha", "--vault", self.vault, "--move")
        self.assertEqual(p.returncode, 2)


class TaskClose(CloseBase):
    def test_close_checks_the_line_and_emits_task_done(self):
        tid = self.add("Ship the thing", "alpha", "--p", "1")
        # the rollup emits task events only once the stream holds one (gt_events docstring)
        self.assertOk(self.py(self.tools / "gt_tasks.py", "--vault", self.vault))
        self.assertOk(self.py(self.tools / "gt_events.py", "--vault", self.vault, "emit",
                              "--kind", "task.open", "--item",
                              "Projects/beta/README.md#t-0000000000", "--project", "beta"))
        self.assertOk(self.py(self.tools / "gt_tasks.py", "--vault", self.vault))
        p = self.run_close("task", tid, "--vault", self.vault, "--reason", "released")
        self.assertOk(p)
        text = (self.alpha / "README.md").read_text()
        self.assertIn("- [x] Ship the thing", text)
        self.assertIn("done %s: released" % TODAY, text)
        self.assertIn("Projects/alpha/README.md", self.broker_paths())
        done = [e for e in self.events() if e["kind"] == "task.done"]
        self.assertTrue(any(e["item"].startswith("Projects/alpha/README.md#t-") for e in done),
                        self.events())

    def test_a_stale_id_is_refused(self):
        p = self.run_close("task", "alpha:99:abcdef", "--vault", self.vault)
        self.assertEqual(p.returncode, 1)


class HandoffClose(CloseBase):
    def test_close_marks_handled_and_emits_handoff_close(self):
        f = self.handoff("open")
        p = self.run_close("handoff", HANDOFF_REL, "--vault", self.vault, "--reason", "all settled")
        self.assertOk(p)
        self.assertIn("status: handled", f.read_text())
        self.assertIn(HANDOFF_REL, self.broker_paths())
        ev = [e for e in self.events() if e["kind"] == "retire" and e["item"] == HANDOFF_REL]
        self.assertEqual(len(ev), 1, self.events())
        self.assertTrue(ev[0]["note"].startswith("handoff.close"))

    def test_an_open_citing_task_refuses_the_close(self):
        f = self.handoff("open")
        self.add("Decide X — context in 2026-09-28-handoff.md")
        p = self.run_close("handoff", HANDOFF_REL, "--vault", self.vault, "--reason", "x")
        self.assertEqual(p.returncode, 1)
        self.assertIn("status: open", f.read_text())

    def test_dry_run_marks_nothing(self):
        f = self.handoff("open")
        self.assertOk(self.run_close("handoff", HANDOFF_REL, "--vault", self.vault,
                                     "--reason", "x", "--dry-run"))
        self.assertIn("status: open", f.read_text())


if __name__ == "__main__":
    unittest.main()
