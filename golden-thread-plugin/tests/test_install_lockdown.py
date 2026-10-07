"""install.sh and the lockdown level (0.20.5).

  * `--lockdown LEVEL` applies the level with no question;
  * with no terminal and no flag, a NEW install stays very secure and says how to choose;
  * with no terminal and no flag, an UPGRADE keeps the level the machine is at;
  * a bad `--lockdown` value stops the install before anything is installed;
  * uninstall takes out gt's lockdown rules and leaves the user's own.

The question itself needs a terminal (`/dev/tty`), which a test run does not have; the
no-terminal paths above are the ones a scripted install takes.
"""
import json
import unittest

import test_install   # the module, not the class: a bare import would collect its tests here too


class InstallLockdown(test_install.InstallTest):
    def settings(self):
        p = self.home / ".claude" / "settings.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def allow(self):
        return (self.settings().get("permissions") or {}).get("allow") or []

    def test_flag_applies_the_level(self):
        p = self.install("--lockdown", "partly-secure")
        self.assertOk(p)
        self.assertIn("Bash(ssh:*)", self.allow())
        self.assertIn("Bash(git status:*)", self.allow())

    def test_new_install_without_terminal_stays_very_secure(self):
        p = self.install()
        self.assertOk(p)
        self.assertEqual([a for a in self.allow() if a.startswith("Bash(git status")], [])
        self.assertIn("Lockdown: very secure", p.stdout)
        self.assertIn("--lockdown", p.stdout)

    def test_upgrade_without_terminal_keeps_the_level(self):
        self.assertOk(self.install("--lockdown", "mostly-secure"))
        p = self.install()
        self.assertOk(p)
        self.assertIn("Bash(git status:*)", self.allow())
        self.assertIn("Lockdown: mostly secure", p.stdout)

    def test_bad_value_stops_before_installing(self):
        p = self.install("--lockdown", "mostly")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("very-secure", p.stdout + p.stderr)
        self.assertFalse(self.cache().exists(), "nothing may be installed after a bad flag")

    def test_uninstall_removes_gt_rules_only(self):
        sp = self.home / ".claude" / "settings.json"
        sp.write_text(json.dumps({"permissions": {"allow": ["Bash(make deploy:*)"]}}) + "\n",
                      encoding="utf-8")
        self.assertOk(self.install("--lockdown", "insecure"))
        self.assertIn("Bash", self.allow())
        u = self.sh(self.repo / "install.sh", "--uninstall", "--yes", timeout=300)
        self.assertOk(u)
        self.assertEqual(self.allow(), ["Bash(make deploy:*)"])


if __name__ == "__main__":
    unittest.main()
