"""gt_log.py — log.md as a generated merge of per-session spool files.

The behaviours these pin, and why each one matters, are in gt_spool.py's docstring.
The one worth restating: `gt_closeout.py` parses log.md, so the merge must render
lines verbatim. A test that only checked "the line is present somewhere" would pass
while closeout quietly stopped finding it.
"""
import re
import unittest
from _harness import Sandbox, TOOLS, SCRIPTS

WORK = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\s+[^\s\[]+){0,2}\s+\[work\]\s+(.+?)"
                  r"(?:\s+(?:—|–|--?)\s|\s*$)")


class GtLog(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.log = self.vault / "log.md"

    def tool(self, *args, **kw):
        return self.py(TOOLS / "gt_log.py", "--vault", self.vault, *args, **kw)

    def add(self, text, sid="alpha"):
        return self.tool("--id", sid, "add", text)

    def migrate(self):
        p = self.tool("migrate")
        self.assertOk(p, "migrate failed")
        return p

    # -- the core guarantee ------------------------------------------------------
    def test_add_does_not_touch_log_md(self):
        """A session writes its own file. log.md is untouched until a merge."""
        self.migrate()
        before = self.log.read_bytes()
        self.assertOk(self.tool("--id", "alpha", "add", "2026-01-01 [work] x — y", "--no-merge"))
        self.assertEqual(self.log.read_bytes(), before,
                         "add wrote to log.md; it must only write the session's spool")
        spool = self.vault / "Projects/golden-thread/spool/log/alpha.md"
        self.assertTrue(spool.is_file(), "no spool file was created")
        self.assertIn("[work] x", spool.read_text())

    def test_two_sessions_both_land(self):
        self.migrate()
        self.add("2026-01-01 10:00 CDT [work] one — from alpha", "alpha")
        self.add("2026-01-01 10:00 CDT [work] two — from bravo", "bravo")
        body = self.log.read_text()
        self.assertIn("from alpha", body)
        self.assertIn("from bravo", body)
        d = self.vault / "Projects/golden-thread/spool/log"
        self.assertIn("from alpha", (d / "alpha.md").read_text())
        self.assertNotIn("from bravo", (d / "alpha.md").read_text(),
                         "one session's entry landed in another's spool")

    def test_merge_is_idempotent(self):
        self.migrate()
        self.add("2026-01-01 [work] one — x")
        first = self.log.read_bytes()
        self.assertOk(self.tool("merge"))
        self.assertEqual(self.log.read_bytes(), first, "second merge changed the file")

    def test_order_is_by_timestamp_not_write_order(self):
        self.migrate()
        self.add("2026-01-02 10:00 CDT [work] one — later stamp, written first", "alpha")
        self.add("2026-01-01 09:00 CDT [work] one — earlier stamp, written second", "alpha")
        lines = [l for l in self.log.read_text().splitlines() if "[work]" in l]
        self.assertLess(lines.index([l for l in lines if "earlier stamp" in l][0]),
                        lines.index([l for l in lines if "later stamp" in l][0]),
                        "entries are not ordered by timestamp")

    def test_same_timestamp_orders_deterministically(self):
        """Identical stamps must not fall back to filesystem order."""
        self.migrate()
        self.add("2026-01-01 10:00 CDT [work] one — from alpha", "alpha")
        self.add("2026-01-01 10:00 CDT [work] one — from bravo", "bravo")
        first = self.log.read_text()
        for _ in range(3):
            self.assertOk(self.tool("merge"))
            self.assertEqual(self.log.read_text(), first, "order changed between merges")

    # -- the constraint found in 0.10.0 -----------------------------------------
    def test_closeout_still_finds_the_work_line(self):
        """gt_closeout parses log.md; the merge must render lines verbatim."""
        self.migrate()
        self.add("2026-01-05 10:00 CDT [work] demo-project — did a thing")
        hits = [m.group(1) for l in self.log.read_text().splitlines()
                if (m := WORK.match(l))
                and "demo-project" in [s.strip() for s in m.group(2).split(",")]]
        self.assertEqual(hits, ["2026-01-05"],
                         "closeout's regex no longer matches the rendered line")

    def test_closeout_runs_against_a_generated_log(self):
        self.migrate()
        self.add("2026-01-05 10:00 CDT [work] demo-project — did a thing")
        p = self.py(TOOLS / "gt_closeout.py", "--vault", self.vault, "candidates")
        self.assertOk(p, "gt_closeout.py failed on a generated log.md")

    # -- migration ---------------------------------------------------------------
    def test_migration_round_trips_byte_for_byte(self):
        original = self.log.read_text()
        self.migrate()
        base = self.vault / "Projects/golden-thread/spool/log/0000-baseline.md"
        self.assertEqual(base.read_text(), original, "baseline is not the original file")
        rendered = self.log.read_text()
        self.assertTrue(rendered.rstrip("\n").endswith(original.rstrip("\n")),
                        "merge did not reproduce the original content")

    def test_migrate_refuses_twice(self):
        self.migrate()
        before = self.log.read_bytes()
        self.tool("migrate")
        self.assertEqual(self.log.read_bytes(), before,
                         "a second migrate changed the file; it must be one-time")

    def test_merge_refuses_before_migration(self):
        """Rendering over an un-migrated log.md would destroy every historical line."""
        p = self.tool("merge")
        self.assertNotEqual(p.returncode, 0, "merge should refuse without a baseline")
        self.assertIn("migrate", (p.stdout + p.stderr).lower())

    # -- robustness --------------------------------------------------------------
    def test_truncated_spool_keeps_complete_lines(self):
        self.migrate()
        self.add("2026-01-01 [work] one — complete line")
        spool = self.vault / "Projects/golden-thread/spool/log/alpha.md"
        with open(spool, "a", encoding="utf-8") as fh:
            fh.write("2026-01-02 [work] one — truncated, no newline")
        self.assertOk(self.tool("merge"))
        body = self.log.read_text()
        self.assertIn("complete line", body)
        self.assertIn("truncated", body, "a final line without a newline was dropped")

    def test_status_reports_each_session(self):
        self.migrate()
        self.add("2026-01-01 [work] one — a", "alpha")
        self.add("2026-01-01 [work] one — b", "bravo")
        out = self.tool("status").stdout
        self.assertIn("alpha.md", out)
        self.assertIn("bravo.md", out)


if __name__ == "__main__":
    unittest.main()
