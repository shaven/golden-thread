"""gt_doctor.py post-install -- the release gate, and the two places it runs by default.

Contract:
  * One command, after install.sh: a PASS/FAIL table, exit 1 on any FAIL, 0 otherwise.
    WARN (a waiting write queue), INFO (an optional thing not installed) and PENDING never
    fail it.
  * Every row fails closed: a row that cannot run is FAIL "could not run: ...", never PASS.
  * Read-only: the vault, settings.json and vault-config.json are byte-identical afterwards.
  * --stage install|session: what only /gt:gt-upgrade can make true is PENDING, not FAIL.
  * install.sh runs it at the end of every install with a vault; a real FAIL is exit 9.
  * gt_version_check.py, as a SessionStart hook, runs it once per installed gt version and
    shows the result in systemMessage; the second session is silent.

The install is REAL (install.sh into a throwaway HOME), because the gate's whole claim is
about what an install leaves behind. One install per class; tests that break something put
it back with addCleanup.
"""
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from _harness import Sandbox, REPO, PYTHON, latest_version_dir, cached_sandbox, source_fingerprint
import test_install as _ti   # module import only: its TestCases must not be collected here

try:
    LOTR = latest_version_dir(REPO / "golden-thread-lotr")
except (RuntimeError, OSError):
    LOTR = None

OLD_RULE1 = "Before writing a vault file, claim it with gt_session.py claim."


class InstalledMachine(Sandbox):
    """A shared, real install: HOME, vault and repo built once per class."""
    _shared = None
    PRE_INSTALL = None          # hook: (self) -> None, run after the vault exists, before install
    INSTALL_ARGS = ()

    @classmethod
    def tearDownClass(cls):
        if cls._shared:
            shutil.rmtree(cls._shared["tmp"], ignore_errors=True)
            cls._shared = None
        super().tearDownClass()

    def setUp(self):
        cls = type(self)
        if cls._shared is None:
            super().setUp()
            self.env["PYTHONDONTWRITEBYTECODE"] = "1"
            def build(case):
                repo = _ti.build_repo_fixture(case, case.tmp / "src" / "golden-thread-plugin")
                if LOTR is not None:
                    shutil.copytree(LOTR, repo / "golden-thread-lotr" / LOTR.name,
                                    ignore=_ti.IGNORE)
                case.vault, case.repo = case.make_vault(), repo
                if cls.PRE_INSTALL:
                    cls.PRE_INSTALL(case)
                q = case.sh(repo / "install.sh", *cls.INSTALL_ARGS, timeout=600)
                return {"returncode": q.returncode, "stdout": q.stdout, "stderr": q.stderr}

            if cls.PRE_INSTALL:
                # Its install depends on what PRE_INSTALL did first: never from the cache.
                r = build(self)
            else:
                # 0.18.1: classes with the same install share ONE build per run
                # (tests/_harness.cached_sandbox; tests/test_cached_install.py).
                key = "postinstall|%s|%s" % (" ".join(cls.INSTALL_ARGS),
                                             source_fingerprint(_ti.GT, _ti.WIKI,
                                                                *([LOTR] if LOTR else [])))
                r, _how = cached_sandbox(self, key, build)
            vault = self.tmp / "vault"
            repo = self.tmp / "src" / "golden-thread-plugin"
            self.vault, self.repo = vault, repo
            p = subprocess.CompletedProcess(["install.sh"], r["returncode"], r["stdout"],
                                            r["stderr"])
            cls._shared = {"tmp": self.tmp, "home": self.home, "env": self.env,
                           "vault": vault, "repo": repo, "install": p}
        s = cls._shared
        self.tmp, self.home, self.env = s["tmp"], s["home"], s["env"]
        self.vault, self.repo, self.install_proc = s["vault"], s["repo"], s["install"]
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"

    def tearDown(self):
        pass                     # the shared install outlives each test; tearDownClass removes it

    # -- helpers -------------------------------------------------------------------
    def gate(self, *args):
        p = self.py(self.hooks / "gt_doctor.py", "post-install", "--vault", self.vault,
                    "--json", *args)
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        data = json.loads(p.stdout)
        return p.returncode, {r["row"]: r for r in data["rows"]}, data

    def break_file(self, path, new_text=None):
        """Replace (or remove, when new_text is None) a file; put it back after the test."""
        path = Path(path)
        saved = path.read_bytes() if path.exists() else None
        mode = path.stat().st_mode if path.exists() else None

        def restore():
            if saved is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(saved)
                os.chmod(path, mode)
        self.addCleanup(restore)
        if new_text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_text, encoding="utf-8")

    def rule1_file(self):
        return self.vault / "core-rules" / "core_concurrent_session_claim.md"

    def set_old_rule1(self):
        f = self.rule1_file()
        text = f.read_text(encoding="utf-8")
        lines = [('imperative: "%s"' % OLD_RULE1) if l.startswith("imperative:") else l
                 for l in text.splitlines()]
        self.break_file(f, "\n".join(lines) + "\n")

    def daily_plist(self, script):
        path = self.home / "Library" / "LaunchAgents" / "com.markethaven.gt-daily.plist"
        data = {"Label": "com.markethaven.gt-daily",
                "ProgramArguments": [PYTHON, str(script), "--vault", str(self.vault)]}
        self.break_file(path, "")
        with path.open("wb") as fh:
            plistlib.dump(data, fh)
        return path

    def session_hook(self):
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        cmd = next(h["command"] for b in s["hooks"]["SessionStart"] for h in b["hooks"]
                   if "gt_version_check.py" in h["command"])
        p = self.run_cmd(["bash", "-c", cmd], input="{}")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return json.loads(p.stdout)["systemMessage"]


