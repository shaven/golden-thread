"""gt_settings.py: the settings registry, its reader, and the hook-output helpers.

Contracts pinned here:
  * every setting is a top-level key in ~/.claude/vault-config.json;
  * `get()` applies the registered default for a missing, unreadable or invalid value,
    and normalises case/whitespace -- it never raises;
  * `set` validates, refuses to create a config without vault_path, and keeps every
    other key in the file;
  * `emit()` produces hook JSON (systemMessage + additionalContext) only when told it
    is a hook; `capture()` keeps partial output when the wrapped function raises.
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _harness import Sandbox, SCRIPTS, load_module, WATCH, REPORT_CARD

TOOL = SCRIPTS / "gt_settings.py"
EXPECTED = {
    "component_updates": ("report", ["off", "report", "confirm", "auto"]),
    "version_check": ("report", ["off", "report"]),
    "orphan_check": ("report", ["off", "report", "reap"]),
    "push_check": ("report", ["off", "report"]),
    "protected_paths": ("ask", ["off", "ask"]),
    "test_gate": ("auto", ["off", "warn", "auto", "block"]),
    "parallel_work": ("on", ["off", "on"]),
    # values None == free-form; `validate` carries the shape instead of a closed list.
    "parallel_max": ("auto", None),
}
# install_demo is gone since 0.14.0: the demo is a module and its install is a module
# choice (install-choices.json), not a gt setting.
REMOVED = ("install_demo",)
# 0.15.0: these moved to the watch and report-card modules and register from module.json.
MOVED_TO_MODULES = {"watch": ("watch", "off", ["off", "report"]),
                    "report_card": ("report-card", "minimal", ["off", "minimal", "full"]),
                    "closeout_check": ("report-card", "ask", ["off", "ask"])}


class SettingsCli(Sandbox):
    def get(self, name):
        p = self.py(TOOL, "get", name)
        self.assertOk(p, "get %s" % name)
        return p.stdout.strip()

    def cfg_path(self):
        return self.home / ".claude" / "vault-config.json"

    def test_set_rejects_a_bad_ceiling_and_accepts_a_good_one(self):
        self.config(vault_path=str(self.tmp))
        p = self.py(TOOL, "set", "parallel_max", "six")
        self.assertNotEqual(p.returncode, 0, "a non-numeric ceiling must be refused")
        self.assertIn("positive integer", p.stdout + p.stderr)
        self.assertEqual(self.get("parallel_max"), "auto", "a refused set writes nothing")
        self.assertOk(self.py(TOOL, "set", "parallel_max", "6"))
        self.assertEqual(self.get("parallel_max"), "6")
        self.assertOk(self.py(TOOL, "set", "parallel_max", "auto"))
        self.assertEqual(self.get("parallel_max"), "auto")

    def test_show_renders_a_freeform_setting(self):
        self.config(vault_path=str(self.tmp))
        p = self.py(TOOL, "show")
        self.assertOk(p)
        self.assertIn("parallel_max", p.stdout)
        self.assertIn("positive integer", p.stdout,
                      "a free-form setting must still print what it accepts")

    # -- reading -------------------------------------------------------------------
    def test_defaults_apply_with_no_config_file(self):
        self.assertFalse(self.cfg_path().exists())
        for name, (default, _) in EXPECTED.items():
            self.assertEqual(self.get(name), default, name)

    def test_defaults_apply_with_malformed_config(self):
        self.cfg_path().write_text("{not json", encoding="utf-8")
        for name, (default, _) in EXPECTED.items():
            self.assertEqual(self.get(name), default, name)
        p = self.py(TOOL, "show")
        self.assertOk(p, "show must survive an unparseable config")

    def test_invalid_value_falls_back_to_default(self):
        self.config(vault_path=str(self.tmp), component_updates="sometimes",
                    push_check="maybe", orphan_check="")
        self.assertEqual(self.get("component_updates"), "report")
        self.assertEqual(self.get("push_check"), "report")
        self.assertEqual(self.get("orphan_check"), "report")

    def test_value_is_normalised(self):
        self.config(vault_path=str(self.tmp), component_updates="  AUTO ",
                    push_check="Off")
        self.assertEqual(self.get("component_updates"), "auto")
        self.assertEqual(self.get("push_check"), "off")

    def test_install_demo_is_no_longer_a_setting(self):
        """The demo is a module since 0.14.0; its install is a module choice."""
        self.config(vault_path=str(self.tmp), install_demo="no")
        self.assertEqual(self.get("install_demo"), "", "install_demo is still registered")
        p = self.py(TOOL, "set", "install_demo", "no")
        self.assertEqual(p.returncode, 2)
        self.assertIn("unknown setting", p.stdout)
        p = self.py(TOOL, "show")
        self.assertOk(p)
        self.assertNotIn("install_demo", p.stdout)
        cfg = json.loads(self.cfg_path().read_text())
        self.assertEqual(cfg.get("install_demo"), "no", "a user's key must be left alone")

    def test_non_string_value_does_not_crash(self):
        # A hand-edited `"orphan_check": true` / `"orphan_check": 1` is the obvious way
        # to write a flag in JSON. It is not a registered value, so the default
        # must apply -- the reader must not raise, and `show` (what /gt:gt-settings
        # renders) must not die on it.
        for bad in (True, 1, ["no"]):
            self.config(vault_path=str(self.tmp), orphan_check=bad)
            p = self.py(TOOL, "get", "orphan_check")
            self.assertOk(p, "gt_settings.get() raised on a non-string value %r "
                             "(`(v or '').strip()` assumes str)" % (bad,))
            self.assertEqual(p.stdout.strip(), "report")
            p = self.py(TOOL, "show")
            self.assertOk(p, "show crashed on orphan_check=%r" % (bad,))

    def test_unknown_setting_get_prints_empty(self):
        p = self.py(TOOL, "get", "no_such_setting")
        self.assertOk(p)
        self.assertEqual(p.stdout.strip(), "")

    def test_show_lists_every_setting_and_marks_defaults(self):
        self.config(vault_path=str(self.tmp), push_check="off")
        p = self.py(TOOL, "show")
        self.assertOk(p)
        for name in EXPECTED:
            self.assertIn(name, p.stdout)
        push = [l for l in p.stdout.splitlines() if l.strip().startswith("push_check")][0]
        self.assertIn("off", push)
        self.assertNotIn("default", push)
        orphan = [l for l in p.stdout.splitlines() if l.strip().startswith("orphan_check")][0]
        self.assertIn("report", orphan)
        self.assertIn("default", orphan)

    def test_explain(self):
        p = self.py(TOOL, "explain", "orphan_check")
        self.assertOk(p)
        self.assertIn("current: report, default: report", p.stdout)
        p = self.py(TOOL, "explain", "bogus")
        self.assertEqual(p.returncode, 2)
        self.assertIn("unknown setting", p.stdout)

    def test_usage_on_bad_command(self):
        p = self.py(TOOL, "frobnicate")
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage", p.stdout)

    # -- writing -------------------------------------------------------------------
    def test_set_refuses_without_vault_path(self):
        p = self.py(TOOL, "set", "orphan_check", "off")
        self.assertEqual(p.returncode, 2)
        self.assertIn("refusing", p.stdout)
        self.assertFalse(self.cfg_path().exists(),
                         "set created a config with no vault_path")
        self.config(core_rules_path="x")
        p = self.py(TOOL, "set", "orphan_check", "off")
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("orphan_check", self.cfg_path().read_text())

    def test_set_writes_top_level_key_and_keeps_others(self):
        self.config(vault_path="/some/vault", core_rules_path="Projects/x",
                    orphan_check="reap")
        p = self.py(TOOL, "set", "push_check", "OFF")
        self.assertOk(p)
        self.assertIn("push_check: report -> off", p.stdout)
        d = json.loads(self.cfg_path().read_text())
        self.assertEqual(d, {"vault_path": "/some/vault", "core_rules_path": "Projects/x",
                             "orphan_check": "reap", "push_check": "off"})
        self.assertEqual(self.get("push_check"), "off")

    def test_set_rejects_invalid_value_and_unknown_name(self):
        self.config(vault_path="/v")
        before = self.cfg_path().read_text()
        p = self.py(TOOL, "set", "version_check", "auto")   # deliberately no auto
        self.assertEqual(p.returncode, 2)
        self.assertIn("invalid value", p.stdout)
        p = self.py(TOOL, "set", "nonsense", "on")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.cfg_path().read_text(), before)


class ModuleSettings(Sandbox):
    """An ON module's settings are registered at load and shown under its name."""

    def setUp(self):
        super().setUp()
        import shutil
        self.root = self.tmp / "Golden Thread" / "plugin"
        gt = self.root / "golden-thread" / "9.9.9"
        (gt / "scripts").mkdir(parents=True)
        (gt / ".claude-plugin").mkdir()
        (gt / ".claude-plugin" / "plugin.json").write_text('{"name": "gt", "version": "9.9.9"}')
        for n in ("gt_settings.py", "gt_components.py"):
            shutil.copy2(SCRIPTS / n, gt / "scripts" / n)
        self.tool = gt / "scripts" / "gt_settings.py"
        self.module("zed", requires=">=9.0.0", settings=[
            {"key": "zed_mode", "default": "calm", "values": ["calm", "loud"],
             "summary": "How loud zed is."},
            {"key": "orphan_check", "default": "reap", "values": ["reap"],
             "summary": "A module may not override a gt setting."}])

    def module(self, name, requires, settings, root=None):
        vd = (root or self.root) / ("golden-thread-" + name) / "1.0.0"
        (vd / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (vd / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "gt-" + name, "version": "1.0.0"}))
        (vd / "module.json").write_text(json.dumps({
            "schema": 1, "name": name, "plugin": "gt-" + name, "version": "1.0.0",
            "requires_gt": requires, "summary": "fixture", "default": "on",
            "settings": settings}))

    def test_on_module_settings_are_registered_and_shown_under_its_name(self):
        self.config(vault_path=str(self.tmp))
        p = self.py(self.tool, "show")
        self.assertOk(p)
        lines = p.stdout.splitlines()
        self.assertIn("module zed", lines)
        head = lines.index("module zed")
        self.assertTrue(any(l.strip().startswith("zed_mode") for l in lines[head:]))
        self.assertFalse(any(l.strip().startswith("zed_mode") for l in lines[:head]))
        self.assertEqual(self.py(self.tool, "get", "zed_mode").stdout.strip(), "calm")
        self.assertOk(self.py(self.tool, "set", "zed_mode", "loud"))
        self.assertEqual(self.py(self.tool, "get", "zed_mode").stdout.strip(), "loud")
        self.assertEqual(self.py(self.tool, "get", "orphan_check").stdout.strip(), "report",
                         "a module setting overrode gt's own")

    def choose(self, **choices):
        path = self.home / ".claude" / "golden-thread" / "install-choices.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "choices": choices}))

    def test_off_module_unset_settings_are_not_shown(self):
        self.choose(zed="off")
        p = self.py(self.tool, "show")
        self.assertOk(p)
        self.assertNotIn("zed_mode", p.stdout)
        self.assertNotIn("module zed", p.stdout)

    def test_a_value_set_survives_the_module_going_off_and_back_on(self):
        """design §4 "Remove": settings keys kept, marked orphaned."""
        self.config(vault_path=str(self.tmp))
        self.assertOk(self.py(self.tool, "set", "zed_mode", "loud"))
        self.choose(zed="off")
        p = self.py(self.tool, "show")
        self.assertOk(p)
        self.assertIn("module zed   (OFF — settings kept, not in effect; install.sh --with zed)",
                      p.stdout)
        self.assertTrue([l for l in p.stdout.splitlines()
                         if l.strip().startswith("zed_mode") and "loud" in l], p.stdout)
        self.assertEqual(self.py(self.tool, "get", "zed_mode").stdout.strip(), "loud")
        self.assertIn("currently OFF", self.py(self.tool, "explain", "zed_mode").stdout)
        p = self.py(self.tool, "set", "zed_mode", "calm")
        self.assertOk(p, "an orphaned setting can still be changed")
        self.assertIn("kept but not in effect", p.stdout)
        self.assertOk(self.py(self.tool, "set", "zed_mode", "loud"))
        self.assertEqual(json.loads((self.home / ".claude" / "vault-config.json").read_text())
                         ["zed_mode"], "loud")
        self.choose(zed="on")
        p = self.py(self.tool, "show")
        self.assertIn("module zed", p.stdout.splitlines())
        self.assertNotIn("OFF", p.stdout)
        self.assertEqual(self.py(self.tool, "get", "zed_mode").stdout.strip(), "loud")

    def test_module_not_admitting_this_gt_is_not_shown(self):
        self.module("zed", requires=">=10.0.0", settings=[
            {"key": "zed_mode", "default": "calm", "values": ["calm"], "summary": "x"}])
        self.assertNotIn("zed_mode", self.py(self.tool, "show").stdout)

    def test_detail_is_carried_into_explain(self):
        self.module("zed", requires=">=9.0.0", settings=[
            {"key": "zed_mode", "default": "calm", "values": ["calm", "loud"],
             "summary": "How loud zed is.",
             "detail": "calm  quiet\nloud  noisy\n\nOn 2026-09-14 zed woke everyone."}])
        p = self.py(self.tool, "explain", "zed_mode")
        self.assertOk(p)
        self.assertIn("On 2026-09-14 zed woke everyone.", p.stdout)
        self.assertIn("Provided by the zed module.", p.stdout)

    def test_no_reachable_source_reads_the_installed_caches(self):
        """Run from the hooks dir with no gt_version_check entry in settings.json (a moved
        source tree): the ON modules are the ones install.sh left a cache for."""
        import shutil
        hooks = self.home / ".claude" / "golden-thread" / "hooks"
        hooks.mkdir(parents=True)
        for n in ("gt_settings.py", "gt_components.py"):
            shutil.copy2(SCRIPTS / n, hooks / n)
        cache = self.home / ".claude" / "plugins" / "cache" / "golden-thread-plugin"
        (cache / "golden-thread-zed").mkdir(parents=True)
        self.module("zed", requires=">=9.0.0", root=cache, settings=[
            {"key": "zed_mode", "default": "calm", "values": ["calm", "loud"],
             "summary": "How loud zed is."}])
        # the cache dir is named after the plugin, not the source dir
        shutil.move(str(cache / "golden-thread-zed"), str(cache / "gt-zed"))
        self.assertEqual(self.py(hooks / "gt_settings.py", "get", "zed_mode").stdout.strip(),
                         "calm", "a module setting vanished when the source was unreachable")


