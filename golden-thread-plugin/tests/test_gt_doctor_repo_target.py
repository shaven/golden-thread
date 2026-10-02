"""gt_doctor `repo-target` (0.18.1, 2026-09-25-repo-scoped-tools-target-the-vault).

On 2026-09-25 Claude Code's /security-review, run in a gt session, reviewed the VAULT's 624 KB
diff instead of a 174-line code change, because the working directory was the vault and the
vault is a git repo. Contract:

  * same repo: both paths named, says they match, exit code unchanged by the row
  * different repo: names BOTH paths and says they differ
  * not a repo: says so explicitly, and the row is not an ok/clean token
  * the row is always emitted (no silent outcome)
  * gt-open's SKILL.md carries the one-line statement, with a concrete example
  * no existing check changes its verdict
"""
import json
import os

from _harness import GT
from test_gt_doctor import DoctorBase


class RepoTarget(DoctorBase):
    def setUp(self):
        super().setUp()
        self.vault = self.git_init(self.tmp / "vault")
        self.config(vault_path=str(self.vault))

    def row(self, cwd):
        p = self.doctor("--json", "--only", "repo-target", "--vault", str(self.vault),
                        cwd=str(cwd))
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        rows = json.loads(p.stdout)["checks"]
        self.assertEqual(["repo-target"], [r["check"] for r in rows], "the row must always appear")
        return p, rows[0]

    def test_same_repo_is_a_note_naming_both_and_exit_is_clean(self):
        p, r = self.row(self.vault)
        self.assertEqual("note", r["state"])
        self.assertIn("VAULT", r["summary"])
        self.assertIn("/security-review", r["summary"])
        self.assertIn("gt_code_review.py", r["summary"])
        real = os.path.realpath(str(self.vault))
        self.assertIn("repo:  %s" % real, r["detail"])
        self.assertIn("vault: %s" % self.vault, r["detail"])
        self.assertEqual(0, p.returncode, "a normal gt session is not unhealthy")
        self.assertEqual("ok", json.loads(p.stdout)["worst"])

    def test_a_subdirectory_of_the_vault_still_resolves_to_the_vault(self):
        sub = self.vault / "Projects"
        sub.mkdir()
        _, r = self.row(sub)
        self.assertIn("VAULT", r["summary"])

    def test_different_repo_names_both_paths(self):
        code = self.git_init(self.tmp / "code")
        p, r = self.row(code)
        self.assertEqual("note", r["state"])
        self.assertIn("NOT the vault", r["summary"])
        self.assertIn(os.path.realpath(str(code)), r["summary"])
        self.assertIn(str(self.vault), r["summary"])
        self.assertEqual(0, p.returncode)

    def test_not_a_repo_says_so_and_is_not_clean(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        p, r = self.row(plain)
        self.assertNotEqual("ok", r["state"])
        self.assertIn("NOT inside any git repo", r["summary"])
        text = self.doctor("--only", "repo-target", "--vault", str(self.vault),
                           cwd=str(plain)).stdout
        line = next(l for l in text.splitlines() if "repo-target" in l)
        self.assertFalse(line.startswith("ok"), line)
        self.assertTrue(line.startswith("i "), line)

    def test_no_existing_check_changes_its_verdict(self):
        import test_gt_doctor  # noqa: F401  (same harness)
        sys_checks = ["version", "components", "wiring", "core-rules", "modules", "vault",
                      "schedule", "workers", "push", "gt-src", "lint", "astgrep"]
        args = []
        for c in sys_checks:
            args += ["--only", c]
        before = self.doctor("--json", *args, "--vault", str(self.vault), cwd=str(self.vault))
        after = self.doctor("--json", *args, "--only", "repo-target", "--vault",
                            str(self.vault), cwd=str(self.vault))
        b, a = json.loads(before.stdout), json.loads(after.stdout)
        strip = lambda rows: [(r["check"], r["state"]) for r in rows if r["check"] != "repo-target"]
        self.assertEqual(strip(b["checks"]), strip(a["checks"]))
        self.assertEqual((before.returncode, b["worst"]), (after.returncode, a["worst"]))


class GtOpenSaysItOnce(DoctorBase):
    def test_gt_open_states_repo_scoped_commands_target_the_vault(self):
        text = " ".join((GT / "skills" / "gt-open" / "SKILL.md").read_text().split())
        self.assertIn("repo-scoped commands", text)
        self.assertIn("will target the **vault**", text)
        self.assertIn("`/security-review`", text)
        self.assertIn("`/code-review`", text)
        self.assertIn("gt_code_review.py plan <repo>", text)
