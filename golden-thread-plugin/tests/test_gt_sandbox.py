"""gt sandbox mode (0.20.0): gt_sandbox.py, the settings it merges into ~/.claude/settings.json,
the queue inbox, and the gt_settings / doctor / verify / surface wiring.

The load-bearing assertions are about BOUNDARIES of what gt writes:
  * every key it writes is one the Claude Code docs name (sandbox.enabled,
    allowUnsandboxedCommands, failIfUnavailable, filesystem.denyWrite/denyRead/allowWrite,
    permissions.deny Read(...)/Edit(...)), with the documented path syntax (`//abs` in rules,
    plain absolute paths in sandbox lists), and no Write(...)/NotebookEdit(...) path rule,
    which Claude Code ignores;
  * `remove` leaves settings.json exactly as it was before `apply` -- a user's own entries,
    including one identical to gt's, survive;
  * native Windows gets NO sandbox keys (failIfUnavailable there would stop Claude Code from
    starting) and the docs-mandated word "friction";
  * the inbox is untrusted: the broker re-validates, rejects, and never follows a symlink.
"""
import io
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

from _harness import (IS_WINDOWS, PYTHON, SCRIPTS, TOOLS, Sandbox, load_module, posix_only,
                      skip_on_windows, WIN_CHMOD_FAULT)

SANDBOX = SCRIPTS / "gt_sandbox.py"
SETTINGS = SCRIPTS / "gt_settings.py"
QUEUE = SCRIPTS / "gt_write_queue.py"
BROKER = SCRIPTS / "gt_broker.py"
DOCTOR = SCRIPTS / "gt_doctor.py"
SURFACE = SCRIPTS / "gt_surface.py"


class InProcess(Sandbox):
    """gt_sandbox imported into this process with HOME pointing at the sandbox home."""

    def setUp(self):
        super().setUp()
        self._env_saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "GT_VAULT",
                                                          "LOTR_HOME", "LOTR_STORE_DIR",
                                                          "CLAUDECODE")}
        os.environ["HOME"] = str(self.home)
        if IS_WINDOWS:
            os.environ["USERPROFILE"] = str(self.home)
        for k in ("GT_VAULT", "LOTR_HOME", "LOTR_STORE_DIR", "CLAUDECODE"):
            os.environ.pop(k, None)
        self.gs = load_module(SANDBOX, "gt_sandbox_under_test")
        self.vault = self.tmp / "vault"
        (self.vault / "Knowledge").mkdir(parents=True)
        (self.vault / "index.md").write_text("# Index\n", encoding="utf-8")
        self.config(vault_path=str(self.vault), sandbox_mode="on")

    def tearDown(self):
        for k, v in self._env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def settings(self):
        p = self.home / ".claude" / "settings.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def write_settings(self, d):
        (self.home / ".claude" / "settings.json").write_text(json.dumps(d, indent=2) + "\n",
                                                             encoding="utf-8")


