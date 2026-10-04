"""gt sandbox mode, independent re-review of 0.20.x (2026-10-04): findings M1 and M2.

M1  `gt_sandbox.py check` must treat every setting that loosens the sandbox as a problem, in user
    settings AND in the project files Claude Code loads (.claude/settings.json in the working
    directory; .claude/settings.local.json there and at the git root). Before the fix a user
    `excludedCommands` reported PASS, and a project's excludedCommands, ignoreViolations,
    unix-socket, enableWeakerNestedSandbox, additionalDirectories or re-opening allowRead
    entries were not reported at all. `gt_sandbox.py managed` prints the admin-required variant
    (managed settings / `claude --settings`), the only way to make the sandbox unloosenable.

M2  The file tools follow permission rules only, so gt must deny Edit on the files Claude Code,
    the login shell and launchd load code or configuration from (~/.claude.json, ~/.claude/
    {agents,skills,commands,hooks,...}, any .mcp.json, shell rc files, LaunchAgents), and set
    permissions.disableBypassPermissionsMode "disable" -- all taken out cleanly by `remove`.
"""
import json
import os
import subprocess
import unittest

from _harness import IS_WINDOWS, PYTHON, SCRIPTS
from test_gt_sandbox import InProcess

SANDBOX = SCRIPTS / "gt_sandbox.py"


class Base(InProcess):
    def setUp(self):
        super().setUp()
        self.gs.platform_mode = lambda: "macos"          # the OS-sandbox branch everywhere
        self.proj = self.tmp / "proj"
        (self.proj / ".claude").mkdir(parents=True)

    def project(self, name, d, where=None):
        p = (where or self.proj) / ".claude" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d), encoding="utf-8")
        return p

    def problems(self, **kw):
        c = self.gs.check(cwd=str(self.proj), managed=kw.get("managed", {}))
        return c, " | ".join(c["problems"])


class M1UserScope(Base):
    def test_user_excluded_commands_is_not_a_pass(self):
        self.write_settings({"sandbox": {"excludedCommands": ["docker *"]}})
        self.gs.apply()
        c, txt = self.problems()
        self.assertEqual(c["state"], "drift")
        self.assertIn("excludedCommands", txt)
        rows = {r["check"]: r for r in self.gs.verify_rows(
            cwd=str(self.proj), live=False,
            status={"enabled": True, "strictMode": True, "supported": True})}
        self.assertEqual(rows["sandbox-settings"]["state"], self.gs.FAIL,
                         "a user excludedCommands must not verify as PASS")

    def test_every_user_loosening_key_is_a_problem(self):
        self.gs.apply()
        base = self.settings()
        cases = {
            "ignoreViolations": {"ignoreViolations": {"*": ["/x"]}},
            "network.allowUnixSockets": {"network": {"allowUnixSockets": ["/var/run/docker.sock"]}},
            "network.allowAllUnixSockets": {"network": {"allowAllUnixSockets": True}},
            "enableWeakerNestedSandbox": {"enableWeakerNestedSandbox": True},
            "allowUnsandboxedCommands": {"allowUnsandboxedCommands": True},
        }
        for key, extra in cases.items():
            d = json.loads(json.dumps(base))
            for k, v in extra.items():
                if isinstance(v, dict) and isinstance(d["sandbox"].get(k), dict):
                    d["sandbox"][k].update(v)
                else:
                    d["sandbox"][k] = v
            self.write_settings(d)
            c, txt = self.problems()
            self.assertEqual(c["state"], "drift", key)
            self.assertIn(key, txt, key)

    def test_user_allow_read_reopening_a_denied_region_is_a_problem(self):
        self.gs.apply()
        d = self.settings()
        d["sandbox"]["filesystem"]["allowRead"] = [str(self.vault / "Knowledge")]
        self.write_settings(d)
        c, txt = self.problems()
        self.assertIn("allowRead", txt)
        # A wider allow does NOT re-open a narrower deny (docs table): not a problem.
        d["sandbox"]["filesystem"]["allowRead"] = [str(self.tmp / "elsewhere")]
        self.write_settings(d)
        c, txt = self.problems()
        self.assertEqual(c["state"], "ok", c)


