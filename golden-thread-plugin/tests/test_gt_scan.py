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
import re
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
        # The member scripts are DERIVED from gt_scan.MEMBERS, not listed here. A hard-coded
        # list meant that adding a member to the aggregator left this fixture one leaf behind,
        # so the new member reported COULD NOT RUN and eleven tests failed for a reason that
        # had nothing to do with what they check. Same shape as the install fixture that
        # duplicated its manifest step: the fix exists, and a second copy bypassed it.
        from _harness import load_module
        members = load_module(AGG, "gt_scan_for_fixture").MEMBERS
        wanted = [AGG, REGISTRY, AGGREGATE] + [
            GT / "scripts" / script for script, *_ in members.values()]
        # ...and whatever those scripts IMPORT, resolved transitively. A hand-maintained copy
        # list has now bitten three times in one day: the install fixture that omitted its
        # manifest step, this fixture's member list, and this fixture's import list -- a member
        # grew an `import gt_staged` and the whole unit failed with ModuleNotFoundError, which
        # looks nothing like "the fixture is incomplete". Deriving it means the next shared
        # helper costs nobody an afternoon.
        seen, queue = set(), list(wanted)
        import re as _re
        while queue:
            src = queue.pop()
            if src in seen or not src.is_file():
                continue
            seen.add(src)
            for name in _re.findall(r"^\s*import (gt_[a-z_]+)", src.read_text(), _re.M):
                queue.append(GT / "scripts" / ("%s.py" % name))
        for src in sorted(seen):
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

    def code_packs(self):
        """Definitions for the `code` member, so "every member ran" can be true.

        Needed because the aggregator's contract is that an unrun member outranks a clean
        one -- so a test about findings cannot leave a member with nothing to load.
        """
        self.put_pack(pack("lint", "l", [
            {"id": "py-self-cmp", "lang": "python", "severity": "warn",
             "message": "a value compared with itself", "rule": {"pattern": "$A == $A"}}]))

    def all_packs(self):
        self.language_packs()
        self.code_packs()

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
        self.assertRegex(r.stdout, r"0 of \d+ member\(s\) ran")

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
        self.assertRegex(r.stdout, r"running \d+ scan member\(s\):")
        self.assertLess(r.stdout.index("running "),
                        r.stdout.index("member(s) ran"), "announced BEFORE the results")

    def test_headline_always_reports_how_many_ran(self):
        self.all_packs()
        self.write("a.py", "def ok(): pass\n")
        r = self.run_agg()
        m = re.search(r"(\d+) of (\d+) member\(s\) ran", r.stdout)
        self.assertTrue(m, "the headline did not state how many ran:\n" + r.stdout)
        self.assertEqual(m.group(1), m.group(2), "not every member ran:\n" + r.stdout)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_findings_give_exit_1_when_every_member_ran(self):
        self.all_packs()
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
        # Derived from the aggregator's own member table: asserting a literal list here meant
        # adding a member broke a test about JSON SHAPE, which is not what it checks.
        from _harness import load_module
        expected = sorted(load_module(AGG, "gt_scan_for_json").MEMBERS)
        self.assertEqual(sorted(d["asked"]), expected)
        self.assertEqual(d["ran"], [])
        self.assertEqual(sorted(m["member"] for m in d["could_not_run"]), expected)


class AFlagIsOnlyPassedToMembersThatTakeIt(ScanBase):
    """`--all-files` was forwarded to EVERY member. `code` has no such flag -- it decides scope
    by a suffix/shebang union rule with nothing to widen -- so it exited 2 on argparse usage and
    `gt_scan.py <tree> --all-files` reported "1 of 2 member(s) ran". A documented flag silently
    halved the scan, and the count line was the only evidence.

    Found by reading the file, not by a test: no test combined a member-specific flag with the
    full member set. This is that test."""

    def test_all_files_does_not_change_which_members_run(self):
        """Fixture-independent, and the assertion that actually pins the defect: whatever this
        fixture's packs let run, asking for `--all-files` must not change it. Asserting "2 of 2"
        would have tied this test to the fixture shipping packs, which it does not."""
        self.remanifest()
        self.write("a.py", "x = 1\n")
        import json as _json
        base = _json.loads(self.run_agg("--json").stdout)
        wide = _json.loads(self.run_agg("--all-files", "--json").stdout)
        self.assertEqual(sorted(wide["ran"]), sorted(base["ran"]),
                         "a member was knocked out by a flag it does not accept")
        self.assertEqual(wide["headline"], base["headline"])

    def test_no_member_ever_exits_on_ARGPARSE_USAGE(self):
        """The defect's exact signature. `code` exited 2 -- a usage error -- because it was
        handed a flag its parser has never had. A member refusing for its OWN reasons is a
        different thing entirely and this does not object to it."""
        self.remanifest()
        self.write("a.py", "x = 1\n")
        import json as _json
        d = _json.loads(self.run_agg("--all-files", "--json").stdout)
        for m in d["could_not_run"]:
            self.assertNotEqual(m["exit"], 2,
                                "member %r was handed a flag it does not accept: %s"
                                % (m["member"], m["detail"]))

    def test_it_says_which_members_the_flag_did_not_apply_to(self):
        """Silently dropping the flag is its own small lie: the caller asked for something and
        was never told part of the run ignored it."""
        self.remanifest()
        self.write("a.py", "x = 1\n")
        r = self.run_agg("--all-files")
        self.assertIn("--all-files does not apply to", r.stdout)

    def test_every_member_declares_its_flag_surface(self):
        """The defect was a member whose accepted flags were assumed rather than declared."""
        from _harness import load_module
        members = load_module(AGG, "gt_scan_for_decl").MEMBERS
        for name, meta in members.items():
            self.assertEqual(len(meta), 3, "member %r does not declare its flags" % name)
            self.assertIsInstance(meta[2], frozenset)


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

    def test_a_name_a_framework_requires_can_be_exempted(self):
        """0.19.0: `exempt` lists names a naming rule must not flag (owner, 2026-10-02).

        unittest only calls `setUp` and the `assert*` helpers by those camelCase names, so a
        snake_case rule flagging them is noise that buries real findings -- 371 hits on gt's
        own tree, nearly all of them this.
        """
        self.put_pack(pack("filetype", "ft", [{"match": "*.py", "lang": "python"}]))
        self.put_pack(pack("construct", "c", [
            {"lang": "python", "construct": "function",
             "pattern": "^[ \\t]*def[ \\t]+([A-Za-z_][A-Za-z0-9_]{0,80})"}]))
        self.put_pack(pack("naming", "n", [
            {"lang": "python", "construct": "function", "style": "snake",
             "exempt": ["^setUp$", "^assert[A-Z][A-Za-z0-9]*$"]}]))
        self.put_pack(pack("encoding", "e", [
            {"lang": "python", "charset": "utf-8", "eol": "lf", "bom": "never"}]))
        self.write("t.py", "def setUp(self): pass\ndef assertAllowed(self): pass\n"
                           "def BadName(): pass\ndef setUpX(): pass\n")
        d = json.loads(self.run_leaf("--json").stdout)
        names = sorted(re.search(r"'(\w+)'", f["message"]).group(1)
                       for f in d["findings"] if f["path"] == "t.py")
        self.assertEqual(names, ["BadName", "setUpX"], d)