class Plan(InProcess):
    def test_posix_plan_writes_only_documented_keys_with_documented_syntax(self):
        p = self.gs.plan(mode="macos")
        self.assertEqual(p["sandbox_bools"], {"enabled": True, "allowUnsandboxedCommands": False,
                                              "failIfUnavailable": True})
        dw = p["lists"]["sandbox.filesystem.denyWrite"]
        dr = p["lists"]["sandbox.filesystem.denyRead"]
        vault = os.path.abspath(str(self.vault)).replace("\\", "/")
        for must in (vault, str(self.home / ".claude" / "golden-thread").replace("\\", "/"),
                     str(self.home / ".claude" / "plugins").replace("\\", "/"),
                     str(self.home / ".claude" / "settings.json").replace("\\", "/"),
                     str(self.home / ".claude" / "vault-config.json").replace("\\", "/"),
                     str(self.home / ".config" / "gt-lotr").replace("\\", "/")):
            self.assertIn(must, dw)
        self.assertIn(str(self.home / ".claude" / "golden-thread" / "unlock").replace("\\", "/"), dr)
        self.assertIn(vault, dr, "sandbox_vault_reads defaults to deny")
        self.assertEqual(p["lists"]["sandbox.filesystem.allowWrite"],
                         [str(self.home / ".gt-inbox").replace("\\", "/")])
        for e in dw + dr:
            self.assertFalse(e.startswith("//"), "sandbox paths are plain absolute paths: %s" % e)
        rules = p["lists"]["permissions.deny"]
        self.assertTrue(rules)
        for r in rules:
            self.assertRegex(r, r"^(Read|Edit)\(//", "only Read/Edit rules, `//` absolute: %s" % r)
            self.assertNotRegex(r, r"^(Write|NotebookEdit|MultiEdit|Glob)\(",
                                "Claude Code never consults such a path rule")
        self.assertIn("Edit(%s/**)" % self.gs.rule_path(str(self.vault), False), rules)
        self.assertIn("Read(%s/**)" % self.gs.rule_path(str(self.vault), False), rules)

    def test_allowing_vault_reads_still_denies_locked_folders(self):
        sec = self.vault / "Secrets"
        sec.mkdir()
        (sec / ".gt-locked").write_text("locked\n")
        self.config(vault_path=str(self.vault), sandbox_mode="on", sandbox_vault_reads="allow")
        p = self.gs.plan(mode="linux")
        dr = p["lists"]["sandbox.filesystem.denyRead"]
        self.assertNotIn(os.path.abspath(str(self.vault)).replace("\\", "/"), dr)
        self.assertIn(os.path.abspath(str(sec)).replace("\\", "/"), dr)
        self.assertIn("Read(%s/**)" % self.gs.rule_path(str(sec), False),
                      p["lists"]["permissions.deny"])
        # writes stay denied whatever the read setting
        self.assertIn(os.path.abspath(str(self.vault)).replace("\\", "/"),
                      p["lists"]["sandbox.filesystem.denyWrite"])

    def test_native_windows_gets_permission_rules_only_and_says_friction(self):
        p = self.gs.plan(mode="windows")
        self.assertEqual(p["sandbox_bools"], {}, "no sandbox keys on native Windows: "
                         "failIfUnavailable would stop Claude Code from starting")
        for k in ("sandbox.filesystem.denyWrite", "sandbox.filesystem.denyRead",
                  "sandbox.filesystem.allowWrite"):
            self.assertEqual(p["lists"][k], [])
        self.assertTrue(p["lists"]["permissions.deny"])
        self.assertTrue(any("friction" in n for n in p["notes"]))
        self.assertIn("friction", self.gs.ENFORCED["windows"])

    def test_rule_path_forms(self):
        self.assertEqual(self.gs.rule_path("/Users/a/vault", True), "//Users/a/vault/**")
        self.assertEqual(self.gs.rule_path("/Users/a/settings.json", False),
                         "//Users/a/settings.json")
        self.assertEqual(self.gs.rule_path("C:\\Users\\a\\My Vault", True), "//c/Users/a/My Vault/**")
        self.assertEqual(self.gs.rule_path("/v/[2024] notes", True), "//v/\\[2024\\] notes/**")

    def test_no_vault_is_refused_not_guessed(self):
        self.config(sandbox_mode="on")
        with self.assertRaises(self.gs.SandboxError):
            self.gs.plan(mode="macos")


