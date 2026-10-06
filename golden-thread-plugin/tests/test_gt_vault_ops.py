"""gt_vault_ops.py -- the gt-vault MCP server's operation tools (0.20.2, gt sandbox mode).

Driven the way Claude Code drives them: JSON-RPC lines into gt_vault_mcp.py, against a vault
scaffolded by vault_init.py fresh, in a throwaway home. What is pinned: every tool's schema is
closed and enforced; no argument reaches a path outside the vault, a locked folder, or an argparse
flag; each operation does what the skill's shell step does (the same script); and the unlock
scope each tool asks for -- read or write -- and that design.md and global-memory/ are never
written by any tool, nor ever cost the owner an unlock prompt.
"""
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

from _harness import IS_WINDOWS, PYTHON, SCRIPTS, Sandbox
from _unlock_fixture import AuthorityCase

SERVER = SCRIPTS / "gt_vault_mcp.py"
sys.path.insert(0, str(SCRIPTS))
import gt_unlock_policy as P     # noqa: E402
import gt_review_target as RT    # noqa: E402  the broker's shared helper (0.20.1)
import gt_vault_ops as OPS       # noqa: E402

SESSION = "sess-mcp-ops"
LINE = "2026-10-04 12:00 CDT [work] alpha — did a thing"


class OpsFixture(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.proj = self.vault / "Projects" / "alpha"
        self.proj.mkdir(parents=True)
        (self.proj / "README.md").write_text(
            "---\nslug: alpha\nstage: active\n---\n# alpha\n\n## Tasks\n\n"
            "- [ ] existing [p:: 2] [since:: 2026-09-01]\n", encoding="utf-8")
        (self.proj / "research.md").write_text("# research\n\n## 2026-10-01 first\n\nA finding.\n",
                                               encoding="utf-8")
        hooks = self.home / ".claude" / "golden-thread" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        # gt_review_target.py: gt_broker imports it at load time since 0.20.1 (identity-based
        # review targets); install.sh lays it beside the broker, so the fixture must too.
        for n in ("gt_broker.py", "gt_write_queue.py", "gt_review_target.py"):
            shutil.copy(SCRIPTS / n, hooks / n)
        self.config(vault_path=str(self.vault), sandbox_mode="on")

    def rpc(self, *msgs, caps=None):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": caps or {}}}
        lines = "".join(json.dumps(m) + "\n" for m in (init,) + msgs)
        p = self.run_cmd([PYTHON, SERVER], input=lines, env={"CLAUDE_CODE_SESSION_ID": SESSION},
                         timeout=300)
        self.assertNotIn("Traceback", p.stderr)
        return [json.loads(x) for x in p.stdout.splitlines()]

    def call(self, name, args=None, extra=(), caps=None):
        out = self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": name, "arguments": args or {}}}, *extra, caps=caps)
        r = [m for m in out if m.get("id") == 2][0]["result"]
        self.assertEqual(json.loads(r["content"][0]["text"]), r["structuredContent"])
        return r["structuredContent"]

    def err(self, name, args, code=None):
        r = self.call(name, args)
        self.assertFalse(r["ok"], r)
        if code:
            self.assertEqual(r["error"]["code"], code, r)
        return r


class Schemas(unittest.TestCase):
    def test_every_tool_is_closed_and_has_a_handler(self):
        for t in OPS.TOOLS:
            self.assertIs(t["inputSchema"]["additionalProperties"], False, t["name"])
            self.assertIn(t["name"], OPS.HANDLERS)
            self.assertIn("annotations", t)
        # and the other way: no handler without a tool (a check in the test, never an assert in
        # shipped code -- python -O removes asserts)
        self.assertEqual(set(OPS.HANDLERS), {t["name"] for t in OPS.TOOLS})

    def test_annotations_say_what_the_tools_do(self):
        a = {t["name"]: t["annotations"] for t in OPS.TOOLS}
        # catchup and handoffs_surface write gt's own state outside the vault
        self.assertIs(a["vault_report"]["readOnlyHint"], False)
        self.assertIn("last-open.json", next(t for t in OPS.TOOLS
                                             if t["name"] == "vault_report")["description"])
        for n in ("vault_project", "vault_optimize"):
            self.assertIs(a[n]["destructiveHint"], True, n)

    def test_no_tool_takes_a_vault_a_session_or_a_command(self):
        for t in OPS.TOOLS:
            props = set(t["inputSchema"]["properties"])
            self.assertFalse(props & {"vault", "session", "cmd", "command", "argv", "script",
                                      "args"}, t["name"])

    def test_scopes(self):
        self.assertEqual(OPS.SCOPES["vault_report"], "gt:vault:read")
        self.assertNotIn("vault_accept", OPS.TOOL_NAMES)       # no accept in 0.20.2 (owner decision)
        for n in OPS.TOOL_NAMES - {"vault_report"}:
            self.assertEqual(OPS.SCOPES[n], "gt:vault:write", n)

    def test_policy_levels(self):
        eff = P.Effective(P.merge({"enabled": True}, None), [], True)
        self.assertEqual(eff.level("gt:vault:write"), "unlocked")   # never read_without_unlock
        self.assertEqual(eff.level("gt:vault:read"), "open")
        self.assertNotIn("gt:vault:accept", P.GT_SCOPES)
        self.assertFalse(P.unattended_scope_ok("gt:vault:write"))


class Refusals(OpsFixture):
    # a VALID call for each tool (nothing missing), so that an extra key is the only fault
    VALID = {"vault_report": {"report": "task_count"},
             "vault_task": {"action": "add", "text": "t", "project": "alpha"},
             "vault_tasks_regen": {},
             "vault_log_add": {"line": LINE},
             "vault_events_emit": {"kind": "capture", "item": "Projects/alpha/research.md"},
             "vault_adr": {"action": "merge", "project": "alpha"},
             "vault_project": {"action": "create", "name": "gamma"},
             "vault_handoff": {"action": "write", "project": "alpha"},
             "vault_closeout": {"action": "ask", "project": "alpha"},
             "vault_derived_write": {"action": "digest", "project": "alpha"},
             "vault_source_store": {"name": "2026-10-04 s.md", "content": "x"},
             "vault_optimize": {"action": "archive", "project": "alpha", "before": "2026-01-01"}}

    def test_unknown_argument_is_refused_by_every_tool(self):
        self.assertEqual(set(self.VALID), OPS.TOOL_NAMES)
        for name, base in self.VALID.items():
            # the base call passes the schema: nothing else is wrong with it ...
            OPS.check_args(name, dict(base))
            # ... so the extra key is what is refused
            r = self.err(name, dict(base, bogus=1), "bad_request")
            self.assertIn("takes no argument bogus", r["error"]["message"], name)

    def test_a_flag_shaped_value_never_reaches_argparse(self):
        self.err("vault_task", {"action": "add", "text": "--inbox", "project": "alpha"},
                 "bad_request")
        self.err("vault_closeout", {"action": "answer", "project": "alpha", "answer": "no",
                                    "note": "--vault /tmp"}, "bad_request")

    def test_paths_stay_inside_the_vault(self):
        for bad in ("../outside", "/etc", "Knowledge/../../x", ".obsidian", "C:/x"):
            r = self.err("vault_report", {"report": "supersede_listing", "folder": bad})
            self.assertIn(r["error"]["code"], ("bad_path", "outside_vault", "bad_request"), bad)
        self.err("vault_task", {"action": "add", "text": "x", "project": "../alpha"},
                 "bad_request")
        self.err("vault_source_store", {"name": "../2026-10-04 x.md", "content": "x"},
                 "bad_request")
        self.err("vault_source_store", {"name": "2026-10-04 a/b.md", "content": "x"},
                 "bad_request")

    def test_a_symlink_out_of_the_vault_is_refused(self):
        out = self.tmp / "elsewhere"
        out.mkdir()
        try:
            (self.vault / "Knowledge" / "link").symlink_to(out, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        self.err("vault_report", {"report": "supersede_listing", "folder": "Knowledge/link"},
                 "outside_vault")

    def test_a_locked_folder_is_never_touched(self):
        (self.proj / ".gt-locked").write_text("locked\n")
        self.err("vault_task", {"action": "add", "text": "x", "project": "alpha"}, "locked")
        self.err("vault_report", {"report": "digest_check", "project": "alpha"}, "locked")

    def test_log_line_needs_the_full_shape(self):
        self.err("vault_log_add", {"line": "did a thing"}, "bad_request")
        self.err("vault_log_add", {"line": LINE + "\nsecond"}, "bad_request")

    def test_adr_body_may_not_carry_a_second_heading(self):
        self.err("vault_adr", {"action": "allocate", "project": "alpha", "title": "T",
                               "body": "x\n## ADR-9: sneaky\n"}, "bad_request")

    def test_adr_body_heading_disguised_by_unicode_is_refused(self):
        """Review m3 (2026-10-06): a zero-width character or fullwidth letters hid a second
        heading from the guard; it is checked on the NFKC-folded text without format characters."""
        for body in ("x\n## A\u200bDR-9: sneaky\n", "x\n## \uff21\uff24\uff32-9: sneaky\n",
                     "x\n#\u00ad# ADR-9: sneaky\n"):
            with self.subTest(body=body):
                self.err("vault_adr", {"action": "allocate", "project": "alpha", "title": "T",
                                       "body": body}, "bad_request")

    def test_unknown_task_filter(self):
        self.err("vault_report", {"report": "tasks", "filters": ["--json; rm"]}, "bad_request")


class Operations(OpsFixture):
    def readme(self):
        return (self.proj / "README.md").read_text(encoding="utf-8")

    def test_task_add_report_and_rollup(self):
        r = self.call("vault_task", {"action": "add", "text": "write the tests",
                                     "project": "alpha", "p": 1})
        self.assertTrue(r["ok"], r)
        self.assertIn("write the tests", self.readme())
        r = self.call("vault_report", {"report": "tasks", "filters": ["alpha"]})
        self.assertTrue(r["ok"], r)
        ids = [t["id"] for t in r["parsed"] if "write the tests" in t["text"]]
        self.assertEqual(len(ids), 1)
        r = self.call("vault_task", {"action": "drop", "id": ids[0], "reason": "not needed"})
        self.assertTrue(r["ok"], r)
        self.assertIn("- [x] write the tests", self.readme())
        r = self.call("vault_tasks_regen")
        self.assertTrue(r["ok"], r)
        self.assertTrue((self.vault / "TASKS.md").is_file())

    def test_log_add_lands_in_log_md(self):
        r = self.call("vault_log_add", {"line": LINE})
        self.assertTrue(r["ok"], r)
        self.assertIn(LINE, (self.vault / "log.md").read_text(encoding="utf-8"))

    def test_create_project(self):
        r = self.call("vault_project", {"action": "create", "name": "beta", "title": "Beta",
                                        "tags": ["infra"]})
        self.assertTrue(r["ok"], r)
        self.assertTrue((self.vault / "Projects" / "beta" / "README.md").is_file())
        # vault_init.py create-project keeps gt's own section in ~/.claude/CLAUDE.md, as from
        # the shell; nothing the caller passed may reach that file.
        g = self.home / ".claude" / "CLAUDE.md"
        if g.exists():
            self.assertNotIn("Beta", g.read_text(encoding="utf-8"))

    def test_adr_allocate_with_body_renders_decisions(self):
        r = self.call("vault_adr", {"action": "allocate", "project": "alpha",
                                    "title": "Use the queue", "body": "- **Decision**: yes\n"})
        self.assertTrue(r["ok"], r)
        dec = (self.proj / "decisions.md").read_text(encoding="utf-8")
        self.assertIn("## ADR-1: Use the queue", dec)
        self.assertIn("- **Decision**: yes", dec)

    def test_closeout_answer_is_recorded(self):
        r = self.call("vault_closeout", {"action": "answer", "project": "alpha",
                                         "answer": "no", "note": "still going"})
        self.assertTrue(r["ok"], r)
        rec = self.vault / "Projects" / "golden-thread" / "closeout-signals.jsonl"
        self.assertIn("still going", rec.read_text(encoding="utf-8"))

    def test_digest_write(self):
        r = self.call("vault_derived_write", {"action": "digest", "project": "alpha"})
        self.assertTrue(r["ok"], r)
        self.assertTrue((self.proj / "research-digest.md").is_file())

    def test_source_store_is_write_once(self):
        args = {"name": "2026-10-04 A source.md", "content": "---\ntype: source\n---\nraw\n"}
        r = self.call("vault_source_store", args)
        self.assertTrue(r["ok"], r)
        self.assertEqual((self.vault / "Sources" / "2026-10-04 A source.md").read_text(),
                         args["content"])
        self.err("vault_source_store", dict(args, content="changed"), "exists")
        self.assertEqual((self.vault / "Sources" / "2026-10-04 A source.md").read_text(),
                         args["content"])

    def test_farm_origin_is_stricter_and_nothing_looser_exists(self):
        r = self.call("vault_queue_write", {"path": "Projects/alpha/research.md", "op": "append",
                                            "section": "2026-10-01 first", "content": "farmed",
                                            "origin": "farm"})
        self.assertEqual(r["decision"], "escalate", r)
        self.assertNotIn("farmed", (self.proj / "research.md").read_text(encoding="utf-8"))
        r = self.call("vault_queue_write", {"path": "Projects/alpha/research.md", "op": "append",
                                            "content": "x", "origin": "session"})
        self.assertFalse(r["ok"], r)

    def test_optimize_previews_unless_apply(self):
        r = self.call("vault_optimize", {"action": "archive", "project": "alpha",
                                         "before": "2026-10-02"})
        self.assertIn("ok", r)
        self.assertIn("A finding.", (self.proj / "research.md").read_text(encoding="utf-8"))

    def test_reports_run(self):
        for args in ({"report": "lint"}, {"report": "task_count"},
                     {"report": "handoffs"}, {"report": "broker_status"},
                     {"report": "digest_check", "project": "alpha"},
                     {"report": "conflicts"}):
            r = self.call("vault_report", args)
            self.assertIn("ok", r)
            self.assertNotEqual((r.get("error") or {}).get("code"), "internal", (args, r))


OPS_PROPS = {t["name"]: set(t["inputSchema"]["properties"]) for t in OPS.TOOLS}
SEPARATORS = ["\u0085", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", " ", " ", "\r", "\n"]


class Separators(OpsFixture):
    """Review MAJOR-3: every Unicode line separator ends a line for str.splitlines() (gt_adr merge,
    the log merge), so a one-line field holding one forges a heading or a second log line."""

    FIELDS = [
        ("vault_adr", {"action": "allocate", "project": "alpha", "title": "t"}, "title"),
        ("vault_adr", {"action": "allocate", "project": "alpha", "title": "t"}, "expires_when"),
        ("vault_log_add", {"line": LINE}, "line"),
        ("vault_log_add", {"line": LINE, "event": "capture", "item": "Projects/alpha/research.md"},
         "note"),
        ("vault_project", {"action": "create", "name": "gamma"}, "title"),
        ("vault_project", {"action": "create", "name": "gamma"}, "domain"),
        ("vault_project", {"action": "create", "name": "gamma"}, "fleet"),
        ("vault_project", {"action": "archive", "project": "alpha"}, "reason"),
        ("vault_task", {"action": "add", "text": "t", "project": "alpha"}, "text"),
        ("vault_task", {"action": "add", "text": "t", "project": "alpha"}, "ref"),
        ("vault_task", {"action": "drop", "id": "alpha:3:abcdef", "reason": "r"}, "reason"),
        ("vault_handoff", {"action": "close", "file": "Projects/alpha/handoff/h.md",
                           "reason": "r"}, "reason"),
        ("vault_closeout", {"action": "answer", "project": "alpha", "answer": "no"}, "note"),
        ("vault_events_emit", {"kind": "capture", "item": "Projects/alpha/research.md"}, "note"),
        ("vault_report", {"report": "adr_lineage", "project": "alpha"}, "topic"),
        ("vault_optimize", {"action": "supersede", "file": "Projects/alpha/research.md",
                            "entry": "e", "by": "b"}, "entry"),
    ]

    def test_every_one_line_field_refuses_every_separator(self):
        for tool, base, field in self.FIELDS:
            OPS.check_args(tool, dict(base, **{field: "plain"}))              # the form is valid
            for sep in SEPARATORS:
                bad = dict(base, **{field: "a%s## ADR-9: forged" % sep})
                with self.assertRaises(OPS.OpError, msg=(tool, field, repr(sep))) as cm:
                    OPS.check_args(tool, bad)
                self.assertEqual(cm.exception.code, "bad_request")

    def test_the_multiline_fields_are_only_body_and_content(self):
        self.assertEqual(set(OPS.MULTILINE_FIELDS), {"body", "content"})
        OPS.check_args("vault_source_store", {"name": "2026-10-04 s.md", "content": "a\nb c"})

    def test_a_pattern_must_match_the_whole_value(self):
        for tool, base, field, junk in (
                ("vault_task", {"action": "defer", "id": "alpha:3:abcdef", "reason": "r"},
                 "until", "2026-10-04junk"),
                ("vault_task", {"action": "drop", "reason": "r"}, "id", "alpha:3:abcdefZZ"),
                ("vault_project", {"action": "create"}, "name", "ok name"),
                ("vault_handoff", {"action": "write", "project": "alpha"}, "since_commit",
                 "abcdef1 --force")):
            args = dict(base, **{field: junk})
            args.setdefault("until", "2026-10-04")
            args.setdefault("id", "alpha:3:abcdef")
            with self.assertRaises(OPS.OpError, msg=(tool, field)) as cm:
                OPS.check_args(tool, {k: v for k, v in args.items()
                                      if k in OPS_PROPS[tool]})
            self.assertEqual(cm.exception.code, "bad_request")

    def test_a_trailing_newline_does_not_pass_a_pattern(self):
        self.err("vault_task", {"action": "add", "text": "t", "project": "alpha\n"},
                 "bad_request")
        self.err("vault_report", {"report": "tasks", "filters": ["p1\n"]}, "bad_request")

    def test_a_forged_adr_heading_after_any_separator_takes_no_number(self):
        for sep in SEPARATORS:
            r = self.err("vault_adr", {"action": "allocate", "project": "alpha", "title": "T",
                                       "body": "fine%s## ADR-9: forged" % sep}, "bad_request")
            self.assertIn("ADR", r["error"]["message"])
        spool = self.vault / "Projects" / "golden-thread" / "spool" / "decisions"
        self.assertEqual(list(spool.rglob("0*.md")) if spool.is_dir() else [], [])

    def test_the_heading_guard_itself_splits_on_every_separator(self):
        # the guard runs on splitlines(), not on "\n": checked without the schema in front of it
        # (the body field is multi-line by design, so the schema lets the separator through)
        for sep in SEPARATORS:
            with self.assertRaises(OPS.OpError, msg=repr(sep)):
                OPS.op_adr(self.ctx(), {"action": "allocate", "project": "alpha", "title": "T",
                                        "body": "x%s## ADR-9: forged" % sep})

    def ctx(self):
        import gt_vault_mcp as M
        return OPS.Ctx(M.Vault(str(self.vault)), SESSION, FakeGate())

    def test_a_forged_log_line_adds_nothing(self):
        before = (self.vault / "log.md").read_text(encoding="utf-8")
        for sep in SEPARATORS:
            self.err("vault_log_add", {"line": LINE + sep + "2026-10-04 12:01 CDT [work] other — "
                                                            "forged"}, "bad_request")
        self.assertEqual((self.vault / "log.md").read_text(encoding="utf-8"), before)


class DashPaths(OpsFixture):
    """Review MAJOR-2: a vault path that starts with '-' is read by argparse as a flag."""

    def setUp(self):
        super().setUp()
        k = self.vault / "Knowledge"
        k.mkdir(exist_ok=True)
        self.page = k / "page1.md"
        self.page.write_text("---\ntitle: P\nstatus: current\n---\n# P\n", encoding="utf-8")

    def test_every_path_argument_refuses_a_leading_dash(self):
        dash = "--date=2031-01-01"
        cases = [
            ("vault_report", {"report": "supersede_rank", "pages": [dash, "Knowledge/page1.md"]}),
            ("vault_report", {"report": "supersede_rank", "pages": ["Knowledge/page1.md", dash]}),
            ("vault_report", {"report": "supersede_listing", "folder": "-x"}),
            ("vault_report", {"report": "memory_check", "project": "alpha", "pages": [dash]}),
            ("vault_report", {"report": "link_suggest", "page": dash}),
            ("vault_derived_write", {"action": "review_stamp",
                                     "pages": [dash, "Knowledge/page1.md"]}),
            ("vault_derived_write", {"action": "links", "page": dash, "to": ["x"]}),
            ("vault_handoff", {"action": "close", "file": "-x", "reason": "r"}),
            ("vault_optimize", {"action": "supersede", "file": dash, "entry": "e", "by": "b"}),
        ]
        for tool, args in cases:
            r = self.err(tool, args)
            self.assertIn(r["error"]["code"], ("bad_path", "bad_request"), (tool, args, r))
        self.assertNotIn("2031", self.page.read_text(encoding="utf-8"))

    def test_the_stamp_probe_stamps_nothing_with_a_flag_in_the_list(self):
        self.err("vault_derived_write", {"action": "review_stamp",
                                         "pages": ["--date=2031-01-01", "Knowledge/page1.md"]})
        self.assertNotIn("last_reviewed", self.page.read_text(encoding="utf-8"))

    def test_double_dash_precedes_every_positional_path_list(self):
        """Defence in depth behind the leading-dash refusal: the argv itself puts `--` before the
        pages, so a path could never be an option even if one slipped through."""
        import gt_vault_mcp as M
        seen = []
        real_run = OPS.run
        OPS.run = lambda ctx, script, args, **kw: seen.append(list(args)) or {"ok": True}
        try:
            ctx = OPS.Ctx(M.Vault(str(self.vault)), SESSION, FakeGate())
            OPS.op_derived(ctx, {"action": "review_stamp", "pages": ["Knowledge/page1.md"]})
            OPS.op_report(ctx, {"report": "supersede_rank", "pages": ["Knowledge/page1.md"]})
        finally:
            OPS.run = real_run
        self.assertEqual(len(seen), 2)
        for args in seen:
            i = args.index("--")
            self.assertEqual(args[i + 1:], ["Knowledge/page1.md"])
            self.assertNotIn("--vault", args[i:])

    def test_real_paths_still_work_after_the_double_dash(self):
        r = self.call("vault_derived_write", {"action": "review_stamp",
                                              "pages": ["Knowledge/page1.md"]})
        self.assertTrue(r["ok"], r)
        self.assertIn("last_reviewed", self.page.read_text(encoding="utf-8"))
        r = self.call("vault_report", {"report": "supersede_rank",
                                       "pages": ["Knowledge/page1.md"]})
        self.assertTrue(r["ok"], r)


class SlotChecks(OpsFixture):
    """M11: vault_adr appends the body to the slot gt_adr.py reserved -- each of the four
    conditions of check_slot is refused on its own."""

    def setUp(self):
        super().setUp()
        self.base = self.vault / "Projects" / "golden-thread" / "spool" / "decisions"
        (self.base / "alpha").mkdir(parents=True)
        self.slot = self.base / "alpha" / "0001.md"
        self.head = "<!-- allocated by %s on 2026-10-04 -->\n## ADR-1: t\n" % SESSION
        self.slot.write_text(self.head, encoding="utf-8")
        import gt_vault_mcp as M
        self.ctx = OPS.Ctx(M.Vault(str(self.vault)), SESSION, FakeGate())

    def refused(self, rel, fname, text):
        with self.assertRaises(OPS.OpError) as cm:
            OPS.check_slot(self.ctx, rel, fname)
        self.assertEqual(cm.exception.code, "unexpected")
        self.assertIn(text, cm.exception.message)

    def test_the_slot_this_session_allocated_passes(self):
        self.assertEqual(OPS.check_slot(self.ctx, "alpha", "0001.md"),
                         os.path.realpath(str(self.slot)))

    def test_a_slot_allocated_by_another_session_is_refused(self):
        self.slot.write_text(self.head.replace(SESSION, "someone-else"), encoding="utf-8")
        self.refused("alpha", "0001.md", "not allocated by this session")

    def test_a_session_id_that_only_prefixes_ours_is_refused(self):
        self.slot.write_text(self.head.replace(SESSION, SESSION + "-2"), encoding="utf-8")
        self.refused("alpha", "0001.md", "not allocated by this session")

    def test_a_symlink_is_refused_even_inside_the_spool(self):
        real = self.base / "alpha" / "0002.md"
        real.write_text(self.head, encoding="utf-8")
        link = self.base / "alpha" / "0003.md"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        self.refused("alpha", "0003.md", "symlink")

    def test_a_directory_is_not_a_slot(self):
        (self.base / "alpha" / "0004.md").mkdir()
        self.refused("alpha", "0004.md", "regular file")

    def test_a_slot_outside_spool_decisions_is_refused(self):
        esc = self.base.parent / "escape"                     # spool/escape: inside the spool,
        esc.mkdir()                                           # outside spool/decisions
        (esc / "0001.md").write_text(self.head, encoding="utf-8")
        self.refused("../escape", "0001.md", "not inside spool/decisions")

    def test_a_missing_slot_is_refused(self):
        with self.assertRaises(OPS.OpError):
            OPS.check_slot(self.ctx, "alpha", "0009.md")


class NamesAndSources(OpsFixture):
    def test_windows_device_names_are_not_project_names(self):
        for bad in ("con", "nul", "aux", "prn", "com1", "lpt9"):
            r = self.err("vault_project", {"action": "create", "name": bad}, "bad_request")
            self.assertIn("Windows", r["error"]["message"])
            if not IS_WINDOWS:                   # on Windows CON, NUL ... "exist" in every folder
                self.assertFalse((self.vault / "Projects" / bad).exists())
        self.err("vault_project", {"action": "create", "name": "ok", "parent": "con"},
                 "bad_request")

    def test_source_names_refuse_hidden_and_reordering_characters(self):
        for ch in ("‮", "‭", "⁦", "​", "‏", "﻿", "\x7f", "\x85",
                   "\x9f", " ", "⁠"):
            self.err("vault_source_store", {"name": "2026-10-04 a%sb.md" % ch, "content": "x"})
        self.assertEqual(sorted(p.name for p in (self.vault / "Sources").glob("2026-10-04*")), [])


class HeldIsNotAnError(OpsFixture):
    def claim(self, rel, sid="wombat-live"):
        p = self.py(self.vault / "Projects/golden-thread/tools/gt_session.py", "--vault",
                    self.vault, "--id", sid, "register", "--task", "t", "--files", rel,
                    env={"CLAUDE_PID": str(os.getpid())})
        self.assertOk(p)

    def test_a_task_held_by_a_live_claim_is_queued_ok(self):
        self.claim("Projects/alpha/README.md")
        r = self.call("vault_task", {"action": "add", "text": "held one", "project": "alpha"})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["held"] and r["queued"], r)
        self.assertNotIn("error", r)
        self.assertIn("queued, not written", r["note"])
        self.assertNotIn("held one", (self.proj / "README.md").read_text(encoding="utf-8"))

    def test_a_real_failure_is_still_an_error(self):
        r = self.err("vault_task", {"action": "drop", "id": "alpha:99:abcdef", "reason": "r"})
        self.assertNotIn("held", r)


class ResultShape(OpsFixture):
    def test_a_parsed_result_is_not_also_in_output(self):
        r = self.call("vault_report", {"report": "tasks"})
        self.assertTrue(r["ok"], r)
        self.assertIsNotNone(r["parsed"])
        self.assertEqual(r["output"], "")
        self.assertLess(len(json.dumps(r)), 2 * OPS.OUT_MAX)

    def test_every_result_says_it_is_untrusted_in_its_own_text(self):
        for name, args in (("vault_report", {"report": "task_count"}),
                           ("vault_log_add", {"line": LINE})):
            r = self.call(name, args)
            self.assertIs(r["untrusted"], True)
            self.assertIn("untrusted data", r["notice"])
        r = self.err("vault_task", {"action": "add", "text": "x", "project": "nope"})
        self.assertIn("untrusted data", r.get("notice", "untrusted data") + "")  # refusals carry none

    def test_the_cap_covers_stderr_and_output_alike(self):
        self.assertEqual(OPS._cap("x" * (OPS.OUT_MAX + 5))[1], True)

    def test_children_get_the_servers_vault_and_session_not_the_callers(self):
        saved = {k: os.environ.get(k) for k in ("GT_VAULT", "CLAUDE_CODE_SESSION_ID",
                                                "GT_BROKER_APPLYING")}
        os.environ.update(GT_VAULT="/tmp/not-the-vault", CLAUDE_CODE_SESSION_ID="attacker",
                          GT_BROKER_APPLYING="1")
        try:
            import gt_vault_mcp as M
            env = OPS._env(OPS.Ctx(M.Vault(str(self.vault)), SESSION, FakeGate()))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertTrue(os.path.samefile(env["GT_VAULT"], self.vault))
        self.assertEqual(env["CLAUDE_CODE_SESSION_ID"], SESSION)
        self.assertNotIn("GT_BROKER_APPLYING", env)


class LintQueues(OpsFixture):
    """Review MINOR-1: the two review-queue writers never clobber a file they did not make."""

    def claim(self, rel):
        p = self.py(self.vault / "Projects/golden-thread/tools/gt_session.py", "--vault",
                    self.vault, "--id", "wombat-live", "register", "--task", "t", "--files", rel,
                    env={"CLAUDE_PID": str(os.getpid())})
        self.assertOk(p)

    def test_lint_queue_waits_for_a_live_sessions_claim(self):
        self.claim("review-queue.md")
        before = (self.vault / "review-queue.md").read_text(encoding="utf-8")
        r = self.err("vault_derived_write", {"action": "lint_queue"}, "held")
        self.assertIn("wombat-live", r["error"]["message"])
        self.assertEqual((self.vault / "review-queue.md").read_text(encoding="utf-8"), before)

    def test_lint_queue_writes_and_keeps_ticks(self):
        r = self.call("vault_derived_write", {"action": "lint_queue"})
        self.assertIn(r["exit"], (0, 1), r)
        q = self.vault / "review-queue.md"
        self.assertIn("Vault Review Queue", q.read_text(encoding="utf-8"))

    def test_lint_queue_holds_the_broker_lock(self):
        import gt_broker as B
        if IS_WINDOWS:                           # the drain lock is fcntl-only (0.20.1 B2)
            self.skipTest("no file locking here")
        before = (self.vault / "review-queue.md").read_text(encoding="utf-8")
        lock = B._lock(self.vault)
        if lock is None:
            self.skipTest("no file locking here")
        try:
            self.err("vault_derived_write", {"action": "lint_queue"}, "busy")
        finally:
            lock.close()
        self.assertEqual((self.vault / "review-queue.md").read_text(encoding="utf-8"), before)

    def wiki(self):
        return self.call("vault_derived_write", {"action": "wiki_lint_queue"})

    def test_wiki_queue_goes_through_the_write_queue_not_a_plain_open(self):
        r = self.wiki()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["path"], "review-queue.md")
        self.assertEqual(r["decision"], "apply", r)
        self.assertTrue((self.vault / "review-queue.md").read_text(encoding="utf-8")
                        .startswith("# Review queue"))
        rows = list((self.vault / "Projects/golden-thread/spool/broker").glob("log-*.jsonl"))
        self.assertIn("review-queue.md", "".join(x.read_text() for x in rows))

    def test_wiki_queue_never_overwrites_a_lint_or_ticked_queue(self):
        owner = "# Vault Review Queue\n\n- [x] `a.md` — triaged by me\n"
        for existing in (owner, "# Review queue\n\nRegenerated by wiki-lint. Pending: 1\n\n"
                                "- [x] ticked by the owner\n"):
            (self.vault / "review-queue.md").write_text(existing, encoding="utf-8")
            r = self.wiki()
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["path"], "review-queue-wiki.md")
            self.assertEqual((self.vault / "review-queue.md").read_text(encoding="utf-8"),
                             existing)
            wq = self.vault / "review-queue-wiki.md"
            if wq.exists():
                wq.unlink()

    def test_wiki_queue_leaves_nothing_in_the_vault_but_the_queued_file(self):
        self.wiki()
        self.assertEqual([p.name for p in self.vault.glob("*.gt-tmp*")], [])
        self.assertEqual([p for p in self.vault.rglob("q.md")], [])



