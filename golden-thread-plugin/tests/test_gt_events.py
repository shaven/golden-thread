"""gt_events.py -- the structured event stream, spooled per session and merged.

Pins: a valid emit lands in THIS session's spool and the merged file; every invalid
field class is refused with exit 2 and nothing written; --dry-run writes nothing; the
merge is idempotent and totally ordered across sessions; validate flags a corrupt
line; list filters; the tool holds the CLI contract.
"""
import json
import shutil
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
        mod.COVERED = {"templates/tools/gt_events.py": ("emit", "merge", "backfill")}
        self.assertEqual(mod.check(str(GT)), [])

    # -- library: the emitters' entry point --------------------------------------
    def lib(self):
        return load_module(TOOL, "gt_events_lib")

    def test_safe_emit_never_raises_and_says_so(self):
        """An emitter failure must not fail the operation it describes."""
        import contextlib
        import io
        E = self.lib()
        blocker = self.spool
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("not a directory\n")          # the spool cannot be created
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(E.safe_emit(self.vault, "capture", "INBOX.md", sid="alpha"))
            self.assertIsNone(E.safe_emit(self.vault, "teleport", "INBOX.md", sid="alpha"))
        self.assertEqual(err.getvalue().count("NOT recorded"), 2, err.getvalue())
        self.assertEqual(blocker.read_text(), "not a directory\n")

    def test_emit_dry_run_validates_and_writes_nothing(self):
        E = self.lib()
        ev = E.emit(self.vault, "adr", "Projects/p/decisions.md#ADR-1", level_to=3,
                    project="p", sid="alpha", dry_run=True)
        self.assertEqual((ev["kind"], ev["level_to"]), ("adr", 3))
        self.assertTrue(self.nothing_written())

    def test_clip_and_note_fields(self):
        E = self.lib()
        self.assertEqual(len(E.clip("x" * 500)), 120)
        self.assertEqual(E.clip("a\tb\n c"), "a b c")
        self.assertEqual(E.note_fields("backfill conf=0.8 src=git:abc123 text"),
                         {"conf": 0.8, "src": "git:abc123"})

    def test_task_item_ignores_fields_but_not_wording(self):
        E = self.lib()
        a = E.task_item("Projects/a/README.md", "write it")
        self.assertRegex(a, r"^Projects/a/README\.md#t-[0-9a-f]{10}$")
        self.assertNotEqual(a, E.task_item("Projects/a/README.md", "write it again"))
        E.validate_event(dict(self.event(kind="task.open", item=a)))

    def test_level_of_follows_the_ladder(self):
        E = self.lib()
        cases = {"Projects/a/memory/n.md": 2, "Projects/a/research.md": 3,
                 "Projects/a/b/decisions.md": 3, "Knowledge/x.md": 4,
                 "global-memory/y.md": 5, "Projects/a/README.md": None, "INBOX.md": None}
        for path, level in cases.items():
            self.assertEqual(E.level_of(path), level, path)


