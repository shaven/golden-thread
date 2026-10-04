"""Content-keyed scanner baselines (0.20.0): a version cut no longer re-flags accepted lines.

Until 0.20.0 tests/secrets-baseline.json and .gt/code-baseline.json keyed an entry by PATH, so
every cut (golden-thread/0.20.0/... copied to golden-thread/0.20.1/...) re-flagged every line
the owner had already accepted. Now an entry is (rule, the path with release-version segments
as <ver>, a hash of the normalised line, a count). Pinned:

  * a byte-identical line moved by a cut stays accepted (secrets and code);
  * an edited line, the same text in an unrelated file, a different rule, or one more copy of
    an accepted line still fails;
  * the secrets baseline holds no matched text, no value, no fragment of one -- only a salted
    PBKDF2 (no values at rest);
  * an old path-keyed baseline is still honoured.

The credential is ASSEMBLED AT RUNTIME: the release's own secrets gate scans this file.
"""
import json
import shutil

from _harness import PYTHON, SCRIPTS, Sandbox, load_module

SECRETS = SCRIPTS / "gt_secrets.py"
CODE = SCRIPTS / "gt_scan_code.py"
VALUE = "Zq8" + "vN3xL0pWm7Tb" + "Y2cKe9RfUa4Hj"
OTHER = "Kp4" + "wR8tM2nXq6Ld" + "B7vHs3JcEe9Fg"          # same length, different value
LINE = 'API_TOKEN = "%s"\n'


class Base(Sandbox):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "repo"
        self.rel = self.root / "golden-thread" / "0.20.0"
        (self.rel / "tests").mkdir(parents=True)
        self.base = self.tmp / "baseline.json"

    def run_tool(self, tool, *args):
        return self.run_cmd([PYTHON, tool, str(self.root), *[str(a) for a in args]])

    def cut(self, new="0.20.1"):
        """What a version cut does: the release dir is copied under the new version."""
        dst = self.rel.parent / new
        shutil.move(str(self.rel), str(dst))
        self.rel = dst


class SecretsBaseline(Base):
    def setUp(self):
        super().setUp()
        self.cfg = self.rel / "tests" / "cfg.py"
        self.cfg.write_text(LINE % VALUE)
        p = self.run_tool(SECRETS, "--write-baseline", self.base)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def scan(self):
        return self.run_tool(SECRETS, "--baseline", self.base)

    def test_a_line_moved_by_a_version_cut_stays_accepted(self):
        self.assertEqual(self.scan().returncode, 0)
        self.cut()
        p = self.scan()
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("0 new finding(s)", p.stdout)

    def test_an_edited_line_fails(self):
        self.cut()
        (self.rel / "tests" / "cfg.py").write_text(LINE % OTHER)
        p = self.scan()
        self.assertEqual(p.returncode, 1, p.stdout)

    def test_the_same_text_in_an_unrelated_file_fails(self):
        (self.rel / "tests" / "elsewhere.py").write_text(LINE % VALUE)
        p = self.scan()
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("elsewhere.py", p.stdout)

    def test_one_more_copy_of_an_accepted_line_fails(self):
        self.cfg.write_text(LINE % VALUE + LINE % VALUE)
        p = self.scan()
        self.assertEqual(p.returncode, 1, p.stdout)

    def test_no_secret_text_at_rest(self):
        text = self.base.read_text()
        self.assertNotIn(VALUE, text)
        for i in range(len(VALUE) - 5):
            self.assertNotIn(VALUE[i:i + 6], text, "a fragment of the value is in the baseline")
        self.assertNotIn("API_TOKEN", text)
        doc = json.loads(text)
        self.assertEqual(doc["format"], 2)
        (e,) = doc["entries"]
        self.assertEqual(set(e), {"rule", "path", "hash", "count"})
        self.assertEqual(e["path"], "golden-thread/<ver>/tests/cfg.py")
        self.assertTrue(e["hash"].startswith("pbkdf2-sha256-"), e["hash"])

    def test_an_old_path_keyed_baseline_is_still_honoured(self):
        old = {"accepted": [["golden-thread/0.20.0/tests/cfg.py",
                             "generic.assigned-credential", len(VALUE)]]}
        self.base.write_text(json.dumps(old))
        self.assertEqual(self.scan().returncode, 0)
        self.cut()
        self.assertEqual(self.scan().returncode, 1, "an old baseline is path-keyed, as before")


class CodeBaseline(Base):
    def setUp(self):
        super().setUp()
        (self.rel / "scripts").mkdir()
        self.src = self.rel / "scripts" / "shipped.py"
        self.src.write_text("def f(x):\n    assert x > 0\n    return x\n")
        p = self.run_tool(CODE, "--write-baseline", self.base)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("recorded 1 finding", p.stdout)

    def scan(self):
        return self.run_tool(CODE, "--baseline", self.base)

    def test_a_line_moved_by_a_version_cut_stays_accepted(self):
        self.cut()
        p = self.scan()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_an_edited_line_or_another_file_fails(self):
        self.cut()
        (self.rel / "scripts" / "shipped.py").write_text(
            "def f(x):\n    assert x >= 0\n    return x\n")
        self.assertEqual(self.scan().returncode, 1)
        (self.rel / "scripts" / "shipped.py").write_text(
            "def f(x):\n    assert x > 0\n    return x\n")
        self.assertEqual(self.scan().returncode, 0)
        (self.rel / "scripts" / "other.py").write_text("def g(x):\n    assert x > 0\n")
        self.assertEqual(self.scan().returncode, 1)

    def test_reasons_survive_a_rewrite(self):
        doc = json.loads(self.base.read_text())
        doc["reasons"] = {"golden-thread/0.20.0/scripts/shipped.py::py-assert-in-shipped-code":
                          "vendored"}
        self.base.write_text(json.dumps(doc))
        p = self.run_tool(CODE, "--write-baseline", self.base)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("vendored", self.base.read_text())


class Normalisation(Sandbox):
    def test_paths_and_lines(self):
        b = load_module(SCRIPTS / "gt_baseline.py", "gt_baseline_t")
        self.assertEqual(b.norm_path("golden-thread/0.20.0/scripts/x.py"),
                         "golden-thread/<ver>/scripts/x.py")
        self.assertEqual(b.norm_path("golden-thread-lotr/0.3.0/a.py"), "golden-thread-lotr/<ver>/a.py")
        self.assertEqual(b.norm_path("tests/x.py"), "tests/x.py")
        self.assertEqual(b.norm_line("  a   =\tb  "), "a = b")
        h1 = b.slow_hash("r", "golden-thread/0.20.0/t.py", "x = 1")
        self.assertEqual(h1, b.slow_hash("r", "golden-thread/0.20.1/t.py", "  x  =  1 "))
        self.assertNotEqual(h1, b.slow_hash("r2", "golden-thread/0.20.0/t.py", "x = 1"))
        self.assertNotEqual(h1, b.slow_hash("r", "golden-thread/0.20.0/u.py", "x = 1"))