class CorrectInstallPasses(InstalledMachine):
    def test_install_itself_succeeded(self):
        self.assertEqual(self.install_proc.returncode, 0,
                         self.install_proc.stdout[-3000:] + self.install_proc.stderr[-2000:])

    def test_every_row_passes_and_exit_is_zero(self):
        rc, rows, data = self.gate()
        bad = {k: r for k, r in rows.items() if r["state"] not in ("PASS", "INFO")}
        self.assertEqual(bad, {}, "a correct install must pass every row")
        self.assertEqual(rc, 0)
        self.assertFalse(data["failed"])
        for row in ("installed", "components", "wiring", "core-rules", "rule1-vault",
                    "rule1-injected", "queue-scripts", "guard-denies", "queue", "vault",
                    "lotr", "daily-job"):
            self.assertIn(row, rows, "the gate lost its %s row" % row)
        for row in ("installed", "components", "wiring", "rule1-vault", "rule1-injected",
                    "queue-scripts", "guard-denies", "queue", "vault", "smoke-queue",
                    "smoke-escalate", "smoke-guard", "smoke-rules", "smoke-daily"):
            self.assertEqual(rows[row]["state"], "PASS", rows[row])
        self.assertIn(rows["smoke-lotr"]["state"], ("PASS", "INFO"))

    def test_the_whole_run_stays_inside_the_time_budget(self):
        """Owner: about a minute on this Mac. Asserted generously so it cannot creep."""
        _rc, _rows, data = self.gate()
        self.assertLess(data["elapsed_s"], 90, "post-install took %ss" % data["elapsed_s"])
        p = self.py(self.hooks / "gt_doctor.py", "post-install", "--vault", self.vault)
        self.assertRegex(p.stdout.strip().splitlines()[-1], r"— \d+\.\ds$",
                         "the summary line must carry the elapsed time")

    def test_smoke_leaves_nothing_behind(self):
        # A private TMPDIR: other test classes run their own gates in parallel, so the shared
        # temp dir is not a stable thing to compare against.
        tmpdir = Path(tempfile.mkdtemp(prefix="gt-pi-tmp-"))
        self.addCleanup(shutil.rmtree, str(tmpdir), True)
        p = self.py(self.hooks / "gt_doctor.py", "post-install", "--vault", self.vault,
                    "--json", env={"TMPDIR": str(tmpdir)})
        self.assertEqual(p.returncode, 0, p.stdout[-2000:])
        self.assertEqual(list(tmpdir.iterdir()), [], "a smoke temp dir was left behind")

    def test_dry_run_is_accepted(self):
        rc, _rows, _ = self.gate("--dry-run")
        self.assertEqual(rc, 0)