class M1ProjectScope(Base):
    def setUp(self):
        super().setUp()
        self.gs.apply()
        self.assertEqual(self.problems()[0]["state"], "ok")

    def assertFlagged(self, name, d, needle, where=None):
        p = self.project(name, d, where)
        c, txt = self.problems()
        self.assertEqual(c["state"], "drift", "%s in %s not flagged" % (needle, name))
        self.assertIn(needle, txt)
        self.assertIn(str(p), txt)
        os.unlink(p)

    def test_each_loosening_key_in_shared_and_local_project_settings(self):
        cases = [
            ({"sandbox": {"excludedCommands": ["cat *"]}}, "excludedCommands"),
            ({"sandbox": {"ignoreViolations": {"*": ["/"]}}}, "ignoreViolations"),
            ({"sandbox": {"network": {"allowUnixSockets": ["/tmp/s"]}}}, "allowUnixSockets"),
            ({"sandbox": {"network": {"allowAllUnixSockets": True}}}, "allowAllUnixSockets"),
            ({"sandbox": {"enableWeakerNestedSandbox": True}}, "enableWeakerNestedSandbox"),
            ({"sandbox": {"enabled": False}}, "sandbox.enabled false"),
            ({"sandbox": {"allowUnsandboxedCommands": True}}, "allowUnsandboxedCommands"),
            ({"permissions": {"additionalDirectories": ["/opt/x"]}}, "additionalDirectories"),
            ({"sandbox": {"filesystem": {"allowRead": [str(self.vault)]}}}, "allowRead"),
            ({"sandbox": {"filesystem": {"allowWrite": [str(self.vault / "Knowledge")]}}},
             "allowWrite"),
            ({"sandbox": {"filesystem": {"allowRead": ["~/.claude/golden-thread/unlock"]}}},
             "allowRead"),
            ({"permissions": {"allow": ["Edit(%s/**)" % self.gs.rule_path(str(self.vault),
                                                                          False)]}},
             "permissions.allow"),
            ({"permissions": {"disableBypassPermissionsMode": "enable"}},
             "disableBypassPermissionsMode"),
        ]
        for name in ("settings.json", "settings.local.json"):
            for d, needle in cases:
                self.assertFlagged(name, d, needle)

    def test_local_settings_at_the_git_root_are_checked_from_a_subdirectory(self):
        (self.proj / ".git").mkdir()
        sub = self.proj / "src" / "pkg"
        sub.mkdir(parents=True)
        self.project("settings.local.json", {"sandbox": {"excludedCommands": ["sh *"]}})
        c = self.gs.check(cwd=str(sub), managed={})
        self.assertTrue(any("excludedCommands" in x for x in c["problems"]), c)

    def test_managed_admin_required_turns_project_loosening_into_notes(self):
        self.project("settings.json", {"sandbox": {"excludedCommands": ["cat *"]}})
        c = self.gs.check(cwd=str(self.proj),
                          managed={"sandbox": {"allowUnsandboxedCommands": False}})
        self.assertFalse(any("excludedCommands" in x for x in c["problems"]), c)
        self.assertTrue(any("excludedCommands" in n and "ignored" in n for n in c["notes"]), c)

    def test_without_admin_required_check_says_how_to_get_it(self):
        c, _ = self.problems()
        self.assertTrue(any("gt_sandbox.py managed" in n for n in c["notes"]), c["notes"])


