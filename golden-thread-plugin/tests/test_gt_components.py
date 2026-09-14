"""gt_components.py: installed-vs-source drift, and hook wiring in settings.json.

Every fixture is a FAKE release built in the sandbox -- a version dir under a path
containing a space (the real plugin lives under "Golden Thread"), manifested by the
real tool, "installed" into the sandbox HOME, and wired from the tool's own
`hook-registrations` output. The real release's MANIFEST.json is never rewritten.

States covered: clean, stale, differs, missing, extra, no-manifest, unwired, badpath;
policies off/report/confirm/auto; --hook JSON vs plain text.
"""
import hashlib
import json
import os
import shlex
import shutil
import unittest

from _harness import Sandbox, SCRIPTS, load_module, ENFORCEMENT_HOOKS, GT

TOOL = SCRIPTS / "gt_components.py"

HOOK_FILES = ENFORCEMENT_HOOKS + ("alpha.sh",)
# Counts come from the declaration, never from a literal: 0.12.0 added an eleventh
# hook and every literal 10 in this file failed a test that was not about it.
_REGS = load_module(SCRIPTS / "gt_components.py", "gt_components_counts").HOOK_REGISTRATIONS
N_HOOKS = len(_REGS)
N_INSTALL_SH = sum(1 for r in _REGS if r["owner"] == "install.sh")
N_ENFORCEMENT = N_HOOKS - N_INSTALL_SH
HOOKDIR_SCRIPTS = ("gt_components.py", "gt_workers.py", "gt_version_check.py",
                   "gt_push_check.py", "gt_watch.py", "gt_report_card.py")


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
        self.assertEqual(len(want), N_HOOKS)
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
                  "gt_version_check.py", "gt_push_check.py", "gt_watch.py", "gt_report_card.py"):
            self.assertIn(n, names)


class HookRegistrations(ComponentsBase):
    def test_commands_resolve_and_survive_a_space_in_the_path(self):
        regs = self.registrations()
        self.assertEqual(len(regs), N_HOOKS)
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
        self.assertIn(f"all {N_HOOKS} hooks wired", out)

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

    def test_an_unexplained_difference_is_never_called_stale(self):
        """Installed-newer-by-mtime is reported as `differs`, not as a direction.

        This test used to assert `ahead` and the words "installed is NEWER". That claim
        was unsupportable: install.sh copies with plain `cp`, so EVERY installed file
        carries the install-time mtime and is always newer than its source. Any content
        mismatch therefore read as "installed is newer, update the plugin from it" -- and
        on another machine, 2026-09-12, that message appeared at every session start for a
        gt_paths.py whose installed copy was in fact OLDER, with no way for the user to
        act on it.

        The safety property is unchanged and still asserted here: never `stale`, never
        auto-applied. Only the claim about direction is gone, replaced by the resolution
        steps.
        """
        self.full_setup()
        dst = self.installed / "alpha.sh"
        dst.write_text("#!/bin/sh\n# newer local fix\n")
        self.set_mtime(dst, +3600)
        out = self.check()
        self.assertRegex(out, r"differs\s+hooks/alpha\.sh")
        self.assertIn("could NOT be established", out)
        self.assertNotIn("installed is NEWER", out,
                         "do not assert a direction mtime cannot establish")
        self.assertNotRegex(out, r"stale\s+hooks/alpha")
        self.assertIn("never auto-applied", out)
        # A message with no way out is a message people scroll past.
        self.assertIn("resolve hooks/alpha.sh", out)
        self.assertIn("re-run install.sh", out)

    def test_a_copy_from_another_release_is_stale_not_unexplained(self):
        """Identity beats mtime: a leftover from a release we can see IS stale.

        The reported case, generalised -- an installed file left behind by an earlier
        install, carrying a fresh mtime from `cp`. If its hash matches the same file in
        another release on disk, its origin is known and it is safe to update.
        """
        self.full_setup()
        dst = self.installed / "alpha.sh"
        old_content = "#!/bin/sh\n# shipped by an earlier release\n"
        dst.write_text(old_content)
        self.set_mtime(dst, +3600)                       # newer, as cp always leaves it
        # A sibling release whose manifest records exactly that content.
        sibling = self.vdir.parent / "1.2.2"
        (sibling / "hooks").mkdir(parents=True)
        (sibling / "hooks" / "alpha.sh").write_text(old_content)
        import hashlib
        sha = hashlib.sha256(old_content.encode()).hexdigest()
        (sibling / "MANIFEST.json").write_text(json.dumps(
            {"version": "1.2.2", "files": {"hooks/alpha.sh": {"sha256": sha}}}), encoding="utf-8")
        out = self.check()
        self.assertRegex(out, r"stale\s+hooks/alpha\.sh",
                         "a copy identified in another release is stale, not unexplained")
        self.assertNotIn("differs", out)

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
        self.assertEqual(ahead.read_text(), "# newer local work\n",
                         "auto overwrote a file it could not prove was safe to touch")
        self.assertTrue(extra.exists(), "auto deleted an `extra`")
        # a second check sees only what auto must never touch
        out = self.check()
        self.assertNotIn("stale", out)
        self.assertNotIn("missing", out)
        self.assertIn("differs", out)

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


