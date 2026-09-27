"""gt_scan_code.py — source validation, and the skip contract it turns on.

The load-bearing tests here are not the detection ones. They are:

  * a rule needing an absent evaluator tier is SKIPPED, counted, put in SARIF's
    toolExecutionNotifications, and makes the exit PARTIAL — never a silent pass;
  * `has`/`inside` default to ast-grep's `stopBy: neighbor`, which is worth 201 false
    positives on this repo if it regresses to an unbounded search;
  * a rule key gt does not implement fails LOUDLY rather than under-matching;
  * an empty rule set is exit 4, because "no findings" must never be readable as
    "no rules exist".
"""
import json
import re
import unittest

from _harness import Sandbox, SCRIPTS

SCAN = SCRIPTS / "gt_scan_code.py"


def pack(entries, name="mine"):
    return {
        "schema": 1, "slot": "lint", "name": name, "tier": "D", "spdx": "MIT",
        "provenance": {"origin": "original", "contributor": "A Dev <d@e.com>",
                       "upstream": None},
        "dco": "Signed-off-by: A Dev <d@e.com>",
        "entries": entries,
    }


class ScanBase(Sandbox):
    """NOTE: the shipped CORE lint pack is in effect in every test here, on top of whatever
    local pack the test writes. That is the real configuration -- precedence is
    community < core < local -- so these tests assert on their OWN rule ids and on the SHAPE
    of the affirmative line, never on absolute totals. A test that hard-coded "2 rule(s)"
    would break every time a core rule is added, which is a test measuring the wrong thing.
    """
    def setUp(self):
        super().setUp()
        self.tree = self.tmp / "tree"
        (self.tree / "scripts").mkdir(parents=True)
        self.vault = self.tmp / "vault"
        self.packs = self.vault / "Projects" / "golden-thread" / "packs"
        self.packs.mkdir(parents=True)

    def write(self, rel, text):
        p = self.tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def put(self, entries, name="mine"):
        (self.packs / ("lint.%s.pack.json" % name)).write_text(
            json.dumps(pack(entries, name), indent=2))

    def scan(self, *extra, expect=None, env=None):
        proc = self.py(SCAN, self.tree, "--vault", self.vault, *extra, env=env)
        if expect is not None:
            self.assertEqual(proc.returncode, expect,
                             "exit %d\n%s\n%s" % (proc.returncode, proc.stdout, proc.stderr))
        return proc

    def findings(self, *extra, env=None):
        proc = self.py(SCAN, self.tree, "--vault", self.vault, "--json", *extra, env=env)
        return json.loads(proc.stdout)

    # The tier list, stated rather than inherited. Tests that need a tier ABSENT force it
    # absent; tests that need it PRESENT ask for it and skip with a reason if the package is
    # not importable. Neither depends on what pip last did on the machine running the suite --
    # which mattered more than expected: ast_grep_py was installed with --user on 2026-09-27
    # and is INVISIBLE here, because the harness gives every test a throwaway HOME and user
    # site-packages resolves from HOME. The developer and the suite saw different tiers.
    STDLIB_ONLY = {"GT_SCAN_TIERS": "text,stdlib"}

    def astgrep_env(self):
        """Env that makes the astgrep tier real, or skipTest with the reason."""
        try:
            import ast_grep_py
        except Exception:
            self.skipTest("ast_grep_py is not importable; the astgrep tier cannot be tested")
        import os as _os
        site = _os.path.dirname(_os.path.dirname(ast_grep_py.__file__))
        return {"GT_SCAN_TIERS": "text,stdlib,astgrep", "PYTHONPATH": site}

    def mine(self, ids, *extra):
        """Findings from the rule ids this test defined, ignoring the core pack's."""
        ids = {ids} if isinstance(ids, str) else set(ids)
        return [f for f in self.findings(*extra)["findings"] if f["rule"] in ids]