class BrokenRowsFail(InstalledMachine):
    def test_old_rule1_text_in_the_vault_fails_row_2(self):
        self.set_old_rule1()
        rc, rows, _ = self.gate()
        self.assertEqual(rows["rule1-vault"]["state"], "FAIL", rows["rule1-vault"])
        self.assertEqual(rows["rule1-injected"]["state"], "FAIL", rows["rule1-injected"])
        self.assertIn("gt-upgrade", rows["rule1-vault"]["fix"])
        self.assertEqual(rc, 1)

    def test_before_the_upgrade_the_same_rows_are_pending_not_failed(self):
        self.set_old_rule1()
        for stage in ("install", "session"):
            rc, rows, _ = self.gate("--stage", stage)
            self.assertEqual(rows["rule1-vault"]["state"], "PENDING", stage)
            self.assertEqual(rows["rule1-injected"]["state"], "PENDING", stage)
            self.assertEqual(rc, 0, "PENDING must not fail the %s stage" % stage)

    def test_missing_broker_in_the_hooks_dir_fails_row_3(self):
        self.break_file(self.hooks / "gt_broker.py")
        rc, rows, _ = self.gate()
        self.assertEqual(rows["queue-scripts"]["state"], "FAIL")
        self.assertIn("gt_broker.py", rows["queue-scripts"]["summary"])
        self.assertTrue(rows["queue-scripts"]["fix"])
        # the queue row cannot run without it: FAIL, never PASS
        self.assertEqual(rows["queue"]["state"], "FAIL")
        self.assertIn("could not run", rows["queue"]["summary"])
        self.assertEqual(rc, 1)

    def test_a_hooks_dir_copy_that_differs_fails_row_3(self):
        f = self.hooks / "gt_demote.py"
        self.break_file(f, f.read_text(encoding="utf-8") + "\n# local edit\n")
        _rc, rows, _ = self.gate()
        self.assertEqual(rows["queue-scripts"]["state"], "FAIL")
        self.assertIn("differ", rows["queue-scripts"]["summary"])

    def test_a_guard_that_does_not_deny_fails_row_4(self):
        self.break_file(self.hooks / "guard_session_claims.sh", "#!/usr/bin/env bash\nexit 0\n")
        rc, rows, _ = self.gate()
        self.assertEqual(rows["guard-denies"]["state"], "FAIL", rows["guard-denies"])
        self.assertIn("NOT denied", rows["guard-denies"]["summary"])
        self.assertEqual(rc, 1)

    def test_a_guard_that_denies_everything_fails_row_4(self):
        deny = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                  "permissionDecision": "deny",
                                                  "permissionDecisionReason": "x"}})
        self.break_file(self.hooks / "guard_session_claims.sh",
                        "#!/usr/bin/env bash\ncat >/dev/null\necho '%s'\n" % deny)
        _rc, rows, _ = self.gate()
        self.assertEqual(rows["guard-denies"]["state"], "FAIL")
        self.assertIn("OUTSIDE", rows["guard-denies"]["summary"])

    def test_a_missing_hook_is_could_not_run_never_pass(self):
        self.break_file(self.hooks / "inject_core_rules.sh")
        _rc, rows, _ = self.gate()
        self.assertEqual(rows["rule1-injected"]["state"], "FAIL")
        self.assertIn("could not run", rows["rule1-injected"]["summary"])

    def test_a_broken_broker_copy_fails_the_queue_smoke(self):
        # Present (so the placement row's only complaint is the byte difference), exits 0,
        # and does nothing: exactly the copy that LOOKS installed.
        self.break_file(self.hooks / "gt_broker.py",
                        "import json, sys\nif 'status' in sys.argv:\n"
                        "    print(json.dumps({'pending': 0}))\nsys.exit(0)\n")
        rc, rows, _ = self.gate()
        self.assertEqual(rows["smoke-queue"]["state"], "FAIL", rows["smoke-queue"])
        self.assertIn("does not hold the queued text", rows["smoke-queue"]["summary"])
        self.assertEqual(rc, 1)

    def test_a_broker_that_overwrites_fails_the_escalation_smoke(self):
        self.break_file(self.hooks / "gt_broker.py",
                        "import json, sys\nprint(json.dumps({'results': [{'decision': 'apply'}]}))\n")
        _rc, rows, _ = self.gate()
        self.assertEqual(rows["smoke-escalate"]["state"], "FAIL", rows["smoke-escalate"])

    def test_a_waiting_queue_is_warn_not_fail(self):
        content = self.tmp / "queued.txt"
        content.write_text("- a queued line\n", encoding="utf-8")
        qdir = self.vault / "Projects" / "golden-thread" / "spool" / "queue"
        before = set(qdir.glob("*")) if qdir.is_dir() else set()
        p = self.py(self.hooks / "gt_write_queue.py", "--vault", self.vault, "--path",
                    "INBOX.md", "--op", "append", "--content-file", content)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.addCleanup(lambda: [f.unlink() for f in set(qdir.glob("*")) - before])
        rc, rows, _ = self.gate()
        self.assertEqual(rows["queue"]["state"], "WARN", rows["queue"])
        self.assertIn("1 write request", rows["queue"]["summary"])
        self.assertIn("drain", rows["queue"]["fix"])
        self.assertEqual(rc, 0, "a waiting queue is a WARN; it must not fail the gate")

    def test_daily_job_pointing_elsewhere_fails_and_at_the_hooks_dir_passes(self):
        self.daily_plist("/somewhere/else/gt_daily.py")
        rc, rows, _ = self.gate()
        self.assertEqual(rows["daily-job"]["state"], "FAIL")
        self.assertEqual(rc, 1)
        self.daily_plist(self.hooks / "gt_daily.py")
        _rc, rows, _ = self.gate()
        self.assertEqual(rows["daily-job"]["state"], "PASS", rows["daily-job"])


