"""Cross-domain link suggestions during gt-work write-back (gt 0.18.1).

Request 2026-09-24-cross-domain-link-suggestions. Fixture: five Knowledge pages across two
domains (infrastructure, trading) plus one new page that shares tags with two of them.
Pinned:
  * candidates include a cross-domain one, and cross-domain candidates rank first;
  * each names the candidate, the reason (shared tag / keyword) and a link type;
  * a page already linked is never suggested;
  * no candidates -> no output, exit 0 (the skill skips silently);
  * suggest reads only the target page in full: every other page is read up to the end of
    its frontmatter (a keyword present only in a body is never matched);
  * suggest writes nothing; skipping (no apply) leaves every page unchanged;
  * apply writes exactly what was selected, bidirectionally, through the write queue, and
    never a link already present; --forward-only skips the back-link; apply refuses with no
    --to and refuses an unknown target;
  * gt-work runs the pass after a Knowledge write and asks before writing.
"""
import hashlib
import json
import sys
import unittest
from pathlib import Path

from _harness import Sandbox, SCRIPTS, GT, load_module

SUGGEST = SCRIPTS / "gt_link_suggest.py"


def page(title, tags, category="reference", body="", domain=None):
    d = "domain: %s\n" % domain if domain else ""
    return ("---\ntitle: %s\ncategory: %s\ntags: [%s]\nsources: []\ncreated: 2026-09-01\n"
            "updated: 2026-09-01\nstatus: growing\n%s---\n\n# %s\n\n%s\n"
            % (title, category, ", ".join(tags), d, title, body))


