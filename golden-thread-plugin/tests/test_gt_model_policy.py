"""gt_model_policy.py -- the model and effort each INSTALLED skill runs at (0.19.1).

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


class Overrides(PolicyBase):
    """6d: per-skill > per-plugin > intent > inherit; `set` re-applies with no reinstall; a
    refused effort is refused at set time; choices survive a profile change (a reinstall)."""

    def test_a_skill_override_beats_a_plugin_override_which_beats_the_intent(self):
        self.assertOk(self.policy("choose", "average"))
        self.assertOk(self.policy("set", "--plugin", "gt", "--model", "sonnet", "--effort", "low"))
        self.assertOk(self.policy("set", "--skill", "gt-plan", "--model", "opus",
                                  "--effort", "max"))
        self.assertEqual(self.got(self.cache, "gt-list"), ("sonnet", "low"))
        self.assertEqual(self.got(self.cache, "gt-plan"), ("opus", "max"))

    def test_set_refuses_an_effort_the_model_does_not_accept_and_records_nothing(self):
        p = self.policy("set", "--skill", "gt-list", "--model", "haiku", "--effort", "low")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("no effort levels", p.stdout + p.stderr)
        choices = self.home / ".claude/golden-thread/install-choices.json"
        rec = json.loads(choices.read_text()) if choices.exists() else {}
        self.assertNotIn("gt-list", rec.get("model", {}).get("skills", {}))

    def test_overrides_survive_a_new_profile(self):
        self.assertOk(self.policy("set", "--skill", "gt-work", "--model", "haiku"))
        self.assertOk(self.policy("choose", "very-high"))
        self.assertOk(self.policy("apply"))
        self.assertEqual(self.got(self.cache, "gt-work"), ("haiku", None))
        self.assertEqual(self.got(self.cache, "gt-list"), ("opus", "xhigh"))

    def test_clear_returns_the_skill_to_its_intent(self):
        self.assertOk(self.policy("choose", "average"))
        self.assertOk(self.policy("set", "--skill", "gt-list", "--model", "opus"))
        self.assertOk(self.policy("clear", "--skill", "gt-list"))
        self.assertEqual(self.got(self.cache, "gt-list"), ("haiku", None))

    def test_show_names_the_effective_pair_and_where_it_came_from(self):
        self.assertOk(self.policy("choose", "average"))
        self.assertOk(self.policy("set", "--skill", "gt-plan", "--model", "sonnet",
                                  "--effort", "high"))
        d = json.loads(self.policy("show", "--json").stdout)
        rows = {(r["plugin"], r["skill"]): r for r in d["skills"]}
        self.assertEqual((rows[("gt", "gt-plan")]["model"], rows[("gt", "gt-plan")]["source"]),
                         ("sonnet", "skill override"))
        self.assertIn("intent", rows[("gt", "gt-work")]["source"])

    def test_gt_settings_show_lists_the_model_profile(self):
        self.assertOk(self.policy("choose", "very-high"))
        p = self.py(SCRIPTS / "gt_settings.py", "show")
        self.assertIn("model_profile", p.stdout)
        self.assertIn("very-high", p.stdout)


class DoctorRow(PolicyBase):
    """6e: the doctor names the active profile, and a hand edit to a field the policy wrote is
    drift (WARN, with the fix) while the policy's own fields are not."""

    def doctor(self):
        p = self.py(SCRIPTS / "gt_doctor.py", "--only", "model-policy", "--json")
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        rows = json.loads(p.stdout)["checks"]
        return next(r for r in rows if r["check"] == "model-policy")

    def test_clean_after_apply_and_names_the_profile(self):
        self.assertOk(self.policy("choose", "average"))
        self.assertOk(self.policy("apply"))
        r = self.doctor()
        self.assertEqual(r["state"], "ok", r)
        self.assertIn("average", r["summary"])

    def test_a_hand_edit_is_a_warning_with_the_fix(self):
        self.assertOk(self.policy("choose", "average"))
        self.assertOk(self.policy("apply"))
        f = self.cache / "skills" / "gt-plan" / "SKILL.md"
        f.write_text(f.read_text().replace("model: opus", "model: haiku"))
        r = self.doctor()
        self.assertEqual(r["state"], "warn", r)
        self.assertIn("gt-plan", r["detail"])
        self.assertIn("gt_model_policy.py apply", r["fix"])


if __name__ == "__main__":
    unittest.main()
