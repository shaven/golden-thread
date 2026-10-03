"""copygt.sh + validate-install.py -- gt-src's own tools, written into it by dev/sync-gt-src.sh.

The receiving machine runs `copygt.sh --dest <repo>`: verify gt-src against SHA256SUMS, mirror it
onto the repository (deletes included; .git and untracked files untouched), run install.sh, run
validate-install.py, and commit only when the validation is fully clean. It never pushes:
pushing is outside copygt.sh (owner, 2026-10-01).

Everything runs against a throwaway gt-src, a throwaway destination repository with a bare
`origin` standing in for GitHub (it must never move: copygt.sh does not push), and a throwaway HOME.

Both tools live in dev/ here and only in gt-src there, so this whole module is dev-only.
"""
import hashlib
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from _harness import Sandbox, REPO, needs_dev, latest_version_dir
import test_install as _ti   # module import only: its TestCases must not be collected here

COPYGT = REPO / "dev" / "copygt.sh"
VALIDATE = REPO / "dev" / "validate-install.py"
GT_SRC_ONLY = ("copygt.sh", "validate-install.py", "SHA256SUMS", "SOURCE.json")
EXCLUDED = ("CLAUDE.md", "SUBMISSIONS.md", "golden-thread-plugin/CLAUDE.md",
            "golden-thread-plugin/BUILD-NOTE.md", "golden-thread-plugin/dev/publish.sh",
            "Golden Thread.code-workspace")


def publish_sums(src: Path, commit="c0ffee" * 6 + "abcd", **versions):
    """Write SHA256SUMS + SOURCE.json the way dev/sync-gt-src.sh does."""
    lines = []
    for p in sorted(src.rglob("*")):
        if not p.is_file() or p.name in ("SHA256SUMS", "SOURCE.json") or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(src).as_posix()
        lines.append("%s  ./%s" % (hashlib.sha256(p.read_bytes()).hexdigest(), rel))
    (src / "SHA256SUMS").write_bytes(("\n".join(lines) + "\n").encode())  # LF, as sync-gt-src.sh writes it
    tree = hashlib.sha256((src / "SHA256SUMS").read_bytes()).hexdigest()
    data = {"commit": commit, "layout": "repository", "plugin_path": "golden-thread-plugin/"}
    data.update(versions or {"gt": "0.0.1"})
    data.update(synced_at="2026-10-01T00:00:00+0000", files=len(lines), tree_sha256=tree)
    (src / "SOURCE.json").write_text(json.dumps(data))
    return tree


def snapshot(root: Path, include_git=True):
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if not include_git and (rel == ".git" or rel.startswith(".git/")):
            continue
        if p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class CopygtBase(Sandbox):
    def setUp(self):
        super().setUp()
        if not shutil.which("git") or not (shutil.which("shasum") or shutil.which("sha256sum")):
            self.skipTest("git and shasum required")
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.src = self.tmp / "gt-src"
        self.dest = self.tmp / "dest"
        self.remote = self.tmp / "origin.git"
        self.state = self.home / ".claude" / "golden-thread" / "copygt"

    def add_tools(self):
        for t in ("copygt.sh", "validate-install.py"):
            shutil.copy2(REPO / "dev" / t, self.src / t)
            os.chmod(self.src / t, 0o755)

    def make_dest(self, files, untracked=()):
        """A git repo holding an OLD gt-src (`files` tracked), with a bare origin it pushed to."""
        for rel, text in files.items():
            p = self.dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.git_init(self.dest)
        self.run_cmd(["git", "init", "-q", "--bare", "-b", "main", self.remote])
        self.run_cmd(["git", "-C", self.dest, "remote", "add", "origin", self.remote])
        self.assertOk(self.run_cmd(["git", "-C", self.dest, "push", "-q", "-u", "origin", "main"]))
        for rel, text in dict(untracked).items():
            p = self.dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)

    def copygt(self, *args, timeout=900):
        return self.run_cmd(["bash", self.src / "copygt.sh", *args], timeout=timeout)

    def head(self, repo):
        return self.run_cmd(["git", "-C", repo, "rev-parse", "HEAD"]).stdout.strip()

    def tracked(self):
        return set(self.run_cmd(["git", "-C", self.dest, "ls-files"]).stdout.splitlines())


