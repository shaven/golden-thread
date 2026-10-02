"""hooks/vault_hints.py -- a few vault page titles relevant to the prompt, off by default (0.19.0).

Request 2026-10-02-prompt-relevant-vault-hints, accepted by the owner into 0.19.0 with the
setting OFF by default. On UserPromptSubmit it matches the prompt against the vault's index.md
and adds at most three "title -- path" lines; never a page body, nothing below the threshold,
nothing when it runs past its time budget, and it always exits 0.
"""
import json
import subprocess
import sys
import unittest

from _harness import Sandbox, GT, SCRIPTS, load_module

HOOK = GT / "hooks" / "vault_hints.py"
SENTINEL = "BODY-SENTINEL-never-in-a-hint"


class HintsBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        k = self.vault / "Knowledge"
        k.mkdir(parents=True)
        pages = {"Backup Rotation": "how nightly snapshots are kept and pruned",
                 "DNS Zones": "internal and public zones, who edits them",
                 "Deploy Windows": "when production deploys are allowed",
                 "Log Retention": "how long logs are kept and where",
                 "Build Cache": "why builds are fast and how to clear the cache"}
        for title, summary in pages.items():
            (k / ("%s.md" % title)).write_text("# %s\n\n%s %s\n" % (title, summary, SENTINEL))
        (self.vault / "index.md").write_text(
            "# Index\n\n" + "".join("- [[%s]] — %s\n" % kv for kv in pages.items()))

    def config(self, **kv):
        d = {"vault_path": str(self.vault)}
        d.update(kv)
        (self.home / ".claude" / "vault-config.json").write_text(json.dumps(d))

    def hook(self, prompt, env=None):
        e = dict(self.env)
        e.update(env or {})
        # the installed layout: the hook runs beside gt_keyword_recall in one directory
        p = subprocess.run([sys.executable, "-B", str(HOOK)], input=json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": prompt}),
            capture_output=True, text=True, env=e, timeout=30,
            cwd=str(self.tmp))
        self.assertEqual(p.returncode, 0, p.stderr)
        if not p.stdout.strip():
            return None
        return json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]


class VaultHints(HintsBase):
    def test_off_by_default_adds_nothing(self):
        self.config()
        self.assertIsNone(self.hook("how long are logs kept in cold storage"))

    def test_on_a_matching_prompt_gets_at_most_three_titles_with_paths(self):
        self.config(vault_hints="on")
        out = self.hook("how long are logs kept, and when are production deploys allowed")
        self.assertIsNotNone(out)
        lines = [l for l in out.splitlines() if l.startswith("- ")]
        self.assertTrue(1 <= len(lines) <= 3, out)
        self.assertIn("Knowledge/Log Retention.md", out)
        self.assertIn("Log Retention", out)

    def test_a_hint_never_carries_page_contents(self):
        self.config(vault_hints="on")
        out = self.hook("nightly snapshots pruned backup rotation")
        self.assertIsNotNone(out)
        self.assertNotIn(SENTINEL, out)
        self.assertNotIn("nightly snapshots are kept", out)

    def test_no_match_above_the_threshold_adds_nothing(self):
        self.config(vault_hints="on")
        self.assertIsNone(self.hook("please refactor the parser to use a generator"))

    def test_past_its_time_budget_it_adds_nothing_and_exits_zero(self):
        self.config(vault_hints="on")
        self.assertIsNone(self.hook("how long are logs kept", env={"GT_HINTS_BUDGET_MS": "0"}))

    def test_no_vault_or_no_index_adds_nothing(self):
        self.config(vault_hints="on", vault_path=str(self.tmp / "nowhere"))
        self.assertIsNone(self.hook("how long are logs kept"))


class SettingAndWiring(HintsBase):
    def test_gt_settings_show_lists_it_default_off(self):
        self.config()
        p = self.py(SCRIPTS / "gt_settings.py", "show")
        self.assertIn("vault_hints", p.stdout)
        line = next(l for l in p.stdout.splitlines() if l.strip().startswith("vault_hints"))
        self.assertIn("off", line)
        self.assertIn("default", line)

    def test_it_is_registered_on_user_prompt_submit(self):
        comp = load_module(SCRIPTS / "gt_components.py", "gt_components_hints")
        regs = [r for r in comp.HOOK_REGISTRATIONS if r["script"] == "vault_hints.py"]
        self.assertEqual([r["event"] for r in regs], ["UserPromptSubmit"])
        self.assertIn("gt_keyword_recall.py", comp.HOOK_DIR_SCRIPTS)


if __name__ == "__main__":
    unittest.main()
