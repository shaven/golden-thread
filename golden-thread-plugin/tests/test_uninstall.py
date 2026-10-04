"""gt_uninstall.py (0.20.1, M15): a complete uninstall with --check, a summary and a re-scan.

Every test runs against a throwaway HOME with GT_TEST_SANDBOX=1, so no real scheduler, crontab,
git config or unlock daemon is touched: gt_uninstall's own run() refuses the write verbs from a
sandbox, and the in-process tests replace run() altogether.
"""
import hashlib
import io
import json
import os
import socket
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from _harness import Sandbox, SCRIPTS, cached_sandbox, load_module, source_fingerprint, IS_WINDOWS
import test_install as _ti   # module import only: its TestCases must not be collected here

TOOL = SCRIPTS / "gt_uninstall.py"
MARKET = "golden-thread-plugin"
SHIM = "#!/usr/bin/env bash\n# gt-python3-shim -- written by gt's install.sh\nexec python \"$@\"\n"


def tree_digest(root: Path, skip=()):
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if any(rel == s or rel.startswith(s + "/") for s in skip):
            continue
        h.update(rel.encode())
        if p.is_file() and not p.is_symlink():
            h.update(p.read_bytes())
    return h.hexdigest()


def _build(case):
    repo = _ti.build_repo_fixture(case, case.tmp / "src" / "golden-thread-plugin")
    case.make_vault()
    q = case.sh(repo / "install.sh", timeout=600)
    return {"returncode": q.returncode, "stdout": q.stdout, "stderr": q.stderr}