class Backfill(Sandbox):
    """Backfill on a throwaway git vault: known history in, known events out, twice."""

    README = ("---\ntype: project\nslug: %s\nstage: active\n---\n\n# %s\n\n## Tasks\n\n"
              "%s\n\n## Notes\n")

    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git not installed")
        self.vault = self.tmp / "vault"
        self.merged = self.vault / "Projects/golden-thread/events.jsonl"
        self.spool = self.vault / "Projects/golden-thread/spool/events"
        self.git_init(self.vault, commit=False)

    def write(self, rel, text):
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg, date):
        self.run_cmd(["git", "-C", self.vault, "add", "-A"])
        p = self.run_cmd(["git", "-C", self.vault, "commit", "-q", "-m", msg],
                         env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        self.assertOk(p, "fixture commit failed")

    def git(self, *args):
        self.assertOk(self.run_cmd(["git", "-C", self.vault, *args]))

    def readme(self, slug, *tasks):
        return self.README % (slug, slug, "\n".join(tasks))

    def history(self):
        self.write("Projects/alpha/README.md",
                   self.readme("alpha", "- [ ] write it [p:: 2]", "- [ ] ship it"))
        self.write("Projects/alpha/research.md", "# r\n")
        self.write("Projects/alpha/memory/note.md", "a note\n")
        self.write("log.md", "\n".join([
            "2026-09-01 10:00 CDT [graduate] Projects/alpha/research.md → Knowledge/topic.md: up",
            "2026-09-02 [work] alpha — no arrow here",
            "2026-09-03 [work] alpha — renamed the thing a -> b",
            "2026-09-04 [work] alpha — nothing -> here",
            "not a dated line -> Knowledge/topic.md",
        ]) + "\n")
        self.commit("start alpha", "2026-09-01T09:00:00-05:00")
        self.write("Knowledge/topic.md", "# topic\n")
        self.write("Projects/alpha/README.md",
                   self.readme("alpha", "- [x] write it [p:: 2]", "- [ ] ship it [p:: 1]"))
        self.commit("promote the finding", "2026-09-02T09:00:00-05:00")
        self.git("mv", "Projects/alpha", "Projects/bravo")
        self.commit("rename alpha to bravo", "2026-09-03T09:00:00-05:00")
        self.write("global-memory/fact.md", "a fact\n")
        self.git("mv", "Projects/bravo/memory/note.md", "Knowledge/note.md")
        self.commit("tidy", "2026-09-04T09:00:00-05:00")
        # uncommitted: a task git has never seen
        self.write("Projects/bravo/README.md",
                   self.readme("bravo", "- [x] write it [p:: 2]", "- [ ] ship it [p:: 1]",
                               "- [ ] fresh idea"))

    def bf(self, *args):
        return self.py(TOOL, "--vault", self.vault, "--id", "bf-session", "backfill", *args)

    def events(self):
        return [json.loads(l) for l in self.merged.read_text().splitlines()]

    def find(self, events, **want):
        return [e for e in events if all(e.get(k) == v for k, v in want.items())]

    def test_known_history_becomes_known_events_and_a_rerun_adds_nothing(self):
        self.history()
        dry = self.bf("--dry-run")
        self.assertOk(dry)
        self.assertIn("dry run: backfill would add", dry.stdout)
        self.assertIn("rename=2", dry.stdout)       # the folder, and the verb-only log line
        self.assertFalse(self.spool.exists(), "--dry-run wrote the spool")
        self.assertFalse(self.merged.exists())

        self.assertOk(self.bf())
        self.assertEqual(sorted(p.name for p in self.spool.iterdir()), ["bf-session.jsonl"],
                         "backfill wrote outside its own session's spool file")
        evs = self.events()
        self.assertTrue(evs)
        self.assertEqual({e["actor"] for e in evs}, {"backfill"})
        for e in evs:
            self.assertRegex(e["note"], r"^backfill conf=\d\.\d src=(git:[0-9a-f]{10}|log:[0-9a-f]{10}|readme) ")

        topic = self.find(evs, kind="promote", item="Knowledge/topic.md", **{"from": None})
        self.assertEqual(len(topic), 1, evs)
        self.assertEqual((topic[0]["level_from"], topic[0]["level_to"]), (3, 4))
        self.assertIn("conf=0.7", topic[0]["note"])

        ren = self.find(evs, kind="rename", item="Projects/bravo")
        self.assertEqual(len(ren), 1, evs)
        self.assertEqual((ren[0]["from"], ren[0]["to"], ren[0]["project"]),
                         ("Projects/alpha", "Projects/bravo", "bravo"))
        self.assertEqual(ren[0]["ts"], "2026-09-03T09:00:00-05:00")

        gm = self.find(evs, kind="promote", item="global-memory/fact.md")
        self.assertEqual((len(gm), gm[0]["level_to"], gm[0]["level_from"]), (1, 5, None))

        note = self.find(evs, kind="promote", item="Knowledge/note.md")
        self.assertEqual(len(note), 1, evs)
        self.assertEqual((note[0]["from"], note[0]["level_from"], note[0]["level_to"]),
                         ("Projects/bravo/memory/note.md", 2, 4))

        E = load_module(TOOL, "gt_events_bf")
        a_readme = "Projects/alpha/README.md"
        write_it, ship_it = E.task_item(a_readme, "write it"), E.task_item(a_readme, "ship it")
        self.assertEqual([e["kind"] for e in self.find(evs, item=write_it)],
                         ["task.open", "task.done"])
        self.assertEqual([e["kind"] for e in self.find(evs, item=ship_it)], ["task.open"],
                         "a priority edit was read as a task change")
        fresh = self.find(evs, item=E.task_item("Projects/bravo/README.md", "fresh idea"))
        self.assertEqual([e["kind"] for e in fresh], ["task.open"])
        self.assertIn("src=readme", fresh[0]["note"])

        logged = self.find(evs, kind="promote", **{"from": "Projects/alpha/research.md"})
        self.assertEqual(len(logged), 1, evs)
        self.assertEqual((logged[0]["to"], logged[0]["ts"], logged[0]["level_from"]),
                         ("Knowledge/topic.md", "2026-09-01T10:00:00-05:00", 3))
        verb = self.find(evs, kind="rename", item="log.md")
        self.assertEqual(len(verb), 1, "the verb-inferred arrow line was not read")
        self.assertIn("conf=0.3", verb[0]["note"])
        self.assertFalse([e for e in evs if "nothing -> here" in e.get("note", "")],
                         "an arrow line with no movement verb became an event")

        before = self.merged.read_bytes()
        again = self.bf()
        self.assertOk(again)
        self.assertIn("nothing new", again.stdout)
        self.assertEqual(self.merged.read_bytes(), before, "a second backfill changed events")
        other = self.py(TOOL, "--vault", self.vault, "--id", "other", "backfill")
        self.assertOk(other)
        self.assertFalse((self.spool / "other.jsonl").exists(),
                         "a backfill from another session duplicated the history")
        self.assertOk(self.py(TOOL, "--vault", self.vault, "validate"))

    def test_since_limits_git_and_log(self):
        self.history()
        p = self.bf("--since", "2026-09-03", "--dry-run", "--sample", "0")
        self.assertOk(p)
        self.assertIn("rename=", p.stdout)
        self.assertOk(self.bf("--since", "2026-09-03"))
        self.assertFalse(self.find(self.events(), item="Knowledge/topic.md"),
                         "--since let an older commit and log line through")

    def test_a_vault_without_git_still_seeds_tasks(self):
        shutil.rmtree(self.vault / ".git")
        self.write("Projects/solo/README.md", self.readme("solo", "- [ ] one", "- [x] two"))
        self.assertOk(self.bf())
        kinds = sorted(e["kind"] for e in self.events())
        self.assertEqual(kinds, ["task.done", "task.open"])


if __name__ == "__main__":
    unittest.main()
