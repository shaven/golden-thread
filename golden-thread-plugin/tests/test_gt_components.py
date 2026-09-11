"""gt_components.py: installed-vs-source drift, and hook wiring in settings.json.

Every fixture is a FAKE release built in the sandbox -- a version dir under a path
containing a space (the real plugin lives under "Golden Thread"), manifested by the
real tool, "installed" into the sandbox HOME, and wired from the tool's own
`hook-registrations` output. The real release's MANIFEST.json is never rewritten.

States covered: clean, stale, ahead, missing, extra, no-manifest, unwired, badpath;
policies off/report/confirm/auto; --hook JSON vs plain text.
"""
import hashlib
import json
import os
import shlex
import shutil
import unittest

from _harness import Sandbox, SCRIPTS, load_module

TOOL = SCRIPTS / "gt_components.py"

HOOK_FILES = ("inject_core_rules.sh", "validate_response.sh", "guard_session_claims.sh",
              "alpha.sh")
HOOKDIR_SCRIPTS = ("gt_components.py", "gt_workers.py", "gt_version_check.py",
                   "gt_push_check.py", "gt_report_card.py")


class ComponentsBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "Golden Thread" / "plugin"          # {plugin_root}
        self.vdir = self.root / "golden-thread" / "1.2.3"           # {src}
        (self.vdir / "hooks").mkdir(parents=True)
        (self.vdir / "scripts" / "__pycache__").mkdir(parents=True)
        (self.vdir / "templates").mkdir(parents=True)
        for n in HOOK_FILES:
            (self.vdir / "hooks" / n).write_text("#!/bin/sh\n# %s v1\n" % n)
        for n in HOOKDIR_SCRIPTS:
            (self.vdir / "scripts" / n).write_text("# %s v1\n" % n)
        (self.vdir / "scripts" / "not_installed.py").write_text("# stays in source\n")
        (self.vdir / "scripts" / "__pycache__" / "x.cpython-39.pyc").write_bytes(b"\0")
        (self.vdir / "scripts" / ".DS_Store").write_bytes(b"\0")
        (self.vdir / "templates" / "t.md").write_text("template\n")
        self.installed = self.home / ".claude" / "golden-thread" / "hooks"
        self.settings = self.home / ".claude" / "settings.json"

    # -- fixture steps --------------------------------------------------------------
    def manifest(self, vdir=None):
        p = self.py(TOOL, "manifest", vdir or self.vdir)
        self.assertOk(p, "manifest")
        return json.loads(((vdir or self.vdir) / "MANIFEST.json").read_text())

    def install(self):
        self.installed.mkdir(parents=True, exist_ok=True)
        for n in HOOK_FILES:
            shutil.copy2(self.vdir / "hooks" / n, self.installed / n)
        for n in HOOKDIR_SCRIPTS:
            shutil.copy2(self.vdir / "scripts" / n, self.installed / n)

    def registrations(self, vdir=None):
        p = self.py(TOOL, "hook-registrations", vdir or self.vdir)
        self.assertOk(p, "hook-registrations")
        return json.loads(p.stdout)

    def wire(self, regs=None, drop=()):
        regs = self.registrations() if regs is None else regs
        events = {}
        for r in regs:
            if r["script"] in drop:
                continue
            events.setdefault(r["event"], []).append(
                {"hooks": [{"type": "command", "command": r["command"]}]})
        self.settings.write_text(json.dumps({"hooks": events}, indent=1))

    def full_setup(self):
        self.manifest()
        self.install()
        self.wire()

    def check(self, *extra, **kw):
        p = self.py(TOOL, "check", self.vdir, *extra, **kw)
        self.assertOk(p, "check must never fail a session start")
        return p.stdout

    def set_mtime(self, path, delta):
        src_m = os.path.getmtime(self.vdir / "hooks" / "alpha.sh")
        os.utime(path, (src_m + delta, src_m + delta))


