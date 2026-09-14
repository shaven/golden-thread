"""guard_session_claims.sh / .py -- the PreToolUse hook for core_concurrent_session_claim.

Contract:
  * Input: the PreToolUse payload on stdin. Always exit 0. A deny is one JSON object
    with hookSpecificOutput.hookEventName == PreToolUse and permissionDecision deny.
    NO objection is NO output: never permissionDecision "allow", which Claude Code
    treats as "skip the user's permission prompt" (hooks reference, 2026-09-13).
  * Deny a Write/Edit/MultiEdit/NotebookEdit whose target is inside the vault when
    another LIVE session's file in Projects/golden-thread/sessions/ claims that path
    (exact file, or a claimed directory prefix).
  * Liveness: same host -> os.kill(pid, 0) decides; otherwise the last_execution
    heartbeat must be <= 30 minutes old. The caller's own session id never blocks it.
  * FAIL OPEN: malformed input, other tools, no vault, target outside the vault, no
    sessions dir, python unavailable -> no output, exit 0.
"""
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import unittest

from _harness import Sandbox, HOOKS, SCRIPTS, PYTHON

SESSIONS = "Projects/golden-thread/sessions"
TS_FMT = "%Y-%m-%d %H:%M:%S %Z"
HOST = socket.gethostname()


def local_stamp(minutes_ago=0):
    dt = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes_ago)
    return dt.strftime(TS_FMT).strip()


class GuardTestBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True)
        for f in HOOKS.iterdir():
            if f.is_file():
                shutil.copy2(f, self.hooks / f.name)
        # install.sh also copies gt_paths.py from scripts/ into this directory, and the
        # guard imports it at run time. Before 0.12.8 a stale duplicate in hooks/ made
        # this work by accident.
        shutil.copy2(SCRIPTS / "gt_paths.py", self.hooks / "gt_paths.py")
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def guard_raw(self, stdin, env=None):
        proc = self.sh(self.hooks / "guard_session_claims.sh", input=stdin, env=env)
        self.assertOk(proc, "the guard must always exit 0")
        if not proc.stdout.strip():
            return {}                    # no objection: the normal permission flow decides
        try:
            out = json.loads(proc.stdout)
        except ValueError as exc:
            self.fail(f"guard stdout is not one JSON object ({exc}):\n{proc.stdout!r}")
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        return hso

    def guard(self, target, tool="Write", env=None, key="file_path"):
        payload = {"session_id": "caller", "hook_event_name": "PreToolUse",
                   "tool_name": tool, "tool_input": {key: str(target), "content": "x"}}
        return self.guard_raw(json.dumps(payload), env=env)

    def assertAllow(self, hso, msg=""):
        """Allowed = exit 0 (checked in guard_raw) and NO permissionDecision at all."""
        self.assertNotIn("permissionDecision", hso,
                         f"{msg}\n{hso.get('permissionDecisionReason', '')}")

    def assertDeny(self, hso, msg=""):
        self.assertEqual(hso.get("permissionDecision"), "deny", msg)
        self.assertIn("core_concurrent_session_claim", hso["permissionDecisionReason"])


