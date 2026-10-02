"""skeptic_pass runs without agent_specialization (0.18.0,
2026-10-01-skeptic-pass-without-agent-specialization).

Until 0.17.11 the gt-work skeptic needed BOTH settings, so turning the skeptic on also handed
ingest and validation to specialist agents. Now each setting governs its own jobs:

  * skeptic_pass on, agent_specialization off -> gt-work spawns the skeptic; ingest and
    validation stay inline.
  * skeptic_pass off -> no skeptic, whatever agent_specialization says.
  * all four combinations are asserted for gt-work, gt-ingest and gt-validate.
  * the settings detail text and the gt-work skill describe the two independently.
  * a vault override of skeptic.json written before 0.18.0 (still listing
    agent_specialization) does not bring the old coupling back.
"""
import json
import re

from _harness import GT, SCRIPTS
from test_gt_agent_spec import AgentSpecBase, SPECS

TOOL = SCRIPTS / "gt_agent_spec.py"


class SkepticPassIndependentTest(AgentSpecBase):
    def actions(self):
        code = self.tree("wombat-src", ["main.py", "pkg/util.go"])
        return {"gt-work": self.resolve("--skill", "gt-work")["action"],
                "gt-validate": self.resolve("--skill", "gt-validate")["action"],
                "gt-ingest": self.resolve("--skill", "gt-ingest", "--path", str(code))["action"]}

    def test_all_four_combinations(self):
        expect = {
            ("off", "off"): {"gt-work": "inline", "gt-validate": "inline", "gt-ingest": "inline"},
            ("on", "off"): {"gt-work": "spawn", "gt-validate": "inline", "gt-ingest": "inline"},
            ("off", "on"): {"gt-work": "inline", "gt-validate": "spawn", "gt-ingest": "spawn"},
            ("on", "on"): {"gt-work": "spawn", "gt-validate": "spawn", "gt-ingest": "spawn"},
        }
        for (skeptic, spec), want in expect.items():
            self.settings(skeptic_pass=skeptic, agent_specialization=spec)
            self.assertEqual(want, self.actions(), (skeptic, spec))

    def test_skeptic_alone_spawns_the_shipped_skeptic_spec(self):
        self.settings(skeptic_pass="on")
        r = self.resolve("--skill", "gt-work")
        self.assertEqual(("skeptic", "spawn", None), (r["job"], r["action"], r["notice"]))
        self.assertTrue(r["spec"].endswith("skeptic.json"))
        self.assertEqual("off", r["agent_specialization"])

    def test_skeptic_off_says_why(self):
        self.settings(agent_specialization="on")
        r = self.resolve("--skill", "gt-work")
        self.assertEqual("inline", r["action"])
        self.assertIn("skeptic_pass is off", r["why"])

    def test_old_vault_override_does_not_restore_the_coupling(self):
        d = self.vault / "Projects" / "golden-thread" / "packs" / "agent-specs"
        d.mkdir(parents=True)
        spec = json.loads((SPECS / "skeptic.json").read_text())
        spec["requires_settings"] = ["agent_specialization", "skeptic_pass"]
        (d / "skeptic.json").write_text(json.dumps(spec))
        self.settings(skeptic_pass="on")
        r = self.resolve("--skill", "gt-work", "--vault", str(self.vault))
        self.assertEqual(("spawn", "vault"), (r["action"], r["source"]))

    def test_skeptic_spec_must_name_skeptic_pass(self):
        spec = json.loads((SPECS / "skeptic.json").read_text())
        spec["requires_settings"] = ["agent_specialization"]
        bad = self.tmp / "skeptic.json"
        bad.write_text(json.dumps(spec))
        p = self.py(TOOL, "validate", bad)
        self.assertEqual(1, p.returncode)
        self.assertIn("'skeptic_pass', the master switch", p.stdout)

    def test_docs_describe_the_settings_independently(self):
        text = (GT / "skills" / "gt-work" / "SKILL.md").read_text()
        head = next(l for l in text.splitlines() if l.startswith("#### Skeptic pass"))
        self.assertNotIn("agent_specialization", head)
        self.assertIn("independent of `agent_specialization`", " ".join(text.split()))
        p = self.py(SCRIPTS / "gt_settings.py", "explain", "skeptic_pass")
        self.assertOk(p)
        self.assertNotIn("needs agent_specialization", p.stdout)
        self.assertRegex(p.stdout, re.compile(r"Independent of\s+agent_specialization"))