class M1Managed(Base):
    def test_managed_snippet_is_admin_required_and_complete(self):
        snip = self.gs.managed_snippet()
        self.assertIs(snip["sandbox"]["allowUnsandboxedCommands"], False,
                      "admin-required needs the retry off in managed/--settings")
        self.assertIs(snip["sandbox"]["enabled"], True)
        self.assertIs(snip["sandbox"]["enableWeakerNestedSandbox"], False)
        self.assertIs(snip["sandbox"]["network"]["allowAllUnixSockets"], False)
        p = self.gs.plan()
        self.assertEqual(snip["sandbox"]["filesystem"]["denyRead"],
                         p["lists"]["sandbox.filesystem.denyRead"])
        self.assertEqual(snip["permissions"]["deny"], p["lists"]["permissions.deny"])
        self.assertEqual(snip["permissions"]["disableBypassPermissionsMode"], "disable")

    def test_cli_prints_snippet_and_settings_command_and_never_touches_settings_json(self):
        env = dict(os.environ)
        out_file = self.tmp / "req.json"
        r = subprocess.run([PYTHON, str(SANDBOX), "managed", "--out", str(out_file)],
                           capture_output=True, text=True, env=env, timeout=60,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("claude --settings %s" % out_file, r.stdout)
        self.assertIn("managed-settings.json", r.stdout)
        saved = json.loads(out_file.read_text())
        self.assertIn("disableBypassPermissionsMode", saved["permissions"])
        if not IS_WINDOWS:
            self.assertIs(saved["sandbox"]["allowUnsandboxedCommands"], False)
        self.assertFalse((self.home / ".claude" / "settings.json").exists(),
                         "`managed` must never write user settings")


class M2ConfigDenies(Base):
    def rules(self):
        return self.gs.plan()["lists"]["permissions.deny"]

    def test_edit_denies_cover_claude_config_mcp_rc_files_and_launch_agents(self):
        rules = self.rules()
        hm = self.gs.rule_path(str(self.home), False)
        want = ["Edit(%s/.claude.json)" % hm,
                "Edit(%s/.claude/agents/**)" % hm, "Edit(%s/.claude/skills/**)" % hm,
                "Edit(%s/.claude/commands/**)" % hm, "Edit(%s/.claude/hooks/**)" % hm,
                "Edit(%s/.claude/settings.local.json)" % hm, "Edit(%s/.claude/CLAUDE.md)" % hm,
                "Edit(//**/.mcp.json)", "Edit(//**/.claude/hooks/**)",
                "Edit(%s/.zshrc)" % hm, "Edit(%s/.bashrc)" % hm, "Edit(%s/.bash_profile)" % hm,
                "Edit(%s/.profile)" % hm, "Edit(%s/.zprofile)" % hm,
                "Edit(%s/Library/LaunchAgents/**)" % hm]
        for w in want:
            self.assertIn(w, rules)
        # The auto-memory folder stays writable by the file tools: never all of ~/.claude.
        self.assertNotIn("Edit(%s/.claude/**)" % hm, rules)
        for r in rules:
            self.assertRegex(r, r"^(Read|Edit)\(//", r)

    def test_disable_bypass_is_set_checked_and_restored(self):
        self.write_settings({"permissions": {"defaultMode": "acceptEdits"}, "model": "opus"})
        before = self.settings()
        self.gs.apply()
        d = self.settings()
        self.assertEqual(d["permissions"]["disableBypassPermissionsMode"], "disable")
        self.assertEqual(self.problems()[0]["state"], "ok")
        d["permissions"].pop("disableBypassPermissionsMode")
        self.write_settings(d)
        c, _ = self.problems()
        self.assertIn('permissions.disableBypassPermissionsMode "disable"', c["missing"])
        self.gs.apply()
        hm = self.gs.rule_path(str(self.home), False)
        d = self.settings()
        d["permissions"]["deny"].remove("Edit(%s/.claude.json)" % hm)
        self.write_settings(d)
        c, _ = self.problems()
        self.assertTrue(any(".claude.json" in m for m in c["missing"]), c["missing"])
        self.gs.apply()
        self.gs.remove()
        self.assertEqual(self.settings(), before, "remove takes out exactly what gt added")

    def test_a_users_own_disable_bypass_value_is_restored(self):
        self.write_settings({"permissions": {"disableBypassPermissionsMode": "x"}})
        self.gs.apply()
        self.assertEqual(self.settings()["permissions"]["disableBypassPermissionsMode"], "disable")
        self.gs.remove()
        self.assertEqual(self.settings(), {"permissions": {"disableBypassPermissionsMode": "x"}})

    def test_off_with_disable_bypass_left_is_stale(self):
        self.gs.apply()
        d = self.settings()
        # the lists taken out by hand, the scalar gt set left behind
        d["permissions"]["deny"] = []
        d.pop("sandbox", None)
        self.write_settings(d)
        self.config(vault_path=str(self.vault), sandbox_mode="off")
        c = self.gs.check()
        self.assertEqual(c["state"], "stale-off")
        self.assertTrue(any("disableBypassPermissionsMode" in x for x in c["stale"]))


if __name__ == "__main__":
    unittest.main()