class GuardTest(GuardTestBase):
    """Hand-built vault: only what the guard reads (config + sessions dir)."""

    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        (self.vault / SESSIONS).mkdir(parents=True)
        self.config(vault_path=str(self.vault))
        self.target = self.vault / "Projects" / "alpha" / "research.md"

    def session(self, sid="other", claims=("Projects/alpha/research.md",), host="elsewhere",
                pid=None, last_execution=None, name=None):
        fm = [f"session_id: {sid}", "task: testing the guard"]
        if last_execution is not None:
            fm.append(f"last_execution: {last_execution}")
        if pid is not None:
            fm.append(f"pid: {pid}")
        fm.append(f"host: {host}")
        body = "# What this session has open\n\n" + "".join(f"- `{c}`\n" for c in claims)
        p = self.vault / SESSIONS / (name or f"{sid}_2026-09-11_1100.md")
        p.write_text("---\n" + "\n".join(fm) + "\n---\n\n" + body, encoding="utf-8")
        return p

    def dead_pid(self):
        p = subprocess.Popen([PYTHON, "-c", "pass"])
        p.wait()
        return p.pid

    # -- deny -------------------------------------------------------------------------
    def test_live_pid_on_this_host_denies(self):
        self.session(host=HOST, pid=os.getpid())         # this test process is alive
        hso = self.guard(self.target)
        self.assertDeny(hso)
        reason = hso["permissionDecisionReason"]
        self.assertIn("Projects/alpha/research.md", reason)
        self.assertIn("session : other", reason)
        self.assertIn("testing the guard", reason)
        self.assertIn("Projects/golden-thread/pending/", reason)

    def test_live_pid_wins_over_an_old_heartbeat(self):
        self.session(host=HOST, pid=os.getpid(), last_execution=local_stamp(600))
        self.assertDeny(self.guard(self.target), "a busy session stays live while its pid runs")

    def test_fresh_heartbeat_from_another_host_denies(self):
        self.session(last_execution=local_stamp(5))
        self.assertDeny(self.guard(self.target))

    def test_every_write_tool_is_guarded(self):
        self.session(last_execution=local_stamp(1))
        for tool in ("Write", "Edit", "MultiEdit"):
            with self.subTest(tool=tool):
                self.assertDeny(self.guard(self.target, tool=tool))
        nb = self.vault / "Projects" / "alpha" / "nb.ipynb"
        self.session(sid="nbowner", claims=("Projects/alpha/nb.ipynb",),
                     last_execution=local_stamp(1))
        self.assertDeny(self.guard(nb, tool="NotebookEdit", key="notebook_path"))

    def test_directory_claim_covers_files_beneath(self):
        self.session(claims=("Projects/alpha/",), last_execution=local_stamp(1))
        self.assertDeny(self.guard(self.target))
        self.assertDeny(self.guard(self.vault / "Projects" / "alpha" / "deep" / "x.md"))
        self.assertAllow(self.guard(self.vault / "Projects" / "alphabet" / "x.md"),
                         "a claim on Projects/alpha must not cover Projects/alphabet")
        self.assertAllow(self.guard(self.vault / "Projects" / "alpha.md"))

    def test_target_reached_through_a_symlinked_vault_path_is_still_guarded(self):
        self.session(last_execution=local_stamp(1))
        alias = self.tmp / "vault-alias"
        alias.symlink_to(self.vault, target_is_directory=True)
        self.assertDeny(self.guard(alias / "Projects" / "alpha" / "research.md"))

    # -- allow ------------------------------------------------------------------------
    def test_own_claim_never_blocks(self):
        self.session(sid="me-123", host=HOST, pid=os.getpid())
        for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID"):
            with self.subTest(var=var):
                self.assertAllow(self.guard(self.target, env={var: "me-123"}))
        self.assertDeny(self.guard(self.target, env={"CLAUDE_CODE_SESSION_ID": "someone-else"}))

    def test_session_id_falls_back_to_the_filename(self):
        p = self.vault / SESSIONS / "fromname_2026-09-11_1100.md"
        p.write_text(f"---\nhost: {HOST}\npid: {os.getpid()}\n---\n\n"
                     "- `Projects/alpha/research.md`\n", encoding="utf-8")
        self.assertAllow(self.guard(self.target, env={"CLAUDE_CODE_SESSION_ID": "fromname"}))
        self.assertDeny(self.guard(self.target))

    def test_dead_pid_on_this_host_is_ignored_even_with_fresh_heartbeat(self):
        self.session(host=HOST, pid=self.dead_pid(), last_execution=local_stamp(0))
        self.assertAllow(self.guard(self.target), "a demonstrably dead session holds nothing")

    def test_stale_missing_or_unreadable_heartbeat_is_ignored(self):
        cases = {"stale (2h)": local_stamp(120), "just past 30 min": local_stamp(31),
                 "unparseable": "yesterday-ish", "absent": None}
        for name, le in cases.items():
            with self.subTest(heartbeat=name):
                self.session(last_execution=le)
                self.assertAllow(self.guard(self.target))

    def test_unclaimed_file_and_readme_are_not_blocked(self):
        self.session(last_execution=local_stamp(1))
        self.assertAllow(self.guard(self.vault / "Projects" / "alpha" / "design.md"))
        (self.vault / SESSIONS / "README.md").write_text(
            "---\nsession_id: readme\n---\n\n- `Projects/beta/x.md`\n", encoding="utf-8")
        self.assertAllow(self.guard(self.vault / "Projects" / "beta" / "x.md"),
                         "README.md in sessions/ is documentation, not a claim")

    def test_non_write_tools_are_allowed(self):
        self.session(last_execution=local_stamp(1))
        for tool in ("Read", "Bash", "Glob", ""):
            with self.subTest(tool=tool):
                self.assertAllow(self.guard(self.target, tool=tool))

    def test_target_outside_vault_is_allowed(self):
        self.session(claims=("Projects/alpha/research.md",), last_execution=local_stamp(1))
        self.assertAllow(self.guard(self.tmp / "elsewhere" / "Projects" / "alpha" / "research.md"))

    def test_no_sessions_dir_or_no_vault_is_allowed(self):
        shutil.rmtree(self.vault / SESSIONS)
        self.assertAllow(self.guard(self.target))
        (self.home / ".claude" / "vault-config.json").unlink()
        self.assertAllow(self.guard(self.target))

    def test_fail_open_on_malformed_input(self):
        self.session(host=HOST, pid=os.getpid())
        for name, stdin in {"empty": "", "garbage": "{nope", "array": "[1,2]",
                            "no tool_input": json.dumps({"tool_name": "Write"}),
                            "tool_input not an object": json.dumps(
                                {"tool_name": "Write", "tool_input": "x"}),
                            "no file_path": json.dumps(
                                {"tool_name": "Write", "tool_input": {"content": "x"}})}.items():
            with self.subTest(case=name):
                self.assertAllow(self.guard_raw(stdin))

    def test_python_unavailable_allows(self):
        self.session(host=HOST, pid=os.getpid())
        stub = self.tmp / "stub-bin"
        stub.mkdir()
        (stub / "python3").write_text("#!/bin/sh\nexit 127\n")
        (stub / "python3").chmod(0o755)
        self.assertAllow(self.guard(self.target, env={"PATH": f"{stub}:/usr/bin:/bin"}))

    def test_no_objection_emits_no_permission_decision(self):
        """2026-09-13: the guard printed permissionDecision "allow" on every call it did
        not block, which skips the user's permission prompt. No objection = no output."""
        payload = {"session_id": "caller", "hook_event_name": "PreToolUse",
                   "tool_name": "Read", "tool_input": {"file_path": str(self.target)}}
        proc = self.sh(self.hooks / "guard_session_claims.sh", input=json.dumps(payload))
        self.assertOk(proc)
        self.assertEqual(proc.stdout, "", "no objection must print nothing")

    def test_broken_python_target_prints_nothing(self):
        (self.hooks / "guard_session_claims.py").write_text("raise SystemExit(3)\n")
        proc = self.sh(self.hooks / "guard_session_claims.sh", input="{}")
        self.assertOk(proc, "a broken guard must still exit 0")
        self.assertEqual(proc.stdout, "", "fail open is silent, never an allow")
        (self.hooks / "guard_session_claims.py").unlink()
        proc = self.sh(self.hooks / "guard_session_claims.sh", input="{}")
        self.assertOk(proc, "a missing guard must still exit 0")
        self.assertEqual(proc.stdout, "")

    # -- heartbeats written in another time zone -----------------------------------------
    def test_fresh_heartbeat_from_another_time_zone_still_denies(self):
        # A session on another host is judged by its heartbeat alone. The heartbeat is
        # written as local time with a zone NAME (gt_session.py TS_FMT '%Z'). A fresh
        # claim must stay live whatever zone the writer and the reader are in.
        utc_now = datetime.datetime.now(datetime.timezone.utc)
        la = datetime.timezone(datetime.timedelta(hours=-7), "PDT")
        cases = [
            # (reader TZ, heartbeat as the writer would have stamped it)
            ("Asia/Tokyo", utc_now.strftime(TS_FMT)),                  # '... UTC'
            ("America/Chicago", utc_now.astimezone(la).strftime(TS_FMT)),  # '... PDT'
        ]
        for reader_tz, stamp in cases:
            with self.subTest(reader=reader_tz, heartbeat=stamp):
                self.session(last_execution=stamp)
                hso = self.guard(self.target, env={"TZ": reader_tz})
                self.assertEqual(
                    hso["permissionDecision"], "deny",
                    f"DEFECT: a heartbeat stamped {stamp!r} (written seconds ago) was not "
                    f"treated as live by a reader in {reader_tz}. guard_session_claims.py "
                    "age_min() parses '%Z' with strptime, which discards the zone (tzinfo "
                    "stays None, so the stamp is re-read as the reader's local time) or "
                    "rejects zone names that are not the reader's own -- and an age of "
                    "None counts as not live. Another host's fresh claim is silently "
                    "ignored and the write it exists to prevent is allowed.")

    def test_same_zone_control_for_the_time_zone_case(self):
        # Control for the test above: same writer and reader zone works.
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(TS_FMT)
        self.session(last_execution=stamp)
        self.assertDeny(self.guard(self.target, env={"TZ": "UTC"}))


