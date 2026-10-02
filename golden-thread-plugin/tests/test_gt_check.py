"""gt_check.py -- the validation host, and module.json `checkers` (0.18.1).

Contracts pinned here (request 2026-09-15-validation-checker-slots):
  * both module validators accept a valid `checkers` list and reject an unknown checker key
    (naming it), a checker script the module does not ship, and a fix grant on a protected path;
  * `list` prints each installed checker, its module, what it applies to, and whether its
    required tools are present;
  * `run` runs only the checkers whose globs or MIME types match; exit 0 only when every
    applicable checker passed; a missing tool, a timeout and malformed output are each
    `cannot-check` with the reason, and non-zero;
  * checkers run concurrently up to `parallel_max`, one at a time at 1 or parallel_work off;
  * `--event commit-msg` runs only checkers bound to it and hands them the message file;
  * a receipt names each checker, the content hash it checked and its verdict;
  * commit_checks on: a commit whose staged content no passing receipt covers is refused,
    naming the failing checker; off (default): commits behave exactly as before;
  * a checker that modifies a file during `run` is a violation and `fail`;
  * gt_doctor reports the checker count and names a checker whose tool is missing;
  * `install.sh --without` removes a module's checkers from `list`.
All in throwaway homes, fixture plugin roots and fixture repos; no network.
"""
import json
import shutil
import time
import unittest

from _harness import Sandbox, REPO, SCRIPTS, HOOKS, GT, WIKI, load_module, needs_dev
from _checkers import make_module, sha

GT_CHECK = SCRIPTS / "gt_check.py"
PLUGINS = REPO / "dev" / "plugins.py"
COMPONENTS = SCRIPTS / "gt_components.py"

HTML = ({"id": "html-marker", "globs": ["*.html"]}, {"mark": "BAD-MARKER"})
MD = ({"id": "md-pass", "globs": ["*.md"]}, {"behaviour": "pass"})
TOOL = ({"id": "needs-tool", "globs": ["*.txt"], "requires_tools": ["gt-fixture-missing-tool"]},
        {"behaviour": "pass"})


class CheckBase(Sandbox):
    checkers = (HTML, MD, TOOL)

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "plugins"
        self.vd = make_module(self.root, list(self.checkers))
        self.tree = self.tmp / "tree"
        self.tree.mkdir()
        (self.tree / "a.html").write_text("<p>BAD-MARKER</p>\n")
        (self.tree / "b.md").write_text("# fine\n")
        (self.tree / "c.txt").write_text("text\n")
        (self.tree / "d.html").write_text("<p>ok</p>\n")

    def check(self, *args, cwd=None, **kw):
        return self.py(GT_CHECK, *args, "--plugin-root", self.root,
                       cwd=str(cwd or self.tree), **kw)

    def run_json(self, *args, **kw):
        p = self.check("run", *args, "--json", **kw)
        try:
            return p, json.loads(p.stdout)
        except ValueError:
            self.fail("run --json printed no JSON:\n%s\n%s" % (p.stdout, p.stderr))

    def verdicts(self, data):
        return {r["checker"]: r["verdict"] for r in data["results"]}