class TheSkipContract(ScanBase):
    """The rule this whole design turns on."""

    def test_a_rule_needing_an_absent_tier_is_skipped_not_passed(self):
        self.put([{"id": "sh-structural", "lang": "shell", "severity": "warn",
                   "message": "structural shell", "evaluator": "astgrep",
                   "rule": {"kind": "command"}}])
        self.write("ok.sh", "#!/bin/sh\nset -u\necho hi\n")
        proc = self.scan(expect=3, env=self.STDLIB_ONLY)
        self.assertIn("SKIPPED", proc.stdout)
        self.assertIn("sh-structural", proc.stdout)

    def test_a_skip_makes_the_exit_partial_rather_than_clean(self):
        """Exit 0 here would be the 2026-09-24 defect exactly: a check that did not run,
        reported as a check that passed."""
        self.put([{"id": "x", "lang": "python", "severity": "warn", "message": "m",
                   "evaluator": "treesitter", "rule": {"kind": "call"}}])
        self.write("scripts/a.py", "x = 1\n")
        self.assertEqual(self.scan(env=self.STDLIB_ONLY).returncode, 3)

    def test_the_affirmative_says_how_many_ran_out_of_how_many(self):
        """Never just the finding count: "0 findings" from 0 evaluated rules is not a result."""
        self.put([{"id": "a", "lang": "python", "severity": "warn", "message": "m",
                   "rule": {"kind": "call"}},
                  {"id": "b", "lang": "python", "severity": "warn", "message": "m",
                   "evaluator": "astgrep", "rule": {"kind": "call"}}])
        self.write("scripts/a.py", "x = 1\n")
        out = self.scan(env=self.STDLIB_ONLY).stdout
        m = re.search(r"(\d+) rule\(s\), (\d+) evaluated .*?(\d+) SKIPPED", out)
        self.assertTrue(m, "the affirmative did not state rules/evaluated/skipped:\n" + out)
        total, evaluated, skipped = (int(g) for g in m.groups())
        self.assertEqual(evaluated + skipped, total,
                         "the counts do not add up, so one of them is not what it says")
        self.assertGreaterEqual(skipped, 1)

    def test_the_skip_reaches_sarif_notifications(self):
        """SARIF's conformant place for "this rule could not run here"."""
        self.put([{"id": "needs-astgrep", "lang": "shell", "severity": "warn",
                   "message": "m", "evaluator": "astgrep", "rule": {"kind": "command"}}])
        self.write("ok.sh", "#!/bin/sh\nset -u\n")
        out = self.tmp / "r.sarif"
        self.scan("--sarif", str(out), env=self.STDLIB_ONLY)
        inv = json.loads(out.read_text())["runs"][0]["invocations"][0]
        self.assertFalse(inv["executionSuccessful"], "a run with a skipped rule claimed success")
        notes = inv["toolExecutionNotifications"]
        self.assertTrue(any(n["associatedRule"]["id"] == "needs-astgrep" for n in notes))

    def test_stripping_the_skip_would_be_visible(self):
        """The protection, stated as its own absence: with NO unavailable tier declared the
        same rule set runs clean at exit 0, so the exit-3 above is caused by the skip and not
        by something incidental."""
        self.put([{"id": "a", "lang": "python", "severity": "warn", "message": "m",
                   "rule": {"kind": "call"}}])
        self.write("scripts/a.py", "x = 1\n")
        self.assertEqual(self.scan().returncode, 0)


