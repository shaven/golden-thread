"""guard_session_claims.sh / .py -- the PreToolUse hook for core_concurrent_session_claim.

Contract:
  * Input: the PreToolUse payload on stdin. Always exit 0. A deny is one JSON object
    with hookSpecificOutput.hookEventName == PreToolUse and permissionDecision deny.
    NO objection is NO output: never permissionDecision "allow", which Claude Code
    treats as "skip the user's permission prompt" (hooks reference, 2026-09-13).
  * Deny a Write/Edit/MultiEdit/NotebookEdit whose target is inside the vault when
    another LIVE session's file in Projects/golden-thread/sessions/ claims that path
    (exact file, or a claimed directory prefix).
  * Liveness: same MACHINE -> os.kill(pid, 0) decides; otherwise the last_execution
    heartbeat must be <= 30 minutes old. The caller's own session id never blocks it.
    "Same machine" is the `machine:` id against ~/.claude/golden-thread/machine-id
    (0.17.2); only a file with no id (older gt) is judged by its `host:` label.
  * FAIL OPEN: malformed input, other tools, no vault, target outside the vault, no
    sessions dir, python unavailable -> no output, exit 0.
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import unittest

from _harness import Sandbox, HOOKS, SCRIPTS, PYTHON, IS_WINDOWS

SESSIONS = "Projects/golden-thread/sessions"
TS_FMT = "%Y-%m-%d %H:%M:%S %Z"
# PINNED, never sampled. This was socket.gethostname() at import, compared against what
# the guard subprocess resolved later; on 2026-09-28 DHCP renamed the machine between the
# two and test_with_no_argument_it_still_denies_a_claimed_file got empty output. The race
# was real and so was the defect behind it (the guard keyed on the hostname), so the value
# the guard sees is now fixed by the harness rather than hoped to hold still.
HOST = "gt-test-host.lan"
MACHINE = "11111111-2222-4333-8444-555555555555"
OTHER_MACHINE = "99999999-8888-4777-8666-555555555555"


def local_stamp(minutes_ago=0):
    dt = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes_ago)
    # Windows spells the local %Z as a phrase ("Central Daylight Time"), which no gt ever
    # wrote: the zone-name heartbeat is a POSIX-era legacy form, and gt_session.py has
    # written a numeric %z since before native Windows existed. So on Windows a local
    # heartbeat is stamped the way gt writes one; the legacy zone-name cases below use
    # explicit zones whose %Z is an abbreviation on every platform.
    return dt.strftime("%Y-%m-%d %H:%M:%S %z" if IS_WINDOWS else TS_FMT).strip()


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
        self.pin_hostname(HOST)
        mid = self.home / ".claude" / "golden-thread" / "machine-id"
        mid.write_text(MACHINE + "\n", encoding="utf-8")

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
        self.target = self.vault / "Projects" / "alpha" / "research.json"

    def session(self, sid="other", claims=("Projects/alpha/research.json",), host="elsewhere",
                pid=None, last_execution=None, name=None, machine=None):
        fm = [f"session_id: {sid}", "task: testing the guard"]
        if machine is not None:
            fm.append(f"machine: {machine}")
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
        # Hold the Popen: on Windows its handle is what stops the dead pid being reused by
        # the next process started (harmless on POSIX).
        self.addCleanup(lambda: p)
        return p.pid

    # -- no registration (request 2026-09-29) --------------------------------------------
    def test_an_unregistered_session_is_warned_and_not_blocked(self):
        hso = self.guard(self.target, env={"CLAUDE_SESSION_ID": "me"})
        self.assertAllow(hso)
        self.assertIn("NOT registered", hso.get("additionalContext", ""))
        self.assertIn("Projects/alpha/research.json", hso["additionalContext"])
        self.assertIn("gt_session.py", hso["additionalContext"])

    def test_a_registered_session_hears_nothing(self):
        self.session(sid="me", claims=())
        self.assertEqual(self.guard(self.target, env={"CLAUDE_SESSION_ID": "me"}), {})

    def test_an_unknown_session_id_is_not_called_unregistered(self):
        self.assertEqual(self.guard(self.target), {}, "cannot tell must not read as unregistered")

    # -- deny -------------------------------------------------------------------------
    def test_live_pid_on_this_host_denies(self):
        self.session(host=HOST, pid=os.getpid())         # this test process is alive
        hso = self.guard(self.target)
        self.assertDeny(hso)
        reason = hso["permissionDecisionReason"]
        self.assertIn("Projects/alpha/research.json", reason)
        self.assertIn("session : other", reason)
        self.assertIn("testing the guard", reason)
        self.assertNotIn("pending/", reason, "claim refused => queue first, not pending/")
        self.assertIn("gt_write_queue.py", reason)
        self.assertIn("HOLDS the write while that claim is live", reason)

    def test_live_pid_wins_over_an_old_heartbeat(self):
        self.session(host=HOST, pid=os.getpid(), last_execution=local_stamp(600))
        self.assertDeny(self.guard(self.target), "a busy session stays live while its pid runs")

    # -- which machine: by id, not by hostname (2026-09-28) --------------------------------
    def test_a_renamed_machine_still_judges_its_own_pids(self):
        """THE BUG. A claim written here under the old hostname, by a session whose pid is
        running but whose heartbeat is 10 hours old (a busy session stays live while its pid
        runs). Keyed on the hostname, the rename un-judged the pid and the claim lapsed in
        silence. Keyed on the machine id, it still denies -- and the deny says so."""
        self.session(host="laptop.office.lan", machine=MACHINE, pid=os.getpid(),
                     last_execution=local_stamp(600))
        hso = self.guard(self.target)
        self.assertDeny(hso, "a hostname change disarmed this machine's own claim")
        reason = hso["permissionDecisionReason"]
        self.assertIn("laptop.office.lan", reason)
        self.assertIn(f"now calls itself '{HOST}'", reason,
                      "the rename must be reported, not silently absorbed")

    def test_another_machine_under_the_same_hostname_is_not_this_machine(self):
        """Same name, different id: its pid means nothing here. Recorded as a pid this
        machine sees DEAD, with a fresh heartbeat -- if the name were trusted the dead pid
        would release the claim; as another machine's, the heartbeat keeps it live."""
        self.session(host=HOST, machine=OTHER_MACHINE, pid=self.dead_pid(),
                     last_execution=local_stamp(1))
        self.assertDeny(self.guard(self.target),
                        "another machine's claim was judged by a pid on this one")

    def test_legacy_file_with_no_machine_id_keeps_its_claims_by_label(self):
        """Migration, not a flag day: a file written by gt <= 0.17.1 carries no machine id.
        While its host label still matches, it is this machine's exactly as before."""
        self.session(host=HOST, pid=os.getpid(), last_execution=local_stamp(600))
        self.assertDeny(self.guard(self.target))

    def test_a_pid_hosting_a_newer_session_does_not_keep_an_older_one_live(self):
        """A pid is a PROCESS, not a session: 2026-09-28, pid 91999 hosted several sessions
        in turn. A finished session whose file records the pid we are running inside, with
        a stale heartbeat, is not live; the same file with a fresh heartbeat still is."""
        self.session(sid="older", machine=MACHINE, host=HOST, pid=os.getpid(),
                     last_execution=local_stamp(120))
        env = {"CLAUDE_PID": str(os.getpid()), "CLAUDE_CODE_SESSION_ID": "newer"}
        self.assertAllow(self.guard(self.target, env=env),
                         "our own pid vouched for a session it no longer hosts")
        self.session(sid="older", machine=MACHINE, host=HOST, pid=os.getpid(),
                     last_execution=local_stamp(1))
        self.assertDeny(self.guard(self.target, env=env))
        # Control: a different process's running pid still decides on its own.
        self.session(sid="older", machine=MACHINE, host=HOST, pid=os.getpid(),
                     last_execution=local_stamp(120))
        self.assertDeny(self.guard(self.target, env={"CLAUDE_CODE_SESSION_ID": "newer"}))

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
        self.assertDeny(self.guard(self.vault / "Projects" / "alpha" / "deep" / "x.json"))
        self.assertAllow(self.guard(self.vault / "Projects" / "alphabet" / "x.json"),
                         "a claim on Projects/alpha must not cover Projects/alphabet")
        self.assertAllow(self.guard(self.vault / "Projects" / "alpha.json"))

    def test_target_reached_through_a_symlinked_vault_path_is_still_guarded(self):
        self.session(last_execution=local_stamp(1))
        alias = self.tmp / "vault-alias"
        alias.symlink_to(self.vault, target_is_directory=True)
        self.assertDeny(self.guard(alias / "Projects" / "alpha" / "research.json"))

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
                     "- `Projects/alpha/research.json`\n", encoding="utf-8")
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
        self.assertAllow(self.guard(self.vault / "Projects" / "alpha" / "design.json"))
        (self.vault / SESSIONS / "README.md").write_text(
            "---\nsession_id: readme\n---\n\n- `Projects/beta/x.json`\n", encoding="utf-8")
        self.assertAllow(self.guard(self.vault / "Projects" / "beta" / "x.json"),
                         "README.md in sessions/ is documentation, not a claim")

    def test_non_write_tools_are_allowed(self):
        self.session(last_execution=local_stamp(1))
        for tool in ("Read", "Bash", "Glob", ""):
            with self.subTest(tool=tool):
                self.assertAllow(self.guard(self.target, tool=tool))

    def test_target_outside_vault_is_allowed(self):
        self.session(claims=("Projects/alpha/research.json",), last_execution=local_stamp(1))
        self.assertAllow(self.guard(self.tmp / "elsewhere" / "Projects" / "alpha" / "research.json"))

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

    def test_the_python_guard_invoked_with_no_argument_fails_open(self):
        """Fail-open has to start at line one, before any try.

        `HERE = sys.argv[1]` was the first statement, so running the guard without the
        directory argument -- by hand, from a settings entry that lost it, from anything but
        the .sh wrapper -- raised IndexError and exited 1 with a traceback. Claude Code reports
        a non-zero PreToolUse hook as a failing hook on EVERY tool call, and the first thing
        anyone does with a guard that noisy is switch it off. Its own contract is that when it
        has nothing to say it says nothing and exits 0."""
        self.session(host=HOST, pid=os.getpid())         # a live claim exists, on another file
        payload = {"session_id": "caller", "hook_event_name": "PreToolUse",
                   "tool_name": "Write",
                   "tool_input": {"file_path": str(self.vault / "index.json"), "content": "x"}}
        proc = self.run_cmd([PYTHON, self.hooks / "guard_session_claims.py"],
                            input=json.dumps(payload))
        self.assertEqual(proc.returncode, 0,
                         "a guard must exit 0 with no argument\nstderr:\n%s" % proc.stderr)
        self.assertEqual(proc.stdout, "", "no objection prints nothing")
        self.assertNotIn("Traceback", proc.stderr)

    def test_with_no_argument_it_still_denies_a_claimed_file(self):
        """Failing open must not mean doing nothing: without argv[1] the guard falls back to
        its own directory, which is where install.sh puts gt_paths.py beside it."""
        self.session(host=HOST, pid=os.getpid())
        payload = {"session_id": "caller", "hook_event_name": "PreToolUse",
                   "tool_name": "Write",
                   "tool_input": {"file_path": str(self.target), "content": "x"}}
        proc = self.run_cmd([PYTHON, self.hooks / "guard_session_claims.py"],
                            input=json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("core_concurrent_session_claim", proc.stdout, proc.stdout)

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


class QueueFirstTest(GuardTestBase):
    """Core rule 1 since 0.17.11 (owner 2026-10-01): vault CONTENT is written only through the
    write queue. A direct Write/Edit to any vault .md outside gt's own trees is denied whether or
    not anyone claims it; the obvious shell writes are denied too; everything else is untouched."""

    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        (self.vault / SESSIONS).mkdir(parents=True)
        self.config(vault_path=str(self.vault))

    def bash(self, command, cwd=None):
        payload = {"session_id": "caller", "hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": command}, "cwd": str(cwd or self.tmp)}
        return self.guard_raw(json.dumps(payload))

    def assertQueueDeny(self, hso, msg=""):
        self.assertDeny(hso, msg)
        reason = hso["permissionDecisionReason"]
        self.assertIn("queue first", reason)
        self.assertIn("gt_write_queue.py", reason)
        self.assertIn("gt_broker.py drain", reason)

    def test_every_write_tool_on_vault_content_is_denied_unclaimed(self):
        for rel in ("Projects/alpha/research.md", "Projects/alpha/design.md", "INBOX.md",
                    "Knowledge/Some Page.md", "global-memory/x.md", "Daily Notes/2026-10-01.md",
                    "Projects/alpha/memory/note.md", "index.md"):
            for tool in ("Write", "Edit", "MultiEdit"):
                with self.subTest(rel=rel, tool=tool):
                    self.assertQueueDeny(self.guard(self.vault / rel, tool=tool))

    def test_the_reason_names_the_path_and_the_generated_file_tools(self):
        r = self.guard(self.vault / "Projects" / "alpha" / "research.md")["permissionDecisionReason"]
        self.assertIn("Projects/alpha/research.md", r)
        self.assertIn("gt_log.py add", r)
        self.assertIn("gt_adr.py allocate", r)

    def test_gt_owned_trees_and_non_markdown_are_not_queue_governed(self):
        for rel in ("Sources/a.md", "core-rules/core_x.md", ".obsidian/x.md",
                    "Projects/golden-thread/spool/queue/a.md", "Projects/golden-thread/sessions/s.md",
                    "Projects/golden-thread/tools/README.md", "Projects/alpha/data.json",
                    "Projects/alpha/tool.py"):
            with self.subTest(rel=rel):
                self.assertAllow(self.guard(self.vault / rel))

    def test_markdown_outside_the_vault_is_not_ours(self):
        self.assertAllow(self.guard(self.tmp / "elsewhere" / "notes.md"))

    def test_shell_writes_into_vault_content_are_denied(self):
        # Paths go into a SHELL command in the shell's own spelling: "/" on every platform
        # (identical on POSIX). An unquoted C:\Users\... is not a path to bash at all --
        # bash strips the backslashes, and so does the guard's shlex, correctly.
        t = (self.vault / "Projects" / "alpha" / "research.md").as_posix()
        for cmd in (f'echo x >> "{t}"', f"echo x > '{t}'", f"printf x>>{t}",
                    f'cat <<EOF > "{t}"\nx\nEOF', f'echo x | tee -a "{t}"',
                    f"sed -i '' 's/a/b/' '{t}'", f"cp /tmp/a.md '{t}'", f"mv /tmp/a.md {t}",
                    "echo x >> Projects/alpha/research.md"):
            with self.subTest(cmd=cmd):
                self.assertQueueDeny(self.bash(cmd, cwd=self.vault))

    def test_shell_writes_the_guard_cannot_resolve_or_that_land_elsewhere_are_allowed(self):
        """0.19.1 (request queue-guard-bash-false-positives): the guard read redirects off the raw
        string, so a quoted `>` was a redirect, `$VAR` was taken literally, and a `cd` earlier in
        the command was ignored -- each one blocked a scratch-file write as vault content."""
        s = (self.tmp / "scratch").as_posix()          # the shell's spelling, see above
        for cmd in (f'S={s}; cat > $S/src.md <<\'EOF\'\nx\nEOF',
                    'echo x > "$D/research.md"', "echo x > `pwd`/INBOX.md",
                    'printf "a > Projects/alpha/research.md"',
                    "echo 'see > INBOX.md for details'",
                    f"cd {s}; cat > INBOX.md", f"cd {s} && cat > INBOX.md",
                    f"cd {s} || exit; echo x >> Projects/alpha/research.md",
                    f"cd {s}\necho x > INBOX.md",
                    f"cd {s} && sed -i '' 's/a/b/' INBOX.md"):
            with self.subTest(cmd=cmd):
                self.assertAllow(self.bash(cmd, cwd=self.vault))

    def test_a_cd_into_the_vault_is_followed(self):
        v = self.vault.as_posix()                       # the shell's spelling, see above
        cmds = [f"cd {v}/Projects; cat > alpha/research.md",
                f"cd {v}/Projects && echo x >> alpha/research.md",
                f"cd {v} && cp /tmp/a.md INBOX.md",
                f'cd "{v}/Projects/alpha"; tee -a research.md < /tmp/x']
        if IS_WINDOWS:
            # Git Bash's own form of a drive path, /c/Users/..., is the same directory (0.20.0).
            msys = "/" + v[0].lower() + v[2:]
            cmds += [f"cd {msys}/Projects && echo x >> alpha/research.md",
                     f'echo x >> "{msys}/INBOX.md"']
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertQueueDeny(self.bash(cmd, cwd=self.tmp))

    def test_shell_reads_and_writes_elsewhere_are_allowed(self):
        t = self.vault / "Projects" / "alpha" / "research.md"
        for cmd in (f'cat "{t}"', f'grep -n x "{t}"', f'cp "{t}" /tmp/copy.md',
                    "echo x > /tmp/scratch.md", f'echo x > "{self.vault}/Projects/alpha/data.json"',
                    f'python3 gt_write_queue.py --vault "{self.vault}" --path Projects/alpha/research.md --op append --content-file /tmp/q.md',
                    'echo "unbalanced', "2>&1 >/dev/null ls"):
            with self.subTest(cmd=cmd):
                self.assertAllow(self.bash(cmd, cwd=self.tmp))


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
        target = self.vault / "Projects" / "golden-thread" / "state.json"
        self.gt_session("register", "--task", "guard e2e")
        self.gt_session("claim", "Projects/golden-thread/state.json")

        b = {"CLAUDE_CODE_SESSION_ID": "sess-B"}
        self.assertDeny(self.guard(target, env=b), "session B must not write A's claimed file")
        self.assertAllow(self.guard(target, env={"CLAUDE_CODE_SESSION_ID": "sess-A"}),
                         "session A may write its own claim")
        self.assertAllow(self.guard(self.vault / "index.json", env=b), "unclaimed file")

        self._stop_holder()
        self.assertAllow(self.guard(target, env=b), "A's process died: its claim is void")

    def test_release_clears_the_claim(self):
        target = self.vault / "index.json"
        self.gt_session("register", "--task", "guard e2e", "--files", "index.json")
        b = {"CLAUDE_CODE_SESSION_ID": "sess-B"}
        self.assertDeny(self.guard(target, env=b))
        self.gt_session("release")
        self.assertAllow(self.guard(target, env=b))


if __name__ == "__main__":
    unittest.main()
