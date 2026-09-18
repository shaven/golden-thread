"""gt_log.py — log.md as a generated merge of per-session spool files.

The behaviours these pin, and why each one matters, are in gt_spool.py's docstring.
The one worth restating: `gt_closeout.py` parses log.md, so the merge must render
lines verbatim. A test that only checked "the line is present somewhere" would pass
while closeout quietly stopped finding it.
"""
import os
import re
import shutil
import subprocess
import unittest
import argparse
import pathlib

from _harness import Sandbox, TOOLS, SCRIPTS, PYTHON, load_module

def _restore_mode(path, mode=0o644):
    """chmod back, ignoring a path the sandbox teardown has already removed."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


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


class SpoolWriteIfChanged(Sandbox):
    """`gt_spool.write_if_changed`: the replace every generated file goes through.

    Two guarantees, both found missing in 0.16.3. (1) The scratch file must be private
    to one writer: it used to be `<target>.gt-tmp`, so two merges -- routine with
    several sessions open, since `gt_log.py add` merges by default -- shared it and one
    rendered the other's half-written bytes into log.md. (2) A caller decides what to
    write from a snapshot read earlier (`hand_written()` runs BEFORE this); a save that
    lands in between used to be overwritten, in the one mechanism that rescues
    hand-written lines. A caller that says what it decided on must be refused instead.
    """

    def setUp(self):
        super().setUp()
        # gt_spool reads no config at import: it only touches the paths it is handed.
        self.S = load_module(TOOLS / "gt_spool.py", "gt_spool_under_test")
        self.dir = self.tmp / "vault"
        self.dir.mkdir()
        self.target = self.dir / "log.md"

    def temps(self):
        return sorted(p.name for p in self.dir.iterdir() if p.name.endswith(".gt-tmp"))

    # -- the behaviour callers already depend on ---------------------------------
    def test_unchanged_bytes_are_not_rewritten(self):
        """Idempotence is an acceptance criterion: an unchanged merge must not dirty git."""
        self.assertIs(self.S.write_if_changed(self.target, "a\nb\n"), True)
        before = self.target.stat().st_mtime_ns
        self.assertIs(self.S.write_if_changed(self.target, "a\nb\n"), False)
        self.assertEqual(self.target.stat().st_mtime_ns, before, "an identical write touched the file")
        self.assertIs(self.S.write_if_changed(self.target, "a\nc\n"), True)
        self.assertEqual(self.target.read_text(), "a\nc\n")

    def test_undecodable_bytes_still_round_trip(self):
        raw = b"caf\xe9 \xff\n"
        self.target.write_bytes(raw)
        body = self.S.read_owner(self.target)
        self.assertIs(self.S.write_if_changed(self.target, body), False)
        self.assertIs(self.S.write_if_changed(self.target, body + "more\n"), True)
        self.assertEqual(self.target.read_bytes(), raw + b"more\n")

    def test_no_temp_file_is_left_behind(self):
        self.S.write_if_changed(self.target, "x\n")
        self.assertEqual(self.temps(), [], "a scratch file survived a successful write")

    def test_a_failed_replace_cleans_up_its_temp_file(self):
        boom = OSError("no")

        def fail(src, dst):
            raise boom

        real = self.S.os.replace
        self.S.os.replace = fail
        try:
            with self.assertRaises(OSError):
                self.S.write_if_changed(self.target, "x\n")
        finally:
            self.S.os.replace = real
        self.assertEqual(self.temps(), [], "a failed write left a .gt-tmp beside the vault file")
        self.assertFalse(self.target.exists())

    def test_the_target_keeps_its_own_permissions(self):
        self.target.write_text("old\n")
        os.chmod(self.target, 0o640)
        self.S.write_if_changed(self.target, "new\n")
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640,
                         "the merge changed log.md's permissions")

    # -- (1) one scratch file per writer -----------------------------------------
    def test_two_writers_in_flight_do_not_share_a_temp_file(self):
        """Deterministic version of the race: a second writer runs inside the first's
        replace. With a shared `<target>.gt-tmp` the second truncates and renames the
        file the first is about to rename, so the first either loses its bytes or
        fails with ENOENT. Distinct names make the two writes simply order."""
        seen = []
        real = self.S.os.replace
        nested = {"done": False}

        def spy(src, dst):
            seen.append(str(src))
            if not nested["done"]:
                nested["done"] = True
                self.S.write_if_changed(self.target, "B" * 5000 + "\n")
            return real(src, dst)

        self.S.os.replace = spy
        try:
            self.S.write_if_changed(self.target, "A" * 5000 + "\n")
        finally:
            self.S.os.replace = real
        self.assertEqual(len(seen), 2, "expected two writes: %r" % seen)
        self.assertNotEqual(seen[0], seen[1],
                            "both writers used the same scratch file: %s" % seen[0])
        self.assertEqual(self.target.read_text(), "A" * 5000 + "\n",
                         "the outer writer's bytes did not survive the inner write")
        self.assertEqual(self.temps(), [])

    def test_concurrent_merges_never_render_a_partial_file(self):
        """Six real processes writing six different payloads at once. Every snapshot an
        observer sees must be ONE writer's complete bytes -- never a mixture, never a
        prefix. This is the 2026-09 corruption in miniature."""
        driver = self.tmp / "writer.py"
        driver.write_text(
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('s', sys.argv[1])\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "m.write_if_changed(sys.argv[2], sys.argv[3] * int(sys.argv[4]) + '\\n')\n",
            encoding="utf-8")
        n = 200000
        chars = "ABCDEF"
        payloads = {c * n + "\n" for c in chars}
        procs = [subprocess.Popen(
            [PYTHON, str(driver), str(TOOLS / "gt_spool.py"), str(self.target), c, str(n)],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for c in chars]
        seen = set()
        while any(p.poll() is None for p in procs):
            try:
                seen.add(self.target.read_text(encoding="utf-8", errors="surrogateescape"))
            except OSError:
                pass
        for p, c in zip(procs, chars):
            out, err = p.communicate()
            self.assertEqual(p.returncode, 0, "writer %s failed:\n%s\n%s" % (c, out, err))
        seen.add(self.target.read_text(encoding="utf-8", errors="surrogateescape"))
        bad = [s for s in seen if s not in payloads]
        self.assertFalse(bad, "a reader saw %d corrupted state(s); first is %d bytes: %r..."
                         % (len(bad), len(bad[0]) if bad else 0, bad[0][:60] if bad else ""))
        self.assertEqual(self.temps(), [], "concurrent writers left scratch files behind")

    # -- (2) the read-decide-write window ----------------------------------------
    def test_a_target_changed_underneath_the_writer_is_refused(self):
        self.target.write_text("one\n")
        decided_on = self.S.content_digest(self.S.read_owner(self.target))
        self.target.write_text("one\ntyped by hand\n")            # the human save
        r = self.S.write_if_changed(self.target, "one\nrendered\n", expect=decided_on)
        self.assertEqual(r, self.S.STALE, "a stale write was not refused: %r" % (r,))
        self.assertEqual(self.target.read_text(), "one\ntyped by hand\n",
                         "the hand-written save was clobbered")
        self.assertEqual(self.temps(), [])
        # Re-deciding on what is there now goes through.
        again = self.S.content_digest(self.S.read_owner(self.target))
        self.assertIs(self.S.write_if_changed(self.target, "one\nrendered\n", expect=again), True)
        self.assertEqual(self.target.read_text(), "one\nrendered\n")

    def test_expect_accepts_the_content_itself(self):
        self.target.write_text("one\n")
        self.assertIs(self.S.write_if_changed(self.target, "two\n", expect="one\n"), True)
        self.assertEqual(self.S.write_if_changed(self.target, "three\n", expect="one\n"),
                         self.S.STALE)
        self.assertEqual(self.target.read_text(), "two\n")

    def test_expect_on_a_file_that_should_not_exist(self):
        """Absent digests as empty, so a file that appeared underneath the caller is stale."""
        self.assertIs(self.S.write_if_changed(self.target, "x\n", expect=""), True)
        self.target.write_text("someone else\n")
        self.assertEqual(self.S.write_if_changed(self.target, "y\n", expect=""), self.S.STALE)
        self.assertEqual(self.target.read_text(), "someone else\n")

    def test_expect_is_optional_and_stale_is_distinct_from_unchanged(self):
        """The callers this must not break pass two arguments and read True/False."""
        self.target.write_text("same\n")
        self.assertIs(self.S.write_if_changed(self.target, "same\n"), False)
        self.assertIs(self.S.write_if_changed(self.target, "same\n",
                                              expect=self.S.content_digest("same\n")), False)
        self.assertTrue(self.S.STALE, "STALE must be truthy so it is never read as 'no change'")
        self.assertNotIn(self.S.STALE, (True, False))


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


class MergePassesThePrecondition(Sandbox):
    """The precondition must be APPLIED, not merely available.

    gt 0.16.4 gave gt_spool.write_if_changed an `expect` argument. A capability nothing passes
    is the failure this project exists to close, one layer down -- so these test the CALLER:
    that gt_log's merge supplies the digest, and that a file changing underneath the decision
    is refused instead of clobbered.
    """

    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.log = self.vault / "log.md"

    def _merge_mod(self):
        return load_module(TOOLS / "gt_log.py", "gt_log_under_test")

    def _patch(self, obj, name, value):
        """Replace obj.name for this test only.

        gt_log imports gt_spool as S, and Python caches that module -- so `mod.S` is the SAME
        object in every test here even though load_module re-execs gt_log. A patch left in
        place leaked into the next test and made its baseline merge refuse, which looked like
        a defect in the code under test rather than in the test. Restore, always.
        """
        original = getattr(obj, name)
        self.addCleanup(setattr, obj, name, original)
        setattr(obj, name, value)
        return original

    def test_merge_supplies_expect_to_write_if_changed(self):
        mod = self._merge_mod()
        seen = {}

        def spy(target, text, expect=None):
            seen["expect"] = expect
            return real(target, text, expect=expect)

        real = self._patch(mod.S, "write_if_changed", spy)

        rc = mod.cmd_merge(argparse.Namespace(vault=str(self.vault), quiet=True, dry_run=False))
        self.assertEqual(rc, 0)
        self.assertIn("expect", seen, "merge never called write_if_changed")
        self.assertIsNotNone(seen["expect"],
                             "merge called write_if_changed WITHOUT the precondition -- the "
                             "mechanism exists and nothing uses it")

    def test_one_save_during_the_decision_is_retried_and_the_line_survives(self):
        """The common case: a human appends a line, then stops typing.

        Pass 1 decides against the pre-save bytes and is refused as stale; pass 2 re-reads,
        finds the line, and rescues it into the spool before rendering. Success here is the
        RIGHT answer -- the precondition exists to stop a decision being applied to bytes that
        have moved, not to refuse whenever anything moves.
        """
        mod = self._merge_mod()
        self.assertEqual(mod.cmd_merge(argparse.Namespace(
            vault=str(self.vault), quiet=True, dry_run=False)), 0, "baseline merge")

        calls = {"n": 0}

        def append_once(target, produced):
            calls["n"] += 1
            if calls["n"] == 1:                       # only the first pass races
                f = pathlib.Path(target)
                f.write_text(f.read_text(encoding="utf-8") + "- a human typed this\n",
                             encoding="utf-8")
            return real_hand(target, produced)

        real_hand = self._patch(mod.S, "hand_written", append_once)
        rc = mod.cmd_merge(argparse.Namespace(vault=str(self.vault), quiet=True, dry_run=False))
        self.assertEqual(rc, 0, "a single race should be retried, not refused")
        self.assertGreaterEqual(calls["n"], 2, "the stale write must have forced a retry")
        spool = self.vault / "Projects/golden-thread/spool/log"
        self.assertTrue(
            any("a human typed this" in f.read_text(encoding="utf-8", errors="replace")
                for f in spool.rglob("*") if f.is_file()),
            "the line typed during the decision was not rescued into the spool")
        self.assertIn("a human typed this", self.log.read_text(encoding="utf-8"),
                      "the rescued line did not come back when the log was re-rendered")

    def test_a_file_that_keeps_changing_is_refused_rather_than_clobbered(self):
        """The bounded half: retrying forever would hang, reporting success would lose work."""
        mod = self._merge_mod()
        self.assertEqual(mod.cmd_merge(argparse.Namespace(
            vault=str(self.vault), quiet=True, dry_run=False)), 0, "baseline merge")

        calls = {"n": 0}

        def never_settles(target, produced):
            calls["n"] += 1
            # DIFFERENT bytes every pass, so the precondition never holds.
            pathlib.Path(target).write_text("edit %d\n" % calls["n"], encoding="utf-8")
            return real_hand(target, produced)

        real_hand = self._patch(mod.S, "hand_written", never_settles)
        rc = mod.cmd_merge(argparse.Namespace(vault=str(self.vault), quiet=True, dry_run=False))
        self.assertEqual(rc, 3, "a file changing under every attempt must refuse")
        self.assertEqual(calls["n"], 3, "the retry must be bounded at three attempts")
        self.assertIn("edit 3", self.log.read_text(encoding="utf-8"),
                      "the last write standing must be the human's, not the merge's")


class RescueHappensExactlyOnce(Sandbox):
    """A retried merge must rescue a hand-written line ONCE, however many attempts it takes.

    0.16.4 put `capture_hand_written()` inside the three-attempt retry without making it
    idempotent. It appends the rescued lines to the SPOOL; the re-render then places them at
    their sorted position -- but the target still holds them where the human typed them, so
    the next attempt's diff reports them missing again and rescues them a second time, into
    the same second-resolution filename opened "ab". Measured: one stale attempt duplicated
    the line, two triplicated it, exit 0 either way.

    The spool is the audit trail, so re-merging cannot undo it: the vault permanently records
    one event two or three times. These tests count what is IN THE SPOOL, not what log.md
    happens to render, because that is where the damage is permanent.
    """

    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.log = self.vault / "log.md"
        self.spool = self.vault / "Projects/golden-thread/spool/log"

    def _patch(self, obj, name, value):
        original = getattr(obj, name)
        self.addCleanup(setattr, obj, name, original)
        setattr(obj, name, value)
        return original

    def _merge(self, mod):
        return mod.cmd_merge(argparse.Namespace(vault=str(self.vault), quiet=True,
                                                dry_run=False))

    def spooled_count(self, needle):
        return sum(f.read_text(encoding="utf-8", errors="replace").count(needle)
                   for f in self.spool.rglob("*") if f.is_file())

    HAND = "2026-01-02 09:00 CDT [note] typed straight into log.md by the owner"
    LATER = "2026-06-01 10:00 CDT [work] demo — spooled, and dated after the hand edit"

    def type_by_hand(self, mod):
        """Spool a LATER-dated line, then append an EARLIER hand edit at the end of log.md.

        The dates matter, and a version of this test without them proves nothing: the
        re-report only happens when the render MOVES the rescued line, so that the target
        (line at the end) and the render (line at its sorted position) still differ on the
        next attempt. A hand edit whose sorted position is where it was typed produces
        identical files, no diff, and no second capture even from the defective code.
        """
        mod.S.append(self.vault, "log", self.LATER, sid="alpha")
        self.assertEqual(self._merge(mod), 0, "baseline merge")
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(self.HAND + "\n")
        body = self.log.read_text(encoding="utf-8").splitlines()
        self.assertGreater(body.index(self.HAND), body.index(self.LATER),
                           "fixture is wrong: the hand edit must start out BELOW a line "
                           "it sorts above, or the render never moves it")

    def _run_with_stale_attempts(self, stale):
        """Force the first `stale` writes to lose the precondition, then merge.

        Staleness is injected at `write_if_changed` rather than by racing the file, so the
        number of wasted attempts is exact and the hand-written content never varies: the
        human typed one line and stopped. Every extra attempt is therefore a re-decision
        about the SAME line, which is the whole question.
        """
        mod = load_module(TOOLS / "gt_log.py", "gt_log_once_%d" % stale)
        self.type_by_hand(mod)
        calls = {"n": 0}

        def flaky(target, text, expect=None):
            calls["n"] += 1
            if calls["n"] <= stale:
                return mod.S.STALE          # someone moved the file; nothing written
            return real(target, text, expect=expect)

        real = self._patch(mod.S, "write_if_changed", flaky)
        rc = self._merge(mod)
        return rc, calls["n"]

    def test_one_stale_attempt_rescues_the_line_once(self):
        rc, attempts = self._run_with_stale_attempts(1)
        self.assertEqual(rc, 0, "one stale attempt should still finish")
        self.assertEqual(attempts, 2)
        self.assertEqual(self.spooled_count(self.HAND), 1,
                         "the rescued line is in the spool %d times; the retry captured it "
                         "again and the audit trail now records the event twice"
                         % self.spooled_count(self.HAND))
        self.assertEqual(self.log.read_text(encoding="utf-8").count(self.HAND), 1)

    def test_two_stale_attempts_rescue_the_line_once(self):
        rc, attempts = self._run_with_stale_attempts(2)
        self.assertEqual(rc, 0, "two stale attempts should still finish on the third")
        self.assertEqual(attempts, 3)
        self.assertEqual(self.spooled_count(self.HAND), 1,
                         "two stale attempts triplicated the rescued line: %d copies"
                         % self.spooled_count(self.HAND))
        self.assertEqual(self.log.read_text(encoding="utf-8").count(self.HAND), 1)

    def test_three_stale_attempts_refuse_but_still_rescue_only_once(self):
        """The merge gives up (exit 3) -- and must not have littered the spool on the way.

        Nothing is written to log.md here, so the ONLY lasting effect of the run is whatever
        reached the spool. One copy of the line is right (it was rescued); two or three are
        the corruption."""
        rc, attempts = self._run_with_stale_attempts(3)
        self.assertEqual(rc, 3, "a file that never settles must refuse")
        self.assertEqual(attempts, 3, "the retry must be bounded at three attempts")
        self.assertEqual(self.spooled_count(self.HAND), 1,
                         "an abandoned merge left %d copies of the rescued line in the spool"
                         % self.spooled_count(self.HAND))

    def test_a_line_typed_twice_is_still_rescued_twice(self):
        """The tally is a MULTISET, not a set: dropping a genuine repeat would be data loss
        of exactly the kind the rescue exists to prevent."""
        mod = load_module(TOOLS / "gt_log.py", "gt_log_once_twice")
        mod.S.append(self.vault, "log", self.LATER, sid="alpha")
        self.assertEqual(self._merge(mod), 0, "baseline merge")
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(self.HAND + "\n" + self.HAND + "\n")
        self.assertEqual(self._merge(mod), 0)
        self.assertEqual(self.spooled_count(self.HAND), 2,
                         "a line the owner really did type twice was rescued only once")


class AnUnreadableTargetIsRefused(Sandbox):
    """A file that EXISTS but cannot be read must never be written over.

    0.16.4: `current_digest()` returned the digest of b"" on any OSError and
    `write_if_changed` set `current = None` on any OSError -- both of which are also what
    an ABSENT file gives. So `before == expect` held, the precondition reported "nobody
    moved underneath you" about a file nobody could see, `hand_written()` returned []
    (its `is_generated` swallows the same OSError), and the render replaced the file.
    `chmod 000 log.md; gt_log.py merge` printed "log.md updated" and exited 0 with the
    hand-written line gone, no warning and no rescue file.

    0.16.4 also made it permanent: mode preservation copies mode 000 onto the replacement,
    so the file stays unreadable and every later merge clobbers it again. safe_write.py's
    rule -- refuse rather than replace a file you cannot read -- has to hold here too.
    """

    def setUp(self):
        super().setUp()
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads mode-000 files, so the defect cannot be reproduced")
        self.vault = self.make_vault()
        self.log = self.vault / "log.md"

    def tool(self, *args):
        return self.py(TOOLS / "gt_log.py", "--vault", self.vault, *args)

    def make_unreadable(self):
        """log.md with an owner's line in it, then mode 000. Restored for tearDown."""
        self.assertOk(self.tool("merge"))
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write("2026-01-02 09:00 CDT [note] the owner's only copy of this\n")
        raw = self.log.read_bytes()
        os.chmod(self.log, 0o000)
        # Restored so tearDown can remove the tree; tolerant because tearDown runs FIRST
        # and has usually already deleted it.
        self.addCleanup(_restore_mode, self.log)
        if os.access(self.log, os.R_OK):                      # pragma: no cover
            self.skipTest("this filesystem ignores mode 000")
        return raw

    def test_merge_refuses_and_the_file_survives(self):
        raw = self.make_unreadable()
        p = self.tool("merge")
        self.assertEqual(p.returncode, 4,
                         "an unreadable log.md was not refused: exit %d\n%s\n%s"
                         % (p.returncode, p.stdout, p.stderr))
        self.assertIn("REFUSED", p.stderr)
        self.assertNotIn("updated", p.stdout)
        os.chmod(self.log, 0o644)
        self.assertEqual(self.log.read_bytes(), raw,
                         "the merge replaced a file it could not read")

    def test_the_unreadable_file_keeps_its_mode(self):
        """The 0.16.4 compounding: the replacement inherited mode 000, so the file stayed
        unreadable and idempotence -- an acceptance criterion -- was broken for good."""
        self.make_unreadable()
        self.tool("merge")
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o000,
                         "log.md's mode changed, so something replaced it")

    def test_add_spools_the_entry_and_reports_the_refusal(self):
        """`add` must still save the line -- the spool is the session's own file and is
        perfectly writable -- while saying, in its exit code, that log.md is not current."""
        self.make_unreadable()
        p = self.tool("--id", "alpha", "add", "2026-01-03 10:00 CDT [work] x — y")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("REFUSED", p.stderr)
        spool = self.vault / "Projects/golden-thread/spool/log/alpha.md"
        self.assertIn("[work] x", spool.read_text(encoding="utf-8"),
                      "the entry was lost as well")

    def test_dry_run_refuses_too(self):
        self.make_unreadable()
        p = self.tool("merge", "--dry-run")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)


