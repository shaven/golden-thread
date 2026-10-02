"""gt_supersede.py -- supersession and expiry at read time (0.19.0).

Request 2026-10-02-supersession-and-expiry-at-read-time. A note may declare
`supersedes: <path>`; gt-open lists only the newest note of a chain, with a pointer to the
older ones; gt-query ranks expired items below current ones and says so; gt-lint reports a
`supersedes:` that points at nothing. Nothing is ever deleted: superseded and expired notes
stay on disk and stay readable.
"""
import json
import unittest

from _harness import Sandbox, SCRIPTS, GT

TOOL = SCRIPTS / "gt_supersede.py"
LINT = SCRIPTS / "gt_lint.py"


def note(path, body="x", **fm):
    path.parent.mkdir(parents=True, exist_ok=True)
    head = "".join("%s: %s\n" % kv for kv in fm.items())
    path.write_text("---\n%s---\n\n%s\n" % (head, body), encoding="utf-8")


class SupersedeBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        self.mem = self.vault / "Projects" / "alpha" / "memory"
        note(self.mem / "a-cache-ttl.md", "TTL is 60s", name="a")
        note(self.mem / "b-cache-ttl.md", "TTL is 300s", name="b", supersedes="a-cache-ttl.md")
        note(self.mem / "c-cache-ttl.md", "TTL is 900s", name="c",
             supersedes="Projects/alpha/memory/b-cache-ttl.md")
        note(self.mem / "d-deploy.md", "deploys on Tuesdays", name="d")
        note(self.mem / "e-old-host.md", "the build host is shbuild1", name="e",
             expired="2026-09-20")
        note(self.mem / "f-promo.md", "promo runs until", name="f", expires="2026-01-31")

    def tool(self, *args):
        p = self.py(TOOL, *args, "--vault", self.vault, "--json")
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        return p, json.loads(p.stdout) if p.stdout.strip() else None


class GtOpenListing(SupersedeBase):
    def test_only_the_newest_of_a_chain_is_listed_with_a_pointer_to_the_older(self):
        p, d = self.tool("listing", "Projects/alpha/memory")
        self.assertOk(p)
        shown = {r["path"]: r for r in d["notes"]}
        rel = "Projects/alpha/memory/%s"
        self.assertIn(rel % "c-cache-ttl.md", shown)
        self.assertNotIn(rel % "a-cache-ttl.md", shown)
        self.assertNotIn(rel % "b-cache-ttl.md", shown)
        self.assertEqual(shown[rel % "c-cache-ttl.md"]["older"],
                         [rel % "b-cache-ttl.md", rel % "a-cache-ttl.md"])
        self.assertIn(rel % "d-deploy.md", shown)

    def test_superseded_notes_stay_on_disk(self):
        self.tool("listing", "Projects/alpha/memory")
        self.assertTrue((self.mem / "a-cache-ttl.md").is_file())
        self.assertTrue((self.mem / "b-cache-ttl.md").is_file())


class GtQueryRanking(SupersedeBase):
    def test_expired_items_are_marked_and_ranked_below_current_ones(self):
        rel = "Projects/alpha/memory/%s"
        ask = [rel % "e-old-host.md", rel % "f-promo.md", rel % "d-deploy.md",
               rel % "a-cache-ttl.md", rel % "c-cache-ttl.md"]
        p, d = self.tool("rank", *ask)
        self.assertOk(p)
        order = [r["path"] for r in d["ranked"]]
        state = {r["path"]: r["state"] for r in d["ranked"]}
        self.assertEqual(order[:2], [rel % "d-deploy.md", rel % "c-cache-ttl.md"])
        self.assertEqual(state[rel % "e-old-host.md"], "expired")
        self.assertEqual(state[rel % "f-promo.md"], "expired")
        self.assertEqual(state[rel % "a-cache-ttl.md"], "superseded")
        self.assertEqual(order[-1], rel % "a-cache-ttl.md", "superseded ranks last")
        a = next(r for r in d["ranked"] if r["path"] == rel % "a-cache-ttl.md")
        self.assertEqual(a["current"], rel % "c-cache-ttl.md", "names the note that replaced it")


class LintReportsADanglingLink(SupersedeBase):
    def test_a_supersedes_link_to_a_missing_path_is_a_finding(self):
        note(self.mem / "g-broken.md", "y", name="g", supersedes="no-such-note.md")
        p, d = self.tool("dangling")
        self.assertEqual([x["path"] for x in d["dangling"]], ["Projects/alpha/memory/g-broken.md"])
        lint = self.py(LINT, "--vault", self.vault, "--json")
        findings = json.loads(lint.stdout)["findings"]
        hits = [f for f in findings if f.get("kind") == "supersedes-missing"]
        self.assertEqual([f["path"] for f in hits], ["Projects/alpha/memory/g-broken.md"])
        self.assertIn("no-such-note.md", hits[0]["message"])


class SkillsUseIt(unittest.TestCase):
    """The read-time half lives in skill prose; these pin that the prose says to use the tool."""

    def skill(self, name):
        return (GT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")

    def test_gt_open_lists_through_the_chain_helper(self):
        self.assertIn("gt_supersede.py", self.skill("gt-open"))
        self.assertIn("listing", self.skill("gt-open"))

    def test_gt_query_ranks_through_it(self):
        self.assertIn("gt_supersede.py", self.skill("gt-query"))
        self.assertIn("rank", self.skill("gt-query"))

    def test_gt_work_records_a_supersedes_link_when_it_resolves_a_contradiction(self):
        text = self.skill("gt-work")
        self.assertIn("supersedes:", text)
        self.assertIn("contradict", text.lower())


if __name__ == "__main__":
    unittest.main()
