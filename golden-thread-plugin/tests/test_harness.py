"""The harness itself: it must find the plugin and build an isolated vault."""
import unittest
from _harness import Sandbox, GT, WIKI, SCRIPTS


class HarnessTest(Sandbox):
    def test_finds_version_dirs(self):
        self.assertTrue((GT / ".claude-plugin" / "plugin.json").is_file())
        self.assertTrue((WIKI / ".claude-plugin" / "plugin.json").is_file())

    def test_vault_fixture_is_isolated(self):
        v = self.make_vault()
        self.assertTrue((v / "CLAUDE.md").is_file())
        self.assertTrue(str(v).startswith(str(self.tmp)))
        cfg = (self.home / ".claude" / "vault-config.json").read_text()
        self.assertIn(str(v.resolve()), cfg)


if __name__ == "__main__":
    unittest.main()
