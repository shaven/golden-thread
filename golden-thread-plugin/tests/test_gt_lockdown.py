"""gt lockdown (0.20.5): gt_lockdown.py, the four levels and the allow rules it merges into
~/.claude/settings.json.

The load-bearing assertions are about BOUNDARIES of what gt writes:
  * `very-secure` leaves settings.json byte-identical -- the default is exactly 0.20.4;
  * each level adds exactly its own rules, and the levels are strictly cumulative;
  * switching levels removes only rules gt added -- a user's own rule, even one identical to
    gt's, survives every switch and `remove`;
  * existing deny rules (gt_sandbox's or the user's) are never removed or reordered;
  * a backup of settings.json is written before any change; re-applying a level is a no-op;
  * every rule string is a well-formed Claude Code permission rule.
"""
import json
import os
import re
import unittest

from _harness import IS_WINDOWS, SCRIPTS, Sandbox, load_module

LOCKDOWN = SCRIPTS / "gt_lockdown.py"

RULE_RE = re.compile(r"^(Bash|Edit|Read|WebFetch|WebSearch)(\([^()]+\))?$")


class InProcess(Sandbox):
    def setUp(self):
        super().setUp()
        self._env_saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "GT_VAULT")}
        os.environ["HOME"] = str(self.home)
        if IS_WINDOWS:
            os.environ["USERPROFILE"] = str(self.home)
        os.environ.pop("GT_VAULT", None)
        self.ld = load_module(LOCKDOWN, "gt_lockdown_under_test")
        self.sp = self.home / ".claude" / "settings.json"

    def tearDown(self):
        for k, v in self._env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def write_settings(self, d):
        self.sp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")

    def settings(self):
        return json.loads(self.sp.read_text(encoding="utf-8")) if self.sp.exists() else {}

    def allow(self):
        return (self.settings().get("permissions") or {}).get("allow") or []

    def backups(self):
        d = self.home / ".claude" / "golden-thread" / "backups"
        return sorted(p.name for p in d.iterdir()) if d.exists() else []


USER_SETTINGS = {
    "model": "opus",
    "permissions": {
        "allow": ["Bash(make deploy:*)", "Bash(git status:*)"],
        "deny": ["Read(//secret/**)", "Edit(//**/.claude/settings.json)"],
    },
}


class Levels(InProcess):
    def test_four_levels_in_order(self):
        self.assertEqual(self.ld.LEVELS,
                         ("very-secure", "mostly-secure", "partly-secure", "insecure"))
        self.assertEqual(self.ld.DEFAULT, "very-secure")

    def test_very_secure_adds_nothing(self):
        self.assertEqual(self.ld.rules_for("very-secure"), {"allow": [], "deny": []})

    def test_levels_are_strictly_cumulative(self):
        prev = set()
        for lvl in self.ld.LEVELS[1:]:
            cur = set(self.ld.rules_for(lvl)["allow"])
            self.assertTrue(prev < cur, "%s must add to the level below it" % lvl)
            prev = cur

    def test_every_rule_is_well_formed(self):
        for lvl in self.ld.LEVELS:
            r = self.ld.rules_for(lvl)
            for rule in r["allow"] + r["deny"]:
                self.assertRegex(rule, RULE_RE, "%s: %r" % (lvl, rule))

    def test_credential_reads_stay_denied_at_every_loosened_level(self):
        for lvl in self.ld.LEVELS[1:]:
            deny = self.ld.rules_for(lvl)["deny"]
            for must in ("Read(~/.ssh/**)", "Read(~/.claude/.credentials.json)"):
                self.assertIn(must, deny, lvl)

    def test_mostly_secure_has_no_network_or_push(self):
        allow = " ".join(self.ld.rules_for("mostly-secure")["allow"])
        for word in ("ssh", "scp", "curl", "wget", "git push", "install", "WebFetch"):
            self.assertNotIn(word, allow)


