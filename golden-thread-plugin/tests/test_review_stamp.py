"""Review scheduling by last READ, not last edit (gt 0.18.1, gt-wiki 0.2.5).

Request 2026-09-24-srs-review-scheduling. Pinned here:
  * wiki_lint's review-due runs from the NEWER of `last_reviewed` and `updated`:
      last_reviewed today, updated long ago        -> not review-due
      last_reviewed 100 days ago (updated older)   -> review-due
      no last_reviewed, updated 100 days ago       -> review-due, message unchanged
  * decision / kind: principle pages stay exempt even with an old last_reviewed;
  * gt_review_stamp.py writes `last_reviewed: <today>` THROUGH THE WRITE QUEUE (the request
    file names op set-property), never touches `updated:`, skips an already-stamped page,
    refuses pages outside Knowledge/ and missing pages, honours --dry-run and the
    `review_stamp` setting; after a stamp the lint stops firing for that page;
  * gt-query stamps after the answer (its step comes after the answer/log steps) and gt-open
    stamps Knowledge pages it loaded.
"""
import datetime
import json
import unittest

from _harness import Sandbox, SCRIPTS, WIKI_SCRIPTS, GT

LINT = WIKI_SCRIPTS / "wiki_lint.py"
STAMP = SCRIPTS / "gt_review_stamp.py"
TODAY = datetime.date.today()
D100 = (TODAY - datetime.timedelta(days=100)).isoformat()
D200 = (TODAY - datetime.timedelta(days=200)).isoformat()


def page(title, updated, extra="", category="concept"):
    return ("---\ntitle: %s\ncategory: %s\nsources: []\ncreated: %s\nupdated: %s\n"
            "status: growing\n%s---\n\n# %s\n\nBody of %s.\n"
            % (title, category, D200, updated, extra, title, title))


class ReviewStamp(Sandbox):
    def setUp(self):
        super().setUp()
        self.v = self.tmp / "vault"
        (self.v / "Knowledge").mkdir(parents=True)
        (self.v / "Sources").mkdir()
        (self.v / "Projects" / "golden-thread").mkdir(parents=True)
        self.write("Fresh Read", page("Fresh Read", D200, "last_reviewed: %s\n" % TODAY.isoformat()))
        self.write("Old Read", page("Old Read", D200, "last_reviewed: %s\n" % D100))
        self.write("Never Read", page("Never Read", D100))
        self.write("A Decision", page("A Decision", D200, "last_reviewed: %s\n" % D200,
                                      category="decision"))
        self.write("A Principle", page("A Principle", D200, "kind: principle\n"))
        (self.v / "index.md").write_text("# Index\n", encoding="utf-8")
        self.config(vault_path=str(self.v))

    def write(self, name, text):
        (self.v / "Knowledge" / (name + ".md")).write_text(text, encoding="utf-8")

    def text(self, name):
        return (self.v / "Knowledge" / (name + ".md")).read_text(encoding="utf-8")

    def review_due(self):
        p = self.py(LINT, self.v, "--json")
        self.assertIn(p.returncode, (0, 1), p.stderr)
        return json.loads(p.stdout)["findings"]["review-due"]

    def due_files(self):
        return sorted(x.split(" (")[0] for x in self.review_due())

    # -- the lint ------------------------------------------------------------------------
    def test_lint_prefers_last_reviewed(self):
        due = self.review_due()
        files = self.due_files()
        self.assertNotIn("Fresh Read.md", files, "read today, yet flagged on its old `updated`")
        self.assertIn("Old Read.md", files)
        self.assertIn("Never Read.md", files)
        self.assertIn("Old Read.md (last review %s)" % D100, due)
        self.assertIn("Never Read.md (last touch %s)" % D100, due,
                      "a page with no last_reviewed must report exactly as before")

    def test_principles_stay_exempt(self):
        files = self.due_files()
        self.assertNotIn("A Decision.md", files)
        self.assertNotIn("A Principle.md", files)

    def test_a_newer_edit_counts_as_a_review(self):
        self.write("Edited", page("Edited", TODAY.isoformat(), "last_reviewed: %s\n" % D200))
        self.assertNotIn("Edited.md", self.due_files())

    def test_a_malformed_last_reviewed_falls_back(self):
        self.write("Garbled", page("Garbled", D100, "last_reviewed: soon\n"))
        self.assertIn("Garbled.md (last touch %s)" % D100, self.review_due())

    # -- the stamp -----------------------------------------------------------------------
    def test_stamp_goes_through_the_queue_and_clears_the_finding(self):
        before = self.text("Never Read")
        d = self.py(STAMP, "--vault", self.v, "--dry-run", "Never Read")
        self.assertOk(d)
        self.assertEqual(self.text("Never Read"), before, "--dry-run wrote")
        queue = self.v / "Projects" / "golden-thread" / "spool" / "queue"
        self.assertFalse(queue.exists() and list(queue.glob("*.json")), "--dry-run queued")

        p = self.py(STAMP, "--vault", self.v, "Knowledge/Never Read.md", "--json")
        self.assertOk(p)
        out = json.loads(p.stdout)
        self.assertEqual([r["op"] for r in out["results"]], ["set-property"],
                         "the stamp must be a write-queue set-property request")
        self.assertEqual(out["results"][0]["decision"], "apply", out)
        after = self.text("Never Read")
        self.assertIn("last_reviewed: %s" % TODAY.isoformat(), after)
        self.assertIn("updated: %s" % D100, after, "a review stamp must not touch `updated`")
        self.assertIn("Body of Never Read.", after)
        self.assertNotIn("Never Read.md", self.due_files())

        again = self.py(STAMP, "--vault", self.v, "Never Read", "--json")
        self.assertOk(again)
        self.assertEqual(json.loads(again.stdout)["skipped"], ["Knowledge/Never Read.md"])

    def test_stamp_refuses_what_is_not_a_knowledge_page(self):
        (self.v / "Projects" / "golden-thread" / "research.md").write_text("# r\n")
        p = self.py(STAMP, "--vault", self.v, "Projects/golden-thread/research.md",
                    "Knowledge/Missing.md", "_template")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("not a Knowledge page", p.stderr)
        self.assertIn("does not exist", p.stderr)
        self.assertIn("template", p.stderr)

    def test_the_setting_turns_it_off(self):
        self.config(vault_path=str(self.v), review_stamp="off")
        before = self.text("Never Read")
        p = self.py(STAMP, "--vault", self.v, "Never Read")
        self.assertOk(p)
        self.assertIn("off", p.stdout)
        self.assertEqual(self.text("Never Read"), before)

    def test_the_setting_is_registered_on_by_default(self):
        import sys
        sys.path.insert(0, str(SCRIPTS))
        import gt_settings
        self.assertEqual(gt_settings.SETTINGS["review_stamp"]["default"], "on")

    # -- the skills ----------------------------------------------------------------------
    def test_gt_query_stamps_after_answering(self):
        text = (GT / "skills" / "gt-query" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("gt_review_stamp.py", text)
        self.assertLess(text.index("Step 7"), text.index("gt_review_stamp.py"),
                        "the stamp must come after the answer and log, not before")
        self.assertIn("--vault", text[text.index("gt_review_stamp.py"):][:120])

    def test_gt_open_stamps_loaded_knowledge_pages(self):
        text = (GT / "skills" / "gt-open" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("gt_review_stamp.py", text)
        self.assertIn("last_reviewed", text)


if __name__ == "__main__":
    unittest.main()
