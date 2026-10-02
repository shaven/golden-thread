"""gt_model_policy.py -- the model and effort each INSTALLED skill runs at (0.19.0).

Request 2026-10-02-model-and-effort-per-plugin. The policy writes `model:` and `effort:` into
the installed copies of each SKILL.md (plugin cache and marketplace), never the release
source, and records exactly what it wrote so a hand edit can be told from its own work.
Profiles (owner, 2026-10-02): average = the intent pack (fast haiku with no effort, balanced
sonnet medium, deep opus high); very-high = opus xhigh everywhere; inherit = write nothing.
"""
import hashlib
import json
import shutil
import unittest
from pathlib import Path

from _harness import Sandbox, SCRIPTS, GT

POLICY = SCRIPTS / "gt_model_policy.py"
MARKET = "golden-thread-plugin"


def frontmatter(path):
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    out = {}
    for line in lines[1:end]:
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


class PolicyBase(Sandbox):
    """A fake install: gt in the plugin cache and the marketplace, three skills, one per tier."""
    SKILLS = {"gt-list": "fast", "gt-work": "balanced", "gt-plan": "deep"}

    def setUp(self):
        super().setUp()
        self.release = self.tmp / "src" / "golden-thread" / "9.9.9"
        plugins = self.home / ".claude" / "plugins"
        self.cache = plugins / "cache" / MARKET / "gt" / "9.9.9"
        self.market = plugins / "marketplaces" / MARKET / "plugins" / "gt"
        for root in (self.release, self.cache, self.market):
            for name, intent in self.SKILLS.items():
                d = root / "skills" / name
                d.mkdir(parents=True)
                (d / "SKILL.md").write_text(
                    "---\nname: %s\ndescription: \"x\"\nmodel_intent: %s\n---\n\nbody\n"
                    % (name, intent), encoding="utf-8")
        (plugins / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
            "gt@%s" % MARKET: [{"scope": "user", "installPath": str(self.cache),
                                "version": "9.9.9"}]}}))
        self.vault = self.tmp / "vault"
        (self.vault / "Projects" / "golden-thread" / "packs").mkdir(parents=True)

    def policy(self, *args):
        p = self.py(POLICY, *args, "--home", self.home, "--vault", self.vault)
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        return p

    def got(self, root, name):
        fm = frontmatter(root / "skills" / name / "SKILL.md")
        return fm.get("model"), fm.get("effort")

    def tree_hash(self, root):
        h = hashlib.sha256()
        for f in sorted(Path(root).rglob("*")):
            if f.is_file():
                h.update(str(f.relative_to(root)).encode() + f.read_bytes())
        return h.hexdigest()


class Profiles(PolicyBase):
    def test_average_follows_the_intent_pack(self):
        self.assertOk(self.policy("apply", "--profile", "average"))
        for root in (self.cache, self.market):
            with self.subTest(root=root.name):
                self.assertEqual(self.got(root, "gt-list"), ("haiku", None))
                self.assertEqual(self.got(root, "gt-work"), ("sonnet", "medium"))
                self.assertEqual(self.got(root, "gt-plan"), ("opus", "high"))

    def test_very_high_is_opus_xhigh_everywhere(self):
        self.assertOk(self.policy("apply", "--profile", "very-high"))
        for name in self.SKILLS:
            self.assertEqual(self.got(self.cache, name), ("opus", "xhigh"), name)

    def test_inherit_writes_nothing_and_removes_what_the_policy_wrote(self):
        before = self.tree_hash(self.cache)
        self.assertOk(self.policy("apply", "--profile", "inherit"))
        self.assertEqual(self.tree_hash(self.cache), before, "inherit wrote something")
        self.assertOk(self.policy("apply", "--profile", "very-high"))
        self.assertOk(self.policy("apply", "--profile", "inherit"))
        self.assertEqual(self.tree_hash(self.cache), before, "inherit left fields behind")

    def test_the_release_source_is_never_touched(self):
        before = self.tree_hash(self.release)
        for prof in ("average", "very-high", "inherit"):
            self.assertOk(self.policy("apply", "--profile", prof))
        self.assertEqual(self.tree_hash(self.release), before)

    def test_reapplying_is_idempotent(self):
        self.assertOk(self.policy("apply", "--profile", "average"))
        once = self.tree_hash(self.cache)
        self.assertOk(self.policy("apply", "--profile", "average"))
        self.assertEqual(self.tree_hash(self.cache), once)

    def test_the_recorded_profile_is_used_when_none_is_passed(self):
        self.assertOk(self.policy("choose", "very-high"))
        self.assertOk(self.policy("apply"))
        self.assertEqual(self.got(self.cache, "gt-list"), ("opus", "xhigh"))
        choices = json.loads((self.home / ".claude/golden-thread/install-choices.json").read_text())
        self.assertEqual(choices["model"]["profile"], "very-high")

    def test_an_unknown_profile_is_refused(self):
        p = self.py(POLICY, "apply", "--profile", "turbo", "--home", self.home)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("average, very-high, inherit", p.stdout + p.stderr)


class TheRecordTellsAHandEditFromThePolicy(PolicyBase):
    def test_verify_is_clean_after_apply_and_names_a_hand_edit(self):
        self.assertOk(self.policy("apply", "--profile", "average"))
        self.assertOk(self.policy("verify"))
        f = self.cache / "skills" / "gt-work" / "SKILL.md"
        f.write_text(f.read_text().replace("effort: medium", "effort: max"))
        p = self.policy("verify")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("gt-work", p.stdout)
        self.assertIn("effort", p.stdout)

    def test_the_table_warns_about_allowance_above_medium(self):
        p = self.policy("table", "--profile", "very-high")
        self.assertIn("xhigh", p.stdout)
        self.assertIn("allowance", p.stdout.lower())
        p = self.policy("table", "--profile", "inherit")
        self.assertNotIn("allowance", p.stdout.lower())


if __name__ == "__main__":
    unittest.main()