class FakeGate:
    def __init__(self, refuse=None):
        self.asked, self.refuse = [], refuse

    def require(self, scope, what):
        self.asked.append(scope)
        if self.refuse and scope == self.refuse:
            raise OPS.OpError("locked", "refused by the test gate")
        return None


class Gating(OpsFixture):
    """The scope each call asks for, and that a refusal runs nothing."""

    def ctx(self, gate):
        sys.path.insert(0, str(SCRIPTS))
        import gt_vault_mcp as M
        return OPS.Ctx(M.Vault(str(self.vault)), SESSION, gate)

    def test_report_asks_read_and_writes_ask_write(self):
        g = FakeGate()
        OPS.call("vault_report", {"report": "broker_status"}, self.ctx(g))
        with self.assertRaises(OPS.OpError):              # no project: refused after the gate
            OPS.call("vault_task", {"action": "add", "text": "x"}, self.ctx(g))
        self.assertEqual(g.asked, ["gt:vault:read", "gt:vault:write"])

    def test_a_refused_write_runs_nothing(self):
        before = (self.proj / "README.md").read_text(encoding="utf-8")
        g = FakeGate(refuse="gt:vault:write")
        with self.assertRaises(OPS.OpError):
            OPS.call("vault_task", {"action": "add", "text": "x", "project": "alpha"},
                     self.ctx(g))
        self.assertEqual((self.proj / "README.md").read_text(encoding="utf-8"), before)