@needs_dev
class MirrorTest(CopygtBase):
    """A toy gt-src whose install.sh does nothing: the mirror, the refusals and the stop-before-
    commit, without the cost of a real install."""

    def setUp(self):
        super().setUp()
        files = {
            "README.md": "# new readme\n",
            "CHANGELOG.md": "# changelog\n",
            ".gitignore": "*.code-workspace\n__pycache__/\n",
            "golden-thread-plugin/install.sh": "#!/bin/sh\necho toy install\n",
            "golden-thread-plugin/MANUAL.md": "# manual\n",
            "golden-thread-plugin/sub dir/file with space.txt": "spaced\n",
            # gt-src must never carry these -- if one slips in, copygt.sh still keeps it out
            "CLAUDE.md": "# dev session\n",
        }
        for rel, text in files.items():
            p = self.src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        os.chmod(self.src / "golden-thread-plugin" / "install.sh", 0o755)
        self.add_tools()
        publish_sums(self.src)
        old = {
            "README.md": "# old readme\n",                              # changed in gt-src
            "golden-thread-plugin/MANUAL.md": "# manual\n",             # unchanged
            "golden-thread-plugin/dropped.txt": "gone in gt-src\n",     # dropped
            "golden-thread-plugin/olddir/gone.txt": "gone too\n",       # dropped, with its dir
        }
        old.update({rel: "excluded\n" for rel in EXCLUDED})             # the five, already held
        self.make_dest(old, untracked={"local-notes.txt": "mine\n",
                                       "golden-thread-plugin/scratch.txt": "mine too\n"})
        self.report = self.tmp / "reports" / "r.md"

    def test_refuses_without_dest(self):
        before = snapshot(self.dest)
        p = self.copygt()
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("--dest", p.stdout)
        self.assertEqual(snapshot(self.dest), before)

    def test_a_corrupted_sha256sums_aborts_before_any_write(self):
        sums = self.src / "SHA256SUMS"
        lines = sums.read_text().splitlines()
        lines[0] = ("0" * 64) + lines[0][64:]
        sums.write_bytes(("\n".join(lines) + "\n").encode())
        before = snapshot(self.dest)
        p = self.copygt("--dest", self.dest, "--report", self.report)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("Nothing was written", p.stdout)
        self.assertEqual(snapshot(self.dest), before, "the destination changed")
        self.assertFalse(self.report.exists())
        self.assertFalse((self.state / "applied.json").exists())

    def test_a_changed_file_in_gt_src_aborts_before_any_write(self):
        (self.src / "golden-thread-plugin" / "MANUAL.md").write_text("half-synced\n")
        before = snapshot(self.dest)
        p = self.copygt("--dest", self.dest)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertEqual(snapshot(self.dest), before)

    def test_a_sums_file_that_is_not_the_published_one_aborts(self):
        # Every listed file verifies, but SHA256SUMS itself is not what SOURCE.json vouches for.
        data = json.loads((self.src / "SOURCE.json").read_text())
        data["tree_sha256"] = "f" * 64
        (self.src / "SOURCE.json").write_text(json.dumps(data))
        p = self.copygt("--dest", self.dest)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("SOURCE.json", p.stdout)

    def test_refuses_a_destination_with_uncommitted_tracked_changes(self):
        (self.dest / "README.md").write_text("work in progress\n")
        p = self.copygt("--dest", self.dest)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual((self.dest / "README.md").read_text(), "work in progress\n")

    def test_dry_run_lists_every_add_change_and_delete_and_writes_nothing(self):
        before = snapshot(self.dest)
        p = self.copygt("--dest", self.dest, "--dry-run")
        self.assertOk(p)
        out = p.stdout
        for rel in ("CHANGELOG.md", "golden-thread-plugin/install.sh",
                    "golden-thread-plugin/sub dir/file with space.txt"):
            self.assertIn("add     " + rel, out)
        self.assertIn("change  README.md", out)
        for rel in ("golden-thread-plugin/dropped.txt", "golden-thread-plugin/olddir/gone.txt") + EXCLUDED:
            self.assertIn("delete  " + rel, out)
        self.assertNotIn("golden-thread-plugin/MANUAL.md", out, "an unchanged file is not listed")
        self.assertEqual(snapshot(self.dest), before, "--dry-run wrote to the destination")
        self.assertFalse(self.state.exists(), "--dry-run wrote state")
        # the same listing the real run applies
        real = self.copygt("--dest", self.dest, "--report", self.report)
        listing = lambda s: [l for l in s.splitlines() if l.startswith("  ") and
                             l.split()[0] in ("add", "change", "delete")]
        self.assertEqual(listing(out), listing(real.stdout))

    def test_mirror_deletes_dropped_updates_changed_and_leaves_git_and_untracked_alone(self):
        git_before = {k: v for k, v in snapshot(self.dest).items() if k.startswith(".git/")}
        head = self.head(self.dest)
        p = self.copygt("--dest", self.dest, "--report", self.report)
        # the toy install has no gt to validate: the run is NOT clean, so it stops before committing
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("NOT CLEAN", p.stdout)
        d = self.dest
        self.assertEqual((d / "README.md").read_text(), "# new readme\n", "a changed file was not updated")
        self.assertEqual((d / "golden-thread-plugin/sub dir/file with space.txt").read_text(), "spaced\n")
        self.assertTrue(os.access(d / "golden-thread-plugin" / "install.sh", os.X_OK))
        self.assertFalse((d / "golden-thread-plugin" / "dropped.txt").exists(), "a dropped file survived")
        self.assertFalse((d / "golden-thread-plugin" / "olddir").exists(), "an emptied dir survived")
        for rel in EXCLUDED:
            self.assertFalse((d / rel).exists(), "%s must be removed from the destination" % rel)
        self.assertFalse((d / "golden-thread-plugin" / "dev").exists())
        for rel in GT_SRC_ONLY:
            self.assertFalse((d / rel).exists(), "%s is gt-src only" % rel)
        self.assertEqual((d / "local-notes.txt").read_text(), "mine\n")
        self.assertEqual((d / "golden-thread-plugin" / "scratch.txt").read_text(), "mine too\n")
        git_after = {k: v for k, v in snapshot(self.dest).items() if k.startswith(".git/")}
        self.assertEqual(git_after, git_before, ".git was touched by a run that did not commit")
        self.assertEqual(self.head(self.dest), head, "a run that was not clean committed")
        self.assertEqual(self.head(self.remote), head, "a run that was not clean pushed")
        # the report: outside the destination, four sections, a declared-denominator verdict
        text = self.report.read_text()
        for section in ("PRESENT", "MISSING", "WORKED", "FAILED"):
            self.assertIn("\n" + section + "\n", text)
        self.assertRegex(text, r"\nclean: (\d+)/(\d+)\n")
        data = json.loads(self.report.with_suffix(".json").read_text())
        self.assertFalse(data["clean"])
        self.assertLess(data["passed"], data["declared"])
        states = {c["check"]: c["state"] for c in data["checks"]}
        self.assertEqual(states.get("install.sh"), "worked")
        self.assertIn("could-not-run", states.values(), "no gt to validate must be could-not-run")
        applied = json.loads((self.state / "applied.json").read_text())
        self.assertEqual(applied["commit"], json.loads((self.src / "SOURCE.json").read_text())["commit"])

    def test_report_is_refused_inside_the_destination(self):
        p = self.copygt("--dest", self.dest, "--report", self.dest / "report.md")
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertFalse((self.dest / "report.md").exists())


