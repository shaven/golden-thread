"""gt_surface.py — what a previous session left behind, put in front of the next one.

The feature exists because gt WROTE handoffs, state files and handoff tasks and nothing at
SessionStart read them. So the load-bearing tests are the ones that assert a thing reaches the
output, that it reaches it ONCE where once is the design (an alarm repeated every session is
one that gets scrolled past), and that the quiet case says it is quiet rather than printing
nothing -- silence reads the same as a hook that never ran.
"""
import datetime
import json
import os
import time
import unittest

from _harness import Sandbox, SCRIPTS

SURFACE = SCRIPTS / "gt_surface.py"
TODAY = "2026-09-28"


class SurfaceBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        (self.vault / "Projects" / "alpha").mkdir(parents=True)
        (self.vault / "Projects" / "alpha" / "README.md").write_text("# alpha\n\n## Tasks\n")
        self.config(vault_path=str(self.vault))

    def deadlines(self, *rows, header="| item | category | due | see |"):
        body = [header, "|---|---|---|---|"] + list(rows)
        (self.vault / "deadlines.md").write_text("# Deadlines\n\n" + "\n".join(body) + "\n")

    def run_check(self, *args, stdin=None, today=TODAY):
        return self.py(SURFACE, "check", *args, input=stdin, env={"GT_TODAY": today})

    def hook(self, source="startup"):
        p = self.run_check("--hook", stdin=json.dumps({"source": source}))
        self.assertOk(p)
        return json.loads(p.stdout)

    def handoff(self, name="2026-09-28-handoff.md", age_days=0, project="alpha"):
        d = self.vault / "Projects" / project / "handoff"
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_text("# Handoff\n")
        t = time.time() - age_days * 86400
        os.utime(f, (t, t))
        return f

    def state_file(self, name="state-2026-09-28T111058.md", body="## Task in hand\nship 0.17.2\n"):
        d = self.home / ".claude" / "golden-thread" / "state"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(body)
        return d / name