class SpoolUnreadable(Sandbox):
    """The gt_spool half of the same defect, at the two functions that swallowed it."""

    def setUp(self):
        super().setUp()
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads mode-000 files")
        self.S = load_module(TOOLS / "gt_spool.py", "gt_spool_unreadable")
        self.target = self.tmp / "log.md"
        self.target.write_text("the owner's bytes\n", encoding="utf-8")
        os.chmod(self.target, 0o000)
        self.addCleanup(_restore_mode, self.target)
        if os.access(self.target, os.R_OK):                   # pragma: no cover
            self.skipTest("this filesystem ignores mode 000")

    def test_cannot_read_is_not_the_same_as_absent(self):
        self.assertIsNone(self.S.read_bytes_or_none(self.tmp / "nothing-here.md"),
                          "an absent file must read as absent")
        with self.assertRaises(self.S.Unreadable):
            self.S.read_bytes_or_none(self.target)

    def test_current_digest_refuses_rather_than_digesting_empty(self):
        with self.assertRaises(self.S.Unreadable):
            self.S.current_digest(self.target)

    def test_write_if_changed_refuses_and_writes_nothing(self):
        with self.assertRaises(self.S.Unreadable):
            self.S.write_if_changed(self.target, "rendered\n")
        with self.assertRaises(self.S.Unreadable):
            self.S.write_if_changed(self.target, "rendered\n",
                                    expect=self.S.content_digest(b""))
        os.chmod(self.target, 0o644)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "the owner's bytes\n",
                         "a file that could not be read was replaced anyway")
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.endswith(".gt-tmp")],
                         [], "a refused write left a scratch file behind")