class MovedSettingsComeFromTheirModules(Sandbox):
    """watch, report_card and closeout_check, from the real modules beside the real gt,
    with the defaults and the explanations they had in gt."""

    def test_defaults_values_and_detail_survive_the_move(self):
        if WATCH is None or REPORT_CARD is None:
            self.skipTest("watch / report-card modules not in this tree")
        for name, (module, default, values) in MOVED_TO_MODULES.items():
            self.assertEqual(self.py(TOOL, "get", name).stdout.strip(), default, name)
            p = self.py(TOOL, "explain", name)
            self.assertOk(p)
            self.assertIn("Provided by the %s module" % module, p.stdout, name)
            self.assertIn(values[-1], p.stdout, name)
        self.assertIn("CYC26", self.py(TOOL, "explain", "closeout_check").stdout,
                      "closeout_check lost the incident that is its argument")
        self.assertIn("cron", self.py(TOOL, "explain", "watch").stdout)

    def test_switching_a_setting_off_is_not_an_uninstall(self):
        if REPORT_CARD is None:
            self.skipTest("report-card module not in this tree")
        self.config(vault_path=str(self.tmp))
        self.assertOk(self.py(TOOL, "set", "report_card", "off"))
        self.assertFalse((self.home / ".claude" / "golden-thread" / "install-choices.json")
                         .exists(), "a setting wrote a module choice")


