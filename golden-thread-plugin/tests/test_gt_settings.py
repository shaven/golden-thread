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
}


class SettingsCli(Sandbox):
    def get(self, name):
        p = self.py(TOOL, "get", name)
        self.assertOk(p, "get %s" % name)
        return p.stdout.strip()

    def cfg_path(self):
        return self.home / ".claude" / "vault-config.json"

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
            self.assertIn(spec["default"], spec["values"], name)
            self.assertEqual((spec["default"], spec["values"]), EXPECTED[name], name)
            self.assertTrue(spec["summary"].strip() and spec["detail"].strip(), name)

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
