"""gt_errors (0.20.1, usability finding B4): a vault tool refused by the OS prints ONE line --
what was refused, why, and the exact next step -- never a traceback, and never Full Disk Access
advice for a gt sandbox mode refusal.

The 0.20 usability run drove the vault tools from Claude's shell under gt sandbox mode: gt_tasks,
gt_lint, gt_promote_detect, gt_adr allocate, vault_init create-project, gt_closeout, gt_broker
drain and gt_log died in tracebacks; gt_log advised Full Disk Access and left a pending file
whose replay hint named no path; gt_write_queue crashed in find_vault before its inbox fallback
when the sandbox refused a stat of the vault.

Two ways to make the OS refuse, so the suite proves it on every POSIX runner and for real where
it can:
  * a read-only vault (chmod) -- EACCES, the same "refused" path, on macOS and Linux;
  * macOS Seatbelt (sandbox-exec) with gt's own deny lists -- EPERM, the refusal Claude Code's
    sandbox gives, including a denied stat of the vault.
"Inside Claude's sandbox" is CLAUDECODE=1 plus sandbox_mode on in vault-config.json; without
CLAUDECODE the same refusal is worded for the interpreter (provenance) as before.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path

from _harness import Sandbox, SCRIPTS, TEMPLATES, PYTHON, IS_WINDOWS, load_module

TOOLS = TEMPLATES / "tools"
WHY = "sandbox mode: the vault is written only through the queue / gt-vault MCP"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


class Copies(unittest.TestCase):
    def test_the_two_copies_are_byte_identical(self):
        """A vault tool imports the copy beside it, the hooks-dir scripts theirs: one text."""
        self.assertEqual((SCRIPTS / "gt_errors.py").read_bytes(),
                         (TOOLS / "gt_errors.py").read_bytes(),
                         "scripts/gt_errors.py and templates/tools/gt_errors.py differ")

    def test_it_is_installed_into_the_hooks_dir(self):
        comp = load_module(SCRIPTS / "gt_components.py", "gt_components_errors")
        self.assertIn("gt_errors.py", comp.HOOK_DIR_SCRIPTS)


class Wording(Sandbox):
    """In-process: the line itself."""

    def setUp(self):
        super().setUp()
        self._saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "CLAUDECODE",
                                                      "SANDBOX_RUNTIME")}
        os.environ["HOME"] = str(self.home)
        if IS_WINDOWS:
            os.environ["USERPROFILE"] = str(self.home)
        os.environ.pop("SANDBOX_RUNTIME", None)
        self.ge = load_module(SCRIPTS / "gt_errors.py", "gt_errors_under_test")

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def eperm(self, path):
        import errno
        return PermissionError(errno.EPERM, "Operation not permitted", str(path))

    @unittest.skipIf(IS_WINDOWS, "native Windows has no Claude Code sandbox")
    def test_sandbox_needs_the_setting_and_claudes_process(self):
        os.environ.pop("CLAUDECODE", None)
        self.config(vault_path=str(self.tmp), sandbox_mode="on")
        self.assertFalse(self.ge.in_sandbox(), "a terminal run is not Claude's sandbox")
        os.environ["CLAUDECODE"] = "1"
        self.assertTrue(self.ge.in_sandbox())
        self.config(vault_path=str(self.tmp), sandbox_mode="off")
        self.assertFalse(self.ge.in_sandbox(), "sandbox mode off: an EPERM is not the sandbox's")

    @unittest.skipIf(IS_WINDOWS, "native Windows has no Claude Code sandbox")
    def test_the_sandbox_line_is_one_line_with_why_and_the_exact_next_step(self):
        os.environ["CLAUDECODE"] = "1"
        self.config(vault_path=str(self.tmp), sandbox_mode="on", sandbox_vault_reads="allow")
        line = self.ge.refusal_line(self.tmp / "TASKS.md.gt-tmp-123", self.eperm("x"),
                                    tool="gt_tasks")
        self.assertNotIn("\n", line)
        self.assertTrue(line.startswith("gt_tasks: refused: %s " % (self.tmp / "TASKS.md")), line)
        self.assertIn(WHY, line)
        self.assertIn("Next: run this from a terminal: ", line)
        self.assertNotIn("Full Disk Access", line)
        self.assertNotIn("read only through", line, "reads are allowed in this config")
        self.config(vault_path=str(self.tmp), sandbox_mode="on")        # reads deny (default)
        line = self.ge.refusal_line("/v/x", self.eperm("x"), tool="gt_broker",
                                    mcp="vault_queue_drain")
        self.assertIn("read only through the gt-vault MCP", line)
        self.assertIn("Next: use the gt-vault MCP tool vault_queue_drain, or run this from a "
                      "terminal: ", line)

    def test_outside_the_sandbox_the_interpreter_wording_stands(self):
        os.environ.pop("CLAUDECODE", None)
        self.config(vault_path=str(self.tmp), sandbox_mode="on")
        line = self.ge.refusal_line("/v/log.md", self.eperm("x"))
        self.assertIn("gt: cannot write /v/log.md", line)
        self.assertNotIn(WHY, line)

    def test_the_terminal_command_has_real_paths(self):
        cmd = self.ge.terminal_command(["tools/gt_tasks.py", "--vault", "v", "--x=1"],
                                       python="/usr/bin/python3")
        self.assertIn(os.path.abspath("tools/gt_tasks.py"), cmd)
        self.assertIn(os.path.abspath("v"), cmd)
        self.assertTrue(cmd.startswith("/usr/bin/python3 "), cmd)

    def test_run_turns_a_refusal_into_one_line_and_exit_5(self):
        import io
        err = io.StringIO()
        saved, sys.stderr = sys.stderr, err
        try:
            rc = self.ge.run(lambda: (_ for _ in ()).throw(self.eperm("/v/a.md")), "gt_x")
        finally:
            sys.stderr = saved
        self.assertEqual(rc, 5)
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1, err.getvalue())
        with self.assertRaises(FileNotFoundError):        # not a refusal: unchanged
            self.ge.run(lambda: open(str(self.tmp / "nope")), "gt_x")


class WriteQueueFindsTheInbox(Sandbox):
    """B4 (unverified in the run, proven here): under sandbox_vault_reads deny the sandbox
    refuses even a stat of the vault, and gt_write_queue died in find_vault -- before the inbox
    fallback that exists for exactly this."""

    def test_a_refused_stat_of_the_vault_still_reaches_the_inbox(self):
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = str(self.home)
        if IS_WINDOWS:
            os.environ["USERPROFILE"] = str(self.home)
        try:
            vault = self.tmp / "vault"
            vault.mkdir()
            self.config(vault_path=str(vault), sandbox_mode="on")
            wq = load_module(SCRIPTS / "gt_write_queue.py", "gt_write_queue_inbox")
            real_is_dir, real_is_file = Path.is_dir, Path.is_file

            def denied(self_):
                if str(self_).startswith(str(vault)):
                    raise PermissionError(1, "Operation not permitted", str(self_))
                return real_is_dir(self_)

            def denied_file(self_):
                if str(self_).startswith(str(vault)):
                    raise PermissionError(1, "Operation not permitted", str(self_))
                return real_is_file(self_)
            Path.is_dir, Path.is_file = denied, denied_file
            try:
                self.assertEqual(wq.find_vault(str(vault)), vault)
                req = wq.build(vault, "INBOX.md", "append", "- [ ] x", None, "s", "session", None)
                self.assertIsNone(req["target_existed"], "unknown, not a guess")
            finally:
                Path.is_dir, Path.is_file = real_is_dir, real_is_file
            self.config(vault_path=str(vault))                  # sandbox off: still raises
            Path.is_dir = denied
            try:
                with self.assertRaises(PermissionError):
                    wq.find_vault(str(vault))
            finally:
                Path.is_dir = real_is_dir
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


@unittest.skipIf(IS_WINDOWS, "chmod does not make a Windows folder read-only, and native Windows "
                             "has no Claude Code sandbox")
@unittest.skipIf(IS_ROOT, "root ignores a read-only folder")
class ReadOnlyVault(Sandbox):
    """Each tool on the B4 list, against a vault the OS will not let it write."""

    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.tools = self.vault / "Projects" / "golden-thread" / "tools"
        for f in TOOLS.glob("*.py"):                         # the release under test's tools
            shutil.copy2(f, self.tools / f.name)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env.pop("SANDBOX_RUNTIME", None)
        # one queued request, so the drain has work to refuse
        p = self.py(SCRIPTS / "gt_write_queue.py", "--vault", self.vault, "--path", "INBOX.md",
                    "--op", "append", "--content", "- [ ] queued before the lock")
        self.assertOk(p)
        self.config(vault_path=str(self.vault), sandbox_mode="on", sandbox_vault_reads="allow")
        # gt_tasks writes nothing when the roll-up is unchanged (0.20.1, install branch), and then
        # a read-only vault is no refusal at all. Remove the receipt so the roll-up is pending and
        # the tool has a write the OS must refuse.
        try:
            (self.vault / "Projects" / "golden-thread" / ".tasks-digest").unlink()
        except FileNotFoundError:
            pass
        self.addCleanup(self.unlock)
        self.lock()

    def _walk(self):
        for root, dirs, files in os.walk(self.vault):
            yield root, True
            for f in files:
                yield os.path.join(root, f), False

    def lock(self):
        for p, is_dir in list(self._walk()):
            os.chmod(p, 0o555 if is_dir else 0o444)

    def unlock(self):
        for p, is_dir in list(self._walk()):
            try:
                os.chmod(p, 0o755 if is_dir else 0o644)
            except OSError:
                pass

    CASES = (
        ("gt_tasks", lambda s: (s.tools / "gt_tasks.py", "--vault", s.vault), None),
        ("gt_adr", lambda s: (s.tools / "gt_adr.py", "--vault", s.vault, "allocate",
                              "golden-thread"), None),
        ("gt_log", lambda s: (s.tools / "gt_log.py", "--vault", s.vault, "add",
                              "2026-10-04 10:00 CDT [work] golden-thread -- test"), None),
        ("gt_task", lambda s: (s.tools / "gt_task.py", "add", "--vault", s.vault, "--project",
                               "golden-thread", "a task"), None),
        ("gt_closeout", lambda s: (s.tools / "gt_closeout.py", "--vault", s.vault, "ask",
                                   "golden-thread"), None),
        ("vault_init", lambda s: (SCRIPTS / "vault_init.py", "create-project", "--vault",
                                  s.vault, "--name", "foo"), None),
        ("gt_broker", lambda s: (SCRIPTS / "gt_broker.py", "drain", "--vault", s.vault),
         "vault_queue_drain"),
    )

    def run_tool(self, args, sandboxed):
        env = {"CLAUDECODE": "1"} if sandboxed else {}
        return self.py(*args(self), env=env)

    def test_in_the_sandbox_each_tool_prints_one_line_and_exits_5(self):
        before = set(os.listdir(self.home / ".claude"))
        for tool, args, mcp in self.CASES:
            with self.subTest(tool=tool):
                p = self.run_tool(args, sandboxed=True)
                lines = [x for x in p.stderr.splitlines() if x.strip()]
                self.assertNotIn("Traceback", p.stderr, p.stderr)
                self.assertEqual(len(lines), 1, "%s printed %d stderr lines:\n%s"
                                 % (tool, len(lines), p.stderr))
                self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
                line = lines[0]
                self.assertTrue(line.startswith(tool + ": refused: "), line)
                self.assertIn(WHY, line)
                self.assertNotIn("Full Disk Access", line)
                self.assertIn("Next: ", line)
                self.assertIn(str(args(self)[0]), line, "the command names the real script")
                if mcp:
                    self.assertIn("use the gt-vault MCP tool %s" % mcp, line)
        after = set(os.listdir(self.home / ".claude"))
        self.assertEqual(sorted(x for x in after - before if x.startswith("pending_")), [],
                         "a sandbox refusal must leave no pending file to replay")
        litter = [p for p, _d in self._walk() if ".pending-" in p]
        self.assertEqual(litter, [], "no sidecar beside the refused target")

    def test_a_terminal_run_keeps_the_interpreter_wording(self):
        """Not Claude's process (no CLAUDECODE): the refusal is not the sandbox's."""
        p = self.run_tool(self.CASES[1][1], sandboxed=False)
        self.assertNotIn("Traceback", p.stderr, p.stderr)
        self.assertNotIn(WHY, p.stderr)
        self.assertIn("gt: cannot write", p.stderr)


@unittest.skipIf(IS_WINDOWS, "chmod does not make a Windows folder read-only")
@unittest.skipIf(IS_ROOT, "root ignores a read-only folder")
class OutsideTheSandbox(Sandbox):
    """gt_log outside the sandbox: the WORDING finding (say the entry is safe in the spool) and
    the replay hint with a full path (B4: `python3 safe_write.py replay` named no path)."""

    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.tools = self.vault / "Projects" / "golden-thread" / "tools"
        for f in TOOLS.glob("*.py"):
            shutil.copy2(f, self.tools / f.name)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env.pop("SANDBOX_RUNTIME", None)
        self.config(vault_path=str(self.vault))
        self.locked = []
        self.addCleanup(self.unlock)

    def lock(self, *paths):
        for p in paths:
            os.chmod(p, 0o555 if os.path.isdir(p) else 0o444)
            self.locked.append(p)

    def unlock(self):
        for p in self.locked:
            os.chmod(p, 0o755 if os.path.isdir(p) else 0o644)

    def add(self):
        return self.py(self.tools / "gt_log.py", "--vault", self.vault, "add",
                       "2026-10-04 10:00 CDT [work] golden-thread -- test", "--id", "sess-1")

    def test_a_refused_log_md_says_the_entry_is_safe_in_the_spool(self):
        self.lock(self.vault / "log.md", self.vault)
        p = self.add()
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        lines = [x for x in p.stderr.splitlines() if x.strip()]
        self.assertEqual(len(lines), 1, p.stderr)
        self.assertIn("The entry is safe in the spool", lines[0])
        self.assertIn(str(self.tools / "gt_log.py"), lines[0])
        self.assertIn("merge", lines[0])

    def test_a_refused_spool_names_the_full_replay_command(self):
        spool = self.vault / "Projects" / "golden-thread" / "spool" / "log"
        spool.mkdir(parents=True, exist_ok=True)
        self.lock(spool)
        p = self.add()
        self.assertNotIn("Traceback", p.stderr, p.stderr)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("%s replay" % (self.tools / "safe_write.py"), p.stderr)
        for pend in (self.home / ".claude").glob("pending_*"):
            pend.unlink()


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("sandbox-exec"),
                     "macOS Seatbelt (sandbox-exec) only")
class SeatbeltLikeGt(Sandbox):
    """The real thing on macOS: Seatbelt with gt's deny lists (vault and ~/.claude/golden-thread
    write-denied; with sandbox_vault_reads deny, the vault read-denied -- stat included)."""

    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.tools = self.vault / "Projects" / "golden-thread" / "tools"
        for f in TOOLS.glob("*.py"):
            shutil.copy2(f, self.tools / f.name)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env.pop("SANDBOX_RUNTIME", None)
        p = self.py(SCRIPTS / "gt_write_queue.py", "--vault", self.vault, "--path", "INBOX.md",
                    "--op", "append", "--content", "- [ ] queued before the fence")
        self.assertOk(p)

    def profile(self, reads):
        v, h = os.path.realpath(self.vault), os.path.realpath(self.home)
        deny = ['(deny file-write* (subpath "%s") (subpath "%s/.claude/golden-thread") '
                '(literal "%s/.claude/settings.json") (literal "%s/.claude/vault-config.json"))'
                % (v, h, h, h)]
        if not reads:
            deny.append('(deny file-read* (subpath "%s"))' % v)
        f = self.tmp / ("gt-%s.sb" % ("w" if reads else "rw"))
        f.write_text("(version 1)\n(allow default)\n" + "\n".join(deny) + "\n")
        return f

    def fenced(self, prof, *args):
        e = dict(self.env, CLAUDECODE="1")
        return subprocess.run(["sandbox-exec", "-f", str(prof), PYTHON] + [str(a) for a in args],
                              env=e, capture_output=True, text=True, timeout=120)

    def one_line(self, p, tool, mcp=None):
        lines = [x for x in p.stderr.splitlines() if x.strip()]
        self.assertNotIn("Traceback", p.stderr, p.stderr)
        self.assertEqual(len(lines), 1, p.stderr)
        self.assertTrue(lines[0].startswith(tool + ": refused: "), lines[0])
        self.assertIn(WHY, lines[0])
        self.assertNotIn("Full Disk Access", lines[0])
        if mcp:
            self.assertIn(mcp, lines[0])
        self.assertEqual(p.returncode, 5)

    def test_vault_reads_denied(self):
        self.config(vault_path=str(self.vault), sandbox_mode="on")
        prof = self.profile(reads=False)
        for tool, args, mcp in (
                ("gt_lint", (SCRIPTS / "gt_lint.py", "--vault", self.vault), None),
                ("gt_promote_detect", (SCRIPTS / "gt_promote_detect.py", "--vault", self.vault),
                 None),
                ("vault_init", (SCRIPTS / "vault_init.py", "create-project", "--vault",
                                self.vault, "--name", "foo"), None),
                ("gt_broker", (SCRIPTS / "gt_broker.py", "drain", "--vault", self.vault),
                 "vault_queue_drain")):
            with self.subTest(tool=tool):
                p = self.fenced(prof, *args)
                self.one_line(p, tool, mcp)
                self.assertIn("read only through the gt-vault MCP", p.stderr)
        p = self.fenced(prof, SCRIPTS / "gt_write_queue.py", "--vault", self.vault, "--path",
                        "INBOX.md", "--op", "append", "--content", "- [ ] from the fence")
        self.assertOk(p, "the inbox fallback must be reached even when stat is refused")
        self.assertIn(".gt-inbox", p.stdout)

    def test_vault_reads_allowed(self):
        self.config(vault_path=str(self.vault), sandbox_mode="on", sandbox_vault_reads="allow")
        prof = self.profile(reads=True)
        for tool, args in (
                ("gt_tasks", (self.tools / "gt_tasks.py", "--vault", self.vault)),
                ("gt_adr", (self.tools / "gt_adr.py", "--vault", self.vault, "allocate",
                            "golden-thread")),
                ("gt_log", (self.tools / "gt_log.py", "--vault", self.vault, "add",
                            "2026-10-04 10:00 CDT [work] golden-thread -- test"))):
            with self.subTest(tool=tool):
                self.one_line(self.fenced(prof, *args), tool)
        self.assertEqual(list((self.home / ".claude").glob("pending_*")), [])


if __name__ == "__main__":
    unittest.main()
