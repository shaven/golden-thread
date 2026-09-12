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

from _harness import Sandbox, SCRIPTS, load_module

TOOL = SCRIPTS / "gt_settings.py"
EXPECTED = {
    "component_updates": ("report", ["off", "report", "confirm", "auto"]),
    "version_check": ("report", ["off", "report"]),
    "orphan_check": ("report", ["off", "report", "reap"]),
    "push_check": ("report", ["off", "report"]),
    "watch": ("off", ["off", "report"]),
    "closeout_check": ("ask", ["off", "ask"]),
    "report_card": ("minimal", ["off", "minimal", "full"]),
    "install_demo": ("yes", ["yes", "no"]),
    "test_gate": ("auto", ["off", "warn", "auto", "block"]),
    "parallel_work": ("on", ["off", "on"]),
    # values None == free-form; `validate` carries the shape instead of a closed list.
    "parallel_max": ("auto", None),
}


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
                    install_demo="maybe", report_card="")
        self.assertEqual(self.get("component_updates"), "report")
        self.assertEqual(self.get("install_demo"), "yes")
        self.assertEqual(self.get("report_card"), "minimal")

    def test_value_is_normalised(self):
        self.config(vault_path=str(self.tmp), component_updates="  AUTO ",
                    install_demo="No")
        self.assertEqual(self.get("component_updates"), "auto")
        self.assertEqual(self.get("install_demo"), "no")

    def test_install_demo_values(self):
        self.assertEqual(self.get("install_demo"), "yes")
        self.config(vault_path=str(self.tmp), install_demo="no")
        self.assertEqual(self.get("install_demo"), "no")
        self.config(vault_path=str(self.tmp), install_demo="yes")
        self.assertEqual(self.get("install_demo"), "yes")

    def test_non_string_value_does_not_crash(self):
        # A hand-edited `"install_demo": true` / `"report_card": 1` is the obvious way
        # to write a yes/no flag in JSON. It is not a registered value, so the default
        # must apply -- the reader must not raise, and `show` (what /gt:gt-settings
        # renders) must not die on it.
        for bad in (True, 1, ["no"]):
            self.config(vault_path=str(self.tmp), install_demo=bad)
            p = self.py(TOOL, "get", "install_demo")
            self.assertOk(p, "gt_settings.get() raised on a non-string value %r "
                             "(`(v or '').strip()` assumes str)" % (bad,))
            self.assertEqual(p.stdout.strip(), "yes")
            p = self.py(TOOL, "show")
            self.assertOk(p, "show crashed on install_demo=%r" % (bad,))

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
        demo = [l for l in p.stdout.splitlines() if l.strip().startswith("install_demo")][0]
        self.assertIn("yes", demo)
        self.assertIn("default", demo)

    def test_explain(self):
        p = self.py(TOOL, "explain", "install_demo")
        self.assertOk(p)
        self.assertIn("current: yes, default: yes", p.stdout)
        p = self.py(TOOL, "explain", "bogus")
        self.assertEqual(p.returncode, 2)
        self.assertIn("unknown setting", p.stdout)

    def test_usage_on_bad_command(self):
        p = self.py(TOOL, "frobnicate")
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage", p.stdout)

    # -- writing -------------------------------------------------------------------
    def test_set_refuses_without_vault_path(self):
        p = self.py(TOOL, "set", "install_demo", "no")
        self.assertEqual(p.returncode, 2)
        self.assertIn("refusing", p.stdout)
        self.assertFalse(self.cfg_path().exists(),
                         "set created a config with no vault_path")
        self.config(core_rules_path="x")
        p = self.py(TOOL, "set", "install_demo", "no")
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("install_demo", self.cfg_path().read_text())

    def test_set_writes_top_level_key_and_keeps_others(self):
        self.config(vault_path="/some/vault", core_rules_path="Projects/x",
                    report_card="full")
        p = self.py(TOOL, "set", "install_demo", "NO")
        self.assertOk(p)
        self.assertIn("install_demo: yes -> no", p.stdout)
        d = json.loads(self.cfg_path().read_text())
        self.assertEqual(d, {"vault_path": "/some/vault", "core_rules_path": "Projects/x",
                             "report_card": "full", "install_demo": "no"})
        self.assertEqual(self.get("install_demo"), "no")

    def test_set_rejects_invalid_value_and_unknown_name(self):
        self.config(vault_path="/v")
        before = self.cfg_path().read_text()
        p = self.py(TOOL, "set", "version_check", "auto")   # deliberately no auto
        self.assertEqual(p.returncode, 2)
        self.assertIn("invalid value", p.stdout)
        p = self.py(TOOL, "set", "nonsense", "on")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.cfg_path().read_text(), before)


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
        self.assertEqual(set(self.m.SETTINGS), set(EXPECTED))
        for name, spec in self.m.SETTINGS.items():
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