class MustDo(SurfaceBase):
    def test_overdue_and_upcoming_render_one_block_most_urgent_first(self):
        self.deadlines("| Decide the execution ladder | decision | 2026-10-04 | [[ladder]] |",
                       "| Rotate relay credentials | rotation | 2026-09-13 | [[pending-rotations]] |")
        out = self.run_check().stdout
        self.assertEqual(out.count("MUST DO:"), 1, out)
        self.assertIn("1 overdue, 1 due within 14d", out)
        self.assertIn("OVERDUE 15d", out)
        self.assertIn("due in 6d", out)
        self.assertLess(out.index("Rotate relay"), out.index("Decide the execution"),
                        "overdue must be listed above upcoming")
        # Severity is visible without reading the words: red for overdue, amber for ahead.
        self.assertIn("\U0001F534 OVERDUE", out)
        self.assertIn("\U0001F7E1 due in", out)

    def test_ages_are_computed_at_run_time_not_stored(self):
        self.deadlines("| Rotate relay credentials | rotation | 2026-09-13 | |")
        self.assertIn("OVERDUE 15d", self.run_check(today="2026-09-28").stdout)
        self.assertIn("OVERDUE 20d", self.run_check(today="2026-10-03").stdout)

    def test_no_rows_and_no_file_print_no_block_but_say_clean(self):
        for setup in (lambda: None, lambda: self.deadlines()):
            setup()
            out = self.run_check().stdout
            self.assertNotIn("MUST DO:", out)
            self.assertIn("nothing waiting", out,
                          "a clean run must say so: silence reads as a hook that never ran")

    def test_a_malformed_row_is_skipped_and_the_rest_still_report(self):
        self.deadlines("| Rotate relay credentials | rotation | someday | |",
                       "| Decide the ladder | decision | 2026-10-01 | |")
        out = self.run_check().stdout
        self.assertIn("Decide the ladder", out)
        self.assertNotIn("Rotate relay", out)
        self.assertIn("1 row in deadlines.md could not be read", out)

    def test_a_credential_shaped_label_is_never_printed(self):
        # Assembled at run time: a literal here is a credential-shaped assignment, and the
        # release gate's own gt_secrets scan (rightly) refuses a tree that carries one.
        secret = "".join(("hunter2", "Xq9vLr7", "TzP4mKw8", "NbY3cJ"))
        self.deadlines("| rotate pass=%s now | rotation | 2026-09-20 | |" % secret,
                       "| rotate %s | rotation | 2026-09-21 | |" % secret)
        for out in (self.run_check().stdout, json.dumps(self.hook())):
            self.assertNotIn(secret, out)
            self.assertIn("label withheld", out)

    def test_a_label_naming_a_credential_is_not_one(self):
        """Both rows were withheld on the first run against the real vault: `pass=` with no
        value after it, and a filename like handoff-ladder-2026-09-28. An alarm that hides
        its own item is an alarm that says nothing."""
        self.deadlines("| Rotate monitor-server `pass=` (shquote) | rotation | 2026-09-13 | |",
                       "| Ladder decision | decision | 2026-10-04 | [[handoff-ladder-2026-09-28]] |")
        out = self.run_check().stdout
        self.assertNotIn("withheld", out)
        self.assertIn("Rotate monitor-server `pass=`", out)
        self.assertIn("[[handoff-ladder-2026-09-28]]", out)

    def test_far_future_items_are_counted_not_listed(self):
        self.deadlines("| Renew the domain | renewal | 2027-01-01 | |")
        out = self.run_check().stdout
        self.assertNotIn("Renew the domain", out)
        self.assertNotIn("MUST DO:", out, "nothing near means no alarm")

    def test_it_runs_whichever_project_the_session_opened(self):
        """The owner's requirement: 'no matter what I opened it would alert me'. The vault
        comes from config, never from the working directory."""
        self.deadlines("| Rotate relay credentials | rotation | 2026-09-13 | |")
        elsewhere = self.tmp / "some-other-repo"
        elsewhere.mkdir()
        p = self.py(SURFACE, "check", cwd=elsewhere, env={"GT_TODAY": TODAY})
        self.assertIn("OVERDUE 15d", p.stdout)


class Handoffs(SurfaceBase):
    """Owner ruling 2026-09-28: an unhandled handoff keeps coming back until it is handled or
    deferred to a date -- 'shown once' let one scrolled past be as good as unwritten."""

    def test_an_open_handoff_is_shown_every_session_until_handled(self):
        self.handoff()
        for _ in range(3):
            self.assertIn("Projects/alpha/handoff/2026-09-28-handoff.md", self.run_check().stdout)
        self.py(SCRIPTS / "gt_handoff_status.py", "mark",
                "Projects/alpha/handoff/2026-09-28-handoff.md", "--vault", self.vault,
                "--status", "handled", "--reason", "done", env={"GT_TODAY": TODAY})
        self.assertNotIn("2026-09-28-handoff.md", self.run_check().stdout)

    def test_a_deferred_handoff_is_hidden_until_its_date(self):
        f = self.handoff()
        f.write_text("---\nstatus: deferred\nuntil: 2026-10-05\n---\n# Handoff\n")
        self.assertNotIn("handoff.md", self.run_check(today="2026-10-04").stdout)
        out = self.run_check(today="2026-10-05").stdout
        self.assertIn("handoff.md", out)
        self.assertIn("deferral ended", out)

    def test_it_never_shows_the_body(self):
        f = self.handoff()
        f.write_text("---\nstatus: open\n---\n# Handoff\nTHE-SECRET-PLAN is in here\n")
        d = self.hook()
        self.assertNotIn("THE-SECRET-PLAN", json.dumps(d))

    def test_an_old_handoff_without_status_is_history_not_news(self):
        self.handoff(age_days=10)
        self.assertNotIn("handoff.md", self.run_check().stdout)

    def test_a_hand_written_handoff_beside_the_readme_is_found(self):
        f = self.vault / "Projects" / "alpha" / "handoff-ladder-2026-09-28.md"
        f.write_text("# by hand\n")
        self.assertIn("handoff-ladder-2026-09-28.md", self.run_check().stdout)

    def test_project_mode_keeps_them_off_the_start_and_on_gt_open(self):
        self.config(vault_path=str(self.vault), handoff_surface="project")
        self.handoff()
        self.assertNotIn("handoff.md", self.run_check().stdout)
        p = self.py(SURFACE, "handoffs", "--project", "alpha", env={"GT_TODAY": TODAY})
        self.assertIn("Projects/alpha/handoff/2026-09-28-handoff.md", p.stdout)
        p = self.py(SURFACE, "handoffs", "--project", "beta", env={"GT_TODAY": TODAY})
        self.assertNotIn("handoff.md", p.stdout)

    def test_manual_mode_shows_them_nowhere_but_the_handle_command(self):
        self.config(vault_path=str(self.vault), handoff_surface="manual")
        self.handoff()
        self.assertNotIn("handoff.md", self.run_check().stdout)
        p = self.py(SURFACE, "handoffs", "--project", "alpha", env={"GT_TODAY": TODAY})
        self.assertEqual(p.stdout.strip(), "")
        p = self.py(SCRIPTS / "gt_handoff_status.py", "list", "--vault", self.vault,
                    env={"GT_TODAY": TODAY})
        self.assertIn("2026-09-28-handoff.md", p.stdout)