class SessionStartRunsOncePerVersion(InstalledMachine):
    def marker(self):
        return self.home / ".claude" / "golden-thread" / "post-install-validated.json"

    def test_first_session_after_a_version_change_runs_then_stays_quiet(self):
        if self.marker().exists():
            self.break_file(self.marker())
        else:
            self.addCleanup(lambda: self.marker().unlink(missing_ok=True))
        first = self.session_hook()
        self.assertIn("post-install %s: passed" % _ti.GT.name, first)
        self.assertEqual(json.loads(self.marker().read_text())["version"], _ti.GT.name)
        second = self.session_hook()
        self.assertNotIn("post-install", second, "the gate must run once per version")
        self.assertIn("GOLDEN THREAD version", second, "the version line itself must stay")
        # A marker for another version is a version change: it runs again.
        self.marker().write_text(json.dumps({"version": "0.0.1"}))
        self.assertIn("post-install", self.session_hook())

    def test_a_fail_reaches_the_user(self):
        if self.marker().exists():
            self.break_file(self.marker())
        else:
            self.addCleanup(lambda: self.marker().unlink(missing_ok=True))
        self.daily_plist("/somewhere/else/gt_daily.py")
        msg = self.session_hook()
        self.assertIn("FAIL", msg)
        self.assertIn("daily-job", msg)
        self.assertIn("fix:", msg)