class StopByDepth(ScanBase):
    """ast-grep's `has`/`inside` default to `neighbor`, and it is worth 201 false positives.

    Measured 2026-09-27: a mutable-default-argument rule written with an unbounded `has`
    reported 201 findings on this repo -- every function that merely contained an empty list
    somewhere in its body. The same rule with the correct default reported 0 and still matched
    the fixture. An unbounded default makes the scanner unusable on the first run, which is
    the only run that decides whether anyone runs it twice.
    """

    FIXTURE = (
        "def bad(x=[]):\n"
        "    return x\n"
        "def good():\n"
        "    items = []\n"          # an empty list INSIDE the body, not a default
        "    return items\n"
    )

    def test_the_default_is_neighbor_so_a_body_literal_does_not_match(self):
        self.put([{"id": "mutable-default", "lang": "python", "severity": "warn",
                   "message": "mutable default", "rule": {
                       "all": [{"kind": "function_definition"},
                               {"has": {"kind": "parameters"}}]}}])
        self.write("scripts/a.py", self.FIXTURE)
        # Both functions have a parameters node, so this matches both -- the point of the
        # test is the NEXT one: that `has` did not reach into the body.
        self.assertEqual(len(self.mine("mutable-default")), 2)

    def test_an_unbounded_has_matches_far_more_than_a_neighbor_one(self):
        """The regression this guards, expressed as a measurable difference rather than a
        claim. `stopBy: end` reaches the body literal; the default must not."""
        matcher = {"all": [{"kind": "function_definition"},
                           {"has": {"kind": "list"}}]}
        self.write("scripts/a.py", self.FIXTURE)

        self.put([{"id": "r", "lang": "python", "severity": "warn", "message": "m",
                   "rule": matcher}], name="neighbor")
        neighbor = len(self.mine("r"))

        (self.packs / "lint.neighbor.pack.json").unlink()
        deep = json.loads(json.dumps(matcher))
        deep["all"][1]["stopBy"] = "end"
        self.put([{"id": "r", "lang": "python", "severity": "warn", "message": "m",
                   "rule": deep}], name="deep")
        end = len(self.mine("r"))

        self.assertLess(neighbor, end,
                        "`has` reached into the body by default — this is the 201-finding "
                        "regression; neighbor=%d end=%d" % (neighbor, end))

    def test_an_unknown_stopby_is_refused(self):
        self.put([{"id": "r", "lang": "python", "severity": "warn", "message": "m",
                   "rule": {"has": {"kind": "list"}, "stopBy": "everywhere"}}])
        self.write("scripts/a.py", self.FIXTURE)
        proc = self.scan(expect=3)
        self.assertIn("stopBy", proc.stdout)


class UnknownKeysFailLoudly(ScanBase):
    """§2.2: never claim ast-grep compatibility beyond the subset implemented."""

    def test_a_rejected_upstream_key_is_named_and_skipped(self):
        self.put([{"id": "r", "lang": "python", "severity": "warn", "message": "m",
                   "rule": {"kind": "call", "nthChild": "2"}}])
        self.write("scripts/a.py", "f()\n")
        proc = self.scan(expect=3)
        self.assertIn("nthChild", proc.stdout)

    def test_an_invented_key_is_refused_rather_than_ignored(self):
        self.put([{"id": "r", "lang": "python", "severity": "warn", "message": "m",
                   "rule": {"kind": "call", "shape": "whatever"}}])
        self.write("scripts/a.py", "f()\n")
        proc = self.scan(expect=3)
        self.assertIn("shape", proc.stdout)

    def test_a_kind_ast_cannot_represent_says_WHY(self):
        """"gt has not implemented this" and "this is impossible at this tier" are different
        facts, and a rule author needs the second one to know not to wait for it."""
        self.put([{"id": "todo", "lang": "python", "severity": "warn", "message": "m",
                   "rule": {"kind": "comment"}}])
        self.write("scripts/a.py", "x = 1  # TODO\n")
        proc = self.scan(expect=3)
        self.assertIn("comment", proc.stdout)
        self.assertIn("discards comments", proc.stdout)


