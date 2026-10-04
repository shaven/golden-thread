"""Private scratch folders for gt's stage agents (0.20.0).

Owner, 2026-10-04: pipeline stage agents shared the run's folder under the vault's
spool/pipeline/<run>/ -- per run, not per agent, and inside the vault, the wrong place for
extracted raw material and a place gt sandbox mode denies every write to. Pinned here:

  * each unit of each stage gets its own folder OUTSIDE the vault, every level 0700 (POSIX);
  * its path is in the rendered prompt, as the only place for intermediate files;
  * workflow-args creates one per unit and names it in the item and the prompt;
  * gt sandbox mode's plan allows writes there (and still denies the vault);
  * finishing a run removes its scratch; `cleanup` removes an abandoned one; `check` reports a
    finished or vanished run's scratch as a leak;
  * nothing is written under the vault spool except gt's own packets and stage files.
"""
import json
import os
import stat
from pathlib import Path

from _harness import IS_WINDOWS, SCRIPTS, load_module, skip_on_windows
from test_gt_ingest_pipeline import RESEARCH, SPEC, PipelineBase, extract_result

SCRATCH = SCRIPTS / "gt_scratch.py"
SANDBOX = SCRIPTS / "gt_sandbox.py"


class ScratchBase(PipelineBase):
    def setUp(self):
        super().setUp()
        # POSIX: ~/.gt-scratch under the sandbox HOME. Windows: the harness points
        # GT_SCRATCH_ROOT into the sandbox, because the default is %LOCALAPPDATA%.
        self.root = (self.home / "AppData" / "Local" / "gt-scratch") if IS_WINDOWS \
            else self.home / ".gt-scratch"

    def scratch(self, *args, expect=0):
        p = self.py(SCRATCH, *args)
        self.assertEqual(expect, p.returncode, p.stdout + p.stderr)
        return p


