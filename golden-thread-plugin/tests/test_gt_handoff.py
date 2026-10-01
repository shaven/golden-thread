"""gt_handoff.py -- gathering what the next session needs without inventing what it cannot know.

Contract:
  * Every fact carries a SOURCE and a VERIFICATION label (Core rule 10). A handoff is exactly
    where an unverified claim gets promoted to settled fact by appearing in a formal-looking
    document -- "the tests pass" becomes folklore the moment it is written without saying who
    ran them and when.
  * The design narrative is NOT written by the script. A document that reads finished but is
    not hands the next session false confidence, which is worse than handing it none.
  * Observed git state is labelled `verified`; quoted document content is `unverified`, because
    the script saw the text, not the truth of it.
  * The vault is always named explicitly, never inferred.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import GT, Sandbox, SCRIPTS, TOOLS

SCRIPT = GT / "scripts" / "gt_handoff.py"


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gt-ho-"))
        self.vault = self.tmp / "vault"
        self.proj = self.vault / "Projects" / "alpha"
        self.proj.mkdir(parents=True)
        (self.proj / "README.md").write_text(
            "---\ntype: project\n---\n\n# Alpha\n\n"
            "Alpha exists to replace the nightly batch with a streaming pipeline.\n\n"
            "## Tasks\n\n- [ ] P1 wire the consumer\n- [x] P2 pick the broker\n",
            encoding="utf-8")
        (self.proj / "decisions.md").write_text(
            "# Decisions\n\n## ADR-1 Use Kafka\n\ntext\n\n## ADR-2 Drop the batch\n\ntext\n",
            encoding="utf-8")

    def run_ho(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "--vault", str(self.vault),
                               "--project", "alpha"] + list(args),
                              capture_output=True, text=True)

    def facts(self, *args):
        r = self.run_ho("--json", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_every_fact_carries_a_source_and_a_verification_label(self):
        d = self.facts()
        self.assertTrue(d["facts"], "expected some facts")
        for name, f in d["facts"].items():
            self.assertIn("source", f, name)
            self.assertIn(f.get("verification"), ("verified", "unverified", "unknown"), name)

    def test_quoted_document_content_is_never_labelled_verified(self):
        """The script read the text; it did not establish that the text is true."""
        d = self.facts()
        for key in ("goal", "open_tasks", "recent_decisions"):
            if key in d["facts"]:
                self.assertEqual(d["facts"][key]["verification"], "unverified", key)

    def test_observed_git_state_is_labelled_verified(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True)
        d = self.facts("--repo", str(repo))
        if "branch" in d["facts"]:
            self.assertEqual(d["facts"]["branch"]["verification"], "verified")

    def test_the_design_section_is_a_placeholder_the_script_refuses_to_fill(self):
        d = self.facts()
        self.assertIn("TO BE WRITTEN BY THE SESSION THAT DID THE WORK", d["markdown"])
        self.assertIn("INCOMPLETE", d["markdown"])

    def test_it_states_what_it_cannot_know(self):
        d = self.facts()
        self.assertTrue(d["cannot_know"])
        joined = " ".join(d["cannot_know"]).lower()
        self.assertIn("tests", joined, "must not let the next session assume tests pass")
        self.assertIn("did not run them", joined)

    def test_the_tests_are_only_ever_mentioned_as_an_open_question(self):
        """It may ASK whether the tests pass -- it must never assert that they do."""
        d = self.facts()
        for line in d["markdown"].split("\n"):
            low = line.lower()
            if "tests" in low and "pass" in low:
                self.assertTrue(low.lstrip().startswith("- [ ]") or "?" in low,
                                "tests/pass may only appear as an unanswered question: %r"
                                % line)

    def test_goal_and_tasks_are_taken_from_the_readme(self):
        d = self.facts()
        self.assertIn("streaming pipeline", d["facts"]["goal"]["value"])
        tasks = d["facts"]["open_tasks"]["value"]
        self.assertTrue(any("wire the consumer" in t for t in tasks))

    def test_writes_under_the_project_by_default(self):
        r = self.run_ho()
        self.assertEqual(r.returncode, 0, r.stderr)
        written = list((self.proj / "handoff").glob("*.md"))
        self.assertEqual(len(written), 1, r.stdout)
        self.assertIn("Handoff: alpha", written[0].read_text(encoding="utf-8"))

    def test_says_out_loud_that_the_document_is_not_finished(self):
        r = self.run_ho()
        self.assertIn("NOT finished", r.stdout)

    def test_vault_and_project_are_required(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--vault", r.stderr)

    def test_a_missing_project_is_an_error_not_an_empty_handoff(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--vault", str(self.vault),
                            "--project", "nosuch"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)
        self.assertIn("no README.md", r.stderr)


class ThroughTheQueue(Sandbox):
    """0.17.11: a handoff inside the vault is a queued `create`, applied by the broker."""

    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        self.proj = self.vault / "Projects" / "alpha"
        self.proj.mkdir(parents=True)
        (self.proj / "README.md").write_text("# Alpha\n\nThe goal.\n\n## Tasks\n\n")
        tools = self.vault / "Projects" / "golden-thread" / "tools"
        tools.mkdir(parents=True)
        for t in ("gt_task.py", "gt_tasks.py", "gt_session.py"):
            shutil.copy(TOOLS / t, tools / t)
        self.sess = tools / "gt_session.py"
        self.spool = self.vault / "Projects" / "golden-thread" / "spool"
        self.out = self.proj / "handoff" / "the-handoff.md"
        self.rel = "Projects/alpha/handoff/the-handoff.md"

    def ho(self, *args):
        return self.py(SCRIPT, "--vault", self.vault, "--project", "alpha",
                       "--out", self.out, *args)

    def rows(self):
        out = []
        for f in sorted((self.spool / "broker").glob("log-*.jsonl")):
            out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return out

    def queued(self):
        q = self.spool / "queue"
        return sorted(q.glob("*.json")) if q.is_dir() else []

    def test_the_handoff_arrives_as_a_create_through_the_broker(self):
        self.assertOk(self.ho())
        self.assertIn("Handoff: alpha", self.out.read_text())
        (row,) = self.rows()
        self.assertEqual((row["path"], row["op"], row["decision"]), (self.rel, "create", "apply"))
        self.assertEqual(self.queued(), [])

    def test_without_force_an_existing_handoff_is_refused_before_anything_is_queued(self):
        self.out.parent.mkdir(parents=True)
        self.out.write_text("someone's record\n")
        p = self.ho()
        self.assertEqual(p.returncode, 3)
        self.assertIn("--force", p.stderr)
        self.assertEqual(self.out.read_text(), "someone's record\n")
        self.assertEqual((self.queued(), self.rows()), ([], []))

    def test_force_over_an_existing_handoff_is_escalated_never_overwritten(self):
        self.out.parent.mkdir(parents=True)
        self.out.write_text("someone's record\n")
        p = self.ho("--force")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("Nothing was replaced", p.stderr)
        self.assertEqual(self.out.read_text(), "someone's record\n")
        (row,) = self.rows()
        self.assertEqual(row["decision"], "escalate")
        conflict = (self.vault / row["conflict"]).read_text()
        self.assertIn("someone's record", conflict)
        self.assertIn("Handoff: alpha", conflict, "the new version must be kept for the owner")
        self.assertIn("#conflict", (self.proj / "README.md").read_text())
        self.assertEqual(self.queued(), [])

    def test_a_handoff_path_claimed_by_a_live_session_stays_queued(self):
        env = {"CLAUDE_SESSION_ID": "holder", "CLAUDE_PID": str(os.getpid())}
        self.assertOk(self.py(self.sess, "--vault", self.vault, "register", "--task", "t",
                              "--files", self.rel, env=env))
        p = self.ho()
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("queued, NOT written yet", p.stderr)
        self.assertIn("holder", p.stderr)
        self.assertFalse(self.out.exists())
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.rows()[-1]["decision"], "held")

    def test_out_outside_the_vault_is_written_directly(self):
        out = self.tmp / "elsewhere" / "h.md"
        self.assertOk(self.py(SCRIPT, "--vault", self.vault, "--project", "alpha", "--out", out))
        self.assertTrue(out.is_file())
        self.assertEqual((self.queued(), self.rows()), ([], []))


if __name__ == "__main__":
    unittest.main()