class Matching(ScanBase):
    def test_a_metavariable_binds_so_self_comparison_is_not_any_comparison(self):
        self.put([{"id": "self-cmp", "lang": "python", "severity": "error",
                   "message": "self comparison", "rule": {"pattern": "$A == $A"}}])
        self.write("scripts/a.py", "if a == a:\n    pass\nif a == b:\n    pass\n")
        hits = self.mine("self-cmp")
        self.assertEqual(len(hits), 1, "the metavariable did not bind: %s" % hits)
        self.assertEqual(hits[0]["line"], 1)

    def test_a_call_with_a_keyword_argument_still_matches_a_multi_wildcard(self):
        """tree-sitter keeps one flat argument_list; ast splits args from keywords, so a naive
        `$$$` requires keywords == [] and silently misses `open(f, errors="ignore")`. Two real
        sites in this repo were missed exactly that way."""
        self.put([{"id": "call", "lang": "python", "severity": "warn", "message": "m",
                   "rule": {"pattern": "open($$$A)"}}])
        self.write("scripts/a.py", 'open(p)\nopen(p, errors="ignore")\nopen(p, "rb")\n')
        hits = self.mine("call")
        self.assertEqual(len(hits), 3,
                         "a keyword argument defeated $$$ — the silent under-match: %s" % hits)


class Scope(ScanBase):
    def test_an_extensionless_shebang_file_is_in_scope(self):
        """The census case: the githooks run on every commit in every vault gt touches, and
        an extension-only definition of "code" would skip them."""
        self.put([{"id": "sh-nounset", "lang": "shell", "severity": "warn", "message": "m",
                   "evaluator": "text", "rule": {"not": {"regex": "set -[a-z]*u"}}}])
        p = self.write("prepare-commit-msg", "#!/bin/sh\necho hi\n")
        p.chmod(0o755)
        self.assertEqual([f["path"] for f in self.mine("sh-nounset")],
                         ["prepare-commit-msg"])

    def test_markdown_is_not_code_by_default(self):
        self.put([{"id": "any-text", "severity": "warn", "message": "m",
                   "evaluator": "text", "rule": {"regex": "."}}])
        self.write("README.md", "# hello\n")
        self.assertEqual(self.mine("any-text"), [],
                         "markdown was scanned without an explicit files: opt-in")

    def test_a_files_glob_that_can_never_match_is_a_rule_wired_to_nothing(self):
        """Found in gt's own first pack: `*/scripts/*.py` cannot match `scripts/a.py`, so the
        rule could never fire. A glob is code-like enough to get wrong silently."""
        import fnmatch
        self.assertFalse(fnmatch.fnmatch("scripts/a.py", "*/scripts/*.py"))
        self.assertTrue(fnmatch.fnmatch("scripts/a.py", "*scripts/*.py"))


class EmptinessAndBaseline(ScanBase):
    def test_no_rules_is_exit_four_not_clean(self):
        """A caller must never be able to read "no findings" as "no rules exist".

        Driven in-process with the rule set forced empty, because the CLI cannot reach this
        state while the shipped core pack resolves -- and the guard exists precisely for when
        it does NOT (a pruned release, a failing MANIFEST check, a corrupted pack). Testing it
        through the CLI would have meant deleting a shipped pack from the release under test,
        which is a mutation no test should make.
        """
        from _harness import load_module
        mod = load_module(SCAN, "gt_scan_code_empty")
        mod.load_rules = lambda vault: ([], [], [])
        rc = mod.main([str(self.tree), "--vault", str(self.vault)])
        self.assertEqual(rc, 4, "an empty rule set did not report NOTHING_LOADED")

    def test_the_guard_is_on_the_slot_and_says_an_empty_set_is_not_clean(self):
        """0.16.0's equivalent guard covered the whole registry rather than the slot, so
        silencing one noisy pattern could disable a check entirely and still exit 0."""
        src = SCAN.read_text()
        self.assertIn("CANNOT RUN", src)
        self.assertIn("not a clean scan", src)

    def test_a_baseline_makes_the_second_run_quiet(self):
        self.put([{"id": "self-cmp", "lang": "python", "severity": "error", "message": "m",
                   "rule": {"pattern": "$A == $A"}}])
        self.write("scripts/a.py", "if a == a:\n    pass\n")
        self.scan(expect=1)
        base = self.tmp / "b.json"
        self.py(SCAN, self.tree, "--vault", self.vault, "--write-baseline", str(base))
        self.scan("--baseline", str(base), expect=0)