class ApplyRemove(InProcess):
    def setUp(self):
        super().setUp()
        self.gs.platform_mode = lambda: "macos"          # the OS-sandbox branch everywhere

    def test_apply_then_remove_restores_the_file_exactly(self):
        mine = "Edit(%s/**)" % self.gs.rule_path(str(self.vault), False)
        before = {"permissions": {"deny": ["Read(./.env)", mine], "allow": ["Bash(ls)"]},
                  "sandbox": {"enabled": False, "excludedCommands": ["docker *"]},
                  "hooks": {"SessionStart": []}, "model": "opus"}
        self.write_settings(before)
        rep = self.gs.apply()
        self.assertTrue(rep["changes"])
        d = self.settings()
        self.assertIs(d["sandbox"]["enabled"], True)
        self.assertIs(d["sandbox"]["allowUnsandboxedCommands"], False)
        self.assertIs(d["sandbox"]["failIfUnavailable"], True)
        self.assertEqual(d["permissions"]["deny"].count(mine), 1, "an existing entry is not doubled")
        self.assertEqual(d["permissions"]["allow"], ["Bash(ls)"])
        self.assertEqual(self.gs.apply()["changes"], [], "apply is idempotent")
        self.assertEqual(self.gs.check()["state"], "ok")
        self.gs.remove()
        self.assertEqual(self.settings(), before, "remove must leave exactly what was there, "
                         "including the user's own copy of an entry gt also wants")
        self.assertFalse((self.home / ".claude" / "golden-thread" / "sandbox" / "state.json").exists())

    def test_remove_leaves_a_boolean_the_user_changed_since(self):
        self.gs.apply()
        d = self.settings()
        d["sandbox"]["failIfUnavailable"] = False
        self.write_settings(d)
        rep = self.gs.remove()
        self.assertIs(self.settings()["sandbox"]["failIfUnavailable"], False)
        self.assertTrue(any("changed since" in n for n in rep["notes"]))

    def test_a_backup_is_taken_before_settings_change(self):
        self.write_settings({"model": "opus"})
        self.gs.apply()
        backups = list((self.home / ".claude" / "golden-thread" / "backups").glob("settings.json.*"))
        self.assertTrue(backups)
        self.assertEqual(json.loads(backups[0].read_text()), {"model": "opus"})

    def test_drift_is_detected_and_repaired(self):
        self.gs.apply()
        d = self.settings()
        d["sandbox"]["filesystem"]["denyWrite"].pop(0)
        d["permissions"]["deny"].pop(0)
        self.write_settings(d)
        c = self.gs.check()
        self.assertEqual(c["state"], "drift")
        self.assertEqual(len(c["missing"]), 2)
        self.gs.apply()
        self.assertEqual(self.gs.check()["state"], "ok")

    def test_a_moved_vault_takes_gts_old_entries_out(self):
        self.gs.apply()
        old = os.path.abspath(str(self.vault)).replace("\\", "/")
        v2 = self.tmp / "vault2"
        v2.mkdir()
        self.config(vault_path=str(v2), sandbox_mode="on")
        self.assertIn("sandbox.filesystem.denyWrite %s" % old, self.gs.check()["stale"])
        self.gs.apply()
        d = self.settings()
        self.assertNotIn(old, d["sandbox"]["filesystem"]["denyWrite"])
        self.assertIn(os.path.abspath(str(v2)).replace("\\", "/"),
                      d["sandbox"]["filesystem"]["denyWrite"])

    def test_managed_and_project_overrides_are_reported_never_written(self):
        self.gs.apply()
        c = self.gs.check(managed={"enabled": False})
        self.assertEqual(c["state"], "drift")
        self.assertTrue(any("managed settings" in p for p in c["problems"]))
        proj = self.tmp / "proj"
        (proj / ".claude").mkdir(parents=True)
        (proj / ".claude" / "settings.local.json").write_text(
            json.dumps({"sandbox": {"enabled": False}}))
        c = self.gs.check(cwd=str(proj), managed={})
        self.assertTrue(any("settings.local.json" in p for p in c["problems"]))

    def test_user_filesystem_disabled_refuses_apply(self):
        self.write_settings({"sandbox": {"filesystem": {"disabled": True}}})
        with self.assertRaises(self.gs.SandboxError):
            self.gs.apply()

    def test_linux_without_bubblewrap_is_refused_unless_forced(self):
        self.gs.platform_mode = lambda: "linux"
        self.gs.linux_deps_missing = lambda: ["bwrap", "socat"]
        with self.assertRaises(self.gs.SandboxError) as cm:
            self.gs.apply()
        self.assertIn("refuse to start", str(cm.exception))
        self.assertFalse((self.home / ".claude" / "settings.json").exists())
        self.gs.apply(force=True)
        self.assertIs(self.settings()["sandbox"]["failIfUnavailable"], True)

    def test_windows_apply_writes_no_sandbox_key(self):
        self.gs.platform_mode = lambda: "windows"
        self.gs.apply()
        d = self.settings()
        self.assertNotIn("sandbox", d)
        self.assertTrue(d["permissions"]["deny"])
        rows = {r["check"]: r for r in self.gs.verify_rows(live=False)}
        self.assertEqual(rows["sandbox-mode"]["state"], self.gs.NC)
        self.assertIn("friction", rows["sandbox-mode"]["why"])

    def test_verify_rows_on_a_sandbox_platform(self):
        self.gs.apply()
        rows = {r["check"]: r for r in self.gs.verify_rows(
            live=False, status={"enabled": True, "strictMode": True, "supported": True,
                                "enabledSource": "settings", "filesystemPolicy": "strict"})}
        self.assertEqual(rows["sandbox-mode"]["state"], self.gs.PASS)
        self.assertEqual(rows["sandbox-settings"]["state"], self.gs.PASS)
        self.assertEqual(rows["claude-sandbox"]["state"], self.gs.PASS)
        self.assertEqual(rows["sandbox-live"]["state"], self.gs.NC)
        rows = {r["check"]: r for r in self.gs.verify_rows(
            live=False, status={"enabled": False, "strictMode": False})}
        self.assertEqual(rows["claude-sandbox"]["state"], self.gs.FAIL)

    def test_off_with_leftovers_is_a_failure(self):
        self.gs.apply()
        self.config(vault_path=str(self.vault), sandbox_mode="off")
        self.assertEqual(self.gs.check()["state"], "stale-off")
        rows = self.gs.verify_rows(live=False)
        self.assertEqual(rows[0]["state"], self.gs.FAIL)


