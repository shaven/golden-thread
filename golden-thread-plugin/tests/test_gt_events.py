"""gt_events.py -- the structured event stream, spooled per session and merged.

Pins: a valid emit lands in THIS session's spool and the merged file; every invalid
field class is refused with exit 2 and nothing written; --dry-run writes nothing; the
merge is idempotent and totally ordered across sessions; validate flags a corrupt
line; list filters; the tool holds the CLI contract.
"""
import json
import unittest

from _harness import Sandbox, TOOLS, REPO, GT, load_module

TOOL = TOOLS / "gt_events.py"


class GtEvents(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        (self.vault / "Projects" / "golden-thread").mkdir(parents=True)
        self.spool = self.vault / "Projects/golden-thread/spool/events"
        self.merged = self.vault / "Projects/golden-thread/events.jsonl"

    def tool(self, *args, **kw):
        return self.py(TOOL, "--vault", self.vault, *args, **kw)

    def emit(self, *args, sid="alpha"):
        return self.tool("--id", sid, "emit", *args)

    def nothing_written(self):
        return not self.spool.exists() and not self.merged.exists()

    def event(self, **over):
        ev = {"v": 1, "ts": "2026-01-01T10:00:00-06:00", "session": "alpha",
              "actor": "claude", "kind": "promote", "item": "Knowledge/x.md",
              "from": None, "to": None, "level_from": None, "level_to": None,
              "project": None}
        ev.update(over)
        return ev

    def spool_lines(self, sid, *events):
        self.spool.mkdir(parents=True, exist_ok=True)
        with open(self.spool / ("%s.jsonl" % sid), "a") as fh:
            for ev in events:
                fh.write((ev if isinstance(ev, str) else json.dumps(ev)) + "\n")

    # -- emit ------------------------------------------------------------------
    def test_valid_emit_lands_in_session_spool_and_merged_file(self):
        p = self.emit("--kind", "promote", "--item", "Knowledge/topic.md",
                      "--from", "Projects/a/research.md", "--to", "Knowledge/topic.md",
                      "--level-from", "3", "--level-to", "4", "--project", "a",
                      "--note", "graduated")
        self.assertOk(p)
        spooled = (self.spool / "alpha.jsonl").read_text().splitlines()
        self.assertEqual(len(spooled), 1)
        ev = json.loads(spooled[0])
        self.assertEqual((ev["v"], ev["session"], ev["actor"], ev["kind"], ev["level_to"]),
                         (1, "alpha", "claude", "promote", 4))
        self.assertRegex(ev["ts"], r"[+-]\d{2}:\d{2}$", "ts carries no offset")
        self.assertEqual(self.merged.read_text().splitlines(), spooled)
        self.assertEqual(list(ev), ["v", "ts", "session", "actor", "kind", "item", "from",
                                    "to", "level_from", "level_to", "project", "note"])

    def test_session_id_comes_from_the_environment_like_gt_log(self):
        p = self.py(TOOL, "--vault", self.vault, "emit", "--kind", "capture",
                    "--item", "INBOX.md", env={"CLAUDE_CODE_SESSION_ID": "sess-env-1"})
        self.assertOk(p)
        self.assertTrue((self.spool / "sess-env-1.jsonl").is_file())

    def test_no_merge_leaves_merged_file_alone(self):
        self.assertOk(self.emit("--kind", "capture", "--item", "INBOX.md", "--no-merge"))
        self.assertTrue((self.spool / "alpha.jsonl").is_file())
        self.assertFalse(self.merged.exists())

    def test_each_invalid_field_class_is_refused_and_nothing_written(self):
        cases = {
            "absolute path": ("--kind", "file", "--item", "/etc/passwd"),
            "absolute from": ("--kind", "file", "--item", "x.md", "--from", "/tmp/x"),
            "dotdot": ("--kind", "file", "--item", "Projects/../../x.md"),
            "bad kind": ("--kind", "teleport", "--item", "x.md"),
            "level 6": ("--kind", "promote", "--item", "x.md", "--level-to", "6"),
            "level text": ("--kind", "promote", "--item", "x.md", "--level-from", "high"),
            "note 121": ("--kind", "capture", "--item", "x.md", "--note", "n" * 121),
            "bad actor": ("--kind", "capture", "--item", "x.md", "--actor", "robot"),
            "bad project": ("--kind", "capture", "--item", "x.md", "--project", "a b"),
        }
        for name, args in cases.items():
            with self.subTest(name):
                p = self.emit(*args)
                self.assertEqual(p.returncode, 2, "%s not refused:\n%s%s"
                                 % (name, p.stdout, p.stderr))
                self.assertIn("REFUSED", p.stderr)
                self.assertTrue(self.nothing_written(), "%s wrote something" % name)

    def test_note_of_exactly_120_is_accepted(self):
        self.assertOk(self.emit("--kind", "capture", "--item", "x.md", "--note", "n" * 120))

    def test_unknown_key_is_refused_by_merge_and_nothing_written(self):
        self.spool_lines("alpha", self.event(), self.event(colour="red"))
        p = self.tool("merge")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("unknown key", p.stderr)
        self.assertFalse(self.merged.exists(), "merge wrote despite an invalid line")

    def test_dry_run_writes_nothing(self):
        for placement in (("--dry-run", "--id", "alpha", "emit"),
                          ("--id", "alpha", "emit", "--dry-run")):
            p = self.tool(*placement, "--kind", "capture", "--item", "INBOX.md")
            self.assertOk(p)
            self.assertIn("dry run", p.stdout)
            self.assertTrue(self.nothing_written())
        self.spool_lines("alpha", self.event())
        self.assertOk(self.tool("merge", "--dry-run"))
        self.assertFalse(self.merged.exists())

    # -- merge -----------------------------------------------------------------
    def test_merge_is_ordered_across_sessions_and_idempotent(self):
        # Written out of order; one stamp in a different zone that is EARLIER as an
        # instant but later as a string; identical instants tie-break by session.
        self.spool_lines("bravo",
                         self.event(session="bravo", ts="2026-01-01T12:00:00-06:00", item="b2"),
                         self.event(session="bravo", ts="2026-01-01T10:00:00-06:00", item="b1"))
        self.spool_lines("alpha",
                         self.event(ts="2026-01-01T10:00:00-06:00", item="a1"),
                         self.event(ts="2026-01-01T15:30:00+00:00", item="a0"))
        self.assertOk(self.tool("merge"))
        items = [json.loads(l)["item"] for l in self.merged.read_text().splitlines()]
        # a0 is 15:30Z, before a1/b1 at 16:00Z (tie -> session), then b2 at 18:00Z.
        self.assertEqual(items, ["a0", "a1", "b1", "b2"])
        first = self.merged.read_bytes()
        p = self.tool("merge")
        self.assertOk(p)
        self.assertIn("unchanged", p.stdout)
        self.assertEqual(self.merged.read_bytes(), first, "second merge changed the file")

    # -- validate --------------------------------------------------------------
    def test_validate_flags_a_hand_corrupted_line(self):
        self.assertOk(self.emit("--kind", "capture", "--item", "INBOX.md"))
        self.assertOk(self.tool("validate"))
        with open(self.merged, "a") as fh:
            fh.write('{"v":1,"ts":"nope"\n')
            fh.write(json.dumps(self.event(level_to=6)) + "\n")
        p = self.tool("validate", str(self.merged))
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn(":2:", p.stdout)
        self.assertIn(":3:", p.stdout)
        self.assertIn("2 invalid", p.stdout)
        self.assertEqual(self.tool("validate").returncode, 1)

    # -- list ------------------------------------------------------------------
    def test_list_filters_and_is_read_only(self):
        self.spool_lines("alpha",
                         self.event(ts="2026-01-01T10:00:00+00:00", kind="capture",
                                    item="i1", project="p1"),
                         self.event(ts="2026-02-01T10:00:00+00:00", kind="promote",
                                    item="i2", project="p1"),
                         self.event(ts="2026-03-01T10:00:00+00:00", kind="promote",
                                    item="i3", project="p2"))

        def items(*args):
            p = self.tool("list", "--json", *args)
            self.assertOk(p)
            return [json.loads(l)["item"] for l in p.stdout.splitlines()]

        self.assertEqual(items(), ["i1", "i2", "i3"])
        self.assertEqual(items("--kind", "promote"), ["i2", "i3"])
        self.assertEqual(items("--project", "p1"), ["i1", "i2"])
        self.assertEqual(items("--since", "2026-01-15T00:00:00+00:00"), ["i2", "i3"])
        self.assertEqual(items("--kind", "promote", "--project", "p1"), ["i2"])
        self.assertFalse(self.merged.exists(), "list wrote the merged file")
        self.assertIn("3 event(s)", self.tool("list").stdout)

    # -- CLI contract ----------------------------------------------------------
    def test_holds_the_cli_contract(self):
        """check_cli_contract's own check, pointed at this tool's writing verbs."""
        mod = load_module(REPO / "dev" / "check_cli_contract.py", "gt_cli_contract_ev")
        mod.COVERED = {"templates/tools/gt_events.py": ("emit", "merge")}
        self.assertEqual(mod.check(str(GT)), [])


if __name__ == "__main__":
    unittest.main()