@needs_dev
class ValidationReportTest(CopygtBase):
    """validate-install.py against a fixture install: no install.sh run, the record written by hand."""

    def setUp(self):
        super().setUp()
        root = _ti.build_repo_fixture(self, self.dest / "golden-thread-plugin")
        self.gt = latest_version_dir(root / "golden-thread")
        self.wiki = latest_version_dir(root / "golden-thread-wiki")
        plugins = self.home / ".claude" / "plugins"
        plugins.mkdir(parents=True)
        record = {"version": 2, "plugins": {}}
        enabled = {}
        for name, vdir in (("gt", self.gt), ("gt-wiki", self.wiki)):
            key = "%s@golden-thread-plugin" % name
            record["plugins"][key] = [{"version": vdir.name, "installPath": str(vdir)}]
            enabled[key] = True
        self.record, self.enabled = record, enabled
        self.write_record()
        self.report = self.tmp / "reports" / "v.md"

    def write_record(self):
        (self.home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(self.record))
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": self.enabled}))

    def fake_doctor(self, body):
        p = self.tmp / "fake_doctor.py"
        p.write_text(body)
        return p

    def passing_doctor(self, drop=()):
        rows = [r for r in self.declared() if r not in drop]
        return self.fake_doctor("import json\nprint(json.dumps({'release': 'x', 'counts': {}, 'rows': %r}))\n"
                                % [{"row": r, "state": "PASS", "summary": "ok", "fix": ""} for r in rows])

    def declared(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("vi_test", str(VALIDATE))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.declared_gate_rows(self.gt / "scripts" / "gt_doctor.py")

    def validate(self, doctor):
        p = self.py(VALIDATE, "--dest", self.dest, "--report", self.report, "--doctor", doctor)
        self.assertNotIn("Traceback", p.stderr)
        return p, json.loads(self.report.with_suffix(".json").read_text())

    def test_declared_rows_come_from_the_release_gate(self):
        rows = self.declared()
        for r in ("installed", "components", "wiring", "core-rules", "rule1-vault", "guard-denies",
                  "queue", "vault", "smoke-queue", "smoke-daily"):
            self.assertIn(r, rows)
        self.assertNotIn("post-install", rows, "the crash row is not a declared check")

    def test_a_complete_fixture_is_clean_against_the_declared_count(self):
        p, data = self.validate(self.passing_doctor())
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertTrue(data["clean"])
        # 2 plugins x 2 checks, plus one per declared gate row
        self.assertEqual(data["declared"], 4 + len(self.declared()))
        self.assertIn("clean: %d/%d" % (data["declared"], data["declared"]), p.stdout)
        self.assertEqual(data["missing"], [])

    def test_a_missing_declared_plugin_is_reported_missing_and_not_clean(self):
        del self.record["plugins"]["gt-wiki@golden-thread-plugin"]
        self.write_record()
        p, data = self.validate(self.passing_doctor())
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertFalse(data["clean"])
        self.assertTrue(any(m.startswith("gt-wiki %s" % self.wiki.name) for m in data["missing"]),
                        data["missing"])
        self.assertEqual(data["declared"], 4 + len(self.declared()),
                         "the denominator is what was DECLARED, not what was installed")
        self.assertIn("MISSING\n  gt-wiki", p.stdout)

    def test_a_crashing_check_is_could_not_run_never_pass(self):
        p, data = self.validate(self.fake_doctor("raise RuntimeError('boom')\n"))
        self.assertEqual(p.returncode, 1)
        gate = [c for c in data["checks"] if c["check"].startswith("gate ")]
        self.assertEqual(len(gate), len(self.declared()))
        self.assertTrue(all(c["state"] == "could-not-run" for c in gate), gate)
        self.assertIn("COULD NOT RUN", p.stdout)

    def test_a_declared_row_the_gate_did_not_produce_is_could_not_run(self):
        p, data = self.validate(self.passing_doctor(drop=("smoke-guard",)))
        self.assertEqual(p.returncode, 1)
        states = {c["check"]: c["state"] for c in data["checks"]}
        self.assertEqual(states["gate smoke-guard"], "could-not-run")

    def test_a_skill_missing_from_the_install_fails(self):
        inst = self.tmp / "installed-gt"
        shutil.copytree(self.gt, inst)
        victim = sorted((inst / "skills").glob("*/SKILL.md"))[0]
        victim.unlink()
        self.record["plugins"]["gt@golden-thread-plugin"][0]["installPath"] = str(inst)
        self.write_record()
        p, data = self.validate(self.passing_doctor())
        self.assertEqual(p.returncode, 1)
        self.assertIn("gt skill %s" % victim.parent.name, data["missing"])


@needs_dev
class EndToEndTest(CopygtBase):
    """copy -> real install.sh -> validate -> commit, never push, in a sandbox HOME."""

    def setUp(self):
        super().setUp()
        root = _ti.build_repo_fixture(self, self.src / "golden-thread-plugin")
        (self.src / "README.md").write_text("# golden thread\n")
        self.add_tools()
        gt = latest_version_dir(root / "golden-thread")
        wiki = latest_version_dir(root / "golden-thread-wiki")
        self.commit = "ab12" * 10
        publish_sums(self.src, commit=self.commit, gt=gt.name, gt_wiki=wiki.name)
        self.make_dest({"README.md": "# old\n", "golden-thread-plugin/BUILD-NOTE.md": "old note\n",
                        "Golden Thread.code-workspace": "{}\n"},
                       untracked={"local-notes.txt": "mine\n"})
        self.report = self.tmp / "reports" / "e2e.md"

    def test_a_clean_run_commits_and_never_pushes(self):
        before = self.head(self.remote)
        self.config(vault_path=str(self.make_vault()))
        p = self.copygt("--dest", self.dest, "--report", self.report)
        self.assertEqual(p.returncode, 0, p.stdout[-6000:] + p.stderr[-2000:]
                         + (self.report.read_text() if self.report.exists() else ""))
        text = self.report.read_text()
        n = text.rsplit("clean: ", 1)[1].split()[0]
        a, b = n.split("/")
        self.assertEqual(a, b, text)
        head = self.head(self.dest)
        self.assertNotEqual(head, before, "the clean run did not commit")
        self.assertEqual(self.head(self.remote), before, "copygt.sh pushed; it must never push")
        self.assertIn("never pushes", p.stdout)
        msg = self.run_cmd(["git", "-C", self.dest, "log", "-1", "--format=%B"]).stdout
        self.assertIn(self.commit, msg)
        self.assertIn("gt ", msg)
        tracked = self.tracked()
        for rel in GT_SRC_ONLY:
            self.assertNotIn(rel, tracked)
        self.assertNotIn("golden-thread-plugin/BUILD-NOTE.md", tracked)
        self.assertNotIn("Golden Thread.code-workspace", tracked)
        self.assertNotIn("local-notes.txt", tracked, "an untracked local file was committed")
        self.assertTrue((self.dest / "local-notes.txt").exists())
        self.assertIn("golden-thread-plugin/install.sh", tracked)
        # a second run has nothing to commit, and says so
        p2 = self.copygt("--dest", self.dest, "--report", self.tmp / "reports" / "again.md")
        self.assertEqual(p2.returncode, 0, p2.stdout[-3000:])
        self.assertIn("nothing to commit", p2.stdout)
        self.assertEqual(self.head(self.dest), head)

    def test_a_run_that_is_not_clean_does_not_commit(self):
        # No vault: the gate's vault rows cannot run, and "could not run" is never a pass.
        head = self.head(self.dest)
        p = self.copygt("--dest", self.dest, "--report", self.report)
        self.assertEqual(p.returncode, 4, p.stdout[-4000:])
        self.assertEqual(self.head(self.dest), head)
        self.assertEqual(self.head(self.remote), head, "the remote moved on a run that was not clean")
        data = json.loads(self.report.with_suffix(".json").read_text())
        self.assertFalse(data["clean"])
        self.assertIn("could-not-run", {c["state"] for c in data["checks"]})


@needs_dev
class ScrubGate(Sandbox):
    """Both tools ship in gt-src, so the release scrub (sync-gt-src.sh runs it on the staged set,
    tools included) must pass on them with the REAL terms -- no machine or employer strings."""

    def test_copygt_and_the_suite_pass_the_scrub_gate(self):
        terms = os.environ.get("GT_SCRUB_TERMS")
        if not terms:
            try:
                cfg = json.loads((Path(os.path.expanduser("~")) / ".claude" / "vault-config.json").read_text())
                terms = str(Path(cfg["vault_path"]) / "Projects" / "golden-thread" / "scrub-terms.txt")
            except (OSError, ValueError, KeyError):
                terms = None
        if not terms or not Path(terms).is_file():
            self.skipTest("the private scrub terms are not reachable on this machine")
        d = self.tmp / "tools"
        d.mkdir()
        for t in ("copygt.sh", "validate-install.py"):
            shutil.copy2(REPO / "dev" / t, d / t)
        p = self.py(REPO / "dev" / "scrub_check.py", d, env={"GT_SCRUB_TERMS": terms})
        self.assertOk(p, "copygt.sh / validate-install.py carry a scrubbed string")
        self.assertIn("2 files", p.stdout)


if __name__ == "__main__":
    import unittest
    unittest.main()


@needs_dev
class NeverPushes(unittest.TestCase):
    def test_copygt_has_no_push_command(self):
        text = (REPO / "dev" / "copygt.sh").read_text()
        code = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
        self.assertFalse([l for l in code if "git" in l and " push" in l],
                         "copygt.sh must never run git push; pushing is outside it")