class SettingsSwitch(Sandbox):
    """gt_settings.py set sandbox_mode on|off drives gt_sandbox and records the value last."""

    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.config(vault_path=str(self.vault))

    def test_registered_with_docs_and_gated_as_a_security_key(self):
        m = load_module(SETTINGS, "gt_settings_sandbox")
        for k, default in (("sandbox_mode", "off"), ("sandbox_vault_reads", "deny"),
                           ("vault_mcp", "auto")):
            self.assertEqual(m.SETTINGS[k]["default"], default)
            self.assertGreater(len(m.SETTINGS[k]["detail"]), 200)
        self.assertIn("sandbox_mode", m.SECURITY_KEYS)
        self.assertIn("sandbox_vault_reads", m.SECURITY_KEYS)
        self.assertIn("friction", m.SETTINGS["sandbox_mode"]["detail"])

    def test_on_then_off_round_trip(self):
        if sys.platform.startswith("linux") and not (shutil.which("bwrap") and shutil.which("socat")):
            p = self.py(SETTINGS, "set", "sandbox_mode", "on")
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("bubblewrap", p.stdout)
            cfg = json.loads((self.home / ".claude" / "vault-config.json").read_text())
            self.assertNotIn("sandbox_mode", cfg, "a refused switch records nothing")
            return
        p = self.py(SETTINGS, "set", "sandbox_mode", "on")
        self.assertOk(p)
        self.assertIn("Restart Claude Code", p.stdout)
        d = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertTrue(d["permissions"]["deny"])
        if IS_WINDOWS:
            self.assertNotIn("sandbox", d)
        else:
            self.assertIs(d["sandbox"]["enabled"], True)
        cfg = json.loads((self.home / ".claude" / "vault-config.json").read_text())
        self.assertEqual(cfg["sandbox_mode"], "on")
        p = self.py(SETTINGS, "set", "sandbox_mode", "off")
        self.assertOk(p)
        self.assertEqual(json.loads((self.home / ".claude" / "settings.json").read_text()), {})