class InstallRunsTheGate(InstalledMachine):
    def test_install_prints_the_table_and_passes(self):
        out = self.install_proc.stdout
        self.assertEqual(self.install_proc.returncode, 0, out[-3000:])
        self.assertIn("Post-install validation", out)
        self.assertIn("PASS    guard-denies", out)
        self.assertNotIn("  PENDING ", out, "a fresh install has nothing pending")
        self.assertIn("post-install --vault", out, "the full-gate command must be named")


class LotrOnSmoke(InstalledMachine):
    INSTALL_ARGS = ("--with", "lotr")

    def setUp(self):
        if LOTR is None:
            self.skipTest("this tree ships no lotr module")
        super().setUp()

    def test_lotr_on_is_placed_and_smoke_tested(self):
        self.assertEqual(self.install_proc.returncode, 0, self.install_proc.stdout[-3000:])
        rc, rows, _ = self.gate()
        self.assertEqual(rows["lotr"]["state"], "PASS", rows["lotr"])
        self.assertEqual(rows["smoke-lotr"]["state"], "PASS", rows["smoke-lotr"])
        self.assertEqual(rc, 0)
        left = subprocess.run(["pgrep", "-f", "[l]otrd.py --home /tmp/gtl-"],
                              capture_output=True, text=True)
        self.assertEqual(left.stdout.strip(), "", "the smoke left lotrd running")


def _old_rule1_before_install(self):
    """A vault with its own uncommitted change, so install.sh will not apply upgrades."""
    f = self.vault / "core-rules" / "core_concurrent_session_claim.md"
    lines = [('imperative: "%s"' % OLD_RULE1) if l.startswith("imperative:") else l
             for l in f.read_text(encoding="utf-8").splitlines()]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")


class InstallBeforeUpgradeIsPending(InstalledMachine):
    PRE_INSTALL = _old_rule1_before_install

    def test_rows_that_need_the_upgrade_are_pending_and_install_succeeds(self):
        out = self.install_proc.stdout
        self.assertEqual(self.install_proc.returncode, 0, out[-3000:])
        self.assertIn("PENDING rule1-vault", out)
        self.assertIn("gt-upgrade", out)
        self.assertNotIn("FAIL    ", out)


def _old_shipped_rule1_before_install(self):
    """An existing git vault holding 0.17.10's UNMODIFIED rule 1 (the 0.17.11 release blocker:
    install-core-rules skipped it and no step refreshed it, so the old text stayed forever)."""
    old = REPO / "golden-thread" / "0.17.10" / "templates" / "core-rules" / \
        "core_concurrent_session_claim.md"
    (self.vault / "core-rules" / old.name).write_bytes(old.read_bytes())
    self.git_init(self.vault, commit=True)


class OldShippedRuleIsRefreshedByInstall(InstalledMachine):
    PRE_INSTALL = _old_shipped_rule1_before_install

    def test_rule1_rows_pass_after_the_refresh(self):
        out = self.install_proc.stdout
        self.assertIn("Updated Core rule → core_concurrent_session_claim.md", out)
        rc, rows, _ = self.gate()
        self.assertEqual(rows["rule1-vault"]["state"], "PASS", rows["rule1-vault"])
        self.assertEqual(rows["rule1-injected"]["state"], "PASS", rows["rule1-injected"])


def _bad_daily_job_before_install(self):
    path = self.home / "Library" / "LaunchAgents" / "com.markethaven.gt-daily.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        plistlib.dump({"Label": "com.markethaven.gt-daily",
                       "ProgramArguments": [PYTHON, "/elsewhere/gt_daily.py"]}, fh)


class InstallWithARealFailExitsNine(InstalledMachine):
    PRE_INSTALL = _bad_daily_job_before_install

    def test_a_real_fail_stops_the_install_with_exit_9(self):
        p = self.install_proc
        self.assertEqual(p.returncode, 9, p.stdout[-3000:])
        self.assertIn("FAIL    daily-job", p.stdout)
        self.assertIn("INSTALL FAILED VALIDATION", p.stdout)


if __name__ == "__main__":
    unittest.main()
