"""gt_promote_detect.py -- promotion candidates /gt:gt-work can show without being asked.

Request 2026-09-24-promotion-candidate-detection. Contract:
  * memory -> research: a memory note changed in 3+ of the last 5 commits to the project's
    memory/ is listed, with a justification and destination.
  * research -> Knowledge: research.md sections in two projects with >= the overlap threshold
    (setting promotion_overlap, default 80) are listed.
  * global demotion: gt_lint's global-scope-leak finding, reused rather than reimplemented.
  * No candidates -> no output, exit 0. Nothing is ever written.
  * --work honours promotion_candidates=off.
"""
import hashlib
import json
import os
import shutil
import subprocess
import unittest

from _harness import Sandbox, SCRIPTS

TOOL = SCRIPTS / "gt_promote_detect.py"

SHARED = ("The relay host drops idle websocket connections after five minutes so every "
          "client must send a heartbeat ping at least once a minute or reconnect with "
          "exponential backoff capped at thirty seconds")


def digest(root):
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            h.update(os.path.relpath(p, root).encode())
            h.update(open(p, "rb").read())
    return h.hexdigest()


class PromoteDetectTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.vault = self.tmp / "vault"
        for slug in ("alpha", "beta"):
            (self.vault / "Projects" / slug / "memory").mkdir(parents=True)
            (self.vault / "Projects" / slug / "README.md").write_text("# %s\n" % slug,
                                                                      encoding="utf-8")
        (self.vault / "global-memory").mkdir()

    def write(self, rel, text):
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def detect(self, *args):
        return self.py(TOOL, "--vault", str(self.vault), *args)

    def cands(self, *args):
        r = self.detect("--json", *args)
        self.assertIn(r.returncode, (0, 1), r.stderr)
        return json.loads(r.stdout)["candidates"]

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.vault)] + list(args), check=True,
                       capture_output=True, env=self.env)

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_a_memory_note_changed_in_4_of_5_commits_is_a_research_candidate(self):
        self.git("init", "-q")
        note = "Projects/alpha/memory/relay.md"
        for i in range(5):
            if i != 2:
                self.write(note, "relay fact, revision %d\n" % i)
            self.write("Projects/alpha/memory/other-%d.md" % i, "x\n")
            self.git("add", "-A")
            self.git("commit", "-q", "-m", "c%d" % i)
        got = [c for c in self.cands() if c["signal"] == "memory-to-research"]
        self.assertEqual([c["path"] for c in got], [note])
        self.assertIn("4 of the last 5", got[0]["why"])
        self.assertIn("research.md", got[0]["destination"])

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_a_note_changed_twice_is_not_a_candidate(self):
        self.git("init", "-q")
        for i in range(5):
            if i < 2:
                self.write("Projects/alpha/memory/relay.md", "rev %d\n" % i)
            self.write("Projects/alpha/memory/other-%d.md" % i, "x\n")
            self.git("add", "-A")
            self.git("commit", "-q", "-m", "c%d" % i)
        self.assertEqual([c for c in self.cands() if c["signal"] == "memory-to-research"], [])

    def test_overlapping_research_sections_across_projects_are_a_knowledge_candidate(self):
        self.write("Projects/alpha/research.md",
                   "# R\n\n## 2026-09-01: relay idles\n\n%s.\n" % SHARED)
        self.write("Projects/beta/research.md",
                   "# R\n\n## 2026-09-02: websocket drops\n\n%s, which we saw too.\n" % SHARED)
        got = [c for c in self.cands() if c["signal"] == "research-to-knowledge"]
        self.assertEqual(len(got), 1, got)
        self.assertGreaterEqual(got[0]["overlap_pct"], 80)
        self.assertIn("Knowledge/", got[0]["destination"])
        self.assertIn("overlap", got[0]["why"])

    def test_the_overlap_threshold_is_the_setting(self):
        self.write("Projects/alpha/research.md", "## a\n\n%s.\n" % SHARED)
        half = " ".join(SHARED.split()[:14]) + " " + " ".join(
            "unrelated%d" % i for i in range(14))
        self.write("Projects/beta/research.md", "## b\n\n%s.\n" % half)
        self.assertEqual([c for c in self.cands() if c["signal"] == "research-to-knowledge"],
                         [])
        self.config(promotion_overlap="60")
        self.assertEqual(len([c for c in self.cands()
                              if c["signal"] == "research-to-knowledge"]), 0)
        self.assertEqual(len([c for c in self.cands("--threshold", "40")
                              if c["signal"] == "research-to-knowledge"]), 1)

    def test_a_global_naming_a_project_is_a_demotion_candidate_from_gt_lint(self):
        self.write("global-memory/alpha-only.md", "The alpha deploy needs a warm cache.\n")
        got = [c for c in self.cands() if c["signal"] == "global-demotion"]
        self.assertEqual([c["path"] for c in got], ["global-memory/alpha-only.md"])
        self.assertIn("global-scope-leak", got[0]["why"])
        self.assertEqual(got[0]["destination"], "Projects/alpha/memory/")

    def test_no_candidates_prints_nothing(self):
        self.write("global-memory/general.md", "Use absolute dates in memory.\n")
        r = self.detect()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_it_never_writes_and_off_under_work_is_silent(self):
        self.write("global-memory/alpha-only.md", "The alpha deploy needs a warm cache.\n")
        before = digest(self.vault)
        r = self.detect()
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing moves without your yes", r.stdout)
        self.config(promotion_candidates="off")
        r = self.detect("--work")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertEqual(digest(self.vault), before)


if __name__ == "__main__":
    unittest.main()