class WriteSeat(AuthorityCase):
    """gt unlock on: gt:vault:write is served only to the session's registered vault server, and
    -- unlike gt:vault:read -- is never opened by read_without_unlock: it needs an unlock."""

    def setUp(self):
        super().setUp()
        self.standard()
        self.auth.vault_shim_ok = lambda peer: True
        self.vshim = self.child()
        self.assertIn("result", self.call(self.vshim, "register_shim", {"role": "vault"}))

    def check(self, proc, scope):
        return self.call(proc, "check", {"scope": scope})["result"]

    def test_the_shell_gets_neither_read_nor_write(self):
        bash = self.child()
        self.assertEqual(self.check(bash, "gt:vault:write")["code"], "mcp_only")
        self.assertEqual(self.check(bash, "gt:vault:read")["code"], "mcp_only")

    def test_write_needs_an_unlock_but_read_does_not(self):
        self.assertTrue(self.check(self.vshim, "gt:vault:read")["allowed"])     # read_without_unlock
        self.assertEqual(self.check(self.vshim, "gt:vault:write")["code"], "locked")
        self.assertIn("result", self.call(self.vshim, "unlock", {"tty": True},
                                          answers=[self.code()]))
        self.assertTrue(self.check(self.vshim, "gt:vault:write")["allowed"])

    def test_the_vault_seat_still_gets_no_other_scope(self):
        for scope in ("lotr:github:read", "gt:secrets"):
            self.assertEqual(self.check(self.vshim, scope)["code"], "mcp_only", scope)