class WriteQueueInbox(InProcess):
    def setUp(self):
        super().setUp()
        self.wq = load_module(QUEUE, "gt_write_queue_sandbox")
        (self.vault / "Projects" / "golden-thread").mkdir(parents=True)

    def _refuse_queue(self, *_a, **_k):
        raise PermissionError(1, "Operation not permitted")

    def test_a_refused_queue_folder_falls_back_to_the_inbox_only_in_sandbox_mode(self):
        self.wq._deposit_queue = self._refuse_queue
        req = self.wq.build(self.vault, "Knowledge/A.md", "create", "# A\n", None, "s1",
                            "session", None)
        dest = self.wq.deposit(self.vault, req)
        self.assertTrue(self.wq.in_inbox(dest))
        body = json.loads(Path(dest).read_text(encoding="utf-8"))
        self.assertEqual(body["gt_inbox"], 1)
        self.assertEqual(body["vault"], os.path.realpath(str(self.vault)))
        self.assertEqual(body["request"]["path"], "Knowledge/A.md")
        results, note = self.wq.submit(self.vault, [{"path": "Knowledge/B.md", "op": "create",
                                                     "content": "# B\n"}])
        self.assertEqual(results[0]["decision"], "queued")
        self.assertIn("sandbox inbox", note)
        self.config(vault_path=str(self.vault), sandbox_mode="off")
        req = self.wq.build(self.vault, "Knowledge/C.md", "create", "# C\n", None, "s1",
                            "session", None)
        with self.assertRaises(PermissionError):
            self.wq.deposit(self.vault, req)


