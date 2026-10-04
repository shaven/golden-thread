"""gt_mcp_inventory.py (0.20.1): every MCP server Claude Code may load, GATED or UNGATED, and the
doctor and `gt_unlock.py verify` rows built on it.

The load-bearing assertions:
  * each source kind is found from a fixture: user and local scope in ~/.claude.json, .mcp.json
    in the folder and a parent, enabled plugins (plugin.json inline / path / list, .mcp.json),
    claude.ai connectors, Claude in Chrome, managed-mcp.json -- and a disabled plugin is not;
  * GATED needs gt's own script, not just the name: a server called gt-lotr that runs something
    else is UNGATED;
  * NO credential VALUE ever reaches the output, text or JSON -- headers, env, args, URL user
    info, query and path, headersHelper -- only the KIND of place it lives;
  * Windows folder keys and managed paths are handled as Windows paths on any host;
  * the verify row is NOT-CHECKED, never PASS, while any ungated server exists, and the doctor
    row is never "ok" then.
"""
import json
import os
import secrets
import shutil
import sys
import unittest
from pathlib import Path

from _harness import IS_WINDOWS, PYTHON, SCRIPTS, Sandbox, load_module

INV = SCRIPTS / "gt_mcp_inventory.py"
DOCTOR = SCRIPTS / "gt_doctor.py"
VERIFY = SCRIPTS / "gt_unlock_verify.py"


def gt_plugin_servers(root):
    """gt's and gt-lotr's real plugin manifests, as an installed cache holds them."""
    for name, script in (("gt", "gt_vault_mcp.py"), ("gt-lotr", "lotr_mcp.py")):
        d = root / name / ".claude-plugin"
        d.mkdir(parents=True)
        server = "gt-vault" if name == "gt" else "gt-lotr"
        (d / "plugin.json").write_text(json.dumps({
            "name": name, "mcpServers": {server: {
                "command": "python3", "args": ["-I", "${CLAUDE_PLUGIN_ROOT}/scripts/" + script]}}}),
            encoding="utf-8")


class Fixture(Sandbox):
    def setUp(self):
        super().setUp()
        self.managed = self.tmp / "managed"
        self.managed.mkdir()
        self.work = self.tmp / "work" / "repo" / "sub"
        self.work.mkdir(parents=True)
        self.cache = self.home / ".claude" / "plugins" / "cache"
        self.cache.mkdir(parents=True)
        self.claude_json = {"mcpServers": {}, "projects": {}}
        self.settings = {"enabledPlugins": {}}
        self.installed = {"version": 2, "plugins": {}}

    def plugin(self, key, root, enabled=True):
        self.installed["plugins"][key] = [{"installPath": str(root), "scope": "user"}]
        self.settings["enabledPlugins"][key] = enabled

    def write(self):
        (self.home / ".claude.json").write_text(json.dumps(self.claude_json), encoding="utf-8")
        (self.home / ".claude" / "settings.json").write_text(json.dumps(self.settings),
                                                             encoding="utf-8")
        (self.home / ".claude" / "plugins" / "installed_plugins.json").write_text(
            json.dumps(self.installed), encoding="utf-8")

    def inventory(self, *extra, text=False):
        self.write()
        args = ["--home", self.home, "--cwd", self.work, "--managed-dir", self.managed]
        p = self.py(INV, *args, *extra, *([] if text else ["--json"]))
        self.assertOk(p)
        return p.stdout if text else json.loads(p.stdout)

    @staticmethod
    def by_name(inv):
        return {s["name"]: s for s in inv["servers"]}


