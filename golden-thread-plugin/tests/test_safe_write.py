"""safe_write.py: a write that resists a hostile destination and never fails silently.

Every call runs the VAULT's seeded copy in a subprocess, with HOME and TMPDIR pointed
into the sandbox: the ledger path is fixed from HOME at import time, and its last
resort is tempfile.gettempdir(), so both must be sandboxed before the import.
"""
import json
import os
import stat
import unittest
from pathlib import Path

from _harness import Sandbox, PYTHON


class SafeWriteTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.tools = self.vault / "Projects" / "golden-thread" / "tools"
        self.work = self.tmp / "work"          # outside any git repo: no attribution noise
        self.work.mkdir()
        tmpdir = self.tmp / "tmpdir"
        tmpdir.mkdir()
        self.env["TMPDIR"] = str(tmpdir)
        self._chmods = []

    def tearDown(self):
        for p, mode in reversed(self._chmods):
            try:
                os.chmod(p, mode)
            except OSError:
                pass
        super().tearDown()

    # -- helpers -------------------------------------------------------------
    def chmod(self, p, mode):
        self._chmods.append((p, stat.S_IMODE(os.stat(p).st_mode)))
        os.chmod(p, mode)

    def sw(self, body):
        """Run `body` with safe_write imported as `sw`; it prints one JSON value."""
        code = ("import json, os, sys\n"
                "sys.path.insert(0, %r)\n"
                "import safe_write as sw\n" % str(self.tools)) + body
        return self.run_cmd([PYTHON, "-c", code])

    def sw_json(self, body):
        proc = self.sw(body)
        self.assertOk(proc, "driver failed")
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def write(self, target, data, mode="w"):
        return self.sw_json("print(json.dumps(sw.write(%r, %r, %r)))" % (str(target), data, mode))

    def ledger(self):
        p = self.home / ".claude" / "safe_write_ledger.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]

    # -- plain writes --------------------------------------------------------
    def test_new_file_is_written_atomically_and_parents_created(self):
        target = self.work / "a" / "b" / "c.txt"
        path, strategy = self.write(target, "hello\n")
        self.assertEqual(strategy, "atomic")
        self.assertEqual(Path(path), target.resolve())
        self.assertEqual(target.read_text(), "hello\n")
        leftovers = [p.name for p in target.parent.iterdir() if p.name.startswith(".sw-")]
        self.assertEqual(leftovers, [], "atomic replace left its temp file behind")
        self.assertEqual(self.ledger(), [], "a clean write must not touch the ledger")

    def test_existing_permission_bits_survive_atomic_replace(self):
        for mode in (0o644, 0o640, 0o666):
            with self.subTest(mode=oct(mode)):
                target = self.work / ("perm-%o.txt" % mode)
                target.write_text("old\n")
                os.chmod(target, mode)
                path, strategy = self.write(target, "new\n")
                self.assertEqual(strategy, "atomic")
                self.assertEqual(target.read_text(), "new\n")
                self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), mode,
                                 "atomic replace changed the file's permission bits "
                                 "(mkstemp creates 0600)")

    def test_bytes_data_with_text_mode_is_written_as_bytes(self):
        target = self.work / "bin.dat"
        self.sw_json("print(json.dumps(sw.write(%r, b'\\x00\\xff\\r\\n', 'w')))" % str(target))
        self.assertEqual(target.read_bytes(), b"\x00\xff\r\n")

    def test_symlink_is_written_through_not_replaced(self):
        real = self.work / "real.txt"
        real.write_text("old\n")
        link = self.work / "link.txt"
        link.symlink_to(real)
        path, _ = self.write(link, "new\n")
        self.assertTrue(link.is_symlink(), "the write replaced the symlink with a regular file")
        self.assertEqual(real.read_text(), "new\n")
        self.assertEqual(Path(path), real.resolve())

    def test_ambiguous_mode_raises_and_writes_nothing(self):
        target = self.work / "ambig.txt"
        target.write_text("keep me\n")
        for mode in ("wa", "aw", "ra", "ax"):
            with self.subTest(mode=mode):
                proc = self.sw("sw.write(%r, 'x', %r)" % (str(target), mode))
                self.assertNotEqual(proc.returncode, 0, "mode %r was accepted" % mode)
                self.assertIn("ValueError", proc.stderr)
                self.assertIn("ambiguous mode", proc.stderr)
        self.assertEqual(target.read_text(), "keep me\n")

    # -- appends -------------------------------------------------------------
    def test_append_appends_in_place_and_never_replaces(self):
        target = self.work / "log.md"
        target.write_text("one\n")
        ino = os.stat(target).st_ino
        path, strategy = self.write(target, "two\n", "a")
        self.assertEqual(strategy, "direct")
        self.assertEqual(target.read_text(), "one\ntwo\n")
        self.assertEqual(os.stat(target).st_ino, ino, "an append replaced the file (new inode)")

    def test_append_to_missing_file_creates_it(self):
        target = self.work / "new.md"
        self.write(target, "first\n", "a")
        self.assertEqual(target.read_text(), "first\n")

    def test_append_in_place_preserves_crlf_and_latin1_bytes(self):
        target = self.work / "mixed.txt"
        old = b"line1\r\ncaf\xe9\r\n"
        target.write_bytes(old)
        self.write(target, "new\n", "a")
        self.assertEqual(target.read_bytes(), old + b"new\n")

    def test_append_fallback_preserves_old_bytes_and_mode(self):
        # A read-only file in a writable directory: the in-place append fails, so the
        # append must be resolved into old bytes + new bytes before the atomic replace.
        target = self.work / "ro.txt"
        old = b"line1\r\ncaf\xe9\r\n"
        target.write_bytes(old)
        self.chmod(target, 0o444)
        path, strategy = self.write(target, "new\n", "a")
        self.assertEqual(strategy, "atomic")
        self.assertEqual(target.read_bytes(), old + b"new\n",
                         "the fallback append re-encoded or dropped existing bytes")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o444)

    def test_append_refuses_when_target_cannot_be_read(self):
        sub = self.work / "locked"
        sub.mkdir()
        target = sub / "precious.md"
        target.write_text("do not lose this\n")
        self.chmod(sub, 0o000)
        proc = self.sw("print(json.dumps(sw.write(%r, 'new line\\n', 'a')))" % str(target))
        os.chmod(sub, 0o755)
        self.assertNotEqual(proc.returncode, 0,
                            "append to an unreadable target returned instead of refusing:\n"
                            + proc.stdout)
        self.assertIn("refusing", proc.stderr)
        self.assertEqual(target.read_text(), "do not lose this\n",
                         "the target was truncated or replaced")
        self.assertEqual(sorted(p.name for p in sub.iterdir()), ["precious.md"])
        self.assertEqual(self.ledger(), [],
                         "a refused append left a ledger `mv` that replay would use to "
                         "overwrite the target with only the new bytes")

    # -- fallbacks and the ledger --------------------------------------------
    def test_writable_file_in_readonly_directory_is_written_direct(self):
        d = self.work / "rodir"
        d.mkdir()
        target = d / "f.txt"
        target.write_text("old\n")
        self.chmod(d, 0o555)
        path, strategy = self.write(target, "new\n")
        self.assertEqual(strategy, "direct")
        self.assertEqual(target.read_text(), "new\n")
        self.assertEqual(self.ledger(), [])

    def test_unwritable_directory_lands_in_ledger_dir_and_replays(self):
        d = self.work / "rodir"
        d.mkdir()
        target = d / "out.json"
        self.chmod(d, 0o555)
        path, strategy = self.write(target, '{"ok": 1}\n')
        self.assertEqual(strategy, "ledger-dir")
        self.assertTrue(Path(path).is_file())
        self.assertTrue(str(path).startswith(str(self.home / ".claude")),
                        "last-resort write escaped the sandbox HOME: %s" % path)
        self.assertFalse(target.exists())
        led = self.ledger()
        self.assertEqual(len(led), 1)
        self.assertEqual(led[0]["target"], str(target.resolve()))
        self.assertFalse(led[0]["done"])
        self.assertIn("mv ", led[0]["action"])

        status = self.py(self.tools / "safe_write.py", "status")
        self.assertOk(status)
        self.assertIn("outstanding writes: 1", status.stdout)

        # Still blocked: replay leaves it outstanding.
        blocked = self.sw_json("print(json.dumps(sw.replay()))")
        self.assertEqual(len(blocked), 1)
        self.assertIn("still-blocked", blocked[0][1])

        os.chmod(d, 0o755)
        moved = self.sw_json("print(json.dumps(sw.replay()))")
        self.assertEqual(moved, [[str(target.resolve()), "moved"]])
        self.assertEqual(target.read_text(), '{"ok": 1}\n')
        self.assertEqual(self.sw_json("print(json.dumps(sw.outstanding()))"), [],
                         "a replayed entry is still reported outstanding")

    def test_directory_in_the_way_goes_to_sidecar_and_is_never_deleted(self):
        target = self.work / "clash"
        target.mkdir()
        (target / "inside.txt").write_text("keep\n")
        path, strategy = self.write(target, "payload\n")
        self.assertEqual(strategy, "sidecar")
        self.assertTrue(Path(path).name.startswith("clash.pending-"))
        self.assertEqual(Path(path).read_text(), "payload\n")
        out = self.sw_json("print(json.dumps(sw.outstanding()))")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["wrote_to"], path)
        res = self.sw_json("print(json.dumps(sw.replay()))")
        self.assertIn("still-blocked", res[0][1])
        self.assertTrue(target.is_dir(), "replay deleted the target to make room")
        self.assertEqual((target / "inside.txt").read_text(), "keep\n")


if __name__ == "__main__":
    unittest.main()
