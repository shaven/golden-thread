"""gt_handoff_status.py — a handoff is open, deferred to a date, or handled.

The rules that carry weight: a deferral cannot exist without a date (an open-ended one is a
drop wearing a softer word), a handoff whose citing tasks are all closed is handled without a
second step, an upgrade does not resurface every old handoff in the vault, and nothing here
ever writes outside the vault.
"""
import json
import unittest

from _harness import Sandbox, SCRIPTS

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
        self.f.write_text("---\nstatus: open\n---\n# Handoff\n")
        self.rel = "Projects/alpha/handoff/2026-09-28-handoff.md"

    def readme(self, tasks):
        (self.proj / "README.md").write_text("# alpha\n\n## Tasks\n\n" + tasks)

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
        self.assertNotIn("until:", self.f.read_text())


class Legacy(StatusBase):
    def test_no_status_and_recent_is_open(self):
        self.f.write_text("# Handoff by hand\n")
        (r,) = self.listed()
        self.assertIn("no status", r["note"])

    def test_no_status_and_old_is_history(self):
        import os, time
        self.f.write_text("# old\n")
        t = time.time() - 20 * 86400
        os.utime(self.f, (t, t))
        self.assertEqual(self.listed(), [])

    def test_mark_adds_frontmatter_to_a_file_without_any(self):
        self.f.write_text("# Handoff by hand\n\nbody\n")
        self.assertOk(self.tool("mark", self.rel, "--vault", self.vault, "--status", "handled"))
        text = self.f.read_text()
        self.assertTrue(text.startswith("---\nstatus: handled\n---\n"), text[:80])
        self.assertIn("body", text)


class Safety(StatusBase):
    def test_it_refuses_a_file_outside_the_vault(self):
        outside = self.tmp / "elsewhere.md"
        outside.write_text("# not in the vault\n")
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


if __name__ == "__main__":
    unittest.main()
