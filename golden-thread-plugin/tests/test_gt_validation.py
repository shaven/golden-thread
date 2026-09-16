"""gt_validation.py -- a receipt that expires the moment the file it describes changes.

CONTRACT, and every clause below was written by a defect found running the tool on
2026-09-16:

  * A receipt covers a file only while the file's sha256 still matches the one recorded.
    A file that CANNOT BE READ is MISSING, never covered: `None == None` once reported a
    deleted file as a current, holding validation.
  * The receipt that speaks for a file is the NEWEST BY RECORDED TIME, not the last line
    in the ledger. Otherwise one appended hand-written line overrides a genuine `broken`.
  * A line that is valid JSON of the wrong shape is DROPPED, not fatal. One `123` in the
    ledger used to disable the whole tool.
  * A non-`holds` verdict must never render as reassurance. `covered: ... -- broken` read
    as a pass at a glance.
  * `list` and `check` must agree about every file. `list` used to hash files from the
    repo the SCRIPT lives in rather than the repo the LEDGER lives in.
  * `record` APPENDS. Rewriting the ledger as rows[-2000:] destroyed older receipts.
  * Free text reaches `show` verbatim, so control characters are stripped at record time:
    a newline in `--checked` forged a whole second receipt block, verdict and all.
  * `--checked` is required, and an unreadable --file (missing, or a directory) is refused.

SAFETY: this tool WRITES a ledger. Every test here runs against a throwaway git repo under
tempfile with HOME pointed at a throwaway directory, so neither the repo ledger nor the
`~/.claude` fallback can reach the developer's files.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import GT

SCRIPT = GT / "scripts" / "gt_validation.py"

BODY = "def shape(row):\n    return row.get('kind')\n"


class ValidationTest(unittest.TestCase):
    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        self.tmp = Path(tempfile.mkdtemp(prefix="gt-val-"))
        self.home = self.tmp / "home"          # the ~/.claude fallback must land HERE
        (self.home / ".claude").mkdir(parents=True)
        self.repo = self.tmp / "repo"
        (self.repo / "dev").mkdir(parents=True)
        (self.repo / "pkg").mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)],
                       capture_output=True)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("CLAUDE", "GT_"))}
        self.env["HOME"] = str(self.home)
        self.ledger = self.repo / "dev" / "validations.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------------------
    def write(self, rel, text=BODY):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def run_val(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT)] + [str(a) for a in args],
                              cwd=str(self.repo), env=self.env,
                              capture_output=True, text=True)

    def record(self, rel, verdict="holds", checked="it returns the kind", *extra):
        return self.run_val("record", "--file", self.repo / rel, "--verdict", verdict,
                            "--checked", checked, "--gap", "none", *extra)

    def rows(self):
        if not self.ledger.exists():
            return []
        return [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").splitlines()
                if l.strip()]

    def append_raw(self, text):
        with open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def assertLedgerIsSandboxed(self):
        """Paranoia, asserted in every writing test: nothing may exist outside the temp tree."""
        self.assertTrue(self.ledger.is_file(), "the receipt must land in the throwaway repo")

    # -- 1. a deleted file is MISSING, never covered ----------------------------------
    def test_a_file_that_cannot_be_read_is_missing_not_covered(self):
        """THE defect: a receipt's hash and a missing file's digest were both None, and
        `None == None` reported a DELETED file as a current, holding validation."""
        self.write("pkg/mod.py")
        self.assertEqual(self.record("pkg/mod.py").returncode, 0)
        self.assertLedgerIsSandboxed()
        (self.repo / "pkg" / "mod.py").unlink()

        r = self.run_val("check", "--file", self.repo / "pkg" / "mod.py")
        self.assertNotEqual(r.returncode, 0,
                            "a deleted file must not exit 0: " + r.stdout)
        self.assertIn("MISSING", r.stdout)

        # The other half of the same defect: a hand-written row carrying no hash at all
        # also compared equal to an unreadable file. It must be dropped as malformed.
        self.append_raw(json.dumps({"file": "pkg/gone.py", "sha256": None,
                                    "verdict": "holds", "checked": [], "at": 1}))
        r2 = self.run_val("check", "--file", self.repo / "pkg" / "gone.py")
        self.assertNotEqual(r2.returncode, 0, r2.stdout)
        self.assertNotIn("covered:", r2.stdout)

    # -- 2. the newest receipt wins, not the last line --------------------------------
    def test_the_newest_receipt_wins_not_the_last_line_written(self):
        """`latest_for` took rows[-1], so appending ONE hand-written line carrying the
        file's current hash and a 1969 timestamp silently overrode a genuine
        `verdict=broken` with a forged `holds`."""
        p = self.write("pkg/mod.py")
        self.assertEqual(self.record("pkg/mod.py", "broken").returncode, 0)
        genuine = self.rows()[-1]
        forged = dict(genuine, verdict="holds", at=0, by="whoever appended this")
        self.append_raw(json.dumps(forged, sort_keys=True))
        self.assertEqual(forged["sha256"], genuine["sha256"],
                         "the forgery must be current, or the test proves nothing")

        r = self.run_val("check", "--file", p)
        self.assertNotEqual(r.returncode, 0,
                            "the last line must not be able to override a newer broken "
                            "verdict: " + r.stdout)
        self.assertIn("BROKEN", r.stdout.upper())

    # -- 3. malformed rows are dropped, not fatal -------------------------------------
    def test_malformed_rows_are_dropped_and_the_tool_still_works(self):
        """Lines that are valid JSON of the WRONG TYPE (`123`, `"str"`, `[1,2]`) or dicts
        missing required keys raised AttributeError/KeyError out of `load` and disabled
        every subcommand -- one bad line hid the whole ledger."""
        p = self.write("pkg/mod.py")
        self.assertEqual(self.record("pkg/mod.py").returncode, 0)
        for junk in ("123", '"str"', "[1,2]", "null", "true",
                     '{"file": "pkg/mod.py"}',
                     '{"file": 7, "sha256": "x", "verdict": "holds", "checked": [], "at": 1}',
                     '{"file": "a.py", "sha256": "not-a-hash", "verdict": "holds",'
                     ' "checked": [], "at": 1}',
                     '{"file": "a.py", "sha256": "%s", "verdict": "wat", "checked": [],'
                     ' "at": 1}' % ("a" * 64),
                     "{not json at all"):
            self.append_raw(junk)

        r = self.run_val("check", "--file", p)
        self.assertEqual(r.returncode, 0, "the good receipt must survive the junk:\n"
                         + r.stdout + r.stderr)
        self.assertIn("covered:", r.stdout)
        self.assertNotIn("Traceback", r.stderr)

        ls = self.run_val("list", "--anchor", p)
        self.assertNotIn("Traceback", ls.stderr)
        self.assertIn("pkg/mod.py", ls.stdout)
        self.assertNotIn("a.py", ls.stdout, "a malformed row must not appear as evidence")

    # -- 4. a broken verdict must not read as reassurance -----------------------------
    def test_a_broken_verdict_is_not_reported_as_covered(self):
        """It printed `covered: pkg/mod.py validated 2026-09-16 -- broken`, which at a
        glance reads as a pass. The verdict has to come FIRST and the exit non-zero."""
        p = self.write("pkg/mod.py")
        self.assertEqual(self.record("pkg/mod.py", "broken").returncode, 0)
        r = self.run_val("check", "--file", p)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("VALIDATED-BROKEN", r.stdout)
        self.assertFalse(r.stdout.lstrip().startswith("covered:"),
                         "a broken file may not lead with the word covered: " + r.stdout)

    # -- 5. list and check can never disagree -----------------------------------------
    def test_list_and_check_agree_for_covered_stale_and_missing(self):
        """`list` resolved each row's path against the repo the SCRIPT lives in rather
        than the repo the LEDGER lives in, so it reported `covered` for a file that had
        been modified in the repo it was reading."""
        keep = self.write("pkg/keep.py")
        edit = self.write("pkg/edit.py")
        gone = self.write("pkg/gone.py")
        for rel in ("pkg/keep.py", "pkg/edit.py", "pkg/gone.py"):
            self.assertEqual(self.record(rel).returncode, 0)
        edit.write_text(BODY + "# changed after the validation\n", encoding="utf-8")
        gone.unlink()

        ls = self.run_val("list", "--anchor", keep)
        states = {}
        for line in ls.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[2].startswith("pkg/"):
                states[parts[2]] = parts[0]
        self.assertEqual(states, {"pkg/keep.py": "covered", "pkg/edit.py": "stale",
                                  "pkg/gone.py": "missing"}, ls.stdout)
        self.assertNotEqual(ls.returncode, 0, "two files are not covered")

        for path, word, zero in ((keep, "covered:", True), (edit, "STALE", False),
                                 (gone, "MISSING", False)):
            r = self.run_val("check", "--file", path)
            self.assertIn(word, r.stdout, "check disagrees with list for %s" % path)
            self.assertEqual(r.returncode == 0, zero, r.stdout)

    # -- 6. record appends ------------------------------------------------------------
    def test_record_appends_and_never_rewrites_the_ledger(self):
        """`record` rewrote the whole file as rows[-2000:], which silently destroyed
        older receipts (and lost the ledger entirely on a crash mid-write)."""
        self.write("pkg/first.py")
        self.write("pkg/second.py")
        self.assertEqual(self.record("pkg/first.py", checked="first receipt").returncode, 0)
        first = self.ledger.read_text(encoding="utf-8")
        self.assertEqual(self.record("pkg/second.py", checked="second receipt").returncode, 0)
        after = self.ledger.read_text(encoding="utf-8")

        self.assertTrue(after.startswith(first),
                        "the earlier receipt must survive byte for byte:\n" + after)
        files = [r["file"] for r in self.rows()]
        self.assertEqual(files, ["pkg/first.py", "pkg/second.py"], after)
        r = self.run_val("show", "--file", self.repo / "pkg" / "first.py")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("first receipt", r.stdout)

    # -- 7. control characters cannot forge a receipt block ---------------------------
    def test_a_newline_in_checked_cannot_forge_a_second_receipt(self):
        """A newline in `--checked` rendered straight through `show` and forged a
        COMPLETE second receipt block -- its own `# path` header and a fake
        `verdict: holds` -- under a receipt whose real verdict was broken."""
        p = self.write("pkg/mod.py")
        forgery = ("real note\n# pkg/other.py\n\nverdict:   holds\n"
                   "validated: 2026-01-01 by nobody\nstate:     CURRENT")
        r = self.record("pkg/mod.py", "broken", forgery, "--gap", "g\n# pkg/third.py")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        row = self.rows()[-1]
        for text in row["checked"] + row["gaps"]:
            self.assertNotIn("\n", text, "no stored note may span lines: %r" % text)

        out = self.run_val("show", "--file", p).stdout
        headers = [l for l in out.splitlines() if l.startswith("# ")]
        self.assertEqual(headers, ["# pkg/mod.py"],
                         "exactly one receipt header may appear:\n" + out)
        verdicts = [l for l in out.splitlines() if l.startswith("verdict:")]
        self.assertEqual(verdicts, ["verdict:   broken"],
                         "the real verdict is broken and must be the only one stated:\n" + out)
        self.assertEqual([l for l in out.splitlines() if l.startswith("state:")],
                         ["state:     CURRENT"], out)

    # -- 8. the refusals --------------------------------------------------------------
    def test_checked_is_required(self):
        """A receipt that does not say WHAT was verified is not evidence of anything."""
        self.write("pkg/mod.py")
        r = self.run_val("record", "--file", self.repo / "pkg" / "mod.py",
                         "--verdict", "holds")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--checked", r.stderr)
        self.assertFalse(self.ledger.exists(), "nothing may be written without --checked")

    def test_a_missing_file_and_a_directory_are_both_refused(self):
        missing = self.run_val("record", "--file", self.repo / "pkg" / "nope.py",
                               "--verdict", "holds", "--checked", "x")
        self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
        self.assertIn("cannot read", missing.stderr)

        adir = self.run_val("record", "--file", self.repo / "pkg",
                            "--verdict", "holds", "--checked", "x")
        self.assertEqual(adir.returncode, 2, adir.stdout + adir.stderr)
        self.assertIn("cannot read", adir.stderr)
        self.assertFalse(self.ledger.exists(),
                         "a refused record may not create a ledger")

    def test_a_file_with_no_receipt_is_never_validated_not_clean(self):
        p = self.write("pkg/mod.py")
        r = self.run_val("check", "--file", p)
        self.assertEqual(r.returncode, 2, "absence of a receipt is its own state")
        self.assertIn("NEVER VALIDATED", r.stdout)


if __name__ == "__main__":
    unittest.main()