class Sources(Fixture):
    def test_every_source_kind_is_found_and_classified(self):
        gt_plugin_servers(self.cache)
        self.plugin("gt@golden-thread-plugin", self.cache / "gt")
        self.plugin("gt-lotr@golden-thread-plugin", self.cache / "gt-lotr")
        # a third-party plugin with an inline server, one with a path, one with a list, one
        # with .mcp.json, and one installed but disabled
        for name, spec in (("inl", {"inline-srv": {"command": "node", "args": ["s.js"]}}),
                           ("pth", "./servers.json"),
                           ("lst", [{"list-a": {"url": "https://a.example.com/mcp",
                                                "type": "http"}}, "./more.json"]),
                           ("mcpj", None), ("off", {"off-srv": {"command": "x"}})):
            root = self.cache / name
            (root / ".claude-plugin").mkdir(parents=True)
            pj = {"name": name}
            if spec is not None:
                pj["mcpServers"] = spec
            (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(pj), encoding="utf-8")
            self.plugin("%s@m" % name, root, enabled=(name != "off"))
        (self.cache / "pth" / "servers.json").write_text(json.dumps(
            {"mcpServers": {"path-srv": {"command": "uvx", "args": ["thing"]}}}), encoding="utf-8")
        (self.cache / "lst" / "more.json").write_text(json.dumps(
            {"list-b": {"command": "/usr/local/bin/b"}}), encoding="utf-8")
        (self.cache / "mcpj" / ".mcp.json").write_text(json.dumps(
            {"mcpServers": {"dotmcp-srv": {"type": "sse", "url": "https://s.example.org/sse"}}}),
            encoding="utf-8")
        self.claude_json["mcpServers"]["user-srv"] = {"command": "npx", "args": ["-y", "pkg"]}
        self.claude_json["projects"][str(self.work)] = {
            "mcpServers": {"local-here": {"command": "local"}},
            "enabledMcpjsonServers": ["proj-approved"],
            "disabledMcpServers": ["claude.ai Google Drive"]}
        self.claude_json["projects"]["/somewhere/else"] = {
            "mcpServers": {"local-elsewhere": {"command": "x"}}}
        self.claude_json["claudeAiMcpEverConnected"] = ["claude.ai Google Drive", "claude.ai Gmail"]
        self.claude_json["claudeInChromeDefaultEnabled"] = True
        (self.tmp / "work" / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "proj-approved": {"command": "a"}, "proj-pending": {"type": "http",
                                                                "url": "https://p.example.net"}}}),
            encoding="utf-8")
        (self.managed / "managed-mcp.json").write_text(json.dumps(
            {"mcpServers": {"corp-srv": {"type": "http", "url": "https://corp.example.com/mcp"}}}),
            encoding="utf-8")

        inv = self.inventory()
        s = self.by_name(inv)
        self.assertTrue(s["plugin:gt:gt-vault"]["gated"])
        self.assertTrue(s["plugin:gt-lotr:gt-lotr"]["gated"])
        expect = {"user-srv": "user", "local-here": "local", "local-elsewhere": "local",
                  "proj-approved": "project", "proj-pending": "project",
                  "plugin:inl:inline-srv": "plugin", "plugin:pth:path-srv": "plugin",
                  "plugin:lst:list-a": "plugin", "plugin:lst:list-b": "plugin",
                  "plugin:mcpj:dotmcp-srv": "plugin", "corp-srv": "managed",
                  "claude.ai Google Drive": "claude.ai", "claude.ai Gmail": "claude.ai",
                  "claude-in-chrome": "built-in"}
        for name, source in expect.items():
            self.assertIn(name, s, "%s not found" % name)
            self.assertEqual(s[name]["source"], source, name)
            self.assertFalse(s[name]["gated"], name)
        self.assertNotIn("plugin:off:off-srv", s, "a disabled plugin's server is not loaded")
        self.assertEqual(s["local-here"]["state"], "enabled")
        self.assertTrue(s["local-here"]["here"])
        self.assertEqual(s["local-elsewhere"]["state"], "other-folder")
        self.assertFalse(s["local-elsewhere"]["here"])
        self.assertEqual(s["proj-approved"]["state"], "enabled")
        self.assertEqual(s["proj-pending"]["state"], "needs-approval")
        self.assertEqual(s["claude.ai Google Drive"]["state"], "disabled-here")
        self.assertEqual(s["claude.ai Gmail"]["credentials"], ["server-side"])
        self.assertEqual(s["claude-in-chrome"]["credentials"], ["browser-profile"])
        self.assertEqual(s["plugin:lst:list-a"]["transport"], "http")
        self.assertEqual(s["plugin:lst:list-a"]["credentials"], ["oauth-store"])
        self.assertEqual(s["plugin:mcpj:dotmcp-srv"]["transport"], "sse")
        self.assertEqual(s["user-srv"]["credentials"], ["none"])
        self.assertEqual(s["plugin:lst:list-b"]["endpoint"], "b")
        self.assertEqual(inv["summary"]["gated"], 2)
        self.assertEqual(inv["summary"]["ungated"], len(expect))
        self.assertEqual(inv["problems"], [])

    def test_a_server_named_like_gts_is_ungated_unless_it_runs_gts_script(self):
        self.claude_json["mcpServers"] = {
            "gt-lotr": {"command": "npx", "args": ["-y", "not-lotr"]},
            "gt-vault": {"command": "python3", "args": ["/opt/gt/scripts/gt_vault_mcp.py"]}}
        s = self.by_name(self.inventory())
        self.assertFalse(s["gt-lotr"]["gated"])
        self.assertIn("does not run lotr_mcp.py", s["gt-lotr"]["note"])
        self.assertTrue(s["gt-vault"]["gated"], "gt's script in user scope is still gt's server")

    def test_policy_denied_servers_are_not_counted_and_managed_only_is_reported(self):
        self.claude_json["mcpServers"] = {"bad": {"command": "bad"},
                                          "web": {"type": "http", "url": "https://w.example.com/x"},
                                          "other": {"command": "o"}}
        (self.managed / "managed-settings.json").write_text(json.dumps({
            "allowManagedMcpServersOnly": True,
            "deniedMcpServers": [{"serverName": "bad"}, {"serverUrl": "https://w.example.com/*"}]}),
            encoding="utf-8")
        inv = self.inventory()
        s = self.by_name(inv)
        self.assertEqual(s["bad"]["state"], "denied-by-policy")
        self.assertEqual(s["web"]["state"], "denied-by-policy")
        self.assertEqual(s["other"]["state"], "not-allowed-by-policy")
        self.assertEqual(inv["summary"]["ungated"], 1)
        self.assertTrue(inv["policy"]["allowManagedMcpServersOnly"])
        self.assertEqual(inv["policy"]["deniedMcpServers"], 2)

    def test_unreadable_config_is_a_problem_by_path_not_a_crash(self):
        self.write()
        (self.home / ".claude.json").write_text("{not json", encoding="utf-8")
        p = self.py(INV, "--home", self.home, "--cwd", self.work, "--managed-dir", self.managed,
                    "--json")
        self.assertOk(p)
        inv = json.loads(p.stdout)
        self.assertEqual(len(inv["problems"]), 1)
        self.assertIn(".claude.json", inv["problems"][0])
        self.assertNotIn("not json", p.stdout)

    def test_empty_home_lists_nothing(self):
        inv = self.inventory()
        self.assertEqual(inv["servers"], [])
        self.assertEqual(inv["summary"]["ungated"], 0)