class Manifest(ComponentsBase):
    def test_manifest_hashes_shipped_files_only(self):
        man = self.manifest()
        self.assertEqual(man["version"], "1.2.3")
        files = man["files"]
        self.assertIn("hooks/alpha.sh", files)
        self.assertIn("scripts/not_installed.py", files)
        self.assertIn("templates/t.md", files)
        self.assertFalse([k for k in files if "__pycache__" in k or k.endswith(".pyc")
                          or os.path.basename(k).startswith(".")], files)
        body = (self.vdir / "hooks" / "alpha.sh").read_bytes()
        self.assertEqual(files["hooks/alpha.sh"],
                         {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)})

    def test_manifest_declares_the_hook_registrations(self):
        man = self.manifest()
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)      # the module expands ~ at import
        try:
            m = load_module(TOOL, "gt_components_under_test")
        finally:
            os.environ["HOME"] = old_home
        want = [{"event": r["event"], "script": r["script"], "args": list(r["args"]),
                 "owner": r["owner"]} for r in m.HOOK_REGISTRATIONS]
        self.assertEqual(man["hooks"], want)
        self.assertEqual(len(want), 9)
        owners = {r["owner"] for r in want}
        self.assertEqual(owners, {"install.sh", "vault_init.py install-core-rules"})
        # templates, not resolved paths: nothing machine-specific in the manifest
        self.assertNotIn(str(self.tmp), json.dumps(man["hooks"]))

    def test_missing_version_dir_arg(self):
        for cmd in ("manifest", "check", "apply", "wiring", "hook-registrations"):
            p = self.py(TOOL, cmd, "--hook")
            self.assertEqual(p.returncode, 2, cmd)
            self.assertIn("need a version dir", p.stdout, cmd)

    def test_hookdir_scripts(self):
        p = self.py(TOOL, "hookdir-scripts")
        self.assertOk(p)
        names = p.stdout.split()
        for n in ("gt_settings.py", "gt_components.py", "gt_workers.py",
                  "gt_version_check.py", "gt_push_check.py", "gt_report_card.py"):
            self.assertIn(n, names)


class HookRegistrations(ComponentsBase):
    def test_commands_resolve_and_survive_a_space_in_the_path(self):
        regs = self.registrations()
        self.assertEqual(len(regs), 9)
        by = {(r["event"], r["script"]): r for r in regs}
        argv = shlex.split(by[("SessionStart", "gt_components.py")]["command"])
        self.assertEqual(argv, ["python3", str(self.installed / "gt_components.py"),
                                "check", str(self.vdir), "--hook"])
        argv = shlex.split(by[("SessionStart", "gt_version_check.py")]["command"])
        self.assertEqual(argv[2:], ["check", str(self.root), "--hook"])
        argv = shlex.split(by[("PreCompact", "gt_report_card.py")]["command"])
        self.assertEqual(argv, ["python3", str(self.installed / "gt_report_card.py")])
        # shell hooks execute directly, not through python3
        argv = shlex.split(by[("Stop", "validate_response.sh")]["command"])
        self.assertEqual(argv, [str(self.installed / "validate_response.sh")])

    def test_explicit_plugin_root(self):
        other = self.tmp / "elsewhere"
        p = self.py(TOOL, "hook-registrations", self.vdir, other)
        self.assertOk(p)
        vc = [r for r in json.loads(p.stdout) if r["script"] == "gt_version_check.py"][0]
        self.assertIn(str(other), shlex.split(vc["command"]))