@needs_dev
class ManifestTest(Sandbox):
    """Both validators, one verdict (the validator blocks are byte-identical; this proves the
    new key behaves the same through both CLIs)."""

    def module(self, **over):
        vd = make_module(self.tmp / "r", [HTML])
        data = json.loads((vd / "module.json").read_text())
        for k, v in over.items():
            data["checkers"][0][k] = v
        (vd / "module.json").write_text(json.dumps(data))
        return vd

    def verdict(self, vd):
        outs = [self.py(t, "module-check", vd) for t in (PLUGINS, COMPONENTS)]
        self.assertEqual(outs[0].returncode, outs[1].returncode, outs[0].stdout + outs[1].stdout)
        return outs[0]

    def test_a_valid_checkers_list_is_accepted(self):
        p = self.verdict(self.module(fixes=["*.html"], rules=["html"], timeout=5,
                                     mime=["text/html"], events=["pre-commit"]))
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_an_unknown_checker_key_is_rejected_by_name(self):
        p = self.verdict(self.module(colour="blue"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("checkers[0] unknown key 'colour'", p.stdout)

    def test_a_script_the_module_does_not_ship_is_rejected(self):
        p = self.verdict(self.module(script="nope.py"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("'nope.py' does not exist under scripts/", p.stdout)

    def test_a_fix_grant_on_a_protected_path_is_rejected(self):
        p = self.verdict(self.module(fixes=["core-rules/*"]))
        self.assertEqual(p.returncode, 1)
        self.assertIn("names a protected path", p.stdout)

    def test_the_validator_blocks_are_identical(self):
        import re
        blk = [re.search(r"# BEGIN module validator.*?# END module validator",
                         p.read_text(), re.S).group(0) for p in (PLUGINS, COMPONENTS)]
        self.assertEqual(blk[0], blk[1])
        self.assertIn('"checkers"', blk[0])


class ListAndRunTest(CheckBase):
    def test_list_names_module_applies_to_and_tool_state(self):
        p = self.check("list")
        self.assertOk(p)
        self.assertIn("fx/html-marker", p.stdout)
        self.assertIn("module fx", p.stdout)
        self.assertIn("globs *.html", p.stdout)
        self.assertIn("MISSING TOOL(S): gt-fixture-missing-tool", p.stdout)
        self.assertIn("3 checker(s) installed", p.stdout)

    def test_only_matching_checkers_run(self):
        p, data = self.run_json("b.md")
        self.assertEqual(self.verdicts(data), {"fx/md-pass": "pass"})
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_mime_types_match_too(self):
        root = self.tmp / "mime-root"
        make_module(root, [({"id": "by-mime", "mime": ["text/html"]}, {"behaviour": "pass"})],
                    name="mm")
        p = self.py(GT_CHECK, "run", "d.html", "b.md", "--json", "--plugin-root", root,
                    cwd=str(self.tree))
        self.assertEqual({r["checker"]: r["files"] for r in json.loads(p.stdout)["results"]},
                         {"mm/by-mime": {"d.html": sha(self.tree / "d.html")}})

    def test_exit_is_nonzero_when_one_fails(self):
        p, data = self.run_json("a.html", "b.md")
        self.assertEqual(self.verdicts(data), {"fx/html-marker": "fail", "fx/md-pass": "pass"})
        self.assertEqual(p.returncode, 1)
        finding = data["results"][0]["findings"][0]
        self.assertEqual((finding["file"], finding["line"], finding["rule"]),
                         ("a.html", 1, "marker"))

    def test_a_missing_tool_is_cannot_check_and_nonzero(self):
        p, data = self.run_json("c.txt")
        r = data["results"][0]
        self.assertEqual(r["verdict"], "cannot-check")
        self.assertIn("gt-fixture-missing-tool", r["reason"])
        self.assertEqual(p.returncode, 1)

    def test_nothing_applicable_is_not_a_pass(self):
        (self.tree / "e.py").write_text("x = 1\n")
        p = self.check("run", "e.py")
        self.assertEqual(p.returncode, 3)
        self.assertIn("nothing was checked", p.stdout)

    def test_a_receipt_names_checker_hash_and_verdict(self):
        self.run_json("a.html", "b.md")
        rows = [json.loads(l) for l in (self.home / ".claude" / "golden-thread" /
                                        "check-runs.jsonl").read_text().splitlines()]
        files = rows[-1]["files"]
        self.assertEqual(files["a.html"], {"sha256": sha(self.tree / "a.html"),
                                           "checkers": {"fx/html-marker": "fail"}})
        self.assertEqual(files["b.md"]["checkers"], {"fx/md-pass": "pass"})


class BadCheckersTest(CheckBase):
    checkers = (
        ({"id": "sleeper", "globs": ["*.html"], "timeout": 1}, {"behaviour": "pass", "sleep": 8}),
        ({"id": "garbage", "globs": ["*.md"]}, {"behaviour": "garbage"}),
        ({"id": "mutator", "globs": ["*.txt"]}, {"behaviour": "mutate"}),
    )

    def test_a_timeout_is_killed_and_cannot_check(self):
        t0 = time.time()
        p, data = self.run_json("d.html")
        self.assertLess(time.time() - t0, 6, "the checker was not killed at its timeout")
        r = data["results"][0]
        self.assertEqual(r["verdict"], "cannot-check")
        self.assertIn("timed out", r["reason"])
        self.assertEqual(p.returncode, 1)

    def test_malformed_output_is_cannot_check_with_the_reason(self):
        p, data = self.run_json("b.md")
        r = data["results"][0]
        self.assertEqual(r["verdict"], "cannot-check")
        self.assertIn("not the result shape", r["reason"])
        self.assertEqual(p.returncode, 1)

    def test_a_checker_that_writes_is_a_violation_and_fails(self):
        before = (self.tree / "c.txt").read_bytes()
        p, data = self.run_json("c.txt")
        r = data["results"][0]
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("read-only violation", r["reason"])
        self.assertIn("gt-check/read-only-violation", [f["rule"] for f in r["findings"]])
        self.assertEqual((self.tree / "c.txt").read_bytes(), before,
                         "the real file must be untouched -- the checker only saw a snapshot")
        self.assertEqual(p.returncode, 1)


class ParallelTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.log = self.tmp / "log"
        self.log.mkdir()
        self.root = self.tmp / "plugins"
        make_module(self.root, [({"id": "p%d" % i, "globs": ["*.md"]},
                                 {"behaviour": "pass", "sleep": 0.6, "log": str(self.log)})
                                for i in range(3)])
        self.tree = self.tmp / "tree"
        self.tree.mkdir()
        (self.tree / "a.md").write_text("x\n")

    def spans(self):
        out = []
        for i in range(3):
            s = float((self.log / ("p%d.start" % i)).read_text())
            e = float((self.log / ("p%d.end" % i)).read_text())
            out.append((s, e))
        return sorted(out)

    def overlaps(self):
        sp = self.spans()
        return any(sp[i + 1][0] < sp[i][1] for i in range(len(sp) - 1))

    def go(self, **cfg):
        self.config(**cfg)
        p = self.py(GT_CHECK, "run", "a.md", "--plugin-root", self.root, cwd=str(self.tree))
        self.assertOk(p)

    def test_parallel_max_1_runs_one_at_a_time(self):
        self.go(parallel_max="1")
        self.assertFalse(self.overlaps(), self.spans())

    def test_parallel_work_off_runs_one_at_a_time(self):
        self.go(parallel_work="off")
        self.assertFalse(self.overlaps(), self.spans())

    def test_parallel_max_4_overlaps(self):
        self.go(parallel_max="4", parallel_profile={"cpu_max": 4, "io_max": 8})
        self.assertTrue(self.overlaps(), self.spans())


class CommitMsgTest(CheckBase):
    checkers = (HTML, ({"id": "ticket", "events": ["commit-msg"]}, {"behaviour": "commitmsg"}))

    def test_only_commit_msg_checkers_run_with_the_message(self):
        good, bad = self.tmp / "good.txt", self.tmp / "bad.txt"
        good.write_text("ABC-123 fix the thing\n")
        bad.write_text("fix the thing\n")
        p, data = self.run_json("--event", "commit-msg", "--message-file", good)
        self.assertEqual(self.verdicts(data), {"fx/ticket": "pass"})
        self.assertEqual(p.returncode, 0)
        p, data = self.run_json("--event", "commit-msg", "--message-file", bad)
        self.assertEqual(self.verdicts(data), {"fx/ticket": "fail"})
        self.assertEqual(p.returncode, 1)


class CommitGateTest(CheckBase):
    def setUp(self):
        super().setUp()
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True)
        for f in HOOKS.iterdir():
            if f.is_file():
                shutil.copy2(f, self.hooks / f.name)
        comp = load_module(COMPONENTS, "gt_components_for_check_gate")
        for name in ("gt_paths.py",) + tuple(comp.HOOK_DIR_SCRIPTS):
            if (SCRIPTS / name).is_file():
                shutil.copy2(SCRIPTS / name, self.hooks / name)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.repo = self.tmp / "repo"
        self.git_init(self.repo)

    def decide(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"},
                   "cwd": str(self.repo), "hook_event_name": "PreToolUse"}
        p = self.sh(self.hooks / "guard_test_before_commit.sh", input=json.dumps(payload))
        self.assertOk(p)
        return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else {}

    def stage(self, rel, text):
        (self.repo / rel).write_text(text)
        self.run_cmd(["git", "-C", self.repo, "add", rel])

    def gate_on(self):
        self.config(vault_path=str(self.tmp), commit_checks="on")

    def test_off_by_default_commits_behave_as_before(self):
        self.config(vault_path=str(self.tmp))
        self.stage("a.html", "<p>BAD-MARKER</p>\n")
        self.assertEqual(self.decide(), {}, "with commit_checks off nothing may change")

    def test_a_passing_receipt_lets_the_commit_through(self):
        self.gate_on()
        self.stage("b.md", "# fine\n")
        self.assertOk(self.check("run", "--staged", cwd=self.repo))
        self.assertEqual(self.decide(), {})

    def test_changed_content_without_a_rerun_is_refused(self):
        self.gate_on()
        self.stage("b.md", "# fine\n")
        self.assertOk(self.check("run", "--staged", cwd=self.repo))
        self.stage("b.md", "# changed after the run\n")
        hso = self.decide()
        self.assertEqual(hso.get("permissionDecision"), "deny")
        self.assertIn("b.md", hso["permissionDecisionReason"])

    def test_a_failing_checker_is_named(self):
        self.gate_on()
        self.stage("a.html", "<p>BAD-MARKER</p>\n")
        self.check("run", "--staged", cwd=self.repo)
        hso = self.decide()
        self.assertEqual(hso.get("permissionDecision"), "deny")
        self.assertIn("fx/html-marker", hso["permissionDecisionReason"])


class DoctorTest(CheckBase):
    def test_doctor_counts_checkers_and_names_a_missing_tool(self):
        p = self.py(SCRIPTS / "gt_doctor.py", "--only", "checkers", "--plugin-root", self.root)
        self.assertIn("3 checker(s) installed", p.stdout)
        self.assertIn("fx/needs-tool", p.stdout)
        self.assertIn("gt-fixture-missing-tool", p.stdout)


class InstallWithoutTest(Sandbox):
    """`install.sh --without fx` takes the module's checkers out of `list`."""

    def test_without_removes_the_checkers(self):
        IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
        repo = self.tmp / "src" / "golden-thread-plugin"
        repo.mkdir(parents=True)
        shutil.copy2(REPO / "install.sh", repo / "install.sh")
        shutil.copytree(GT, repo / "golden-thread" / GT.name, ignore=IGNORE)
        shutil.copytree(WIKI, repo / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)
        make_module(repo, [HTML])
        gt_src = repo / "golden-thread" / GT.name
        self.assertOk(self.py(gt_src / "scripts" / "gt_components.py", "manifest", gt_src))
        self.assertOk(self.sh(repo / "install.sh", "--no-vault", timeout=300))
        lst = self.py(GT_CHECK, "list")
        self.assertIn("fx/html-marker", lst.stdout, lst.stdout + lst.stderr)
        self.assertOk(self.sh(repo / "install.sh", "--no-vault", "--without", "fx", timeout=300))
        lst = self.py(GT_CHECK, "list")
        self.assertNotIn("fx/html-marker", lst.stdout)
        self.assertIn("no installed checker", lst.stdout)


if __name__ == "__main__":
    unittest.main()
