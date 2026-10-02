"""gt_model.py -- skills declare a model INTENT, never a model name (0.18.0).

Contracts pinned here (request 2026-09-27-skills-declare-a-model-intent):
  * `model_intent` is fast|balanced|deep; any other value is refused by dev/submissions.py
    (packs and skills) and by the release gate's skill lint, naming where it is and what it is;
  * a skill that declares nothing resolves to the session's own model -- unchanged;
  * resolution goes through the registry's `model` slot: a local pack in the vault beats the
    shipped core mapping, and core's row is reported SHADOWED with the winner named;
  * an unmapped intent is reported UNMAPPED and runs at balanced -- loud, never fatal;
  * every resolution states the model (gt_code_review's plan included);
  * no shipped file carries a literal model id outside the one mapping pack, and no shipped
    skill pins `model:` in its frontmatter;
  * the mapping pack records the date each name was last verified.
"""
import json
import re
import unittest
from pathlib import Path

from _harness import Sandbox, REPO, SCRIPTS, GT, latest_version_dir, needs_dev

GT_MODEL = SCRIPTS / "gt_model.py"
SUBMISSIONS = REPO / "dev" / "submissions.py"
SKILL_LINT = SCRIPTS / "skill_lint.py"
REVIEW = SCRIPTS / "gt_code_review.py"
CORE_PACK = GT / "packs" / "core" / "model.intents.pack.json"
# The shape of a provider model ID: what must never be hard-coded outside the mapping pack.
MODEL_ID = re.compile(
    r"\b(claude-(?:opus|sonnet|haiku|fable|instant|\d)[A-Za-z0-9.\-]*"
    r"|anthropic\.claude[A-Za-z0-9.:\-]*|gpt-\d[A-Za-z0-9.\-]*|gemini-\d[A-Za-z0-9.\-]*)")


def pack(slot, entries, name="mine", tier="D", **extra):
    d = {"schema": 1, "slot": slot, "name": name, "tier": tier, "spdx": "MIT",
         "provenance": {"origin": "original", "contributor": "A Dev <d@e.com>",
                        "upstream": None},
         "dco": "Signed-off-by: A Dev <d@e.com>", "entries": entries}
    d.update(extra)
    return d


def skill(root, name, intent=None):
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    fm = "---\nname: %s\ndescription: \"Fixture. Use when the user says: run %s.\"\n" % (name, name)
    if intent is not None:
        fm += "model_intent: %s\n" % intent
    (d / "SKILL.md").write_text(fm + "---\n\nbody\n")
    return d


class ModelBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        self.packs = self.vault / "Projects" / "golden-thread" / "packs"
        self.packs.mkdir(parents=True)

    def local(self, entries=(), **extra):
        (self.packs / "model.mine.pack.json").write_text(
            json.dumps(pack("model", list(entries), **extra)))

    def resolve(self, *args):
        p = self.py(GT_MODEL, "resolve", *args, "--vault", self.vault, "--json")
        return p, (json.loads(p.stdout) if p.stdout.strip().startswith("{") else None)


@needs_dev
class UnknownIntentIsRefused(ModelBase):
    def test_a_pack_declaring_cheapest_is_rejected_naming_the_value(self):
        f = self.tmp / "review.x.pack.json"
        f.write_text(json.dumps(pack("review", [{"id": "dim", "title": "T", "rubric": "R",
                                                 "model_intent": "cheapest"}])))
        p = self.py(SUBMISSIONS, "validate", f)
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("REJECT", p.stdout)
        self.assertIn("entries[0].model_intent", p.stdout)
        self.assertIn("'cheapest'", p.stdout)

    def test_a_mapping_entry_with_an_unknown_intent_is_rejected(self):
        f = self.tmp / "model.x.pack.json"
        f.write_text(json.dumps(pack("model", [{"intent": "cheapest", "model": "x",
                                                "verified": "2026-10-01"}])))
        p = self.py(SUBMISSIONS, "validate", f)
        self.assertEqual(p.returncode, 2)
        self.assertIn("entries[0].intent is 'cheapest'", p.stdout)

    def test_a_contributed_skill_declaring_cheapest_is_rejected_with_its_line(self):
        d = skill(self.tmp, "fx", "cheapest")
        p = self.py(SUBMISSIONS, "skill", d / "SKILL.md")
        self.assertEqual(p.returncode, 2)
        self.assertIn("line 4", p.stdout)
        self.assertIn("'cheapest'", p.stdout)
        ok = skill(self.tmp / "ok", "fy", "fast")
        self.assertOk(self.py(SUBMISSIONS, "skill", ok / "SKILL.md"))

    def test_a_mapping_without_a_verified_date_is_rejected(self):
        f = self.tmp / "model.y.pack.json"
        f.write_text(json.dumps(pack("model", [{"intent": "deep", "model": "x",
                                                "verified": "last week"}])))
        p = self.py(SUBMISSIONS, "validate", f)
        self.assertIn("bad-date", p.stdout)


class LintRefusesUnknownIntent(ModelBase):
    def test_skill_lint_refuses_with_file_line_and_value(self):
        skill(self.tmp / "root", "good", "deep")
        skill(self.tmp / "root", "bad", "cheapest")
        p = self.py(SKILL_LINT, self.tmp / "root")
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertRegex(p.stdout, r"bad/SKILL\.md:4: model_intent 'cheapest'")
        self.assertNotIn("good/SKILL.md", p.stdout)

    def test_gt_model_check_agrees(self):
        skill(self.tmp / "root", "bad", "cheapest")
        p = self.py(GT_MODEL, "check", self.tmp / "root")
        self.assertEqual(p.returncode, 1)
        self.assertIn("'cheapest'", p.stdout)

    def test_every_shipped_skill_passes(self):
        p = self.py(GT_MODEL, "check", GT / "skills")
        self.assertOk(p)

    def test_resolve_refuses_an_unknown_value(self):
        p, _ = self.resolve("cheapest")
        self.assertEqual(p.returncode, 2)
        self.assertIn("'cheapest'", p.stderr)