class Apply(InProcess):
    def test_a_very_secure_is_byte_identical(self):
        self.write_settings(USER_SETTINGS)
        before = self.sp.read_bytes()
        self.ld.apply("very-secure")
        self.assertEqual(self.sp.read_bytes(), before)
        self.assertEqual(self.backups(), [], "nothing changed, so nothing to back up")

    def test_b_each_level_adds_exactly_its_rules(self):
        for lvl in self.ld.LEVELS[1:]:
            self.write_settings({})
            self.ld.remove()
            self.ld.apply(lvl)
            want = self.ld.rules_for(lvl)
            self.assertEqual(sorted(self.allow()), sorted(want["allow"]), lvl)
            self.assertEqual(sorted(self.settings()["permissions"]["deny"]),
                             sorted(want["deny"]), lvl)

    def test_c_switching_removes_only_gt_rules(self):
        self.write_settings(USER_SETTINGS)
        self.ld.apply("partly-secure")
        self.ld.apply("mostly-secure")
        allow = self.allow()
        self.assertIn("Bash(make deploy:*)", allow, "the user's own rule survives")
        self.assertIn("Bash(git status:*)", allow,
                      "a user rule identical to one of gt's survives the switch")
        for rule in set(self.ld.rules_for("partly-secure")["allow"]) - \
                set(self.ld.rules_for("mostly-secure")["allow"]):
            self.assertNotIn(rule, allow, "partly-only rule left behind: %s" % rule)
        self.ld.remove()
        s = self.settings()
        self.assertEqual(s["permissions"]["allow"], ["Bash(make deploy:*)", "Bash(git status:*)"])
        self.assertEqual(s["permissions"]["deny"], USER_SETTINGS["permissions"]["deny"])
        self.assertEqual(s["model"], "opus")

    def test_d_existing_deny_rules_untouched(self):
        self.write_settings(USER_SETTINGS)
        for lvl in self.ld.LEVELS:
            self.ld.apply(lvl)
            deny = (self.settings().get("permissions") or {}).get("deny") or []
            self.assertEqual(deny[:2], USER_SETTINGS["permissions"]["deny"], lvl)
        self.ld.remove()
        self.assertEqual(self.settings(), USER_SETTINGS)

    def test_e_backup_before_change(self):
        self.write_settings(USER_SETTINGS)
        self.ld.apply("mostly-secure")
        b = self.backups()
        self.assertEqual(len(b), 1)
        self.assertTrue(b[0].startswith("settings.json.") and b[0].endswith(".lockdown"), b[0])
        saved = json.loads((self.home / ".claude" / "golden-thread" / "backups" / b[0])
                           .read_text(encoding="utf-8"))
        self.assertEqual(saved, USER_SETTINGS)

    def test_f_reapply_is_a_noop(self):
        self.write_settings(USER_SETTINGS)
        self.ld.apply("mostly-secure")
        once = self.sp.read_bytes()
        rep = self.ld.apply("mostly-secure")
        self.assertEqual(self.sp.read_bytes(), once)
        self.assertEqual(rep["changes"], [])

    def test_g_current_level_and_log(self):
        self.assertEqual(self.ld.current(), "very-secure")
        self.ld.apply("insecure")
        self.assertEqual(self.ld.current(), "insecure")
        log = self.home / ".claude" / "golden-thread" / "lockdown" / "log.jsonl"
        lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines[-1]["level"], "insecure")
        self.assertEqual(lines[-1]["from"], "very-secure")

    def test_h_unknown_level_refused(self):
        with self.assertRaises(ValueError):
            self.ld.apply("mostly")

    def test_i_invalid_json_never_overwritten(self):
        self.sp.write_text("{not json", encoding="utf-8")
        with self.assertRaises(Exception):
            self.ld.apply("mostly-secure")
        self.assertEqual(self.sp.read_text(encoding="utf-8"), "{not json")


class Table(InProcess):
    def test_markdown_table_names_every_level(self):
        md = self.ld.table_markdown()
        for label in ("very secure", "mostly secure", "partly secure", "insecure"):
            self.assertIn("**%s**" % label, md)
        self.assertTrue(md.startswith("| Level |"))


if __name__ == "__main__":
    unittest.main()
