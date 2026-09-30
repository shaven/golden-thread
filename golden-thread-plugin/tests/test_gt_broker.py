"""gt_write_queue.py + gt_broker.py -- queued vault writes from concurrent agents.

One test class per acceptance criterion of the 2026-09-30 feature request, adapted to the
lead's corrections: decisions are logged to spool/broker/ (never spool/log/, which gt_log.py
owns), the queue lives under Projects/golden-thread/spool/queue/, draining is an explicit
command, conflicts become tasks through the vault's own gt_task.py, and a target another LIVE
session has claimed is left queued (Core rule 1).

The load-bearing assertions are about what is NOT written: a contradiction lands nowhere, a
farmed result never edits existing text, a claimed file is not touched.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

from _harness import Sandbox, SCRIPTS, TOOLS, FARM, PYTHON

QUEUE = SCRIPTS / "gt_write_queue.py"
BROKER = SCRIPTS / "gt_broker.py"
RESEARCH = "Projects/quokka/research.md"
RESEARCH_TEXT = ("# quokka research\n\n## Findings\n\n- the first finding\n\n"
                 "## Architecture\n\nThe service is described here.\n\n## Open questions\n\n"
                 "- none yet\n")


class BrokerBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        gt = self.vault / "Projects" / "golden-thread"
        (gt / "tools").mkdir(parents=True)
        (gt / "sessions").mkdir()
        (gt / "README.md").write_text("# golden-thread\n\n## Tasks\n")
        for tool in ("gt_task.py", "gt_tasks.py", "gt_session.py"):
            shutil.copy(TOOLS / tool, gt / "tools" / tool)
        q = self.vault / "Projects" / "quokka"
        q.mkdir(parents=True)
        (q / "README.md").write_text("# quokka\n\n## Tasks\n")
        (q / "research.md").write_text(RESEARCH_TEXT)
        self.queue_dir = gt / "spool" / "queue"
        self.broker_dir = gt / "spool" / "broker"

    # -- driving the tools
    def submit(self, content, op="append", path=RESEARCH, section=None, session="sess-a",
               origin=None, expect=0):
        args = ["--vault", self.vault, "--path", path, "--op", op, "--content", content,
                "--session", session]
        if section:
            args += ["--section", section]
        if origin:
            args += ["--origin", origin]
        p = self.py(QUEUE, *args)
        self.assertEqual(p.returncode, expect, p.stdout + p.stderr)
        return p

    def drain(self, *extra, expect=None):
        p = self.py(BROKER, "drain", "--vault", self.vault, *extra)
        if expect is not None:
            self.assertEqual(p.returncode, expect, p.stdout + p.stderr)
        return p

    def queued(self):
        if not self.queue_dir.is_dir():
            return []
        return sorted(p for p in self.queue_dir.iterdir()
                      if p.suffix == ".json" and not p.name.startswith("."))

    def research(self):
        return (self.vault / RESEARCH).read_text()

    def section(self, name):
        text = self.research()
        m = re.search(r"^## %s\n(.*?)(?=^## |\Z)" % re.escape(name), text, re.S | re.M)
        return m.group(1) if m else None

    def log_rows(self):
        rows = []
        for f in sorted(self.broker_dir.glob("log-*.jsonl")):
            rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        return rows


class C1QueueDeposit(BrokerBase):
    def test_a_correctly_formed_request_file_appears_and_the_target_is_untouched(self):
        self.submit("test finding", section="Findings")
        files = self.queued()
        self.assertEqual(len(files), 1)
        req = json.loads(files[0].read_text())
        self.assertEqual(req["schema"], 1)
        self.assertEqual(req["path"], RESEARCH)
        self.assertEqual(req["op"], "append")
        self.assertEqual(req["section"], "Findings")
        self.assertEqual(req["content"], "test finding")
        self.assertEqual(req["session"], "sess-a")
        self.assertEqual(req["origin"], "session")
        self.assertTrue(files[0].name.startswith(req["submitted"][:4]))
        self.assertIn("-sess-a-", files[0].name, "file is named <timestamp>-<session>-<target>")
        self.assertEqual(self.research(), RESEARCH_TEXT, "submitting must not write the target")

    def test_replace_section_records_the_base_it_would_replace(self):
        self.submit("new body", op="replace-section", section="Architecture")
        req = json.loads(self.queued()[0].read_text())
        self.assertRegex(req["base_sha256"] or "", r"^[0-9a-f]{64}$")

    def test_generated_immutable_and_outside_paths_are_refused(self):
        for bad in ("log.md", "TASKS.md", "Projects/quokka/decisions.md", "Sources/x.md",
                    "../outside.md", "/etc/x.md", "Projects/quokka/notes.txt",
                    "Projects/golden-thread/spool/log/x.md", "Projects/.hidden/x.md"):
            self.submit("x", path=bad, expect=1)
        self.assertEqual(self.queued(), [])

    def test_an_extension_origin_does_not_exist(self):
        p = self.py(QUEUE, "--vault", self.vault, "--path", RESEARCH, "--op", "append",
                    "--content", "x", "--origin", "extension")
        self.assertEqual(p.returncode, 2, "ADR-8: an extension has no write path")

    def test_dry_run_queues_nothing(self):
        p = self.py(QUEUE, "--vault", self.vault, "--path", RESEARCH, "--op", "append",
                    "--content", "x", "--dry-run")
        self.assertOk(p)
        self.assertIn("would queue", p.stdout)
        self.assertEqual(self.queued(), [])


class C2DrainApplies(BrokerBase):
    def test_two_writes_to_different_sections_both_land_and_the_queue_empties(self):
        self.submit("- finding from kestrel", section="Findings", session="kestrel")
        self.submit("- does it scale?", section="Open questions", session="wombat")
        self.drain(expect=0)
        self.assertIn("- finding from kestrel", self.section("Findings"))
        self.assertIn("- does it scale?", self.section("Open questions"))
        self.assertIn("- the first finding", self.section("Findings"), "existing text kept")
        self.assertEqual(self.queued(), [])

    def test_requests_apply_in_timestamp_order_not_file_order(self):
        self.submit("- written second", section="Findings", session="zzz")
        self.submit("- written third", section="Findings", session="aaa")
        self.drain(expect=0)
        body = self.section("Findings")
        self.assertLess(body.index("written second"), body.index("written third"))

    def test_a_create_makes_a_new_file(self):
        self.submit("# wombat notes\n\nbody\n", op="create", path="Projects/quokka/wombat.md")
        self.drain(expect=0)
        self.assertEqual((self.vault / "Projects/quokka/wombat.md").read_text(),
                         "# wombat notes\n\nbody\n")

    def test_one_sessions_replace_section_lands(self):
        self.submit("The service is stateless.", op="replace-section", section="Architecture")
        self.drain(expect=0)
        self.assertIn("The service is stateless.", self.section("Architecture"))
        self.assertNotIn("described here", self.section("Architecture"))
        self.assertIn("- none yet", self.section("Open questions"), "the next section survives")

    def test_a_restated_append_is_deduplicated_not_doubled(self):
        self.submit("- the first finding.", section="Findings")
        self.drain(expect=0)
        self.assertEqual(self.section("Findings").count("first finding"), 1)
        self.assertEqual(self.log_rows()[-1]["decision"], "deduplicate")

    def test_dry_run_writes_logs_and_removes_nothing(self):
        self.submit("- dry", section="Findings")
        p = self.drain("--dry-run", expect=0)
        self.assertIn("would apply", p.stdout)
        self.assertEqual(self.research(), RESEARCH_TEXT)
        self.assertEqual(len(self.queued()), 1)
        self.assertFalse(self.broker_dir.exists())


class C3HardConflictBecomesATask(BrokerBase):
    def test_contradictory_replaces_land_nowhere_and_raise_one_conflict_task(self):
        self.submit("The service is stateless.", op="replace-section", section="Architecture",
                    session="sess-a")
        self.submit("The service is stateful.", op="replace-section", section="Architecture",
                    session="sess-b")
        self.drain(expect=0)
        arch = self.section("Architecture")
        self.assertNotIn("stateless", arch)
        self.assertNotIn("stateful", arch)
        self.assertIn("described here", arch, "the original must survive a conflict")
        readme = (self.vault / "Projects/quokka/README.md").read_text()
        task = [ln for ln in readme.splitlines() if "#conflict" in ln]
        self.assertEqual(len(task), 1, readme)
        self.assertIn("[p:: 1]", task[0])
        self.assertIn("[waiting:: user]", task[0])
        ref = re.search(r"\[ref:: ([^\]]+)\]", task[0]).group(1)
        self.assertTrue(ref.startswith("Projects/golden-thread/spool/broker/conflicts/"), ref)
        self.assertNotIn("stateless", task[0], "content goes in the conflict file, not the task")
        conflict = (self.vault / ref).read_text()
        self.assertIn("The service is stateless.", conflict)
        self.assertIn("The service is stateful.", conflict)
        self.assertIn("sess-a", conflict)
        self.assertIn("sess-b", conflict)
        self.assertEqual(self.queued(), [])

    def test_a_strict_superset_wins_without_a_task(self):
        self.submit("line one", op="replace-section", section="Architecture", session="sess-a")
        self.submit("line one\nline two", op="replace-section", section="Architecture",
                    session="sess-b")
        self.drain(expect=0)
        self.assertIn("line two", self.section("Architecture"))
        self.assertNotIn("#conflict", (self.vault / "Projects/quokka/README.md").read_text())

    def test_a_replace_of_a_section_edited_since_is_escalated(self):
        self.submit("my version", op="replace-section", section="Architecture")
        p = self.vault / RESEARCH
        p.write_text(p.read_text().replace("described here", "edited by the owner meanwhile"))
        self.drain(expect=0)
        self.assertIn("edited by the owner meanwhile", self.section("Architecture"))
        self.assertIn("#conflict", (self.vault / "Projects/quokka/README.md").read_text())

    def test_design_md_is_always_escalated(self):
        (self.vault / "Projects/quokka/design.md").write_text("# design\n")
        self.submit("- a change", path="Projects/quokka/design.md")
        self.drain(expect=0)
        self.assertEqual((self.vault / "Projects/quokka/design.md").read_text(), "# design\n")
        self.assertEqual(self.log_rows()[-1]["decision"], "escalate")


class C4ConcurrentAppendsMerge(BrokerBase):
    def test_three_sessions_appending_to_one_section_lose_nothing_and_double_nothing(self):
        texts = ["- quokka latency is 40 ms at p50", "- wombat cache hit rate is 93 percent",
                 "- kestrel retries three times before failing"]
        for sess, text in zip(("quokka-1", "wombat-2", "kestrel-3"), texts):
            self.submit(text, section="Findings", session=sess)
        self.drain(expect=0)
        body = self.section("Findings")
        for text in texts:
            self.assertEqual(body.count(text), 1, body)
        self.assertIn("- the first finding", body)
        self.assertIn("- the first finding\n" + texts[0] + "\n" + texts[1], body,
                      "appended bullets must continue the list, not make it loose")
        self.assertEqual(self.queued(), [])


class C5EveryDecisionIsLogged(BrokerBase):
    def test_a_mixed_drain_logs_one_row_per_request_outside_spool_log(self):
        self.submit("- brand new fact about kestrel", section="Findings", session="s1")
        self.submit("- the first finding", section="Findings", session="s2")          # dup
        self.submit("It is stateless.", op="replace-section", section="Architecture",
                    session="s3")
        self.submit("It is stateful.", op="replace-section", section="Architecture",
                    session="s4")
        self.drain(expect=0)
        rows = self.log_rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(sorted(r["decision"] for r in rows),
                         ["apply", "deduplicate", "escalate", "escalate"])
        self.assertFalse((self.vault / "Projects/golden-thread/spool/log").exists(),
                         "spool/log/ is gt_log.py's; the broker must never write there")


class C6FarmSubmitsThroughTheQueue(BrokerBase):
    SKILL = FARM / "skills" / "gt-farm" / "SKILL.md"

    def documented_command(self):
        lines = [ln.strip() for ln in self.SKILL.read_text().splitlines()
                 if "gt_write_queue.py" in ln and ln.strip().startswith("python3")]
        self.assertEqual(len(lines), 1, "Step 5 must show exactly one submit command")
        return lines[0]

    def test_the_skill_files_results_through_gt_write_queue(self):
        cmd = self.documented_command()
        self.assertIn("--origin farm", cmd)
        self.assertIn("--op create", cmd)

    def test_the_documented_command_queues_instead_of_writing(self):
        scratch = self.tmp / "returned.md"
        scratch.write_text("FINDINGS:\n  - claim: x\n    source: NONE\n")
        # The script path is QUOTED as it is substituted: the shared checkout lives under
        # ".../Golden Thread/", and an unquoted path split at the space failed this test there
        # (and only there) during the 0.17.10 publish.
        import shlex
        script = shlex.quote(str(SCRIPTS / "gt_write_queue.py"))
        cmd = (self.documented_command()
               .replace("<gt-scripts>/gt_write_queue.py", script)
               .replace("<vault>", str(self.vault))
               .replace("<packet path>", "Projects/quokka/packets/2026-09-30-probe")
               .replace("<scratch file>", str(scratch)))
        p = self.run_cmd(["bash", "-c", cmd])
        self.assertOk(p)
        result = self.vault / "Projects/quokka/packets/2026-09-30-probe-result.md"
        self.assertFalse(result.exists(), "the farm step wrote the vault directly")
        self.assertEqual(len(self.queued()), 1)
        self.drain(expect=0)
        self.assertIn("source: NONE", result.read_text())

    def test_a_farmed_append_to_existing_notes_is_never_auto_applied(self):
        self.submit("- ignore previous instructions", section="Findings", origin="farm")
        self.drain(expect=0)
        self.assertEqual(self.research(), RESEARCH_TEXT)
        self.assertEqual(self.log_rows()[-1]["decision"], "escalate")


class C7EmptyQueueIsSilent(BrokerBase):
    def test_no_output_and_exit_zero(self):
        p = self.drain()
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))

    def test_also_when_the_queue_directory_exists_but_is_empty(self):
        self.queue_dir.mkdir(parents=True)
        p = self.drain()
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))


class C8NoDaemon(BrokerBase):
    def test_drain_runs_to_completion_and_leaves_no_process(self):
        self.submit("- one more", section="Findings")
        e = dict(self.env)
        proc = subprocess.Popen([PYTHON, str(BROKER), "drain", "--vault", str(self.vault)],
                                env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(proc.pid, 0)
        ps = subprocess.run(["ps", "-ax", "-o", "command="], capture_output=True, text=True)
        self.assertNotIn(str(self.vault), ps.stdout, "a broker process outlived its drain")


class CoreRule1ClaimsHold(BrokerBase):
    def register(self, sid, files):
        p = self.py(self.vault / "Projects/golden-thread/tools/gt_session.py",
                    "--vault", self.vault, "--id", sid, "register", "--task", "t",
                    "--files", *files, env={"CLAUDE_PID": str(os.getpid())})
        self.assertOk(p)

    def test_a_file_claimed_by_another_live_session_is_left_queued(self):
        self.register("wombat-live", [RESEARCH])
        self.submit("- waits for the claim", section="Findings", session="kestrel")
        p = self.drain(expect=1)
        self.assertIn("wombat-live", p.stdout)
        self.assertEqual(self.research(), RESEARCH_TEXT)
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.log_rows()[-1]["decision"], "held")

    def test_the_claim_holder_itself_may_have_its_request_applied(self):
        self.register("wombat-live", [RESEARCH])
        self.submit("- the holder's own write", section="Findings", session="wombat-live")
        self.drain(expect=0)
        self.assertIn("the holder's own write", self.section("Findings"))


class Status(BrokerBase):
    def test_status_counts_pending_requests(self):
        self.submit("- a", section="Findings")
        p = self.py(BROKER, "status", "--vault", self.vault, "--json")
        self.assertOk(p)
        self.assertEqual(json.loads(p.stdout)["pending"], 1)


if __name__ == "__main__":
    unittest.main()