class AFolderPerUnit(ScratchBase):
    def test_each_unit_gets_its_own_folder_outside_the_vault(self):
        a = json.loads(self.scratch("path", "r1", "--stage", "extract", "--unit", "api",
                                    "--json").stdout)["path"]
        b = json.loads(self.scratch("path", "r1", "--stage", "extract", "--unit", "web/x",
                                    "--json").stdout)["path"]
        self.assertNotEqual(a, b)
        for d in (a, b):
            self.assertTrue(os.path.isdir(d))
            self.assertFalse(os.path.realpath(d).startswith(os.path.realpath(str(self.vault))))
            self.assertTrue(os.path.realpath(d).startswith(os.path.realpath(str(self.root))))
        self.assertEqual(os.path.basename(b), "extract-web__x")

    @skip_on_windows("POSIX mode bits; Windows uses an owner-only ACL (icacls)")
    def test_every_level_is_owner_only(self):
        d = json.loads(self.scratch("path", "r1", "--stage", "verify", "--unit", "c01",
                                    "--json").stdout)["path"]
        for p in (self.root, os.path.dirname(d), d):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o700, p)

    @skip_on_windows("symlinks need privileges on Windows")
    def test_a_symlinked_root_is_refused_not_followed(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        os.symlink(str(elsewhere), str(self.root))
        p = self.scratch("path", "r1", "--stage", "extract", "--unit", "api", expect=1)
        self.assertIn("not a real directory", p.stderr)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_windows_uses_local_app_data_with_an_owner_only_acl(self):
        from unittest import mock
        m = load_module(SCRATCH, "gt_scratch_win")
        env = {"LOCALAPPDATA": os.path.join("C:", "Users", "u", "AppData", "Local"),
               "USERNAME": "u", "USERDOMAIN": "BOX"}
        with mock.patch.object(m, "IS_WINDOWS", True), mock.patch.dict(os.environ, env):
            os.environ.pop("GT_SCRATCH_ROOT", None)
            self.assertEqual(m.root(), os.path.join(env["LOCALAPPDATA"], "gt-scratch"))
            calls = []

            def run(args, **kw):
                calls.append(args)
                out = '"box\\u","S-1-5-21-1-2-3-1001"\r\n' if args[0] == "whoami" else ""
                return mock.Mock(returncode=0, stdout=out, stderr="")
            with mock.patch.object(m.subprocess, "run", run):
                m._owner_only_windows("D")
        # By SID: over ssh USERDOMAIN can read WORKGROUP, which icacls cannot map to an account.
        self.assertEqual(calls[-1], ["icacls", "D", "/inheritance:r", "/grant:r",
                                     "*S-1-5-21-1-2-3-1001:(OI)(CI)F"])

    def test_names_cannot_climb_out(self):
        self.scratch("path", "../x", "--stage", "extract", "--unit", "api", expect=1)
        d = json.loads(self.scratch("path", "r1", "--stage", "extract", "--unit", "../../etc",
                                    "--json").stdout)["path"]
        self.assertTrue(os.path.realpath(d).startswith(os.path.realpath(str(self.root))))


class ThePathIsInThePrompt(ScratchBase):
    def test_render_names_the_folder_as_the_only_place_for_intermediate_files(self):
        r = self.py(SPEC, "render", "verify", "--input", "claim=x", "--input", "rules=y",
                    "--input", "artifact=z", "--scratch-run", "p1", "--scratch-unit", "c01")
        self.assertOk(r)
        d = self.root / "p1" / "verify-c01"
        self.assertTrue(d.is_dir())
        self.assertIn("## Scratch folder", r.stdout)
        self.assertIn(str(d), r.stdout)
        self.assertIn("ONLY place", r.stdout)

    def test_without_a_run_there_is_no_scratch_section(self):
        r = self.py(SPEC, "render", "verify", "--input", "claim=x", "--input", "rules=y",
                    "--input", "artifact=z")
        self.assertOk(r)
        self.assertNotIn("## Scratch folder", r.stdout)
        self.assertFalse(self.root.exists())

    def test_every_stage_spec_names_the_scratch_folder(self):
        from test_gt_ingest_pipeline import SPECS, STAGES
        for stage in STAGES:
            data = json.loads((SPECS / "stages" / ("%s.json" % stage)).read_text())
            delta = data["prompt_delta"]
            text = " ".join(delta if isinstance(delta, list) else [delta])
            self.assertIn("scratch folder", text, stage)

    def test_workflow_args_gives_each_unit_its_folder_in_the_prompt(self):
        src = self.tree("wombat", {"api/main.py": "print(1)\n", "web/a.ts": "let a = 1\n"})
        self.survey(src)
        p = self.pipe("workflow-args", "r1", "--stage", "extract", "--json", expect=0)
        a = json.loads(p.stdout)
        for it in a["items"]:
            d = self.root / "r1" / ("extract-%s" % it["unit"])
            self.assertEqual(os.path.realpath(it["scratch_dir"]), os.path.realpath(str(d)))
            self.assertTrue(d.is_dir())
            self.assertIn(str(d), Path(it["prompt_file"]).read_text(encoding="utf-8"))

    def test_a_skill_rendered_prompt_without_a_folder_gets_one(self):
        cands = self.result_file("cands", [{"claim": "rsync -c compares checksums",
                                            "origin": RESEARCH, "evidence": "man"}])
        self.pipe("promote-scan", "--candidates-file", cands, "--run", "p1", expect=0)
        r = self.py(SPEC, "render", "verify", "--input", "claim=x", "--input", "rules=y",
                    "--input", "artifact=z")
        good = self.tmp / "verify.md"
        good.write_text(r.stdout, encoding="utf-8")
        a = json.loads(self.pipe("workflow-args", "p1", "--stage", "verify", "--json",
                                 "--prompt", "c01=%s" % good, expect=0).stdout)
        (it,) = a["items"]
        d = self.root / "p1" / "verify-c01"
        self.assertIn(str(d), Path(it["prompt_file"]).read_text(encoding="utf-8"))


class SandboxAllowsIt(ScratchBase):
    @skip_on_windows("native Windows has no OS sandbox: gt writes only permission rules there")
    def test_the_sandbox_plan_allows_writes_to_the_scratch_root_and_still_denies_the_vault(self):
        m = load_module(SANDBOX, "gt_sandbox_scratch")
        with_env = dict(os.environ)
        try:
            os.environ.pop("GT_SCRATCH_ROOT", None)
            p = m.plan(h=str(self.home), vault=str(self.vault), vault_reads="deny",
                       mode="macos")
        finally:
            os.environ.clear()
            os.environ.update(with_env)
        allow = p["lists"]["sandbox.filesystem.allowWrite"]
        self.assertIn(m.sandbox_path(str(self.root)), allow)
        self.assertIn(m.sandbox_path(str(self.vault)), p["lists"]["sandbox.filesystem.denyWrite"])
        self.assertFalse(any(a.startswith(str(self.vault)) for a in allow))


class Cleanup(ScratchBase):
    def finish_ingest(self):
        src = self.tree("wombat", {"api/main.py": "print(1)\n"})
        self.survey(src)
        self.pipe("workflow-args", "r1", "--stage", "extract", "--json", expect=0)
        self.assertTrue((self.root / "r1").is_dir())
        self.packet("r1", "extract", "api", extract_result(["unit api is documented"]))
        self.pipe("fan-in", "r1", "--stage", "extract", expect=0)
        self.pipe("reconcile", "r1", expect=0)

    def test_finishing_the_run_removes_its_scratch(self):
        self.finish_ingest()
        p = self.pipe("draft", "r1", "--session", "s1", expect=0)
        self.assertFalse((self.root / "r1").exists(), p.stdout)

    def test_cleanup_removes_an_abandoned_run_and_check_reports_leaks(self):
        self.finish_ingest()
        p = self.py(SCRATCH, "check", "--vault", self.vault)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)      # in progress: not a leak
        (self.spool / "r1" / "run.json").unlink()                   # the run is gone
        p = self.py(SCRATCH, "check", "--vault", self.vault)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("LEAK r1", p.stdout)
        self.assertTrue((self.root / "r1").is_dir(), "check must never remove anything")
        p = self.pipe("cleanup", "r1", expect=0)
        self.assertFalse((self.root / "r1").exists())
        p = self.py(SCRATCH, "check", "--vault", self.vault)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_status_names_the_scratch(self):
        self.finish_ingest()
        st = json.loads(self.pipe("status", "r1", "--json", expect=0).stdout)
        self.assertTrue(st["scratch"]["present"])
        self.assertEqual(os.path.realpath(st["scratch"]["path"]),
                         os.path.realpath(str(self.root / "r1")))

    def test_nothing_but_packets_and_stage_files_lands_in_the_vault_spool(self):
        self.finish_ingest()
        scratch = self.root / "r1" / "extract-api"
        (scratch / "notes.txt").write_text("an agent's intermediate file\n")
        names = sorted(str(p.relative_to(self.spool / "r1")).replace(os.sep, "/")
                       for p in (self.spool / "r1").rglob("*") if p.is_file())
        allowed = {"run.json", "survey.json", "fanin-extract.json", "reconciled.json",
                   "workflow-extract.json", "extract/api.json", "prompts/extract/api.md"}
        self.assertEqual(set(names) - allowed, set(), names)
        self.assertFalse(any("notes.txt" in n for n in names))
        self.assertEqual(list(self.vault.rglob("notes.txt")), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