class BrokerInbox(Sandbox):
    """The broker treats every inbox file as data from an untrusted writer."""

    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        gt = self.vault / "Projects" / "golden-thread"
        (gt / "tools").mkdir(parents=True)
        (gt / "sessions").mkdir()
        for tool in ("gt_task.py", "gt_tasks.py", "gt_session.py"):
            shutil.copy(TOOLS / tool, gt / "tools" / tool)
        (self.vault / "Knowledge").mkdir()
        self.inbox = self.home / ".gt-inbox" / "queue"
        self.inbox.mkdir(parents=True)
        self.config(vault_path=str(self.vault), sandbox_mode="on")

    def inbox_req(self, name, path="Knowledge/New.md", op="create", content="# New\n",
                  vault=None, **over):
        req = {"schema": 1, "id": name, "submitted": "2026-10-03T20:00:00.000000+00:00",
               "session": "s-inbox", "origin": "session", "path": path, "op": op,
               "section": None, "content": content, "key": None, "target_existed": False,
               "base_sha256": None, "hint": None}
        req.update(over)
        body = {"gt_inbox": 1, "vault": os.path.realpath(str(vault or self.vault)), "request": req}
        (self.inbox / (name + ".json")).write_text(json.dumps(body), encoding="utf-8")
        return self.inbox / (name + ".json")

    def drain(self):
        return self.py(BROKER, "drain", "--vault", self.vault, "--json")

    def test_a_valid_request_is_applied_and_leaves_the_inbox(self):
        f = self.inbox_req("r1")
        p = self.drain()
        rows = json.loads(p.stdout)["results"]
        self.assertEqual([r["decision"] for r in rows], ["apply"], p.stdout + p.stderr)
        self.assertEqual((self.vault / "Knowledge" / "New.md").read_text(), "# New\n")
        self.assertFalse(f.exists())

    def test_another_vaults_request_is_left_alone(self):
        other = self.tmp / "other"
        other.mkdir()
        f = self.inbox_req("r2", vault=other)
        self.drain()
        self.assertTrue(f.exists())
        self.assertFalse((self.vault / "Knowledge" / "New.md").exists())

    def test_disallowed_paths_are_rejected_into_the_vault_rejected_folder(self):
        for i, path in enumerate(("log.md", "core-rules/x.md", "../escape.md", "Sources/a.md")):
            self.inbox_req("bad%d" % i, path=path)
        p = self.drain()
        rows = json.loads(p.stdout)["results"]
        self.assertEqual({r["decision"] for r in rows}, {"reject"}, p.stdout)
        rej = self.vault / "Projects" / "golden-thread" / "spool" / "broker" / "rejected"
        self.assertEqual(len(list(rej.glob("inbox-bad*.json"))), 4)
        self.assertEqual(list(self.inbox.glob("*.json")), [])
        self.assertFalse((self.tmp / "escape.md").exists())

    def test_garbage_is_rejected_not_parsed_as_a_request(self):
        (self.inbox / "junk.json").write_text("{not json", encoding="utf-8")
        (self.inbox / "shape.json").write_text(json.dumps({"path": "Knowledge/X.md"}))
        rows = json.loads(self.drain().stdout)["results"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["decision"] == "reject" for r in rows))

    @skip_on_windows("creating a symlink needs a privilege a standard Windows account lacks")
    def test_a_symlink_is_never_followed(self):
        target = self.tmp / "secret.json"
        target.write_text(json.dumps({"gt_inbox": 1, "vault": str(self.vault), "request": {}}))
        os.symlink(str(target), str(self.inbox / "link.json"))
        rows = json.loads(self.drain().stdout)["results"]
        self.assertIn("symlink", rows[0]["reason"])
        self.assertFalse((self.inbox / "link.json").exists(), "the link is removed")
        self.assertTrue(target.exists(), "its target is untouched")

    @skip_on_windows("creating a symlink needs a privilege a standard Windows account lacks")
    def test_a_symlinked_inbox_folder_is_not_read(self):
        """A sandboxed process may replace ~/.gt-inbox/queue with a link to the vault's own
        queue; the broker must not then 'reject' the legitimate requests it finds there."""
        qdir = self.vault / "Projects" / "golden-thread" / "spool" / "queue"
        qdir.mkdir(parents=True)
        self.assertOk(self.py(QUEUE, "--vault", self.vault, "--path", "Knowledge/Q.md", "--op",
                              "create", "--content", "# Q\n", "--session", "s"))
        self.inbox.rmdir()
        os.symlink(str(qdir), str(self.inbox))
        rows = json.loads(self.drain().stdout)["results"]
        self.assertEqual([r["decision"] for r in rows], ["apply"], rows)
        rej = self.vault / "Projects" / "golden-thread" / "spool" / "broker" / "rejected"
        self.assertFalse(rej.exists() and list(rej.iterdir()))

    def test_an_oversized_file_is_reported_and_left(self):
        big = self.inbox / "big.json"
        big.write_bytes(b" " * (2 * 256 * 1024 + 10))
        p = self.py(BROKER, "status", "--vault", self.vault, "--json")
        self.assertEqual(json.loads(p.stdout)["inbox"], 1)
        self.drain()
        self.assertTrue(big.exists())

    def test_a_design_write_from_the_inbox_still_escalates(self):
        (self.vault / "Projects" / "alpha").mkdir(parents=True)
        (self.vault / "Projects" / "alpha" / "README.md").write_text("# alpha\n\n## Tasks\n")
        (self.vault / "Projects" / "alpha" / "design.md").write_text("# d\n\n## A\n\nx\n")
        self.inbox_req("d1", path="Projects/alpha/design.md", op="append", content="more\n",
                       section="A")
        rows = json.loads(self.drain().stdout)["results"]
        self.assertEqual(rows[-1]["decision"], "escalate", rows)