class Check(ComponentsBase):
    def test_clean_says_so_out_loud(self):
        self.full_setup()
        out = self.check()
        self.assertIn("components: clean", out)
        self.assertIn("installed matches 1.2.3", out)
        self.assertIn("all 9 hooks wired", out)

    def test_hook_flag_emits_json_for_user_and_model(self):
        self.full_setup()
        out = self.check("--hook")
        d = json.loads(out)
        self.assertIn("components: clean", d["systemMessage"])
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(d["hookSpecificOutput"]["additionalContext"], d["systemMessage"])
        # GT_HOOK=1 is the documented alternative to the flag
        d = json.loads(self.check(env={"GT_HOOK": "1"}))
        self.assertIn("clean", d["systemMessage"])
        # and without either it is plain text, not JSON
        self.assertTrue(self.check().startswith("GOLDEN THREAD components"))

    def test_stale_installed_older(self):
        self.full_setup()
        dst = self.installed / "alpha.sh"
        dst.write_text("#!/bin/sh\n# old build\n")
        self.set_mtime(dst, -3600)
        out = self.check()
        self.assertIn("drifted from 1.2.3", out)
        self.assertRegex(out, r"stale\s+hooks/alpha\.sh")
        self.assertNotIn("apply with", out)          # report policy: no command offered

    def test_ahead_installed_newer_is_never_called_stale(self):
        self.full_setup()
        dst = self.installed / "alpha.sh"
        dst.write_text("#!/bin/sh\n# newer local fix\n")
        self.set_mtime(dst, +3600)
        out = self.check()
        self.assertRegex(out, r"ahead\s+hooks/alpha\.sh")
        self.assertIn("installed is NEWER", out)
        self.assertNotRegex(out, r"stale\s+hooks/alpha")
        self.assertIn("never auto-applied", out)

    def test_hookdir_script_drift_is_mapped(self):
        self.full_setup()
        dst = self.installed / "gt_workers.py"
        dst.write_text("# stale copy\n")
        self.set_mtime(dst, -3600)
        self.assertRegex(self.check(), r"stale\s+scripts/gt_workers\.py")

    def test_missing_and_extra(self):
        self.full_setup()
        (self.installed / "alpha.sh").unlink()
        (self.installed / "zeta.sh").write_text("# only here\n")
        (self.installed / "junk.pyc").write_bytes(b"\0")
        out = self.check()
        self.assertRegex(out, r"missing\s+hooks/alpha\.sh")
        self.assertRegex(out, r"extra\s+zeta\.sh")
        self.assertNotIn("junk.pyc", out)
        # files shipped under scripts/ but installed into hooks/ are not "extra"
        self.assertNotRegex(out, r"extra\s+gt_components\.py")

    def test_no_manifest(self):
        self.install()
        self.wire()
        out = self.check()
        self.assertIn("no-manifest", out)

    def test_policy_off_is_silent(self):
        self.config(vault_path=str(self.tmp), component_updates="off")
        self.manifest()                               # nothing installed, nothing wired
        self.assertEqual(self.check(), "")
        self.assertEqual(self.check("--hook"), "")

    def test_policy_confirm_prints_apply_command(self):
        self.config(vault_path=str(self.tmp), component_updates="confirm")
        self.full_setup()
        (self.installed / "alpha.sh").unlink()
        out = self.check()
        self.assertIn("apply with:", out)
        self.assertFalse((self.installed / "alpha.sh").exists(), "confirm must not apply")

    def test_policy_auto_applies_only_stale_and_missing(self):
        self.config(vault_path=str(self.tmp), component_updates="auto")
        self.full_setup()
        (self.installed / "inject_core_rules.sh").unlink()            # missing
        stale = self.installed / "validate_response.sh"
        stale.write_text("# old\n")
        self.set_mtime(stale, -3600)
        ahead = self.installed / "alpha.sh"
        ahead.write_text("# newer local work\n")
        self.set_mtime(ahead, +3600)
        extra = self.installed / "zeta.sh"
        extra.write_text("# only here\n")
        out = self.check()
        self.assertIn("applied automatically", out)
        src = self.vdir / "hooks"
        self.assertEqual((self.installed / "inject_core_rules.sh").read_text(),
                         (src / "inject_core_rules.sh").read_text())
        self.assertEqual(stale.read_text(), (src / "validate_response.sh").read_text())
        self.assertEqual(ahead.read_text(), "# newer local work\n", "auto overwrote `ahead`")
        self.assertTrue(extra.exists(), "auto deleted an `extra`")
        # a second check sees only what auto must never touch
        out = self.check()
        self.assertNotIn("stale", out)
        self.assertNotIn("missing", out)
        self.assertIn("ahead", out)

    def test_apply_subcommand(self):
        self.full_setup()
        (self.installed / "alpha.sh").unlink()
        p = self.py(TOOL, "apply", self.vdir)
        self.assertOk(p)
        self.assertIn("hooks/alpha.sh", p.stdout)
        self.assertTrue(os.access(self.installed / "alpha.sh", os.X_OK))
        self.assertIn("clean", self.check())

    def test_unwired_is_reported_even_when_files_match(self):
        # The 2026-09-10 shape: every file present and identical, nothing connected.
        self.manifest()
        self.install()
        self.settings.write_text("{}")
        out = self.check()
        self.assertNotIn("clean", out)
        for s in ("gt_components.py", "gt_workers.py", "inject_core_rules.sh"):
            self.assertRegex(out, r"unwired\s+%s" % s.replace(".", r"\."))
        self.assertIn("NEVER RUNS", out)
        self.assertIn("install.sh", out)
        self.assertIn("vault_init.py install-core-rules", out)

    def test_badpath_after_version_dir_removed(self):
        self.full_setup()
        old = self.root / "golden-thread" / "1.2.2"
        old.mkdir()
        self.wire(self.registrations(old))          # wired to an older release...
        shutil.rmtree(old)                          # ...which a bump then deleted
        out = self.check()
        self.assertRegex(out, r"badpath\s+gt_components\.py")
        self.assertIn(str(old), out)


