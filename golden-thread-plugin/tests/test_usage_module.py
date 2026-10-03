"""gt-usage as a gt module: the plan-allowance meter.

The contract that matters here, and the one this file exists for:

  **Whether the meter is SHOWN and what it ADVISES are different questions.**

`usage_alert` chooses when the meter speaks. It must not change what the meter SAYS about
the numbers. Both shipped bugs in this module were that same mistake:

  0.1.1  the status line's context hint was gated on the display threshold, so under
         `always` (every threshold -1) it advised "cutting now is cheap" at 10% context.
  0.1.2  the session-start line did the same with "the weekly window is the one to
         watch", firing at 13% weekly -- in the file the 0.1.1 fix did not reach.

Neither was caught by reading the code; both were caught by looking at output. So the
question is asked here mechanically, of every surface, for every display level: change
ONLY the display level and the advice must not move.

Also pinned: module.json validity, absent-is-not-zero, a stale reading quoted with its
age rather than as current, and a status line that survives hostile input -- because it
runs on every render in front of the user and must never raise, block, or exit non-zero.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from _harness import IS_WINDOWS, Sandbox, REPO, PYTHON, latest_version_dir

USAGE = latest_version_dir(REPO / "golden-thread-usage")
METER = USAGE / "scripts" / "gt_usage.py"
BRIEF = USAGE / "scripts" / "gt_usage_brief.py"
SKILL = USAGE / "skills" / "gt-usage" / "SKILL.md"

LEVELS = ("early", "normal", "late", "always")
QUIET = {"five_hour": 2.0, "seven_day": 13.0}          # nothing near any ceiling
LOUD = {"five_hour": 40.0, "seven_day": 78.0}          # the weekly window genuinely is it

ADVICE = (
    "the weekly window is the one to watch",
    "the 5-hour window resets sooner",
    "cutting now is cheap",
)


def blob(limits, context=None, cache=None):
    out = {"rate_limits": {k: {"used_percentage": v} for k, v in limits.items()}}
    if context is not None:
        out["context_window"] = {"used_percentage": context}
    if cache is not None:
        out["prompt_cache"] = {"hit_ratio": cache, "warm": True}
    return json.dumps(out)


def advice_in(text):
    low = (text or "").lower()
    return {phrase for phrase in ADVICE if phrase in low}


class UsageModule(Sandbox):
    """Each test runs against a HOME of its own, so nothing touches the real install."""

    def setUp(self):
        super().setUp()
        self.gt = Path(self.home) / ".claude" / "golden-thread"
        (self.gt / "hooks").mkdir(parents=True, exist_ok=True)
        (self.gt / "usage").mkdir(parents=True, exist_ok=True)
        # gt_components.py must travel WITH gt_settings.py: the registry is loaded
        # from beside it, and its absence makes _module_settings() throw and
        # register nothing -- silently, because it returns on any exception.
        for name in ("gt_settings.py", "gt_paths.py", "gt_components.py"):
            src = REPO / "golden-thread" / os.environ.get("GT_TEST_VERSION", "") / "scripts" / name
            if not src.exists():
                src = latest_version_dir(REPO / "golden-thread") / "scripts" / name
            if src.exists():
                (self.gt / "hooks" / name).write_bytes(src.read_bytes())
        for src in (METER, BRIEF):
            (self.gt / "hooks" / src.name).write_bytes(src.read_bytes())
        # gt_settings registers a module's settings from the INSTALLED plugin caches
        # when it is not running inside a release tree. Without this the settings are
        # unknown here, every `set` is refused, and the tests skip while looking like
        # they ran -- which is how this file first reported three silent skips.
        cache = Path(self.home) / ".claude" / "plugins" / "cache" / "golden-thread-plugin"
        shutil.copytree(USAGE, cache / "gt-usage" / USAGE.name)
        # Settings are stored in the vault config, so a sandbox with no vault refuses
        # every write with "Run /gt:gt-init first" -- which reads as the module being
        # unknown when it is the vault that is missing.
        self.vault = self.make_vault()

    def senv(self):
        """Named senv, not env: Sandbox already carries an `env` dict attribute."""
        env = dict(os.environ, HOME=str(self.home))
        if IS_WINDOWS:
            # Windows Python's expanduser reads USERPROFILE: without it every tool run here
            # read the real ~/.claude, not the sandbox's (Sandbox.env does the same)
            env.update(USERPROFILE=str(self.home), PYTHONUTF8="1")
        return env

    def reading(self, limits, age_hours=0.0):
        row = dict(limits)
        row["ts"] = int(time.time() - age_hours * 3600)
        (self.gt / "usage" / "readings.jsonl").write_text(json.dumps(row) + "\n")

    def set_level(self, level):
        """Set through gt_settings, so a value it would reject cannot be smuggled in."""
        out = subprocess.run([PYTHON, str(self.gt / "hooks" / "gt_settings.py"),
                              "set", "usage_alert", level],
                             capture_output=True, text=True, env=self.senv())
        return out.returncode == 0

    def meter(self, payload):
        out = subprocess.run([PYTHON, str(self.gt / "hooks" / "gt_usage.py")],
                             input=payload, capture_output=True, text=True, env=self.senv())
        self.assertEqual(out.returncode, 0, "a status line must always exit 0: %s" % out.stderr)
        return out.stdout.strip()

    def brief(self):
        out = subprocess.run([PYTHON, str(self.gt / "hooks" / "gt_usage_brief.py")],
                             input="{}", capture_output=True, text=True, env=self.senv())
        self.assertEqual(out.returncode, 0, "a SessionStart hook must never block a start")
        raw = out.stdout.strip()
        if not raw:
            return ""
        try:
            return json.loads(raw).get("systemMessage", raw)
        except ValueError:
            return raw

    # -- the contract this file exists for -----------------------------------

    def test_display_level_does_not_change_the_advice_on_the_status_line(self):
        """Quiet numbers must draw no advice, however loudly the meter is set to speak."""
        for level in LEVELS:
            self.assertTrue(self.set_level(level),
                            "gt_settings refused %r -- the module registry is missing, "
                            "so this test would otherwise skip and read as a pass" % level)
            text = self.meter(blob(QUIET, context=60, cache=0.99))
            self.assertFalse(advice_in(text),
                             "%s: advice appeared for numbers that do not warrant it: %r"
                             % (level, text))

    def test_display_level_does_not_change_the_advice_at_session_start(self):
        """The 0.1.2 bug: 'the weekly window is the one to watch' at 13%."""
        self.reading(QUIET)
        for level in LEVELS:
            self.assertTrue(self.set_level(level),
                            "gt_settings refused %r -- the module registry is missing, "
                            "so this test would otherwise skip and read as a pass" % level)
            self.assertFalse(advice_in(self.brief()),
                             "%s: session-start line advised on quiet numbers" % level)

    def test_advice_still_appears_when_the_numbers_earn_it(self):
        """The fix must not be 'never advise'. Loud numbers must still be advised on."""
        self.reading(LOUD)
        for level in LEVELS:
            self.assertTrue(self.set_level(level),
                            "gt_settings refused %r -- the module registry is missing, "
                            "so this test would otherwise skip and read as a pass" % level)
            self.assertTrue(advice_in(self.brief()),
                            "%s: no advice on numbers that warrant it" % level)
        self.set_level("always")
        self.assertIn("cutting now is cheap", self.meter(blob(QUIET, context=91)).lower(),
                      "a context past its ceiling must still be advised on")

    def test_always_changes_visibility_and_only_visibility(self):
        for level in ("normal", "always"):
            self.set_level(level)
            shown = bool(self.meter(blob(QUIET, context=60)))
            self.assertEqual(shown, level == "always",
                             "%s: visibility is what the level chooses" % level)

    # -- honesty rules --------------------------------------------------------

    def test_a_window_the_plan_does_not_report_is_absent_not_zero(self):
        self.reading({"seven_day": 13.0})          # no five_hour, no spend
        self.set_level("always")
        out = subprocess.run([PYTHON, str(self.gt / "hooks" / "gt_usage.py"), "--now"],
                             capture_output=True, text=True, env=self.senv())
        self.assertIn("not reported on this plan", out.stdout)
        self.assertNotIn("5-hour allowance      0.0%", out.stdout)

    def test_a_stale_reading_is_quoted_with_its_age_not_as_current(self):
        self.reading(LOUD, age_hours=30)
        self.set_level("normal")
        text = self.brief()
        self.assertIn("hours old", text, "a stale reading must say so")
        self.assertFalse(advice_in(text), "stale numbers must not be advised on")

    def test_no_token_count_is_converted_into_a_percentage_of_an_allowance(self):
        """Undocumented conversion. Presenting it would be a guess dressed as measurement.

        Checked line by line, and reported by line number: asserting over a whole file
        prints the whole file on failure, which is how this test first drowned its own
        result in two scripts' source.
        """
        banned = ("tokens as a percentage", "% of your allowance",
                  "of the allowance used by", "allowance_pct")
        for path in (METER, BRIEF):
            for n, line_text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for phrase in banned:
                    self.assertNotIn(phrase, line_text,
                                     "%s:%d converts tokens into allowance" % (path.name, n))

    # -- a status line is a hostile place to run code -------------------------

    def test_hostile_input_never_breaks_a_render(self):
        for payload in ("", "   ", "not json", "[]", "null",
                        '{"rate_limits": null}', '{"rate_limits": {"five_hour": null}}',
                        '{"context_window": {"used_percentage": "high"}}'):
            out = subprocess.run([PYTHON, str(self.gt / "hooks" / "gt_usage.py")],
                                 input=payload, capture_output=True, text=True, env=self.senv())
            self.assertEqual(out.returncode, 0, "exit 0 for %r, got %d" % (payload, out.returncode))

    def test_a_reading_is_recorded_at_most_once_a_minute(self):
        self.set_level("normal")
        for _ in range(5):
            self.meter(blob(QUIET, context=50))
        rows = (self.gt / "usage" / "readings.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(rows), 1, "five renders in one second must record one reading")

    def test_recording_continues_while_the_display_is_off(self):
        subprocess.run([PYTHON, str(self.gt / "hooks" / "gt_settings.py"),
                        "set", "usage_meter", "off"], capture_output=True, env=self.senv())
        self.assertEqual(self.meter(blob(LOUD, context=95)), "",
                         "usage_meter=off shows nothing, even at a ceiling")
        self.assertTrue((self.gt / "usage" / "readings.jsonl").exists(),
                        "turning the display off must not stop the history")

    # -- the module itself ----------------------------------------------------

    def test_module_declares_what_it_ships(self):
        data = json.loads((USAGE / "module.json").read_text())
        self.assertEqual(data["name"], "usage")
        self.assertEqual(data["plugin"], "gt-usage")
        self.assertEqual(data["version"], USAGE.name)
        self.assertEqual(data["default"], "on")
        for script in data["scripts"]:
            self.assertTrue((USAGE / "scripts" / script).exists(), script)
        for skill in data["skills"]:
            self.assertTrue((USAGE / "skills" / skill / "SKILL.md").exists(), skill)
        keys = {s["key"] for s in data["settings"]}
        self.assertEqual(keys, {"usage_meter", "usage_alert"})
        for spec in data["settings"]:
            self.assertIn(spec["default"], spec["values"])
        alert = [s for s in data["settings"] if s["key"] == "usage_alert"][0]
        self.assertIn("always", alert["values"])

    def test_the_skill_states_what_the_meter_is_not(self):
        """The person most likely to over-read this is the one who just installed it."""
        text = SKILL.read_text(encoding="utf-8").lower()
        self.assertIn("not a bill", text)
        self.assertIn("undocumented", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