class DuplicateDestinations(Sandbox):
    """Two shipped files must never install to the same path.

    gt_paths.py shipped from BOTH hooks/ (a 0.12.2-era copy) and scripts/ (current)
    through every release from 0.9.13 to 0.12.7. install.sh copies hooks/* first and then
    overwrites with scripts/gt_paths.py, so the correct file won -- by ordering, not by
    design -- while the MANIFEST kept a hash for each path. The drift check therefore had
    to disagree with one of them on every machine, forever. Worse, once 0.12.7 resolved
    direction by identity it called that row `stale`, the auto-appliable state, so
    component_updates=auto would have copied the 0.12.2 file over the correct one and
    silently disabled the gated_by/budget_from keys the parallel Core rule reads.
    """
    def setUp(self):
        super().setUp()
        self.mod = load_module(SCRIPTS / "gt_components.py", "gt_components_dupdest")

    def manifest(self, *rels):
        d = self.tmp / "rel"
        d.mkdir(exist_ok=True)
        files = {rel: {"sha256": "%064x" % i, "bytes": 1} for i, rel in enumerate(rels)}
        (d / "MANIFEST.json").write_text(json.dumps({"version": "9.9.9", "files": files}),
                                        encoding="utf-8")
        return d

    def test_the_shipped_release_ships_nothing_twice(self):
        self.assertEqual(self.mod.duplicate_destinations(str(GT)), {},
                         "a release must not install two files to one destination")

    def test_the_gt_paths_shape_is_detected(self):
        d = self.manifest("hooks/gt_paths.py", "scripts/gt_paths.py", "scripts/vault_init.py")
        dups = self.mod.duplicate_destinations(str(d))
        self.assertEqual(len(dups), 1, dups)
        dst, rels = next(iter(dups.items()))
        self.assertTrue(dst.endswith("gt_paths.py"))
        self.assertEqual(sorted(rels), ["hooks/gt_paths.py", "scripts/gt_paths.py"])

    def test_same_basename_different_destinations_is_fine(self):
        """templates/*/README.md pairs are distinct files, not a collision."""
        d = self.manifest("templates/core-rules/README.md",
                          "templates/demo-pizzabot/project/README.md")
        self.assertEqual(self.mod.duplicate_destinations(str(d)), {})

    def test_a_missing_manifest_is_not_a_crash(self):
        self.assertEqual(self.mod.duplicate_destinations(str(self.tmp / "nope")), {})


