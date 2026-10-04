"""gt_doctor.py post-install -- the release gate, and the two places it runs by default.

Contract:
  * One command, after install.sh: a PASS/FAIL table, exit 1 on any FAIL, 0 otherwise.
    WARN (a waiting write queue), INFO (an optional thing not installed) and PENDING never
    fail it.
  * Every row fails closed: a row that cannot run is FAIL "could not run: ...", never PASS.
  * Read-only: the vault, settings.json and vault-config.json are byte-identical afterwards.
    Its one write (0.19.1) is the receipt, post-install-validated.json, after a COMPLETED
    session/final run -- never after a crash, an install-stage run or --dry-run.
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

from _harness import (Sandbox, REPO, PYTHON, IS_WINDOWS, latest_version_dir, rmtree,
                      cached_sandbox, source_fingerprint)
import test_install as _ti   # module import only: its TestCases must not be collected here

try:
    LOTR = latest_version_dir(REPO / "golden-thread-lotr")
except (RuntimeError, OSError):
    LOTR = None

OLD_RULE1 = "Before writing a vault file, claim it with gt_session.py claim."


def job_path(home, job):
    """Where an installed gt job lives: a launchd plist, or on Windows (0.20.0) the Task
    Scheduler spec gt_schedule keeps beside the job's .cmd wrapper (gt_schedule.job_file)."""
    if IS_WINDOWS:
        return home / ".claude" / "golden-thread" / "jobs" / ("gt-%s.json" % job)
    return home / "Library" / "LaunchAgents" / ("com.markethaven.gt-%s.plist" % job)


def write_job(path, data):
    """Write a job description in the form job_path() names: plist bytes, or the spec JSON --
    with the log paths every spec gt_schedule writes carries (its .cmd wrapper is built from
    them when the job is rewritten)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        logs = path.parent.parent
        job = data["Label"].rsplit("gt-", 1)[-1]
        data = dict({"StandardOutPath": str(logs / ("%s.out" % job)),
                     "StandardErrorPath": str(logs / ("%s.err" % job))}, **data)
        path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    else:
        with path.open("wb") as fh:
            plistlib.dump(data, fh)


def read_job(path):
    if IS_WINDOWS:
        return json.loads(path.read_text(encoding="utf-8"))
    with path.open("rb") as fh:
        return plistlib.load(fh)


class InstalledMachine(Sandbox):
    """A shared, real install: HOME, vault and repo built once per class."""
    _shared = None
    PRE_INSTALL = None          # hook: (self) -> None, run after the vault exists, before install
    INSTALL_ARGS = ()
    KEEP_SANDBOX_AFTER_TEST = True      # shared by every test in the class; removed below

    @classmethod
    def tearDownClass(cls):
        if cls._shared:
            # The harness rmtree: git writes read-only objects, which shutil.rmtree cannot
            # delete on Windows -- ignore_errors left the whole install behind (0.20.0).
            rmtree(cls._shared["tmp"])
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
        path = job_path(self.home, "daily")
        data = {"Label": "com.markethaven.gt-daily",
                "ProgramArguments": [PYTHON, str(script), "--vault", str(self.vault)]}
        self.break_file(path, "")
        write_job(path, data)
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


class TheReleaseIsTheInstalledOneFromAnyPath(InstalledMachine):
    """0.19.1 (request post-install-misreads-release-from-marketplace-path). Run from the
    marketplace copy, whose plugin directory is named `gt`, post-install took `gt` for the
    release and failed components against it; from the cache copy it passed. One install, one
    answer, whichever copy of the doctor is asked."""

    def copies(self):
        plugins = self.home / ".claude" / "plugins"
        cache = sorted(plugins.glob("cache/*/gt/*/scripts/gt_doctor.py"))
        market = sorted(plugins.glob("marketplaces/*/plugins/gt/scripts/gt_doctor.py"))
        self.assertTrue(cache and market, "the fixture install has no cache/marketplace copy")
        return {"cache": cache[-1], "marketplace": market[-1]}

    def run_from(self, doctor):
        p = self.py(doctor, "post-install", "--vault", self.vault, "--json")
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        return json.loads(p.stdout)

    def test_every_copy_names_the_installed_release_and_agrees_on_every_row(self):
        got = {k: self.run_from(v) for k, v in self.copies().items()}
        cache_rel = self.copies()["cache"].parent.parent.name
        for where, data in got.items():
            with self.subTest(where=where):
                self.assertEqual(data["release"], cache_rel)
        states = {k: {r["row"]: r["state"] for r in d["rows"]} for k, d in got.items()}
        self.assertEqual(states["marketplace"], states["cache"])
        self.assertFalse(got["marketplace"]["failed"], got["marketplace"]["rows"])


def _old_lint_weekly_job(case):
    """Before the install: a gt-lint-weekly job on another python, as on the publishing Mac."""
    write_job(job_path(case.home, "lint-weekly"),
              {"Label": "com.markethaven.gt-lint-weekly",
               # a path no machine's install runs, so it always differs
               "ProgramArguments": ["/opt/gt-test-old/bin/python3",
                                    str(case.home / ".claude/golden-thread/hooks/"
                                        "gt_lint_weekly.py")],
               "StartCalendarInterval": {"Hour": 7, "Minute": 0, "Weekday": 1}})


class InstallRecordsOneInterpreterForEveryJob(InstalledMachine):
    """0.19.1 (request lint-weekly-uses-an-ungranted-interpreter): install.sh records the python
    it runs under and rewrites an installed job on another one. The sandbox HOME is not the
    real user's, so nothing may be reloaded into launchd."""
    PRE_INSTALL = _old_lint_weekly_job

    def test_the_interpreter_is_recorded_and_the_old_job_rewritten_to_it(self):
        self.assertEqual(self.install_proc.returncode, 0, self.install_proc.stdout[-2000:])
        rec = json.loads((self.home / ".claude/golden-thread/interpreter.json").read_text())
        self.assertTrue(os.access(rec["python"], os.X_OK), rec)
        self.assertEqual(read_job(job_path(self.home, "lint-weekly"))["ProgramArguments"][0],
                         rec["python"])
        self.assertIn("now runs %s" % rec["python"], self.install_proc.stdout)
        self.assertNotIn("reload of", self.install_proc.stdout + self.install_proc.stderr)