class NoValueLeaks(Fixture):
    """Secret-shaped values in every field a config can carry: none may be printed."""

    def test_no_header_env_arg_or_url_value_reaches_any_output(self):
        # Random per run, so a leak cannot hide behind a value that happens to be printed
        # elsewhere, and nothing secret-shaped is stored in this file.
        vals = {k: pfx + secrets.token_hex(20) for k, pfx in (
            ("hdr", "Bearer ghp_"), ("hdr2", "sk-ant-api03-"), ("env", "xoxb-"),
            ("env2", "AKIA"), ("arg", "glpat-"), ("user", "u"), ("pw", "p"), ("q", "k"),
            ("path", "zap"), ("helper", "h"), ("argeq", "tok"))}
        self.claude_json["mcpServers"] = {
            "leaky-http": {"type": "http",
                           "url": "https://%s:%s@api.example.com/hooks/%s?api_key=%s" % (
                               vals["user"], vals["pw"], vals["path"], vals["q"]),
                           "headers": {"Authorization": vals["hdr"], "X-Api-Key": vals["hdr2"]},
                           "headersHelper": "/usr/bin/printf %s" % vals["helper"]},
            "leaky-stdio": {"command": "server",
                            "args": ["--token", vals["arg"], "--api-key=" + vals["argeq"]],
                            "env": {"SLACK_TOKEN": vals["env"], "AWS_KEY": vals["env2"],
                                    "REF_ONLY": "${SOME_VAR}"}}}
        self.claude_json["projects"][str(self.work)] = {"mcpServers": {
            "leaky-local": {"command": "c", "env": {"K": vals["env"]}}}}
        (self.tmp / "work" / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "leaky-proj": {"type": "sse", "url": "https://x.example.com/sse?token=" + vals["q"],
                           "headers": {"Authorization": vals["hdr"]}}}}), encoding="utf-8")
        outputs = [self.inventory(text=True), json.dumps(self.inventory())]
        for out in outputs:
            for k, v in vals.items():
                self.assertNotIn(v, out, "the %s value leaked into the output" % k)
                self.assertNotIn(v[-12:], out, "a fragment of the %s value leaked" % k)
        s = self.by_name(self.inventory())
        self.assertEqual(s["leaky-http"]["endpoint"], "https://api.example.com")
        self.assertEqual(set(s["leaky-http"]["credentials"]),
                         {"headers-in-config", "headers-helper", "url-in-config"})
        self.assertTrue(s["leaky-http"]["plaintext"])
        self.assertEqual(set(s["leaky-stdio"]["credentials"]), {"env-in-config", "args-in-config"})
        self.assertTrue(s["leaky-stdio"]["plaintext"])

    def test_a_var_reference_is_not_plaintext(self):
        self.claude_json["mcpServers"] = {"ref": {"type": "http", "url": "https://r.example.com",
                                                  "headers": {"Authorization": "${GH_TOKEN}"}}}
        s = self.by_name(self.inventory())["ref"]
        self.assertEqual(s["credentials"], ["headers-in-config"])
        self.assertFalse(s["plaintext"])

    def test_doctor_and_verify_rows_do_not_leak_either(self):
        v = "ghp_" + secrets.token_hex(20)
        self.claude_json["mcpServers"] = {"leaky": {"command": "s", "env": {"T": v},
                                                    "headers": {"A": v}}}
        self.write()
        d = self.py(DOCTOR, "--only", "mcp", "--json", cwd=self.work)
        r = self.py(VERIFY, "--json", cwd=self.work)
        for p in (d, r):
            self.assertNotIn(v, p.stdout + p.stderr)


