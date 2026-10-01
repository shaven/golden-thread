"""gt_broker.py audit -- the read-only report of vault .md files changed outside the broker.

A detection aid, not a guard: it cannot name the writer, so it lists every governed .md whose
mtime is recent and that has no broker `apply` row near that mtime. The assertions are about
what is and is not listed, that --json parses, and that the report never writes the vault.
"""
import json
import os
import shutil
import time
import unittest

from _harness import Sandbox, SCRIPTS, TOOLS

QUEUE = SCRIPTS / "gt_write_queue.py"
BROKER = SCRIPTS / "gt_broker.py"
RESEARCH = "Projects/quokka/research.md"


class AuditBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        gt = self.vault / "Projects" / "golden-thread"
        (gt / "tools").mkdir(parents=True)
        (gt / "sessions").mkdir()
        (gt / "README.md").write_text("# golden-thread\n\n## Tasks\n")
        for tool in ("gt_task.py", "gt_tasks.py", "gt_session.py"):
            shutil.copy(TOOLS / tool, gt / "tools" / tool)
        q = self.vault / "Projects" / "quokka"
        q.mkdir(parents=True)
        (q / "README.md").write_text("# quokka\n\n## Tasks\n")
        (q / "research.md").write_text("# quokka research\n\n## Findings\n\n- first\n")
        # Everything made by setUp is "old": outside any window the tests use.
        old = time.time() - 3 * 86400
        for root, _dirs, files in os.walk(self.vault):
            for name in files:
                os.utime(os.path.join(root, name), (old, old))

    def audit(self, *extra):
        p = self.py(BROKER, "audit", "--vault", self.vault, "--json", *extra)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return [r["path"] for r in json.loads(p.stdout)]

    def write(self, rel, text="x\n", age=0):
        f = self.vault / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        if age:
            t = time.time() - age
            os.utime(f, (t, t))

    def snapshot(self):
        out = {}
        for root, _dirs, files in os.walk(self.vault):
            for name in files:
                p = os.path.join(root, name)
                with open(p, "rb") as fh:
                    out[p] = (os.stat(p).st_mtime, fh.read())
        return out


class AuditReport(AuditBase):
    def test_a_brokered_append_is_not_listed(self):
        p = self.py(QUEUE, "--vault", self.vault, "--path", RESEARCH, "--op", "append",
                    "--content", "a brokered finding", "--section", "Findings",
                    "--session", "sess-a")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        d = self.py(BROKER, "drain", "--vault", self.vault)
        self.assertEqual(d.returncode, 0, d.stdout + d.stderr)
        self.assertIn("a brokered finding", (self.vault / RESEARCH).read_text())
        self.assertNotIn(RESEARCH, self.audit())

    def test_a_direct_write_is_listed(self):
        self.write(RESEARCH, "# quokka research\n\nedited directly\n")
        self.write("Projects/quokka/notes.md")
        listed = self.audit()
        self.assertIn(RESEARCH, listed)
        self.assertIn("Projects/quokka/notes.md", listed)

    def test_a_direct_write_after_a_brokered_one_is_listed(self):
        self.py(QUEUE, "--vault", self.vault, "--path", RESEARCH, "--op", "append",
                "--content", "brokered", "--session", "sess-a")
        self.py(BROKER, "drain", "--vault", self.vault)
        later = time.time() + 60                    # well past the 5 s tolerance
        os.utime(self.vault / RESEARCH, (later, later))
        self.assertIn(RESEARCH, self.audit())

    def test_exempt_and_generated_files_are_not_listed(self):
        for rel in ("Sources/s.md", "core-rules/r.md", ".obsidian/o.md", ".trash/t.md",
                    ".gt/g.md", "Projects/golden-thread/spool/log/x.md",
                    "Projects/golden-thread/sessions/s.md", "Projects/golden-thread/tools/t.md",
                    "log.md", "TASKS.md", "Projects/quokka/decisions.md",
                    "Projects/quokka/review-queue.md", "Projects/quokka/notes.txt"):
            self.write(rel)
        self.write("Projects/quokka/visible.md")
        self.assertEqual(self.audit(), ["Projects/quokka/visible.md"])

    def test_files_older_than_the_window_are_not_listed(self):
        self.write("Projects/quokka/old.md", age=3 * 3600)
        self.write("Projects/quokka/new.md")
        self.assertEqual(self.audit("--since", "1"), ["Projects/quokka/new.md"])
        self.assertIn("Projects/quokka/old.md", self.audit("--since", "4"))

    def test_json_rows_carry_path_mtime_and_git(self):
        self.write("Projects/quokka/notes.md")
        p = self.py(BROKER, "audit", "--vault", self.vault, "--json")
        rows = json.loads(p.stdout)
        self.assertIsInstance(rows, list)
        self.assertEqual(set(rows[0]), {"path", "mtime", "git"})
        self.assertIsNone(rows[0]["git"], "no git repo: git state is not guessed")

    def test_text_output_says_it_is_not_an_alarm_and_exits_zero(self):
        self.write("Projects/quokka/notes.md")
        p = self.py(BROKER, "audit", "--vault", self.vault)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Obsidian edits appear here too", p.stdout)
        self.assertIn("not an alarm", p.stdout)
        self.assertIn("1 file(s) changed", p.stdout)

    def test_audit_writes_nothing(self):
        self.write("Projects/quokka/notes.md")
        before = self.snapshot()
        self.py(BROKER, "audit", "--vault", self.vault)
        self.py(BROKER, "audit", "--vault", self.vault, "--dry-run", "--json")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