def _cache_skill(case, name):
    hits = sorted((case.home / ".claude/plugins/cache").glob("*/gt/*/skills/%s/SKILL.md" % name))
    case.assertTrue(hits, "no installed %s" % name)
    head = hits[-1].read_text(encoding="utf-8").split("\n---", 1)[0]
    got = dict(l.split(":", 1) for l in head.splitlines()[1:] if ":" in l)
    return (got.get("model", "").strip() or None, got.get("effort", "").strip() or None)


class ANewInstallRunsTheAverageProfile(InstalledMachine):
    """0.19.1 (request model-and-effort-per-plugin): a new install defaults to `average` and
    writes it into the installed copies; the release source stays byte-identical."""

    def test_fast_balanced_and_deep_skills_carry_the_average_profile(self):
        self.assertEqual(_cache_skill(self, "gt-list"), ("haiku", None))
        self.assertEqual(_cache_skill(self, "gt-work"), ("sonnet", "medium"))
        self.assertEqual(_cache_skill(self, "gt-plan"), ("opus", "high"))
        self.assertIn("Model profile: average", self.install_proc.stdout)

    def test_the_release_source_carries_no_model_or_effort(self):
        for f in (self.repo / "golden-thread").glob("*/skills/*/SKILL.md"):
            head = f.read_text(encoding="utf-8").split("\n---", 1)[0]
            self.assertNotRegex(head, r"(?m)^(model|effort):", str(f))


def _previous_gt_install(case):
    """An earlier gt is recorded as installed, so this install is an UPGRADE."""
    p = case.home / ".claude" / "plugins" / "installed_plugins.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    old = case.home / ".claude/plugins/cache/golden-thread-plugin/gt/0.18.1"
    old.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": 2, "plugins": {"gt@golden-thread-plugin": [
        {"scope": "user", "installPath": str(old), "version": "0.18.1"}]}}))


class AScriptedUpgradeKeepsInherit(InstalledMachine):
    """An upgrade with no terminal changes nothing unasked: inherit, no fields written, and the
    one-time question is not used up (nothing recorded)."""
    PRE_INSTALL = _previous_gt_install

    def test_no_model_or_effort_is_written_and_nothing_is_recorded(self):
        self.assertEqual(self.install_proc.returncode, 0, self.install_proc.stdout[-2000:])
        for name in ("gt-list", "gt-work", "gt-plan"):
            self.assertEqual(_cache_skill(self, name), (None, None), name)
        choices = self.home / ".claude/golden-thread/install-choices.json"
        rec = json.loads(choices.read_text()) if choices.exists() else {}
        self.assertNotIn("profile", rec.get("model", {}))


