"""The `lockdown` setting (0.20.5): gt_settings.py set/get/explain/show wired to gt_lockdown.py.

  * `set lockdown <level>` applies the level's rules to ~/.claude/settings.json and records it;
  * an unknown level is refused with the four valid ones, and nothing is written;
  * `explain lockdown` prints the chart; `show` lists the setting;
  * `lockdown` is a security key, so gt unlock gates it like sandbox_mode.
"""
import json
import unittest

from _harness import SCRIPTS, Sandbox, load_module

TOOL = SCRIPTS / "gt_settings.py"
LOCKDOWN = SCRIPTS / "gt_lockdown.py"


class LockdownSetting(Sandbox):
    def setUp(self):
        super().setUp()
        self.config(vault_path=str(self.tmp))
        self.sp = self.home / ".claude" / "settings.json"

    def allow(self):
        if not self.sp.exists():
            return []
        return (json.loads(self.sp.read_text(encoding="utf-8")).get("permissions") or {}) \
            .get("allow") or []

    def cfg(self):
        return json.loads((self.home / ".claude" / "vault-config.json").read_text(encoding="utf-8"))

    def test_set_applies_the_level_and_records_it(self):
        p = self.py(TOOL, "set", "lockdown", "mostly-secure")
        self.assertOk(p)
        self.assertIn("Bash(git status:*)", self.allow())
        self.assertNotIn("Bash(ssh:*)", self.allow())
        self.assertEqual(self.cfg().get("lockdown"), "mostly-secure")
        g = self.py(TOOL, "get", "lockdown")
        self.assertOk(g)
        self.assertEqual(g.stdout.strip(), "mostly-secure")
        self.assertOk(self.py(TOOL, "set", "lockdown", "very-secure"))
        self.assertEqual(self.allow(), [], "back to very secure removes gt's rules")

    def test_default_is_very_secure(self):
        g = self.py(TOOL, "get", "lockdown")
        self.assertOk(g)
        self.assertEqual(g.stdout.strip(), "very-secure")

    def test_unknown_level_refused_and_nothing_written(self):
        p = self.py(TOOL, "set", "lockdown", "mostly")
        self.assertNotEqual(p.returncode, 0)
        for lvl in ("very-secure", "mostly-secure", "partly-secure", "insecure"):
            self.assertIn(lvl, p.stdout + p.stderr)
        self.assertFalse(self.sp.exists())
        self.assertNotIn("lockdown", self.cfg())

    def test_explain_prints_the_chart(self):
        p = self.py(TOOL, "explain", "lockdown")
        self.assertOk(p)
        for label in ("very secure", "mostly secure", "partly secure", "insecure"):
            self.assertIn(label, p.stdout)
        self.assertIn("not a security boundary", p.stdout)

    def test_show_lists_it(self):
        p = self.py(TOOL, "show")
        self.assertOk(p)
        self.assertIn("lockdown", p.stdout)

    def test_lockdown_is_a_security_key(self):
        gs = load_module(TOOL, "gt_settings_lockdown_keys")
        self.assertIn("lockdown", gs.SECURITY_KEYS)


if __name__ == "__main__":
    unittest.main()
