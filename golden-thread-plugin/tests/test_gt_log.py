"""gt_log.py — log.md as a generated merge of per-session spool files.

The behaviours these pin, and why each one matters, are in gt_spool.py's docstring.
The one worth restating: `gt_closeout.py` parses log.md, so the merge must render
lines verbatim. A test that only checked "the line is present somewhere" would pass
while closeout quietly stopped finding it.
"""
import re
import shutil
import unittest
from _harness import Sandbox, TOOLS, SCRIPTS

WORK = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\s+[^\s\[]+){0,2}\s+\[work\]\s+(.+?)"
                  r"(?:\s+(?:—|–|--?)\s|\s*$)")


class GtLog(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.log = self.vault / "log.md"
        # Since 0.14.0 `vault_init fresh` creates a vault that is already migrated. These
        # tests exercise the MIGRATION itself, so put the vault back into the pre-0.11 shape
        # an upgraded vault arrives in: log.md holds the lines, and there is no spool.
        spool = self.vault / "Projects" / "golden-thread" / "spool" / "log"
        baseline = spool / "0000-baseline.md"
        if baseline.is_file():
            self.log.write_bytes(baseline.read_bytes())
            shutil.rmtree(spool)

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

    def test_hand_written_lines_in_generated_log_survive_the_next_add(self):
        """0.15.0 validator: a line typed into the generated log.md vanished at the next
        render (any `add`, including install.sh's upgrade receipt), with no word."""
        self.migrate()
        self.add("2026-01-01 10:00 CDT [work] first — spooled", "alpha")
        hand = "2026-01-02 09:00 CDT [note] typed straight into log.md by the owner"
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(hand + "\n")
        p = self.add("2026-01-03 10:00 CDT [work] later — spooled", "bravo")
        self.assertOk(p)
        body = self.log.read_text()
        self.assertIn(hand, body, "a hand-written log.md line was deleted by the render")
        self.assertIn("hand-written line(s) kept", p.stdout)
        lines = body.splitlines()
        self.assertLess(lines.index(hand), next(i for i, l in enumerate(lines) if "later" in l),
                        "the kept line should sort by its own date")
        # idempotent afterwards: nothing captured twice
        p2 = self.tool("merge")
        self.assertOk(p2)
        self.assertNotIn("hand-written", p2.stdout)
        self.assertEqual(self.log.read_text().count(hand), 1)

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

    def test_non_utf8_bytes_survive_migration_and_merge(self):
        """0.15.0: a latin-1 log.md (`\\xe9t\\xe9`) was read with errors="replace", so the
        baseline and the generated file got U+FFFD, and the round-trip gate compared two
        already-replaced copies and passed. Owner bytes must come through untouched."""
        raw = self.log.read_bytes() + b"2026-01-01 [work] caf\xe9 \xe9t\xe9 \x80 -- latin-1\n"
        self.log.write_bytes(raw)
        self.migrate()
        base = self.vault / "Projects/golden-thread/spool/log/0000-baseline.md"
        self.assertEqual(base.read_bytes(), raw, "baseline bytes differ from the original")
        self.assertNotIn(b"\xef\xbf\xbd", self.log.read_bytes(),
                         "U+FFFD written into log.md: owner bytes were transcoded")
        self.assertTrue(self.log.read_bytes().endswith(raw),
                        "generated log.md does not end with the original bytes")
        self.add("2026-02-01 10:00 CDT [work] later — after migration")
        self.assertIn(b"caf\xe9 \xe9t\xe9 \x80", self.log.read_bytes(),
                      "a later merge transcoded the baseline's bytes")
        self.assertNotIn(b"\xef\xbf\xbd", self.log.read_bytes())
        p = self.tool("merge", "--dry-run")
        self.assertOk(p)
        self.assertIn("unchanged", p.stdout, "dry run misreads a byte-identical file")

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


class GtLogEvent(Sandbox):
    """`add --event`: one structured event beside the log line; plain add unchanged."""

    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.events = self.vault / "Projects/golden-thread/spool/events"
        self.log_spool = self.vault / "Projects/golden-thread/spool/log/alpha.md"

    def add(self, *args):
        return self.py(TOOLS / "gt_log.py", "--vault", self.vault, "--id", "alpha", "add", *args)

    def spooled(self):
        import json
        f = self.events / "alpha.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    LINE = "2026-09-14 10:00 CDT [graduate] Projects/a/research.md → Knowledge/x.md: up"
    EVENT = ("--event", "promote", "--item", "Knowledge/x.md", "--from", "Projects/a/research.md",
             "--to", "Knowledge/x.md", "--level-from", "3", "--level-to", "4", "--project", "a")

    def test_plain_add_emits_nothing(self):
        self.assertOk(self.add("2026-09-14 [work] a — plain"))
        self.assertFalse(self.events.exists(), "plain add wrote an event")

    def test_event_lands_with_the_log_line(self):
        p = self.add(self.LINE, *self.EVENT)
        self.assertOk(p)
        self.assertIn("graduate", self.log_spool.read_text())
        evs = self.spooled()
        self.assertEqual(len(evs), 1)
        e = evs[0]
        self.assertEqual((e["kind"], e["item"], e["from"], e["level_from"], e["level_to"],
                          e["project"], e["session"]),
                         ("promote", "Knowledge/x.md", "Projects/a/research.md", 3, 4, "a", "alpha"))
        self.assertTrue(e["note"].startswith("[graduate] Projects/a/research.md"),
                        "the note should be the log line without its date stamp: " + e["note"])
        self.assertTrue((self.vault / "Projects/golden-thread/events.jsonl").is_file())

    def test_dry_run_writes_neither(self):
        p = self.add(self.LINE, *self.EVENT, "--dry-run")
        self.assertOk(p)
        self.assertIn("would also spool the event", p.stdout)
        self.assertFalse(self.events.exists())
        self.assertFalse(self.log_spool.exists())

    def test_event_without_item_is_refused_before_anything_is_written(self):
        p = self.add(self.LINE, "--event", "promote")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertFalse(self.log_spool.exists())
        self.assertFalse(self.events.exists())

    def test_a_failing_event_never_fails_the_log_line(self):
        bad = self.add(self.LINE, "--event", "teleport", "--item", "Knowledge/x.md")
        self.assertOk(bad, "an invalid event failed add")
        self.assertIn("NOT recorded", bad.stderr)
        self.assertIn("graduate", self.log_spool.read_text())
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.events.write_text("blocks the spool directory\n")
        blocked = self.add("2026-09-14 [work] a — second", *self.EVENT)
        self.assertOk(blocked, "an unwritable event spool failed add")
        self.assertIn("NOT recorded", blocked.stderr)
        self.assertIn("second", self.log_spool.read_text())


if __name__ == "__main__":
    unittest.main()
