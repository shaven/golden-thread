"""gt-lotr as a gt MODULE: optional, off by default, stdlib-only, and self-consistent.

Contracts pinned here (vault: Projects/golden-thread/mcp-gateway, ADR-1..4):
  * module.json is a valid module per BOTH validators (dev/plugins.py), admits this gt, and
    is OFF by default -- a machine gets the gateway only with `./install.sh --with lotr`;
  * plugin.json agrees with module.json and its directory, and declares exactly one MCP
    server, the stdio shim, addressed through ${CLAUDE_PLUGIN_ROOT} (plugin.json travels with
    every install; a root .mcp.json would not be copied by install.sh);
  * every script module.json lists exists, and the skill exists with its triggers;
  * nothing under scripts/ imports a third-party package: the same code must run on this
    Mac's python3 3.9 and on a Linux hub with no pip step;
  * the MCP shim's four tools are the whole surface -- adding connections never changes it.
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import unittest

from _harness import REPO, latest_version_dir, needs_dev
from _harness import IS_WINDOWS

GW = latest_version_dir(REPO / "golden-thread-lotr")
SCRIPTS = GW / "scripts"

# Python 3.9 has no sys.stdlib_module_names; this is the set the gateway may use.
STDLIB = {
    "__future__", "argparse", "ast", "base64", "binascii", "collections", "contextlib", "copy", "dataclasses",
    "datetime", "difflib", "errno", "fnmatch", "functools", "getpass", "hashlib", "hmac", "ipaddress", "ipaddress",
    "importlib", "winreg",
    "http", "io", "itertools", "json", "logging", "math", "os", "pathlib", "platform", "queue",
    "re", "runpy", "secrets", "select", "selectors", "shlex", "shutil", "signal", "socket",
    "socketserver", "ssl", "stat", "string", "struct", "subprocess", "sys", "tempfile",
    "textwrap", "threading", "time", "traceback", "typing", "urllib", "uuid", "warnings",
}


class GatewayModule(unittest.TestCase):
    @needs_dev
    def test_valid_module_admits_this_gt_and_is_off_by_default(self):
        gt = latest_version_dir(REPO / "golden-thread").name
        r = subprocess.run([sys.executable, str(REPO / "dev" / "plugins.py"), "module-check",
                            str(GW), "--gt", gt], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = json.loads((GW / "module.json").read_text())
        self.assertEqual(m["default"], "off")
        self.assertEqual(m["hooks"], [])          # no hook: an off-by-default module costs nothing
        r = subprocess.run([sys.executable, str(REPO / "dev" / "plugins.py"), "modules"],
                           capture_output=True, text=True)
        self.assertIn(f"lotr gt-lotr {GW.name} off", r.stdout)

    def test_plugin_json_agrees_and_declares_the_shim(self):
        p = json.loads((GW / ".claude-plugin" / "plugin.json").read_text())
        m = json.loads((GW / "module.json").read_text())
        self.assertEqual(p["name"], m["plugin"])
        self.assertEqual(p["version"], m["version"])
        self.assertEqual(p["version"], GW.name)
        servers = p["mcpServers"]
        self.assertEqual(list(servers), ["gt-lotr"])
        args = servers["gt-lotr"]["args"]
        self.assertEqual(args, ["${CLAUDE_PLUGIN_ROOT}/scripts/lotr_mcp.py"])
        self.assertFalse((GW / ".mcp.json").exists())

    def test_listed_scripts_and_skill_exist(self):
        m = json.loads((GW / "module.json").read_text())
        for s in m["scripts"]:
            self.assertTrue((SCRIPTS / s).is_file(), s)
        for sk in m["skills"]:
            text = (GW / "skills" / sk / "SKILL.md").read_text()
            self.assertIn(f"name: {sk}", text)
            self.assertIn("Use when the user says", text)

    def test_stdlib_only(self):
        offenders = []
        for f in sorted(SCRIPTS.rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"), str(f))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for n in names:
                    top = n.split(".")[0]
                    if top not in STDLIB and top != "lotrlib":
                        offenders.append(f"{f.relative_to(GW)}: {n}")
        self.assertEqual(offenders, [])

    def test_mcp_is_the_same_cli_as_lotr(self):
        # Owner, 2026-10-01: "both names work". `mcp` forwards to `lotr`; same output, same exit.
        import tempfile
        home = tempfile.mkdtemp(dir=None if IS_WINDOWS else "/tmp", prefix="lm")
        try:
            outs = []
            for name in ("lotr.py", "mcp.py"):
                h = os.path.join(home, name)
                r = [subprocess.run([sys.executable, str(SCRIPTS / name), "--home", h, *a],
                                    capture_output=True, text=True)
                     for a in (["init", "--zone", "personal", "--mode", "local"], ["connections"])]
                outs.append([(x.returncode, json.loads(x.stdout)) for x in r])
            self.assertEqual(outs[0][1], outs[1][1])
            self.assertEqual([c for c, _ in outs[0]], [c for c, _ in outs[1]])
        finally:
            shutil.rmtree(home, True)

    def test_shipped_recipes_load(self):
        sys.path.insert(0, str(SCRIPTS))
        from lotrlib import recipes
        names = {r["name"] for r in recipes.load_recipes([GW / "templates" / "recipes"])}
        self.assertTrue({"github.my_open_prs", "jira.my_open_issues", "m365.today"} <= names)


if __name__ == "__main__":
    unittest.main()