class EveryMutatingToolIsGated(OpsFixture):
    """With unlock on, each tool asks the authority for exactly one scope, through the server's own
    gate, before anything runs: gt:vault:read for a report, gt:vault:write for every other."""

    def ctx(self, gate):
        import gt_vault_mcp as M
        return OPS.Ctx(M.Vault(str(self.vault)), SESSION, gate)

    def test_each_tool_asks_for_its_scope_and_a_refusal_runs_nothing(self):
        base = Refusals.VALID
        self.assertEqual(set(base), OPS.TOOL_NAMES)
        before = (self.proj / "README.md").read_text(encoding="utf-8")
        for name, args in base.items():
            want = "gt:vault:read" if name == "vault_report" else "gt:vault:write"
            g = FakeGate(refuse=want)
            with self.assertRaises(OPS.OpError, msg=name) as cm:
                OPS.call(name, dict(args), self.ctx(g))
            self.assertEqual(cm.exception.code, "locked", name)
            self.assertEqual(g.asked, [want], name)
        self.assertEqual((self.proj / "README.md").read_text(encoding="utf-8"), before)
        self.assertFalse((self.vault / "Projects" / "gamma").exists())

    def test_the_server_gates_the_queue_tools_with_the_write_scope_too(self):
        import io
        import gt_vault_mcp as M
        for name, args in (("vault_queue_write", {"path": "Projects/alpha/research.md",
                                                  "op": "append", "content": "x"}),
                           ("vault_queue_drain", {})):
            g = FakeGate(refuse="gt:vault:write")
            srv = M.Server(str(self.vault), out=io.StringIO(), gate=g, offered=True)
            # the server's own Gate raises ToolError; the fake raises OpError -- either way a
            # refusal runs nothing and asks for exactly the write scope
            try:
                r = srv.tools_call(name, args)["structuredContent"]
                self.assertFalse(r["ok"], name)
            except OPS.OpError:
                pass
            self.assertEqual(g.asked, ["gt:vault:write"], name)
        self.assertEqual(list((self.vault / "Projects/golden-thread/spool/queue").glob("*.json"))
                         if (self.vault / "Projects/golden-thread/spool/queue").is_dir() else [], [])


