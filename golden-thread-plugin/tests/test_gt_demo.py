"""gt_demo.sh + the demo tour — the demo runs in its own vault and never touches the real one.

Rewritten for 0.9.14 (request 2026-09-11-demo-full-tour-own-vault). The previous design
ran in the user's real vault and rewound its git history on `clean`; its tests pinned the
guards around that reset. The reset no longer exists, so neither do those tests: what is
pinned now is that nothing outside the demo vault changes, whatever the demo does.
"""
import hashlib
import json
import re
import shutil
import unittest
from pathlib import Path

from _harness import Sandbox, GT, PYTHON

ACTS = 10


def tree_digest(root: Path):
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


class DemoTest(Sandbox):
    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git required")
        # A fake install, laid out like the plugin cache, run from under ~/.claude.
        self.plugin = self.home / ".claude" / "plugins" / "cache" / "golden-thread-plugin" / "gt" / GT.name
        self.plugin.mkdir(parents=True)
        for d in ("scripts", "templates", "skills", "hooks"):
            shutil.copytree(GT / d, self.plugin / d)
        hooks = self.home / ".claude" / "golden-thread" / "hooks"
        shutil.copytree(GT / "hooks", hooks)
        # The user's REAL vault and config — the demo must leave both byte-identical.
        self.real = self.make_vault("real")
        self.cfg = self.home / ".claude" / "vault-config.json"
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.write_text("{}\n")
        self.demo = self.home / ".claude" / "golden-thread" / "demo-vault"
        self.script = self.plugin / "scripts" / "gt_demo.sh"

    def demo_cmd(self, *args, **kw):
        return self.sh(self.script, *args, **kw)

    def snapshot(self):
        return (tree_digest(self.real), self.cfg.read_bytes(), self.settings.read_bytes(),
                (self.home / ".claude" / "CLAUDE.md").read_bytes() if (self.home / ".claude" / "CLAUDE.md").exists() else b"")

    # -- start ---------------------------------------------------------------------------
    def test_start_builds_a_seeded_demo_vault(self):
        self.assertOk(self.demo_cmd("start"))
        self.assertTrue((self.demo / ".demo" / "DEMO_VAULT").is_file())
        self.assertTrue((self.demo / "Projects" / "demo-pizzabot" / "README.md").is_file())
        self.assertTrue(any((self.demo / "Sources").glob("*PizzaBot*.md")))
        self.assertIn("oven timer", (self.demo / "INBOX.md").read_text())
        log = self.run_cmd(["git", "-C", self.demo, "log", "--oneline"]).stdout
        self.assertIn("PizzaBot 3000 seeded", log)
        readme = (self.demo / "Projects" / "demo-pizzabot" / "README.md").read_text()
        self.assertNotIn("{{TODAY}}", readme, "task dates must be filled in at start")

    def test_real_vault_config_and_settings_untouched_by_every_command(self):
        before = self.snapshot()
        for cmd in ("start", "end", "status", "clean", "end"):
            self.demo_cmd(cmd)
        self.assertEqual(self.snapshot(), before,
                         "the demo changed the real vault, vault-config.json, settings.json or CLAUDE.md")

    def test_start_prints_the_pinned_launch_command(self):
        out = self.demo_cmd("start").stdout
        self.assertIn(f'GT_VAULT="{self.demo}" claude', out)
        self.assertIn("/gt:gt-demo tour", out)

    def test_start_twice_refuses(self):
        self.assertOk(self.demo_cmd("start"))
        self.assertNotEqual(self.demo_cmd("start").returncode, 0)

    # -- what the tour relies on -------------------------------------------------------------
    def test_demo_vault_lints_with_only_the_planted_broken_link(self):
        self.assertOk(self.demo_cmd("start"))
        out = self.py(self.plugin / "scripts" / "gt_lint.py", self.demo).stdout
        found = [l for l in re.findall(r"^\[([a-z-]+)\] (.+)$", out, re.M) if l[0] != "core-unenforced"]
        self.assertEqual(found, [("broken-link", "Projects/demo-pizzabot/README.md")], out)

    def test_due_today_task_ranks_first(self):
        self.assertOk(self.demo_cmd("start"))
        tool = self.demo / "Projects" / "golden-thread" / "tools" / "gt_tasks.py"
        self.assertOk(self.py(tool, "--vault", self.demo))
        rows = re.findall(r"^\| `PP\d+-P\d+` \| (.+?) \|", (self.demo / "TASKS.md").read_text(), re.M)
        self.assertTrue(rows, "no ranked tasks in TASKS.md")
        self.assertIn("/order", rows[0])

    def test_act_one_transcript_is_blocked_by_the_stop_hook(self):
        self.assertOk(self.demo_cmd("start"))
        payload = (self.demo / ".demo" / "secret-transcript.json").read_text()
        proc = self.run_cmd(["bash", self.home / ".claude" / "golden-thread" / "hooks" / "validate_response.sh"], input=payload)
        self.assertEqual(json.loads(proc.stdout)["decision"], "block")
        self.assertIn(".demo/", (self.demo / ".gitignore").read_text(), "the transcript must never be committed")

    def test_no_key_shaped_literal_in_the_shipped_plugin(self):
        pat = re.compile(r"AKIA[0-9A-Z]{16}")
        for p in list((GT / "scripts").iterdir()) + list((GT / "templates" / "demo-pizzabot").rglob("*")):
            if p.is_file():
                self.assertIsNone(pat.search(p.read_text(errors="replace")), f"key-shaped literal in {p}")

    def test_tour_has_ten_complete_acts_naming_real_skills(self):
        tour = (GT / "templates" / "demo-pizzabot" / "tour.md").read_text()
        acts = re.split(r"^## Act \d+ — ", tour, flags=re.M)[1:]
        self.assertEqual(len(acts), ACTS)
        skills = {p.name for p in (GT / "skills").iterdir()} | {"gt-wiki-ingest"}
        for body in acts:
            for key in ("narration:", "do:", "point:"):
                self.assertIn(key, body, body[:60])
            for s in re.findall(r"\bthe (gt-[a-z-]+) skill\b", body):
                self.assertIn(s, skills, f"tour names a skill that does not ship: {s}")

    # -- end, clean, remove ------------------------------------------------------------------
    def test_end_lists_what_the_tour_produced(self):
        self.assertOk(self.demo_cmd("start"))
        (self.demo / "Knowledge" / "Topping Conflict Matrix.md").write_text("# TCM\n")
        self.run_cmd(["git", "-C", self.demo, "add", "-A"])
        self.run_cmd(["git", "-C", self.demo, "commit", "-qm", "promote"])
        out = self.demo_cmd("end").stdout
        self.assertIn("promote", out)
        self.assertIn("Topping Conflict Matrix.md", out)

    def test_clean_rebuilds_without_any_git_reset(self):
        self.assertOk(self.demo_cmd("start"))
        (self.demo / "leftover.md").write_text("from the last run\n")
        self.assertOk(self.demo_cmd("clean"))
        self.assertFalse((self.demo / "leftover.md").exists())
        self.assertTrue((self.demo / ".demo" / "DEMO_VAULT").is_file())
        self.assertNotIn("git reset", self.script.read_text())

    def test_clean_and_remove_refuse_a_directory_without_the_marker(self):
        other = self.tmp / "not-a-demo"
        other.mkdir()
        (other / "precious.md").write_text("keep me\n")
        for cmd in ("clean", "remove"):
            proc = self.demo_cmd(cmd, env={"GT_DEMO_VAULT": str(other)})
            self.assertNotEqual(proc.returncode, 0)
            self.assertTrue((other / "precious.md").exists(), f"{cmd} deleted a non-demo directory")

    def test_remove_deletes_demo_and_switches_it_off(self):
        self.assertOk(self.demo_cmd("start"))
        self.assertOk(self.demo_cmd("remove"))
        self.assertFalse(self.demo.exists())
        self.assertEqual(json.loads(self.cfg.read_text()).get("install_demo"), "no")
        self.assertFalse((self.plugin / "skills" / "gt-demo").exists())
        self.assertTrue(self.real.exists())

    def test_remove_never_touches_a_source_checkout(self):
        src = self.tmp / "checkout"
        shutil.copytree(GT / "scripts", src / "scripts")
        shutil.copytree(GT / "templates", src / "templates")
        shutil.copytree(GT / "skills", src / "skills")
        self.sh(src / "scripts" / "gt_demo.sh", "remove")
        self.assertTrue((src / "skills" / "gt-demo" / "SKILL.md").exists())
        self.assertTrue((src / "scripts" / "gt_demo.sh").exists())

    # -- every skill can be pinned to the demo vault ---------------------------------------
    def test_every_skill_that_locates_the_vault_honors_gt_vault(self):
        for p in (GT / "skills").glob("*/SKILL.md"):
            t = p.read_text()
            if "vault-config" in t:
                self.assertIn("GT_VAULT", t, f"{p.parent.name} reads vault-config.json but ignores $GT_VAULT")


if __name__ == "__main__":
    unittest.main()