class WindowsPaths(unittest.TestCase):
    """Windows folder keys and managed paths, checked as pure path logic on any host."""

    def setUp(self):
        self.m = load_module(INV, "gt_mcp_inventory_win")

    def test_folder_keys_match_across_case_and_slashes(self):
        k = self.m.path_key
        self.assertEqual(k("C:\\Users\\Me\\Proj"), k("c:/users/me/proj/"))
        self.assertEqual(k("C:\\Users\\Me\\Proj"), "c:/users/me/proj")
        self.assertEqual(k("C:\\"), "c:/")
        self.assertNotEqual(k("/Users/Me/Proj"), k("/users/me/proj"),
                            "POSIX paths stay case-sensitive")

    def test_ancestors_of_a_windows_folder(self):
        self.assertEqual(self.m.ancestors("C:\\Users\\Me\\Proj"),
                         ["c:/users/me/proj", "c:/users/me", "c:/users", "c:/"])
        self.assertEqual(self.m.ancestors("/a/b"), ["/a/b", "/a", "/"])

    def test_managed_paths_per_os(self):
        self.assertEqual(self.m.managed_dir("win32"), r"C:\Program Files\ClaudeCode")
        self.assertEqual(self.m.managed_dir("darwin"), "/Library/Application Support/ClaudeCode")
        self.assertEqual(self.m.managed_dir("linux"), "/etc/claude-code")
        mcp, settings = self.m.managed_files("win32")
        self.assertEqual(mcp, r"C:\Program Files\ClaudeCode\managed-mcp.json")
        self.assertEqual(settings[0], r"C:\Program Files\ClaudeCode\managed-settings.json")

    def test_windows_command_paths_give_a_base_name_endpoint(self):
        self.assertEqual(self.m.endpoint({"command": "C:\\Tools\\srv.exe"}), "srv.exe")
        g, _ = self.m.gated("gt-lotr", {"command": "python",
                                        "args": ["-I", "C:\\Users\\me\\.claude\\plugins\\cache\\"
                                                       "gt-lotr\\scripts\\lotr_mcp.py"]})
        self.assertTrue(g)

    def test_managed_recipe_names_the_windows_path_and_is_guidance_only(self):
        t = self.m.managed_text("win32")
        self.assertIn(r"C:\Program Files\ClaudeCode\managed-settings.json", t)
        self.assertIn("GUIDANCE ONLY", t)
        self.assertIn('"allowManagedMcpServersOnly": true', t)