class SarifShape(ScanBase):
    """Required properties taken from the schema's own `required` arrays, not from prose --
    a prose reading gets invocation.executionSuccessful wrong."""

    def test_every_required_property_is_present(self):
        self.put([{"id": "self-cmp", "lang": "python", "severity": "error", "message": "m",
                   "rule": {"pattern": "$A == $A"}}])
        self.write("scripts/a.py", "if a == a:\n    pass\n")
        out = self.tmp / "r.sarif"
        self.scan("--sarif", str(out), expect=1)
        d = json.loads(out.read_text())
        self.assertEqual(d["version"], "2.1.0")
        run = d["runs"][0]
        self.assertIn("name", run["tool"]["driver"])
        self.assertIn("executionSuccessful", run["invocations"][0])
        for r in run["results"]:
            self.assertIn("message", r)
            self.assertIn("ruleId", r)
        for rd in run["tool"]["driver"]["rules"]:
            self.assertIn("id", rd)


if __name__ == "__main__":
    unittest.main()


class TheAstgrepTier(ScanBase):
    """The optional tier, tested when it is really there.

    Until 2026-09-27 this code path had never executed in a test: the package was not
    installed, so every astgrep test proved only that gt reports its ABSENCE. A tier whose
    presence is untested is a tier that works by assumption.
    """

    def test_a_structural_shell_rule_runs_and_finds(self):
        """What the tier buys: shell gets real structure, not line matching.

        `[ "$x" = y ]` is a POSIX test command; ast-grep sees it as a command node with a
        name, which no regex over lines can distinguish from the same text in a comment.
        """
        env = self.astgrep_env()
        self.put([{"id": "sh-eval-call", "lang": "shell", "severity": "error",
                   "message": "eval in a shell script", "evaluator": "astgrep",
                   "rule": {"pattern": "eval $ARG"}}])
        self.write("run.sh", '#!/bin/sh\nset -u\neval "$CMD"\necho done\n')
        hits = [f for f in self.findings(env=env)["findings"] if f["rule"] == "sh-eval-call"]
        self.assertEqual(len(hits), 1, "the astgrep tier did not evaluate: %s" % hits)
        self.assertEqual(hits[0]["line"], 3)

    def test_the_same_rule_is_skipped_when_the_tier_is_absent(self):
        """Both halves of the contract, same rule, one difference: the tier."""
        self.put([{"id": "sh-eval-call", "lang": "shell", "severity": "error",
                   "message": "eval in a shell script", "evaluator": "astgrep",
                   "rule": {"pattern": "eval $ARG"}}])
        self.write("run.sh", '#!/bin/sh\nset -u\neval "$CMD"\n')
        proc = self.scan(expect=3, env=self.STDLIB_ONLY)
        self.assertIn("SKIPPED", proc.stdout)
        self.assertIn("sh-eval-call", proc.stdout)

    def test_an_unsupported_language_is_reported_not_crashed(self):
        """ast-grep-py is a RUST extension: an unknown language raises PanicException, which
        subclasses BaseException and sails through `except Exception`. Measured on 0.30.0 --
        asking it for "markdown" panics. One unsupported file must not take the scan down.
        """
        env = self.astgrep_env()
        self.put([{"id": "md-structural", "severity": "warn", "message": "m",
                   "evaluator": "astgrep", "files": "*.md",
                   "rule": {"pattern": "$X"}}])
        self.write("notes.md", "# heading\n")
        self.write("scripts/a.py", "x = 1\n")
        proc = self.scan(env=env)
        self.assertIn(proc.returncode, (0, 1, 3), proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("PanicException", proc.stderr)

    def test_forcing_the_tier_list_is_announced(self):
        """A scan whose tiers were overridden is not a scan of this machine, and must say so."""
        self.put([{"id": "x", "lang": "python", "severity": "warn", "message": "m",
                   "rule": {"kind": "call"}}])
        self.write("scripts/a.py", "f()\n")
        proc = self.scan(env=self.STDLIB_ONLY)
        self.assertIn("FORCED", proc.stderr)