REVIEW_TARGETS = ["Projects/alpha/design.md", "Projects/alpha/Design.MD", "design.md", "DESIGN.MD",
                  "Projects/alpha/design.md.", "Projects/alpha/design.md ",
                  "Projects/alpha/design.md::$DATA", "global-memory/x.md", "Global-Memory/x.md",
                  "GLOBAL-MEMORY/sub/y.md", "global-memory"]


class ReviewTargets(OpsFixture):
    """Owner decision 2026-10-04: no gt tool writes design.md or global-memory/. Refused for
    every mutating tool and every path argument, before the unlock gate is asked."""

    def setUp(self):
        super().setUp()
        (self.proj / "design.md").write_text("# alpha design\n", encoding="utf-8")
        gm = self.vault / "global-memory"
        gm.mkdir(exist_ok=True)
        (gm / "x.md").write_text("# x\n", encoding="utf-8")
        k = self.vault / "Knowledge"
        k.mkdir(exist_ok=True)
        (k / "ok.md").write_text("---\ntitle: ok\n---\n# ok\n", encoding="utf-8")
        self.snapshot = self.files()

    def files(self):
        return {str(p.relative_to(self.vault)): p.read_bytes()
                for d in ("Projects/alpha", "global-memory", "Knowledge", "Sources")
                for p in (self.vault / d).rglob("*") if p.is_file()}

    def ctx(self, gate):
        import gt_vault_mcp as M
        return OPS.Ctx(M.Vault(str(self.vault)), SESSION, gate)

    def refused(self, tool, args, ctx=None):
        g = FakeGate()
        with self.assertRaises(OPS.OpError, msg=(tool, args)) as cm:
            OPS.call(tool, args, ctx or self.ctx(g))
        self.assertEqual(cm.exception.code, "owner_only", (tool, args, cm.exception.message))
        self.assertEqual(cm.exception.message, OPS.REVIEW_MESSAGE)
        self.assertEqual(g.asked, [], "a refused target must never cost the owner an unlock "
                                      "prompt: %r" % (args,))

    def test_the_message_is_the_one_line_the_owner_asked_for(self):
        self.assertEqual(OPS.REVIEW_MESSAGE, "design.md and global-memory/ are changed by you, "
                                             "in an editor — gt tools never write them")

    def test_every_path_argument_of_every_mutating_tool(self):
        for t in REVIEW_TARGETS:
            self.refused("vault_handoff", {"action": "close", "file": t, "reason": "r"})
            self.refused("vault_handoff", {"action": "mark", "file": t, "status": "handled"})
            self.refused("vault_derived_write", {"action": "links", "page": t, "to": ["Other"]})
            self.refused("vault_derived_write", {"action": "links", "page": "Knowledge/ok.md",
                                                 "to": [t]})
            self.refused("vault_derived_write", {"action": "review_stamp",
                                                 "pages": ["Knowledge/ok.md", t]})
            self.refused("vault_optimize", {"action": "supersede", "file": t, "entry": "e",
                                            "by": "b"})
        # page NAMES that become <name>.md
        self.refused("vault_derived_write", {"action": "links", "page": "Knowledge/ok.md",
                                             "to": ["design"]})
        self.refused("vault_derived_write", {"action": "links", "page": "Knowledge/ok.md",
                                             "to": ["DESIGN"]})
        self.assertEqual(self.files(), self.snapshot)

    def test_the_operations_refuse_them_themselves_without_the_precheck(self):
        """Defence in depth: each operation also refuses on its own (the precheck is only first)."""
        c = self.ctx(FakeGate())
        for to in ("design", "DESIGN", "global-memory/x", "Projects/alpha/design.md"):
            with self.assertRaises(OPS.OpError, msg=to) as cm:
                OPS.op_derived(c, {"action": "links", "page": "Knowledge/ok.md", "to": [to]})
            self.assertEqual(cm.exception.code, "owner_only", to)
        for t in REVIEW_TARGETS:
            for fn, args in ((OPS.op_derived, {"action": "review_stamp", "pages": [t]}),
                             (OPS.op_derived, {"action": "links", "page": t, "to": ["x"]}),
                             (OPS.op_handoff, {"action": "close", "file": t, "reason": "r"}),
                             (OPS.op_optimize, {"action": "supersede", "file": t, "entry": "e",
                                                "by": "b"})):
                with self.assertRaises(OPS.OpError, msg=(fn.__name__, t)) as cm:
                    fn(c, args)
                self.assertEqual(cm.exception.code, "owner_only", (fn.__name__, t))
        self.assertEqual(self.files(), self.snapshot)

    def test_check_path_treats_a_page_name_as_a_path_to_name_dot_md(self):
        for x in ("design", "Design", "DESIGN"):
            with self.assertRaises(OPS.OpError, msg=x):
                OPS.check_path(str(self.vault), x)
        OPS.check_path(str(self.vault), "Knowledge/ok")

    def test_a_source_store_name_can_never_name_one(self):
        # the name is checked against the whole 'YYYY-MM-DD <title>.md' pattern by the schema, so
        # none of these reaches the gate; the file would land in Sources/ anyway
        for t in REVIEW_TARGETS + ["../design.md", "../global-memory/x.md",
                                   "2026-10-04 ../../design.md"]:
            g = FakeGate()
            with self.assertRaises(OPS.OpError, msg=t):
                OPS.call("vault_source_store", {"name": t, "content": "x"}, self.ctx(g))
            self.assertEqual(g.asked, [], t)
        self.assertEqual(self.files(), self.snapshot)

    def test_ctx_rel_refuses_them_too_the_resolver_itself(self):
        c = self.ctx(FakeGate())
        for t in REVIEW_TARGETS:
            with self.assertRaises(OPS.OpError, msg=t) as cm:
                c.rel(t)
            self.assertEqual(cm.exception.code, "owner_only", t)
        self.assertEqual(c.rel("Knowledge/ok.md"), "Knowledge/ok.md")
        for t in ("Projects/alpha/design.md", "global-memory/x.md"):   # reads are not writes
            self.assertEqual(c.rel(t, read=True), t)

    def test_symlinks_to_them_are_refused_by_what_they_resolve_to(self):
        k = self.vault / "Knowledge"
        try:
            (k / "link.md").symlink_to(self.proj / "design.md")
            (k / "gm").symlink_to(self.vault / "global-memory", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        self.snapshot = self.files()                     # the links are part of the fixture
        self.refused("vault_derived_write", {"action": "review_stamp",
                                             "pages": ["Knowledge/link.md"]})
        self.refused("vault_derived_write", {"action": "review_stamp",
                                             "pages": ["Knowledge/gm/x.md"]})
        self.refused("vault_optimize", {"action": "supersede", "file": "Knowledge/link.md",
                                        "entry": "e", "by": "b"})
        c = self.ctx(FakeGate())
        with self.assertRaises(OPS.OpError) as cm:
            c.rel("Knowledge/link.md")                      # past precheck: the resolver itself
        self.assertEqual(cm.exception.code, "owner_only")
        self.assertEqual(self.files(), self.snapshot)

    def test_vault_queue_write_refuses_them_before_any_prompt(self):
        import io
        import gt_vault_mcp as M
        for t in REVIEW_TARGETS:
            for op in ("append", "create", "replace-file"):
                g = FakeGate()
                srv = M.Server(str(self.vault), out=io.StringIO(), gate=g, offered=True)
                r = srv.tools_call("vault_queue_write", {"path": t, "op": op, "content": "x"})
                r = r["structuredContent"]
                self.assertFalse(r["ok"], (t, op))
                self.assertEqual(r["error"]["code"], "owner_only", (t, op, r))
                self.assertEqual(r["error"]["message"], OPS.REVIEW_MESSAGE)
                self.assertEqual(g.asked, [], (t, op))
        q = self.vault / "Projects/golden-thread/spool/queue"
        self.assertEqual(list(q.glob("*.json")) if q.is_dir() else [], [])
        self.assertEqual(self.files(), self.snapshot)

    def test_vault_queue_write_refuses_a_symlinked_review_target(self):
        import io
        import gt_vault_mcp as M
        try:
            (self.vault / "Knowledge" / "link.md").symlink_to(self.proj / "design.md")
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        g = FakeGate()
        srv = M.Server(str(self.vault), out=io.StringIO(), gate=g, offered=True)
        r = srv.tools_call("vault_queue_write", {"path": "Knowledge/link.md", "op": "append",
                                                 "content": "x"})["structuredContent"]
        self.assertEqual(r["error"]["code"], "owner_only")
        self.assertEqual(g.asked, [])

    def test_ordinary_targets_still_work_and_ask_for_the_write_scope(self):
        import io
        import gt_vault_mcp as M
        g = FakeGate()
        srv = M.Server(str(self.vault), out=io.StringIO(), gate=g, offered=True)
        r = srv.tools_call("vault_queue_write", {"path": "Projects/alpha/research.md",
                                                 "op": "append", "section": "2026-10-01 first",
                                                 "content": "- ordinary"})["structuredContent"]
        self.assertTrue(r["ok"], r)
        self.assertEqual(g.asked, ["gt:vault:write"])

    def test_reads_and_event_names_may_name_them(self):
        r = self.call("vault_report", {"report": "supersede_rank",
                                       "pages": ["Projects/alpha/design.md"]})
        self.assertTrue(r["ok"], r)
        r = self.call("vault_events_emit", {"kind": "capture", "item": "Projects/alpha/design.md",
                                            "merge": False})
        self.assertNotEqual((r.get("error") or {}).get("code"), "owner_only", r)
        self.assertEqual(self.files(), self.snapshot)

    def test_create_project_still_scaffolds_its_own_new_design_md(self):
        r = self.call("vault_project", {"action": "create", "name": "newproj"})
        self.assertTrue(r["ok"], r)
        self.assertTrue((self.vault / "Projects" / "newproj" / "design.md").is_file())

    def test_the_broker_still_escalates_one_that_arrives_from_the_shell(self):
        p = self.py(SCRIPTS / "gt_write_queue.py", "--vault", self.vault, "--path",
                    "Projects/alpha/design.md", "--op", "append", "--content", "from the shell",
                    "--session", "writer-1")
        self.assertOk(p)
        r = self.call("vault_queue_drain")
        self.assertEqual([x["decision"] for x in r["results"]], ["escalate"], r)
        self.assertEqual((self.proj / "design.md").read_text(encoding="utf-8"),
                         "# alpha design\n")
        conflicts = list((self.vault / "Projects/golden-thread/spool/broker/conflicts").glob("*.md"))
        self.assertEqual(len(conflicts), 1)
        self.assertIn("#conflict", (self.proj.parent / "golden-thread" / "README.md")
                      .read_text(encoding="utf-8") + (self.proj / "README.md").read_text(
                          encoding="utf-8"))


def _broker_decides_by_identity():
    """Has gt_broker.py the 0.20.1 identity fix (it imports the shared helper's review_target)?
    That fix is the broker's own branch (fix/0.20.1-broker-identity); until it is merged under
    this tree the broker compares exact names, and the inbox tests below cannot pass."""
    try:
        import gt_broker
        return hasattr(gt_broker, "review_target")
    except Exception:                                    # noqa: BLE001
        return False


@unittest.skipUnless(_broker_decides_by_identity(),
                     "needs the broker's identity fix (gt 0.20.1, fix/0.20.1-broker-identity): "
                     "this tree's gt_broker.py still compares exact names -- runs once merged")
class BrokerInboxAliases(OpsFixture):
    """Re-review m7: what the MCP tools refuse, a sandboxed SHELL can still ask for by leaving a
    request in ~/.gt-inbox/queue/. The broker must escalate every spelling of, and every link to,
    a review target -- never apply it."""

    def setUp(self):
        super().setUp()
        (self.proj / "design.md").write_text("# alpha design\n", encoding="utf-8")
        gm = self.vault / "global-memory"
        gm.mkdir(exist_ok=True)
        (gm / "MEMORY.md").write_text("# MEM\n", encoding="utf-8")
        (self.vault / "Knowledge").mkdir(exist_ok=True)
        self.inbox = self.home / ".gt-inbox" / "queue"
        self.inbox.mkdir(parents=True)
        self.n = 0

    def protected(self):
        gm = self.vault / "global-memory"
        return ((self.proj / "design.md").read_bytes(),
                sorted((q.name, q.read_bytes()) for q in gm.rglob("*") if q.is_file()))

    def leave(self, path, op="append", content="PWNED\n"):
        self.n += 1
        rid = "inbox-%d" % self.n
        req = {"schema": 1, "id": rid, "submitted": "2026-10-04T20:00:%02d+00:00" % self.n,
               "session": "s", "origin": "session", "path": path, "op": op, "section": None,
               "content": content, "key": None,
               "target_existed": os.path.exists(str(self.vault / path)), "base_sha256": None,
               "hint": None}
        (self.inbox / (rid + ".json")).write_text(
            json.dumps({"gt_inbox": 1, "vault": os.path.realpath(str(self.vault)),
                        "request": req}), encoding="utf-8")
        return rid

    def drain(self):
        r = self.call("vault_queue_drain")
        return {x["path"]: x["decision"] for x in r["results"]}, r

    def test_every_spelling_is_escalated_never_applied(self):
        before = self.protected()
        paths = ["Projects/alpha/DESIGN.md", "Projects/alpha/Design.MD",
                 "Projects/alpha/deſign.md", "Projects/alpha/ｄesign.md",
                 "Projects/alpha/design.md.", "GLOBAL-MEMORY/MEMORY.md",
                 "Global-Memory/new.md", "ｇlobal-memory/new2.md"]
        for q in paths:
            self.leave(q)
        got, r = self.drain()
        for q in paths:
            self.assertIn(got.get(q), ("escalate", "reject"), (q, r))
        for q in ("Projects/alpha/DESIGN.md", "Projects/alpha/deſign.md",
                  "GLOBAL-MEMORY/MEMORY.md"):
            self.assertEqual(got.get(q), "escalate", (q, r))
        self.assertEqual(self.protected(), before)
        for name in ("DESIGN.md", "Design.MD", "deſign.md"):       # nothing created beside it
            f = self.proj / name
            self.assertFalse(f.exists() and not os.path.samefile(str(f),
                                                                 str(self.proj / "design.md")), name)

    def test_every_link_is_escalated_never_followed(self):
        k = self.vault / "Knowledge"
        try:
            (k / "link.md").symlink_to(self.proj / "design.md")
            (self.proj / "gmlink").symlink_to(self.vault / "global-memory",
                                              target_is_directory=True)
            (k / "dangling.md").symlink_to("../global-memory/made-by-link.md")
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        before = self.protected()
        paths = ["Knowledge/link.md", "Projects/alpha/gmlink/MEMORY.md",
                 "Projects/alpha/gmlink/new.md", "Knowledge/dangling.md"]
        for q in paths:
            self.leave(q)
        self.leave("Knowledge/dangling.md", op="create", content="# made\n")
        got, r = self.drain()
        for q in paths:
            self.assertIn(got.get(q), ("escalate", "reject"), (q, r))
        self.assertEqual(self.protected(), before)
        self.assertFalse((self.vault / "global-memory" / "made-by-link.md").exists())

    def test_an_ordinary_inbox_request_still_applies(self):
        self.leave("Knowledge/FromShell.md", op="create", content="# From the shell\n")
        got, r = self.drain()
        self.assertEqual(got.get("Knowledge/FromShell.md"), "apply", r)


ALIASES = [
    "Projects/alpha/deſign.md",              # long s: U+017F == 's' on a case-insensitive volume
    "Projects/alpha/ｄｅｓｉｇｎ.md",           # fullwidth
    "Projects/alpha/DEſIGN.MD",
    "ｇlobal-memory/x.md",                    # fullwidth g
    "global‐memory/x.md".replace("‐", "-"),
    "Projects/alpha/des​ign.md",             # zero-width space
    "Projects/alpha/des­ign.md",             # soft hyphen
    "Projects/alpha/designㅤ.md",             # Hangul filler
    "Projects/alpha/de⠀sign.md",             # braille blank
    "Projects/alpha/design️.md",             # variation selector
    "Projects/alpha/design᠎.md",
    "Projects/alpha/design؜.md",             # Arabic letter mark
    "Projects/alpha/design\U000e0041.md",         # tag character
    "Projects/alpha/design￺.md",             # interlinear annotation
    "Projects/alpha/design․md".replace("․", "."),
    "Projects/alpha/design.md․",             # NFKC makes the one dot leader a dot
    "global-memory／x.md",                    # fullwidth solidus: NFKC makes a separator
    "Projects/alpha/DESIGN~1.MD", "GLOBAL~1/x.md",   # 8.3 short names
]
SPACED = [" Projects/alpha/design.md", " Projects/alpha/design.md",
          "Projects/alpha/design.md ", "  Projects/alpha/design.md  ",
          " global-memory/x.md", "global-memory/x.md "]


class ReviewAliases(OpsFixture):
    """Re-review B1/m1: the refusal is by IDENTITY and by the name as any file system reads it,
    not by a case-insensitive string compare."""

    def setUp(self):
        super().setUp()
        (self.proj / "design.md").write_text("# alpha design\n", encoding="utf-8")
        (self.vault / "global-memory").mkdir(exist_ok=True)
        (self.vault / "global-memory" / "x.md").write_text("# x\n", encoding="utf-8")
        (self.vault / "Knowledge").mkdir(exist_ok=True)
        (self.vault / "Knowledge" / "ok.md").write_text("# ok\n", encoding="utf-8")
        self.snap = self.snapshot()

    def snapshot(self):
        return {str(q.relative_to(self.vault)): q.read_bytes()
                for d in ("Projects/alpha", "global-memory", "Knowledge")
                for q in (self.vault / d).rglob("*") if q.is_file() and not q.is_symlink()}

    def ctx(self, gate=None):
        import gt_vault_mcp as M
        return OPS.Ctx(M.Vault(str(self.vault)), SESSION, gate or FakeGate())

    def test_the_shared_helper_folds_every_alias_to_the_real_name(self):
        self.assertEqual(RT.fold("de\u017fign.md"), "design.md")
        self.assertEqual(RT.fold("\uff47lobal-memory"), "global-memory")
        self.assertEqual(RT.fold("\ufb01le"), "file")                    # the fi ligature
        self.assertEqual(RT.fold(" design.md . "), "design.md")
        self.assertEqual(RT.fold("design.md::$DATA"), "design.md")
        # a tool argument is read more strictly than the helper reads a path on disk
        self.assertIn("global-memory/x", OPS._forms("global-memory\uff0fx"))
        self.assertIn("Projects/alpha/design.md", OPS._forms("Projects/alpha/design\u3164.md"))

    def test_the_decision_is_the_shared_helpers_not_a_second_rule(self):
        """gt_vault_ops asks gt_review_target (the broker's helper) and adds no rule of its own:
        whatever the helper calls a review target is refused, and nothing it clears is."""
        real = RT.review_target
        try:
            RT.review_target = lambda vault, rel: ("design.md", "x") if "quokka" in rel else None
            self.assertEqual(OPS.review_refusal(str(self.vault), "Knowledge/quokka.md"),
                             ("owner_only", OPS.REVIEW_MESSAGE))
            self.assertIsNone(OPS.review_refusal(str(self.vault), "Projects/alpha/design.md"))
            RT.review_target = lambda vault, rel: ("symlink", "a/b is a dangling symlink")
            code, msg = OPS.review_refusal(str(self.vault), "Knowledge/ok.md")
            self.assertEqual(code, "bad_path")
            self.assertIn("dangling symlink", msg)
        finally:
            RT.review_target = real

    def test_a_link_the_helper_will_not_vouch_for_is_refused_as_a_bad_path(self):
        """Not a review target, and still not written through: a symlink that leaves the folder
        it sits in (to a harmless file), and one that dangles. No unlock prompt for either."""
        k = self.vault / "Knowledge"
        try:
            (k / "out.md").symlink_to(self.proj / "research.md")
            (k / "dangling.md").symlink_to("nowhere.md")
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        before = (self.proj / "research.md").read_bytes()
        for t, word in (("Knowledge/out.md", "leaves its own directory"),
                        ("Knowledge/dangling.md", "dangling")):
            self.assertEqual(OPS.review_refusal(str(self.vault), t)[0], "bad_path", t)
            for tool, args in (("vault_derived_write", {"action": "review_stamp", "pages": [t]}),
                               ("vault_optimize", {"action": "supersede", "file": t,
                                                   "entry": "e", "by": "b", "apply": True})):
                g = FakeGate()
                with self.assertRaises(OPS.OpError, msg=(tool, t)) as cm:
                    OPS.call(tool, args, self.ctx(g))
                self.assertEqual(cm.exception.code, "bad_path", (tool, t))
                self.assertIn(word, cm.exception.message)
                self.assertEqual(g.asked, [], (tool, t))
        self.assertEqual((self.proj / "research.md").read_bytes(), before)
        self.assertFalse((k / "nowhere.md").exists())

    def test_a_path_that_is_the_global_memory_folder_itself_is_refused(self):
        for t in ("global-memory", "global-memory/", "GLOBAL-MEMORY"):
            self.assertEqual(OPS.review_refusal(str(self.vault), t),
                             ("owner_only", OPS.REVIEW_MESSAGE), t)
        self.assertIsNone(OPS.review_refusal(str(self.vault), "Knowledge"))
        self.assertIsNone(OPS.review_refusal(str(self.vault), "Projects/alpha"))

    def test_what_is_not_a_review_target_is_not_refused(self):
        for t in ("Projects/alpha/xdesign.md", "Projects/alpha/design.md.bak",
                  "Projects/alpha/design-notes.md", "global-memory-x/a.md",
                  "Projects/alpha/research.md", "Knowledge/ok.md"):
            self.assertIsNone(OPS.review_refusal(str(self.vault), t), t)

    def test_every_alias_is_refused_by_every_gate_before_any_prompt(self):
        for t in ALIASES + SPACED:
            hit = OPS.review_refusal(str(self.vault), t.strip())
            self.assertEqual(hit, ("owner_only", OPS.REVIEW_MESSAGE), repr(t))
            g = FakeGate()
            with self.assertRaises(OPS.OpError, msg=repr(t)) as cm:
                OPS.check_path(str(self.vault), t)
            self.assertEqual(cm.exception.code, "owner_only", repr(t))
        for t in ALIASES + SPACED:
            for tool, args in (("vault_derived_write", {"action": "review_stamp", "pages": [t]}),
                               ("vault_optimize", {"action": "supersede", "file": t,
                                                   "entry": "e", "by": "b", "apply": True}),
                               ("vault_handoff", {"action": "close", "file": t, "reason": "r"})):
                g = FakeGate()
                with self.assertRaises(OPS.OpError, msg=(tool, repr(t))) as cm:
                    OPS.call(tool, args, self.ctx(g))
                self.assertEqual(cm.exception.code, "owner_only", (tool, repr(t)))
                self.assertEqual(g.asked, [], (tool, repr(t)))
        self.assertEqual(self.snapshot(), self.snap)

    def test_vault_queue_write_refuses_every_alias_before_any_prompt(self):
        import io
        import gt_vault_mcp as M
        for t in ALIASES + SPACED:
            g = FakeGate()
            srv = M.Server(str(self.vault), out=io.StringIO(), gate=g, offered=True)
            r = srv.tools_call("vault_queue_write", {"path": t, "op": "append",
                                                     "content": "PWNED"})["structuredContent"]
            self.assertFalse(r["ok"], repr(t))
            self.assertEqual(r["error"]["code"], "owner_only", (repr(t), r))
            self.assertEqual(g.asked, [], repr(t))
        self.assertEqual(self.snapshot(), self.snap)

    def test_leading_unicode_whitespace_does_not_pass_the_pregate(self):
        for t in SPACED:
            if any(ch in t for ch in "\t\n\r"):
                continue
            g = FakeGate()
            with self.assertRaises(OPS.OpError, msg=repr(t)) as cm:
                OPS.call("vault_derived_write", {"action": "review_stamp", "pages": [t]},
                         self.ctx(g))
            self.assertEqual(cm.exception.code, "owner_only", repr(t))
            self.assertEqual(g.asked, [], repr(t))

    def test_a_hard_link_to_design_md_is_refused_by_identity_alone(self):
        link = self.proj / "notes.md"
        try:
            os.link(str(self.proj / "design.md"), str(link))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("no hard links here")
        self.assertEqual(RT.fold("notes.md"), "notes.md")           # nothing in the NAME says so
        self.assertEqual(OPS.review_refusal(str(self.vault), "Projects/alpha/notes.md"),
                         ("owner_only", OPS.REVIEW_MESSAGE))
        with self.assertRaises(OPS.OpError) as cm:
            OPS.check_path(str(self.vault), "Projects/alpha/notes.md")
        self.assertEqual(cm.exception.code, "owner_only")

    def test_a_folder_the_file_system_calls_global_memory_is_refused_by_identity(self):
        """What a case-insensitive or normalising volume does with 'GLOBAL-MEMORY' or a bind
        mount: the folder is another NAME for global-memory. Modelled with the file system's own
        answer (samefile) stubbed true for one pair -- no case-insensitive volume needed."""
        (self.vault / "Knowledge" / "Alias").mkdir()
        (self.vault / "Knowledge" / "Alias" / "n.md").write_text("n\n")
        real = RT._same
        RT._same = lambda a, b: os.path.basename(a) == "Alias" and \
            os.path.basename(b) == "global-memory" or real(a, b)
        try:
            self.assertEqual(OPS.review_refusal(str(self.vault), "Knowledge/Alias/n.md"),
                             ("owner_only", OPS.REVIEW_MESSAGE))
            self.assertIsNone(OPS.review_refusal(str(self.vault), "Knowledge/ok.md"))
        finally:
            RT._same = real

    def test_the_identity_check_is_what_catches_a_name_the_fold_cannot_know(self):
        """No name rule can know every alias a volume invents, so the identity layer is the one
        that must not be removable: with it stubbed off, a hard link passes the names."""
        link = self.proj / "other.md"
        try:
            os.link(str(self.proj / "design.md"), str(link))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("no hard links here")
        real = RT._same
        RT._same = lambda a, b: False
        try:
            self.assertIsNone(OPS.review_refusal(str(self.vault), "Projects/alpha/other.md"))
        finally:
            RT._same = real
        self.assertEqual(OPS.review_refusal(str(self.vault), "Projects/alpha/other.md"),
                         ("owner_only", OPS.REVIEW_MESSAGE))


class LinkedProjects(OpsFixture):
    """Re-review M2: a project folder that is a link (or holds one) is not a project."""

    def setUp(self):
        super().setUp()
        gm = self.vault / "global-memory"
        gm.mkdir(exist_ok=True)
        (gm / "README.md").write_text("# gm\n\n## Tasks\n", encoding="utf-8")
        self.gm_before = {q.name: q.read_bytes() for q in gm.rglob("*") if q.is_file()}

    def gm_untouched(self):
        gm = self.vault / "global-memory"
        self.assertEqual({q.name: q.read_bytes() for q in gm.rglob("*") if q.is_file()},
                         self.gm_before)

    def link(self, at, to, dir=False):
        try:
            Path(at).symlink_to(to, target_is_directory=dir)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")

    def test_a_project_that_is_a_link_to_global_memory_is_refused_everywhere(self):
        self.link(self.vault / "Projects" / "gmproj", self.vault / "global-memory", dir=True)
        for tool, args in (
                ("vault_project", {"action": "create", "name": "gmproj"}),
                ("vault_task", {"action": "add", "text": "t", "project": "gmproj"}),
                ("vault_adr", {"action": "allocate", "project": "gmproj", "title": "T"}),
                ("vault_handoff", {"action": "write", "project": "gmproj"}),
                ("vault_closeout", {"action": "ask", "project": "gmproj"}),
                ("vault_project", {"action": "archive", "project": "gmproj", "reason": "r"}),
                ("vault_optimize", {"action": "archive", "project": "gmproj",
                                    "before": "2026-01-01", "apply": True}),
                ("vault_derived_write", {"action": "digest", "project": "gmproj"})):
            r = self.err(tool, args)
            self.assertIn(r["error"]["code"], ("bad_path", "owner_only"), (tool, r))
        self.gm_untouched()

    def test_a_project_behind_any_link_is_refused_not_only_to_global_memory(self):
        other = self.tmp / "elsewhere"
        other.mkdir()
        self.link(self.vault / "Projects" / "outside", other, dir=True)
        self.err("vault_task", {"action": "add", "text": "t", "project": "outside"}, "bad_path")
        self.assertEqual(list(other.iterdir()), [])

    def test_a_dangling_design_md_link_is_not_followed_by_create(self):
        proj = self.vault / "Projects" / "alpha2"
        proj.mkdir()
        self.link(proj / "design.md", "../../global-memory/gm-new.md")
        self.assertFalse(os.path.exists(str(proj / "design.md")))
        r = self.err("vault_project", {"action": "create", "name": "alpha2"}, "bad_path")
        self.assertIn("design.md", r["error"]["message"])
        self.assertFalse((self.vault / "global-memory" / "gm-new.md").exists())
        self.gm_untouched()

    def test_the_scaffold_itself_never_follows_a_dangling_link(self):
        """The same repro with the tool the skill's shell step runs: vault_init's ensure_file."""
        proj = self.vault / "Projects" / "alpha3"
        proj.mkdir()
        self.link(proj / "design.md", "../../global-memory/gm-new3.md")
        p = self.py(SCRIPTS / "vault_init.py", "create-project", "--vault", self.vault,
                    "--name", "alpha3")
        self.assertFalse((self.vault / "global-memory" / "gm-new3.md").exists(),
                         p.stdout + p.stderr)
        self.assertTrue(os.path.islink(str(proj / "design.md")))
        self.gm_untouched()

    def test_a_symlink_in_memory_is_refused_too(self):
        proj = self.vault / "Projects" / "alpha4"
        (proj / "memory").mkdir(parents=True)
        self.link(proj / "memory" / "MEMORY.md", "../../../global-memory/m.md")
        self.err("vault_project", {"action": "create", "name": "alpha4"}, "bad_path")
        self.assertFalse((self.vault / "global-memory" / "m.md").exists())

    def test_ordinary_projects_are_untouched_by_the_check(self):
        r = self.call("vault_task", {"action": "add", "text": "ok", "project": "alpha"})
        self.assertTrue(r["ok"], r)
        r = self.call("vault_project", {"action": "create", "name": "fine", "parent": "alpha"})
        self.assertTrue(r["ok"], r)


class MoreFixes(OpsFixture):
    def test_every_invisible_source_name_character_is_refused(self):
        for ch in ("؜", "\U000e0041", "\U000e0001", "­", "ㅤ", "⠀", "️",
                   "᠎", "￹", "￺", "￻", "‍", "⁤", "\U000e0100",
                   "", "͸"):
            self.err("vault_source_store", {"name": "2026-10-04 a%sb.md" % ch, "content": "x"},
                     "bad_request")
        self.assertEqual(sorted(q.name for q in (self.vault / "Sources").glob("2026-10-04*")), [])

    def test_the_adr_heading_guard_catches_every_spelling(self):
        for body in ("## ADR 9: x", "##ADR-9", "   ## ADR-9: x", "## adr-9", "\t##\tADR_9",
                     "### ADR-9", "ok\n  ## Adr 12"):
            self.err("vault_adr", {"action": "allocate", "project": "alpha", "title": "T",
                                   "body": body}, "bad_request")
        # prose that merely mentions one is fine
        OPS.check_args("vault_adr", {"action": "allocate", "project": "alpha", "title": "T",
                                     "body": "we kept ADR-9 as it was"})

    def test_the_wiki_queue_never_overwrites_a_ticked_wiki_queue_either(self):
        (self.vault / "review-queue.md").write_text("# Vault Review Queue\n\n- [x] mine\n")
        wf = self.vault / "review-queue-wiki.md"
        wf.write_text("# Review queue\n\nRegenerated by wiki-lint. Pending: 1\n\n- [x] ticked\n")
        r = self.err("vault_derived_write", {"action": "wiki_lint_queue"}, "held")
        self.assertIn("ticks", r["error"]["message"])
        self.assertIn("- [x] ticked", wf.read_text(encoding="utf-8"))

    def test_derived_write_is_marked_destructive(self):
        a = {t["name"]: t["annotations"] for t in OPS.TOOLS}
        self.assertIs(a["vault_derived_write"]["destructiveHint"], True)


if __name__ == "__main__":
    unittest.main()