class Wiring(ComponentsBase):
    def wiring(self, *args):
        return self.py(TOOL, "wiring", *args)

    def test_no_settings_file_means_everything_unwired(self):
        self.manifest()
        p = self.wiring(self.vdir)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.count("unwired"), 9, p.stdout)

    def test_empty_settings_file(self):
        self.manifest()
        self.settings.write_text("")
        p = self.wiring(self.vdir)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.count("unwired"), 9)

    def test_all_wired(self):
        self.full_setup()
        p = self.wiring(self.vdir)
        self.assertOk(p, "wiring")
        self.assertIn("all 9 declared hooks are wired", p.stdout)

    def test_owner_filter_after_dir(self):
        # install.sh's form: before a vault exists the enforcement hooks are
        # legitimately unwired, and must not count against the installer.
        self.manifest()
        self.install()
        self.wire(drop=("inject_core_rules.sh", "validate_response.sh",
                        "guard_session_claims.sh"))
        p = self.wiring(self.vdir, "--owner", "install.sh")
        self.assertOk(p, "install.sh-owned hooks are all wired")
        self.assertIn("all 6 declared hooks are wired (install.sh)", p.stdout)
        p = self.wiring(self.vdir)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.count("unwired"), 3)

    def test_old_manifest_is_judged_by_what_it_declared(self):
        # A release whose manifest declared only one hook must not be failed for
        # hooks it never shipped.
        self.manifest()
        man_p = self.vdir / "MANIFEST.json"
        man = json.loads(man_p.read_text())
        man["hooks"] = [h for h in man["hooks"] if h["script"] == "gt_workers.py"]
        man_p.write_text(json.dumps(man))
        self.install()
        self.wire([r for r in self.registrations() if r["script"] == "gt_workers.py"])
        p = self.wiring(self.vdir)
        self.assertOk(p, "manifest-declared hooks are wired")

    def test_manifest_without_hooks_falls_back_to_registry(self):
        self.manifest()
        man_p = self.vdir / "MANIFEST.json"
        man = json.loads(man_p.read_text())
        del man["hooks"]                           # pre-0.9.13 manifest
        man_p.write_text(json.dumps(man))
        self.settings.write_text("{}")
        p = self.wiring(self.vdir)
        self.assertEqual(p.stdout.count("unwired"), 9)

    def test_owner_flag_before_dir(self):
        # main() says flags are skipped when locating the version dir "or
        # `wiring --owner X <dir>` would take '--owner' as the directory". It skips
        # the flag but not its VALUE, so X is taken as the directory, the manifest
        # is never read, and the release is judged against HOOK_REGISTRATIONS
        # instead of what it declared.
        self.manifest()
        man_p = self.vdir / "MANIFEST.json"
        man = json.loads(man_p.read_text())
        man["hooks"] = [h for h in man["hooks"] if h["script"] == "gt_workers.py"]
        man_p.write_text(json.dumps(man))
        self.install()
        self.wire([r for r in self.registrations() if r["script"] == "gt_workers.py"])
        after = self.wiring(self.vdir, "--owner", "install.sh")
        before = self.wiring("--owner", "install.sh", self.vdir)
        self.assertOk(after)
        self.assertEqual(before.returncode, after.returncode,
                         "`wiring --owner X <dir>` took X as the version dir:\n"
                         + before.stdout)

    def test_script_named_in_wrong_event_is_unwired(self):
        self.manifest()
        self.install()
        regs = self.registrations()
        for r in regs:
            if r["script"] == "gt_workers.py":
                r["event"] = "Stop"
        self.wire(regs)
        p = self.wiring(self.vdir)
        self.assertEqual(p.returncode, 1)
        self.assertRegex(p.stdout, r"unwired\s+gt_workers\.py\s+SessionStart")


if __name__ == "__main__":
    unittest.main()
