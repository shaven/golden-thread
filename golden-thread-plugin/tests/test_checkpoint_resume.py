"""Batch checkpoints and resume for gt-scan and gt-ingest (gt 0.18.1).

Request 2026-09-24-batch-skill-checkpoint-resume, with the owner's triage correction: resume
must work ACROSS sessions. Pinned:
  * gt_scan.py writes a checkpoint before starting and after each item; an interruption
    (GT_CHECKPOINT_ABORT_AFTER, exit 75) leaves it in place;
  * `--resume` continues from the last completed item: files already scanned are NOT rescanned
    (an edit made to them after the interruption is not seen), files after it are, and the
    prior results are merged into the final report;
  * the checkpoint is deleted on completion;
  * a run without --resume starts fresh regardless of any checkpoint;
  * a checkpoint written under one session id is found from another (`gt_checkpoint find`);
  * the file is JSON {items, last_completed_index, results}, written by atomic rename;
  * gt_ingest.py: the candidates are the batch -- checkpoint at scan, --done per item in order,
    --resume prints only what is left (from a "new session"), completion merges every result
    and deletes the checkpoint; --no-checkpoint writes nothing;
  * checkpoints older than 7 days are pruned by `gt_checkpoint prune` and by gt_session.py
    register;
  * the gt-scan and gt-ingest skills offer the resume at start.
"""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from _harness import Sandbox, SCRIPTS, TOOLS, GT
from test_gt_scan import ScanBase

CK = SCRIPTS / "gt_checkpoint.py"
INGEST = SCRIPTS / "gt_ingest.py"
GOOD = "def good_name_%d():\n    return %d\n"