class StateFiles(SurfaceBase):
    def test_a_state_file_is_named_once(self):
        f = self.state_file()
        self.assertIn(str(f), self.run_check().stdout)
        self.assertNotIn("SESSION STATE", self.run_check().stdout)

    def test_after_a_compaction_the_state_is_handed_to_the_model(self):
        self.state_file(body="## Decisions already made\nuse machine-id, not hostname\n")
        d = self.hook(source="compact")
        ctx = d["hookSpecificOutput"]["additionalContext"]
        self.assertIn("use machine-id, not hostname", ctx)
        self.assertNotIn("use machine-id", d["systemMessage"], "the terminal gets the pointer only")

    def test_on_a_plain_startup_only_the_pointer_is_given(self):
        self.state_file(body="## Decisions already made\nsecret-plan\n")
        d = self.hook(source="startup")
        self.assertNotIn("secret-plan", d["hookSpecificOutput"]["additionalContext"])


class Delivery(SurfaceBase):
    def test_hook_output_reaches_the_user_and_the_model(self):
        self.deadlines("| Rotate relay credentials | rotation | 2026-09-13 | |")
        d = self.hook()
        self.assertIn("MUST DO", d["systemMessage"])
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("MUST DO", d["hookSpecificOutput"]["additionalContext"])

    def test_the_setting_turns_it_off(self):
        self.config(vault_path=str(self.vault), surface="off")
        self.deadlines("| Rotate relay credentials | rotation | 2026-09-13 | |")
        p = self.run_check()
        self.assertOk(p)
        self.assertEqual(p.stdout.strip(), "")

    def test_it_is_never_fatal(self):
        self.handoff()
        self.state_file()
        gt = self.home / ".claude" / "golden-thread"
        (gt / "surface").write_text("a file where a directory should be")
        p = self.run_check("--hook", stdin="not json")
        self.assertOk(p)
        self.assertIn("handoff", json.loads(p.stdout)["systemMessage"].lower())

    def test_it_is_registered_as_a_session_start_hook(self):
        from _harness import load_module
        comp = load_module(SCRIPTS / "gt_components.py", "gt_components_surface")
        self.assertIn("gt_surface.py", comp.HOOK_DIR_SCRIPTS)
        self.assertIn("gt_handoff_status.py", comp.HOOK_DIR_SCRIPTS,
                      "gt_surface imports it from the hooks dir")
        self.assertTrue(any(r["script"] == "gt_surface.py" and r["event"] == "SessionStart"
                            for r in comp.HOOK_REGISTRATIONS))


if __name__ == "__main__":
    unittest.main()