class SettingsInProcess(Sandbox):
    """Registry integrity and the emit/capture helpers, imported in-process."""

    def setUp(self):
        super().setUp()
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.m = load_module(TOOL, "gt_settings_under_test")

    def tearDown(self):
        if self._home is not None:
            os.environ["HOME"] = self._home
        super().tearDown()

    def test_config_path_is_in_sandbox(self):
        self.assertTrue(self.m.CONFIG.startswith(str(self.home)))

    def test_registry_is_self_consistent(self):
        # gt's own settings; a module's (registered from its module.json) carry "module".
        own = {n for n, s in self.m.SETTINGS.items() if not s.get("module")}
        self.assertEqual(own, set(EXPECTED))
        for gone in REMOVED:
            self.assertNotIn(gone, self.m.SETTINGS)
        for name in MOVED_TO_MODULES:
            self.assertNotIn(name, own, "%s is a module setting since 0.15.0" % name)
        for name, spec in self.m.SETTINGS.items():
            if spec.get("module"):
                self.assertIn(spec["default"], spec["values"], name)
                self.assertTrue(spec["summary"].strip() and spec["detail"].strip(), name)
                continue
            if spec["values"] is None:
                # A free-form setting must still say what it accepts, and its own
                # default must pass its own validator -- a default the reader rejects
                # would make every unset value fall back to something invalid.
                self.assertTrue(spec.get("validate", "").strip(), name)
                self.assertTrue(self.m._freeform_ok(name, spec["default"]), name)
            else:
                self.assertIn(spec["default"], spec["values"], name)
            self.assertEqual((spec["default"], spec["values"]), EXPECTED[name], name)
            self.assertTrue(spec["summary"].strip() and spec["detail"].strip(), name)

    # -- the parallel budget ---------------------------------------------------------
    def test_parallel_max_accepts_auto_and_integers(self):
        for good in ("auto", "1", "4", "64"):
            self.assertTrue(self.m._freeform_ok("parallel_max", good), good)
        for bad in ("", "0", "-2", "many", "4.5", "as many as possible"):
            self.assertFalse(self.m._freeform_ok("parallel_max", bad), bad)

    def test_parallel_jobs_caps_at_the_setting(self):
        cores = os.cpu_count() or 4
        self.config(vault_path=str(self.tmp))
        # auto: never more workers than there is work, never more than the machine.
        self.assertEqual(self.m.parallel_jobs(3), 3)
        self.assertEqual(self.m.parallel_jobs(10 ** 6), cores)
        # I/O-bound work is allowed above core count -- that is what `auto` means here.
        # The ceiling is READ from the machine profile, never hardcoded: this test used
        # to assert `<= 20`, which was the constant 0.12.4 shipped, and it failed the
        # moment 0.12.5 started measuring the machine (32 here). A test that pins a
        # number the product deliberately derives will fail on the next machine anyway.
        io_max = self.m.detect_machine()["io_max"]
        self.assertGreater(io_max, cores - 1, "auto must allow oversubscription for I/O")
        self.assertEqual(self.m.parallel_jobs(10 ** 6, io_bound=True), io_max)

    def test_parallel_jobs_honours_a_ceiling_and_off(self):
        self.config(vault_path=str(self.tmp), parallel_max="2")
        self.assertEqual(self.m.parallel_jobs(10 ** 6), 2)
        self.assertEqual(self.m.parallel_jobs(10 ** 6, io_bound=True), 2)
        self.config(vault_path=str(self.tmp), parallel_work="off", parallel_max="8")
        self.assertEqual(self.m.parallel_jobs(10 ** 6), 1,
                         "parallel_work=off means serial whatever the ceiling says")

    def test_parallel_jobs_never_returns_zero(self):
        self.config(vault_path=str(self.tmp), parallel_max="4")
        self.assertEqual(self.m.parallel_jobs(0), 1)
        # A nonsense ceiling falls back to the default rather than serialising silently.
        self.config(vault_path=str(self.tmp), parallel_max="lots")
        self.assertEqual(self.m.get("parallel_max"), "auto")
        self.assertGreaterEqual(self.m.parallel_jobs(8), 1)

    def test_hook_args(self):
        argv, is_hook = self.m.hook_args(["check", "--hook", "/x"])
        self.assertEqual((argv, is_hook), (["check", "/x"], True))
        old = os.environ.pop("GT_HOOK", None)
        try:
            self.assertEqual(self.m.hook_args(["check"]), (["check"], False))
            os.environ["GT_HOOK"] = "1"
            self.assertEqual(self.m.hook_args(["check"]), (["check"], True))
        finally:
            os.environ.pop("GT_HOOK", None)
            if old is not None:
                os.environ["GT_HOOK"] = old

    def test_emit_plain_and_hook(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.m.emit("  hello\n", as_hook=False)
        self.assertEqual(buf.getvalue(), "hello\n")

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.m.emit("line one\nline two", event="PreCompact", as_hook=True)
        d = json.loads(buf.getvalue())
        self.assertEqual(d["systemMessage"], "line one\nline two")
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "PreCompact")
        self.assertEqual(d["hookSpecificOutput"]["additionalContext"], "line one\nline two")

    def test_emit_empty_says_nothing(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.m.emit("   ", as_hook=True)
            self.m.emit(None, as_hook=False)
        self.assertEqual(buf.getvalue(), "")

    def test_capture_keeps_partial_output_on_crash(self):
        def boom():
            print("three orphans listed")
            raise RuntimeError("reap exploded")
        err = io.StringIO()
        with redirect_stderr(err):
            r, text = self.m.capture(boom)
        self.assertIsNone(r)
        self.assertIn("three orphans listed", text)
        self.assertIn("check aborted: RuntimeError: reap exploded", text)
        self.assertIn("Traceback", err.getvalue())

    def test_capture_returns_result(self):
        r, text = self.m.capture(lambda: (print("x"), 7)[1])
        self.assertEqual((r, text), (7, "x\n"))


if __name__ == "__main__":
    unittest.main()