class Wiring(ComponentsBase):
    def wiring(self, *args):
        return self.py(TOOL, "wiring", *args)

    def test_no_settings_file_means_everything_unwired(self):
        self.manifest()
        p = self.wiring(self.vdir)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.count("unwired"), N_HOOKS, p.stdout)

    def test_empty_settings_file(self):
        self.manifest()
        self.settings.write_text("")
        p = self.wiring(self.vdir)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.count("unwired"), N_HOOKS)

    def test_all_wired(self):
        self.full_setup()
        p = self.wiring(self.vdir)
        self.assertOk(p, "wiring")
        self.assertIn(f"all {N_HOOKS} declared hooks are wired", p.stdout)

    def test_owner_filter_after_dir(self):
        # install.sh's form: before a vault exists the enforcement hooks are
        # legitimately unwired, and must not count against the installer.
        self.manifest()
        self.install()
        self.wire(drop=ENFORCEMENT_HOOKS)
        p = self.wiring(self.vdir, "--owner", "install.sh")
        self.assertOk(p, "install.sh-owned hooks are all wired")
        self.assertIn(f"all {N_INSTALL_SH} declared hooks are wired (install.sh)", p.stdout)
        p = self.wiring(self.vdir)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.count("unwired"), N_ENFORCEMENT)

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
        self.assertEqual(p.stdout.count("unwired"), N_HOOKS)

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



class ModuleBase(ComponentsBase):
    """A fixture plugin root with gt 1.2.3 and two modules beside it.

    zed (default on) declares a SessionStart reporter hook taking {src} and a hook-dir
    script; yak (default off) declares a Stop guard.
    """

    def setUp(self):
        super().setUp()
        (self.vdir / ".claude-plugin").mkdir()
        (self.vdir / ".claude-plugin" / "plugin.json").write_text(
            '{"name": "gt", "version": "1.2.3"}')
        self.zed = self.module("zed", "on", hooks=[
            {"event": "SessionStart", "script": "zed_report.py",
             "args": ["check", "{src}", "--hook"], "kind": "reporter"}],
            hookdir=["zed_report.py"])
        self.yak = self.module("yak", "off", hooks=[
            {"event": "Stop", "script": "yak_guard.sh", "kind": "guard"}])
        self.choices = self.home / ".claude" / "golden-thread" / "install-choices.json"

    def module(self, name, default, hooks=(), hookdir=(), requires=">=1.0.0,<2.0.0",
               version="0.3.0"):
        vd = self.root / ("golden-thread-" + name) / version
        for sub in (".claude-plugin", "scripts", "hooks"):
            (vd / sub).mkdir(parents=True, exist_ok=True)
        (vd / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "gt-" + name, "version": version}))
        for h in hooks:
            sub = "hooks" if h["script"].endswith(".sh") else "scripts"
            (vd / sub / h["script"]).write_text("# %s\n" % h["script"])
        for s in hookdir:
            (vd / "scripts" / s).write_text("# %s\n" % s)
        (vd / "module.json").write_text(json.dumps({
            "schema": 1, "name": name, "plugin": "gt-" + name, "version": version,
            "requires_gt": requires, "summary": "fixture " + name, "default": default,
            "hooks": list(hooks), "hookdir_scripts": list(hookdir)}))
        return vd

    def states(self, *args):
        return self.py(TOOL, "module-states", self.root, "--home", self.home, *args)

    def choose(self, **choices):
        self.choices.parent.mkdir(parents=True, exist_ok=True)
        self.choices.write_text(json.dumps({"version": 1, "choices": choices}))