class EveryCompletedRunWritesTheReceipt(InstalledMachine):
    """0.19.1 (request gate-receipt-only-written-at-session-start). The receipt was written only
    on the SessionStart hook path, so a manual run that passed left the previous version's
    receipt in place. Now every completed session/final run writes it and names its writer; a
    gate that could not run, an install-stage run and --dry-run write nothing."""

    def receipt(self):
        return self.home / ".claude" / "golden-thread" / "post-install-validated.json"

    def fresh(self):
        """No receipt now; whatever was there before is put back after the test."""
        if self.receipt().exists():
            self.break_file(self.receipt())
        else:
            self.addCleanup(self.receipt().unlink, missing_ok=True)

    def read(self):
        return json.loads(self.receipt().read_text())

    def test_a_manual_final_run_writes_version_verdict_counts_and_writer(self):
        self.fresh()
        rc, _rows, data = self.gate()
        r = self.read()
        self.assertEqual((r["version"], r["failed"], r["counts"], r["writer"], r["stage"]),
                         (data["release"], data["failed"], data["counts"], "manual", "final"))
        self.assertEqual(rc, 0)

    def test_a_failing_manual_run_records_the_failure(self):
        self.set_old_rule1()
        self.fresh()
        rc, _rows, data = self.gate()
        self.assertEqual(rc, 1)
        self.assertTrue(self.read()["failed"])
        self.assertEqual(self.read()["counts"], data["counts"])

    def test_the_session_hook_writes_it_as_the_hook(self):
        self.fresh()
        self.session_hook()
        r = self.read()
        self.assertEqual((r["writer"], r["stage"]), ("hook", "session"))

    def test_install_stage_and_dry_run_write_nothing(self):
        self.fresh()
        self.gate("--stage", "install")
        self.gate("--dry-run")
        self.assertFalse(self.receipt().exists())

    def test_a_gate_that_could_not_run_leaves_the_previous_receipt_byte_identical(self):
        self.break_file(self.receipt(), '{"version": "0.0.1", "failed": false}\n')
        before = self.receipt().read_bytes()
        code = ("import sys; sys.path.insert(0, %r); import gt_doctor as d\n"
                "def boom(*a, **k): raise RuntimeError('forced')\n"
                "d.post_install = boom\n"
                "sys.exit(d.main(['post-install', '--vault', %r, '--json']))\n"
                % (str(self.hooks), str(self.vault)))
        p = self.run_cmd([PYTHON, "-c", code])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("could not run", p.stdout)
        self.assertEqual(self.receipt().read_bytes(), before)

    def test_the_skill_says_where_the_receipt_comes_from(self):
        text = (_ti.GT / "skills" / "gt-doctor" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("post-install-validated.json", text)
        for writer in ("hook", "manual"):
            self.assertIn("`%s`" % writer, text)


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
        # 0.20.0: gt-lotr 0.3.0 runs on Windows too (named pipe), so the gate places and
        # smoke-tests it there exactly as on POSIX.
        rc, rows, _ = self.gate()
        self.assertEqual(rows["lotr"]["state"], "PASS", rows["lotr"])
        self.assertEqual(rows["smoke-lotr"]["state"], "PASS", rows["smoke-lotr"])
        # 0.20.0: the smoke starts the CONFIGURED command (the installed manifest's), so the
        # PASS above proves what Claude Code will run. On Windows that command is the
        # interpreter install.sh resolved, never `python3` (the Store stub); elsewhere it is
        # the manifest as shipped.
        self.assertIn("the configured MCP command", rows["smoke-lotr"]["summary"])
        cache = self.home / ".claude" / "plugins" / "cache" / "golden-thread-plugin" / "gt-lotr"
        for man in list(cache.glob("*/.claude-plugin/plugin.json")) + [
                self.home / ".claude" / "plugins" / "marketplaces" / "golden-thread-plugin"
                / "plugins" / "gt-lotr" / ".claude-plugin" / "plugin.json"]:
            cmd = json.loads(man.read_text(encoding="utf-8"))["mcpServers"]["gt-lotr"]["command"]
            if IS_WINDOWS:
                self.assertTrue(os.path.isabs(cmd) and os.path.isfile(cmd), (man, cmd))
                self.assertNotIn("windowsapps", cmd.lower())
            else:
                self.assertEqual(cmd, "python3", man)
        self.assertEqual(rc, 0)
        if IS_WINDOWS:
            return                      # no pgrep; the smoke terminates its lotrd itself
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
    write_job(job_path(self.home, "daily"),
              {"Label": "com.markethaven.gt-daily",
               "ProgramArguments": [PYTHON, "/elsewhere/gt_daily.py"]})


class InstallWithARealFailExitsNine(InstalledMachine):
    PRE_INSTALL = _bad_daily_job_before_install

    def test_a_real_fail_stops_the_install_with_exit_9(self):
        p = self.install_proc
        self.assertEqual(p.returncode, 9, p.stdout[-3000:])
        self.assertIn("FAIL    daily-job", p.stdout)
        self.assertIn("INSTALL FAILED VALIDATION", p.stdout)


if __name__ == "__main__":
    unittest.main()