class SafeWriteRefusalIsNotSwallowed(Sandbox):
    """`gt_spool._safe_write` must not retry, unguarded, a write safe_write declined.

    Through 0.16.4 the whole safe_write branch sat under `except Exception: pass` and fell
    through to `open(target, "a")` -- so safe_write's central promise (refuse rather than
    replace a file you cannot read) was caught, discarded, and the write retried without
    it. The same branch dropped safe_write's return value, so a sidecar or ledger-dir
    strategy -- which leaves the bytes NEXT TO the spool with a `mv` queued in the ledger --
    reported a spooled entry that no merge would ever render.

    Both are exercised by giving gt_spool a safe_write.py that behaves that way, which is
    the module it loads by path from its own directory.
    """

    def _spool_beside(self, safe_write_body, name):
        d = self.tmp / name
        d.mkdir()
        shutil.copy(TOOLS / "gt_spool.py", d / "gt_spool.py")
        (d / "safe_write.py").write_text(safe_write_body, encoding="utf-8")
        return load_module(d / "gt_spool.py", "gt_spool_" + name)

    REFUSES = ('def write(target, data, mode="w"):\n'
               '    raise OSError("safe_write: cannot read %s in order to append to it; '
               'refusing rather than replacing it with only the new bytes" % target)\n')

    SIDECAR = ('def write(target, data, mode="w"):\n'
               '    side = target + ".pending-20260918"\n'
               '    with open(side, "a") as fh:\n'
               '        fh.write(data)\n'
               '    return side, "sidecar"\n')

    def test_a_refusal_propagates_and_nothing_is_appended(self):
        S = self._spool_beside(self.REFUSES, "refuses")
        target = self.tmp / "spool" / "alpha.md"
        with self.assertRaises(OSError, msg="safe_write's refusal was swallowed and the "
                                            "write retried with a plain append"):
            S._safe_write(target, "2026-01-01 [work] x — y\n")
        self.assertFalse(target.exists(),
                         "the refused bytes were written anyway: %r"
                         % (target.read_text(encoding="utf-8") if target.exists() else ""))

    def test_bytes_that_did_not_reach_the_target_are_an_error(self):
        S = self._spool_beside(self.SIDECAR, "sidecar")
        target = self.tmp / "spool2" / "alpha.md"
        target.parent.mkdir()
        target.write_text("", encoding="utf-8")            # exists -> append mode
        with self.assertRaises(OSError) as ctx:
            S._safe_write(target, "2026-01-01 [work] x — y\n")
        self.assertIn("sidecar", str(ctx.exception),
                      "the caller was not told which strategy left the bytes elsewhere")
        self.assertEqual(target.read_text(encoding="utf-8"), "",
                         "the entry is not in the spool, yet the call reported success")

    def test_the_fallback_still_appends_when_there_is_no_safe_write(self):
        """A vault whose tools predate safe_write.py must keep working -- that fallback is
        the one thing the swallowed exception was there for, and it stays."""
        d = self.tmp / "bare"
        d.mkdir()
        shutil.copy(TOOLS / "gt_spool.py", d / "gt_spool.py")
        S = load_module(d / "gt_spool.py", "gt_spool_bare")
        target = d / "spool" / "alpha.md"
        S._safe_write(target, "one\n")
        S._safe_write(target, "two\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "one\ntwo\n")


if __name__ == "__main__":
    unittest.main()