class ModuleStates(ModuleBase):
    def test_default_then_choice_then_override(self):
        p = self.states()
        self.assertOk(p)
        self.assertEqual(json.loads(p.stdout), {"yak": "off", "zed": "on"})
        self.choose(zed="off", yak="on")
        self.assertEqual(json.loads(self.states().stdout), {"yak": "on", "zed": "off"})
        p = self.states("--with", "zed", "--without", "yak")
        self.assertEqual(json.loads(p.stdout), {"yak": "off", "zed": "on"})
        self.assertEqual(json.loads(self.choices.read_text())["choices"],
                         {"zed": "off", "yak": "on"}, "module-states must never persist")

    def test_detail_says_why(self):
        self.choose(yak="on")
        det = json.loads(self.states("--detail", "--without", "zed").stdout)
        self.assertEqual(det["zed"]["reason"], "--without")
        self.assertEqual(det["yak"]["reason"], "recorded choice")
        self.assertEqual(det["yak"]["version"], "0.3.0")
        self.assertEqual(det["yak"]["plugin"], "gt-yak")
        self.assertTrue(det["yak"]["admitted"])

    def test_unknown_name_and_gt_are_refused_with_the_module_list(self):
        for args in (("--with", "nope"), ("--without", "nope"), ("--with", "gt"),
                     ("--without", "gt")):
            p = self.states(*args)
            self.assertEqual(p.returncode, 2, args)
            self.assertIn("modules: yak, zed", p.stdout + p.stderr, args)
        p = self.states("--with", "zed", "--without", "zed")
        self.assertEqual(p.returncode, 2)

    def test_requires_gt_mismatch_is_off_for_this_run_and_choice_kept(self):
        self.module("far", "on", requires=">=2.0.0")
        self.choose(far="on")
        p = self.states("--with", "far")
        self.assertOk(p)
        self.assertEqual(json.loads(p.stdout)["far"], "off")
        self.assertIn("requires gt >=2.0.0", p.stderr)
        self.assertEqual(json.loads(self.choices.read_text())["choices"]["far"], "on")
        # Judged against the gt being installed, which install.sh may pin.
        p = self.states("--gt-version", "2.1.0")
        self.assertEqual(json.loads(p.stdout)["far"], "on")
        self.assertEqual(json.loads(p.stdout)["zed"], "off")

    def test_invalid_module_is_listed_but_off(self):
        bad = self.module("bad", "on")
        data = json.loads((bad / "module.json").read_text())
        data["colour"] = "blue"
        (bad / "module.json").write_text(json.dumps(data))
        det = json.loads(self.states("--detail").stdout)
        self.assertEqual(det["bad"]["state"], "off")
        self.assertIn("unknown key 'colour'", det["bad"]["reason"])

    def test_newest_module_release_is_the_one_read(self):
        self.module("zed", "off", version="0.10.0")
        det = json.loads(self.states("--detail").stdout)
        self.assertEqual((det["zed"]["version"], det["zed"]["state"]), ("0.10.0", "off"))

    def test_no_modules_is_an_empty_object(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        p = self.py(TOOL, "module-states", empty, "--home", self.home)
        self.assertOk(p)
        self.assertEqual(json.loads(p.stdout), {})

    def test_record_choice(self):
        self.choose(yak="on")
        data = json.loads(self.choices.read_text())
        data["note"] = "kept"
        self.choices.write_text(json.dumps(data))
        p = self.py(TOOL, "record-choice", self.home, "zed", "off")
        self.assertOk(p)
        doc = json.loads(self.choices.read_text())
        self.assertEqual(doc["choices"], {"yak": "on", "zed": "off"})
        self.assertEqual((doc["version"], doc["note"]), (1, "kept"))
        self.assertEqual(json.loads(self.states().stdout)["zed"], "off")
        for args in (("gt", "on"), ("zed", "maybe"), ("Bad Name", "on")):
            self.assertEqual(self.py(TOOL, "record-choice", self.home, *args).returncode, 2, args)
        p = self.py(TOOL, "record-choice", self.home, "nope", "on", "--plugin-root", self.root)
        self.assertEqual(p.returncode, 2)
        self.assertIn("modules: yak, zed", p.stdout)
        self.assertNotIn("nope", json.loads(self.choices.read_text())["choices"])

    def test_record_choice_creates_the_file(self):
        self.assertFalse(self.choices.exists())
        self.assertOk(self.py(TOOL, "record-choice", self.home, "yak", "on"))
        self.assertEqual(json.loads(self.choices.read_text()),
                         {"version": 1, "choices": {"yak": "on"}})

    def test_usage(self):
        self.assertEqual(self.py(TOOL, "module-states").returncode, 2)
        self.assertEqual(self.py(TOOL, "record-choice", self.home, "zed").returncode, 2)


class ModuleHooks(ModuleBase):
    def test_on_module_hooks_are_registered_and_tagged(self):
        regs = self.registrations()
        mine = [r for r in regs if r.get("module")]
        self.assertEqual(len(regs), N_HOOKS + 1)
        self.assertEqual(len(mine), 1)
        r = mine[0]
        self.assertEqual((r["event"], r["script"], r["owner"], r["module"]),
                         ("SessionStart", "zed_report.py", "install.sh", "zed"))
        argv = shlex.split(r["command"])
        self.assertEqual(argv[:2], ["python3", str(self.installed / "zed_report.py")])
        self.assertEqual(argv[2:], ["check", str(self.zed), "--hook"],
                         "{src} in a module hook is the module's own version dir")
        self.assertFalse(any("module" in x for x in regs[:N_HOOKS]))

    def test_off_module_hooks_are_not_registered(self):
        self.choose(zed="off", yak="on")
        mine = [r for r in self.registrations() if r.get("module")]
        self.assertEqual([(r["module"], r["script"]) for r in mine], [("yak", "yak_guard.sh")])
        self.assertEqual(shlex.split(mine[0]["command"]), [str(self.installed / "yak_guard.sh")])

    def test_home_flag_reads_that_homes_choices(self):
        other = self.tmp / "other-home"
        (other / ".claude" / "golden-thread").mkdir(parents=True)
        (other / ".claude" / "golden-thread" / "install-choices.json").write_text(
            json.dumps({"choices": {"zed": "off"}}))
        p = self.py(TOOL, "hook-registrations", self.vdir, "--home", other)
        self.assertOk(p)
        self.assertFalse(any(r.get("module") for r in json.loads(p.stdout)))

    def test_manifest_hooks_stay_gts_static_list(self):
        man = self.manifest()
        self.assertEqual(len(man["hooks"]), N_HOOKS)
        self.assertFalse(any("module" in h or h["script"].startswith("zed")
                             for h in man["hooks"]))

    def test_hookdir_scripts_include_on_modules(self):
        p = self.py(TOOL, "hookdir-scripts")
        self.assertNotIn("zed_report.py", p.stdout.split())
        p = self.py(TOOL, "hookdir-scripts", self.root, "--home", self.home)
        self.assertOk(p)
        names = p.stdout.split()
        self.assertEqual(names[-1], "zed_report.py")
        self.choose(zed="off")
        p = self.py(TOOL, "hookdir-scripts", self.root)
        self.assertNotIn("zed_report.py", p.stdout.split())

    def test_wiring_counts_on_module_hooks_and_skips_off_ones(self):
        self.full_setup()                            # wires gt + zed (on)
        shutil.copy2(self.zed / "scripts" / "zed_report.py", self.installed / "zed_report.py")
        p = self.py(TOOL, "wiring", self.vdir)
        self.assertOk(p)
        self.assertIn("all %d declared hooks are wired" % (N_HOOKS + 1), p.stdout)
        # yak switched on but not wired: its hook is drift.
        self.choose(yak="on")
        p = self.py(TOOL, "wiring", self.vdir)
        self.assertEqual(p.returncode, 1)
        self.assertRegex(p.stdout, r"unwired\s+yak_guard\.sh\s+Stop")
        # zed off and its entry removed: not drift -- not installed by choice.
        self.choose(zed="off")
        self.wire([r for r in self.registrations()], drop=("zed_report.py", "yak_guard.sh"))
        p = self.py(TOOL, "wiring", self.vdir)
        self.assertOk(p, "an OFF module's hook was reported as unwired")
        self.assertIn("all %d declared hooks are wired" % N_HOOKS, p.stdout)

    def test_check_is_clean_with_an_on_module_installed(self):
        self.full_setup()
        shutil.copy2(self.zed / "scripts" / "zed_report.py", self.installed / "zed_report.py")
        out = self.check()
        self.assertIn("clean", out)
        self.assertIn("all %d hooks wired" % (N_HOOKS + 1), out)
        self.assertNotIn("extra", out)


if __name__ == "__main__":
    unittest.main()