class Rows(Fixture):
    """The doctor row and the verify row."""

    def test_doctor_row_lists_ungated_and_is_never_ok_then(self):
        self.claude_json["claudeAiMcpEverConnected"] = ["claude.ai Drive"]
        self.claude_json["mcpServers"] = {"third": {"command": "x"}}
        self.write()
        p = self.py(DOCTOR, "--only", "mcp", "--json", cwd=self.work)
        row = [r for r in json.loads(p.stdout)["checks"] if r["check"] == "mcp"][0]
        self.assertEqual(row["state"], "note")
        self.assertIn("2 MCP server(s) outside LOTR", row["summary"])
        self.assertIn("claude.ai Drive", row["detail"])
        self.assertIn("third", row["detail"])

    def test_doctor_row_ok_when_only_gts_servers(self):
        gt_plugin_servers(self.cache)
        self.plugin("gt@golden-thread-plugin", self.cache / "gt")
        self.write()
        p = self.py(DOCTOR, "--only", "mcp", "--json", cwd=self.work)
        row = [r for r in json.loads(p.stdout)["checks"] if r["check"] == "mcp"][0]
        self.assertEqual(row["state"], "ok", row)

    def test_verify_row_not_checked_while_any_ungated_and_caps_the_level(self):
        self.claude_json["mcpServers"] = {"third": {"command": "x"}}
        self.write()
        p = self.py(VERIFY, "--json", cwd=self.work)
        v = json.loads(p.stdout)
        row = [r for r in v["rows"] if r["check"] == "mcp"][0]
        self.assertEqual(row["state"], "NOT-CHECKED")
        self.assertIn("third", row["why"])
        self.assertIn("LOTR and gt-vault only", row["why"])
        self.assertEqual(v["level"].get("scope"), "LOTR and gt-vault only")

    def test_verify_level_line_is_annotated_when_unlock_is_on(self):
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else
                                 os.environ.__setitem__(k, v) for k, v in saved.items()])
        os.environ["HOME"] = str(self.home)
        if IS_WINDOWS:
            os.environ["USERPROFILE"] = str(self.home)
        self.claude_json["mcpServers"] = {"third": {"command": "x"}}
        self.write()
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(sys.path.remove, str(SCRIPTS))
        vm = load_module(VERIFY, "gt_unlock_verify_mcp")
        r = vm.Run()
        level = {"level": "L2", "why": "TOTP + Touch ID"}
        self.assertEqual(vm.check_mcp(r, level), 1)
        self.assertEqual(r.rows[0]["state"], vm.NC)
        self.assertIn("covers LOTR and gt-vault only; 1 MCP server(s) bypass it", level["why"])
        (self.home / ".claude.json").write_text("{}", encoding="utf-8")
        r = vm.Run()
        self.assertEqual(vm.check_mcp(r, {"level": "L2", "why": "x"}), 0)
        self.assertEqual(r.rows[0]["state"], vm.PASS)


if __name__ == "__main__":
    unittest.main()