class GuardWithSessionToolTest(GuardTestBase):
    """End to end with the real vault and the real gt_session.py claim files."""

    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.tool = self.vault / "Projects" / "golden-thread" / "tools" / "gt_session.py"
        if not self.tool.is_file():
            self.skipTest("vault has no gt_session.py")
        self.holder = subprocess.Popen(["sleep", "60"])     # stands in for session A's CLI
        self.addCleanup(self._stop_holder)
        self.a_env = {"CLAUDE_CODE_SESSION_ID": "sess-A", "CLAUDE_PID": str(self.holder.pid)}

    def _stop_holder(self):
        if self.holder.poll() is None:
            self.holder.kill()
            self.holder.wait()

    def gt_session(self, *args):
        proc = self.py(self.tool, *args, env=self.a_env, cwd=self.vault)
        self.assertOk(proc, f"gt_session.py {' '.join(args)}")

    def test_claim_blocks_other_session_until_released_or_dead(self):
        target = self.vault / "Projects" / "golden-thread" / "README.md"
        self.gt_session("register", "--task", "guard e2e")
        self.gt_session("claim", "Projects/golden-thread/README.md")

        b = {"CLAUDE_CODE_SESSION_ID": "sess-B"}
        self.assertDeny(self.guard(target, env=b), "session B must not write A's claimed file")
        self.assertAllow(self.guard(target, env={"CLAUDE_CODE_SESSION_ID": "sess-A"}),
                         "session A may write its own claim")
        self.assertAllow(self.guard(self.vault / "index.md", env=b), "unclaimed file")

        self._stop_holder()
        self.assertAllow(self.guard(target, env=b), "A's process died: its claim is void")

    def test_release_clears_the_claim(self):
        target = self.vault / "index.md"
        self.gt_session("register", "--task", "guard e2e", "--files", "index.md")
        b = {"CLAUDE_CODE_SESSION_ID": "sess-B"}
        self.assertDeny(self.guard(target, env=b))
        self.gt_session("release")
        self.assertAllow(self.guard(target, env=b))


if __name__ == "__main__":
    unittest.main()