class InstalledMachine(Sandbox):
    """A real install.sh run into the sandbox HOME, then uninstalled."""

    def setUp(self):
        super().setUp()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        key = "uninstall|" + source_fingerprint(_ti.GT, _ti.WIKI)
        r, _how = cached_sandbox(self, key, _build)
        self.assertEqual(r["returncode"], 0, r["stdout"][-2000:] + r["stderr"][-2000:])
        self.vault = self.tmp / "vault"
        self.claude = self.home / ".claude"
        # What a user (and older releases) leave beside an install:
        s = json.loads((self.claude / "settings.json").read_text(encoding="utf-8"))
        s.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/local/bin/mine.sh"}]})
        s.setdefault("enabledPlugins", {})["other@elsewhere"] = True
        (self.claude / "settings.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
        la = self.home / "Library" / "LaunchAgents"
        la.mkdir(parents=True)
        (la / "com.markethaven.gt-daily.plist").write_text("<plist/>", encoding="utf-8")
        (la / "io.goldenthread.gt-lint-weekly.plist").write_text("<plist/>", encoding="utf-8")
        (la / "com.example.other.plist").write_text("<plist/>", encoding="utf-8")
        (self.home / "bin").mkdir(exist_ok=True)       # Windows: the install wrote its shim there
        (self.home / "bin" / "python3").write_text(SHIM, encoding="utf-8")
        (self.home / ".gt-inbox" / "queue").mkdir(parents=True)
        (self.home / ".gt-scratch" / "run1").mkdir(parents=True)
        self.env["GT_SCRATCH_ROOT"] = str(self.home / ".gt-scratch")
        self.env["GT_UNINSTALL_IPC_ROOT"] = str(self.tmp / "ipc")

    def uninstall(self, *args):
        p = self.py(TOOL, *args, timeout=300)
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        return p

    def test_check_changes_nothing_then_run_removes_exactly_gts(self):
        before_home = tree_digest(self.home)
        before_vault = tree_digest(self.vault)
        p = self.uninstall("--check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for want in ("plugin cache", "settings.json gt entries", "~/.claude/golden-thread",
                     "scheduled jobs", "com.markethaven.gt-daily", "io.goldenthread.gt-lint-weekly",
                     "~/bin/python3 shim", "vault-config.json", "never touched"):
            self.assertIn(want, p.stdout)
        self.assertEqual(tree_digest(self.home), before_home, "--check changed the home")

        p = self.uninstall("--yes")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("re-scan clean", p.stdout)
        self.assertEqual(tree_digest(self.vault), before_vault, "the vault was touched")

        self.assertFalse((self.claude / "plugins" / "cache" / MARKET).exists())
        self.assertFalse((self.claude / "plugins" / "marketplaces" / MARKET).exists())
        gt = self.claude / "golden-thread"
        self.assertEqual(sorted(e.name for e in gt.iterdir()) if gt.exists() else [],
                         ["backups"] if (gt / "backups").exists() else [])
        self.assertFalse((self.claude / "vault-config.json").exists())
        s = json.loads((self.claude / "settings.json").read_text(encoding="utf-8"))
        cmds = [h["command"] for blocks in s.get("hooks", {}).values() for b in blocks
                for h in b["hooks"]]
        self.assertEqual(cmds, ["/usr/local/bin/mine.sh"])
        self.assertEqual(s["enabledPlugins"], {"other@elsewhere": True})
        inst = json.loads((self.claude / "plugins" / "installed_plugins.json").read_text())
        self.assertFalse([k for k in inst.get("plugins", {}) if k.endswith("@" + MARKET)])
        known = json.loads((self.claude / "plugins" / "known_marketplaces.json").read_text())
        self.assertNotIn(MARKET, known)
        la = self.home / "Library" / "LaunchAgents"
        self.assertEqual(sorted(f.name for f in la.iterdir()), ["com.example.other.plist"])
        self.assertFalse((self.home / "bin" / "python3").exists())
        self.assertFalse((self.home / ".gt-inbox").exists())
        self.assertFalse((self.home / ".gt-scratch").exists())

        backups = sorted(self.claude.glob("gt-uninstall-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "settings.json").is_file())
        self.assertTrue((backups[0] / "vault-config.json").is_file())
        self.assertIn("cp -p", p.stdout)
        old = json.loads((backups[0] / "settings.json").read_text(encoding="utf-8"))
        self.assertTrue(any(k.endswith("@" + MARKET) for k in old["enabledPlugins"]))

        # Proven gone: a second look finds nothing, a second run has nothing to do.
        again = self.uninstall("--check", "--json")
        data = json.loads(again.stdout)
        self.assertEqual([i for i in data["items"] if i["state"] == "present"], [])
        self.assertEqual(self.uninstall("--yes").returncode, 0)

    def test_without_confirmation_nothing_changes(self):
        before = tree_digest(self.home)
        p = self.uninstall()                       # stdin is not a TTY, no --yes
        self.assertEqual(p.returncode, 2)
        self.assertIn("--yes", p.stdout)
        self.assertEqual(tree_digest(self.home), before)

    def test_queued_vault_writes_refuse_unless_discarded(self):
        (self.home / ".gt-inbox" / "queue" / "r1.json").write_text("{}", encoding="utf-8")
        before = tree_digest(self.home)
        p = self.uninstall("--yes")
        self.assertEqual(p.returncode, 2)
        self.assertIn("drain", p.stderr)
        self.assertEqual(tree_digest(self.home), before)
        self.assertIn("BLOCKER", self.uninstall("--check").stdout)
        p = self.uninstall("--yes", "--discard-queued")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse((self.home / ".gt-inbox").exists())

    def test_keep_vault_config_and_a_python3_that_is_not_gts(self):
        (self.home / "bin" / "python3").write_text("#!/bin/sh\nexec /opt/py \"$@\"\n")
        p = self.uninstall("--yes", "--keep-vault-config")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.claude / "vault-config.json").is_file())
        self.assertTrue((self.home / "bin" / "python3").is_file())
        self.assertIn("not gt's", p.stdout)


class Refusals(Sandbox):
    def test_no_dot_claude_is_refused(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        p = self.py(TOOL, "--yes", env={"HOME": str(bare), "USERPROFILE": str(bare)})
        self.assertEqual(p.returncode, 2)
        self.assertIn("no .claude", p.stderr)

    def test_nothing_installed_is_clean(self):
        p = self.py(TOOL, "--yes")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("nothing of gt's is installed", p.stdout)


class InProcess(Sandbox):
    """Steps whose system calls are stubbed: run() is replaced, real_home() forced."""

    def setUp(self):
        super().setUp()
        self.mod = load_module(TOOL, "gt_uninstall_t")
        # In-process: this test process's own environment is what the module reads, so mark it
        # a sandbox -- never the machine's /tmp or scheduler.
        env = mock.patch.dict(os.environ, {"GT_TEST_SANDBOX": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []
        self.claude = self.home / ".claude"

    def main_raw(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.mod, "run", lambda *a, **k: self.mod._R(0, "", "")), \
                redirect_stdout(out), redirect_stderr(err):
            rc = self.mod.main(["--home", str(self.home)] + list(args))
        return rc, out.getvalue(), err.getvalue()

    def main(self, *args, responses=None):
        responses = responses or {}

        def fake_run(args, env=None, input=None, timeout=120):
            args = [str(a) for a in args]
            self.calls.append((args, input))
            for key, resp in responses.items():
                if key in " ".join(args):
                    return self.mod._R(*resp)
            return self.mod._R(0, "", "")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.mod, "run", fake_run), \
                mock.patch.dict(os.environ, {"GT_UNINSTALL_IPC_ROOT": str(self.tmp / "ipc")}), \
                redirect_stdout(out), redirect_stderr(err):
            rc = self.mod.main(["--home", str(self.home)] + list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_failed_sandbox_removal_keeps_gt_home_and_fails(self):
        st = self.claude / "golden-thread" / "sandbox"
        st.mkdir(parents=True)
        (st / "state.json").write_text("{}", encoding="utf-8")
        hooks = self.claude / "golden-thread" / "hooks"
        hooks.mkdir()
        (hooks / "gt_sandbox.py").write_text("# stub\n", encoding="utf-8")
        rc, out, _ = self.main("--yes", responses={"gt_sandbox.py remove": (1, "", "boom")})
        self.assertEqual(rc, 1)
        self.assertTrue((st / "state.json").is_file(), "the sandbox record was destroyed")
        self.assertIn("FAILED", out.upper())
        self.assertIn("INCOMPLETE", out)

    def test_claude_md_section_removed_only_when_untouched(self):
        vault = str(self.tmp / "v")
        self.config(vault_path=vault)
        sec = self.mod._gt_section_text(vault)
        md = self.claude / "CLAUDE.md"
        md.write_text("# Mine\n\nkeep me\n\n" + sec + "\n## Later\n\nalso mine\n", encoding="utf-8")
        rc, out, _ = self.main("--yes")
        self.assertEqual(rc, 0, out)
        text = md.read_text(encoding="utf-8")
        self.assertNotIn("## Golden Thread", text)
        self.assertIn("keep me", text)
        self.assertIn("## Later", text)

        md.write_text("## Golden Thread\n\nmy own words\n", encoding="utf-8")
        self.config(vault_path=vault)
        rc, out, _ = self.main("--yes")
        self.assertEqual(md.read_text(encoding="utf-8"), "## Golden Thread\n\nmy own words\n")
        self.assertIn("by hand", out)

    @unittest.skipIf(IS_WINDOWS, "crontab and git config are POSIX here")
    def test_cron_lines_and_git_helper_on_the_real_account(self):
        mine = "0 7 * * * python3 %s/.claude/hooks/gt_watch.py fetch # gt-watch" % self.home
        theirs = "0 7 * * * python3 /other/home/.claude/hooks/gt_watch.py fetch # gt-watch"
        user = "5 5 * * * /usr/bin/backup"
        legacy = "0 22 * * * python3 x # io.goldenthread.gt-daily"
        crontab = "\n".join([mine, theirs, user, legacy]) + "\n"
        helper = "credential.https://github.com.helper !python3 ~/.claude/golden-thread/hooks/gt_unlock.py git-credential\n"
        with mock.patch.object(self.mod, "real_home", lambda h: True), \
                mock.patch.object(self.mod.shutil, "which", lambda n: "/usr/bin/" + n):
            rc, out, _ = self.main("--yes", responses={"crontab -l": (0, crontab, ""),
                                                       "--get-regexp": (0, helper, "")})
        writes = [inp for args, inp in self.calls if args[:2] == ["crontab", "-"]]
        self.assertTrue(writes)
        for w in writes:
            self.assertIn(user, w)
            self.assertIn(theirs, w)
        self.assertNotIn(legacy, writes[0])
        self.assertTrue(any(mine not in w for w in writes))
        self.assertIn(["git", "config", "--global", "--unset-all",
                       "credential.https://github.com.helper"], [a for a, _i in self.calls])
        backups = list(self.claude.glob("gt-uninstall-backup-*/crontab*"))
        self.assertTrue(backups)

    @unittest.skipIf(IS_WINDOWS, "Unix-domain IPC dirs are POSIX only")
    def test_stale_ipc_dirs_go_live_ones_stay(self):
        import tempfile
        ipc = Path(tempfile.mkdtemp(prefix="gtu-", dir="/tmp"))     # short: a socket path is capped
        self.addCleanup(__import__("shutil").rmtree, str(ipc), True)
        stale = ipc / ("gt-%d-aaaaaaaaaaaa" % os.getuid())
        live = ipc / ("gt-%d-bbbbbbbbbbbb" % os.getuid())
        other = ipc / "gt-0-cccccccccccc"
        for d in (stale, live, other):
            d.mkdir(parents=True)
        (stale / "unlockd.sock").write_text("", encoding="utf-8")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(live / "u.sock"))
        srv.listen(64)
        try:
            with mock.patch.dict(os.environ, {"GT_UNINSTALL_IPC_ROOT": str(ipc)}):
                rc, out, _ = self.main_raw("--yes")
        finally:
            srv.close()
        self.assertEqual(rc, 0, out)
        self.assertFalse(stale.exists())
        self.assertTrue(live.exists())
        self.assertTrue(other.exists())

    def test_check_with_unreadable_settings_exits_1(self):
        (self.claude / "settings.json").write_text("{not json", encoding="utf-8")
        rc, out, _ = self.main("--check")
        self.assertEqual(rc, 1)
        self.assertIn("unreadable", out)


if __name__ == "__main__":
    unittest.main()