class DoctorRow(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def row(self):
        p = self.py(DOCTOR, "--only", "sandbox", "--json")
        self.assertNotIn("Traceback", p.stderr)
        return next(r for r in json.loads(p.stdout)["checks"] if r["check"] == "sandbox")

    def test_off_is_a_note(self):
        self.config(vault_path=str(self.vault))
        r = self.row()
        self.assertEqual(r["state"], "note")
        self.assertIn("sandbox mode: off", r["summary"])

    def test_on_and_applied_then_drifted(self):
        self.config(vault_path=str(self.vault), sandbox_mode="on")
        self.assertOk(self.py(SANDBOX, "apply", "--force"))
        r = self.row()
        if IS_WINDOWS:
            self.assertEqual(r["state"], "warn")
            self.assertIn("friction", r["summary"])
        else:
            self.assertEqual(r["state"], "ok", r)
        d = json.loads((self.home / ".claude" / "settings.json").read_text())
        d["permissions"]["deny"] = []
        (self.home / ".claude" / "settings.json").write_text(json.dumps(d))
        r = self.row()
        self.assertEqual(r["state"], "fail")
        self.assertIn("drifted", r["summary"])


class Surface(Sandbox):
    def test_session_start_says_sandbox_mode_and_counts_the_inbox(self):
        vault = self.tmp / "vault"
        vault.mkdir()
        self.config(vault_path=str(vault), sandbox_mode="on")
        inbox = self.home / ".gt-inbox" / "queue"
        inbox.mkdir(parents=True)
        (inbox / "a.json").write_text("{}")
        p = self.py(SURFACE, "check", "--hook", input=json.dumps({"source": "startup"}))
        self.assertOk(p)
        out = json.loads(p.stdout)
        self.assertIn("SANDBOX MODE: on", out["systemMessage"])
        self.assertIn("SANDBOX INBOX: 1", out["systemMessage"])
        self.assertIn("vault_queue_write", out["hookSpecificOutput"]["additionalContext"])

    def test_nothing_is_said_when_off(self):
        vault = self.tmp / "vault"
        vault.mkdir()
        self.config(vault_path=str(vault))
        p = self.py(SURFACE, "check", "--hook", input=json.dumps({"source": "startup"}))
        self.assertNotIn("SANDBOX", p.stdout)


@unittest.skipUnless(os.environ.get("GT_SRT"), "live sandbox proof: set GT_SRT to the path of "
                     "@anthropic-ai/sandbox-runtime's srt (the runtime Claude Code builds on)")
class LiveSrt(InProcess):
    """With GT_SRT set, run real commands under the sandbox runtime with the filesystem lists
    gt generates, plus what Claude Code allows by default (the working directory -- here the
    vault itself, the worst case -- and the temp dir). Proves the OS refuses the writes."""

    def test_a_sandboxed_command_cannot_write_or_read_the_vault_but_can_use_the_inbox(self):
        import subprocess
        (self.vault / "Knowledge" / "Page.md").write_text("vault text\n")
        p = self.gs.plan()
        inbox = self.home / ".gt-inbox"
        inbox.mkdir()
        cfg = {"network": {"allowedDomains": [], "deniedDomains": []},
               "filesystem": {"denyRead": p["lists"]["sandbox.filesystem.denyRead"],
                              "allowWrite": [str(self.vault), str(self.tmp / "t")]
                              + p["lists"]["sandbox.filesystem.allowWrite"],
                              "denyWrite": p["lists"]["sandbox.filesystem.denyWrite"]}}
        (self.tmp / "t").mkdir()
        f = self.tmp / "srt.json"
        f.write_text(json.dumps(cfg))

        def run(cmd):
            return subprocess.run([os.environ["GT_SRT"], "--settings", str(f), "sh", "-c", cmd],
                                  capture_output=True, text=True, timeout=120,
                                  cwd=str(self.vault))
        r = run("echo x > '%s'" % (self.vault / "Knowledge" / "Evil.md"))
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.vault / "Knowledge" / "Evil.md").exists())
        r = run("cat '%s'" % (self.vault / "Knowledge" / "Page.md"))
        self.assertNotIn("vault text", r.stdout)
        r = run("echo y > '%s'" % (inbox / "ok.txt"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((inbox / "ok.txt").exists())


if __name__ == "__main__":
    unittest.main()
