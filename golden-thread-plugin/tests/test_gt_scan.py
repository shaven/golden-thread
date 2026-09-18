"""gt_scan.py -- the scan aggregator, and gt_scan_language.py -- the language leaf.

The aggregator contract. Every clause is a way an aggregator normally hides something:
  * "A member could not run" OUTRANKS "a member found something". An aggregator is exactly
    where a step that did not happen gets absorbed into an overall pass, and a scan that
    reports "0 findings" because it never loaded its definitions has manufactured the very
    assurance it exists to provide.
  * The member list is ANNOUNCED before anything runs. Composition is never a surprise.
  * The headline always says how many members ran out of how many were asked for. A finding
    count alone cannot distinguish "nothing is wrong" from "nothing was checked".
  * A member that is not installed is REPORTED, never silently skipped.
  * The aggregator owns no checking logic; the leaf is the truth.

The language leaf's own contract:
  * Language knowledge comes from packs (`filetype`, `construct`), never from the script, so a
    contributed language works with no code change.
  * It refuses to run rather than report a clean tree it never actually inspected.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import GT

AGG = GT / "scripts" / "gt_scan.py"
LEAF = GT / "scripts" / "gt_scan_language.py"
REGISTRY = GT / "scripts" / "gt_registry.py"
AGGREGATE = GT / "scripts" / "gt_aggregate.py"
PACKS = GT / "packs"


def pack(slot, name, entries):
    return {"schema": 1, "slot": slot, "name": name, "tier": "A", "spdx": "MIT",
            "provenance": {"origin": "original", "contributor": "T <t@example.com>",
                           "upstream": None},
            "dco": "Signed-off-by: T <t@example.com>", "entries": entries}


class ScanBase(unittest.TestCase):
    """A fixture release, so the tests never depend on what core happens to ship today."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gt-scan-"))
        self.release = self.tmp / "golden-thread" / "9.9.9"
        (self.release / "scripts").mkdir(parents=True)
        (self.release / "packs" / "core").mkdir(parents=True)
        (self.release / "packs" / "community").mkdir(parents=True)
        for src in (AGG, LEAF, REGISTRY, AGGREGATE):
            shutil.copy2(src, self.release / "scripts" / src.name)
        self.tree = self.tmp / "tree"
        self.tree.mkdir()
        self.vault = self.tmp / "vault"
        (self.vault / "Projects" / "golden-thread" / "packs").mkdir(parents=True)

    def put_pack(self, obj, tier="core"):
        d = self.release / "packs" / tier
        (d / ("%s.%s.pack.json" % (obj["slot"], obj["name"]))).write_text(
            json.dumps(obj, indent=2), encoding="utf-8")
        self.remanifest()

    def remanifest(self):
        files = {}
        for sub in ("core", "community"):
            for f in sorted((self.release / "packs" / sub).glob("*.pack.json")):
                raw = f.read_bytes()
                files["packs/%s/%s" % (sub, f.name)] = {
                    "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        (self.release / "MANIFEST.json").write_text(json.dumps({"files": files}),
                                                    encoding="utf-8")

    def language_packs(self):
        self.put_pack(pack("filetype", "ft", [{"match": "*.py", "lang": "python"}]))
        self.put_pack(pack("construct", "c", [
            {"lang": "python", "construct": "function",
             "pattern": "^[ \\t]*def[ \\t]+([A-Za-z_][A-Za-z0-9_]{0,80})"}]))
        self.put_pack(pack("naming", "n", [
            {"lang": "python", "construct": "function", "style": "snake"}]))
        self.put_pack(pack("encoding", "e", [
            {"lang": "python", "charset": "utf-8", "eol": "lf", "bom": "never"}]))

    def write(self, rel, text):
        p = self.tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def run_agg(self, *args):
        return subprocess.run([sys.executable, str(self.release / "scripts" / "gt_scan.py"),
                               str(self.tree), "--vault", str(self.vault)] + list(args),
                              capture_output=True, text=True)

    def run_leaf(self, *args):
        return subprocess.run([sys.executable,
                               str(self.release / "scripts" / "gt_scan_language.py"),
                               str(self.tree), "--vault", str(self.vault)] + list(args),
                              capture_output=True, text=True)