class LinkSuggest(Sandbox):
    def setUp(self):
        super().setUp()
        self.v = self.tmp / "vault"
        k = self.v / "Knowledge"
        k.mkdir(parents=True)
        (self.v / "Projects" / "golden-thread").mkdir(parents=True)
        pages = {
            # infrastructure
            "Proxmox Cluster Layout": page("Proxmox Cluster Layout", ["proxmox", "hosts"],
                                           "concept", "Nodes and storage."),
            "Backup Jobs": page("Backup Jobs", ["backups", "hosts"], "runbook",
                                "Nightly zebrafish rotation."),
            "Infrastructure Index": page("Infrastructure Index", ["hub", "hosts"], "reference",
                                         "See [[Proxmox Cluster Layout]]."),
            # trading
            "Order Router": page("Order Router", ["orders", "latency"], "concept", "Routing."),
            "Fill Latency Budget": page("Fill Latency Budget", ["latency", "orders"], "reference",
                                        "How long a fill may take."),
            # the new page: trading-ish content but listed under Infrastructure; shares
            # `latency` with two trading pages and `hosts` with infra pages.
            "Colocated Host Latency": page("Colocated Host Latency", ["latency", "hosts"],
                                           "runbook",
                                           "Measured latency from the colocated hosts. "
                                           "See [[Infrastructure Index]]."),
        }
        for name, text in pages.items():
            (k / (name + ".md")).write_text(text, encoding="utf-8")
        (self.v / "index.md").write_text(
            "# Index\n\n## Infrastructure\n\n- [[Proxmox Cluster Layout]]\n- [[Backup Jobs]]\n"
            "- [[Infrastructure Index]]\n- [[Colocated Host Latency]]\n\n## Trading\n\n"
            "- [[Order Router]]\n- [[Fill Latency Budget]]\n", encoding="utf-8")
        self.config(vault_path=str(self.v))
        self.page = "Knowledge/Colocated Host Latency.md"

    def digest(self):
        h = hashlib.sha256()
        for f in sorted((self.v / "Knowledge").glob("*.md")):
            h.update(f.name.encode() + f.read_bytes())
        return h.hexdigest()

    def suggest(self, page=None):
        p = self.py(SUGGEST, "suggest", "--vault", self.v, "--page", page or self.page, "--json")
        self.assertOk(p)
        return json.loads(p.stdout)["candidates"]

    def test_cross_domain_first_with_reason_and_type(self):
        c = self.suggest()
        self.assertTrue(c)
        self.assertLessEqual(len(c), 5)
        types = [x["type"] for x in c]
        self.assertIn("cross-domain", types)
        first_non = next((i for i, t in enumerate(types) if t != "cross-domain"), len(types))
        self.assertTrue(all(t != "cross-domain" for t in types[first_non:]),
                        "cross-domain candidates must rank first: %s" % types)
        targets = {x["target"] for x in c}
        self.assertTrue({"Order Router", "Fill Latency Budget"} <= targets, c)
        for x in c:
            self.assertTrue(x["title"] and x["reason"] and x["type"], x)
            self.assertRegex(x["reason"], r"shared tag|title keyword")
        same = {x["target"]: x["type"] for x in c if x["type"] != "cross-domain"}
        self.assertEqual(same.get("Proxmox Cluster Layout"), "upstream", c)  # concept vs runbook
        self.assertEqual(same.get("Backup Jobs"), "sibling", c)

    def test_already_linked_pages_are_not_suggested(self):
        self.assertNotIn("Infrastructure Index", {x["target"] for x in self.suggest()})

    def test_no_candidates_is_silent(self):
        (self.v / "Knowledge" / "Lonely Page.md").write_text(
            page("Lonely Page", ["zzz-unique"], body="nothing shared"), encoding="utf-8")
        p = self.py(SUGGEST, "suggest", "--vault", self.v, "--page", "Knowledge/Lonely Page.md")
        self.assertOk(p)
        self.assertEqual(p.stdout.strip(), "")

    def test_reads_only_frontmatter_of_other_pages(self):
        # `zebrafish` is only in Backup Jobs' BODY. A new page whose title says zebrafish and
        # shares nothing else must not be matched to it.
        (self.v / "Knowledge" / "Zebrafish Tank.md").write_text(
            page("Zebrafish Tank", ["aquarium"]), encoding="utf-8")
        self.assertEqual(self.suggest("Knowledge/Zebrafish Tank.md"), [])
        m = load_module(SUGGEST, "gt_link_suggest_under_test")
        reads = []
        orig = Path.read_text

        def spy(self_, *a, **kw):
            reads.append(self_.name)
            return orig(self_, *a, **kw)
        Path.read_text = spy
        try:
            m.suggest(self.v, self.v / self.page)
        finally:
            Path.read_text = orig
        self.assertEqual(sorted(set(reads)), sorted({"Colocated Host Latency.md", "index.md"}),
                         "only the page itself (and index.md) may be read whole")

    def test_suggest_and_skip_write_nothing(self):
        before = self.digest()
        self.suggest()
        self.py(SUGGEST, "suggest", "--vault", self.v, "--page", self.page)
        self.assertEqual(self.digest(), before)
        q = self.v / "Projects" / "golden-thread" / "spool" / "queue"
        self.assertFalse(q.exists() and list(q.glob("*.json")))

    def test_apply_needs_a_selection(self):
        p = self.py(SUGGEST, "apply", "--vault", self.v, "--page", self.page)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        p = self.py(SUGGEST, "apply", "--vault", self.v, "--page", self.page, "--to", "Nope")
        self.assertEqual(p.returncode, 1)
        self.assertIn("REFUSED", p.stderr)

    def test_apply_writes_bidirectional_links_through_the_queue(self):
        before = self.digest()
        d = self.py(SUGGEST, "apply", "--vault", self.v, "--page", self.page, "--to",
                    "Order Router", "--dry-run", "--json")
        self.assertOk(d)
        self.assertEqual(len(json.loads(d.stdout)["results"]), 2)
        self.assertEqual(self.digest(), before, "--dry-run wrote")

        p = self.py(SUGGEST, "apply", "--vault", self.v, "--page", self.page, "--to",
                    "Order Router", "--json")
        self.assertOk(p)
        res = json.loads(p.stdout)["results"]
        self.assertEqual({r["op"] for r in res}, {"append"})
        self.assertEqual({r["decision"] for r in res}, {"apply"}, res)
        new = (self.v / self.page).read_text(encoding="utf-8")
        other = (self.v / "Knowledge" / "Order Router.md").read_text(encoding="utf-8")
        self.assertIn("## Related", new)
        self.assertIn("[[Order Router]]", new)
        self.assertIn("[[Colocated Host Latency]]", other)
        untouched = (self.v / "Knowledge" / "Fill Latency Budget.md").read_text()
        self.assertNotIn("Colocated", untouched, "an unselected candidate was written")
        # already present now: nothing written twice
        again = self.py(SUGGEST, "apply", "--vault", self.v, "--page", self.page, "--to",
                        "Order Router", "--json")
        self.assertOk(again)
        self.assertEqual(json.loads(again.stdout)["results"], [])
        self.assertEqual(new.count("[[Order Router]]"), 1)
        self.assertNotIn("Order Router", {x["target"] for x in self.suggest()})

    def test_forward_only(self):
        p = self.py(SUGGEST, "apply", "--vault", self.v, "--page", self.page, "--to",
                    "Backup Jobs", "--forward-only")
        self.assertOk(p)
        self.assertIn("[[Backup Jobs]]", (self.v / self.page).read_text())
        self.assertNotIn("Colocated", (self.v / "Knowledge" / "Backup Jobs.md").read_text())

    def test_gt_work_runs_the_pass_and_asks(self):
        text = (GT / "skills" / "gt-work" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("gt_link_suggest.py suggest", text)
        self.assertIn("gt_link_suggest.py apply", text)
        self.assertIn("Write only what the user picked", text)
        self.assertIn("skip the pass silently", text)


if __name__ == "__main__":
    unittest.main()