class ShippedPackFalsePositives(unittest.TestCase):
    """The core packs as shipped, against the false positives found scanning gt's own tree."""

    def core_entry(self, slot_file, match):
        entries = json.loads((GT / "packs" / "core" / slot_file).read_text())["entries"]
        return next(e for e in entries if all(e.get(k) == v for k, v in match.items()))

    def test_core_python_function_naming_exempts_unittest_names(self):
        e = self.core_entry("naming.conventions.pack.json",
                            {"lang": "python", "construct": "function"})
        exempt = [re.compile(x) for x in e.get("exempt", [])]
        hit = lambda n: any(x.search(n) for x in exempt)
        for name in ("setUp", "tearDown", "setUpClass", "tearDownClass", "setUpModule",
                     "tearDownModule", "asyncSetUp", "asyncTearDown", "assertAllowed",
                     "assertCurrent"):
            self.assertTrue(hit(name), name)
        for name in ("BadName", "setUpThing", "assert_ok_but_snake", "doThing", "do_get",
                     "do_Post"):
            self.assertFalse(hit(name), name)

    def test_core_python_function_naming_exempts_http_server_handlers(self):
        """http.server dispatches on `do_<METHOD>`; `do_POST` is the stdlib's name, not ours."""
        e = self.core_entry("naming.conventions.pack.json",
                            {"lang": "python", "construct": "function"})
        exempt = [re.compile(x) for x in e.get("exempt", [])]
        for name in ("do_GET", "do_POST", "do_HEAD", "do_OPTIONS"):
            self.assertTrue(any(x.search(name) for x in exempt), name)

    def test_a_leading_underscore_marks_a_private_class_not_a_bad_name(self):
        """`class _Usage` is PascalCase plus Python's private prefix (lotr.py:64)."""
        from _harness import load_module
        ok = load_module(GT / "scripts" / "gt_scan_language.py", "gt_scan_language_t").STYLE_OK
        for name in ("_Usage", "_HttpServer", "Usage"):
            self.assertTrue(ok["pascal"].match(name), name)
        for name in ("_usage", "__Usage", "_", "usage"):
            self.assertFalse(ok["pascal"].match(name), name)

    def test_bare_except_rule_accepts_an_underscore_exception_name(self):
        """`except _Usage as e:` names its exception; the rule read `_` as bare (lotr.py:301)."""
        e = self.core_entry("lint.common.pack.json", {"id": "py-bare-except"})
        rx = next(c["not"]["regex"] for c in e["rule"]["all"] if "not" in c)
        for named in ("except _Usage as e:", "except ValueError:", "except (A, B):"):
            self.assertTrue(re.match(rx, named), named)
        self.assertFalse(re.match(rx, "except:"))



class OldReleaseFoldersAreNotScanned(ScanBase):
    """0.19.0: a plugin repo keeps every release it ever shipped (gt: 29 folders). Only the
    newest two -- what gt-src carries -- are scanned; the rest are history, re-reporting the
    same findings, and alone they pushed a scan past the commit gate's time limit."""

    def test_only_the_newest_two_release_folders_are_scanned(self):
        self.language_packs()
        for v in ("0.9.0", "0.10.0", "0.10.1"):
            d = self.tree / "plug" / "golden-thread" / v
            (d / ".claude-plugin").mkdir(parents=True)
            (d / ".claude-plugin" / "plugin.json").write_text("{}")
            self.write("plug/golden-thread/%s/x.py" % v, "def BadName(): pass\n")
        d = json.loads(self.run_leaf("--json").stdout)
        got = sorted({f["path"].split("/")[2] for f in d["findings"] if f["path"].endswith("x.py")})
        self.assertEqual(got, ["0.10.0", "0.10.1"])

    def test_plain_versioned_folders_without_a_plugin_are_still_scanned(self):
        self.language_packs()
        for v in ("1.0.0", "1.1.0", "1.2.0"):
            self.write("docs/%s/x.py" % v, "def BadName(): pass\n")
        d = json.loads(self.run_leaf("--json").stdout)
        self.assertEqual(len([f for f in d["findings"] if f["path"].endswith("x.py")]), 3)

if __name__ == "__main__":
    unittest.main()