class AggregatorTest(ScanBase):
    def test_a_member_that_cannot_run_is_not_a_pass(self):
        """No packs at all: the leaf refuses, and the aggregate must NOT report success."""
        self.remanifest()                       # a manifest, but zero packs
        self.write("a.py", "def ok(): pass\n")
        r = self.run_agg()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("COULD NOT RUN", r.stdout)
        self.assertIn("NOT a pass", r.stdout)
        self.assertIn("0 of 1", r.stdout)

    def test_exit_4_from_the_leaf_is_interpreted_not_printed_as_a_number(self):
        """NOTHING_LOADED was declared "so the aggregator interprets rather than guesses" and
        nothing read it: the reader got a bare `exit=4` and had to know what 4 meant."""
        self.remanifest()                       # a manifest, but zero packs
        self.write("a.py", "def ok(): pass\n")
        r = self.run_agg()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("no definitions", r.stdout,
                      "exit 4 was reported as a number, not as what it means")

    def test_a_failing_member_outranks_a_clean_one(self):
        """Even when another member is fine, an unrun member decides the exit code."""
        self.remanifest()
        r = self.run_agg()
        self.assertEqual(r.returncode, 3)
        self.assertNotIn("exit=0", r.stdout)

    def test_members_are_announced_before_running(self):
        self.language_packs()
        self.write("a.py", "def ok(): pass\n")
        r = self.run_agg()
        self.assertIn("running 1 scan member(s): language", r.stdout)
        self.assertLess(r.stdout.index("running 1 scan member"),
                        r.stdout.index("member(s) ran"), "announced BEFORE the results")

    def test_headline_always_reports_how_many_ran(self):
        self.language_packs()
        self.write("a.py", "def ok(): pass\n")
        r = self.run_agg()
        self.assertIn("1 of 1 member(s) ran", r.stdout)
        self.assertEqual(r.returncode, 0)

    def test_findings_give_exit_1_when_every_member_ran(self):
        self.language_packs()
        self.write("a.py", "def BadName(): pass\n")
        r = self.run_agg()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("1 reported findings", r.stdout)

    def test_unknown_member_is_usage_not_a_silent_skip(self):
        self.language_packs()
        r = self.run_agg("--only", "nosuchmember")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown member", r.stderr)

    def test_list_names_every_member_and_marks_absent_ones(self):
        r = subprocess.run([sys.executable, str(self.release / "scripts" / "gt_scan.py"),
                            "--list"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("language", r.stdout)

    def test_json_reports_asked_ran_and_could_not_run_separately(self):
        self.remanifest()
        r = self.run_agg("--json")
        d = json.loads(r.stdout)
        self.assertEqual(d["asked"], ["language"])
        self.assertEqual(d["ran"], [])
        self.assertEqual([m["member"] for m in d["could_not_run"]], ["language"])


class LanguageLeafTest(ScanBase):
    def test_refuses_rather_than_reporting_a_tree_it_never_inspected(self):
        self.remanifest()
        self.write("a.py", "def BadName(): pass\n")
        r = self.run_leaf()
        self.assertEqual(r.returncode, 4, r.stdout)
        self.assertIn("REFUSING TO SCAN", r.stderr)

    def test_a_pack_that_failed_to_load_outranks_the_findings(self):
        """A partial scan must not exit like a complete one.

        The leaf exited 3 only when there were problems AND no findings, so a pack that failed
        to verify was invisible whenever anything else happened to match: exit 1, which the
        aggregator reads as "ran, found something", and nothing anywhere said the definitions
        were incomplete. The findings still print -- only the code changes."""
        self.language_packs()
        (self.release / "packs" / "core" / "naming.broken.pack.json").write_text(
            "{not json", encoding="utf-8")
        self.write("a.py", "def BadName(): pass\n")
        r = self.run_leaf()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("BadName", r.stdout, "the findings that WERE made must still print")
        self.assertIn("PROBLEM", r.stdout + r.stderr)

    def test_a_language_is_taught_entirely_by_packs(self):
        """The proof that language knowledge is data: a language the script has never heard of."""
        self.language_packs()
        self.put_pack(pack("filetype", "ex", [{"match": "*.ex", "lang": "elixir"}]))
        self.put_pack(pack("construct", "exc", [
            {"lang": "elixir", "construct": "function",
             "pattern": "^[ \\t]*def[ \\t]+([A-Za-z_][A-Za-z0-9_]{0,80})"}]))
        self.put_pack(pack("naming", "exn", [
            {"lang": "elixir", "construct": "function", "style": "snake"}]))
        self.write("app.ex", "def BadElixirName(x) do\n  x\nend\n")
        r = self.run_leaf("--json")
        d = json.loads(r.stdout)
        hits = [f for f in d["findings"] if f["path"] == "app.ex"]
        self.assertEqual(len(hits), 1, r.stdout)
        self.assertIn("BadElixirName", hits[0]["message"])

    def test_a_file_whose_extension_does_not_match_can_be_assigned(self):
        self.language_packs()
        (self.vault / "Projects" / "golden-thread" / "packs" / "ft.local.pack.json").write_text(
            json.dumps(pack("filetype", "mine", [{"match": "bin/deploy", "lang": "python"}])),
            encoding="utf-8")
        self.write("bin/deploy", "def BadName(): pass\n")
        r = self.run_leaf("--json")
        d = json.loads(r.stdout)
        self.assertTrue([f for f in d["findings"] if f["path"] == "bin/deploy"], r.stdout)

    def test_a_local_retract_switches_a_language_off(self):
        self.language_packs()
        self.write("a.py", "def BadName(): pass\n")
        self.assertEqual(self.run_leaf().returncode, 1, "findings before the retract")
        p = pack("filetype", "off", [{"match": "*.never", "lang": "placeholder"}])
        p["retract"] = [{"lang": "python"}]
        (self.vault / "Projects" / "golden-thread" / "packs" / "off.pack.json").write_text(
            json.dumps(p), encoding="utf-8")
        r = self.run_leaf("--json")
        d = json.loads(r.stdout)
        self.assertEqual([f for f in d["findings"] if f["path"] == "a.py"], [], r.stdout)

    def test_the_retract_hint_names_a_real_language_not_a_rule_name(self):
        """`rule` for an encoding finding is bom/eol/charset; a retract on that matches nothing."""
        self.language_packs()
        self.write("crlf.py", "def ok():\r\n    pass\r\n")
        r = self.run_leaf()
        self.assertIn('"retract": [{"lang": "python"}]', r.stdout)
        self.assertNotIn('"lang": "eol"', r.stdout)

    def test_ignored_and_generated_files_are_skipped(self):
        self.language_packs()
        self.put_pack(pack("ignore", "ig", [{"path": "node_modules/"}]))
        self.write("node_modules/pkg/a.py", "def BadName(): pass\n")
        r = self.run_leaf("--json")
        d = json.loads(r.stdout)
        self.assertEqual([f for f in d["findings"] if "node_modules" in f["path"]], [])


if __name__ == "__main__":
    unittest.main()