class ScanResume(ScanBase):
    def setUp(self):
        super().setUp()
        self.all_packs()
        for i in range(10):
            self.write("f%02d.py" % i, GOOD % (i, i))
        # f01 has a finding that the FIRST half will record
        self.write("f01.py", "def PriorBad():\n    return 1\n")

    def spool(self, tool="scan"):
        return self.vault / "Projects" / "golden-thread" / "spool" / tool

    def agg(self, *args, env=None):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("GT_CHECKPOINT", "CLAUDE"))}
        e.update(env or {})
        return subprocess.run([sys.executable, str(self.release / "scripts" / "gt_scan.py"),
                               str(self.tree), "--vault", str(self.vault)] + list(args),
                              capture_output=True, text=True, env=e)

    def interrupt(self, session="sess-one"):
        r = self.agg(env={"GT_CHECKPOINT_ABORT_AFTER": "5", "CLAUDE_CODE_SESSION_ID": session})
        self.assertEqual(r.returncode, 75, r.stdout + r.stderr)
        top = [p for p in self.spool().glob("*.progress.json") if ".language." not in p.name]
        leaf = list(self.spool().glob("*.language.progress.json"))
        self.assertEqual(len(top), 1, list(self.spool().iterdir()))
        self.assertEqual(len(leaf), 1)
        return top[0], leaf[0]

    def test_checkpoint_shape_after_interruption(self):
        top, leaf = self.interrupt()
        d = json.loads(leaf.read_text())
        self.assertEqual(len(d["items"]), 10)
        self.assertEqual(d["last_completed_index"], 4, "5 files done -> index 4")
        self.assertEqual(len(d["results"]), 5)
        t = json.loads(top.read_text())
        self.assertEqual(sorted(t["items"]), ["code", "language"])
        li = t["items"].index("language")
        self.assertEqual(t["last_completed_index"], li - 1,
                         "every member before `language` is done; `language` is not")
        self.assertFalse(list(self.spool().glob(".ckpt.*")), "a temp file was left behind")

    def test_resume_skips_done_items_scans_the_rest_and_merges(self):
        top, leaf = self.interrupt()
        # After the interruption: fix f01 (already scanned) and break f00 (already scanned)
        # and f07 (not yet scanned). A resume must not rescan f00/f01, and must scan f07.
        self.write("f01.py", GOOD % (1, 1))
        self.write("f00.py", "def EarlyBad():\n    return 0\n")
        self.write("f07.py", "def LateBad():\n    return 7\n")
        r = self.agg("--resume", str(top), "--json")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(sorted(d["ran"]), ["code", "language"])
        lang = next(x for x in d["results"] if x["member"] == "language")["output"]
        self.assertIn("LateBad", lang, "a file after the checkpoint was not scanned")
        self.assertIn("PriorBad", lang, "the prior results were not merged in")
        self.assertNotIn("EarlyBad", lang, "a file before the checkpoint was rescanned")
        self.assertFalse(list(self.spool().glob("*.progress.json")) if self.spool().exists()
                         else [], "the checkpoint survived a completed run")

    def test_a_plain_run_starts_fresh(self):
        top, _leaf = self.interrupt()
        self.write("f00.py", "def EarlyBad():\n    return 0\n")
        r = self.agg("--json")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        lang = next(x for x in json.loads(r.stdout)["results"] if x["member"] == "language")
        self.assertIn("EarlyBad", lang["output"], "a run without --resume skipped files")
        self.assertTrue(top.exists(), "a fresh run consumed somebody else's checkpoint")

    def test_found_from_another_session(self):
        top, _leaf = self.interrupt(session="sess-one")
        e = dict(os.environ, CLAUDE_CODE_SESSION_ID="sess-two")
        r = subprocess.run([sys.executable, str(CK), "find", "--tool", "scan", "--target",
                            str(self.tree), "--vault", str(self.vault), "--json"],
                           capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(r.stdout)
        self.assertEqual([x["path"] for x in rows], [str(top)])
        self.assertEqual(rows[0]["session"], "sess-one")
        t = json.loads(top.read_text())
        self.assertEqual((rows[0]["done"], rows[0]["total"]),
                         (t["items"].index("language"), 2))

    def test_resume_refuses_a_checkpoint_for_another_tree(self):
        top, _leaf = self.interrupt()
        other = self.tmp / "other"
        other.mkdir()
        r = subprocess.run([sys.executable, str(self.release / "scripts" / "gt_scan.py"),
                            str(other), "--vault", str(self.vault), "--resume", str(top)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("cannot resume", r.stderr)

    def test_no_checkpoint_writes_nothing(self):
        r = self.agg("--no-checkpoint")
        self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
        self.assertFalse(self.spool().exists() and list(self.spool().iterdir()))


class IngestResume(Sandbox):
    def setUp(self):
        super().setUp()
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        for n in ("ARCHITECTURE.md", "NOTES.md", "IDEAS.md"):
            (self.proj / n).write_text("# %s\n\nsome text for %s\n" % (n, n))
        self.vault = self.tmp / "vault"
        (self.vault / "Projects" / "golden-thread").mkdir(parents=True)

    def spool(self):
        return self.vault / "Projects" / "golden-thread" / "spool" / "ingest"

    def ingest(self, *args, session="s-a"):
        return self.py(INGEST, *args, env={"CLAUDE_CODE_SESSION_ID": session})

    def test_full_cycle_across_sessions(self):
        p = self.ingest(self.proj, "--json", "--vault", self.vault)
        self.assertOk(p)
        cands = json.loads(p.stdout)
        self.assertEqual([c["index"] for c in cands], [0, 1, 2])
        line = [x for x in p.stderr.splitlines() if x.startswith("checkpoint: ")]
        self.assertTrue(line, p.stderr)
        ck = line[0].split("checkpoint: ", 1)[1].rsplit(" (", 1)[0]
        self.assertTrue(Path(ck).is_file())
        self.assertEqual(Path(ck).parent, self.spool())

        self.assertOk(self.ingest("--done", ck, "--index", "0", "--result", "Projects/x/design.md"))
        bad = self.ingest("--done", ck, "--index", "2", "--result", "x", session="s-b")
        self.assertEqual(bad.returncode, 1, "out-of-order completion was accepted")
        # a NEW session picks it up
        r = self.ingest("--resume", ck, "--json", session="s-b")
        self.assertOk(r)
        left = json.loads(r.stdout)
        self.assertEqual([c["index"] for c in left], [1, 2])
        self.assertIn("done #0", r.stderr)
        self.assertOk(self.ingest("--done", ck, "--index", "1", "--result", "research.md",
                                  session="s-b"))
        fin = self.ingest("--done", ck, "--index", "2", "--result", "INBOX.md", "--json",
                          session="s-b")
        self.assertOk(fin)
        out = json.loads(fin.stdout)
        self.assertTrue(out["complete"])
        self.assertEqual([r["result"] for r in out["results"]],
                         ["Projects/x/design.md", "research.md", "INBOX.md"])
        self.assertFalse(Path(ck).exists(), "the checkpoint survived completion")

    def test_a_plain_scan_never_resumes(self):
        a = self.ingest(self.proj, "--json", "--vault", self.vault)
        b = self.ingest(self.proj, "--json", "--vault", self.vault)
        self.assertEqual(len(json.loads(b.stdout)), 3, "a plain scan returned a partial list")
        self.assertEqual(len(list(self.spool().glob("*.progress.json"))), 2)
        self.assertNotEqual(a.stderr, b.stderr)

    def test_no_checkpoint(self):
        p = self.ingest(self.proj, "--json", "--vault", self.vault, "--no-checkpoint")
        self.assertOk(p)
        self.assertFalse(self.spool().exists())
        self.assertNotIn("checkpoint:", p.stderr)


class Pruning(Sandbox):
    def old(self, d, name, days):
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        when = time.time() - days * 86400
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(when))
        f.write_text(json.dumps({"schema": 1, "tool": "scan", "target": "/x", "items": [],
                                 "last_completed_index": -1, "results": [],
                                 "updated": stamp}))
        os.utime(f, (when, when))
        return f

    def test_prune_removes_only_week_old_checkpoints(self):
        v = self.tmp / "vault"
        d = v / "Projects" / "golden-thread" / "spool" / "scan"
        stale, fresh = self.old(d, "a.progress.json", 8), self.old(d, "b.progress.json", 1)
        dry = self.py(CK, "prune", "--vault", v, "--dry-run")
        self.assertOk(dry)
        self.assertTrue(stale.exists())
        self.assertOk(self.py(CK, "prune", "--vault", v))
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_session_registration_prunes(self):
        v = self.make_vault()
        d = v / "Projects" / "golden-thread" / "spool" / "ingest"
        stale, fresh = self.old(d, "a.progress.json", 9), self.old(d, "b.progress.json", 2)
        p = self.py(v / "Projects" / "golden-thread" / "tools" / "gt_session.py", "--vault", v,
                    "--id", "prune-test", "register", "--task", "t",
                    env={"CLAUDE_PID": str(os.getpid())})
        self.assertOk(p)
        self.assertFalse(stale.exists(), p.stdout)
        self.assertTrue(fresh.exists())
        self.assertIn("pruned 1 batch checkpoint", p.stdout)


class Skills(unittest.TestCase):
    def test_skills_offer_the_resume(self):
        for skill, tool in (("gt-scan", "scan"), ("gt-ingest", "ingest")):
            text = (GT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("gt_checkpoint.py find --tool %s" % tool, text)
            self.assertIn("interrupted at item N of", text)
            self.assertIn("--resume", text)


if __name__ == "__main__":
    unittest.main()