class Resolution(ModelBase):
    def test_silence_is_unchanged(self):
        with_, without = skill(self.tmp, "with", "deep"), skill(self.tmp, "without")
        a = json.loads(self.py(GT_MODEL, "skill", with_, "--vault", self.vault, "--json").stdout)
        b = json.loads(self.py(GT_MODEL, "skill", without, "--vault", self.vault,
                               "--json").stdout)
        self.assertEqual((b["declared"], b["model"], b["ran_at"]), (False, None, None))
        self.assertIn("session's own model", b["line"])
        self.assertEqual((a["declared"], a["intent"]), (True, "deep"))
        self.assertIsNotNone(a["model"])

    def test_every_resolution_states_the_model(self):
        p = self.py(GT_MODEL, "resolve", "fast", "--vault", self.vault)
        self.assertOk(p)
        core = json.loads(CORE_PACK.read_text())
        fast = next(e["model"] for e in core["entries"] if e["intent"] == "fast")
        self.assertIn("-> model %s" % fast, p.stdout)

    def test_local_beats_core_and_core_is_shadowed(self):
        self.local([{"intent": "deep", "model": "my-own-model", "verified": "2026-10-01"}])
        p, r = self.resolve("deep")
        self.assertOk(p)
        self.assertEqual((r["model"], r["tier"]), ("my-own-model", "local"))
        sh = [s for s in r["shadowed"] if s["intent"] == "deep"]
        self.assertEqual(len(sh), 1, r["shadowed"])
        self.assertEqual(sh[0]["tier"], "core")
        self.assertIn("mine (local)", sh[0]["lost_to"])
        text = self.py(GT_MODEL, "resolve", "deep", "--vault", self.vault).stdout
        self.assertIn("SHADOWED core deep=", text)

    def test_unmapped_is_loud_not_fatal_and_runs_at_balanced(self):
        self.local(retract=[{"intent": "deep"}])
        p, r = self.resolve("deep")
        self.assertEqual(p.returncode, 0, "an unmapped intent must never refuse to run")
        self.assertTrue(r["unmapped"])
        self.assertEqual(r["ran_at"], "balanced")
        core = json.loads(CORE_PACK.read_text())
        balanced = next(e["model"] for e in core["entries"] if e["intent"] == "balanced")
        self.assertEqual(r["model"], balanced)
        text = self.py(GT_MODEL, "resolve", "deep", "--vault", self.vault)
        self.assertIn("UNMAPPED", text.stdout)
        self.assertIn("running at balanced", text.stdout)
        self.assertIn("has no mapping", text.stderr)

    def test_code_review_plan_states_the_model(self):
        (self.packs / "review.mine.pack.json").write_text(json.dumps(pack(
            "review", [{"id": "dim", "title": "T", "rubric": "R", "model_intent": "fast"}])))
        tree = self.tmp / "tree"
        (tree / "scripts").mkdir(parents=True)
        (tree / "scripts" / "a.py").write_text("x = 1\n")
        p = self.py(REVIEW, "plan", tree, "--vault", self.vault, "--json")
        self.assertOk(p)
        row = json.loads(p.stdout)["plan"][0]
        self.assertIn("model_intent fast -> model", row["model_resolution"])
        self.assertTrue(row["model"])


class TheMappingPackIsTheOnlyPlace(unittest.TestCase):
    def shipped_dirs(self):
        out = []
        for d in sorted(REPO.glob("golden-thread*")):
            if d.is_dir():
                try:
                    out.append(GT if d.name == "golden-thread" else latest_version_dir(d))
                except (RuntimeError, OSError):
                    continue
        return out

    def test_no_literal_model_id_outside_the_mapping_pack(self):
        leaks = []
        for vd in self.shipped_dirs():
            for f in vd.rglob("*"):
                if not f.is_file() or f == CORE_PACK or "__pycache__" in f.parts:
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                for m in MODEL_ID.finditer(text):
                    leaks.append("%s: %s" % (f.relative_to(REPO), m.group(0)))
        self.assertEqual(leaks, [], "model ids belong in packs/core/model.intents.pack.json "
                                    "only:\n" + "\n".join(leaks))

    def test_no_shipped_skill_pins_a_model_in_its_frontmatter(self):
        pins = []
        for vd in self.shipped_dirs():
            for f in list(vd.rglob("SKILL.md")) + list(vd.rglob("agents/*.md")):
                head = f.read_text(encoding="utf-8").split("\n---", 1)[0]
                if re.search(r"(?m)^model:\s*\S", head):
                    pins.append(str(f.relative_to(REPO)))
        self.assertEqual(pins, [], "declare model_intent instead of pinning model:")

    def test_the_pack_records_when_each_name_was_verified(self):
        data = json.loads(CORE_PACK.read_text())
        self.assertEqual(data["slot"], "model")
        self.assertEqual(sorted(e["intent"] for e in data["entries"]),
                         ["balanced", "deep", "fast"])
        for e in data["entries"]:
            self.assertRegex(e.get("verified", ""), r"^\d{4}-\d{2}-\d{2}$", e)


if __name__ == "__main__":
    unittest.main()
