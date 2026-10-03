"""gt_doctor `hooks-schema` (0.18.1, 2026-09-24-hook-config-drift-detector).

`wiring` asks whether every hook the release declares is in settings.json. It cannot see an
entry that is present, correctly pointed, and never fires because its event or tool matcher
names something Claude Code does not have. Fixtures:

  1 unknown event   -> hooks-unknown-event
  2 unknown tool    -> hooks-unknown-tool (only on tool events; mcp__, '*' and regex left alone)
  3 missing file    -> hooks-missing-file (an entry pointing into the gt hooks dir at nothing)
  4 clean           -> no findings
plus: the allowlists ship, are not empty, and contain every event gt and its modules register;
the check is part of the default run (so /gt:gt-allin, which runs the doctor, shows it).
"""
import glob
import json

from _harness import GT, REPO, SCRIPTS, load_module
from test_gt_doctor import DoctorBase


class HooksSchema(DoctorBase):
    def settings(self, hooks):
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"hooks": hooks}))

    def entry(self, cmd, matcher=None):
        b = {"hooks": [{"type": "command", "command": cmd}]}
        if matcher is not None:
            b["matcher"] = matcher
        return b

    def real_hook(self, name="guard.sh"):
        f = self.hooks / name
        f.write_text("#!/bin/sh\n")
        return f.as_posix()     # as gt writes a hook command; == str(f) on POSIX

    def row(self):
        p = self.doctor("--json", "--only", "hooks-schema")
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        rows = json.loads(p.stdout)["checks"]
        self.assertEqual(["hooks-schema"], [r["check"] for r in rows])
        return rows[0]

    def test_unknown_event(self):
        self.settings({"NonExistentEvent": [self.entry(self.real_hook())]})
        r = self.row()
        self.assertEqual("warn", r["state"])
        self.assertIn("hooks-unknown-event NonExistentEvent", r["detail"])

    def test_unknown_tool(self):
        self.settings({"PreToolUse": [self.entry(self.real_hook(), "Write|NonExistentTool")]})
        r = self.row()
        self.assertEqual("warn", r["state"])
        self.assertIn("hooks-unknown-tool", r["detail"])
        self.assertIn("NonExistentTool", r["detail"])
        self.assertNotIn("no known tool is called Write", r["detail"])

    def test_missing_file(self):
        self.settings({"Stop": [self.entry("bash %s" % (self.hooks / "missing.sh").as_posix())]})
        r = self.row()
        self.assertEqual("warn", r["state"])
        self.assertIn("hooks-missing-file Stop", r["detail"])
        self.assertIn("missing.sh", r["detail"])

    def test_missing_file_via_tilde_and_home(self):
        self.settings({"Stop": [self.entry("~/.claude/golden-thread/hooks/gone.sh"),
                                self.entry("$HOME/.claude/golden-thread/hooks/gone2.sh")]})
        r = self.row()
        self.assertIn("gone.sh", r["detail"])
        self.assertIn("gone2.sh", r["detail"])

    def test_clean_hooks_produce_no_findings(self):
        h = self.real_hook()
        self.settings({
            "PreToolUse": [self.entry(h, "Write|Edit"), self.entry(h, "mcp__github__.*"),
                           self.entry(h, "*"), self.entry(h, "Notebook.*"), self.entry(h)],
            "SessionStart": [self.entry("python3 %s check --hook" % h, "compact")],
            "UserPromptSubmit": [self.entry(h)], "Stop": [self.entry(h)],
            "PreCompact": [self.entry(h)], "SessionEnd": [self.entry(h)],
            "PostToolUse": [self.entry("/usr/bin/true", "Bash")],
        })
        r = self.row()
        self.assertEqual("ok", r["state"], r)
        self.assertEqual("", r["detail"])

    def test_session_start_matcher_is_not_judged_as_a_tool(self):
        self.settings({"SessionStart": [self.entry(self.real_hook(), "startup|resume")]})
        self.assertEqual("ok", self.row()["state"])

    def test_no_settings_is_unknown_not_clean(self):
        self.assertEqual("unknown", self.row()["state"])

    def test_it_is_in_the_default_run(self):
        self.settings({"Stop": [self.entry(self.real_hook())]})
        p = self.doctor("--json")
        names = [r["check"] for r in json.loads(p.stdout)["checks"]]
        self.assertIn("hooks-schema", names)


class AllowlistsShip(DoctorBase):
    def load(self, name):
        f = GT / "hooks" / name
        self.assertTrue(f.is_file(), "%s does not ship in hooks/" % name)
        return json.loads(f.read_text())

    def test_not_empty_and_cover_every_registered_event(self):
        ev = self.load("known_events.json")
        tools = self.load("known_tools.json")["tools"]
        self.assertTrue(ev["events"] and ev["tool_events"] and tools)
        self.assertTrue(set(ev["tool_events"]) <= set(ev["events"]))
        comp = load_module(SCRIPTS / "gt_components.py", "gt_components_for_schema")
        used = {r["event"] for r in comp.HOOK_REGISTRATIONS}
        for mj in glob.glob(str(REPO / "golden-thread-*" / "*" / "module.json")):
            for h in json.loads(open(mj).read()).get("hooks", []):
                used.add(h["event"])
        missing = used - set(ev["events"])
        self.assertFalse(missing, "events gt registers but the allowlist lacks: %s" % missing)
        for t in ("Bash", "Write", "Edit", "Read"):
            self.assertIn(t, tools)
