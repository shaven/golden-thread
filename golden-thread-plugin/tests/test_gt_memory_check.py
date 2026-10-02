"""gt_memory_check.py -- contradiction detection over memory notes, run by /gt:gt-work.

Request 2026-09-24-memory-contradiction-detection. Contract:
  * Two same-topic notes that disagree (a different value for the same key; enabled vs
    disabled about one subject) are surfaced as a pair: both paths, the conflicting
    sentences, and a resolution question.
  * No file is modified, under any flag.
  * No contradiction -> no output at all, exit 0 (the pass is silent in gt-work).
  * Only notes written/changed this session plus their close neighbours are compared --
    an unrelated contradicting pair the session never touched is not reported.
  * --work honours memory_contradiction_check=off.
"""
import hashlib
import json
import os
import unittest

from _harness import Sandbox, SCRIPTS

TOOL = SCRIPTS / "gt_memory_check.py"


def digest(root):
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            h.update(os.path.relpath(p, root).encode())
            h.update(open(p, "rb").read())
    return h.hexdigest()


class MemoryCheckTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.vault = self.tmp / "vault"
        self.mem = self.vault / "Projects" / "alpha" / "memory"
        self.mem.mkdir(parents=True)
        (self.mem / "MEMORY.md").write_text("- index\n", encoding="utf-8")

    def note(self, name, text):
        (self.mem / name).write_text(text, encoding="utf-8")

    def check(self, *args):
        return self.py(TOOL, "--vault", str(self.vault), "--project", "alpha", *args)

    def seed_conflict(self):
        self.note("auth-token-rotation.md",
                  "---\nname: auth token rotation\n---\n"
                  "token rotation: 24h\n\nThe auth middleware requires token rotation every day.\n")
        self.note("token-rotation-interval.md",
                  "---\nname: token rotation interval\n---\n"
                  "token rotation: disabled\n\nThe auth middleware token rotation is disabled "
                  "for service accounts.\n")

    def test_a_contradicting_pair_is_surfaced_with_sentences_and_a_question(self):
        self.seed_conflict()
        before = digest(self.vault)
        r = self.check("--changed", "token-rotation-interval.md")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Projects/alpha/memory/auth-token-rotation.md", r.stdout)
        self.assertIn("Projects/alpha/memory/token-rotation-interval.md", r.stdout)
        self.assertIn("token rotation: 24h", r.stdout)
        self.assertIn("token rotation: disabled", r.stdout)
        self.assertIn("Which is current?", r.stdout)
        self.assertEqual(digest(self.vault), before, "no file may be modified")

    def test_json_names_the_kinds(self):
        self.seed_conflict()
        d = json.loads(self.check("--changed", "auth-token-rotation.md", "--json").stdout)
        kinds = {c["kind"] for p in d["pairs"] for c in p["conflicts"]}
        self.assertIn("value", kinds)
        self.assertIn("polarity", kinds)
        self.assertFalse(d["pairs"][0]["linked"])

    def test_no_contradiction_is_silent(self):
        self.note("auth-token-rotation.md", "token rotation: 24h\n")
        self.note("token-rotation-history.md", "token rotation: 24h\nWe kept it daily.\n")
        r = self.check("--changed", "auth-token-rotation.md")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_only_changed_notes_and_their_neighbours_are_compared(self):
        self.seed_conflict()
        self.note("deploy-window.md", "Deploys are enabled on Fridays for the relay host.\n")
        self.note("deploy-freeze.md", "Deploys are disabled on Fridays for the relay host.\n")
        r = self.check("--changed", "deploy-window.md", "--json")
        pairs = json.loads(r.stdout)["pairs"]
        files = {f for p in pairs for f in (p["a"], p["b"])}
        self.assertIn("Projects/alpha/memory/deploy-freeze.md", files)
        self.assertNotIn("Projects/alpha/memory/auth-token-rotation.md", files,
                         "a pair the session did not touch is out of scope")

    def test_a_note_on_another_topic_is_not_a_neighbour(self):
        self.note("auth-token-rotation.md", "token rotation: 24h\n")
        self.note("coffee-machine.md", "token rotation: disabled\n")
        r = self.check("--changed", "auth-token-rotation.md")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_off_under_work_is_silent(self):
        self.seed_conflict()
        self.config(memory_contradiction_check="off")
        r = self.check("--work", "--changed", "auth-token-rotation.md")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        # A person running it by hand gets the check regardless.
        self.assertEqual(self.check("--changed", "auth-token-rotation.md").returncode, 1)

    def test_project_must_be_a_slug(self):
        r = self.py(TOOL, "--vault", str(self.vault), "--project", "../x")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
